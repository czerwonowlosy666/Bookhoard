"""Anna's Archive search + download resolution, and the background download worker.

Download strategy, in order:
  1. Member fast-download API (needs AA_API_KEY) – the most reliable.
  2. LibGen mirrors keyed by the same MD5 (ads.php → get.php).
  3. External mirror links found on the Anna's Archive /md5/ page.
"Slow download" partner servers sit behind browser checks and wait timers,
so they're not automated here.
"""
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from . import db, http
from .config import config

log = logging.getLogger("bookhoard.annas")
EXTS = "epub|pdf|mobi|azw3|fb2|djvu|cbz|cbr|txt|kepub|doc|rtf|zip"
MD5_RE = re.compile(r"/md5/([0-9a-f]{32})")
_wake = threading.Event()


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _fetch(path: str, **kw):
    """GET against the configured mirrors in order, returning the first success."""
    errors = []
    for base in config.AA_BASE_URLS:
        host = base.split("//")[-1].strip("/")
        try:
            return http.get(urljoin(base.rstrip("/") + "/", path.lstrip("/")), **kw), base
        except Exception as e:
            resp = getattr(e, "response", None)
            url = getattr(resp, "url", "") or ""
            status = getattr(resp, "status_code", None)
            if status in (403, 429, 503) and ("check=" in url or "challenge" in url or status == 403):
                msg = f"{host}: blocked by a browser check (HTTP {status})"
            elif status:
                msg = f"{host}: HTTP {status}"
            else:
                msg = f"{host}: {type(e).__name__}"
            errors.append(msg)
            log.info("mirror %s failed: %s", base, e)
    raise RuntimeError("No Anna's Archive mirror worked – " + "; ".join(errors) +
                       ". Add other mirrors to AA_BASE_URLS (comma-separated).")


# ------------------------------------------------------------------ search

def parse_meta_line(text: str) -> dict:
    out = {}
    m = re.search(r"\.(%s)\b" % EXTS, text, re.I) or re.search(r"\b(%s)\b" % EXTS, text, re.I)
    if m:
        out["ext"] = m.group(1).lower()
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(KB|MB|GB)\b", text, re.I)
    if m:
        out["size"] = f"{m.group(1).replace(',', '.')}{m.group(2).upper()}"
    m = re.search(r"\[([a-z]{2,3}(?:-[A-Za-z]+)?)\]", text)
    if m:
        out["lang"] = m.group(1)
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", text)
    if m:
        out["year"] = m.group(1)
    return out


def _card_for(a, root):
    card = a
    for _ in range(8):
        parent = card.parent
        if parent is None or parent is root:
            break
        ids = {m.group(1) for x in parent.select('a[href*="/md5/"]')
               if (m := MD5_RE.search(x.get("href", "")))}
        if len(ids) > 1:
            break
        card = parent
    return card


