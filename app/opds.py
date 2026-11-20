"""OPDS 1.2 catalog (Atom) for KOReader and other reading apps."""
import datetime as dt
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

from flask import Blueprint, Response, request, url_for

from . import db
from .config import config

bp = Blueprint("opds", __name__, url_prefix="/opds")
PAGE = 50
NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"
MIME = {
    "epub": "application/epub+zip", "kepub": "application/kepub+zip",
    "mobi": "application/x-mobipocket-ebook", "azw3": "application/vnd.amazon.ebook",
    "fb2": "application/x-fictionbook+xml", "pdf": "application/pdf",
    "djvu": "image/vnd.djvu", "cbz": "application/vnd.comicbook+zip", "txt": "text/plain",
}


def _iso(ts: float | None = None) -> str:
    t = dt.datetime.fromtimestamp(ts, dt.timezone.utc) if ts else dt.datetime.now(dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _feed(fid: str, title: str, kind: str, entries: list[str], links: list[str] = ()) -> Response:
    self_href = request.full_path.rstrip("?")
    xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/" '
        'xmlns:opds="http://opds-spec.org/2010/catalog" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">',
        f"<id>urn:bookhoard:{escape(fid)}</id>",
        f"<title>{escape(title)}</title>",
        f"<updated>{_iso()}</updated>",
        f"<author><name>{escape(config.APP_NAME)}</name></author>",
        f'<link rel="self" href={quoteattr(self_href)} type="{kind}"/>',
        f'<link rel="start" href="{url_for("opds.root")}" type="{NAV}"/>',
        f'<link rel="search" href="{url_for("opds.opensearch")}" '
        'type="application/opensearchdescription+xml"/>',
        f'<link rel="search" href="{url_for("opds.search")}?q={{searchTerms}}" type="{ACQ}"/>',
        *links,
        *entries,
        "</feed>",
    ]
    return Response("\n".join(xml), mimetype="application/atom+xml; charset=utf-8")


def _nav_entry(eid: str, title: str, href: str, content: str = "", kind: str = ACQ) -> str:
    return (f"<entry><title>{escape(title)}</title><id>urn:bookhoard:{escape(eid)}</id>"
            f"<updated>{_iso()}</updated>"
            f'<link rel="subsection" href={quoteattr(href)} type="{kind}"/>'
            f'<content type="text">{escape(content)}</content></entry>')


def _book_entry(b: dict) -> str:
    parts = [f"<entry><title>{escape(b['title'])}</title>",
             f"<id>urn:bookhoard:book:{b['id']}</id>",
             f"<updated>{_iso(b['updated_at'])}</updated>"]
    for a in b["authors"]:
        parts.append(f"<author><name>{escape(a)}</name></author>")
    if b.get("language"):
        parts.append(f"<dc:language>{escape(b['language'])}</dc:language>")
    if b.get("publisher"):
        parts.append(f"<dc:publisher>{escape(b['publisher'])}</dc:publisher>")
    if b.get("published"):
        parts.append(f"<dc:issued>{escape(b['published'])}</dc:issued>")
    for t in b["tags"]:
        parts.append(f"<category term={quoteattr(t)} label={quoteattr(t)}/>")
    summary = b.get("description") or ""
    if b.get("series"):
        idx = b.get("series_index")
        idx = f" #{int(idx) if idx and float(idx).is_integer() else idx}" if idx else ""
        summary = f"[{b['series']}{idx}] " + summary
    if b.get("rating"):
        summary = f"★ {b['rating']}/10  " + summary
    if summary:
        parts.append(f'<summary type="text">{escape(summary[:1500])}</summary>')
    if b.get("cover"):
        cov = url_for("api.cover", book_id=b["id"], v=int(b["updated_at"]))
        parts.append(f'<link rel="http://opds-spec.org/image" href={quoteattr(cov)} type="image/jpeg"/>')
        parts.append(f'<link rel="http://opds-spec.org/image/thumbnail" '
                     f'href={quoteattr(cov + "&thumb=1")} type="image/jpeg"/>')
    from .db import list_files
    for f in list_files(b["id"]):  # one acquisition link per format – KOReader lets you pick
        href = url_for("api.download", book_id=b["id"], file_id=f["id"], filename=_filename(b, f["format"]))
        parts.append(f'<link rel="http://opds-spec.org/acquisition" href={quoteattr(href)} '
                     f'type="{MIME.get(f["format"], "application/octet-stream")}" '
                     f'title="{f["format"].upper()}" length="{f.get("size") or 0}"/>')
    parts.append("</entry>")
    return "".join(parts)


def _filename(b: dict, fmt: str | None = None) -> str:
    from .library import file_suffix, safe_name
    author = b["authors"][0] if b["authors"] else "Unknown"
    return quote(f"{safe_name(b['title'], 60)} - {safe_name(author, 40)}{file_suffix(fmt or b['format'])}")


