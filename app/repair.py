"""Repair a library whose metadata was overwritten by wrong lubimyczytac matches.

The file names are the source of truth: Calibre and Bookhoard both name files
"Title - Author.ext" (Calibre also uses Author/Title (id)/ folders), and those names
were never touched. For every book we compare its files' names with its metadata:

  * files whose names belong to different books get split into separate books
  * books whose title/author no longer match their files get their title/author restored
    from the file (from the file's own metadata if that is still intact, otherwise from
    the file name), wrong fields cleared and the original cover re-extracted
  * files whose embedded metadata was overwritten get it reset
Nothing changes until apply() is called with the ids from plan().
"""
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from . import db, ebookmeta, library
from .config import config

log = logging.getLogger("bookhoard.repair")
SUFFIX_RE = re.compile(r"(\.kepub)?\.(epub|kepub|mobi|azw3|azw|fb2|pdf|djvu|cbz|cbr|txt|zip)$", re.I)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b or a.startswith(b) or b.startswith(a):  # file names are often truncated
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _person(name: str) -> str:
    """'Ćwirlej, Ryszard' → 'Ryszard Ćwirlej'."""
    name = name.strip().strip("_").strip()
    if name.count(",") == 1:
        last, first = (x.strip() for x in name.split(","))
        if last and first:
            return f"{first} {last}"
    return name


def _clean_title(t: str) -> str:
    t = re.sub(r"\s*\(\d+\)$", "", t.strip())          # "(2)" duplicate counters, Calibre ids
    t = re.sub(r"_ ", ": ", t).replace("_", " ")        # Calibre writes ':' as '_'
    return re.sub(r"\s+", " ", t).strip(" .-")


def identity_from_path(rel: str) -> dict | None:
    """Title + authors encoded in a library-relative file path, or None if unknown."""
    p = Path(rel)
    stem = SUFFIX_RE.sub("", p.name)
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    if re.fullmatch(r"[0-9a-f]{32}", stem):  # temporary md5 names – no information
        return None
    if " - " in stem:
        title, author = stem.rsplit(" - ", 1)
        authors = [_person(a) for a in re.split(r"\s*&\s*|\s*;\s*", author) if a.strip()]
        title = _clean_title(title)
        if title and authors:
            return {"title": title, "authors": authors}
    # fall back to folders: Author/Title (id)/file.ext
    parts = p.parts
    if len(parts) >= 3:
        return {"title": _clean_title(parts[-2]), "authors": [_person(parts[-3])]}
    return None


def _matches(ident: dict, title: str, authors: list[str]) -> bool:
    if _sim(ident["title"], title) < 0.75:
        return False
    if not authors or not ident["authors"]:
        return True
    words = lambda xs: {w for x in xs for w in _norm(x).split() if len(w) > 2}
    return bool(words(ident["authors"]) & words(authors))


def _same_identity(a: dict, b: dict) -> bool:
    return _matches(a, b["title"], b["authors"])


def _file_meta(path: Path) -> dict:
    try:
        return ebookmeta.read_metadata(path)
    except Exception:
        return {}


def tag_case_duplicates() -> list[list[str]]:
    groups: dict[str, list[str]] = {}
    for name, _ in db.facet("tags"):
        groups.setdefault(name.lower(), []).append(name)
    return [v for v in groups.values() if len(v) > 1]


def plan() -> list[dict]:
    """Proposed repairs, one entry per book that needs one."""
    out = []
    for row in db.query("SELECT id FROM books ORDER BY id"):
        book = db.get_book(row[0])
        files = db.list_files(book["id"])
        groups: list[dict] = []  # files grouped by the book their name says they belong to
        for f in files:
            ident = identity_from_path(f["file_path"])
            if ident is None:
                ident = {"title": book["title"], "authors": book["authors"], "unknown": True}
            g = next((g for g in groups if _same_identity(ident, g["ident"])), None)
            if g:
                g["files"].append(f)
            else:
                groups.append({"ident": ident, "files": [f]})
        mismatched = [g for g in groups if not g["ident"].get("unknown")
                      and not _matches(g["ident"], book["title"], book["authors"])]
        if len(groups) == 1 and not mismatched:
            continue
        actions = []
        for g in groups:
            ok = g["ident"].get("unknown") or _matches(g["ident"], book["title"], book["authors"])
            actions.append({
                "title": g["ident"]["title"], "authors": g["ident"]["authors"],
                "files": [{"id": f["id"], "format": f["format"], "name": Path(f["file_path"]).name}
                          for f in g["files"]],
                "restore": not ok,
            })
        # the group matching the book's current metadata keeps the book; else the first one
        keep = next((i for i, a in enumerate(actions) if not a["restore"]), 0)
        for i, a in enumerate(actions):
            a["keeps_book"] = i == keep
        out.append({
            "book_id": book["id"], "current_title": book["title"], "current_authors": book["authors"],
            "split": len(groups) > 1, "groups": actions,
        })
    return out


