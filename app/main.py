"""Flask application: auth, JSON API for the web UI, file/cover serving."""
import hmac
import io
import logging
import os
import tempfile
from pathlib import Path

from flask import Blueprint, Flask, Response, abort, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from . import annas, authors, db, libgen, library, lubimyczytac, opds, repair, tagsync
from .config import config

log = logging.getLogger("bookhoard")
api = Blueprint("api", __name__)
STATIC = Path(__file__).parent / "static"


# ------------------------------------------------------------------ auth

def _authorized() -> bool:
    if not config.APP_PASSWORD:
        return True
    auth = request.authorization
    return bool(auth) and hmac.compare_digest(auth.username or "", config.APP_USER) and \
        hmac.compare_digest(auth.password or "", config.APP_PASSWORD)


def _require_auth():
    if request.path in ("/healthz", "/manifest.webmanifest") or request.path.startswith("/static/"):
        return None
    if not _authorized():
        return Response("Authentication required", 401,
                        {"WWW-Authenticate": f'Basic realm="{config.APP_NAME}", charset="UTF-8"'})
    return None


# ------------------------------------------------------------------ helpers

def _book_or_404(book_id: int) -> dict:
    b = db.get_book(book_id)
    if not b:
        abort(404)
    return b


def _with_shelves(b: dict) -> dict:
    b["shelves"] = db.shelves_for_book(b["id"])
    b["files"] = [{k: f[k] for k in ("id", "format", "size", "file_path")} for f in db.list_files(b["id"])]
    return b


# ------------------------------------------------------------------ books

@api.get("/api/stats")
def stats():
    return jsonify({**db.stats(), "app_name": config.APP_NAME,
                    "aa_api_key": bool(config.AA_API_KEY),
                    "aa_default_lang": config.AA_DEFAULT_LANG,
                    "aa_default_ext": config.AA_DEFAULT_EXT,
                    "search_source": config.SEARCH_SOURCE})


@api.get("/api/books")
def list_books():
    a = request.args
    books, total = db.search_books(
        q=a.get("q", "").strip(), author=a.get("author") or None, series=a.get("series") or None,
        tag=a.get("tag") or None, sort=a.get("sort", "added"),
        shelf=a.get("shelf", type=int), limit=min(a.get("limit", 60, type=int), 500),
        offset=a.get("offset", 0, type=int))
    for b in books:
        b.pop("description", None)
    return jsonify({"total": total, "books": books})


@api.get("/api/books/<int:book_id>")
def get_book(book_id: int):
    return jsonify(_with_shelves(_book_or_404(book_id)))


@api.patch("/api/books/<int:book_id>")
def edit_book(book_id: int):
    old = _book_or_404(book_id)
    data = request.get_json(force=True) or {}
    if "authors" in data and isinstance(data["authors"], str):
        data["authors"] = [x.strip() for x in data["authors"].split("&") if x.strip()]
    if "author_sort" in data:
        data["author_sort"] = (data["author_sort"] or "").strip()
        # authors changed but the sort field still holds the old value → recompute it
        if "authors" in data and data["authors"] != old["authors"] and \
                data["author_sort"] == (old.get("author_sort") or ""):
            data["author_sort"] = ""
        if not data["author_sort"]:
            data["author_sort"] = db.authors_sort(data.get("authors", old["authors"]))
    if "tags" in data and isinstance(data["tags"], str):
        data["tags"] = [x.strip() for x in data["tags"].split(",") if x.strip()]
    for num in ("series_index", "rating", "pages"):
        if num in data:
            try:
                data[num] = float(data[num]) if data[num] not in ("", None) else None
            except (TypeError, ValueError):
                data[num] = None
    if data.get("title"):
        data["sort_title"] = library.sort_title(data["title"])
    data["meta_status"] = "manual"
    db.update_book(book_id, data)
    book = db.get_book(book_id)
    library.write_back(book)
    return jsonify(_with_shelves(db.get_book(book_id)))


@api.delete("/api/books/<int:book_id>")
def remove_book(book_id: int):
    _book_or_404(book_id)
    library.delete_book(book_id, delete_file=request.args.get("keep_file") != "1")
    return jsonify({"ok": True})


