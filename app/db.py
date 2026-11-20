"""SQLite storage. One shared connection guarded by a lock (single gunicorn worker)."""
import json
import sqlite3
import threading
import time
from typing import Any

from .config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    sort_title    TEXT,
    authors       TEXT NOT NULL DEFAULT '[]',   -- JSON list
    author_sort   TEXT,                         -- "Surname, First & Surname2, First2"
    series        TEXT,
    series_index  REAL,
    description   TEXT,
    publisher     TEXT,
    published     TEXT,
    language      TEXT,
    isbn          TEXT,
    tags          TEXT NOT NULL DEFAULT '[]',   -- JSON list
    rating        REAL,                         -- 0..10 (lubimyczytac scale)
    pages         INTEGER,
    translator    TEXT,
    lc_url        TEXT,                         -- lubimyczytac.pl book page
    cover         TEXT,                         -- file name in covers dir
    cover_color   TEXT,                         -- dominant cover colour, '#rrggbb'
    file_path     TEXT NOT NULL UNIQUE,         -- relative to LIBRARY_DIR
    format        TEXT NOT NULL,                -- format of the primary file
    formats       TEXT,                         -- all formats, e.g. "epub,kepub"
    size          INTEGER,
    md5           TEXT,
    meta_status   TEXT NOT NULL DEFAULT 'none', -- none|matched|manual|nomatch
    added_at      REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_books_added ON books(added_at);
CREATE INDEX IF NOT EXISTS idx_books_md5 ON books(md5);

CREATE TABLE IF NOT EXISTS book_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    file_path   TEXT NOT NULL UNIQUE,           -- relative to LIBRARY_DIR
    format      TEXT NOT NULL,
    size        INTEGER,
    md5         TEXT,
    added_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_book ON book_files(book_id);
CREATE INDEX IF NOT EXISTS idx_files_md5 ON book_files(md5);

CREATE TABLE IF NOT EXISTS authors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,           -- exactly as used in books.authors
    lc_url      TEXT,
    photo       TEXT,                           -- file name in DATA_DIR/authors
    bio         TEXT,
    born        TEXT,
    died        TEXT,
    status      TEXT NOT NULL DEFAULT 'none',   -- none|ok|nomatch|error
    fetched_at  REAL
);

CREATE TABLE IF NOT EXISTS shelves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    color       TEXT,
    icon        TEXT,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shelf_books (
    shelf_id    INTEGER NOT NULL REFERENCES shelves(id) ON DELETE CASCADE,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    added_at    REAL NOT NULL,
    PRIMARY KEY (shelf_id, book_id)
);

CREATE TABLE IF NOT EXISTS downloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    md5         TEXT NOT NULL,
    title       TEXT,
    author      TEXT,
    ext         TEXT,
    status      TEXT NOT NULL DEFAULT 'queued', -- queued|downloading|done|error
    progress    REAL NOT NULL DEFAULT 0,
    error       TEXT,
    book_id     INTEGER,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""

JSON_FIELDS = ("authors", "tags")
BOOK_FIELDS = (
    "title", "sort_title", "authors", "author_sort", "series", "series_index", "description", "publisher",
    "published", "language", "isbn", "tags", "rating", "pages", "translator", "lc_url", "cover", "cover_color",
    "file_path", "format", "formats", "size", "md5", "meta_status",
)


# ---------------------------------------------------------------- author sort

PARTICLES = {"de", "da", "di", "du", "del", "della", "der", "den", "des", "la", "le", "van", "von",
             "ten", "ter", "dos", "das", "y", "st.", "al", "el", "bin", "ibn"}
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "phd", "md"}


