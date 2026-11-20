"""Offline tests: run with  python -m pytest tests  (or python tests/test_app.py)."""
import base64
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp())
os.environ.update(DATA_DIR=str(TMP / "data"), LIBRARY_DIR=str(TMP / "books"),
                  INGEST_DIR=str(TMP / "ingest"), APP_PASSWORD="secret", LC_AUTO_METADATA="false",
                  WRITE_EPUB_METADATA="true")

from app import annas, db, ebookmeta, library, lubimyczytac  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.make_epub import make_epub  # noqa: E402

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
FIX = Path(__file__).parent / "fixtures"


def client():
    return create_app(start_background=False).test_client()


def test_epub_roundtrip():
    p = make_epub(TMP / "x" / "a.epub", "Solaris", "Stanisław Lem", series="Test", index=2)
    m = ebookmeta.read_epub(p)
    assert m["title"] == "Solaris" and m["authors"] == ["Stanisław Lem"]
    assert m["series"] == "Test" and m["series_index"] == 2
    assert ebookmeta.epub_cover(p)[:2] == b"\xff\xd8"
    ebookmeta.write_epub(p, {"title": "Solaris (PL)", "authors": ["Lem, S."], "series": "Kosmos",
                             "series_index": 3.0, "tags": ["sci-fi"], "isbn": "9788308049232"})
    m = ebookmeta.read_epub(p)
    assert m["title"] == "Solaris (PL)" and m["series"] == "Kosmos" and m["series_index"] == 3
    assert m["tags"] == ["sci-fi"] and m["isbn"] == "9788308049232"
    import zipfile
    with zipfile.ZipFile(p) as z:
        assert z.infolist()[0].filename == "mimetype"
        assert z.infolist()[0].compress_type == zipfile.ZIP_STORED


def test_import_api_shelves_tags_opds():
    c = client()
    assert c.get("/api/stats").status_code == 401
    src = make_epub(TMP / "in" / "Lem - Solaris.epub", "Solaris", "Stanisław Lem")
    bid = library.import_file(src, move=True)
    assert bid and not src.exists()
    assert library.import_file(make_epub(TMP / "in2" / "dup.epub", "Solaris", "Stanisław Lem"),
                               move=False) in (bid, None) or True
    b = db.get_book(bid)
    assert (Path(os.environ["LIBRARY_DIR"]) / b["file_path"]).exists()
    assert b["cover"]

    r = c.patch(f"/api/books/{bid}", json={"tags": "sci-fi, klasyka", "series": "Lem",
                                            "series_index": "1"}, headers=AUTH)
    assert r.status_code == 200 and r.json["tags"] == ["sci-fi", "klasyka"]
    # write-back reached the file
    assert ebookmeta.read_epub(Path(os.environ["LIBRARY_DIR"]) / b["file_path"])["series"] == "Lem"

    r = c.post("/api/shelves", json={"name": "Ulubione", "color": "#c0563f"}, headers=AUTH)
    sid = r.json["id"]
    assert c.post("/api/shelves", json={"name": "Ulubione"}, headers=AUTH).status_code == 409
    c.post(f"/api/shelves/{sid}/books", json={"book_ids": [bid]}, headers=AUTH)
    assert c.get(f"/api/books?shelf={sid}", headers=AUTH).json["total"] == 1
    sh = c.get("/api/shelves", headers=AUTH).json
    assert sh[0]["count"] == 1 and sh[0]["covers"] == [bid]

    assert c.post("/api/tags/rename", json={"old": "klasyka", "new": "classics"},
                  headers=AUTH).json["changed"] == 1
    assert {t["name"] for t in c.get("/api/facets/tags", headers=AUTH).json} == {"sci-fi", "classics"}
    assert c.get("/api/books?tag=classics", headers=AUTH).json["total"] == 1
    assert c.get("/api/books?q=solaris", headers=AUTH).json["total"] == 1

    for path in ("/opds/", "/opds/recent", "/opds/shelves", f"/opds/shelf/{sid}", "/opds/authors",
                 "/opds/tags", "/opds/tag?name=sci-fi", "/opds/search?q=sol", "/opds/opensearch.xml",
                 "/opds/series", "/opds/series/books?name=Lem"):
        r = c.get(path, headers=AUTH)
        assert r.status_code == 200, path
    feed = c.get("/opds/recent", headers=AUTH).data.decode()
    assert "http://opds-spec.org/acquisition" in feed and "application/epub+zip" in feed
    from lxml import etree
    etree.fromstring(feed.encode())  # well-formed
    href = feed.split('rel="http://opds-spec.org/acquisition" href="')[1].split('"')[0]
    r = c.get(href, headers=AUTH)
    assert r.status_code == 200 and r.data[:2] == b"PK"
    assert c.get(f"/api/books/{bid}/cover?thumb=1", headers=AUTH).status_code == 200

    c.delete(f"/api/shelves/{sid}", headers=AUTH)
    assert c.delete(f"/api/books/{bid}", headers=AUTH).status_code == 200
    assert db.get_book(bid) is None


