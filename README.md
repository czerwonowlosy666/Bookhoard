# Bookhoard — a self-hosted book library

A small, single-container e-book server for a home server or NAS:

- **Gallery-style web UI**: light / true-black dark / auto theme. The home page has a *Recently added* row and a row per bookshelf. Book pages have a hero tinted with the cover's colours, a 3D cover, the series in reading order and *More by this author*. There's a grid or list view, search, sorting and drag-and-drop upload, and it works on phones.
- **Authors**: a portrait grid (A–Z by surname or first name) and a page per author with portrait, bio and their books grouped by series. Profiles come from lubimyczytac.pl, either on first visit or for everyone via *Fetch portraits*.
- **Bookshelves**: your own named collections with an icon and colour (a book can sit on several). Each shelf page has a cover-collage header, and you can drag covers to reorder it or drag any book onto a shelf in the sidebar to add it. Add books from the book page, or select several (Shift/Ctrl-click or the ✓ on a cover) and use *Add to shelf*.
- **Sort by surname**: every book gets an author sort key ("Sapkowski, Andrzej"), worked out automatically, editable for names it gets wrong, and saved in the EPUB the way Calibre does (`opf:file-as`). Sort the library by *Author (surname)* or *Author (first name)*. The *Authors* page is an A–Z index you can switch between surname and first name. In KOReader, authors are listed as "Surname, First name", and there's an *All books by author* feed.
- **One book, several formats**: an EPUB and a KEPUB (or MOBI, PDF…) of the same book are one entry with a download button per format. KOReader also gets one link per format. Files are matched by title and authors when imported. Existing duplicates are merged automatically on startup, keeping the entry with the best metadata and combining shelves and tags. For leftovers whose metadata differs, select them and click **Merge**. `.kepub.epub` files are recognised as KEPUB.
- **Tags**: tag cloud in the sidebar, add/remove tags per book or in bulk, rename/merge/delete tags across the library. **⟳ Refresh tags from lubimyczytac.pl** (next to *Tags* in the sidebar, or *Refresh tags* for selected books) re-reads every book's tags in the background, either replacing yours or adding the missing ones. By default you review an old → new list and choose what to apply.
- **lubimyczytac.pl metadata**: new books are matched automatically (title, authors, series and number, description, tags, rating, publisher, ISBN, pages, translator, cover). You can also match by hand, compare old and new values field by field, and pick which ones to apply.
- **Anna's Archive and LibGen**: search either from the *Discover* page and click **Get**, or paste a book link or MD5 into *Add by link*. Books are downloaded in the background, imported, and matched.
- **KOReader on your Kobo**: the whole library is an **OPDS catalog** (Recently added, Bookshelves, Authors, Series, Tags, Top rated, plus search), so you browse and download books from KOReader over Wi-Fi.
- Edited metadata is written back into EPUB files, so KOReader shows the same title, author and series.
- The library is kept as plain files on disk (`books/Author/Title/Title - Author.epub`) with SQLite next to them. Nothing is locked in.

## Quick start

```bash
cd bookhoard
cp .env.example .env          # set APP_PASSWORD at least
mkdir -p data books ingest    # create these first so they're owned by you, not root
docker compose up -d --build
```

Open `http://<server-ip>:8080` and sign in with `APP_USER` / `APP_PASSWORD`.

Ways to add books:
1. Drag files onto the page, or use **Upload**.
2. Put files in `./ingest/`. They're imported within `SCAN_INTERVAL` seconds and moved into `./books/`.
3. **Discover** → search → **Get**.
4. Files you place directly under `./books/` are picked up when the container starts.

Supported formats: epub, kepub, mobi, azw3, fb2, pdf, djvu, cbz, txt. EPUB and FB2 get full metadata and covers read from the file; other formats use the file name (`Author - Title.ext`) and then lubimyczytac.

## Unraid

The template is `unraid/bookhoard.xml`. It pulls `ghcr.io/czerwonowlosy666/bookhoard:latest`, so publish the image once first:

1. **Publish the image (one-time).** Create a GitHub repo named `bookhoard` and push this project to it:
   ```bash
   git init && git add . && git commit -m "Bookhoard"
   git branch -M main
   git remote add origin https://github.com/czerwonowlosy666/Bookhoard.git
   git push -u origin main
   ```
   The included GitHub Action (`.github/workflows/docker.yml`) builds the image and pushes it to GHCR on every push to `main`. When the first run finishes, open the package page (GitHub → your profile → *Packages* → `bookhoard` → *Package settings*) and set its visibility to **Public**, so Unraid can pull it without logging in.
2. **Add the template.** On the Unraid console (or the terminal in the web UI):
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-bookhoard.xml \
     https://raw.githubusercontent.com/czerwonowlosy666/Bookhoard/main/unraid/bookhoard.xml
   ```
   No GitHub needed: paste the contents of `unraid/install-template.sh` into the Unraid terminal. It writes the template and creates the folders. You can also copy `unraid/bookhoard.xml` to that path over the `flash` SMB share.
3. **Install.** Go to **Docker → Add Container → Template**, pick **bookhoard**, set a **Password** (and your Anna's Archive key if you have one), check the three paths, then click **Apply**.
4. **Updates.** Push to `main`, wait for the Action to finish, then use **Check for updates** on Unraid's Docker tab.

Default paths:

| Container | Unraid | |
|---|---|---|
| `/data` | `/mnt/user/appdata/bookhoard` | database and covers |
| `/books` | `/mnt/user/books` | the library |
| `/ingest` | `/mnt/user/downloads/bookhoard-ingest` | drop folder. Keep it **outside** the library folder. |

The container starts as root, fixes ownership of these folders, then runs as `PUID:PGID`, which Unraid sets to `99:100` (nobody:users), so the files stay accessible over SMB.

Prefer to skip GitHub? Build the image on the server with `docker build -t bookhoard:latest /path/to/bookhoard` and change the template's *Repository* to `bookhoard:latest`. It runs fine that way, but Unraid's update check won't work.

## Connect KOReader on the Kobo

1. On the Kobo, open KOReader → 🔍 **Search** menu → **OPDS catalog**.
2. Tap **＋** and add a catalog:
   - **URL**: `http://<server-ip>:8080/opds/` (use your server's LAN IP, not `localhost`)
   - **Username / password**: the same ones you use for the web UI