def _book_feed(fid: str, title: str, **filters) -> Response:
    page = max(1, request.args.get("page", 1, type=int))
    books, total = db.search_books(limit=PAGE, offset=(page - 1) * PAGE, **filters)
    links = []
    base = request.path
    extra = "&".join(f"{k}={quote(str(v))}" for k, v in request.args.items() if k != "page")
    sep = "&" if extra else ""
    if page * PAGE < total:
        links.append(f'<link rel="next" href={quoteattr(f"{base}?{extra}{sep}page={page + 1}")} type="{ACQ}"/>')
    if page > 1:
        links.append(f'<link rel="previous" href={quoteattr(f"{base}?{extra}{sep}page={page - 1}")} type="{ACQ}"/>')
    links.append(f"<opensearch:totalResults>{total}</opensearch:totalResults>")
    links.append(f"<opensearch:itemsPerPage>{PAGE}</opensearch:itemsPerPage>")
    return _feed(fid, title, ACQ, [_book_entry(b) for b in books], links)


@bp.route("/")
@bp.route("")
def root():
    s = db.stats()
    entries = [
        _nav_entry("recent", "Recently added", url_for("opds.recent"), f"{s['books']} books"),
        _nav_entry("shelves", "Bookshelves", url_for("opds.shelves"), f"{s['shelves']} shelves", NAV),
        _nav_entry("titles", "All books by title", url_for("opds.titles")),
        _nav_entry("by-author", "All books by author", url_for("opds.by_author"), "Surname A–Z"),
        _nav_entry("authors", "Authors", url_for("opds.authors"), f"{s['authors']} authors", NAV),
        _nav_entry("series", "Series", url_for("opds.series_list"), "", NAV),
        _nav_entry("tags", "Tags", url_for("opds.tags"), f"{s['tags']} tags", NAV),
        _nav_entry("top", "Top rated", url_for("opds.top")),
    ]
    return _feed("root", config.APP_NAME, NAV, entries)


@bp.route("/recent")
def recent():
    return _book_feed("recent", "Recently added", sort="added")


@bp.route("/titles")
def titles():
    return _book_feed("titles", "All books", sort="title")


@bp.route("/by-author")
def by_author():
    return _book_feed("by-author", "All books by author", sort="author")


@bp.route("/top")
def top():
    return _book_feed("top", "Top rated", sort="rating")


@bp.route("/search")
def search():
    q = request.args.get("q", "").strip()
    return _book_feed(f"search:{q}", f"Search: {q}", q=q, sort="title")


@bp.route("/opensearch.xml")
def opensearch():
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">'
           f"<ShortName>{escape(config.APP_NAME)}</ShortName>"
           "<Description>Search the library</Description><InputEncoding>UTF-8</InputEncoding>"
           f'<Url type="{ACQ}" template="{url_for("opds.search")}?q={{searchTerms}}"/>'
           "</OpenSearchDescription>")
    return Response(xml, mimetype="application/opensearchdescription+xml")


def _facet_feed(fid: str, title: str, kind: str, endpoint: str) -> Response:
    from .db import author_sort_name
    label = author_sort_name if kind == "authors" else (lambda x: x)
    entries = [_nav_entry(f"{fid}:{name}", label(name), url_for(endpoint, name=name),
                          f"{count} book{'s' if count != 1 else ''}")
               for name, count in db.facet(kind)]
    return _feed(fid, title, NAV, entries)


@bp.route("/authors")
def authors():
    return _facet_feed("authors", "Authors", "authors", "opds.author")


@bp.route("/author")
def author():
    name = request.args.get("name", "")
    return _book_feed(f"author:{name}", name, author=name, sort="series")


@bp.route("/series")
def series_list():
    return _facet_feed("series", "Series", "series", "opds.series")


@bp.route("/series/books")
def series():
    name = request.args.get("name", "")
    return _book_feed(f"series:{name}", name, series=name, sort="series")


@bp.route("/tags")
def tags():
    return _facet_feed("tags", "Tags", "tags", "opds.tag")


@bp.route("/tag")
def tag():
    name = request.args.get("name", "")
    return _book_feed(f"tag:{name}", name, tag=name, sort="title")


@bp.route("/shelves")
def shelves():
    entries = [_nav_entry(f"shelf:{s['id']}", s["name"], url_for("opds.shelf", shelf_id=s["id"]),
                          s.get("description") or f"{s['count']} books")
               for s in db.list_shelves()]
    return _feed("shelves", "Bookshelves", NAV, entries)


@bp.route("/shelf/<int:shelf_id>")
def shelf(shelf_id: int):
    s = db.get_shelf(shelf_id)
    return _book_feed(f"shelf:{shelf_id}", s["name"] if s else "Shelf", shelf=shelf_id)
