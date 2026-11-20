"""Background job: refresh book tags from lubimyczytac.pl.

Modes:  replace – the book's tags become exactly lubimyczytac's
        merge   – keep the book's tags and add lubimyczytac's missing ones
With `review=True` the job only collects proposed changes; `apply()` writes them later.
"""
import logging
import threading
import time

from . import db, library
from .config import config

log = logging.getLogger("bookhoard.tagsync")
_lock = threading.Lock()
_cancel = threading.Event()
_state: dict = {"running": False}


def _new_state(**kw) -> dict:
    return {"running": True, "total": 0, "done": 0, "changed": 0, "unchanged": 0, "nomatch": 0,
            "notags": 0, "errors": 0, "applied": False, "started": time.time(), "finished": None,
            "results": [], **kw}


def status() -> dict:
    with _lock:
        s = dict(_state)
        s["results"] = list(_state.get("results", []))
        return s


def start(mode: str = "replace", book_ids: list[int] | None = None, match_unmatched: bool = True,
          review: bool = True) -> dict:
    if mode not in ("replace", "merge"):
        raise ValueError("mode must be 'replace' or 'merge'")
    with _lock:
        if _state.get("running"):
            raise RuntimeError("A tag refresh is already running.")
        ids = book_ids or [r[0] for r in db.query("SELECT id FROM books ORDER BY id")]
        _state.clear()
        _state.update(_new_state(mode=mode, review=review, match_unmatched=match_unmatched,
                                 total=len(ids)))
    _cancel.clear()
    threading.Thread(target=_run, args=(ids, mode, match_unmatched, review),
                     name="tag-sync", daemon=True).start()
    return status()


def cancel() -> None:
    _cancel.set()


def _new_tags(old: list[str], lc: list[str], mode: str) -> list[str]:
    lc = db.canonical_tags(lc)
    if mode == "replace":
        return list(dict.fromkeys(lc))
    lower = {t.lower() for t in old}
    return old + [t for t in dict.fromkeys(lc) if t.lower() not in lower]


def _run(ids: list[int], mode: str, match_unmatched: bool, review: bool) -> None:
    from . import lubimyczytac
    for bid in ids:
        if _cancel.is_set():
            break
        book = db.get_book(bid)
        if not book:
            _bump("done")
            continue
        entry = {"id": bid, "title": book["title"], "authors": book["authors"],
                 "old": book["tags"], "new": None, "status": None}
        try:
            url, details = book.get("lc_url"), None
            if url:
                details = lubimyczytac.fetch_book(url)
                # a link saved by an older version may point at the wrong book – check it
                if lubimyczytac.verify(details, book["title"], book["authors"]) < config.LC_MIN_SCORE:
                    url, details = None, None
            if not url and match_unmatched:
                details = lubimyczytac.match(book["title"], book["authors"], book.get("isbn"))
                if details:
                    url = details["lc_url"]
                    db.update_book(bid, {"lc_url": url})
            if not url:
                entry["status"] = "nomatch"
            else:
                lc_tags = details.get("tags") or []
                entry["lc_url"] = url
                if not lc_tags:
                    entry["status"] = "notags"
                else:
                    new = _new_tags(book["tags"], lc_tags, mode)
                    entry["new"] = new
                    if new == book["tags"]:
                        entry["status"] = "unchanged"
                    else:
                        entry["status"] = "changed"
                        if not review:
                            _write(bid, new)
        except Exception as e:
            log.warning("tag refresh failed for book %s: %s", bid, e)
            entry["status"], entry["error"] = "errors", str(e)[:200]
        with _lock:
            _state["done"] += 1
            key = entry["status"]
            _state[key] = _state.get(key, 0) + 1
            if entry["status"] != "unchanged":  # keep the list focused on what matters
                _state["results"].append(entry)
    with _lock:
        _state["running"] = False
        _state["finished"] = time.time()
        _state["cancelled"] = _cancel.is_set()
        _state["applied"] = not review
    log.info("tag refresh finished: %s changed, %s unmatched", _state["changed"], _state["nomatch"])


def _bump(key: str) -> None:
    with _lock:
        _state[key] = _state.get(key, 0) + 1


def _write(book_id: int, tags: list[str]) -> None:
    db.update_book(book_id, {"tags": tags})
    library.write_back(db.get_book(book_id))


def apply(book_ids: list[int] | None = None) -> int:
    """Apply reviewed changes (optionally only for some books). Returns how many were written."""
    with _lock:
        if _state.get("running"):
            raise RuntimeError("Wait for the refresh to finish first.")
        todo = [r for r in _state.get("results", [])
                if r["status"] == "changed" and not r.get("applied")
                and (book_ids is None or r["id"] in book_ids)]
    for r in todo:
        if db.get_book(r["id"]):
            _write(r["id"], r["new"])
        r["applied"] = True
    with _lock:
        _state["applied"] = all(r.get("applied") for r in _state.get("results", [])
                                if r["status"] == "changed")
    return len(todo)