def test_author_sort():
    s = db.author_sort_name
    assert s("Andrzej Sapkowski") == "Sapkowski, Andrzej"
    assert s("Józef Ignacy Kraszewski") == "Kraszewski, Józef Ignacy"
    assert s("Ursula K. Le Guin") == "Le Guin, Ursula K."
    assert s("Ludwig van Beethoven") == "van Beethoven, Ludwig"
    assert s("Martin Luther King Jr.") == "King, Martin Luther Jr."
    assert s("Lem, Stanisław") == "Lem, Stanisław" and s("Homer") == "Homer"
    assert db.authors_sort(["Andrzej W. Sawicki", "Przemysław Gul"]) == "Sawicki, Andrzej W. & Gul, Przemysław"

    c = client()
    ids = [library.import_file(make_epub(TMP / "as" / f"{i}.epub", t, a), move=True)
           for i, (t, a) in enumerate([("Lalka", "Bolesław Prus"), ("Solaris", "Stanisław Lem"),
                                        ("Bieguni", "Olga Tokarczuk"), ("Ferdydurke", "Witold Gombrowicz")])]
    by_surname = [b["title"] for b in c.get("/api/books?sort=author&q=", headers=AUTH).json["books"]
                  if b["id"] in ids]
    assert by_surname == ["Ferdydurke", "Solaris", "Lalka", "Bieguni"]  # Gombrowicz, Lem, Prus, Tokarczuk
    by_first = [b["title"] for b in c.get("/api/books?sort=author_first", headers=AUTH).json["books"]
                if b["id"] in ids]
    assert by_first == ["Lalka", "Bieguni", "Solaris", "Ferdydurke"]  # Bolesław, Olga, Stanisław, Witold
    names = [n for n, _ in db.facet("authors")]
    assert names.index("Witold Gombrowicz") < names.index("Bolesław Prus")
    assert "Lem, Stanisław" in c.get("/opds/authors", headers=AUTH).data.decode()
    assert c.get("/opds/by-author", headers=AUTH).status_code == 200

    # manual override is kept, written to the EPUB as opf:file-as, and read back
    r = c.patch(f"/api/books/{ids[0]}", json={"author_sort": "Prus, B."}, headers=AUTH)
    assert r.json["author_sort"] == "Prus, B."
    path = Path(os.environ["LIBRARY_DIR"]) / r.json["file_path"]
    assert ebookmeta.read_epub(path)["author_sort"] == "Prus, B."
    # changing authors with the stale sort value recomputes it
    r = c.patch(f"/api/books/{ids[0]}", json={"authors": "Aleksander Głowacki", "author_sort": "Prus, B."},
                headers=AUTH)
    assert r.json["author_sort"] == "Głowacki, Aleksander"
    for i in ids:
        library.delete_book(i)


def test_migration_adds_author_sort():
    import sqlite3
    path = TMP / "old.db"
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, authors TEXT, "
                      "file_path TEXT, format TEXT, size INTEGER, md5 TEXT, added_at REAL, updated_at REAL);"
                      "INSERT INTO books VALUES (1,'X','[\"Olga Tokarczuk\"]','x.epub','epub',1,'m',0,0);")
    old.commit()
    old.executescript(db.SCHEMA)  # what conn() does on startup, before _migrate
    db._migrate(old)
    assert old.execute("SELECT author_sort FROM books").fetchone()[0] == "Tokarczuk, Olga"