def author_sort_name(name: str) -> str:
    """'Andrzej Sapkowski' -> 'Sapkowski, Andrzej'; 'Ursula K. Le Guin' -> 'Le Guin, Ursula K.'"""
    name = " ".join((name or "").split())
    if not name or "," in name:            # already "Surname, First" (or a corporate name)
        return name
    parts = name.split(" ")
    suffix = ""
    if len(parts) > 2 and parts[-1].lower().strip(",") in SUFFIXES:
        suffix = " " + parts.pop()
    if len(parts) < 2:
        return name + suffix
    i = len(parts) - 1                     # surname starts here; pull in particles before it
    while i > 1 and parts[i - 1].lower() in PARTICLES:
        i -= 1
    surname, first = " ".join(parts[i:]), " ".join(parts[:i])
    return f"{surname}, {first}{suffix}"


def authors_sort(authors: list[str]) -> str:
    return " & ".join(author_sort_name(a) for a in authors if a)


FORMAT_PREFERENCE = ["epub", "kepub", "azw3", "mobi", "fb2", "pdf", "djvu", "cbz", "txt"]


def _order_formats(fmts) -> list[str]:
    uniq = list(dict.fromkeys(fmts))
    return sorted(uniq, key=lambda f: FORMAT_PREFERENCE.index(f) if f in FORMAT_PREFERENCE else 99)


def _migrate(c: sqlite3.Connection) -> None:
    cols = {r[1] for r in c.execute("PRAGMA table_info(books)")}
    if "author_sort" not in cols:
        c.execute("ALTER TABLE books ADD COLUMN author_sort TEXT")
    if "formats" not in cols:
        c.execute("ALTER TABLE books ADD COLUMN formats TEXT")
    if "cover_color" not in cols:
        c.execute("ALTER TABLE books ADD COLUMN cover_color TEXT")
    # every book's file also lives in book_files (books.file_path = the primary file)
    c.execute("INSERT INTO book_files (book_id, file_path, format, size, md5, added_at) "
              "SELECT id, file_path, format, size, md5, added_at FROM books "
              "WHERE file_path NOT IN (SELECT file_path FROM book_files)")
    c.execute("UPDATE book_files SET format = 'kepub' WHERE lower(file_path) LIKE '%.kepub.epub' "
              "AND format != 'kepub'")
    c.execute("UPDATE books SET format = 'kepub' WHERE lower(file_path) LIKE '%.kepub.epub' "
              "AND format != 'kepub'")
    for (bid,) in c.execute("SELECT id FROM books WHERE formats IS NULL").fetchall():
        fmts = [r[0] for r in c.execute("SELECT format FROM book_files WHERE book_id = ?", (bid,))]
        c.execute("UPDATE books SET formats = ? WHERE id = ?", (",".join(_order_formats(fmts)), bid))
    for row in c.execute("SELECT id, authors FROM books WHERE author_sort IS NULL").fetchall():
        try:
            names = json.loads(row[1] or "[]")
        except ValueError:
            names = []
        c.execute("UPDATE books SET author_sort = ? WHERE id = ?", (authors_sort(names), row[0]))


_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.ensure_dirs()
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
            _conn.commit()
        return _conn


def reset_for_tests() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


def query(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, params).fetchall()


def execute(sql: str, params: tuple | list = ()) -> int:
    with _lock:
        cur = conn().execute(sql, params)
        conn().commit()
        return cur.lastrowid


# ---------------------------------------------------------------- books

def book_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in JSON_FIELDS:
        try:
            d[f] = json.loads(d.get(f) or "[]")
        except ValueError:
            d[f] = []
    return d


def _encode(fields: dict[str, Any]) -> dict[str, Any]:
    out = {}
    if fields.get("authors") is not None and not fields.get("author_sort"):
        fields = {**fields, "author_sort": authors_sort(fields["authors"])}
    for k, v in fields.items():
        if k not in BOOK_FIELDS:
            continue
        if k in JSON_FIELDS:
            v = json.dumps([x for x in (v or []) if x], ensure_ascii=False)
        out[k] = v
    return out


def insert_book(fields: dict[str, Any]) -> int:
    data = _encode(fields)
    now = time.time()
    data["added_at"] = now
    data["updated_at"] = now
    data.setdefault("formats", data.get("format"))
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    book_id = execute(f"INSERT INTO books ({cols}) VALUES ({marks})", list(data.values()))
    if data.get("file_path"):
        execute("INSERT OR IGNORE INTO book_files (book_id, file_path, format, size, md5, added_at) "
                "VALUES (?,?,?,?,?,?)", (book_id, data["file_path"], data["format"], data.get("size"),
                                         data.get("md5"), now))
    return book_id