@api.get("/api/books/<int:book_id>/cover")
def cover(book_id: int):
    path = library.cover_file(book_id)
    if not path.exists():
        abort(404)
    if request.args.get("thumb"):
        from PIL import Image
        img = Image.open(path)
        img.thumbnail((300, 450))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        buf.seek(0)
        resp = send_file(buf, mimetype="image/jpeg")
    else:
        resp = send_file(path, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@api.get("/api/books/<int:book_id>/file")
@api.get("/download/<int:book_id>/<path:filename>")
@api.get("/download/<int:book_id>/f/<int:file_id>/<path:filename>")
def download(book_id: int, filename: str | None = None, file_id: int | None = None):
    _book_or_404(book_id)
    file_id = file_id or request.args.get("file_id", type=int)
    files = db.list_files(book_id)
    f = next((x for x in files if x["id"] == file_id), None) if file_id else (files[0] if files else None)
    if f is None:
        abort(404)
    path = config.LIBRARY_DIR / f["file_path"]
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename or path.name,
                     mimetype=opds.MIME.get(f["format"], "application/octet-stream"))


@api.delete("/api/books/<int:book_id>/files/<int:file_id>")
def remove_format(book_id: int, file_id: int):
    _book_or_404(book_id)
    remains = library.delete_file(book_id, file_id, delete_from_disk=request.args.get("keep_file") != "1")
    return jsonify(_with_shelves(db.get_book(book_id)) if remains else {"deleted": True})


@api.post("/api/books/merge")
def merge_selected():
    """Merge hand-picked books into one entry (the one with the best metadata is kept)."""
    ids = [int(x) for x in (request.get_json(force=True) or {}).get("book_ids", [])]
    books = [b for b in (db.get_book(i) for i in ids) if b]
    if len(books) < 2:
        return jsonify({"error": "Select at least two books to merge."}), 400
    books.sort(key=library._keeper_rank)
    for other in books[1:]:
        library.merge_books(books[0]["id"], other["id"])
    keeper = db.get_book(books[0]["id"])
    library.write_back(keeper)
    return jsonify(_with_shelves(keeper))


@api.get("/api/maintenance/repair")
def repair_plan():
    return jsonify({"books": repair.plan(), "tag_case": repair.tag_case_duplicates()})


@api.post("/api/maintenance/repair")
def repair_apply():
    d = request.get_json(force=True) or {}
    ids = [int(x) for x in d["book_ids"]] if d.get("book_ids") is not None else None
    if ids == [] and d.get("tags_only"):
        return jsonify({"restored": 0, "split": 0, "merged": 0, "tags": db.merge_tag_case()})
    return jsonify(repair.apply(ids))


@api.post("/api/maintenance/merge-formats")
def merge_formats():
    return jsonify({"merged": library.merge_duplicates()})


@api.post("/api/upload")
def upload():
    files = request.files.getlist("files")
    ids = []
    for f in files:
        name = secure_filename(f.filename or "") or "upload"
        # keep non-ASCII names readable (secure_filename strips Polish letters)
        ext = Path(f.filename or "").suffix.lower()
        stem = library.safe_name(Path(f.filename or name).stem)
        tmpdir = Path(tempfile.mkdtemp(dir=config.DATA_DIR))
        dest = tmpdir / f"{stem}{ext}"
        f.save(dest)
        try:
            bid = library.import_file(dest, move=True)
            if bid:
                ids.append(bid)
        finally:
            dest.unlink(missing_ok=True)
            tmpdir.rmdir()
    return jsonify({"imported": ids})


@api.get("/api/books/<int:book_id>/related")
def related(book_id: int):
    b = _book_or_404(book_id)
    series = []
    if b.get("series"):
        series, _ = db.search_books(series=b["series"], sort="series", limit=100)
    by_author = []
    if b["authors"]:
        books, _ = db.search_books(author=b["authors"][0], sort="series", limit=60)
        ids = {x["id"] for x in series} | {book_id}
        by_author = [x for x in books if x["id"] not in ids][:24]
    for x in series + by_author:
        x.pop("description", None)
    return jsonify({"series": series, "by_author": by_author})


# ------------------------------------------------------------------ authors

@api.get("/api/authors")
def authors_list():
    return jsonify({"authors": authors.listing(), "job": authors.job_status()})