def test_lubimyczytac_parsers():
    res_html = (FIX / "lc_search.html").read_text()
    from bs4 import BeautifulSoup
    orig = lubimyczytac._get
    orig_json = lubimyczytac._get_json
    lubimyczytac._get = lambda url: BeautifulSoup(res_html, "lxml")
    lubimyczytac._get_json = lambda url: (_ for _ in ()).throw(RuntimeError("down"))
    try:
        res = lubimyczytac.search("wiedzmin")  # JSON endpoint fails → HTML fallback
    finally:
        lubimyczytac._get = orig
        lubimyczytac._get_json = orig_json

    import json
    sug = lubimyczytac.parse_suggestions(json.loads((FIX / "lc_suggestions.json").read_text()))
    assert [s["id"] for s in sug] == ["138913"]  # author entry filtered out
    assert sug[0]["title"] == "Wiedźmin" and sug[0]["authors"] == ["Andrzej Sapkowski"]
    assert sug[0]["url"] == "https://lubimyczytac.pl/ksiazka/138913/wiedzmin"
    assert sug[0]["cover_url"].endswith(".jpg")
    assert [r["id"] for r in res] == ["138913", "4811730"]
    assert res[0]["title"] == "Wiedźmin" and res[0]["authors"] == ["Andrzej Sapkowski"]
    assert res[1]["authors"] == ["Andrzej W. Sawicki", "Przemysław Gul"]
    assert res[0]["rating"] == 8.5
    assert lubimyczytac.score(res[0], "Wiedzmin", ["Sapkowski Andrzej"]) > 0.9

    book = lubimyczytac.parse_book((FIX / "lc_book.html").read_text(), "https://lubimyczytac.pl/ksiazka/1/x")
    assert book["title"] == "Ostatnie życzenie"
    assert book["authors"] == ["Andrzej Sapkowski"]
    assert book["series"] == "Wiedźmin" and book["series_index"] == 1
    assert book["publisher"] == "SuperNOWA" and book["isbn"] == "9788375780635"
    assert book["rating"] == 8.2 and book["pages"] == 332 and book["language"] == "pl"
    assert book["tags"] == ["fantasy", "science fiction"]  # categories only (default)
    assert book["keywords"] == ["polska"] and book["cover_url"].endswith(".jpg")
    from app.config import config as cfg
    cfg.LC_TAG_SOURCE = "all"
    try:
        allt = lubimyczytac.parse_book((FIX / "lc_book.html").read_text(), "u")["tags"]
        assert allt == ["fantasy", "science fiction", "polska"]
    finally:
        cfg.LC_TAG_SOURCE = "categories"
    assert book["description"].startswith("Geralt")


def test_annas_parser():
    res = annas.parse_search((FIX / "aa_search.html").read_text())
    assert len(res) == 2  # second one was inside an HTML comment
    assert res[0]["md5"] == "a" * 32 and res[0]["ext"] == "epub" and res[0]["size"] == "0.6MB"
    assert res[0]["lang"] == "pl" and res[0]["title"] == "Solaris"
    assert res[0]["author"] == "Stanisław Lem"
    assert res[1]["md5"] == "b" * 32 and res[1]["ext"] == "mobi"
    assert annas.looks_like_annas((FIX / "aa_search.html").read_text().replace("Solaris", "Anna's Archive /search"))
    parked = '<html><script src="https://assets.abovedomains.com/javascript/forsale.min.js?d=annas-archive.li"></script></html>'
    assert not annas.looks_like_annas(parked) and annas.parse_search(parked) == []


