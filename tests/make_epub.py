"""Build a minimal valid EPUB with a cover, for tests and demo data."""
import io
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw


def make_epub(path: Path, title: str, author: str, color=(120, 60, 40), lang="pl",
              series: str | None = None, index: int | None = None) -> Path:
    img = Image.new("RGB", (400, 600), color)
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 380, 580], outline=(240, 230, 210), width=4)
    d.text((40, 260), title[:28], fill=(250, 245, 235))
    d.text((40, 300), author[:28], fill=(230, 220, 200))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    series_meta = ""
    if series:
        series_meta = (f'<meta name="calibre:series" content="{series}"/>'
                       f'<meta name="calibre:series_index" content="{index or 1}"/>')
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
  <dc:title>{title}</dc:title><dc:creator opf:role="aut">{author}</dc:creator>
  <dc:language>{lang}</dc:language><dc:identifier id="uid">urn:uuid:{abs(hash(title))}</dc:identifier>
  <meta name="cover" content="cover-img"/>{series_meta}
 </metadata>
 <manifest>
  <item id="cover-img" href="cover.jpg" media-type="image/jpeg"/>
  <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
  <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
 </manifest>
 <spine toc="ncx"><itemref idref="c1"/></spine>
</package>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   "</rootfiles></container>")
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/cover.jpg", buf.getvalue())
        z.writestr("OEBPS/c1.xhtml", f"<html xmlns='http://www.w3.org/1999/xhtml'><body><h1>{title}</h1>"
                   + "<p>Lorem ipsum.</p>" * 200 + "</body></html>")
        z.writestr("OEBPS/toc.ncx", "<ncx xmlns='http://www.daisy.org/z3986/2005/ncx/' version='2005-1'>"
                   "<navMap/></ncx>")
    return path


if __name__ == "__main__":
    make_epub(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
