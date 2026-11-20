/* Bookhoard — web UI (vanilla JS, no build step) */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const PAGE = 60;
const SHELF_COLORS = ["#f0483e", "#ff9f0a", "#30b158", "#14a3a8", "#0a84ff", "#5e5ce6", "#bf5af2", "#636366"];
const SHELF_ICONS = ["📚", "⭐", "❤️", "📖", "🔖", "🐉", "🚀", "🔍", "🗡️", "🏰", "🌙", "☕", "🎓", "🧒", "🏖️", "💼", "🎨", "🧠", "🌿", "👻", "🕵️", "💌", "🏆", "📌"];

const state = {
  q: "", sort: localStorage.getItem("bookhoard-sort") || "added",
  view: localStorage.getItem("bookhoard-view") || "grid",
  books: [], total: 0, offset: 0, filters: {},
  selected: new Set(), shelves: [], tags: [], stats: {}, route: "", lastListHash: "#/library", dragIds: null,
};

/* ───────────────────────── utilities ───────────────────────── */
async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers["Content-Type"] = "application/json"; delete o.json; }
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (e) { /* not json */ }
  if (!r.ok || (data && data.error)) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}
function toast(msg, bad = false) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.textContent = msg;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3200);
}
function hue(s) { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h % 360; }
function coverURL(b, thumb = true) { return `/api/books/${b.id}/cover?v=${Math.floor(b.updated_at || 0)}${thumb ? "&thumb=1" : ""}`; }
function coverHTML(b, thumb = true) {
  if (b.cover) {
    return `<div class="cover"><img loading="lazy" alt="" src="${coverURL(b, thumb)}" onload="this.classList.add('loaded')"></div>`;
  }
  const h = hue(b.title + (b.authors || [])[0]);
  return `<div class="cover"><div class="gen-cover" style="background:linear-gradient(155deg,hsl(${h} 62% 52%),hsl(${(h + 40) % 360} 58% 30%))">
    <b>${esc(b.title)}</b><span>${esc((b.authors || []).join(", "))}</span></div></div>`;
}
function stars(r, label = true) {
  if (!r) return "";
  const full = Math.round(r / 2);
  return `<span class="stars">${"★".repeat(full)}${"☆".repeat(5 - full)}${label ? `<small>${r.toFixed(1)}</small>` : ""}</span>`;
}
function fmtSize(n) { if (!n) return ""; const u = ["B", "KB", "MB", "GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return `${n.toFixed(i ? 1 : 0)} ${u[i]}`; }
function fmtSuffix(f) { return f === "kepub" ? ".kepub.epub" : "." + f; }
function fmtIdx(i) { return i == null ? "" : Number.isInteger(i) ? String(i) : String(i); }
function seriesLabel(b) { if (!b.series) return ""; return b.series_index != null ? `${b.series} #${fmtIdx(b.series_index)}` : b.series; }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function shelfColor(s) { return s.color || SHELF_COLORS[s.id % SHELF_COLORS.length]; }
function shelfIcon(s, cls = "shelf-icon") { const c = shelfColor(s); return `<span class="${cls}" style="background:${c}29;color:${c}">${esc(s.icon || "📚")}</span>`; }
function initials(name) { return (name || "?").split(/[\s,]+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase(); }
function link(route, params) {
  const q = new URLSearchParams(Object.entries(params || {}).filter(([, v]) => v)).toString();
  return `#/${route}${q ? "?" + q : ""}`;
}
function authorLink(name) { return link("author", { name }); }
function plural(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }
function heroTint(color, fallbackSeed) {
  const c = color || `hsl(${hue(fallbackSeed || "")} 45% 40%)`;
  return `background:linear-gradient(135deg, color-mix(in srgb, ${c} 78%, #000), color-mix(in srgb, ${c} 40%, #000))`;
}

/* ───────────────────────── theme ───────────────────────── */
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("bookhoard-theme", t); } catch (e) {}
  $$("[data-theme-set]").forEach((b) => b.classList.toggle("active", b.dataset.themeSet === t));
}

/* ───────────────────────── sidebar ───────────────────────── */
async function loadSidebar() {
  const [shelves, tags, stats] = await Promise.all([api("/api/shelves"), api("/api/facets/tags"), api("/api/stats")]);
  state.shelves = shelves; state.tags = tags; state.stats = stats;
  $("#brand-name").textContent = stats.app_name || "Bookhoard";
  document.title = stats.app_name || "Bookhoard";
  $("#nav-count").textContent = stats.books || "";
  $("#shelf-list").innerHTML = shelves.length
    ? shelves.map((s) => `<a href="#/shelf/${s.id}" data-shelf="${s.id}">${shelfIcon(s)}${esc(s.name)}<span class="count">${s.count}</span></a>`).join("")
    : `<div class="empty">No shelves yet — make one with ＋</div>`;
  const top = [...tags].sort((a, b) => b.count - a.count).slice(0, 24).sort((a, b) => a.name.localeCompare(b.name, "pl"));
  $("#tag-cloud").innerHTML = top.length
    ? top.map((t) => `<a class="chip ${state.filters.tag === t.name ? "active" : ""}" href="${link("library", { tag: t.name })}">${esc(t.name)} <small>${t.count}</small></a>`).join("")
    : `<div class="empty" style="font-size:13px;color:var(--muted);padding:0 4px">Tags appear here as books get them</div>`;
  $("#bulk-shelf").innerHTML = `<option value="">Add to shelf…</option>` + shelves.map((s) => `<option value="${s.id}">${esc((s.icon || "") + " " + s.name)}</option>`).join("") + `<option value="new">＋ New shelf…</option>`;
  markActiveNav();
}
function markActiveNav() {
  const r = state.route;
  const navRoute = { author: "authors", shelves: "", book: "" }[r] ?? r;
  $$(".nav a").forEach((a) => a.classList.toggle("active", navRoute === a.dataset.nav && !state.filters.shelf));
  $$(".shelf-list a").forEach((a) => a.classList.toggle("active", r === "shelf" && String(state.filters.shelf) === a.dataset.shelf));
}

/* ───────────────────────── router ───────────────────────── */
function parseHash() {
  const h = location.hash.replace(/^#\/?/, "") || "library";
  const [path, qs] = h.split("?");
  const parts = path.split("/");
  return { route: parts[0], id: parts[1], params: Object.fromEntries(new URLSearchParams(qs || "")) };
}
async function router() {
  const { route, id, params } = parseHash();
  state.route = route;
  if (route !== "book") state.lastListHash = location.hash || "#/library";
  $("#view").onclick = null;
  closeSidebar();
  clearSelection();
  window.scrollTo({ top: 0 });
  if (route === "discover") return renderDiscover(params);
  if (route === "downloads") return renderDownloads();
  if (route === "authors") return renderAuthors();
  if (route === "author") return renderAuthor(params.name || "");
  if (route === "book") return renderBookPage(Number(id));
  if (route === "shelves") return renderShelves();
  if (route === "shelf") { state.filters = { shelf: Number(id) }; return renderLibrary(); }
  state.filters = { tag: params.tag, author: params.author, series: params.series };
  if (params.q !== undefined) { state.q = params.q; $("#search").value = params.q; }
  return renderLibrary();
}

/* ───────────────────────── rows (horizontal carousels) ───────────────────────── */
function rowSection(title, books, { seeAll = "", icon = "", currentId = null, idx = false, big = false } = {}) {
  if (!books.length) return "";
  return `<section class="section"><div class="row-head"><h2>${icon}${esc(title)}</h2>
      ${seeAll ? `<a class="see-all" href="${seeAll}">See all</a>` : ""}
      <div class="row-nav" ${seeAll ? "" : 'style="margin-left:auto"'}><button class="icon-btn" data-scroll="-1" aria-label="Scroll left">‹</button><button class="icon-btn" data-scroll="1" aria-label="Scroll right">›</button></div></div>
    <div class="row-scroll ${big ? "big" : ""}">${books.map((b) => cardHTML(b, { current: b.id === currentId, idx })).join("")}</div></section>`;
}

/* ───────────────────────── library & home ───────────────────────── */
function libraryHeader() {
  const f = state.filters;
  if (f.shelf) {
    const s = state.shelves.find((x) => x.id === f.shelf) || { id: f.shelf, name: "Shelf", covers: [] };
    const covers = (s.covers || []).slice(0, 4);
    const mosaic = covers.map((id) => `<img src="/api/books/${id}/cover?thumb=1" alt="">`).join("") + "<div></div>".repeat(Math.max(0, 4 - covers.length));
    return `<div class="hero" style="${heroTint(shelfColor(s))}">
      ${covers.length ? `<div class="hero-bg" style="background-image:url('/api/books/${covers[0]}/cover?thumb=1')"></div>` : ""}
      <div class="hero-inner"><div class="mosaic">${mosaic}</div><div>
        <div class="kicker">Bookshelf</div><h1>${esc((s.icon || "📚") + " " + s.name)}</h1>
        <div class="sub">${s.description ? esc(s.description) + " · " : ""}<span id="count-label"></span>${state.sort === "added" ? " · drag covers to reorder" : ""}</div>
        <div class="hero-actions"><button class="btn" id="edit-shelf">Edit shelf</button><a class="btn" href="#/shelves">All shelves</a></div>
      </div></div></div>`;
  }
  let title = "Library", crumb = "";
  if (f.tag) { title = f.tag; crumb = "Tag"; }
  if (f.author) { title = f.author; crumb = "Author"; }
  if (f.series) { title = f.series; crumb = "Series"; }
  if (state.q && !crumb) { title = `“${state.q}”`; crumb = "Search"; }
  const unmatched = state.stats.unmatched;
  return `<div class="view-head"><div>
      ${crumb ? `<div class="sub" style="margin:0 0 4px">${crumb} · <a href="#/library" style="color:var(--accent);font-weight:600">clear</a></div>` : ""}
      <h1>${esc(title)}</h1><div class="sub" id="count-label"></div></div>
      <div class="actions">${f.author ? `<a class="btn" href="${authorLink(f.author)}">Author page</a>` : ""}${!crumb && unmatched ? `<button class="btn" id="match-all" title="Look up lubimyczytac.pl metadata for books never matched">✦ Match ${unmatched} with lubimyczytac</button>` : ""}</div></div>`;
}
async function fillHomeRows() {
  const box = $("#home-rows");
  if (!box) return;
  const recent = await api("/api/books?sort=added&limit=20");
  const shelves = state.shelves.filter((s) => s.count);
  const shelfBooks = await Promise.all(shelves.map((s) => api(`/api/books?shelf=${s.id}&limit=20`)));
  if (!$("#home-rows")) return;
  box.innerHTML = rowSection("Recently added", recent.books, { big: true }) +
    shelves.map((s, i) => rowSection(s.name, shelfBooks[i].books, { icon: shelfIcon(s), seeAll: `#/shelf/${s.id}` })).join("");
}
async function renderLibrary(append = false) {
  const view = $("#view");
  const f = state.filters;
  const isHome = !f.shelf && !f.tag && !f.author && !f.series && !state.q;
  if (!append) {
    state.offset = 0;
    view.innerHTML = libraryHeader() +
      (isHome ? `<div id="home-rows"></div><div class="row-head"><h2>All books</h2></div>` : "") +
      `<div id="books" class="${state.view}">${'<div class="card skeleton"><div class="cover"></div></div>'.repeat(state.view === "grid" ? 12 : 0)}</div><div class="more" id="more"></div>`;
    $("#edit-shelf")?.addEventListener("click", () => editShelf(state.shelves.find((s) => s.id === f.shelf)));
    $("#match-all")?.addEventListener("click", matchAll);
    if (isHome) { fillHomeRows(); repairBanner(); }
  }
  markActiveNav();
  const params = new URLSearchParams({ q: state.q, sort: state.sort, offset: state.offset, limit: PAGE });
  for (const k of ["tag", "author", "series", "shelf"]) if (f[k]) params.set(k, f[k]);
  let data;
  try { data = await api("/api/books?" + params); } catch (e) { view.innerHTML = `<div class="empty-state"><h2>Couldn't load books</h2>${esc(e.message)}</div>`; return; }
  state.books = append ? state.books.concat(data.books) : data.books;
  state.total = data.total;
  $("#count-label") && ($("#count-label").textContent = plural(data.total, "book"));
  const box = $("#books");
  if (!box) return;
  if (!state.books.length) {
    box.className = "";
    box.innerHTML = emptyState(f);
    $("#more").innerHTML = "";
    return;
  }
  box.className = state.view;
  const html = data.books.map((b) => state.view === "grid" ? cardHTML(b) : rowHTML(b)).join("");
  if (append) box.insertAdjacentHTML("beforeend", html); else box.innerHTML = html;
  $("#more").innerHTML = state.books.length < state.total ? `<button class="btn" id="load-more">Show more (${state.total - state.books.length} left)</button>` : "";
  $("#load-more")?.addEventListener("click", () => { state.offset += PAGE; renderLibrary(true); });
}
function emptyState(f) {
  if (f.shelf) return `<div class="empty-state"><div class="big">🪵</div><h2>This shelf is empty</h2>Drag books onto the shelf in the sidebar, or open a book and tick this shelf.</div>`;
  if (state.q || f.tag || f.author || f.series) return `<div class="empty-state"><div class="big">🔍</div><h2>Nothing found</h2>Try another search — or <a href="${link("discover", { q: state.q })}">look for it in Discover</a>.</div>`;
  return `<div class="empty-state"><div class="big">📚</div><h2>Your library is empty</h2>Drop e-book files anywhere on this page, put them in the ingest folder, or <a href="#/discover">discover books</a>.</div>`;
}
function cardHTML(b, { current = false, idx = false } = {}) {
  const sel = state.selected.has(b.id);
  return `<div class="card ${sel ? "selected" : ""} ${current ? "current" : ""}" tabindex="0" draggable="true" data-book="${b.id}">
    ${!idx && b.series ? `<span class="pill series">${esc(b.series_index != null ? "#" + fmtIdx(b.series_index) : "▤")}</span>` : ""}
    ${b.rating ? `<span class="pill">★ ${b.rating.toFixed(1)}</span>` : ""}
    <span class="select-box" data-select="${b.id}">${sel ? "✓" : ""}</span>
    ${idx && b.series_index != null ? coverHTML(b).replace('<div class="cover">', `<div class="cover"><span class="idx">${esc(fmtIdx(b.series_index))}</span>`) : coverHTML(b)}
    <div class="card-title" title="${esc(b.title)}">${esc(b.title)}</div>
    <div class="card-author">${esc((b.authors || []).join(", ") || "Unknown author")}</div>
    ${(b.formats || "").includes(",") ? `<div class="card-fmts">${esc(b.formats.split(",").join(" · ").toUpperCase())}</div>` : ""}
  </div>`;
}
function rowHTML(b) {
  return `<div class="row" data-book="${b.id}" draggable="true">${coverHTML(b)}
    <div><div class="t">${esc(b.title)}</div><div class="m">${esc(seriesLabel(b))}</div></div>
    <div class="m">${esc((b.authors || []).join(", "))}</div>
    <div class="m hide-m">${esc((b.tags || []).slice(0, 3).join(", "))}</div>
    <div class="m hide-m">${b.rating ? "★ " + b.rating.toFixed(1) : ""}</div>
    <div class="fmt">${esc((b.formats || b.format).split(",").join(" · "))}</div></div>`;
}

/* selection */
function toggleSelect(id) {
  state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id);
  $$(`.card[data-book="${id}"]`).forEach((card) => { card.classList.toggle("selected", state.selected.has(id)); $(".select-box", card).textContent = state.selected.has(id) ? "✓" : ""; });
  updateBulk();
}
function clearSelection() { state.selected.clear(); $$(".card.selected").forEach((c) => { c.classList.remove("selected"); $(".select-box", c).textContent = ""; }); updateBulk(); }
function updateBulk() {
  const n = state.selected.size;
  $("#bulkbar").hidden = n === 0;
  $("#bulk-count").textContent = `${n} selected`;
  $("#bulk-merge").hidden = n < 2;
  $$("#books, .row-scroll").forEach((el) => el.classList.toggle("selecting", n > 0));
}

/* ───────────────────────── shelves ───────────────────────── */
function renderShelves() {
  markActiveNav();
  const cards = state.shelves.map((s) => {
    const covers = (s.covers || []).slice(0, 3);
    const rot = [-6, 3, 8];
    return `<div class="shelf-card" data-go="#/shelf/${s.id}" style="--shelf:${shelfColor(s)}">
      <div class="collage">${covers.map((id, i) => `<img src="/api/books/${id}/cover?thumb=1" alt="" style="--r:${rot[i]}deg">`).join("")}</div>
      <div class="shade"></div>
      <div class="info"><span class="ic">${esc(s.icon || "📚")}</span><h3>${esc(s.name)}</h3><p>${plural(s.count, "book")}${s.description ? " · " + esc(s.description) : ""}</p></div></div>`;
  }).join("");
  $("#view").innerHTML = `<div class="view-head"><div><h1>Bookshelves</h1><div class="sub">${plural(state.shelves.length, "shelf").replace("shelfs", "shelves")} · drag any book onto a shelf in the sidebar to add it</div></div></div>
    <div class="shelves-grid">${cards}<div class="shelf-card add" id="add-shelf-card">＋ New shelf</div></div>`;
  $("#add-shelf-card").onclick = () => editShelf(null);
}
async function editShelf(shelf, addBookId) {
  const color = shelf ? shelfColor(shelf) : SHELF_COLORS[state.shelves.length % SHELF_COLORS.length];
  const icon = shelf?.icon || "📚";
  const res = await formModal(`<h2>${shelf ? "Edit shelf" : "New bookshelf"}</h2>
    <input name="name" value="${esc(shelf?.name || "")}" placeholder="e.g. To read, Fantasy, Kids" required autofocus>
    <input name="description" value="${esc(shelf?.description || "")}" placeholder="Description (optional)">
    <div class="field-label">Icon</div>
    <div class="swatches">${SHELF_ICONS.map((ic) => `<label><input type="radio" name="icon" value="${ic}" ${ic === icon ? "checked" : ""}><span>${ic}</span></label>`).join("")}</div>
    <div class="field-label">Colour</div>
    <div class="swatches colors">${SHELF_COLORS.map((c) => `<label><input type="radio" name="color" value="${c}" ${c === color ? "checked" : ""}><span style="background:${c}"></span></label>`).join("")}</div>
    <div class="modal-actions">${shelf ? `<button type="button" class="btn danger" data-delete style="margin-right:auto">Delete shelf</button>` : ""}
      <button value="cancel" class="btn" formnovalidate>Cancel</button><button value="ok" class="btn primary">${shelf ? "Save" : "Create"}</button></div>`,
  (body) => { $("[data-delete]", body)?.addEventListener("click", async () => {
      $("#modal").close("cancel");
      if (await confirmModal("Delete this shelf?", "The books stay in your library.", "Delete shelf")) {
        await api(`/api/shelves/${shelf.id}`, { method: "DELETE" }); toast("Shelf deleted"); await loadSidebar(); location.hash = "#/shelves";
      }
    }); });
  if (!res) return null;
  try {
    let s;
    if (shelf) s = await api(`/api/shelves/${shelf.id}`, { method: "PATCH", json: res });
    else s = await api("/api/shelves", { method: "POST", json: res });
    if (addBookId) await api(`/api/shelves/${s.id}/books`, { method: "POST", json: { book_ids: [addBookId] } });
    await loadSidebar();
    if (addBookId) renderBookPage(addBookId);
    else if (shelf) router(); else location.hash = `#/shelf/${s.id}`;
    toast(shelf ? "Shelf saved" : `Shelf “${s.name}” created`);
    return s;
  } catch (e) { toast(e.message, true); return null; }
}
async function addToShelf(shelfId, ids) {
  try {
    await api(`/api/shelves/${shelfId}/books`, { method: "POST", json: { book_ids: ids } });
    const s = state.shelves.find((x) => x.id === shelfId);
    toast(`Added ${plural(ids.length, "book")} to ${s ? s.name : "shelf"}`);
    loadSidebar();
  } catch (e) { toast(e.message, true); }
}

/* ───────────────────────── authors ───────────────────────── */
function surnameFirst(n) {
  n = (n || "").trim().replace(/\s+/g, " ");
  if (!n || n.includes(",")) return n;
  const parts = n.split(" ");
  if (parts.length < 2) return n;
  const particles = new Set(["de", "da", "di", "du", "del", "della", "der", "den", "des", "la", "le", "van", "von", "ten", "ter", "dos", "das"]);
  let i = parts.length - 1;
  while (i > 1 && particles.has(parts[i - 1].toLowerCase())) i--;
  return `${parts.slice(i).join(" ")}, ${parts.slice(0, i).join(" ")}`;
}
function portraitHTML(a) {
  if (a.photo && a.id) return `<div class="portrait"><img loading="lazy" src="/api/authors/${a.id}/photo" alt=""></div>`;
  const h = hue(a.name);
  if (a.covers && a.covers.length) {
    const rot = a.covers.length === 1 ? [0] : a.covers.length === 2 ? [-8, 8] : [-12, 0, 12];
    return `<div class="portrait" style="background:linear-gradient(150deg,hsl(${h} 50% 55%),hsl(${(h + 40) % 360} 50% 35%))"><div class="fan">
      ${a.covers.map((id, i) => `<img loading="lazy" src="/api/books/${id}/cover?thumb=1" alt="" style="--r:${rot[i]}deg">`).join("")}</div></div>`;
  }
  return `<div class="portrait" style="background:linear-gradient(150deg,hsl(${h} 55% 55%),hsl(${(h + 40) % 360} 55% 35%))"><span class="initials">${esc(initials(a.name))}</span></div>`;
}
let authorsTimer = null;
async function renderAuthors() {
  markActiveNav();
  clearTimeout(authorsTimer);
  const view = $("#view");
  if (!$("#authors")) {
    view.innerHTML = `<div class="view-head"><div><h1>Authors</h1><div class="sub" id="count-label"></div></div>
      <div class="actions"><span class="job-note" id="author-job"></span><div class="seg" id="author-order"><button data-o="surname">Surname</button><button data-o="first">First name</button></div></div></div>
      <div id="authors"><div class="loading"><span class="spinner"></span></div></div>`;
  }
  const { authors, job } = await api("/api/authors");
  if (state.route !== "authors") return;
  $("#count-label").textContent = plural(authors.length, "author");
  const missing = authors.filter((a) => a.status === "none").length;
  $("#author-job").innerHTML = job.running
    ? `<span class="spinner"></span> Fetching portraits ${job.done}/${job.total}…`
    : missing ? `<button class="btn" id="fetch-portraits">Fetch ${missing} portrait${missing === 1 ? "" : "s"} from lubimyczytac</button>` : "";
  $("#fetch-portraits")?.addEventListener("click", async () => { await api("/api/authors/fetch-all", { method: "POST" }); renderAuthors(); });
  const draw = (order) => {
    localStorage.setItem("bookhoard-author-order", order);
    $$("#author-order button").forEach((b) => b.classList.toggle("active", b.dataset.o === order));
    const items = authors.map((a) => ({ ...a, label: order === "surname" ? (a.sort || surnameFirst(a.name)) : a.name }))
      .sort((x, y) => x.label.localeCompare(y.label, "pl", { sensitivity: "base" }));
    let last = "", html = "";
    for (const a of items) {
      const k = (a.label[0] || "#").toLocaleUpperCase("pl");
      if (k !== last) { html += `<div class="letter-tile"><span>${esc(k)}</span></div>`; last = k; }
      html += `<a class="author-tile" href="${authorLink(a.name)}">${portraitHTML(a)}<div class="n">${esc(a.label)}</div><div class="c">${plural(a.count, "book")}</div></a>`;
    }
    $("#authors").innerHTML = items.length ? `<div class="author-grid">${html}</div>` : `<div class="empty-state"><div class="big">✒️</div><h2>No authors yet</h2></div>`;
  };
  $("#author-order").onclick = (e) => { const o = e.target.closest("button")?.dataset.o; if (o) draw(o); };
  draw(localStorage.getItem("bookhoard-author-order") || "surname");
  if (job.running) authorsTimer = setTimeout(renderAuthors, 3000);
}
async function renderAuthor(name) {
  markActiveNav();
  const view = $("#view");
  view.innerHTML = `<a class="back-link" href="#/authors">‹ Authors</a><div class="loading"><span class="spinner"></span> Loading ${esc(name)}…</div>`;
  let a;
  try { a = await api("/api/authors/profile?" + new URLSearchParams({ name, fetch: "1" })); }
  catch (e) { view.innerHTML = `<div class="empty-state"><h2>Couldn't load author</h2>${esc(e.message)}</div>`; return; }
  if (state.route !== "author") return;
  const first = a.books.find((b) => b.cover);
  const bg = a.photo ? `/api/authors/${a.id}/photo` : first ? coverURL(first) : "";
  const dates = a.born ? (a.died ? `${a.born.slice(0, 4)}–${a.died.slice(0, 4)}` : `born ${a.born.slice(0, 4)}`) : "";
  const groups = {}, standalone = [];
  for (const b of a.books) (b.series ? (groups[b.series] ||= []) : standalone).push(b);
  const portrait = a.photo ? `<img src="/api/authors/${a.id}/photo" alt="">` : `<span class="initials">${esc(initials(name))}</span>`;
  view.innerHTML = `<a class="back-link" href="#/authors">‹ Authors</a>
    <div class="hero author-page" style="${heroTint(first?.cover_color, name)}">
      ${bg ? `<div class="hero-bg" style="background-image:url('${bg}')"></div>` : ""}
      <div class="hero-inner"><div class="big-portrait">${portrait}</div><div>
        <div class="kicker">Author</div><h1>${esc(name)}</h1>
        <div class="sub">${dates ? esc(dates) + " · " : ""}${plural(a.total, "book")} in your library${Object.keys(groups).length ? " · " + plural(Object.keys(groups).length, "series").replace("seriess", "series") : ""}</div>
        ${a.bio ? `<div class="author-bio ${a.bio.length > 360 ? "clamped" : ""}" id="author-bio">${esc(a.bio)}</div>` : ""}
        <div class="hero-actions">
          ${a.bio && a.bio.length > 360 ? `<button class="btn" id="bio-more">Read more</button>` : ""}
          ${a.lc_url ? `<a class="btn" href="${esc(a.lc_url)}" target="_blank" rel="noopener">lubimyczytac ↗</a>` : ""}
          <button class="btn" id="author-refresh">⟳ ${a.status === "ok" ? "Refresh profile" : "Look up profile"}</button>
        </div>
        ${a.status === "nomatch" ? `<div class="sub" style="margin-top:12px">Not found on lubimyczytac.pl.</div>` : ""}
      </div></div></div>
    ${Object.entries(groups).sort(([x], [y]) => x.localeCompare(y, "pl")).map(([s, books]) => rowSection(s, books, { idx: true, seeAll: link("library", { series: s }) })).join("")}
    ${standalone.length ? `<div class="row-head"><h2>${Object.keys(groups).length ? "Other books" : "Books"}</h2></div><div class="grid">${standalone.map((b) => cardHTML(b)).join("")}</div>` : ""}`;
  $("#bio-more")?.addEventListener("click", (e) => { $("#author-bio").classList.remove("clamped"); e.target.remove(); });
  $("#author-refresh").onclick = async (e) => {
    e.target.innerHTML = `<span class="spinner"></span>`;
    await api("/api/authors/refresh", { method: "POST", json: { name } });
    renderAuthor(name);
  };
}

/* ───────────────────────── book page ───────────────────────── */
function openBook(id) { location.hash = `#/book/${id}`; }
function closeDrawer() { location.hash = state.lastListHash || "#/library"; }
async function renderBookPage(id) {
  markActiveNav();
  $("#view").innerHTML = `<div class="loading"><span class="spinner"></span> Loading…</div>`;
  try { renderBook(await api(`/api/books/${id}`)); }
  catch (e) { $("#view").innerHTML = `<div class="empty-state"><h2>Book not found</h2>${esc(e.message)}</div>`; }
}
function renderBook(b) {
  if (state.route !== "book") { location.hash = `#/book/${b.id}`; return; }
  const view = $("#view");
  const metaBadge = { matched: "✓ lubimyczytac", manual: "edited", nomatch: "no lubimyczytac match", none: "not matched yet" }[b.meta_status] || "";
  const year = (b.published || "").slice(0, 4);
  const by = (b.authors || []).map((a, i) => `${i === 0 ? '<span class="ava" id="d-ava"></span>' : ""}<a class="link" href="${authorLink(a)}">${esc(a)}</a>`).join(", ") || "Unknown author";
  view.innerHTML = `
    <a class="back-link" data-close>‹ Back</a>
    <div class="hero book-page" style="${heroTint(b.cover_color, b.title)}">
      ${b.cover ? `<div class="hero-bg" style="background-image:url('${coverURL(b)}')"></div>` : ""}
      <div class="hero-inner">
        <div class="book3d"><div class="b"><div class="back"></div>${coverHTML(b, false)}<div class="pages"></div></div></div>
        <div class="book-meta">
          ${b.series ? `<div class="kicker"><a class="link" href="${link("library", { series: b.series })}">${esc(b.series)}</a>${b.series_index != null ? ` · Book ${esc(fmtIdx(b.series_index))}` : ""}</div>` : ""}
          <h1>${esc(b.title)}</h1>
          <div class="by">${by}</div>
          <div class="facts">${b.rating ? `<span>${stars(b.rating)}</span>` : ""}${year ? `<span>${esc(year)}</span>` : ""}${b.pages ? `<span>${b.pages} pages</span>` : ""}${b.publisher ? `<span>${esc(b.publisher)}</span>` : ""}${b.language ? `<span>${esc(b.language.toUpperCase())}</span>` : ""}</div>
          <div class="hero-actions">
            ${(b.files || []).map((f, i) => `<a class="btn ${i === 0 ? "primary" : ""}" href="/download/${b.id}/f/${f.id}/${encodeURIComponent(b.title)}${fmtSuffix(f.format)}">⬇ ${esc(f.format.toUpperCase())}</a>`).join("")}
            <button class="btn" data-act="match">✦ lubimyczytac</button>
            <button class="btn" data-act="edit">✎ Edit</button>
            <button class="btn" data-act="delete" title="Delete book" aria-label="Delete book">🗑</button>
          </div>
        </div>
      </div>
    </div>
    <div class="book-body">
      <div>
        ${b.description ? `<div class="d-block"><h4>About this book</h4><div class="d-desc ${b.description.length > 700 ? "clamped" : ""}" id="d-desc">${esc(b.description)}</div>
          ${b.description.length > 700 ? `<button class="btn small" id="more-desc" style="margin-top:10px">Read more</button>` : ""}</div>` : ""}
        <div class="d-block"><h4>Tags</h4><div class="chips" id="d-tags">
          ${(b.tags || []).map((t) => `<span class="chip"><a href="${link("library", { tag: t })}">${esc(t)}</a><span class="x" data-untag="${esc(t)}" title="Remove tag">✕</span></span>`).join("")}
          <input id="add-tag" list="tag-options" placeholder="＋ add tag" style="width:130px;padding:3px 12px;border-radius:99px;font-size:12.5px">
          <datalist id="tag-options">${state.tags.map((t) => `<option value="${esc(t.name)}">`).join("")}</datalist>
        </div></div>
        <div class="d-block"><h4>Bookshelves</h4><div class="chips" id="d-shelves">
          ${state.shelves.map((s) => `<button class="chip shelf-toggle ${b.shelves.includes(s.id) ? "active" : ""}" data-shelf-toggle="${s.id}"><span class="shelf-icon">${esc(s.icon || "📚")}</span>${esc(s.name)}</button>`).join("")}
          <button class="chip" data-act="new-shelf">＋ New shelf</button></div></div>
        <div id="d-extra"></div>
      </div>
      <aside class="panel">
        <h4>Details</h4>
        <dl class="kv">
          ${[["Series", seriesLabel(b)], ["Published", b.published], ["Publisher", b.publisher], ["Translator", b.translator], ["Pages", b.pages],
             ["Language", b.language], ["ISBN", b.isbn], ["Sorted as", b.author_sort], ["Metadata", metaBadge],
             ["Added", new Date(b.added_at * 1000).toLocaleDateString()]].filter(([, v]) => v).map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}
          ${b.lc_url ? `<dt>Source</dt><dd><a href="${esc(b.lc_url)}" target="_blank" rel="noopener" style="color:var(--accent)">lubimyczytac ↗</a></dd>` : ""}
        </dl>
        <h4 style="margin-top:22px">Files</h4>
        <div class="file-list">${(b.files || []).map((f) => `<div class="file-row">
          <span class="fmt-badge">${esc(f.format.toUpperCase())}</span><span class="m">${fmtSize(f.size)}</span>
          <button class="icon-btn" data-rm-file="${f.id}" title="Remove this format" aria-label="Remove ${esc(f.format)}">✕</button>
          <span class="fp" title="${esc(f.file_path)}">${esc(f.file_path.split("/").pop())}</span></div>`).join("")}</div>
      </aside>
    </div>
    <div id="d-related"></div>`;

  // author avatar + related rows load after the page is visible
  if (b.authors?.length) {
    api("/api/authors/profile?" + new URLSearchParams({ name: b.authors[0] })).then((a) => {
      const el = $("#d-ava");
      if (!el) return;
      el.outerHTML = a.photo ? `<img src="/api/authors/${a.id}/photo" alt="">` : `<span class="ava">${esc(initials(b.authors[0]))}</span>`;
    }).catch(() => $("#d-ava")?.remove());
  } else $("#d-ava")?.remove();
  api(`/api/books/${b.id}/related`).then((r) => {
    const box = $("#d-related");
    if (!box || state.route !== "book") return;
    box.innerHTML = (r.series.length > 1 ? rowSection(b.series, r.series, { idx: true, currentId: b.id, seeAll: link("library", { series: b.series }) }) : "") +
      (r.by_author.length ? rowSection(`More by ${b.authors[0]}`, r.by_author, { seeAll: authorLink(b.authors[0]) }) : "");
    const cur = $(".row-scroll .card.current");
    if (cur) { const row = cur.parentElement; row.scrollLeft = cur.offsetLeft - row.offsetLeft - (row.clientWidth - cur.clientWidth) / 2; }
  }).catch(() => {});

  view.onclick = async (e) => {
    const t = e.target;
    if (t.closest("[data-close]")) { e.preventDefault(); return closeDrawer(); }
    const st = t.closest("[data-shelf-toggle]");
    if (st) {
      const sid = Number(st.dataset.shelfToggle);
      const on = st.classList.toggle("active");
      try {
        if (on) await api(`/api/shelves/${sid}/books`, { method: "POST", json: { book_ids: [b.id] } });
        else await api(`/api/shelves/${sid}/books/${b.id}`, { method: "DELETE" });
        b.shelves = on ? [...b.shelves, sid] : b.shelves.filter((x) => x !== sid);
        loadSidebar();
      } catch (err) { toast(err.message, true); }
      return;
    }
    const rf = t.closest("[data-rm-file]");
    if (rf) {
      const f = b.files.find((x) => x.id === Number(rf.dataset.rmFile));
      const last = b.files.length === 1;
      if (await confirmModal(last ? "Delete this book?" : `Remove the ${f.format.toUpperCase()} file?`,
          last ? `This is the only file, so “${b.title}” will be removed from the library.` : `The file is deleted from disk; the book and its other formats stay.`,
          last ? "Delete book" : "Remove file")) {
        const r = await api(`/api/books/${b.id}/files/${f.id}`, { method: "DELETE" });
        if (r.deleted) { toast("Book deleted"); closeDrawer(); }
        else { renderBook(r); toast(`${f.format.toUpperCase()} removed`); }
        loadSidebar();
      }
      return;
    }
    const ut = t.closest("[data-untag]");
    if (ut) return saveBook(b.id, { tags: b.tags.filter((x) => x !== ut.dataset.untag) });
    if (t.id === "more-desc") { $("#d-desc").classList.remove("clamped"); t.remove(); return; }
    const act = t.closest("[data-act]")?.dataset.act;
    if (act === "edit") return editBook(b);
    if (act === "match") return matchBook(b);
    if (act === "new-shelf") return editShelf(null, b.id);
    if (act === "delete") {
      if (await confirmModal("Delete this book?", `“${b.title}” and all its files will be removed from the library.`, "Delete")) {
        await api(`/api/books/${b.id}`, { method: "DELETE" });
        toast("Book deleted"); loadSidebar(); closeDrawer();
      }
    }
  };
  $("#add-tag").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.value.trim()) {
      const v = e.target.value.trim();
      if (!b.tags.includes(v)) saveBook(b.id, { tags: [...b.tags, v] });
    }
  });
  $("#add-tag").addEventListener("change", (e) => {
    const v = e.target.value.trim();
    if (v && state.tags.some((t) => t.name === v) && !b.tags.includes(v)) saveBook(b.id, { tags: [...b.tags, v] });
  });
}
async function saveBook(id, patch, msg) {
  try {
    const b = await api(`/api/books/${id}`, { method: "PATCH", json: patch });
    renderBook(b);
    if (msg) toast(msg);
    loadSidebar();
  } catch (e) { toast(e.message, true); }
}
function refreshCard(b) {
  $$(`[data-book="${b.id}"]`).forEach((el) => { if (el.closest("#books")) el.outerHTML = (state.view === "grid" ? cardHTML : rowHTML)(b); });
}