def update_book(book_id: int, fields: dict[str, Any]) -> None:
    data = _encode(fields)
    if not data:
        return
    data["updated_at"] = time.time()
    sets = ", ".join(f"{k} = ?" for k in data)
    execute(f"UPDATE books SET {sets} WHERE id = ?", [*data.values(), book_id])


def get_book(book_id: int) -> dict | None:
    rows = query("SELECT * FROM books WHERE id = ?", (book_id,))
    return book_to_dict(rows[0]) if rows else None


def delete_book(book_id: int) -> None:
    execute("DELETE FROM books WHERE id = ?", (book_id,))


# ---------------------------------------------------------------- files (formats)

def list_files(book_id: int) -> list[dict]:
    rows = [dict(r) for r in query("SELECT * FROM book_files WHERE book_id = ?", (book_id,))]
    order = {f: i for i, f in enumerate(FORMAT_PREFERENCE)}
    return sorted(rows, key=lambda r: (order.get(r["format"], 99), r["id"]))


def get_file(file_id: int) -> dict | None:
    rows = query("SELECT * FROM book_files WHERE id = ?", (file_id,))
    return dict(rows[0]) if rows else None


def add_file(book_id: int, file_path: str, fmt: str, size: int | None, md5: str | None) -> int:
    fid = execute("INSERT INTO book_files (book_id, file_path, format, size, md5, added_at) "
                  "VALUES (?,?,?,?,?,?)", (book_id, file_path, fmt, size, md5, time.time()))
    sync_primary(book_id)
    return fid


def delete_file_row(file_id: int) -> None:
    execute("DELETE FROM book_files WHERE id = ?", (file_id,))


def has_md5(md5: str) -> int | None:
    rows = query("SELECT book_id FROM book_files WHERE md5 = ?", (md5,))
    return rows[0][0] if rows else None


def library_md5s() -> set[str]:
    return {r[0] for r in query("SELECT md5 FROM book_files WHERE md5 IS NOT NULL")}


def sync_primary(book_id: int) -> None:
    """Point books.file_path/format/size/md5 at the preferred file and refresh `formats`."""
    files = list_files(book_id)
    if not files:
        return
    p = files[0]
    with _lock:
        c = conn()
        # file_path is UNIQUE on books – only another *book* could clash, never the same row
        c.execute("UPDATE books SET file_path = ?, format = ?, size = ?, md5 = ?, formats = ? "
                  "WHERE id = ?", (p["file_path"], p["format"], p["size"], p["md5"],
                                   ",".join(_order_formats(f["format"] for f in files)), book_id))
        c.commit()


SORTS = {
    "added": "added_at DESC",
    "title": "COALESCE(sort_title, title) COLLATE NOCASE ASC",
    "author": "COALESCE(author_sort, authors) COLLATE NOCASE ASC, series COLLATE NOCASE ASC, "
              "series_index ASC, COALESCE(sort_title, title) COLLATE NOCASE ASC",
    "author_first": "authors COLLATE NOCASE ASC, COALESCE(sort_title, title) COLLATE NOCASE ASC",
    "rating": "COALESCE(rating, 0) DESC",
    "series": "series COLLATE NOCASE ASC, series_index ASC",
}


