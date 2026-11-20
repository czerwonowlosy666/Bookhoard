"""Read (and for EPUB, write) embedded e-book metadata and covers."""
import base64
import io
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
DC = "{%s}" % NS["dc"]
OPF = "{%s}" % NS["opf"]
FB2 = {"fb": "http://www.gribuser.ru/xml/fictionbook/2.0", "l": "http://www.w3.org/1999/xlink"}


def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def guess_from_filename(path: Path) -> dict:
    stem = re.sub(r"[_]+", " ", path.stem).strip()
    stem = re.sub(r"\s*\((z-lib|libgen|anna'?s archive)[^)]*\)", "", stem, flags=re.I)
    if " - " in stem:
        left, right = stem.split(" - ", 1)
        # "Author - Title" is the most common naming convention
        return {"title": right.strip(), "authors": [left.strip()]}
    return {"title": stem, "authors": []}


# ------------------------------------------------------------------ EPUB

def _opf_path(zf: zipfile.ZipFile) -> str:
    container = etree.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(".//c:rootfile", NS)
    return rootfile.get("full-path")


def read_epub(path: Path) -> dict:
    meta: dict = {}
    with zipfile.ZipFile(path) as zf:
        opf_name = _opf_path(zf)
        opf = etree.fromstring(zf.read(opf_name))
        md = opf.find("opf:metadata", NS)
        if md is None:
            return meta
        meta["title"] = _text(md.find("dc:title", NS))
        creators = [a for a in md.findall("dc:creator", NS) if _text(a)
                    and (a.get(OPF + "role") in (None, "aut"))]
        meta["authors"] = [_text(a) for a in creators]
        file_as = [a.get(OPF + "file-as") for a in creators]
        if creators and all(file_as):
            meta["author_sort"] = " & ".join(file_as)
        meta["description"] = _text(md.find("dc:description", NS))
        meta["publisher"] = _text(md.find("dc:publisher", NS))
        meta["language"] = _text(md.find("dc:language", NS))[:8]
        meta["published"] = _text(md.find("dc:date", NS))[:10]
        meta["tags"] = [_text(s) for s in md.findall("dc:subject", NS) if _text(s)]
        for ident in md.findall("dc:identifier", NS):
            val = _text(ident)
            digits = re.sub(r"[^0-9Xx]", "", val)
            scheme = (ident.get(OPF + "scheme") or "").lower()
            if "isbn" in scheme or "isbn" in val.lower() or len(digits) in (10, 13) and digits.isalnum():
                if len(digits) in (10, 13):
                    meta["isbn"] = digits
                    break
        for m in md.findall("opf:meta", NS):
            name, content = m.get("name"), m.get("content")
            if name == "calibre:series" and content:
                meta["series"] = content
            elif name == "calibre:series_index" and content:
                try:
                    meta["series_index"] = float(content)
                except ValueError:
                    pass
        # description may contain HTML-escaped markup – strip tags
        if meta.get("description"):
            meta["description"] = re.sub(r"<[^>]+>", " ", meta["description"]).strip()
    return {k: v for k, v in meta.items() if v}


def _epub_cover_name(zf: zipfile.ZipFile) -> str | None:
    opf_name = _opf_path(zf)
    base = os.path.dirname(opf_name)
    opf = etree.fromstring(zf.read(opf_name))
    manifest = opf.find("opf:manifest", NS)
    if manifest is None:
        return None
    items = manifest.findall("opf:item", NS)
    href = None
    # EPUB3 properties="cover-image"
    for it in items:
        if "cover-image" in (it.get("properties") or ""):
            href = it.get("href")
    # EPUB2 <meta name="cover" content="id">
    if not href:
        meta = opf.find(".//opf:meta[@name='cover']", NS)
        if meta is not None:
            for it in items:
                if it.get("id") == meta.get("content"):
                    href = it.get("href")
    # heuristic
    if not href:
        for it in items:
            if (it.get("media-type") or "").startswith("image/") and "cover" in (
                    (it.get("id") or "") + (it.get("href") or "")).lower():
                href = it.get("href")
                break
    if not href:
        return None
    name = os.path.normpath(os.path.join(base, href)).replace("\\", "/")
    return name if name in zf.namelist() else None


def epub_cover(path: Path) -> bytes | None:
    with zipfile.ZipFile(path) as zf:
        name = _epub_cover_name(zf)
        return zf.read(name) if name else None


