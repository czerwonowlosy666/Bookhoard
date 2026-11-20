"""Importing files into the library, applying metadata, and the ingest-folder watcher."""
import hashlib
import logging
import re
import shutil
import threading
import time
import unicodedata
from pathlib import Path

from . import db, ebookmeta
from .config import config

log = logging.getLogger("bookhoard.library")
_import_lock = threading.Lock()


def safe_name(s: str, maxlen: int = 80) -> str:
    s = unicodedata.normalize("NFC", s or "").strip()
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:maxlen].rstrip(" .")) or "Unknown"


def sort_title(title: str) -> str:
    return re.sub(r"^(the|a|an)\s+", "", (title or "").strip(), flags=re.I)


def md5sum(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_format(path: Path) -> str:
    """'x.kepub.epub' → 'kepub' (Kobo's convention); otherwise the extension."""
    name = path.name.lower()
    if name.endswith(".kepub.epub") or name.endswith(".kepub"):
        return "kepub"
    return path.suffix.lower().lstrip(".")


def file_suffix(fmt: str) -> str:
    return ".kepub.epub" if fmt == "kepub" else f".{fmt}"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("ł", "l")
    s = re.sub(r"\((kepub|epub|mobi|azw3|pdf)\)|\[(kepub|epub)\]", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def book_key(title: str, authors: list[str]) -> str:
    """Identity used to decide that two files are the same book in different formats."""
    names = sorted(_norm(" ".join(sorted(_norm_word(a)))) for a in authors or [] if a)
    return _norm(title) + "|" + ",".join(names)


def _norm_word(author: str) -> list[str]:
    # "Lem, Stanisław" and "Stanisław Lem" → same set of words
    return [w for w in re.split(r"[\s,]+", unicodedata.normalize("NFKD", author.lower())) if w]


def _names_agree(paths_a: list[str], paths_b: list[str]) -> bool:
    """False only when file names clearly belong to different books (guards against merging
    books whose metadata is wrong)."""
    from .repair import identity_from_path, _same_identity
    ia = [i for i in (identity_from_path(p) for p in paths_a) if i]
    ib = [i for i in (identity_from_path(p) for p in paths_b) if i]
    if not ia or not ib:
        return True
    return any(_same_identity(x, y) for x in ia for y in ib)


def find_book_for(title: str, authors: list[str], fmt: str, rel_path: str | None = None) -> int | None:
    """An existing book with the same title+authors that doesn't have this format yet."""
    key = book_key(title, authors)
    if key.startswith("|"):
        return None
    for r in db.query("SELECT id, title, authors, formats FROM books"):
        b = db.book_to_dict(r)
        if book_key(b["title"], b["authors"]) == key and fmt not in (b.get("formats") or "").split(","):
            if rel_path and not _names_agree([rel_path], [f["file_path"] for f in db.list_files(b["id"])]):
                continue
            return b["id"]
    return None


def _target_path(meta: dict, fmt: str, folder: Path | None = None) -> Path:
    author = safe_name(meta["authors"][0] if meta.get("authors") else "Unknown author")
    title = safe_name(meta.get("title") or "Untitled")
    folder = folder or (config.LIBRARY_DIR / author / title)
    suffix = file_suffix(fmt)
    target = folder / f"{title} - {author}{suffix}"
    n = 2
    while target.exists():
        target = folder / f"{title} - {author} ({n}){suffix}"
        n += 1
    return target


def cover_file(book_id: int) -> Path:
    return config.COVERS_DIR / f"{book_id}.jpg"


def set_cover(book_id: int, data: bytes) -> dict:
    """Save a cover image for a book; returns the fields to store (cover + its colour)."""
    path = cover_file(book_id)
    if not ebookmeta.save_cover(data, path):
        return {}
    return {"cover": f"{book_id}.jpg", "cover_color": ebookmeta.dominant_color(path)}


def backfill_cover_colors() -> int:
    n = 0
    for r in db.query("SELECT id FROM books WHERE cover IS NOT NULL AND cover_color IS NULL"):
        path = cover_file(r[0])
        if path.exists():
            db.update_book(r[0], {"cover_color": ebookmeta.dominant_color(path) or ""})
            n += 1
    return n


def import_file(src: Path, move: bool = True, md5: str | None = None,
                extra_meta: dict | None = None) -> int | None:
    """Import one e-book file. Returns the book id it now belongs to.

    A file whose book is already in the library in another format (same title + authors)
    is attached to that book as an extra format instead of becoming a second entry.
    """
    fmt = file_format(src)
    if fmt not in config.ALLOWED_FORMATS:
        log.info("skipping unsupported file %s", src)
        return None
    attached = False
    with _import_lock:
        digest = md5 or md5sum(src)
        dup = db.has_md5(digest)
        if dup:
            log.info("duplicate %s (book %s)", src.name, dup)
            if move:
                src.unlink(missing_ok=True)
            return dup

        meta = ebookmeta.read_metadata(src)
        if extra_meta:  # e.g. title/author from Anna's Archive when the file has none
            for k, v in extra_meta.items():
                if v and not meta.get(k):
                    meta[k] = v

        existing = find_book_for(meta.get("title", ""), meta.get("authors", []), fmt, src.name)
        if existing:
            book = db.get_book(existing)
            folder = (config.LIBRARY_DIR / book["file_path"]).parent
            target = _target_path(book, fmt, folder)
        else:
            target = _target_path(meta, fmt)
        target.parent.mkdir(parents=True, exist_ok=True)
        (shutil.move if move else shutil.copy2)(str(src), str(target))
        rel = str(target.relative_to(config.LIBRARY_DIR))

        if existing:
            db.add_file(existing, rel, fmt, target.stat().st_size, digest)
            book_id, attached = existing, True
            log.info("added %s to book %s as an extra format", target.name, book_id)
        else:
            book_id = db.insert_book({
                **meta,
                "sort_title": sort_title(meta.get("title", "")),
                "file_path": rel, "format": fmt, "size": target.stat().st_size, "md5": digest,
            })
            cover = ebookmeta.extract_cover(target)
            if cover:
                db.update_book(book_id, set_cover(book_id, cover))
            log.info("imported %s as book %s", target.name, book_id)

    if attached:
        write_back(db.get_book(book_id))  # give the new file the book's (matched) metadata
    elif config.LC_AUTO_METADATA:
        threading.Thread(target=auto_match, args=(book_id,), daemon=True).start()
    return book_id


# ------------------------------------------------------------------ merging formats

def _keeper_rank(b: dict) -> tuple:
    status = {"matched": 0, "manual": 1}.get(b.get("meta_status"), 2)
    filled = sum(1 for k in ("description", "series", "isbn", "publisher", "cover", "lc_url") if b.get(k))
    return (status, -filled, b["added_at"])


def merge_books(keeper_id: int, other_id: int) -> None:
    """Move all files, shelves and missing metadata of `other` into `keeper`, then drop `other`."""
    keeper, other = db.get_book(keeper_id), db.get_book(other_id)
    if not keeper or not other or keeper_id == other_id:
        return
    fill = {}
    for k in ("description", "series", "series_index", "publisher", "published", "language", "isbn",
              "rating", "pages", "translator", "lc_url"):
        if not keeper.get(k) and other.get(k):
            fill[k] = other[k]
    tags = list(dict.fromkeys((keeper.get("tags") or []) + (other.get("tags") or [])))
    if tags != (keeper.get("tags") or []):
        fill["tags"] = tags
    if not keeper.get("cover") and other.get("cover") and cover_file(other_id).exists():
        shutil.move(str(cover_file(other_id)), str(cover_file(keeper_id)))
        fill["cover"] = f"{keeper_id}.jpg"
        fill["cover_color"] = other.get("cover_color")
    with db._lock:
        c = db.conn()
        c.execute("UPDATE book_files SET book_id = ? WHERE book_id = ?", (keeper_id, other_id))
        c.execute("INSERT OR IGNORE INTO shelf_books (shelf_id, book_id, position, added_at) "
                  "SELECT shelf_id, ?, position, added_at FROM shelf_books WHERE book_id = ?",
                  (keeper_id, other_id))
        c.execute("UPDATE downloads SET book_id = ? WHERE book_id = ?", (keeper_id, other_id))
        c.execute("DELETE FROM books WHERE id = ?", (other_id,))
        c.commit()
    cover_file(other_id).unlink(missing_ok=True)
    if fill:
        db.update_book(keeper_id, fill)
    db.sync_primary(keeper_id)


def merge_duplicates() -> int:
    """Merge books that are the same title+authors in different formats. Returns merges done."""
    groups: dict[str, list[dict]] = {}
    for r in db.query("SELECT * FROM books"):
        b = db.book_to_dict(r)
        key = book_key(b["title"], b["authors"])
        if not key.startswith("|"):
            groups.setdefault(key, []).append(b)
    merged = 0
    for books in groups.values():
        if len(books) < 2:
            continue
        books.sort(key=_keeper_rank)
        keepers: list[dict] = []
        for b in books:
            fmts = set((b.get("formats") or b["format"]).split(","))
            paths = [f["file_path"] for f in db.list_files(b["id"])]
            target = next((k for k in keepers if not (k["_fmts"] & fmts)
                           and _names_agree(paths, [f["file_path"] for f in db.list_files(k["id"])])), None)
            if target is None:  # same format twice = genuinely different editions – keep both
                b["_fmts"] = fmts
                keepers.append(b)
                continue
            merge_books(target["id"], b["id"])
            target["_fmts"] |= fmts
            merged += 1
            log.info("merged book %s into %s (%s)", b["id"], target["id"], ",".join(sorted(fmts)))
    return merged


def auto_match(book_id: int) -> None:
    from . import lubimyczytac
    book = db.get_book(book_id)
    if not book:
        return
    try:
        details = lubimyczytac.match(book["title"], book["authors"], book.get("isbn"))
    except Exception as e:  # network trouble shouldn't break imports
        log.warning("lubimyczytac lookup failed for %s: %s", book_id, e)
        return
    if details:
        apply_metadata(book_id, details, status="matched")
    else:
        db.update_book(book_id, {"meta_status": "nomatch"})


def apply_metadata(book_id: int, meta: dict, status: str = "manual",
                   fields: list[str] | None = None) -> dict | None:
    """Apply metadata (from lubimyczytac or a manual edit) to a book, including cover + file."""
    book = db.get_book(book_id)
    if not book:
        return None
    allowed = ["title", "authors", "series", "series_index", "description", "publisher",
               "published", "language", "isbn", "tags", "rating", "pages", "translator", "lc_url"]
    if fields:
        allowed = [f for f in allowed if f in fields or f == "lc_url"]
    update = {k: meta[k] for k in allowed if k in meta and meta[k] not in (None, "", [])}
    if "tags" in update:
        update["tags"] = db.canonical_tags(update["tags"])
    if "title" in update:
        update["sort_title"] = sort_title(update["title"])
    update["meta_status"] = status

    cover_url = meta.get("cover_url")
    if cover_url and (not fields or "cover" in fields):
        from . import http
        try:
            data = http.get(cover_url, timeout=20).content
            update.update(set_cover(book_id, data))
        except Exception as e:
            log.warning("cover download failed: %s", e)

    db.update_book(book_id, update)
    if meta.get("author_urls"):
        from . import authors
        for name, url in meta["author_urls"].items():
            authors.remember_url(name, url)
    book = db.get_book(book_id)
    write_back(book)
    return book


def write_back(book: dict) -> None:
    """Embed metadata in the EPUB so KOReader shows the same title/author/series."""
    if not config.WRITE_EPUB_METADATA or not book:
        return
    for f in db.list_files(book["id"]):
        if f["format"] not in ("epub", "kepub"):
            continue
        path = config.LIBRARY_DIR / f["file_path"]
        try:
            ebookmeta.write_epub(path, book)
            db.execute("UPDATE book_files SET size = ? WHERE id = ?", (path.stat().st_size, f["id"]))
        except Exception as e:
            log.warning("could not write metadata into %s: %s", path, e)
    db.sync_primary(book["id"])


def delete_book(book_id: int, delete_file: bool = True) -> None:
    book = db.get_book(book_id)
    if not book:
        return
    if delete_file:
        for f in db.list_files(book_id):
            _remove_from_disk(config.LIBRARY_DIR / f["file_path"])
    cover_file(book_id).unlink(missing_ok=True)
    db.delete_book(book_id)


def _remove_from_disk(path: Path) -> None:
    path.unlink(missing_ok=True)
    for parent in (path.parent, path.parent.parent):  # tidy up empty author/title folders
        try:
            if parent != config.LIBRARY_DIR and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass


def delete_file(book_id: int, file_id: int, delete_from_disk: bool = True) -> bool:
    """Remove one format. Deleting the last format deletes the book. Returns True if book remains."""
    f = db.get_file(file_id)
    if not f or f["book_id"] != book_id:
        return db.get_book(book_id) is not None
    if len(db.list_files(book_id)) <= 1:
        delete_book(book_id, delete_file=delete_from_disk)
        return False
    if delete_from_disk:
        _remove_from_disk(config.LIBRARY_DIR / f["file_path"])
    db.delete_file_row(file_id)
    db.sync_primary(book_id)
    return True


# ------------------------------------------------------------------ watchers

def scan_ingest_once() -> int:
    count = 0
    now = time.time()
    for p in sorted(config.INGEST_DIR.rglob("*")):
        if not p.is_file() or p.name.startswith(".") or p.suffix in (".part", ".tmp", ".crdownload"):
            continue
        if now - p.stat().st_mtime < 5:  # still being written
            continue
        try:
            if import_file(p, move=True):
                count += 1
        except Exception as e:
            log.exception("import failed for %s: %s", p, e)
            failed = config.INGEST_DIR / "_failed"
            failed.mkdir(exist_ok=True)
            shutil.move(str(p), str(failed / p.name))
    return count


def scan_library_once() -> int:
    """Pick up files dropped straight into the library folder, and forget missing ones."""
    known = {r[0] for r in db.query("SELECT file_path FROM book_files")}
    count = 0
    for p in sorted(config.LIBRARY_DIR.rglob("*")):
        if not p.is_file():
            continue
        fmt = file_format(p)
        if fmt not in config.ALLOWED_FORMATS:
            continue
        rel = str(p.relative_to(config.LIBRARY_DIR))
        if rel in known:
            continue
        digest = md5sum(p)
        if db.has_md5(digest):
            continue
        meta = ebookmeta.read_metadata(p)
        existing = find_book_for(meta.get("title", ""), meta.get("authors", []), fmt, rel)
        if existing:
            db.add_file(existing, rel, fmt, p.stat().st_size, digest)
        else:
            book_id = db.insert_book({**meta, "sort_title": sort_title(meta.get("title", "")),
                                      "file_path": rel, "format": fmt,
                                      "size": p.stat().st_size, "md5": digest})
            cover = ebookmeta.extract_cover(p)
            if cover:
                db.update_book(book_id, set_cover(book_id, cover))
        count += 1
    # forget files that disappeared; books with no files left go too
    for f in db.query("SELECT id, book_id, file_path FROM book_files"):
        if not (config.LIBRARY_DIR / f["file_path"]).exists():
            db.delete_file_row(f["id"])
            if db.list_files(f["book_id"]):
                db.sync_primary(f["book_id"])
            else:
                db.delete_book(f["book_id"])
                cover_file(f["book_id"]).unlink(missing_ok=True)
    return count


def start_watcher() -> None:
    def loop():
        try:
            n = scan_library_once()
            if n:
                log.info("library scan found %s new files", n)
            m = merge_duplicates()
            if m:
                log.info("merged %s duplicate books into multi-format entries", m)
            backfill_cover_colors()
        except Exception:
            log.exception("library scan failed")
        while True:
            try:
                scan_ingest_once()
            except Exception:
                log.exception("ingest scan failed")
            time.sleep(config.SCAN_INTERVAL)

    threading.Thread(target=loop, name="ingest-watcher", daemon=True).start()