@api.get("/api/authors/profile")
def author_profile():
    name = request.args.get("name", "")
    if not name:
        abort(400)
    a = authors.ensure(name)
    if a["status"] == "none" and request.args.get("fetch") == "1":
        a = authors.refresh(name)
    books, total = db.search_books(author=name, sort="series", limit=500)
    for x in books:
        x.pop("description", None)
    return jsonify({**a, "books": books, "total": total, "sort": db.author_sort_name(name)})


@api.post("/api/authors/refresh")
def author_refresh():
    name = (request.get_json(force=True) or {}).get("name", "")
    if not name:
        abort(400)
    return jsonify(authors.refresh(name))


@api.post("/api/authors/fetch-all")
def authors_fetch_all():
    return jsonify(authors.fetch_all_missing())


@api.get("/api/authors/<int:author_id>/photo")
def author_photo(author_id: int):
    rows = db.query("SELECT photo FROM authors WHERE id = ?", (author_id,))
    if not rows or not rows[0][0]:
        abort(404)
    path = authors.photo_dir() / rows[0][0]
    if not path.exists():
        abort(404)
    resp = send_file(path, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@api.get("/api/facets/<kind>")
def facets(kind: str):
    if kind not in ("authors", "series", "tags"):
        abort(404)
    return jsonify([{"name": n, "count": c} for n, c in db.facet(kind)])


@api.post("/api/tags/rename")
def tag_rename():
    data = request.get_json(force=True) or {}
    n = db.rename_tag(data.get("old", ""), (data.get("new") or "").strip())
    return jsonify({"changed": n})


@api.get("/api/tags/sync")
def tag_sync_status():
    return jsonify(tagsync.status())


@api.post("/api/tags/sync")
def tag_sync_start():
    d = request.get_json(force=True) or {}
    ids = [int(x) for x in d.get("book_ids") or []] or None
    try:
        return jsonify(tagsync.start(mode=d.get("mode", "replace"), book_ids=ids,
                                     match_unmatched=bool(d.get("match_unmatched", True)),
                                     review=bool(d.get("review", True))))
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 409


@api.post("/api/tags/sync/cancel")
def tag_sync_cancel():
    tagsync.cancel()
    return jsonify({"ok": True})


@api.post("/api/tags/sync/apply")
def tag_sync_apply():
    d = request.get_json(force=True) or {}
    ids = [int(x) for x in d["book_ids"]] if d.get("book_ids") is not None else None
    try:
        return jsonify({"applied": tagsync.apply(ids)})
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409


@api.post("/api/rescan")
def rescan():
    return jsonify({"ingested": library.scan_ingest_once(), "found": library.scan_library_once()})


# ------------------------------------------------------------------ metadata (lubimyczytac)

@api.get("/api/books/<int:book_id>/lc/candidates")
def lc_candidates(book_id: int):
    b = _book_or_404(book_id)
    try:
        return jsonify(lubimyczytac.candidates(b["title"], b["authors"], request.args.get("q") or None))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@api.get("/api/lc/book")
def lc_book():
    url = request.args.get("url", "")
    if "lubimyczytac.pl/ksiazka/" not in url:
        abort(400)
    try:
        return jsonify(lubimyczytac.fetch_book(url))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@api.post("/api/books/<int:book_id>/lc/apply")
def lc_apply(book_id: int):
    _book_or_404(book_id)
    data = request.get_json(force=True) or {}
    try:
        meta = data.get("meta") or lubimyczytac.fetch_book(data["url"])
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    book = library.apply_metadata(book_id, meta, status="matched", fields=data.get("fields"))
    return jsonify(_with_shelves(book))


@api.post("/api/lc/match-all")
def lc_match_all():
    import threading
    ids = [r[0] for r in db.query("SELECT id FROM books WHERE meta_status = 'none'")]

    def run():
        for i in ids:
            library.auto_match(i)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"queued": len(ids)})


# ------------------------------------------------------------------ shelves

@api.get("/api/shelves")
def shelves():
    return jsonify(db.list_shelves())


@api.post("/api/shelves")
def create_shelf():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    if db.query("SELECT 1 FROM shelves WHERE name = ?", (name,)):
        return jsonify({"error": "a shelf with this name already exists"}), 409
    sid = db.create_shelf(name, data.get("description", ""), data.get("color", ""), data.get("icon", ""))
    return jsonify(db.get_shelf(sid))