function editBook(b) {
  const box = $("#d-extra");
  box.innerHTML = `<div class="d-block"><h4>Edit metadata</h4><form class="form" id="edit-form">
    <label class="full">Title<input name="title" value="${esc(b.title)}" required></label>
    <label class="full">Authors <small>(separate with &amp;)</small><input name="authors" value="${esc((b.authors || []).join(" & "))}"></label>
    <label class="full">Sort authors as <small>(Surname, First name — filled in automatically; fix it for names it gets wrong)</small><input name="author_sort" value="${esc(b.author_sort || "")}" placeholder="Sapkowski, Andrzej"></label>
    <label>Series<input name="series" value="${esc(b.series || "")}"></label>
    <label>Number in series<input name="series_index" type="number" step="0.1" value="${b.series_index ?? ""}"></label>
    <label class="full">Tags <small>(comma separated)</small><input name="tags" value="${esc((b.tags || []).join(", "))}"></label>
    <label>Publisher<input name="publisher" value="${esc(b.publisher || "")}"></label>
    <label>Published<input name="published" value="${esc(b.published || "")}" placeholder="YYYY or YYYY-MM-DD"></label>
    <label>Language<input name="language" value="${esc(b.language || "")}" placeholder="pl"></label>
    <label>ISBN<input name="isbn" value="${esc(b.isbn || "")}"></label>
    <label>Translator<input name="translator" value="${esc(b.translator || "")}"></label>
    <label>Rating (0–10)<input name="rating" type="number" step="0.1" min="0" max="10" value="${b.rating ?? ""}"></label>
    <label class="full">Description<textarea name="description">${esc(b.description || "")}</textarea></label>
    <div class="full" style="display:flex;gap:8px;justify-content:flex-end;grid-column:1/-1">
      <button type="button" class="btn" id="cancel-edit">Cancel</button><button class="btn primary">Save</button></div>
  </form></div>`;
  box.scrollIntoView({ behavior: "smooth" });
  $("#cancel-edit").onclick = () => (box.innerHTML = "");
  $("#edit-form").onsubmit = (e) => {
    e.preventDefault();
    saveBook(b.id, Object.fromEntries(new FormData(e.target)), "Saved");
  };
}