def _restore_fields(book_id: int, title: str, authors: list[str], files: list[dict]) -> None:
    """Reset a book to what its files say, and fix files whose metadata was overwritten."""
    fields = {"title": title, "authors": authors, "sort_title": library.sort_title(title),
              "series": None, "series_index": None, "description": None, "publisher": None,
              "published": None, "isbn": None, "rating": None, "pages": None, "translator": None,
              "lc_url": None, "tags": [], "meta_status": "none", "cover": None, "cover_color": None}
    good_meta = None
    for f in files:
        path = config.LIBRARY_DIR / f["file_path"]
        meta = _file_meta(path)
        intact = meta.get("title") and _sim(meta["title"], title) >= 0.75
        if intact and good_meta is None:
            good_meta = meta
        elif not intact and f["format"] in ("epub", "kepub") and path.exists():
            try:
                ebookmeta.reset_epub(path, title, authors)
                db.execute("UPDATE book_files SET size = ? WHERE id = ?", (path.stat().st_size, f["id"]))
            except Exception as e:
                log.warning("could not reset metadata in %s: %s", path, e)
    if good_meta:  # a file still has its own, untouched metadata – use it
        for k in ("series", "series_index", "description", "publisher", "language", "isbn", "tags"):
            if good_meta.get(k):
                fields[k] = good_meta[k]
        if good_meta.get("published"):
            fields["published"] = good_meta["published"]
    library.cover_file(book_id).unlink(missing_ok=True)
    for f in files:  # original cover from the file itself
        data = ebookmeta.extract_cover(config.LIBRARY_DIR / f["file_path"])
        if data:
            fields.update(library.set_cover(book_id, data))
            break
    db.update_book(book_id, fields)


def apply(book_ids: list[int] | None = None) -> dict:
    import uuid
    items = plan()
    if book_ids is not None:
        wanted = set(book_ids)
        items = [i for i in items if i["book_id"] in wanted]
    restored = split = 0
    for item in items:
        bid = item["book_id"]
        if not db.get_book(bid):
            continue
        targets: list[tuple[int, dict]] = []
        for g in item["groups"]:
            ids = [f["id"] for f in g["files"]]
            if g["keeps_book"]:
                targets.append((bid, g))
                continue
            first = db.get_file(ids[0])
            placeholder = f"__repair__/{uuid.uuid4().hex}"  # books.file_path is unique
            target = db.insert_book({"title": g["title"], "authors": g["authors"],
                                     "sort_title": library.sort_title(g["title"]),
                                     "file_path": placeholder, "format": first["format"],
                                     "size": first["size"], "md5": first["md5"]})
            db.execute("DELETE FROM book_files WHERE file_path = ?", (placeholder,))
            for fid in ids:
                db.execute("UPDATE book_files SET book_id = ? WHERE id = ?", (target, fid))
            targets.append((target, g))
            split += 1
        db.sync_primary(bid)  # the original book first, so its old main file is released
        for target, g in targets:
            if target != bid:
                db.sync_primary(target)
        for target, g in targets:
            if g["restore"] or target != bid:
                files = [f for f in (db.get_file(x["id"]) for x in g["files"]) if f]
                _restore_fields(target, g["title"], g["authors"], files)
                db.sync_primary(target)
                restored += 1
    merged = library.merge_duplicates()  # re-pair EPUB + KEPUB of the same (now correct) book
    tags = db.merge_tag_case()
    log.info("repair: %s books restored, %s split off, %s re-paired, %s tag spellings merged",
             restored, split, merged, tags)
    return {"restored": restored, "split": split, "merged": merged, "tags": tags}