def search_books(q: str = "", author: str | None = None, series: str | None = None,
                 tag: str | None = None, sort: str = "added", limit: int = 60,
                 offset: int = 0, shelf: int | None = None) -> tuple[list[dict], int]:
    where, params = [], []
    if shelf is not None:
        where.append("id IN (SELECT book_id FROM shelf_books WHERE shelf_id = ?)")
        params.append(shelf)
        if sort == "added":
            sort = "shelf"
    if q:
        for term in q.split():
            like = f"%{term}%"
            where.append("(title LIKE ? OR authors LIKE ? OR series LIKE ? OR tags LIKE ? OR isbn LIKE ?)")
            params += [like] * 5
    if author:
        where.append("EXISTS (SELECT 1 FROM json_each(books.authors) WHERE value = ?)")
        params.append(author)
    if series:
        where.append("series = ?")
        params.append(series)
    if tag:
        where.append("EXISTS (SELECT 1 FROM json_each(books.tags) WHERE value = ?)")
        params.append(tag)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    if sort == "shelf" and shelf is not None:
        order = ("(SELECT position FROM shelf_books sb WHERE sb.book_id = books.id "
                 "AND sb.shelf_id = ?) ASC, added_at DESC")
        params_order = [shelf]
    else:
        order = SORTS.get(sort, SORTS["added"])
        params_order = []
    total = query(f"SELECT COUNT(*) FROM books {clause}", params)[0][0]
    rows = query(f"SELECT * FROM books {clause} ORDER BY {order} LIMIT ? OFFSET ?",
                 [*params, *params_order, limit, offset])
    return [book_to_dict(r) for r in rows], total


# ---------------------------------------------------------------- shelves

def list_shelves() -> list[dict]:
    rows = query(
        "SELECT s.*, (SELECT COUNT(*) FROM shelf_books sb WHERE sb.shelf_id = s.id) AS count, "
        "(SELECT b.cover FROM shelf_books sb JOIN books b ON b.id = sb.book_id "
        " WHERE sb.shelf_id = s.id AND b.cover IS NOT NULL ORDER BY sb.position LIMIT 1) AS cover_hint "
        "FROM shelves s ORDER BY s.name COLLATE NOCASE")
    out = []
    for r in rows:
        d = dict(r)
        d["covers"] = [x[0] for x in query(
            "SELECT b.id FROM shelf_books sb JOIN books b ON b.id = sb.book_id "
            "WHERE sb.shelf_id = ? AND b.cover IS NOT NULL ORDER BY sb.position, sb.added_at LIMIT 4",
            (d["id"],))]
        out.append(d)
    return out


def get_shelf(shelf_id: int) -> dict | None:
    rows = query("SELECT * FROM shelves WHERE id = ?", (shelf_id,))
    return dict(rows[0]) if rows else None


def create_shelf(name: str, description: str = "", color: str = "", icon: str = "") -> int:
    return execute("INSERT INTO shelves (name, description, color, icon, created_at) VALUES (?,?,?,?,?)",
                   (name.strip(), description, color, icon, time.time()))


def update_shelf(shelf_id: int, **fields: Any) -> None:
    fields = {k: v for k, v in fields.items() if k in ("name", "description", "color", "icon")}
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        execute(f"UPDATE shelves SET {sets} WHERE id = ?", [*fields.values(), shelf_id])


def delete_shelf(shelf_id: int) -> None:
    execute("DELETE FROM shelves WHERE id = ?", (shelf_id,))


def add_to_shelf(shelf_id: int, book_id: int) -> None:
    pos = query("SELECT COALESCE(MAX(position), 0) + 1 FROM shelf_books WHERE shelf_id = ?",
                (shelf_id,))[0][0]
    execute("INSERT OR IGNORE INTO shelf_books (shelf_id, book_id, position, added_at) VALUES (?,?,?,?)",
            (shelf_id, book_id, pos, time.time()))


def remove_from_shelf(shelf_id: int, book_id: int) -> None:
    execute("DELETE FROM shelf_books WHERE shelf_id = ? AND book_id = ?", (shelf_id, book_id))


def reorder_shelf(shelf_id: int, book_ids: list[int]) -> None:
    with _lock:
        c = conn()
        for i, bid in enumerate(book_ids):
            c.execute("UPDATE shelf_books SET position = ? WHERE shelf_id = ? AND book_id = ?",
                      (i, shelf_id, bid))
        c.commit()