def parse_search(html: str) -> list[dict]:
    # Anna's Archive lazy-renders later results inside HTML comments – unwrap them.
    html = html.replace("<!--", "").replace("-->", "")
    soup = BeautifulSoup(html, "lxml")
    results, seen = [], set()
    for a in soup.select('a[href*="/md5/"]'):
        m = MD5_RE.search(a.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        md5 = m.group(1)
        seen.add(md5)
        card = _card_for(a, soup)

        title = ""
        h = card.find(["h3", "h2"])
        if h:
            title = _clean(h.get_text())
        if not title:
            texts = [_clean(x.get_text()) for x in card.select(f'a[href*="/md5/{md5}"]')]
            title = max(texts, key=len, default="")

        lines = [_clean(s) for s in card.stripped_strings]
        lines = [l for l in lines if l and l != title]
        meta_line = next((l for l in lines if re.search(r"\.(%s)\b" % EXTS, l, re.I)
                          and re.search(r"\d\s*(KB|MB|GB)", l, re.I)), "")
        if not meta_line:
            meta_line = next((l for l in lines if re.search(r"\d\s*(KB|MB|GB)", l, re.I)), "")
        info = parse_meta_line(meta_line)

        author = ""
        el = card.find(class_=re.compile(r"\bitalic\b"))
        if el:
            author = _clean(el.get_text())
        if not author:
            el = card.select_one('a[href*="search?q="]')
            author = _clean(el.get_text()) if el else ""
        if not author:
            rest = [l for l in lines if l != meta_line and not re.search(r"/(lgli|lgrs|zlib|nexus)", l)]
            author = rest[0] if rest else ""

        img = card.find("img")
        cover = img.get("src") if img is not None else None
        results.append({
            "md5": md5,
            "title": title or "(untitled)",
            "author": re.sub(r"^[^\w]+", "", author),
            "meta": meta_line,
            "cover_url": cover if cover and cover.startswith("http") else None,
            **info,
        })
    return results


def looks_like_annas(html: str) -> bool:
    """False for parked/for-sale/seized domains that answer 200 with an ad page."""
    low = html.lower()
    if any(x in low for x in ("forsale", "domain is for sale", "buy this domain", "parkingcrew",
                              "sedoparking", "abovedomains", "seized")):
        return False
    return "anna" in low and ("/search" in low or "/md5/" in low)


def search(q: str, lang: str | None = None, ext: str | None = None, page: int = 1) -> list[dict]:
    params = {"q": q}
    if lang:
        params["lang"] = lang
    if ext:
        params["ext"] = ext
    if page > 1:
        params["page"] = page
    r, base = _fetch("/search?" + urlencode(params), timeout=40)
    results = parse_search(r.text)
    if not results and not looks_like_annas(r.text):
        host = base.split("//")[-1].strip("/")
        raise RuntimeError(f"{host} doesn't look like Anna's Archive any more (the domain may be "
                           "parked, for sale or seized). Put a current mirror in AA_BASE_URLS.")
    have = db.library_md5s()
    for x in results:
        x["in_library"] = x["md5"] in have
    return results


# ------------------------------------------------------------------ resolve

def _fast_url(md5: str) -> str | None:
    if not config.AA_API_KEY:
        return None
    r, _ = _fetch(f"/dyn/api/fast_download.json?{urlencode({'md5': md5, 'key': config.AA_API_KEY})}",
                  timeout=30)
    data = r.json()
    if data.get("download_url"):
        return data["download_url"]
    raise RuntimeError(f"fast download API: {data.get('error') or 'no url'}")


def _libgen_url(md5: str, base: str) -> str | None:
    host = base.split("//")[-1].strip("/")
    try:
        r = http.get(f"{base.rstrip('/')}/ads.php?md5={md5}", timeout=30)
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        raise RuntimeError(f"{host}: HTTP {status}" if status else f"{host}: {type(e).__name__}")
    soup = BeautifulSoup(r.text, "lxml")
    a = soup.select_one('a[href*="get.php"]')
    if a is None:
        for cand in soup.find_all("a", href=True):
            if cand.get_text(strip=True).upper() in ("GET", "DOWNLOAD", "POBIERZ"):
                a = cand
                break
    if a is None:
        low = r.text.lower()
        if "not found" in low or "no record" in low or "nie znaleziono" in low:
            raise RuntimeError(f"{host}: this MD5 isn't on LibGen")
        raise RuntimeError(f"{host}: page loaded but had no download link ({len(r.text)} bytes)")
    return urljoin(r.url, a["href"])


def _mirror_urls_from_md5_page(md5: str) -> list[str]:
    r, base = _fetch(f"/md5/{md5}", timeout=40)
    soup = BeautifulSoup(r.text, "lxml")
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/slow_download/" in href or "/fast_download/" in href:
            continue
        if re.search(r"ads\.php\?md5=|get\.php\?md5=|/get\.php|libgen|library\.lol", href):
            urls.append(urljoin(base, href))
    return list(dict.fromkeys(urls))


def resolve(md5: str, reasons: list[str] | None = None) -> list[str]:
    """Return candidate direct download URLs, best first. Failure reasons go into `reasons`."""
    reasons = reasons if reasons is not None else []
    out: list[str] = []
    if config.AA_API_KEY:
        try:
            u = _fast_url(md5)
            if u:
                out.append(u)
        except Exception as e:
            reasons.append(f"Anna's Archive fast download: {e}")
    else:
        reasons.append("Anna's Archive fast download: no AA_API_KEY set")
    for base in config.LIBGEN_BASE_URLS:
        try:
            u = _libgen_url(md5, base)
            if u:
                out.append(u)
        except Exception as e:
            reasons.append(f"LibGen {e}")
    if not out:
        try:
            for page in _mirror_urls_from_md5_page(md5):
                if "ads.php" in page:
                    try:
                        u = _libgen_url(md5, re.match(r"https?://[^/]+", page).group())
                        if u:
                            out.append(u)
                    except Exception as e:
                        reasons.append(f"LibGen (via Anna's Archive) {e}")
                else:
                    out.append(page)
        except Exception as e:
            reasons.append(f"Anna's Archive page: {str(e)[:120]}")
    return out


# ------------------------------------------------------------------ download worker

def md5_from_text(text: str) -> str | None:
    m = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])", text or "")
    return m.group(1).lower() if m else None


