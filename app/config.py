"""Runtime configuration, read from environment variables."""
import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


class Config:
    APP_NAME = os.environ.get("APP_NAME", "Bookhoard")

    DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
    LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/books"))
    INGEST_DIR = Path(os.environ.get("INGEST_DIR", "/ingest"))

    # Basic auth for UI, API and OPDS. Leave APP_PASSWORD empty to disable auth (LAN only!).
    APP_USER = os.environ.get("APP_USER", "admin")
    APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

    SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "30"))
    ALLOWED_FORMATS = _list("ALLOWED_FORMATS", "epub,kepub,mobi,azw3,fb2,pdf,djvu,cbz,txt")

    # lubimyczytac.pl
    LC_AUTO_METADATA = _bool("LC_AUTO_METADATA", True)  # match new imports automatically
    LC_MIN_SCORE = float(os.environ.get("LC_MIN_SCORE", "0.72"))  # auto-apply threshold
    LC_REQUEST_DELAY = float(os.environ.get("LC_REQUEST_DELAY", "1.0"))  # politeness delay (s)
    WRITE_EPUB_METADATA = _bool("WRITE_EPUB_METADATA", False)
    # which lubimyczytac labels become tags: "categories" (genres only) or "all" (genres + reader tags)
    LC_TAG_SOURCE = os.environ.get("LC_TAG_SOURCE", "categories").lower()  # embed edited metadata in EPUB files

    # Anna's Archive
    AA_BASE_URLS = _list("AA_BASE_URLS", "https://annas-archive.gl")
    AA_API_KEY = os.environ.get("AA_API_KEY", "")  # member key → fast download API
    AA_DEFAULT_LANG = os.environ.get("AA_DEFAULT_LANG", "pl")
    AA_DEFAULT_EXT = os.environ.get("AA_DEFAULT_EXT", "epub")
    LIBGEN_BASE_URLS = _list("LIBGEN_BASE_URLS", "https://libgen.li")
    SEARCH_SOURCE = os.environ.get("SEARCH_SOURCE", "auto").lower()  # auto | libgen | annas
    DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "120"))

    USER_AGENT = os.environ.get(
        "USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0 Safari/537.36",
    )
    HTTP_PROXY = os.environ.get("OUTBOUND_PROXY", "")  # optional, e.g. http://gluetun:8888

    @property
    def DB_PATH(self) -> Path:
        return self.DATA_DIR / "bookhoard.db"

    @property
    def COVERS_DIR(self) -> Path:
        return self.DATA_DIR / "covers"

    def ensure_dirs(self) -> None:
        for d in (self.DATA_DIR, self.LIBRARY_DIR, self.INGEST_DIR, self.COVERS_DIR):
            d.mkdir(parents=True, exist_ok=True)


config = Config()