def write_epub(path: Path, meta: dict) -> None:
    """Rewrite the OPF metadata of an EPUB in place (title, authors, series, etc.)."""
    with zipfile.ZipFile(path) as zf:
        opf_name = _opf_path(zf)
        opf = etree.fromstring(zf.read(opf_name))
        md = opf.find("opf:metadata", NS)
        if md is None:
            return

        def set_single(tag: str, value: str | None):
            if not value:
                return
            el = md.find(f"dc:{tag}", NS)
            if el is None:
                el = etree.SubElement(md, DC + tag)
            for child in list(el):
                el.remove(child)
            el.text = value

        set_single("title", meta.get("title"))
        set_single("description", meta.get("description"))
        set_single("publisher", meta.get("publisher"))
        set_single("language", meta.get("language"))
        set_single("date", meta.get("published"))

        if meta.get("authors"):
            from .db import author_sort_name
            for el in md.findall("dc:creator", NS):
                if el.get(OPF + "role") in (None, "aut"):
                    md.remove(el)
            sorts = [x.strip() for x in (meta.get("author_sort") or "").split(" & ")]
            if len(sorts) != len(meta["authors"]):
                sorts = [author_sort_name(a) for a in meta["authors"]]
            for a, srt in zip(meta["authors"], sorts):
                el = etree.SubElement(md, DC + "creator")
                el.text = a
                el.set(OPF + "role", "aut")
                el.set(OPF + "file-as", srt)
        if meta.get("tags"):
            for el in md.findall("dc:subject", NS):
                md.remove(el)
            for t in meta["tags"]:
                etree.SubElement(md, DC + "subject").text = t
        if meta.get("isbn"):
            if not any(meta["isbn"] in _text(i) for i in md.findall("dc:identifier", NS)):
                el = etree.SubElement(md, DC + "identifier")
                el.set(OPF + "scheme", "ISBN")
                el.text = meta["isbn"]

        for name in ("calibre:series", "calibre:series_index"):
            for el in md.findall(f"opf:meta[@name='{name}']", NS):
                md.remove(el)
        if meta.get("series"):
            etree.SubElement(md, OPF + "meta", name="calibre:series", content=meta["series"])
            if meta.get("series_index") is not None:
                idx = meta["series_index"]
                idx = int(idx) if float(idx).is_integer() else idx
                etree.SubElement(md, OPF + "meta", name="calibre:series_index", content=str(idx))

        new_opf = etree.tostring(opf, xml_declaration=True, encoding="utf-8")

        fd, tmp = tempfile.mkstemp(suffix=".epub", dir=str(path.parent))
        os.close(fd)
        with zipfile.ZipFile(tmp, "w") as out:
            # mimetype must be first and stored uncompressed
            if "mimetype" in zf.namelist():
                out.writestr(zipfile.ZipInfo("mimetype"), zf.read("mimetype"),
                             compress_type=zipfile.ZIP_STORED)
            for item in zf.infolist():
                if item.filename == "mimetype":
                    continue
                data = new_opf if item.filename == opf_name else zf.read(item.filename)
                out.writestr(item, data, compress_type=zipfile.ZIP_DEFLATED)
    shutil.move(tmp, path)


# ------------------------------------------------------------------ FB2

def _fb2_root(path: Path):
    if path.suffix.lower() == ".zip" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith(".fb2"))
            data = zf.read(name)
    else:
        data = path.read_bytes()
    return etree.fromstring(data, parser=etree.XMLParser(recover=True, huge_tree=True))


def read_fb2(path: Path) -> dict:
    root = _fb2_root(path)
    ti = root.find(".//fb:description/fb:title-info", FB2)
    if ti is None:
        return {}
    authors = []
    for a in ti.findall("fb:author", FB2):
        parts = [_text(a.find(f"fb:{p}", FB2)) for p in ("first-name", "middle-name", "last-name")]
        name = " ".join(p for p in parts if p)
        if name:
            authors.append(name)
    meta = {
        "title": _text(ti.find("fb:book-title", FB2)),
        "authors": authors,
        "description": _text(ti.find("fb:annotation", FB2)),
        "language": _text(ti.find("fb:lang", FB2)),
        "tags": [_text(g) for g in ti.findall("fb:genre", FB2) if _text(g)],
    }
    seq = ti.find("fb:sequence", FB2)
    if seq is not None and seq.get("name"):
        meta["series"] = seq.get("name")
        try:
            meta["series_index"] = float(seq.get("number"))
        except (TypeError, ValueError):
            pass
    return {k: v for k, v in meta.items() if v}


def fb2_cover(path: Path) -> bytes | None:
    root = _fb2_root(path)
    img = root.find(".//fb:title-info/fb:coverpage/fb:image", FB2)
    if img is None:
        return None
    ref = (img.get("{%s}href" % FB2["l"]) or "").lstrip("#")
    for b in root.findall("fb:binary", FB2):
        if b.get("id") == ref:
            return base64.b64decode(b.text or "")
    return None


