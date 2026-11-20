"""Metadata provider for lubimyczytac.pl (the "Polish Goodreads").

The site has no API, so this parses HTML. It relies mostly on stable hooks
(Open Graph / books:* meta tags, JSON-LD, URL patterns, Polish field labels)
rather than CSS class names, so it survives cosmetic redesigns.
"""
import json
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from . import http
from .config import config

BASE = "https://lubimyczytac.pl"
_rate_lock = threading.Lock()
_last_request = 0.0


def _get(url: str) -> BeautifulSoup:
    global _last_request
    with _rate_lock:  # be polite: serialise and space out requests
        wait = config.LC_REQUEST_DELAY - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
    r = http.get(url, timeout=25)
    return BeautifulSoup(r.text, "lxml")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("ł", "l")
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _sim(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # "Wiedźmin" vs "Wiedźmin. Ostatnie życzenie": reward prefix matches
    if b.startswith(a) or a.startswith(b):
        ratio = max(ratio, 0.85)
    return ratio


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ------------------------------------------------------------------ search

def _book_ids(el) -> set[str]:
    ids = set()
    for x in el.select('a[href*="/ksiazka/"]'):
        m = re.search(r"/ksiazka/(\d+)", x.get("href", ""))
        if m:
            ids.add(m.group(1))
    return ids


def _result_card(a, container):
    """Largest ancestor of `a` that still contains only this one book."""
    card = a
    for _ in range(8):
        parent = card.parent
        if parent is None or parent is container or len(_book_ids(parent)) > 1:
            break
        card = parent
    return card


def _get_json(url: str):
    global _last_request
    with _rate_lock:
        wait = config.LC_REQUEST_DELAY - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
    r = http.get(url, timeout=20, headers={"Accept": "application/json",
                                           "X-Requested-With": "XMLHttpRequest"})
    return r.json()


def parse_suggestions(data, limit: int = 12) -> list[dict]:
    """Parse the JSON from /searcher/getsuggestions (the site's own autocomplete)."""
    results = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("category", "book") != "book":
            continue
        url = item.get("url") or ""
        m = re.search(r"/ksiazka/(\d+)", url)
        book_id = str(item.get("id") or (m.group(1) if m else ""))
        title = _clean(item.get("title"))
        if not book_id or not title:
            continue
        authors = [_clean(a.get("fullname")) for a in item.get("authors") or []
                   if isinstance(a, dict) and a.get("fullname")]
        cover = item.get("cover")
        results.append({
            "id": book_id,
            "title": title,
            "authors": authors,
            "url": urljoin(BASE, url) if url else f"{BASE}/ksiazka/{book_id}",
            "cover_url": urljoin(BASE, cover) if cover else None,
            "series": None,
            "rating": None,  # the suggestion "rating" field has an undocumented scale
        })
        if len(results) >= limit:
            break
    return results


def search(query: str, limit: int = 12) -> list[dict]:
    # 1) JSON autocomplete endpoint – cheap and stable when it works
    try:
        found = parse_suggestions(
            _get_json(f"{BASE}/searcher/getsuggestions?phrase={quote_plus(query)}"), limit)
        if found:
            return found
    except Exception:
        pass
    # 2) fall back to parsing the HTML search results page
    return search_html(query, limit)


def search_html(query: str, limit: int = 12) -> list[dict]:
    soup = _get(f"{BASE}/szukaj/ksiazki?phrase={quote_plus(query)}")
    container = soup.find(id="ksiazki") or soup
    results, seen = [], set()
    for a in container.select('a[href*="/ksiazka/"]'):
        href = a.get("href", "")
        m = re.search(r"/ksiazka/(\d+)", href)
        if not m or m.group(1) in seen:
            continue
        title = _clean(a.get_text()) or _clean(a.get("title"))
        if not title:
            continue  # cover link – the title link with text comes next
        seen.add(m.group(1))
        card = _result_card(a, container)
        authors = []
        for au in card.select('a[href*="/autor/"]'):
            name = _clean(au.get_text())
            if name and name not in authors:
                authors.append(name)
        img = card.find("img")
        cover = None
        if img is not None:
            cover = img.get("data-src") or img.get("src")
            if cover and cover.startswith("data:"):
                cover = None
        series = None
        cyc = card.find(string=re.compile(r"Cykl:"))
        if cyc:
            series = _clean(cyc.parent.get_text() if cyc.parent else cyc).replace("Cykl:", "").strip()
        rating = None
        rt = card.find(string=re.compile(r"^\s*\d+,\d\s*$"))
        if rt:
            rating = float(rt.strip().replace(",", "."))
        results.append({
            "id": m.group(1),
            "title": title,
            "authors": authors,
            "url": urljoin(BASE, href),
            "cover_url": urljoin(BASE, cover) if cover else None,
            "series": series or None,
            "rating": rating,
        })
        if len(results) >= limit:
            break
    return results


def score(candidate: dict, title: str, authors: list[str]) -> float:
    t = _sim(title, candidate["title"])
    if authors and candidate.get("authors"):
        a = max(_sim(x, y) for x in authors for y in candidate["authors"])
        # tolerate "Sapkowski Andrzej" vs "Andrzej Sapkowski"
        a = max(a, max(_sim(" ".join(sorted(_norm(x).split())), " ".join(sorted(_norm(y).split())))
                       for x in authors for y in candidate["authors"]))
        return 0.65 * t + 0.35 * a
    return t * 0.85


def best_match(title: str, authors: list[str], isbn: str | None = None) -> dict | None:
    """Best search candidate, scored by title + author similarity.

    An ISBN is only used to find extra candidates – it is never trusted on its own
    (the suggestions endpoint doesn't search ISBNs and can return an unrelated book).
    """
    found: list[dict] = []
    if isbn:
        try:
            found += search_html(isbn, limit=5)
        except Exception:
            pass
    q = f"{title} {authors[0]}" if authors else title
    try:
        found += search(q)
    except Exception:
        pass
    if authors and not any(score(c, title, authors) >= 0.7 for c in found):
        try:
            found += search(title)
        except Exception:
            pass
    uniq: dict[str, dict] = {}
    for c in found:
        c["score"] = round(score(c, title, authors), 3)
        if c["id"] not in uniq or c["score"] > uniq[c["id"]]["score"]:
            uniq[c["id"]] = c
    ranked = sorted(uniq.values(), key=lambda c: c["score"], reverse=True)
    return ranked[0] if ranked else None


def verify(details: dict, title: str, authors: list[str], isbn: str | None = None) -> float:
    """How well a fetched book page matches our book (0..1). Same scale as score()."""
    s = score({"title": details.get("title") or "", "authors": details.get("authors") or []}, title, authors)
    if isbn and details.get("isbn") and re.sub(r"[^0-9X]", "", isbn.upper()) == details["isbn"].upper():
        s = min(1.0, s + 0.15)  # a matching ISBN helps, but can't rescue a wrong title/author
    return round(s, 3)


def match(title: str, authors: list[str], isbn: str | None = None,
          min_score: float | None = None) -> dict | None:
    """Find, fetch and *verify* the lubimyczytac page for a book. None when not confident."""
    min_score = config.LC_MIN_SCORE if min_score is None else min_score
    best = best_match(title, authors, isbn)
    if not best or best["score"] < min_score:
        return None
    details = fetch_book(best["url"])
    if verify(details, title, authors, isbn) < min_score:
        return None
    return details


def candidates(title: str, authors: list[str], query: str | None = None) -> list[dict]:
    found = search(query or (f"{title} {authors[0]}" if authors else title))
    for c in found:
        c["score"] = round(score(c, title, authors), 3)
    return sorted(found, key=lambda c: c["score"], reverse=True)


# ------------------------------------------------------------------ book page

def _meta(soup: BeautifulSoup, prop: str) -> str | None:
    el = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return el.get("content") if el and el.get("content") else None


def _label_value(soup: BeautifulSoup, label: str) -> str | None:
    """Find a value that follows a Polish label like 'Wydawnictwo:' (dt/dd or span pairs)."""
    node = soup.find(string=re.compile(r"^\s*" + re.escape(label) + r"\s*$"))
    if node is None:
        node = soup.find(string=re.compile(re.escape(label)))
    if node is None:
        return None
    el = node.parent
    # "Label: <a>Value</a>" inside the same element
    txt = _clean(el.get_text()).replace(label, "", 1).strip(" :")
    if txt:
        return txt
    # <dt>Label</dt><dd>Value</dd>
    sib = el.find_next_sibling()
    if sib is not None and sib.name in ("dd", "span", "div", "a") and _clean(sib.get_text()):
        return _clean(sib.get_text())
    return None


def _jsonld(soup: BeautifulSoup) -> dict:
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("Book", ["Book"]):
                return item
    return {}


def parse_book(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    ld = _jsonld(soup)

    title = _clean((soup.find("h1").get_text() if soup.find("h1") else "")) or \
        _clean(ld.get("name")) or _clean(_meta(soup, "og:title"))

    authors: list[str] = []
    ld_auth = ld.get("author")
    if isinstance(ld_auth, dict):
        ld_auth = [ld_auth]
    for a in ld_auth or []:
        if isinstance(a, dict) and a.get("name"):
            authors.append(_clean(a["name"]))
    author_urls: dict[str, str] = {}
    scope = soup.find(class_=re.compile("author")) or soup
    for a in scope.select('a[href*="/autor/"]'):
        n = _clean(a.get_text())
        if n and n not in author_urls and len(author_urls) < 6:
            author_urls[n] = urljoin(BASE, a["href"])
    if not authors:
        authors = list(author_urls)[:4]
    author_urls = {k: v for k, v in author_urls.items() if k in authors}

    description = ""
    desc_el = soup.find(id=re.compile(r"book-description|bookDescription|sBookDescriptionLong")) or \
        soup.find(class_=re.compile(r"collapse-content"))
    if desc_el is not None:
        for br in desc_el.find_all("br"):
            br.replace_with("\n")
        paragraphs = [_clean(p.get_text()) for p in desc_el.find_all("p")] or [desc_el.get_text()]
        description = "\n\n".join(p.strip() for p in paragraphs if p and p.strip())
    if not description:
        description = _clean(ld.get("description")) or _clean(_meta(soup, "og:description"))

    series, series_index = None, None
    cyc = _label_value(soup, "Cykl:")
    if cyc:
        m = re.match(r"(.*?)\s*\(tom\s*([\d.,]+)(?:\s*-\s*[\d.,]+)?\)", cyc)
        if m:
            series = m.group(1).strip()
            try:
                series_index = float(m.group(2).replace(",", "."))
            except ValueError:
                pass
        else:
            series = cyc

    rating = None
    rv = _meta(soup, "books:rating:value")
    if rv:
        try:
            rating = round(float(rv.replace(",", ".")), 1)
        except ValueError:
            pass

    isbn = _meta(soup, "books:isbn") or ld.get("isbn")
    isbn = re.sub(r"[^0-9Xx]", "", isbn) if isbn else None

    # categories (/ksiazki/k/) are lubimyczytac's genres, e.g. "fantasy, science fiction";
    # tags (/ksiazki/t/) are free-form reader labels. Categories are split into one tag per genre.
    categories: list[str] = []
    for a in soup.select('a[href*="/ksiazki/k/"]'):
        for t in _clean(a.get_text()).split(","):
            t = t.strip()
            if t and t.lower() not in (x.lower() for x in categories) and len(t) < 40:
                categories.append(t)
    keywords: list[str] = []
    for a in soup.select('a[href*="/ksiazki/t/"]'):
        t = _clean(a.get_text())
        if t and t.lower() not in (x.lower() for x in categories + keywords) and len(t) < 40:
            keywords.append(t)
    tags = categories if config.LC_TAG_SOURCE == "categories" else categories + keywords

    language = None
    lang_txt = (_label_value(soup, "Język:") or "").lower()
    if "pol" in lang_txt:
        language = "pl"
    elif "ang" in lang_txt:
        language = "en"
    elif lang_txt:
        language = lang_txt[:20]

    pages = ld.get("numberOfPages")
    try:
        pages = int(pages) if pages else None
    except (TypeError, ValueError):
        pages = None
    if not pages:
        lp = _label_value(soup, "Liczba stron:")
        if lp and re.search(r"\d+", lp):
            pages = int(re.search(r"\d+", lp).group())

    published = _meta(soup, "books:release_date") or \
        _label_value(soup, "Data wydania:") or _label_value(soup, "Data 1. wyd. pol.:") or \
        _label_value(soup, "Data 1. wydania:")
    if published:
        m = re.search(r"\d{4}-\d{2}-\d{2}|\d{4}", published)
        published = m.group() if m else None

    cover = _meta(soup, "og:image")
    if cover and "nocover" in cover:
        cover = None

    return {
        "title": title,
        "authors": authors,
        "description": description,
        "publisher": _label_value(soup, "Wydawnictwo:"),
        "series": series,
        "series_index": series_index,
        "translator": _label_value(soup, "Tłumacz:"),
        "rating": rating,
        "isbn": isbn,
        "tags": tags[:12],
        "categories": categories,
        "keywords": keywords,
        "language": language,
        "pages": pages,
        "published": published,
        "cover_url": cover,
        "lc_url": url,
        "author_urls": author_urls,
    }


def fetch_book(url: str) -> dict:
    if not url.startswith("http"):
        url = urljoin(BASE, url)
    soup = _get(url)
    return parse_book(str(soup), url)


# ------------------------------------------------------------------ authors

def find_author(name: str) -> dict | None:
    """Best-matching author page for a name: {'name', 'url', 'score'}."""
    candidates: list[dict] = []
    try:
        data = _get_json(f"{BASE}/searcher/getsuggestions?phrase={quote_plus(name)}")
        for item in data if isinstance(data, list) else []:
            if isinstance(item, dict) and item.get("category") == "author" and item.get("url"):
                candidates.append({"name": _clean(item.get("title") or item.get("fullname")),
                                   "url": urljoin(BASE, item["url"])})
    except Exception:
        pass
    if not candidates:
        soup = _get(f"{BASE}/szukaj/autorzy?phrase={quote_plus(name)}")
        for a in soup.select('a[href*="/autor/"]'):
            n = _clean(a.get_text())
            if n and not any(c["url"] == urljoin(BASE, a["href"]) for c in candidates):
                candidates.append({"name": n, "url": urljoin(BASE, a["href"])})
    best = None
    for c in candidates:
        sc = max(_sim(name, c["name"]),
                 _sim(" ".join(sorted(_norm(name).split())), " ".join(sorted(_norm(c["name"]).split()))))
        if best is None or sc > best["score"]:
            best = {**c, "score": round(sc, 3)}
    return best


def parse_author(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    person = {}
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
        except (ValueError, TypeError):
            continue
        for item in (data if isinstance(data, list) else data.get("@graph", [data])):
            if isinstance(item, dict) and item.get("@type") in ("Person", ["Person"]):
                person = item
                break
    name = _clean(soup.find("h1").get_text() if soup.find("h1") else "") or _clean(person.get("name"))
    photo = person.get("image") if isinstance(person.get("image"), str) else None
    photo = photo or _meta(soup, "og:image")
    if photo and re.search(r"no-?(photo|avatar|image)|default|placeholder", photo, re.I):
        photo = None
    bio = ""
    el = soup.find(id=re.compile(r"author-?description|authorDescription|about", re.I)) or \
        soup.find(class_=re.compile(r"author-?description|author__description|collapse-content"))
    if el is not None:
        for br in el.find_all("br"):
            br.replace_with("\n")
        bio = "\n\n".join(p for p in (_clean(x.get_text()) for x in (el.find_all("p") or [el])) if p)
    bio = bio or _clean(person.get("description")) or _clean(_meta(soup, "og:description"))

    def date_of(key, *labels):
        v = person.get(key)
        if not v:
            for lab in labels:
                v = _label_value(soup, lab)
                if v:
                    break
        m = re.search(r"\d{4}-\d{2}-\d{2}|\d{4}", v or "")
        return m.group() if m else None

    return {"name": name, "url": url, "photo_url": urljoin(BASE, photo) if photo else None,
            "bio": bio[:4000], "born": date_of("birthDate", "Urodzony:", "Data urodzenia:"),
            "died": date_of("deathDate", "Zmarły:", "Data śmierci:")}


def fetch_author(url: str) -> dict:
    soup = _get(url)
    return parse_author(str(soup), url)