@api.patch("/api/shelves/<int:shelf_id>")
def edit_shelf(shelf_id: int):
    if not db.get_shelf(shelf_id):
        abort(404)
    data = request.get_json(force=True) or {}
    if "order" in data:
        db.reorder_shelf(shelf_id, [int(x) for x in data.pop("order")])
    db.update_shelf(shelf_id, **data)
    return jsonify(db.get_shelf(shelf_id))


@api.delete("/api/shelves/<int:shelf_id>")
def del_shelf(shelf_id: int):
    db.delete_shelf(shelf_id)
    return jsonify({"ok": True})


@api.post("/api/shelves/<int:shelf_id>/books")
def shelf_add(shelf_id: int):
    if not db.get_shelf(shelf_id):
        abort(404)
    data = request.get_json(force=True) or {}
    for bid in data.get("book_ids", []):
        db.add_to_shelf(shelf_id, int(bid))
    return jsonify({"ok": True})


@api.delete("/api/shelves/<int:shelf_id>/books/<int:book_id>")
def shelf_remove(shelf_id: int, book_id: int):
    db.remove_from_shelf(shelf_id, book_id)
    return jsonify({"ok": True})


# ------------------------------------------------------------------ Anna's Archive

@api.get("/api/aa/search")
def aa_search():
    a = request.args
    q = a.get("q", "").strip()
    if not q:
        return jsonify([])
    lang, ext = a.get("lang") or None, a.get("ext") or None
    source = (a.get("source") or config.SEARCH_SOURCE).lower()
    if source == "libgen":
        try:
            return jsonify({"source": "libgen", "results": libgen.search(q, lang=lang, ext=ext)})
        except Exception as e:
            return jsonify({"error": str(e)}), 502
    try:
        res = annas.search(q, lang=lang, ext=ext, page=a.get("page", 1, type=int))
        for x in res:
            x["source"] = "annas"
        return jsonify({"source": "annas", "results": res})
    except Exception as e:
        if source != "auto":
            return jsonify({"error": str(e)}), 502
        aa_error = str(e)
    try:  # auto: Anna's Archive unavailable → LibGen
        return jsonify({"source": "libgen", "results": libgen.search(q, lang=lang, ext=ext),
                        "notice": f"Anna's Archive unavailable, showing LibGen results. ({aa_error})"})
    except Exception as e:
        return jsonify({"error": f"Anna's Archive: {aa_error} | LibGen: {e}"}), 502


@api.get("/api/downloads")
def downloads():
    return jsonify(db.list_downloads())


@api.post("/api/downloads")
def add_download():
    d = request.get_json(force=True) or {}
    md5 = (d.get("md5") or "").lower()
    if d.get("link"):  # "Add by link": any Anna's Archive / LibGen URL or a bare MD5
        md5 = annas.md5_from_text(d["link"]) or ""
        if not md5:
            return jsonify({"error": "No MD5 found in that link. Paste a book page link "
                                     "(…/md5/…) or the 32-character MD5 itself."}), 400
        if db.has_md5(md5):
            return jsonify({"error": "That book is already in your library."}), 409
    if len(md5) != 32:
        return jsonify({"error": "bad md5"}), 400
    dl_id = annas.enqueue(md5, d.get("title", ""), d.get("author", ""), d.get("ext", ""))
    return jsonify({"id": dl_id})


@api.post("/api/downloads/<int:dl_id>/retry")
def retry_download(dl_id: int):
    annas.retry(dl_id)
    return jsonify({"ok": True})


@api.delete("/api/downloads/<int:dl_id>")
def delete_download(dl_id: int):
    db.execute("DELETE FROM downloads WHERE id = ? AND status != 'downloading'", (dl_id,))
    return jsonify({"ok": True})


# ------------------------------------------------------------------ app

def create_app(start_background: bool = True) -> Flask:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.ensure_dirs()
    db.conn()
    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024
    app.before_request(_require_auth)
    app.register_blueprint(api)
    app.register_blueprint(opds.bp)

    @app.get("/healthz")
    def healthz():
        return "ok"

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/manifest.webmanifest")
    def manifest():
        return jsonify({"name": config.APP_NAME, "short_name": config.APP_NAME, "start_url": "/",
                        "display": "standalone", "background_color": "#000000",
                        "theme_color": "#000000",
                        "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})

    if start_background:
        library.start_watcher()
        annas.start_worker()
    return app