# ------------------------------------------------------------------ dispatch

def read_metadata(path: Path) -> dict:
    ext = path.suffix.lower().lstrip(".")
    meta: dict = {}
    try:
        if ext in ("epub", "kepub"):
            meta = read_epub(path)
        elif ext == "fb2":
            meta = read_fb2(path)
    except Exception:  # broken files still get imported with filename metadata
        meta = {}
    fallback = guess_from_filename(path)
    if not meta.get("title"):
        meta["title"] = fallback["title"]
    if not meta.get("authors"):
        meta["authors"] = fallback["authors"]
    return meta


def extract_cover(path: Path) -> bytes | None:
    ext = path.suffix.lower().lstrip(".")
    try:
        if ext in ("epub", "kepub"):
            return epub_cover(path)
        if ext == "fb2":
            return fb2_cover(path)
        if ext == "cbz":
            with zipfile.ZipFile(path) as zf:
                imgs = sorted(n for n in zf.namelist()
                              if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
                return zf.read(imgs[0]) if imgs else None
    except Exception:
        return None
    return None


def save_cover(data: bytes, dest: Path, max_height: int = 900) -> bool:
    """Normalise any image to a JPEG cover."""
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        if img.height > max_height:
            img.thumbnail((int(img.width * max_height / img.height), max_height))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, "JPEG", quality=85, optimize=True)
        return True
    except Exception:
        return False


def dominant_color(path: Path) -> str | None:
    """A vivid, representative colour of a cover as '#rrggbb' (for tinting the book page)."""
    import colorsys
    from PIL import Image
    try:
        img = Image.open(path).convert("RGB")
        img.thumbnail((80, 80))
        pal = img.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
        palette = pal.getpalette()
        counts = sorted(pal.getcolors() or [], reverse=True)
    except Exception:
        return None
    total = sum(c for c, _ in counts) or 1
    best, best_score = None, -1.0
    for count, idx in counts:
        r, g, b = palette[idx * 3: idx * 3 + 3]
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        if l < 0.08 or l > 0.92:  # skip near-black / near-white
            continue
        score = (count / total) * 0.6 + s * 0.4
        if score > best_score:
            best, best_score = (r, g, b), score
    if best is None and counts:
        idx = counts[0][1]
        best = tuple(palette[idx * 3: idx * 3 + 3])
    return "#%02x%02x%02x" % best if best else None


def reset_epub(path: Path, title: str, authors: list[str]) -> None:
    """Undo bad metadata in an EPUB: set title/authors, drop description, publisher, date,
    subjects, ISBN identifiers and series (they came from the wrong book)."""
    with zipfile.ZipFile(path) as zf:
        opf_name = _opf_path(zf)
        opf = etree.fromstring(zf.read(opf_name))
        md = opf.find("opf:metadata", NS)
        if md is None:
            return
        for tag in ("description", "publisher", "date", "subject"):
            for el in md.findall(f"dc:{tag}", NS):
                md.remove(el)
        uid = opf.get("unique-identifier")
        for el in md.findall("dc:identifier", NS):
            if el.get("id") != uid and re.sub(r"[^0-9Xx]", "", _text(el)) and \
                    len(re.sub(r"[^0-9Xx]", "", _text(el))) in (10, 13):
                md.remove(el)
        for name in ("calibre:series", "calibre:series_index", "calibre:rating"):
            for el in md.findall(f"opf:meta[@name='{name}']", NS):
                md.remove(el)
        new_opf = etree.tostring(opf, xml_declaration=True, encoding="utf-8")
    _replace_opf(path, opf_name, new_opf)
    write_epub(path, {"title": title, "authors": authors})


def _replace_opf(path: Path, opf_name: str, new_opf: bytes) -> None:
    fd, tmp = tempfile.mkstemp(suffix=".epub", dir=str(path.parent))
    os.close(fd)
    with zipfile.ZipFile(path) as zf, zipfile.ZipFile(tmp, "w") as out:
        if "mimetype" in zf.namelist():
            out.writestr(zipfile.ZipInfo("mimetype"), zf.read("mimetype"), compress_type=zipfile.ZIP_STORED)
        for item in zf.infolist():
            if item.filename == "mimetype":
                continue
            out.writestr(item, new_opf if item.filename == opf_name else zf.read(item.filename),
                         compress_type=zipfile.ZIP_DEFLATED)
    shutil.move(tmp, path)