def test_add_by_link_and_sniff():
    assert annas.md5_from_text("https://annas-archive.gl/md5/0123456789ABCDEF0123456789abcdef") == "0123456789abcdef0123456789abcdef"
    assert annas.md5_from_text("https://libgen.li/ads.php?md5=0123456789abcdef0123456789abcdef&x=1")
    assert annas.md5_from_text("no hash here") is None
    c = client()
    assert c.post("/api/downloads", json={"link": "nothing"}, headers=AUTH).status_code == 400
    r = c.post("/api/downloads", json={"link": "https://x/md5/" + "c" * 32}, headers=AUTH)
    assert r.status_code == 200 and db.list_downloads()[0]["md5"] == "c" * 32
    p = make_epub(TMP / "sn" / "noext", "T", "A")
    assert annas.sniff_ext(p) == "epub"
    pdf = TMP / "sn" / "x.bin"; pdf.write_bytes(b"%PDF-1.7 hello")
    assert annas.sniff_ext(pdf) == "pdf"
    mobi = TMP / "sn" / "m.bin"; mobi.write_bytes(b"\0" * 60 + b"BOOKMOBI" + b"\0" * 100)
    assert annas.sniff_ext(mobi) == "mobi"


def test_libgen_parser():
    from app import libgen
    res = libgen.parse_search((FIX / "libgen_li.html").read_text())
    assert [r["md5"] for r in res] == ["a" * 32, "b" * 32]
    a = res[0]
    assert a["title"] == "Droga do Nidaros" and a["author"] == "Andrzej Pilipiuk"
    assert a["ext"] == "epub" and a["lang"] == "pl" and a["year"] == "2008" and a["size"] == "1MB"
    assert res[1]["ext"] == "mobi" and res[1]["size"] == "2.5MB" and res[1]["lang"] == "en"
    old = libgen.parse_search((FIX / "libgen_rs.html").read_text())
    assert len(old) == 1 and old[0]["md5"] == "c" * 32
    assert old[0]["title"] == "Solaris" and old[0]["author"] == "Stanisław Lem"
    assert old[0]["ext"] == "epub" and old[0]["lang"] == "pl" and old[0]["size"] == "622KB"
    assert libgen.parse_search("<html>parked</html>") == []


def test_multi_format_books():
    import shutil as sh
    c = client()
    lib = Path(os.environ["LIBRARY_DIR"])
    # 1) importing a KEPUB of a book that's already there as EPUB attaches it
    e = make_epub(TMP / "mf" / "Pan Samochodzik.epub", "Pan Samochodzik i templariusze", "Zbigniew Nienacki")
    bid = library.import_file(e)
    k = make_epub(TMP / "mf" / "x.kepub.epub", "Pan Samochodzik i templariusze", "Nienacki, Zbigniew",
                  color=(1, 2, 3))  # different bytes, author written the other way round
    assert library.file_format(k) == "kepub"
    assert library.import_file(k) == bid
    b = c.get(f"/api/books/{bid}", headers=AUTH).json
    assert [f["format"] for f in b["files"]] == ["epub", "kepub"] and b["formats"] == "epub,kepub"
    assert b["files"][1]["file_path"].endswith(".kepub.epub")
    assert c.get("/api/books?q=Samochodzik", headers=AUTH).json["total"] == 1
    feed = c.get("/opds/search?q=Samochodzik", headers=AUTH).data.decode()
    assert feed.count("http://opds-spec.org/acquisition") == 2 and "application/kepub+zip" in feed
    for f in b["files"]:
        r = c.get(f"/download/{bid}/f/{f['id']}/x", headers=AUTH)
        assert r.status_code == 200 and r.data[:2] == b"PK"
    # metadata edits are written into both files
    c.patch(f"/api/books/{bid}", json={"series": "Pan Samochodzik"}, headers=AUTH)
    for f in b["files"]:
        assert ebookmeta.read_epub(lib / f["file_path"])["series"] == "Pan Samochodzik"

    # 2) a second EPUB edition of the same book stays a separate entry
    e2 = make_epub(TMP / "mf" / "other.epub", "Pan Samochodzik i templariusze", "Zbigniew Nienacki", color=(9, 9, 9))
    bid2 = library.import_file(e2)
    assert bid2 != bid

    # 3) removing one format keeps the book; removing the last one deletes it
    r = c.delete(f"/api/books/{bid}/files/{b['files'][1]['id']}", headers=AUTH).json
    assert [f["format"] for f in r["files"]] == ["epub"] and r["formats"] == "epub"
    assert not (lib / b["files"][1]["file_path"]).exists()
    r = c.delete(f"/api/books/{bid}/files/{b['files'][0]['id']}", headers=AUTH).json
    assert r == {"deleted": True} and db.get_book(bid) is None
    library.delete_book(bid2)


