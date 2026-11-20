"""Author profiles (portrait, bio, dates) from lubimyczytac.pl, cached in the database."""
import logging
import threading
import time
from pathlib import Path

from . import db, ebookmeta, http
from .config import config

log = logging.getLogger("bookhoard.authors")
_job = {"running": False, "done": 0, "total": 0}
_job_lock = threading.Lock()


def photo_dir() -> Path:
    d = config.DATA_DIR / "authors"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get(name: str) -> dict | None:
    rows = db.query("SELECT * FROM authors WHERE name = ?", (name,))
    return dict(rows[0]) if rows else None


def ensure(name: str) -> dict:
    db.execute("INSERT OR IGNORE INTO authors (name) VALUES (?)", (name,))
    return get(name)


def remember_url(name: str, url: str) -> None:
    """Called when a book is matched – its author links are the most reliable source."""
    a = ensure(name)
    if not a.get("lc_url"):
        db.execute("UPDATE authors SET lc_url = ? WHERE id = ?", (url, a["id"]))


def refresh(name: str) -> dict:
    from . import lubimyczytac
    a = ensure(name)
    url = a.get("lc_url")
    try:
        if not url:
            best = lubimyczytac.find_author(name)
            if not best or best["score"] < 0.8:
                db.execute("UPDATE authors SET status = 'nomatch', fetched_at = ? WHERE id = ?",
                           (time.time(), a["id"]))
                return get(name)
            url = best["url"]
        info = lubimyczytac.fetch_author(url)
        photo = a.get("photo")
        if info.get("photo_url"):
            try:
                data = http.get(info["photo_url"], timeout=20).content
                fname = f"{a['id']}.jpg"
                if ebookmeta.save_cover(data, photo_dir() / fname, max_height=600):
                    photo = fname
            except Exception as e:
                log.info("author photo download failed for %s: %s", name, e)
        db.execute("UPDATE authors SET lc_url = ?, photo = ?, bio = ?, born = ?, died = ?, "
                   "status = 'ok', fetched_at = ? WHERE id = ?",
                   (url, photo, info.get("bio") or None, info.get("born"), info.get("died"),
                    time.time(), a["id"]))
    except Exception as e:
        log.warning("author lookup failed for %s: %s", name, e)
        db.execute("UPDATE authors SET status = 'error', fetched_at = ? WHERE id = ?",
                   (time.time(), a["id"]))
    return get(name)


def listing() -> list[dict]:
    """Every author in the library with book count, photo and a few cover ids."""
    rows = db.query("SELECT value AS name, COUNT(*) AS count FROM books, json_each(books.authors) "
                    "GROUP BY value")
    info = {r["name"]: dict(r) for r in db.query("SELECT * FROM authors")}
    out = []
    for r in rows:
        a = info.get(r["name"], {})
        covers = [x[0] for x in db.query(
            "SELECT id FROM books WHERE cover IS NOT NULL AND EXISTS "
            "(SELECT 1 FROM json_each(books.authors) WHERE value = ?) ORDER BY added_at DESC LIMIT 3",
            (r["name"],))]
        out.append({"name": r["name"], "count": r["count"], "sort": db.author_sort_name(r["name"]),
                    "id": a.get("id"), "photo": bool(a.get("photo")), "status": a.get("status", "none"),
                    "covers": covers})
    return sorted(out, key=lambda x: x["sort"].lower())


def fetch_all_missing() -> dict:
    """Background job: fetch profiles for authors never looked up."""
    with _job_lock:
        if _job["running"]:
            return dict(_job)
        names = [a["name"] for a in listing() if a["status"] == "none"]
        _job.update(running=True, done=0, total=len(names))

    def run():
        for n in names:
            refresh(n)
            with _job_lock:
                _job["done"] += 1
        with _job_lock:
            _job["running"] = False

    threading.Thread(target=run, name="author-profiles", daemon=True).start()
    return dict(_job)


def job_status() -> dict:
    with _job_lock:
        return dict(_job)