def shelves_for_book(book_id: int) -> list[int]:
    return [r[0] for r in query("SELECT shelf_id FROM shelf_books WHERE book_id = ?", (book_id,))]


def rename_tag(old: str, new: str) -> int:
    """Rename (or merge, if `new` exists) a tag across all books. Empty `new` deletes it."""
    changed = 0
    for r in query("SELECT id, tags FROM books WHERE EXISTS "
                   "(SELECT 1 FROM json_each(books.tags) WHERE value = ?)", (old,)):
        tags = json.loads(r["tags"])
        out: list[str] = []
        for t in tags:
            t2 = new if t == old else t
            if t2 and t2 not in out:
                out.append(t2)
        update_book(r["id"], {"tags": out})
        changed += 1
    return changed


def canonical_tags(tags: list[str]) -> list[str]:
    """Reuse the spelling already in the library ('fantasy' vs 'Fantasy'); drop case-duplicates."""
    existing: dict[str, tuple[int, str]] = {}
    for name, count in facet("tags"):
        k = name.lower()
        if k not in existing or count > existing[k][0]:
            existing[k] = (count, name)
    out: list[str] = []
    for t in tags or []:
        t = existing.get(t.lower(), (0, t))[1]
        if t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out


def merge_tag_case() -> int:
    """Fold tags that differ only in capitals into the most used spelling. Returns tags merged."""
    groups: dict[str, list[tuple[str, int]]] = {}
    for name, count in facet("tags"):
        groups.setdefault(name.lower(), []).append((name, count))
    merged = 0
    for variants in groups.values():
        if len(variants) < 2:
            continue
        keep = max(variants, key=lambda v: v[1])[0]
        for name, _ in variants:
            if name != keep:
                rename_tag(name, keep)
                merged += 1
    return merged


def stats() -> dict:
    return {
        "books": query("SELECT COUNT(*) FROM books")[0][0],
        "files": query("SELECT COUNT(*) FROM book_files")[0][0],
        "bytes": query("SELECT COALESCE(SUM(size), 0) FROM book_files")[0][0],
        "authors": query("SELECT COUNT(DISTINCT value) FROM books, json_each(books.authors)")[0][0],
        "shelves": query("SELECT COUNT(*) FROM shelves")[0][0],
        "tags": query("SELECT COUNT(DISTINCT value) FROM books, json_each(books.tags)")[0][0],
        "unmatched": query("SELECT COUNT(*) FROM books WHERE meta_status IN ('none','nomatch')")[0][0],
    }


def facet(kind: str) -> list[tuple[str, int]]:
    if kind == "authors":
        rows = query("SELECT value, COUNT(*) c FROM books, json_each(books.authors) GROUP BY value")
        return sorted(((r[0], r[1]) for r in rows), key=lambda x: author_sort_name(x[0]).lower())
    elif kind == "tags":
        sql = ("SELECT value, COUNT(*) c FROM books, json_each(books.tags) "
               "GROUP BY value ORDER BY value COLLATE NOCASE")
    elif kind == "series":
        sql = ("SELECT series, COUNT(*) c FROM books WHERE series IS NOT NULL AND series != '' "
               "GROUP BY series ORDER BY series COLLATE NOCASE")
    else:
        raise ValueError(kind)
    return [(r[0], r[1]) for r in query(sql)]


# ---------------------------------------------------------------- downloads

def add_download(md5: str, title: str, author: str, ext: str) -> int:
    now = time.time()
    return execute(
        "INSERT INTO downloads (md5, title, author, ext, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (md5, title, author, ext, now, now),
    )


def update_download(dl_id: int, **fields: Any) -> None:
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k} = ?" for k in fields)
    execute(f"UPDATE downloads SET {sets} WHERE id = ?", [*fields.values(), dl_id])


def list_downloads(limit: int = 100) -> list[dict]:
    return [dict(r) for r in query("SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (limit,))]


def next_queued_download() -> dict | None:
    rows = query("SELECT * FROM downloads WHERE status = 'queued' ORDER BY id ASC LIMIT 1")
    return dict(rows[0]) if rows else None