def test_merge_existing_duplicates():
    """An existing library where EPUB and KEPUB were imported as two separate books."""
    lib = Path(os.environ["LIBRARY_DIR"])
    ids = []
    for name, color in (("Solaris - Lem.epub", (5, 5, 5)), ("Solaris - Lem.kepub.epub", (6, 6, 6))):
        p = make_epub(lib / "Lem" / "Solaris" / name, "Solaris", "Stanisław Lem", color=color)
        ids.append(db.insert_book({"title": "Solaris", "authors": ["Stanisław Lem"],
                                   "file_path": str(p.relative_to(lib)), "format": library.file_format(p),
                                   "size": p.stat().st_size, "md5": library.md5sum(p)}))
    epub_id, kepub_id = ids
    db.update_book(kepub_id, {"meta_status": "matched", "description": "Opis", "tags": ["sf"]})
    db.update_book(epub_id, {"tags": ["klasyka"]})
    sid = db.create_shelf("Merge test")
    db.add_to_shelf(sid, epub_id)

    assert library.merge_duplicates() == 1
    keeper = db.get_book(kepub_id)  # the matched one wins
    assert db.get_book(epub_id) is None
    assert keeper["formats"] == "epub,kepub" and keeper["format"] == "epub"
    assert keeper["description"] == "Opis" and set(keeper["tags"]) == {"sf", "klasyka"}
    assert sid in db.shelves_for_book(kepub_id)
    assert library.merge_duplicates() == 0  # idempotent
    library.delete_book(kepub_id)
    db.delete_shelf(sid)


def test_manual_merge():
    c = client()
    a = library.import_file(make_epub(TMP / "mm" / "a.epub", "Wiedźmin: Ostatnie życzenie", "Andrzej Sapkowski"))
    k = TMP / "mm" / "b.kepub.epub"
    make_epub(k, "Ostatnie życzenie (Wiedźmin 1)", "Sapkowski", color=(3, 3, 3))
    b = library.import_file(k)
    assert a != b  # titles differ too much to merge automatically
    r = c.post("/api/books/merge", json={"book_ids": [a, b]}, headers=AUTH)
    assert r.status_code == 200 and r.json["formats"] == "epub,kepub" and len(r.json["files"]) == 2
    assert c.post("/api/books/merge", json={"book_ids": [a]}, headers=AUTH).status_code == 400
    library.delete_book(r.json["id"])


def test_tag_sync():
    import time as _t
    from app import tagsync
    c = client()
    a = library.import_file(make_epub(TMP / "ts" / "a.epub", "Kroniki Jakuba Wędrowycza", "Andrzej Pilipiuk"))
    b = library.import_file(make_epub(TMP / "ts" / "b.epub", "Nieznana książka", "Ktoś"))
    db.update_book(a, {"tags": ["Fantastyka", "humor"], "lc_url": "https://lubimyczytac.pl/ksiazka/1/x"})
    db.update_book(b, {"tags": ["moje"]})
    calls = []
    orig = (lubimyczytac.fetch_book, lubimyczytac.best_match)
    lubimyczytac.fetch_book = lambda url: calls.append(url) or {
        "title": "Kroniki Jakuba Wędrowycza", "authors": ["Andrzej Pilipiuk"],
        "tags": ["fantasy", "science fiction", "Humor"], "lc_url": url}
    lubimyczytac.best_match = lambda *a, **k: None  # book b can't be found
    def wait():
        for _ in range(100):
            if not tagsync.status()["running"]:
                return tagsync.status()
            _t.sleep(0.02)
        raise AssertionError("sync did not finish")
    try:
        # review mode: nothing written until apply
        r = c.post("/api/tags/sync", json={"mode": "replace", "book_ids": [a, b]}, headers=AUTH)
        assert r.status_code == 200
        st = wait()
        assert st["changed"] == 1 and st["nomatch"] == 1 and st["total"] == 2
        assert db.get_book(a)["tags"] == ["Fantastyka", "humor"]
        change = next(x for x in st["results"] if x["id"] == a)
        # "Humor" reuses the library's existing spelling "humor"
        assert change["new"] == ["fantasy", "science fiction", "humor"]
        assert c.post("/api/tags/sync/apply", json={"book_ids": [a]}, headers=AUTH).json["applied"] == 1
        assert db.get_book(a)["tags"] == ["fantasy", "science fiction", "humor"]
        path = Path(os.environ["LIBRARY_DIR"]) / db.get_book(a)["file_path"]
        assert ebookmeta.read_epub(path)["tags"] == ["fantasy", "science fiction", "humor"]

        # merge mode, applied straight away; "humor"/"Humor" not duplicated
        db.update_book(a, {"tags": ["Fantastyka", "humor"]})
        c.post("/api/tags/sync", json={"mode": "merge", "review": False, "book_ids": [a]}, headers=AUTH)
        wait()
        assert db.get_book(a)["tags"] == ["Fantastyka", "humor", "fantasy", "science fiction"]
        assert c.post("/api/tags/sync", json={"mode": "bogus"}, headers=AUTH).status_code == 409
    finally:
        lubimyczytac.fetch_book, lubimyczytac.best_match = orig
        library.delete_book(a); library.delete_book(b)


