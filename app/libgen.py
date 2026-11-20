"""LibGen search (libgen.li-style mirrors from LIBGEN_BASE_URLS).

Results come back in the same shape as Anna's Archive results, so Discover and the
download queue treat them identically – downloads go through the MD5 → ads.php → get.php
path that annas.resolve() already uses.

Parsing is heuristic: it takes every table row with an `md5=` link and reads the cells by
what they look like (a format word, a size, a 4-digit year, a language name) rather than by
fixed column positions, so small layout differences between mirrors don't break it.
"""
import logging
import re
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from . import db, http
from .config import config

log = logging.getLogger("bookhoard.libgen")
EXTS = {"epub", "mobi", "azw3", "fb2", "pdf", "djvu", "cbz", "cbr", "txt", "doc", "rtf", "azw", "lit"}
LANGS = {
    "polish": "pl", "english": "en", "german": "de", "french": "fr", "spanish": "es",
    "russian": "ru", "ukrainian": "uk", "italian": "it", "portuguese": "pt", "czech": "cs",
    "slovak": "sk", "dutch": "nl", "swedish": "sv", "norwegian": "no", "danish": "da",
    "finnish": "fi", "hungarian": "hu", "japanese": "ja", "chinese": "zh", "turkish": "tr",
    "polski": "pl", "angielski": "en",
}
MD5_RE = re.compile(r"md5=([0-9a-fA-F]{32})")
SIZE_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(kb|mb|gb|b)\s*$", re.I)
ISBN_RE = re.compile(r"(?:\b97[89][\d-]{10,14}\b|\b\d{9}[\dXx]\b)")


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


MIRROR_HREF = re.compile(r"ads\.php|get\.php|library\.lol|/main/|annas-archive|/md5/|file\.php|series\.php|column=", re.I)


def _title_from_cell(td) -> str:
    """Longest link text in the cell that isn't a mirror/series/author/file link."""
    best = ""
    for a in td.find_all("a", href=True):
        txt = _clean(a.get_text(" "))
        if MIRROR_HREF.search(a["href"]) or re.fullmatch(r"\[\d+\]|\d+(\.\d+)?\s*\w?b", txt, re.I):
            continue
        for i in a.find_all(["i", "font"]):  # ISBNs printed inside the link
            i.decompose()
        txt = ISBN_RE.sub("", _clean(a.get_text(" "))).strip(" ;,")
        if len(txt) > len(best):
            best = txt
    return best


def parse_search(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results, seen = [], set()
    for tr in soup.find_all("tr"):
        link = next((a for a in tr.find_all("a", href=True) if MD5_RE.search(a["href"])), None)
        if link is None:
            continue
        md5 = MD5_RE.search(link["href"]).group(1).lower()
        if md5 in seen:
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 3:
            continue
        seen.add(md5)
        texts = [_clean(td.get_text(" ")) for td in tds]

        ext = next((t.lower() for t in texts if t.lower() in EXTS), None)
        size = next((t for t in texts if SIZE_RE.match(t)), None)
        if size:
            m = SIZE_RE.match(size)
            size = f"{m.group(1).replace(',', '.')}{m.group(2).upper()}"
        year = next((t for t in texts if re.fullmatch(r"(1[5-9]|20)\d\d", t)), None)
        lang_word = next((t for t in texts if t.lower() in LANGS), None)

        # title: the cell with the longest non-mirror link text, among the first few cells
        title_idx, title = 0, ""
        for i, td in enumerate(tds[:4]):
            t = _title_from_cell(td)
            if len(t) > len(title) and t.lower() not in EXTS:
                title_idx, title = i, t
        if not title:  # no usable link anywhere – fall back to the first cell's text
            title = ISBN_RE.sub("", texts[0]).strip(" ;,")
        # author: on libgen.li the cell right after the title; on old layouts the one before
        author = ""
        auth_link = tr.find("a", href=re.compile(r"column=author"))
        if auth_link is not None:  # old libgen.rs layout links authors explicitly
            author = _clean(auth_link.parent.get_text(" ") if auth_link.parent.name == "td"
                            else auth_link.get_text(" "))
        for j in (() if author else (title_idx + 1, title_idx - 1)):
            if 0 <= j < len(texts) and texts[j] and not texts[j].isdigit() and texts[j] != title:
                author = texts[j]
                break
        publisher = texts[title_idx + 2] if title_idx + 2 < len(texts) else ""

        results.append({
            "md5": md5,
            "title": title or "(untitled)",
            "author": author[:200],
            "ext": ext,
            "size": size,
            "lang": LANGS.get((lang_word or "").lower()),
            "year": year,
            "meta": " · ".join(x for x in (publisher, year, lang_word, ext, size) if x),
            "cover_url": None,
            "source": "libgen",
        })
    return results


def search(q: str, lang: str | None = None, ext: str | None = None) -> list[dict]:
    params = {"req": q, "res": 100, "filesuns": "all"}
    errors = []
    for base in config.LIBGEN_BASE_URLS:
        host = base.split("//")[-1].strip("/")
        try:
            r = http.get(f"{base.rstrip('/')}/index.php?{urlencode(params)}", timeout=40)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            errors.append(f"{host}: HTTP {status}" if status else f"{host}: {type(e).__name__}")
            continue
        results = parse_search(r.text)
        if not results and "md5=" not in r.text and "libgen" not in r.text.lower():
            errors.append(f"{host}: doesn't look like LibGen (parked or blocked page?)")
            continue
        if lang:
            results = [x for x in results if not x["lang"] or x["lang"] == lang]
        if ext:
            results = [x for x in results if x["ext"] == ext]
        have = db.library_md5s()
        for x in results:
            x["in_library"] = x["md5"] in have
        return results
    raise RuntimeError("No LibGen mirror worked – " + "; ".join(errors) +
                       ". Add other mirrors to LIBGEN_BASE_URLS (comma-separated).")


def page_url(md5: str) -> str:
    return urljoin(config.LIBGEN_BASE_URLS[0].rstrip("/") + "/", f"ads.php?md5={md5}")