3. Open the catalog, browse or search, and tap a book to download it. You can set where downloads are saved in the catalog's menu.

The *Connect KOReader* button in the sidebar shows the exact URL for your server.

To reach the library away from home, put it behind Tailscale/WireGuard, or a reverse proxy with HTTPS (Caddy, Traefik, Nginx Proxy Manager). Keep `APP_PASSWORD` set. KOReader works with HTTPS OPDS URLs.

## Configuration (`.env`)

| Variable | Default | What it does |
|---|---|---|
| `APP_USER`, `APP_PASSWORD` | `admin`, *(empty)* | HTTP Basic login for the UI, API and OPDS. An empty password turns auth **off**, which is only OK on a trusted LAN. |
| `APP_NAME` | `Bookhoard` | Name shown in the UI and in KOReader. |
| `PUID`, `PGID` | `1000` | User/group that owns files in `./books`. |
| `SCAN_INTERVAL` | `30` | Seconds between ingest-folder scans. |
| `LC_AUTO_METADATA` | `true` | Match new imports on lubimyczytac.pl automatically. |
| `LC_MIN_SCORE` | `0.72` | How confident (0–1) an automatic match must be. Anything below is marked *no match* so you can match it by hand. |
| `LC_REQUEST_DELAY` | `1.0` | Pause between lubimyczytac requests, in seconds. |
| `LC_TAG_SOURCE` | `categories` | Which lubimyczytac labels become tags: `categories` takes only the genres ("fantasy, science fiction" becomes two tags), `all` also takes readers' free-form tags. |
| `WRITE_EPUB_METADATA` | `true` | Embed edited metadata into EPUB files. |
| `AA_BASE_URLS` | `https://annas-archive.gl` | Anna's Archive mirrors, comma-separated, tried in order. **The domains change often** (.org, .se and .li are all gone), so check the current ones on Anna's Archive's Wikipedia page. |
| `AA_API_KEY` | *(empty)* | Your member key, for the fast-download API. **Strongly recommended.** Without it, downloads go through LibGen mirrors only. |
| `AA_DEFAULT_LANG`, `AA_DEFAULT_EXT` | `pl`, `epub` | Default filters on the Discover page. |
| `LIBGEN_BASE_URLS` | `https://libgen.li` | LibGen mirrors for search and downloads (by MD5). |
| `SEARCH_SOURCE` | `auto` | Where Discover searches: `auto` (Anna's Archive, falling back to LibGen when it's unreachable), `libgen` or `annas`. You can also switch per search on the Discover page. |
| `OUTBOUND_PROXY` | *(empty)* | Send all scraping and downloads through a proxy, e.g. a gluetun VPN container: `http://gluetun:8888`. |

## How downloads work

1. If `AA_API_KEY` is set, the member **fast download API** gives a direct link.
2. Otherwise it tries **LibGen** mirrors by the same MD5 (`ads.php` → `get.php`).
3. As a last resort it follows external mirror links from the book's Anna's Archive page.

Anna's Archive "slow download" partner servers use browser checks and waiting rooms, so they aren't automated. Failed downloads show the reason on the *Downloads* page and can be retried.

## Notes and limits

- **Scrapers break when sites change.** lubimyczytac.pl has no API, and Anna's Archive changes its markup and domains from time to time. Both parsers rely on stable hooks where possible (meta tags, JSON-LD, `/ksiazka/` and `/md5/` URLs), but if search results suddenly come back empty, the HTML has probably changed. The parsing lives in `app/lubimyczytac.py` and `app/annas.py`, and `tests/fixtures/` holds sample pages for testing.
- lubimyczytac.pl's terms restrict automated extraction. Keep the request delay on and use this for your own library only.
- Downloading copyrighted books you don't have rights to may be illegal where you live. That's your call.
- Run **one** gunicorn worker (as in the Dockerfile). The ingest watcher and download queue run as threads inside it.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest tests          # offline tests: EPUB read/write, import, shelves, tags, OPDS, both parsers
DATA_DIR=./data LIBRARY_DIR=./books INGEST_DIR=./ingest python wsgi.py   # dev server on :8080
```

Layout:

```
app/
  main.py          Flask app, auth, JSON API
  opds.py          OPDS 1.2 catalog for KOReader
  library.py       import, ingest watcher, metadata apply + EPUB write-back
  ebookmeta.py     EPUB/FB2 metadata and cover read/write
  lubimyczytac.py  lubimyczytac.pl search + book page parser
  annas.py         Anna's Archive search, download resolution, queue worker
  db.py            SQLite (books, shelves, downloads)
  static/          the web UI (plain HTML/CSS/JS, no build step)
```