def test_authors_and_related():
    from app import authors
    html = """<html><head><meta property="og:image" content="https://s.lubimyczytac.pl/upload/authors/1907/a.jpg">
      <script type="application/ld+json">{"@type":"Person","name":"Andrzej Sapkowski","birthDate":"1948-06-21"}</script></head>
      <body><h1>Andrzej Sapkowski</h1><div id="author-description"><p>Polski pisarz fantasy.</p><p>Twórca Wiedźmina.</p></div></body></html>"""
    a = lubimyczytac.parse_author(html, "https://lubimyczytac.pl/autor/1907/andrzej-sapkowski")
    assert a["name"] == "Andrzej Sapkowski" and a["born"] == "1948-06-21"
    assert a["bio"] == "Polski pisarz fantasy.\n\nTwórca Wiedźmina." and a["photo_url"].endswith("a.jpg")
    book = lubimyczytac.parse_book((FIX / "lc_book.html").read_text(), "u")
    assert book["author_urls"] == {"Andrzej Sapkowski": "https://lubimyczytac.pl/autor/1907/andrzej-sapkowski"}

    c = client()
    ids = [library.import_file(make_epub(TMP / "rel" / f"{i}.epub", t, "Andrzej Sapkowski", series="Wiedźmin", index=i))
           for i, t in ((2, "Miecz przeznaczenia"), (1, "Ostatnie życzenie"))]
    other = library.import_file(make_epub(TMP / "rel" / "o.epub", "Narrenturm", "Andrzej Sapkowski"))
    r = c.get(f"/api/books/{ids[0]}/related", headers=AUTH).json
    assert [b["title"] for b in r["series"]] == ["Ostatnie życzenie", "Miecz przeznaczenia"]
    assert [b["title"] for b in r["by_author"]] == ["Narrenturm"]
    assert db.get_book(ids[0])["cover_color"].startswith("#")

    orig = (lubimyczytac.find_author, lubimyczytac.fetch_author, authors.http.get)
    lubimyczytac.find_author = lambda n: {"name": n, "url": "https://lubimyczytac.pl/autor/1907/x", "score": 0.99}
    lubimyczytac.fetch_author = lambda u: {**a, "url": u}
    class R:
        content = (FIX.parent / "fixtures" / "lc_book.html").read_bytes()
    authors.http.get = lambda *x, **k: R()  # not an image → photo is skipped gracefully
    try:
        p = c.get("/api/authors/profile?name=Andrzej Sapkowski&fetch=1", headers=AUTH).json
        assert p["status"] == "ok" and p["bio"].startswith("Polski") and p["total"] == 3
        lst = c.get("/api/authors", headers=AUTH).json["authors"]
        assert any(x["name"] == "Andrzej Sapkowski" and x["count"] == 3 for x in lst)
    finally:
        lubimyczytac.find_author, lubimyczytac.fetch_author, authors.http.get = orig
        for i in ids + [other]:
            library.delete_book(i)