def sniff_ext(path: Path, filename_hint: str = "") -> str | None:
    """Work out the real format from the file's first bytes (links don't tell us the format)."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head[:8] == b"AT&TFORM":
        return "djvu"
    if len(head) > 68 and head[60:68] == b"BOOKMOBI":
        return "azw3" if filename_hint.lower().endswith(".azw3") else "mobi"
    if b"FictionBook" in head:
        return "fb2"
    if head.startswith(b"PK"):
        if b"mimetypeapplication/epub+zip" in head:
            return "epub"
        import zipfile
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
            if "META-INF/container.xml" in names:
                return "epub"
            if any(n.lower().endswith(".fb2") for n in names):
                return "fb2"
            if names and all(n.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", "/"))
                             for n in names):
                return "cbz"
        except Exception:
            pass
    m = re.search(r"\.(%s)$" % EXTS, filename_hint.lower())
    return m.group(1) if m else None


def _download(url: str, dest: Path, dl_id: int) -> None:
    with http.session().get(url, stream=True, timeout=config.DOWNLOAD_TIMEOUT,
                            allow_redirects=True) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "text/html" in ctype:
            raise RuntimeError("mirror returned a web page instead of a file")
        total = int(r.headers.get("content-length") or 0)
        got, last = 0, 0.0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                got += len(chunk)
                if total and time.time() - last > 0.5:
                    db.update_download(dl_id, progress=round(got / total, 3))
                    last = time.time()
    if got < 1024:
        raise RuntimeError("downloaded file is suspiciously small")


def process(dl: dict) -> None:
    from . import library
    dl_id, md5 = dl["id"], dl["md5"]
    db.update_download(dl_id, status="downloading", progress=0, error=None)
    reasons: list[str] = []
    urls = resolve(md5, reasons)
    if not urls:
        raise RuntimeError("No download source worked. " + " | ".join(reasons))
    ext = dl.get("ext") or config.AA_DEFAULT_EXT
    tmp_dir = config.DATA_DIR / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp = tmp_dir / f"{md5}.{ext}"
    errors = []
    for url in urls:
        try:
            _download(url, tmp, dl_id)
            break
        except Exception as e:
            errors.append(f"{url.split('/')[2]}: {e}")
            tmp.unlink(missing_ok=True)
    else:
        raise RuntimeError("; ".join(errors)[:500])

    real = sniff_ext(tmp)
    if real and real != ext:
        fixed = tmp.with_suffix("." + real)
        tmp.rename(fixed)
        tmp, ext = fixed, real

    actual = library.md5sum(tmp)
    if actual != md5:
        log.warning("md5 mismatch for %s (got %s) – importing anyway", md5, actual)
    extra = {"title": dl.get("title"), "authors": [dl["author"]] if dl.get("author") else []}
    book_id = library.import_file(tmp, move=True, md5=md5, extra_meta=extra)
    book = db.get_book(book_id) if book_id else None
    db.update_download(dl_id, status="done", progress=1, book_id=book_id, ext=ext,
                       title=dl.get("title") or (book or {}).get("title") or md5,
                       author=dl.get("author") or ", ".join((book or {}).get("authors") or []))


def enqueue(md5: str, title: str, author: str, ext: str) -> int:
    existing = db.query("SELECT id FROM downloads WHERE md5 = ? AND status IN ('queued','downloading')",
                        (md5,))
    if existing:
        return existing[0][0]
    dl_id = db.add_download(md5, title, author, ext)
    _wake.set()
    return dl_id


def retry(dl_id: int) -> None:
    db.update_download(dl_id, status="queued", progress=0, error=None)
    _wake.set()


def start_worker() -> None:
    # anything left "downloading" from a previous run gets re-queued
    db.execute("UPDATE downloads SET status = 'queued' WHERE status = 'downloading'")

    def loop():
        while True:
            dl = db.next_queued_download()
            if dl is None:
                _wake.wait(timeout=15)
                _wake.clear()
                continue
            try:
                process(dl)
            except Exception as e:
                log.warning("download %s failed: %s", dl["id"], e)
                db.update_download(dl["id"], status="error", error=str(e)[:500])

    threading.Thread(target=loop, name="download-worker", daemon=True).start()