/* lubimyczytac matching */
async function matchBook(b, query) {
  const box = $("#d-extra");
  box.innerHTML = `<div class="d-block"><h4>Match on lubimyczytac.pl</h4>
    <form id="lc-q" style="display:flex;gap:8px;margin-bottom:12px"><input name="q" style="flex:1" value="${esc(query || [b.title, (b.authors || [])[0]].filter(Boolean).join(" "))}"><button class="btn">Search</button></form>
    <div id="lc-results"><div class="loading"><span class="spinner"></span> Asking lubimyczytac.pl…</div></div></div>`;
  box.scrollIntoView({ behavior: "smooth" });
  $("#lc-q").onsubmit = (e) => { e.preventDefault(); matchBook(b, e.target.q.value); };
  let list;
  try { list = await api(`/api/books/${b.id}/lc/candidates?` + new URLSearchParams({ q: query || "" })); }
  catch (e) { $("#lc-results").innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
  if (!list.length) { $("#lc-results").innerHTML = `<p style="color:var(--muted)">No results. Try a shorter title or the ISBN.</p>`; return; }
  $("#lc-results").innerHTML = `<div class="match-list">${list.map((c, i) => `
    <div class="match" data-i="${i}">${c.cover_url ? `<img src="${esc(c.cover_url)}" alt="" referrerpolicy="no-referrer">` : `<div class="ph"></div>`}
      <div><div style="font-weight:600">${esc(c.title)}</div><div style="color:var(--muted);font-size:13px">${esc(c.authors.join(", "))}${c.series ? " · " + esc(c.series) : ""}${c.rating ? " · ★ " + c.rating : ""}</div></div>
      <span class="score ${c.score >= 0.8 ? "hi" : ""}">${Math.round(c.score * 100)}%</span></div>`).join("")}</div><div id="lc-preview"></div>`;
  $$(".match", box).forEach((m) => m.onclick = () => {
    $$(".match", box).forEach((x) => x.classList.remove("active"));
    m.classList.add("active");
    previewMatch(b, list[Number(m.dataset.i)]);
  });
}
async function previewMatch(b, cand) {
  const box = $("#lc-preview");
  box.innerHTML = `<div class="loading"><span class="spinner"></span> Fetching details…</div>`;
  let m;
  try { m = await api("/api/lc/book?" + new URLSearchParams({ url: cand.url })); } catch (e) { box.innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
  const fields = [
    ["title", "Title", b.title, m.title], ["authors", "Authors", (b.authors || []).join(", "), (m.authors || []).join(", ")],
    ["series", "Series", seriesLabel(b), m.series ? `${m.series}${m.series_index != null ? " #" + m.series_index : ""}` : ""],
    ["description", "Description", b.description, m.description], ["tags", "Tags", (b.tags || []).join(", "), (m.tags || []).join(", ")],
    ["rating", "Rating", b.rating, m.rating], ["publisher", "Publisher", b.publisher, m.publisher],
    ["published", "Published", b.published, m.published], ["isbn", "ISBN", b.isbn, m.isbn], ["pages", "Pages", b.pages, m.pages],
    ["translator", "Translator", b.translator, m.translator], ["language", "Language", b.language, m.language],
    ["cover", "Cover", b.cover ? "current" : "none", m.cover_url ? "new cover" : ""],
  ].filter(([, , , nv]) => nv !== null && nv !== undefined && nv !== "");
  box.innerHTML = `<div class="compare"><span></span><span class="h">Field</span><span class="h">New</span><span class="h old-h" style="text-transform:uppercase">Current</span>
    ${fields.map(([k, label, ov, nv]) => `<input type="checkbox" data-field="${k}" ${k === "series" && !nv ? "" : "checked"}><span>${label}</span>
      <span class="val">${k === "cover" && m.cover_url ? `<img src="${esc(m.cover_url)}" referrerpolicy="no-referrer" style="height:70px;border-radius:3px">` : esc(nv)}</span><span class="old val">${esc(ov ?? "")}</span>`).join("")}
  </div><div style="display:flex;justify-content:flex-end;gap:8px"><a class="btn" href="${esc(cand.url)}" target="_blank" rel="noopener">Open ↗</a><button class="btn primary" id="apply-lc">Apply selected</button></div>`;
  $("#apply-lc").onclick = async () => {
    const chosen = $$("[data-field]", box).filter((x) => x.checked).map((x) => x.dataset.field);
    if (chosen.includes("series")) chosen.push("series_index");
    $("#apply-lc").innerHTML = `<span class="spinner"></span>`;
    try {
      const nb = await api(`/api/books/${b.id}/lc/apply`, { method: "POST", json: { url: cand.url, meta: m, fields: chosen } });
      renderBook(nb); refreshCard(nb); loadSidebar(); toast("Metadata applied from lubimyczytac.pl");
    } catch (e) { toast(e.message, true); }
  };
}
async function matchAll() {
  const r = await api("/api/lc/match-all", { method: "POST" });
  toast(`Matching ${r.queued} books in the background — refresh in a few minutes`);
}

/* ───────────────────────── tags manager ───────────────────────── */
async function manageTags() {
  const tags = await api("/api/facets/tags");
  formModal(`<h2>Tags</h2><p>Rename a tag to fix typos or merge two tags into one. Clearing the name removes the tag from all books.</p>
    <button type="button" class="btn" id="tags-sync-shortcut">⟳ Refresh all tags from lubimyczytac.pl…</button>
    <div class="tag-admin">${tags.length ? tags.map((t) => `<div><span>${esc(t.name)}</span><small>${t.count}</small>
      <button type="button" class="btn small" data-rename="${esc(t.name)}">Rename</button></div>`).join("") : `<div><span style="color:var(--muted)">No tags yet</span></div>`}</div>
    <div class="modal-actions"><button value="cancel" class="btn">Close</button></div>`,
  (body) => { $("#tags-sync-shortcut", body).onclick = () => tagSyncDialog(null); $$("[data-rename]", body).forEach((btn) => btn.onclick = async () => {
    const old = btn.dataset.rename;
    const nv = prompt(`Rename “${old}” to (empty = delete):`, old);
    if (nv === null || nv === old) return;
    const r = await api("/api/tags/rename", { method: "POST", json: { old, new: nv } });
    toast(`${r.changed} book${r.changed === 1 ? "" : "s"} updated`);
    $("#modal").close(); await loadSidebar(); renderLibrary(); manageTags();
  }); });
}

/* ───────────────────────── refresh tags from lubimyczytac ───────────────────────── */
let syncTimer = null;
function tagSyncDialog(bookIds) {
  const dlg = $("#modal"), body = $("#modal-body");
  const scope = bookIds ? `the ${bookIds.length} selected book${bookIds.length === 1 ? "" : "s"}` : `all ${state.stats.books} books`;
  body.onsubmit = (e) => e.preventDefault();
  body.onkeydown = null;
  body.innerHTML = `<h2>Refresh tags from lubimyczytac.pl</h2>
    <p>Look up ${scope} on lubimyczytac.pl and update their tags.</p>
    <div class="opt-group">
      <label class="opt"><input type="radio" name="mode" value="replace" checked><span><b>Replace</b> — tags become exactly what lubimyczytac has</span></label>
      <label class="opt"><input type="radio" name="mode" value="merge"><span><b>Add</b> — keep my tags, add the ones lubimyczytac has that I don't</span></label>
    </div>
    <label class="opt"><input type="checkbox" name="review" checked><span><b>Let me review</b> the changes before anything is saved</span></label>
    <label class="opt"><input type="checkbox" name="match" checked><span>Also look up books that were never matched (takes longer)</span></label>
    <p class="hint">lubimyczytac is queried politely — roughly ${Math.max(1, Math.round((bookIds ? bookIds.length : state.stats.books) * 1.5 / 60))} min. It runs in the background; you can close this window.</p>
    <div class="modal-actions"><button type="button" class="btn" data-x>Cancel</button><button type="button" class="btn primary" data-go>Start</button></div>`;
  $("[data-x]", body).onclick = () => dlg.close();
  $("[data-go]", body).onclick = async () => {
    const f = new FormData(body);
    try {
      await api("/api/tags/sync", { method: "POST", json: { mode: f.get("mode"), review: !!f.get("review"), match_unmatched: !!f.get("match"), book_ids: bookIds || null } });
      clearSelection();
      watchTagSync(true);
    } catch (e) { toast(e.message, true); }
  };
  if (!dlg.open) dlg.showModal();
}
async function watchTagSync(showDialog) {
  clearTimeout(syncTimer);
  let st;
  try { st = await api("/api/tags/sync"); } catch (e) { return; }
  $("#sync-tags").classList.toggle("spinning", !!st.running);
  const dlg = $("#modal"), body = $("#modal-body");
  const mine = dlg.open && body.dataset.view === "tagsync";
  if (showDialog || mine) renderTagSync(st);
  if (st.running) syncTimer = setTimeout(() => watchTagSync(false), 1500);
  else if (st.finished && sessionStorage.getItem("bh-sync-notified") !== String(st.finished)) {
    sessionStorage.setItem("bh-sync-notified", String(st.finished));
    if (!mine && !showDialog) toast(`Tag refresh finished — ${st.changed} book${st.changed === 1 ? "" : "s"} to update`);
    loadSidebar();
  }
}
function tagDiff(oldT, newT) {
  const o = new Set(oldT.map((t) => t.toLowerCase())), n = new Set(newT.map((t) => t.toLowerCase()));
  return [...oldT.filter((t) => !n.has(t.toLowerCase())).map((t) => `<span class="chip removed">${esc(t)}</span>`),
          ...newT.map((t) => `<span class="chip ${o.has(t.toLowerCase()) ? "" : "added"}">${esc(t)}</span>`)].join(" ");
}
function renderTagSync(st) {
  const dlg = $("#modal"), body = $("#modal-body");
  body.dataset.view = "tagsync";
  body.onsubmit = (e) => e.preventDefault();
  const pct = st.total ? Math.round((st.done / st.total) * 100) : 0;
  const counts = `<div class="sync-counts"><span><b>${st.changed}</b> to change</span><span><b>${st.unchanged}</b> already fine</span>
    <span><b>${st.nomatch}</b> not found</span><span><b>${st.notags}</b> no tags there</span>${st.errors ? `<span class="err"><b>${st.errors}</b> errors</span>` : ""}</div>`;
  if (st.running) {
    body.innerHTML = `<h2>Refreshing tags…</h2><p>${st.done} of ${st.total} books checked (${st.mode === "replace" ? "replace" : "add"} mode).</p>
      <div class="progress"><div style="width:${pct}%"></div></div>${counts}
      <p class="hint">You can close this window — it keeps going. The ⟳ next to Tags spins while it runs.</p>
      <div class="modal-actions"><button type="button" class="btn danger" data-stop>Stop</button><button type="button" class="btn" data-x>Close</button></div>`;
    $("[data-stop]", body).onclick = () => api("/api/tags/sync/cancel", { method: "POST" });
  } else {
    const changes = st.results.filter((r) => r.status === "changed");
    const missing = st.results.filter((r) => r.status === "nomatch" || r.status === "errors");
    const pending = st.review && changes.some((r) => !r.applied);
    body.innerHTML = `<h2>${st.cancelled ? "Tag refresh stopped" : "Tag refresh finished"}</h2>${counts}
      ${changes.length ? `<div class="sync-head"><b>${pending ? "Proposed changes" : "Changed"}</b>${pending ? `<label><input type="checkbox" id="sync-all" checked> all</label>` : ""}</div>
      <div class="sync-list">${changes.map((r) => `<div class="sync-row">${pending && !r.applied ? `<input type="checkbox" data-bid="${r.id}" checked>` : `<span class="st done">✓</span>`}
        <div><div class="t"><a href="#" data-open-book="${r.id}">${esc(r.title)}</a> <small>${esc((r.authors || []).join(", "))}</small></div>
        <div class="diff">${tagDiff(r.old || [], r.new || [])}</div></div></div>`).join("")}</div>
      <p class="hint"><span class="chip added">new</span> added · <span class="chip removed">old</span> removed</p>` : `<p>No tag changes needed.</p>`}
      ${missing.length ? `<details><summary>${missing.length} book${missing.length === 1 ? "" : "s"} couldn't be matched — match them by hand</summary><div class="sync-list small">
        ${missing.map((r) => `<div><a href="#" data-open-book="${r.id}">${esc(r.title)}</a> <small>${esc((r.authors || []).join(", "))}${r.error ? " — " + esc(r.error) : ""}</small></div>`).join("")}</div></details>` : ""}
      <div class="modal-actions"><button type="button" class="btn" data-x>Close</button>${pending ? `<button type="button" class="btn primary" data-apply>Apply selected</button>` : ""}</div>`;
    $("#sync-all", body)?.addEventListener("change", (e) => $$("[data-bid]", body).forEach((c) => (c.checked = e.target.checked)));
    $("[data-apply]", body)?.addEventListener("click", async () => {
      const ids = $$("[data-bid]", body).filter((c) => c.checked).map((c) => Number(c.dataset.bid));
      if (!ids.length) return toast("Nothing selected", true);
      const r = await api("/api/tags/sync/apply", { method: "POST", json: { book_ids: ids } });
      toast(`Tags updated on ${r.applied} book${r.applied === 1 ? "" : "s"}`);
      loadSidebar(); if (state.route === "library" || state.route === "shelf") renderLibrary();
      watchTagSync(true);
    });
  }
  $("[data-x]", body).onclick = () => dlg.close();
  $$("[data-open-book]", body).forEach((a) => a.onclick = (e) => { e.preventDefault(); dlg.close(); openBook(Number(a.dataset.openBook)); });
  if (!dlg.open) dlg.showModal();
}

/* ───────────────────────── repair library ───────────────────────── */
async function repairDialog() {
  const dlg = $("#modal"), body = $("#modal-body");
  body.onsubmit = (e) => e.preventDefault(); body.onkeydown = null; body.dataset.view = "repair";
  body.innerHTML = `<h2>Repair library</h2><div class="loading"><span class="spinner"></span> Checking every book against its file names…</div>`;
  if (!dlg.open) dlg.showModal();
  let items, tagCase;
  try { const r = await api("/api/maintenance/repair"); items = r.books; tagCase = r.tag_case; } catch (e) { body.innerHTML = `<h2>Repair library</h2><p class="err">${esc(e.message)}</p>`; return; }
  const names = (a) => esc((a || []).join(", "));
  body.innerHTML = `<h2>Repair library</h2>
    ${items.length ? `<p>${plural(items.length, "book")} don't match their files — usually from a wrong lubimyczytac match.
      Title and author are restored from the <b>file names</b>, wrong details (series, description, ISBN, tags, rating, cover) are cleared,
      wrongly paired files are separated, and each EPUB is paired with its own KEPUB again. Nothing changes until you click Repair.</p>
      <div class="sync-head"><b>Proposed fixes</b><label><input type="checkbox" id="rp-all" checked> all</label></div>
      <div class="sync-list">${items.map((it) => `<div class="sync-row"><input type="checkbox" data-bid="${it.book_id}" checked>
        <div><div class="t"><span class="chip removed">${esc(it.current_title)}</span> <small>${names(it.current_authors)}</small>${it.split ? ` <span class="fmt-badge">split</span>` : ""}</div>
        ${it.groups.map((g) => `<div class="diff" style="align-items:center">→ <b>${esc(g.title)}</b> <small>${names(g.authors)}</small>
          ${g.files.map((f) => `<span class="fmt-badge" title="${esc(f.name)}">${esc(f.format.toUpperCase())}</span>`).join("")}</div>`).join("")}
        </div></div>`).join("")}</div>
      <p class="hint">After repairing, use ✦ / “Match with lubimyczytac” to fill in details again — matches are now double-checked against the title and author.</p>`
    : `<p>✓ Every book matches its files.</p>`}
    ${tagCase.length ? `<p><b>${plural(tagCase.length, "tag")}</b> exist in different capitalisation (${tagCase.slice(0, 6).map((g) => esc(g.join(" / "))).join(", ")}${tagCase.length > 6 ? "…" : ""}) and will be merged into the most used spelling.</p>` : ""}
    <div class="modal-actions"><button type="button" class="btn" data-x>Close</button>${items.length || tagCase.length ? `<button type="button" class="btn primary" data-go>${items.length ? "Repair selected" : "Merge tag spellings"}</button>` : ""}</div>`;
  $("[data-x]", body).onclick = () => dlg.close();
  $("#rp-all", body)?.addEventListener("change", (e) => $$("[data-bid]", body).forEach((c) => (c.checked = e.target.checked)));
  $("[data-go]", body)?.addEventListener("click", async (e) => {
    const ids = $$("[data-bid]", body).filter((c) => c.checked).map((c) => Number(c.dataset.bid));
    if (!ids.length && !tagCase.length) return toast("Nothing selected", true);
    e.target.innerHTML = `<span class="spinner"></span> Repairing…`; e.target.disabled = true;
    try {
      const r = await api("/api/maintenance/repair", { method: "POST", json: { book_ids: ids, tags_only: !ids.length } });
      toast(`Repaired ${plural(r.restored, "book")}${r.split ? `, separated ${r.split}` : ""}${r.merged ? `, re-paired ${r.merged}` : ""}${r.tags ? `, merged ${plural(r.tags, "tag spelling")}` : ""}`);
      await loadSidebar(); router(); repairDialog();
    } catch (err) { toast(err.message, true); e.target.disabled = false; e.target.textContent = "Repair selected"; }
  });
}
async function repairBanner() {
  try {
    const items = (await api("/api/maintenance/repair")).books;
    if (!items.length || !$("#home-rows")) return;
    $("#home-rows").insertAdjacentHTML("beforebegin", `<div class="notice" id="repair-notice">⚠️ ${plural(items.length, "book")} don't match their files (probably wrong lubimyczytac matches).
      <button class="btn small primary" style="margin-left:8px" id="repair-open">Review &amp; repair</button></div>`);
    $("#repair-open").onclick = repairDialog;
  } catch (e) { /* ignore */ }
}

/* ───────────────────────── discover (Anna's Archive) ───────────────────────── */
function renderDiscover(params = {}) {
  const s = state.stats;
  const lang = params.lang ?? s.aa_default_lang ?? "pl";
  const ext = params.ext ?? s.aa_default_ext ?? "epub";
  const source = params.source ?? s.search_source ?? "auto";
  $("#view").innerHTML = `<div class="view-head"><div><h1>Discover</h1><div class="sub">Search Anna's Archive or LibGen and send books straight to your library.</div></div></div>
    ${s.aa_api_key ? "" : `<div class="notice">No <b>AA_API_KEY</b> configured — downloads fall back to LibGen mirrors and may fail for some books. A membership key makes downloads fast and reliable.</div>`}
    <form class="discover-form" id="aa-form">
      <input type="search" name="q" placeholder="Title, author or ISBN…" value="${esc(params.q || "")}" autofocus>
      <select name="lang">${[["", "Any language"], ["pl", "Polish"], ["en", "English"], ["de", "German"], ["fr", "French"], ["es", "Spanish"], ["ru", "Russian"], ["uk", "Ukrainian"]].map(([v, l]) => `<option value="${v}" ${v === lang ? "selected" : ""}>${l}</option>`).join("")}</select>
      <select name="ext">${[["", "Any format"], ["epub", "EPUB"], ["mobi", "MOBI"], ["azw3", "AZW3"], ["fb2", "FB2"], ["pdf", "PDF"]].map(([v, l]) => `<option value="${v}" ${v === ext ? "selected" : ""}>${l}</option>`).join("")}</select>
      <select name="source" title="Where to search">${[["auto", "Auto"], ["libgen", "LibGen"], ["annas", "Anna's Archive"]].map(([v, l]) => `<option value="${v}" ${v === source ? "selected" : ""}>${l}</option>`).join("")}</select>
      <button class="btn primary">Search</button>
    </form>
    <form class="discover-form" id="link-form" style="margin-top:-8px">
      <input name="link" placeholder="…or paste an Anna's Archive / LibGen book link or MD5 to download it directly" style="flex:1;min-width:240px">
      <button class="btn">⬇ Add by link</button>
    </form><div id="aa-results"></div>`;
  markActiveNav();
  const form = $("#aa-form");
  form.onsubmit = (e) => { e.preventDefault(); const f = Object.fromEntries(new FormData(form)); history.replaceState(null, "", link("discover", f)); aaSearch(f); };
  $("#link-form").onsubmit = async (e) => {
    e.preventDefault();
    const input = e.target.link;
    if (!input.value.trim()) return;
    try {
      await api("/api/downloads", { method: "POST", json: { link: input.value.trim() } });
      input.value = "";
      toast("Queued — watch it on the Downloads page");
      pollDownloads();
    } catch (err) { toast(err.message, true); }
  };
  if (params.q) aaSearch({ q: params.q, lang, ext, source });
}
async function aaSearch(f) {
  const box = $("#aa-results");
  const where = { libgen: "LibGen", annas: "Anna's Archive" }[f.source] || "Anna's Archive / LibGen";
  box.innerHTML = `<div class="loading"><span class="spinner"></span> Searching ${where}…</div>`;
  let data, res;
  try { data = await api("/api/aa/search?" + new URLSearchParams(f)); res = data.results || []; }
  catch (e) { box.innerHTML = `<div class="notice">Search failed: ${esc(e.message)}<br>Mirror lists live in <b>AA_BASE_URLS</b> and <b>LIBGEN_BASE_URLS</b> (comma-separated, tried in order).</div>`; return; }
  const notice = data.notice ? `<div class="notice">${esc(data.notice)}</div>` : "";
  if (!res.length) { box.innerHTML = `${notice}<div class="empty-state"><div class="big">🔭</div><h2>No results</h2>Try fewer words, another language or any format.</div>`; return; }
  box.innerHTML = notice + `<div class="results">${res.map((r, i) => `<div class="result">
      ${r.cover_url ? `<div class="cover"><img loading="lazy" src="${esc(r.cover_url)}" referrerpolicy="no-referrer" alt="" onerror="this.remove()"></div>` : coverHTML({ title: r.title, authors: [r.author] })}
      <div><div class="t">${esc(r.title)}</div><div class="a">${esc(r.author)}</div>
        <div class="tagline">${[r.source === "libgen" ? "LibGen" : r.source === "annas" ? "Anna's" : "", r.ext, r.lang, r.size, r.year].filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join("")}</div>
        <div class="meta" title="${esc(r.meta)}">${esc((r.meta || "").slice(0, 140))}</div></div>
      <div>${r.in_library ? `<span class="st done">In library</span>` : `<button class="btn primary" data-dl="${i}">⬇ Get</button>`}</div></div>`).join("")}</div>`;
  $$("[data-dl]", box).forEach((btn) => btn.onclick = async () => {
    const r = res[Number(btn.dataset.dl)];
    try {
      await api("/api/downloads", { method: "POST", json: { md5: r.md5, title: r.title, author: r.author, ext: r.ext } });
      btn.outerHTML = `<span class="st queued">Queued</span>`;
      toast(`“${r.title}” queued`);
      pollDownloads();
    } catch (e) { toast(e.message, true); }
  });
}

/* ───────────────────────── downloads ───────────────────────── */
let dlTimer = null;
async function renderDownloads() {
  markActiveNav();
  const view = $("#view");
  if (!$("#dl-list")) view.innerHTML = `<div class="view-head"><div><h1>Downloads</h1><div class="sub">Finished books are imported and matched automatically.</div></div></div><div id="dl-list" class="results"></div>`;
  const list = await api("/api/downloads");
  updateDlBadge(list);
  $("#dl-list").innerHTML = list.length ? list.map((d) => `<div class="result" style="grid-template-columns:1fr auto">
      <div><div class="t">${esc(d.title || d.md5)}</div><div class="a">${esc(d.author || "")}</div>
        <div class="meta"><span class="st ${d.status}">${d.status}</span> · ${esc((d.ext || "").toUpperCase())} · ${new Date(d.created_at * 1000).toLocaleString()}</div>
        ${d.status === "downloading" ? `<div class="progress ${d.progress ? "" : "indet"}"><div style="width:${Math.round(d.progress * 100)}%"></div></div>` : ""}
        ${d.error ? `<div class="err">${esc(d.error)}</div>` : ""}</div>
      <div style="display:flex;gap:6px">${d.book_id ? `<button class="btn small" data-open="${d.book_id}">Open</button>` : ""}
        ${d.status === "error" ? `<button class="btn small" data-retry="${d.id}">Retry</button>` : ""}
        ${d.status !== "downloading" ? `<button class="icon-btn" data-rm="${d.id}" title="Remove from list">✕</button>` : ""}</div></div>`).join("")
    : `<div class="empty-state"><div class="big">⬇</div><h2>No downloads yet</h2>Find something in <a href="#/discover" style="color:var(--accent)">Discover</a>.</div>`;
  $$("[data-open]").forEach((b) => b.onclick = () => openBook(Number(b.dataset.open)));
  $$("[data-retry]").forEach((b) => b.onclick = async () => { await api(`/api/downloads/${b.dataset.retry}/retry`, { method: "POST" }); renderDownloads(); pollDownloads(); });
  $$("[data-rm]").forEach((b) => b.onclick = async () => { await api(`/api/downloads/${b.dataset.rm}`, { method: "DELETE" }); renderDownloads(); });
}
function updateDlBadge(list) {
  const active = list.filter((d) => d.status === "queued" || d.status === "downloading").length;
  $("#dl-badge").hidden = !active;
  $("#dl-badge").textContent = active;
  return active;
}
async function pollDownloads() {
  clearTimeout(dlTimer);
  try {
    const list = await api("/api/downloads");
    const before = Number($("#dl-badge").textContent || 0);
    const active = updateDlBadge(list);
    if (state.route === "downloads") renderDownloads();
    if (before && active < before) { loadSidebar(); if (state.route === "library") renderLibrary(); }
    if (active) dlTimer = setTimeout(pollDownloads, 2500);
  } catch (e) { /* ignore */ }
}

/* ───────────────────────── modals ───────────────────────── */
function formModal(html, setup) {
  return new Promise((resolve) => {
    const dlg = $("#modal"), body = $("#modal-body");
    body.innerHTML = html;
    delete body.dataset.view;
    body.noValidate = true;
    body.onsubmit = (e) => {
      e.preventDefault();
      const v = e.submitter ? e.submitter.value : "ok";
      if (v === "ok" && !body.reportValidity()) return;
      dlg.close(v || "cancel");
    };
    body.onkeydown = (e) => {
      if (e.key === "Enter" && e.target.tagName === "INPUT") {
        e.preventDefault();
        if (body.reportValidity()) dlg.close("ok");
      }
    };
    setup?.(body);
    const onClose = () => {
      dlg.removeEventListener("close", onClose);
      if (dlg.returnValue === "ok") {
        const data = Object.fromEntries(new FormData(body));
        resolve(data);
      } else resolve(null);
    };
    dlg.returnValue = "";
    dlg.addEventListener("close", onClose);
    dlg.showModal();
  });
}
async function confirmModal(title, text, okLabel = "OK") {
  const r = await formModal(`<h2>${esc(title)}</h2><p>${esc(text)}</p><div class="modal-actions"><button value="cancel" class="btn">Cancel</button><button value="ok" class="btn primary">${esc(okLabel)}</button></div>`);
  return r !== null;
}
function koreaderHelp() {
  const url = `${location.origin}/opds/`;
  formModal(`<h2>Connect KOReader</h2>
    <p>Your library is published as an OPDS catalog. On the Kobo, in KOReader:</p>
    <ol class="steps"><li>Open the <b>Search</b> menu (magnifier) → <b>OPDS catalog</b>.</li>
      <li>Tap <b>＋</b> to add a catalog and enter:</li></ol>
    <div class="code">${esc(url)}</div>
    <ol class="steps" start="3"><li>Enter your Bookhoard username and password (the ones you log in with here).</li>
      <li>Browse <i>Recently added</i>, <i>Bookshelves</i>, <i>Authors</i>, <i>Tags</i> — or search — and tap a book to download it.</li></ol>
    <p style="font-size:13px;color:var(--muted)">Use your server's LAN address (e.g. http://192.168.1.20:8080/opds/), not “localhost”. Set KOReader's download folder in the catalog settings.</p>
    <div class="modal-actions"><button value="cancel" class="btn primary">Done</button></div>`);
}

/* ───────────────────────── upload ───────────────────────── */
async function uploadFiles(files) {
  files = [...files];
  if (!files.length) return;
  toast(`Uploading ${files.length} file${files.length === 1 ? "" : "s"}…`);
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  try {
    const r = await api("/api/upload", { method: "POST", body: fd });
    toast(`Added ${r.imported.length} book${r.imported.length === 1 ? "" : "s"}`);
    await loadSidebar();
    if (state.route === "library") renderLibrary();
    setTimeout(() => { loadSidebar(); if (state.route === "library") renderLibrary(); }, 8000); // metadata arrives async
  } catch (e) { toast(e.message, true); }
}

/* ───────────────────────── sidebar on mobile ───────────────────────── */
function closeSidebar() { $("#sidebar").classList.remove("open"); $("#scrim").classList.remove("show"); }

/* ───────────────────────── wiring ───────────────────────── */
function init() {
  setTheme(document.documentElement.dataset.theme || "auto");
  $$("[data-theme-set]").forEach((b) => b.onclick = () => setTheme(b.dataset.themeSet));
  $("#sort").value = state.sort;
  $$("#view-toggle button").forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));

  $("#search").addEventListener("input", debounce((e) => {
    state.q = e.target.value.trim();
    if (state.route !== "library" && state.route !== "shelf") { location.hash = "#/library?q=" + encodeURIComponent(state.q); return; }
    renderLibrary();
  }, 250));
  $("#sort").onchange = (e) => { state.sort = e.target.value; localStorage.setItem("bookhoard-sort", state.sort); renderLibrary(); };
  $("#view-toggle").onclick = (e) => {
    const v = e.target.closest("button")?.dataset.view; if (!v) return;
    state.view = v; localStorage.setItem("bookhoard-view", v);
    $$("#view-toggle button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
    if (state.route === "library" || state.route === "shelf") renderLibrary();
  };
  $("#upload").onchange = (e) => { uploadFiles(e.target.files); e.target.value = ""; };
  $("#new-shelf").onclick = () => editShelf(null);
  $("#manage-tags").onclick = manageTags;
  $("#sync-tags").onclick = async () => {
    const st = await api("/api/tags/sync");
    if (st.running || (st.finished && st.review && st.results.some((r) => r.status === "changed" && !r.applied))) watchTagSync(true);
    else tagSyncDialog(null);
  };
  $("#bulk-sync-tags").onclick = () => tagSyncDialog([...state.selected]);
  $("#koreader-help").onclick = koreaderHelp;
  $("#repair-btn").onclick = repairDialog;
  $("#menu-btn").onclick = () => { $("#sidebar").classList.add("open"); $("#scrim").classList.add("show"); };
  $("#scrim").onclick = closeSidebar;
  $("#view").addEventListener("click", (e) => {
    const sc = e.target.closest("[data-scroll]");
    if (sc) {
      const row = sc.closest(".section")?.querySelector(".row-scroll");
      if (row) row.scrollBy({ left: Number(sc.dataset.scroll) * row.clientWidth * 0.85, behavior: "smooth" });
      return;
    }
    const sel = e.target.closest("[data-select]");
    if (sel) { e.stopPropagation(); return toggleSelect(Number(sel.dataset.select)); }
    const go = e.target.closest("[data-go]");
    if (go) { location.hash = go.dataset.go; return; }
    const card = e.target.closest("[data-book]");
    if (card) {
      if (state.selected.size || e.shiftKey || e.metaKey || e.ctrlKey) return toggleSelect(Number(card.dataset.book));
      openBook(Number(card.dataset.book));
    }
  });
  $("#view").addEventListener("keydown", (e) => {
    const card = e.target.closest?.("[data-book]");
    if (card && e.key === "Enter") openBook(Number(card.dataset.book));
  });

  // bulk actions
  $("#bulk-clear").onclick = clearSelection;
  $("#bulk-shelf").onchange = async (e) => {
    let sid = e.target.value; e.target.value = "";
    if (!sid) return;
    if (sid === "new") { const s = await editShelf(null); if (!s) return; sid = s.id; }
    await api(`/api/shelves/${sid}/books`, { method: "POST", json: { book_ids: [...state.selected] } });
    toast(`Added ${state.selected.size} book${state.selected.size === 1 ? "" : "s"} to shelf`);
    clearSelection(); loadSidebar();
  };
  $("#bulk-merge").onclick = async () => {
    const n = state.selected.size;
    if (n < 2) return toast("Select at least two books to merge", true);
    if (!(await confirmModal(`Merge ${n} books into one?`, "Their files become formats of a single book. The entry with the best metadata is kept; shelves and tags are combined. No files are deleted.", "Merge"))) return;
    try {
      const b = await api("/api/books/merge", { method: "POST", json: { book_ids: [...state.selected] } });
      toast(`Merged into “${b.title}” (${b.formats.split(",").join(", ").toUpperCase()})`);
      clearSelection(); loadSidebar(); renderLibrary();
    } catch (e) { toast(e.message, true); }
  };
  $("#bulk-tag").onclick = async () => {
    const tag = prompt("Tag to add to the selected books:");
    if (!tag?.trim()) return;
    for (const id of state.selected) {
      const b = await api(`/api/books/${id}`);
      if (!b.tags.includes(tag.trim())) await api(`/api/books/${id}`, { method: "PATCH", json: { tags: [...b.tags, tag.trim()] } });
    }
    toast("Tag added"); clearSelection(); loadSidebar(); renderLibrary();
  };

  // keyboard
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); $("#search").focus(); }
    if (e.key === "Escape" && !$("#modal").open) {
      if (state.selected.size) clearSelection();
      else if (state.route === "book" && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) closeDrawer();
    }
  });

  // drag books: onto a sidebar shelf (add) or within a shelf page (reorder)
  const DT = "application/x-bookhoard";
  document.addEventListener("dragstart", (e) => {
    const card = e.target.closest?.("[data-book]");
    if (!card) return;
    const id = Number(card.dataset.book);
    state.dragIds = state.selected.has(id) ? [...state.selected] : [id];
    e.dataTransfer.setData(DT, JSON.stringify(state.dragIds));
    e.dataTransfer.effectAllowed = "copyMove";
    card.classList.add("dragging");
  });
  document.addEventListener("dragend", () => {
    state.dragIds = null;
    $$(".dragging, .drop-target, .drag-over").forEach((el) => el.classList.remove("dragging", "drop-target", "drag-over"));
  });
  const shelfList = $("#shelf-list");
  shelfList.addEventListener("dragover", (e) => {
    const a = e.target.closest("a[data-shelf]");
    if (!a || !state.dragIds) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "copy";
    $$(".drop-target", shelfList).forEach((x) => x !== a && x.classList.remove("drop-target"));
    a.classList.add("drop-target");
  });
  shelfList.addEventListener("dragleave", (e) => e.target.closest?.("a[data-shelf]")?.classList.remove("drop-target"));
  shelfList.addEventListener("drop", (e) => {
    const a = e.target.closest("a[data-shelf]");
    if (!a || !state.dragIds) return;
    e.preventDefault(); a.classList.remove("drop-target");
    addToShelf(Number(a.dataset.shelf), state.dragIds);
    clearSelection();
  });
  const canReorder = () => state.route === "shelf" && state.sort === "added" && state.view === "grid";
  $("#view").addEventListener("dragover", (e) => {
    if (!canReorder() || !state.dragIds) return;
    const card = e.target.closest("#books .card");
    if (!card) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "move";
    $$("#books .drag-over").forEach((x) => x !== card && x.classList.remove("drag-over"));
    card.classList.add("drag-over");
  });
  $("#view").addEventListener("drop", async (e) => {
    if (!canReorder() || !state.dragIds) return;
    const target = e.target.closest("#books .card");
    if (!target) return;
    e.preventDefault();
    const box = $("#books");
    const moving = state.dragIds.map((id) => box.querySelector(`.card[data-book="${id}"]`)).filter(Boolean);
    if (moving.includes(target)) return;
    const cards = [...box.children];
    const after = cards.indexOf(moving[0]) < cards.indexOf(target);
    moving.forEach((m) => after ? target.after(m) : target.before(m));
    target.classList.remove("drag-over");
    const order = [...box.querySelectorAll(".card[data-book]")].map((c) => Number(c.dataset.book));
    if (state.books.length < state.total) toast("Showing part of the shelf — order saved for the visible books");
    try { await api(`/api/shelves/${state.filters.shelf}`, { method: "PATCH", json: { order } }); loadSidebar(); }
    catch (err) { toast(err.message, true); }
  });

  // drag & drop upload
  let dragDepth = 0;
  window.addEventListener("dragenter", (e) => { if ([...e.dataTransfer.types].includes("Files")) { dragDepth++; $("#dropzone").classList.add("show"); } });
  window.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#dropzone").classList.remove("show"); } });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; $("#dropzone").classList.remove("show"); uploadFiles(e.dataTransfer.files); });

  window.addEventListener("hashchange", router);
  loadSidebar().then(router);
  pollDownloads();
  watchTagSync(false);
}
init();