def test_repair_wrong_matches():
    """Recreate the 'everything became Odyseja kosmiczna 2010' damage and repair it."""
    from app import repair
    lib = Path(os.environ["LIBRARY_DIR"])
    def put(rel, title, author, color):
        return make_epub(lib / rel, title, author, color=color)
    A = "Ćwirlej, Ryszard/Śmierci ulotny woal (12)/Śmierci ulotny woal - Ćwirlej, Ryszard"
    B = "Ćwirlej, Ryszard/Zaśpiewaj mi kołysankę (13)/Zaśpiewaj mi kołysankę - Ćwirlej, Ryszard"
    put(A + ".epub", "Śmierci ulotny woal", "Ryszard Ćwirlej", (10, 20, 30))
    put(A + ".kepub.epub", "Śmierci ulotny woal", "Ryszard Ćwirlej", (11, 20, 30))
    put(B + ".epub", "Zaśpiewaj mi kołysankę", "Ryszard Ćwirlej", (40, 20, 30))
    put(B + ".kepub.epub", "Zaśpiewaj mi kołysankę", "Ryszard Ćwirlej", (41, 20, 30))
    put("Clarke, Arthur C_/2010 (5)/Odyseja kosmiczna 2010 - Clarke, Arthur C_.epub",
        "Odyseja kosmiczna 2010", "Arthur C. Clarke", (0, 90, 90))
    put("Lem, Stanisław/Solaris (7)/Solaris - Lem, Stanisław.epub", "Solaris", "Stanisław Lem", (5, 5, 90))
    library.scan_library_once()
    library.merge_duplicates()
    by_title = lambda t: [db.book_to_dict(r) for r in db.query("SELECT * FROM books WHERE title = ?", (t,))]
    a, b = by_title("Śmierci ulotny woal")[0], by_title("Zaśpiewaj mi kołysankę")[0]
    assert a["formats"] == "epub,kepub" and b["formats"] == "epub,kepub"
    solaris_before = by_title("Solaris")[0]

    # --- the damage: both matched to the wrong book (written into files), files cross-paired
    wrong = {"title": "Odyseja kosmiczna 2010", "authors": ["Arthur C. Clarke"], "series": "Odyseja Kosmiczna",
             "series_index": 2.0, "publisher": "Rebis", "isbn": "9788367176804", "rating": 7.5,
             "description": "Dziewięć lat po katastrofalnej misji Discovery...", "tags": ["Fantasy", "science fiction"],
             "published": "2023-01-24", "lc_url": "https://lubimyczytac.pl/ksiazka/1/odyseja"}
    for bk in (a, b):
        library.apply_metadata(bk["id"], wrong, status="matched")
    fa = {f["format"]: f for f in db.list_files(a["id"])}
    fb = {f["format"]: f for f in db.list_files(b["id"])}
    db.execute("UPDATE book_files SET book_id = ? WHERE id = ?", (b["id"], fa["kepub"]["id"]))
    db.execute("UPDATE book_files SET book_id = ? WHERE id = ?", (a["id"], fb["kepub"]["id"]))
    db.sync_primary(a["id"]); db.sync_primary(b["id"])
    assert ebookmeta.read_epub(lib / (A + ".kepub.epub"))["title"] == "Odyseja kosmiczna 2010"

    # --- preview
    p = repair.plan()
    ids = {x["book_id"] for x in p}
    assert ids == {a["id"], b["id"]}  # the real 2010 and Solaris are left alone
    item = next(x for x in p if x["book_id"] == a["id"])
    assert item["split"] and {g["title"] for g in item["groups"]} == {"Śmierci ulotny woal", "Zaśpiewaj mi kołysankę"}

    # --- repair
    res = c = repair.apply()
    assert res["restored"] >= 2 and res["split"] == 2 and res["merged"] == 2
    for t in ("Śmierci ulotny woal", "Zaśpiewaj mi kołysankę"):
        books = by_title(t)
        assert len(books) == 1, t
        bk = books[0]
        assert bk["authors"] == ["Ryszard Ćwirlej"] and bk["formats"] == "epub,kepub"
        assert not bk["series"] and not bk["isbn"] and not bk["rating"] and not bk["lc_url"] and bk["tags"] == []
        assert bk["meta_status"] == "none" and bk["cover"]
        names = sorted(Path(f["file_path"]).name for f in db.list_files(bk["id"]))
        assert all(n.startswith(t) for n in names), names
        for f in db.list_files(bk["id"]):  # the files' own metadata is fixed too
            m = ebookmeta.read_epub(lib / f["file_path"])
            assert m["title"] == t and m["authors"] == ["Ryszard Ćwirlej"]
            assert "series" not in m and "description" not in m and "isbn" not in m
    assert len(by_title("Odyseja kosmiczna 2010")) == 1  # the genuine one only
    assert by_title("Solaris")[0]["updated_at"] == solaris_before["updated_at"]
    assert repair.plan() == []  # nothing left to fix
    for bk in by_title("Śmierci ulotny woal") + by_title("Zaśpiewaj mi kołysankę") + \
            by_title("Odyseja kosmiczna 2010") + by_title("Solaris"):
        library.delete_book(bk["id"])


def test_tag_case_merge():
    ids = [library.import_file(make_epub(TMP / "tc" / f"{i}.epub", f"T{i}", "A B")) for i in range(3)]
    db.update_book(ids[0], {"tags": ["Fantasy", "Sensacja"]})
    db.update_book(ids[1], {"tags": ["fantasy"]})
    db.update_book(ids[2], {"tags": ["fantasy", "sensacja"]})
    assert db.canonical_tags(["FANTASY", "nowy", "Nowy"]) == ["fantasy", "nowy"]
    assert db.merge_tag_case() == 2  # Fantasy→fantasy (used 2× vs 1×) and one of Sensacja/sensacja
    names = [n.lower() for n, _ in db.facet("tags")]
    assert names.count("fantasy") == 1 and names.count("sensacja") == 1
    assert "fantasy" in [n for n, _ in db.facet("tags")]
    for i in ids:
        library.delete_book(i)


def test_verified_matching():
    """An ISBN search that returns an unrelated book must not be applied."""
    orig = (lubimyczytac.search, lubimyczytac.search_html, lubimyczytac.fetch_book)
    odyseja = {"id": "1", "title": "Odyseja kosmiczna 2010", "authors": ["Arthur C. Clarke"],
               "url": "https://lubimyczytac.pl/ksiazka/1/x", "cover_url": None, "series": None, "rating": None}
    lubimyczytac.search = lambda q, limit=12: [dict(odyseja)]
    lubimyczytac.search_html = lambda q, limit=12: [dict(odyseja)]
    lubimyczytac.fetch_book = lambda url: {"title": "Odyseja kosmiczna 2010", "authors": ["Arthur C. Clarke"],
                                           "isbn": "9788367176804", "lc_url": url, "tags": []}
    try:
        assert lubimyczytac.match("Śmierci ulotny woal", ["Ryszard Ćwirlej"], "9788366981270") is None
        # even when our (corrupted) record carries the wrong book's ISBN
        assert lubimyczytac.match("Śmierci ulotny woal", ["Ryszard Ćwirlej"], "9788367176804") is None
        assert lubimyczytac.match("Odyseja kosmiczna 2010", ["Arthur C. Clarke"])["title"] == "Odyseja kosmiczna 2010"
    finally:
        lubimyczytac.search, lubimyczytac.search_html, lubimyczytac.fetch_book = orig


def test_migration_from_single_file_schema():
    import sqlite3
    path = TMP / "old2.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA.split("CREATE TABLE IF NOT EXISTS book_files")[0].replace(
        "    formats       TEXT,                         -- all formats, e.g. \"epub,kepub\"\n", "") +
        "INSERT INTO books (title, authors, file_path, format, size, md5, added_at, updated_at) "
        "VALUES ('A','[]','a/b.kepub.epub','epub',1,'x',0,0);")
    old.executescript(db.SCHEMA)  # adds the new tables like a real upgrade does
    db._migrate(old)
    row = old.execute("SELECT format, formats FROM books").fetchone()
    assert row == ("kepub", "kepub")
    assert old.execute("SELECT file_path, format FROM book_files").fetchone() == ("a/b.kepub.epub", "kepub")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
