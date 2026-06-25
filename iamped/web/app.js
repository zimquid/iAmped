const $ = (id) => document.getElementById(id);
const fmtBytes = (b) => {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; b = Number(b);
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(b < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
};
const fmtDur = (ms) => {
  const s = Math.round((ms || 0) / 1000), m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
};
const fmtTotal = (ms) => {
  const h = (ms || 0) / 3600000;
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 24) return `${h.toFixed(1)} hours`;
  return `${(h / 24).toFixed(1)} days`;
};
const stars = (r) => r ? "★".repeat(Math.round(r / 2)) : "";
const esc = (s) => (s || "").replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));

async function api(path, method = "GET", body = null) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}
function pollJob(jobId, { onProgress, onDone, onError }) {
  const tick = async () => {
    let job;
    try { job = await api(`/api/job/${jobId}`); } catch (e) { onError(e.message); return; }
    onProgress(job);
    if (job.status === "running") setTimeout(tick, 600);
    else if (job.status === "done") onDone(job);
    else onError(job.error || "Job failed");
  };
  tick();
}

const STATE = {
  view: null,          // {type:'library'|'playlist', id, title, source}
  tracks: [],          // current queue
  selected: new Set(),
  sort: "artist",
  search: "",
  offset: 0, total: 0, loading: false,
  playlists: [],
  playing: -1,
  readbackPlan: null,
  plexLoginId: null,
  plexPollTimer: null,
  manualKeys: null,
  review: [],
  currentDevice: null,
  visualizer: null,
  visualizerEnabled: false,
};

const fmtClock = (sec) => {
  sec = Math.max(0, Math.round(sec || 0));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
};

// ---- LCD + bottom bar ---------------------------------------------------
// `lcd()` drives the title/subtitle and the generic progress fill used by
// build/sync/planning jobs (no track times shown).
function lcd(main, sub, frac) {
  if (main != null) $("lcd-main").textContent = main;
  if (sub != null) $("lcd-sub").textContent = sub;
  const row = $("lcd-progrow");
  row.classList.remove("with-time");        // generic progress — no clock times
  if (frac == null) row.classList.add("hidden");
  else { row.classList.remove("hidden"); $("lcd-prog").firstElementChild.style.width = `${Math.min(100, frac * 100)}%`; }
}
// `scrub()` is the playback face of the same bar: a real iTunes-style seek bar
// flanked by elapsed / remaining time.
function scrub(frac, elapsed, dur) {
  const row = $("lcd-progrow");
  row.classList.remove("hidden"); row.classList.add("with-time");
  $("lcd-prog").firstElementChild.style.width = `${Math.min(100, (frac || 0) * 100)}%`;
  $("lcd-elapsed").textContent = fmtClock(elapsed || 0);
  $("lcd-remain").textContent = "-" + fmtClock(dur ? dur - (elapsed || 0) : 0);
}
function bottomBrowse() {
  $("bottom-info").classList.remove("hidden");
  $("capacity").classList.add("hidden"); $("cap-legend").classList.add("hidden");
  $("sync-actions").classList.add("hidden");
  const ms = STATE.tracks.reduce((a, t) => a + (t.duration_ms || 0), 0);
  const loaded = STATE.tracks.length;
  const count = (STATE.view?.type === "library" && STATE.total > loaded)
    ? `${loaded.toLocaleString()} of ${STATE.total.toLocaleString()} songs`
    : `${loaded.toLocaleString()} songs`;
  $("bottom-info").textContent = `${count} · ${fmtTotal(ms)}`;
}
function bottomDevice() {
  $("bottom-info").classList.add("hidden");
  $("capacity").classList.remove("hidden"); $("cap-legend").classList.remove("hidden");
  $("sync-actions").classList.remove("hidden");
}

// iTunes-style sync status bar: while a sync runs it replaces the capacity
// gauge in the bottom bar with a determinate progress bar + "x of y" count.
function showSyncProgress(on) {
  $("sync-progress").classList.toggle("hidden", !on);
  $("capacity").classList.toggle("hidden", on);
  $("cap-legend").classList.toggle("hidden", on);
  $("sync-actions").classList.toggle("hidden", on);
}
function setSyncProgress(label, done, total) {
  $("sp-label").textContent = label;
  $("sp-count").textContent = total ? `${done.toLocaleString()} of ${total.toLocaleString()}` : "";
  $("sp-fill").style.width = total ? `${Math.min(100, (done / total) * 100)}%` : "0%";
}

// ---- panes --------------------------------------------------------------
function selectPane(paneId, srcEl) {
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === paneId));
  document.querySelectorAll(".src-item").forEach((s) => s.classList.remove("selected"));
  if (srcEl) srcEl.classList.add("selected");
  if (paneId === "pane-device") bottomDevice();
  else if (paneId === "pane-browse") bottomBrowse();
  else { $("bottom-info").classList.remove("hidden"); $("capacity").classList.add("hidden"); $("cap-legend").classList.add("hidden"); $("sync-actions").classList.add("hidden"); $("bottom-info").textContent = "—"; }
}
document.querySelectorAll(".src-item[data-pane]").forEach((el) => { el.onclick = () => selectPane(el.dataset.pane, el); });

// ---------------------------------------------------------------- config / connect
async function loadConfig() {
  const cfg = await api("/api/config");
  $("baseurl").value = cfg.plex_baseurl || ""; $("token").value = cfg.plex_token || "";
  $("device-path").value = cfg.last_device_path || ""; $("reserve").value = cfg.reserve_mb ?? 200;
  $("strategy").value = cfg.fill_strategy || "most_played";
  $("transcode").checked = cfg.transcode_lossless !== false;
  $("mirror").checked = cfg.mirror !== false;
  for (const r of document.getElementsByName("dtype")) r.checked = r.value === (cfg.last_device_type || "massstorage");
  toggleDeviceType();
  if (!cfg.has_ffmpeg) { $("ffmpeg-note").textContent = "(ffmpeg not found — lossless copied as-is, FLAC won't play in-app)"; $("transcode").checked = false; }
  // The cached library is browsable offline; only build/play/sync need Plex.
  let st = { tracks: 0 };
  try { st = await api("/api/library/stats"); await refreshStats(); await loadSidebar(); } catch (e) {}
  if (cfg.plex_baseurl && cfg.plex_token) connect();
  if (st.tracks > 0) openLibrary();
}

async function connect() {
  const s = $("connect-status"); s.className = "status"; s.textContent = "Connecting…";
  lcd("Connecting…", "Reaching Plex Media Server");
  try {
    const res = await api("/api/connect", "POST", { baseurl: $("baseurl").value.trim(), token: $("token").value.trim() });
    if (!res.ok) throw new Error(res.error);
    await showConnected(res);
  } catch (e) { s.className = "status err"; s.textContent = `Could not connect: ${e.message}`; lcd("iAmped", "Not connected"); }
}

function showConnected(res) {
  const s = $("connect-status");
  s.className = "status ok";
  s.textContent = `Connected to ${res.server.name} (Plex ${res.server.version}).`;
  lcd("iAmped", `Connected to ${res.server.name}`);
  $("section").innerHTML = res.sections.map((x) => `<option>${esc(x)}</option>`).join("");
  $("section-wrap").classList.toggle("hidden", res.sections.length === 0);
  $("btn-build").disabled = false;
  return Promise.all([refreshStats(), loadSidebar()]);
}

async function finishPlexOAuth() {
  const s = $("oauth-status");
  s.className = "status"; s.textContent = "Connecting to Plex Media Server…";
  $("btn-oauth-server").disabled = true;
  try {
    const res = await api("/api/plex/oauth/connect", "POST", {
      login_id: STATE.plexLoginId,
      server_id: $("oauth-server").value,
    });
    $("baseurl").value = (await api("/api/config")).plex_baseurl || "";
    $("token").value = "";
    $("oauth-server-wrap").classList.add("hidden");
    $("oauth-link").classList.add("hidden");
    s.className = "status ok"; s.textContent = `Signed in and connected to ${res.server.name}.`;
    await showConnected(res);
    STATE.plexLoginId = null;
  } catch (e) {
    s.className = "status err"; s.textContent = e.message;
    $("btn-oauth-server").disabled = false;
  }
}

async function pollPlexOAuth() {
  if (!STATE.plexLoginId) return;
  const s = $("oauth-status");
  try {
    const res = await api(`/api/plex/oauth/status/${STATE.plexLoginId}`);
    if (res.status === "pending") {
      STATE.plexPollTimer = setTimeout(pollPlexOAuth, 1200);
      return;
    }
    if (res.status !== "authorized") throw new Error(res.error || "Plex sign-in expired.");
    const select = $("oauth-server");
    select.innerHTML = res.servers.map((server) => {
      const details = [server.owned ? "owned" : "shared", server.online ? "online" : "offline", server.platform].filter(Boolean).join(" · ");
      return `<option value="${esc(server.id)}">${esc(server.name)} — ${esc(details)}</option>`;
    }).join("");
    if (res.servers.length === 1) {
      await finishPlexOAuth();
    } else {
      $("oauth-server-wrap").classList.remove("hidden");
      s.textContent = "Signed in. Choose the Plex server to use.";
    }
  } catch (e) {
    STATE.plexLoginId = null;
    s.className = "status err"; s.textContent = e.message;
    $("btn-plex-oauth").disabled = false;
  }
}

async function startPlexOAuth() {
  clearTimeout(STATE.plexPollTimer);
  const s = $("oauth-status");
  $("btn-plex-oauth").disabled = true;
  $("oauth-server-wrap").classList.add("hidden");
  s.className = "status"; s.textContent = "Opening Plex sign-in in your browser…";
  try {
    const res = await api("/api/plex/oauth/start", "POST");
    STATE.plexLoginId = res.login_id;
    const link = $("oauth-link");
    link.href = res.auth_url; link.classList.remove("hidden");
    s.textContent = "Approve iAmped in Plex. This page will update automatically.";
    pollPlexOAuth();
  } catch (e) {
    s.className = "status err"; s.textContent = e.message;
    $("btn-plex-oauth").disabled = false;
  }
}

async function refreshStats() {
  const st = await api("/api/library/stats");
  $("lib-stats").innerHTML = `
    <div class="stat"><b>${st.tracks.toLocaleString()}</b><span>tracks cached</span></div>
    <div class="stat"><b>${st.playlists}</b><span>playlists</span></div>
    <div class="stat"><b>${fmtBytes(st.total_bytes)}</b><span>library (originals)</span></div>
    <div class="stat"><b>${st.cached.toLocaleString()}</b><span>files downloaded</span></div>`;
}

async function buildLibrary() {
  const s = $("build-status"); $("btn-build").disabled = true; s.className = "status"; s.textContent = "Starting…";
  try {
    const { job } = await api("/api/library/build", "POST", { section: $("section").value });
    pollJob(job, {
      onProgress: (j) => { s.textContent = j.message || j.phase; lcd("Building library", j.message || j.phase, j.total ? j.done / j.total : 0.4); },
      onDone: async (j) => { s.className = "status ok"; s.textContent = j.message; lcd("iAmped", j.message); $("btn-build").disabled = false; await refreshStats(); await loadSidebar(); openLibrary(); },
      onError: (e) => { s.className = "status err"; s.textContent = e; lcd("iAmped", "Build failed"); $("btn-build").disabled = false; },
    });
  } catch (e) { s.className = "status err"; s.textContent = e.message; $("btn-build").disabled = false; }
}

// ---------------------------------------------------------------- sidebar
const ipodSvg = '<svg viewBox="0 0 16 16"><rect x="3" y="1" width="10" height="14" rx="2" fill="none" stroke="#5f6b7e"/><rect x="4.5" y="2.5" width="7" height="5"/><circle cx="8" cy="11" r="2.2" fill="none" stroke="#5f6b7e"/></svg>';
const ejectSvg = '<svg viewBox="0 0 16 16"><path d="M8 3l5 6H3zM3 11h10v2H3z"/></svg>';
const plSvg = '<svg viewBox="0 0 16 16"><path d="M1 3h10v1.6H1zM1 6h10v1.6H1zM1 9h6v1.6H1zM12 7l3 2-3 2z"/></svg>';
const sonicSvg = '<svg viewBox="0 0 16 16"><path d="M1 8h2l1-4 2 8 2-10 2 12 2-6h2"/><path d="M1 8h2l1-4 2 8 2-10 2 12 2-6h2" fill="none" stroke="#5f6b7e" stroke-width="1.1"/></svg>';

async function loadSidebar() {
  STATE.playlists = await api("/api/playlists");
  const list = $("playlist-list");
  if (!STATE.playlists.length) { list.innerHTML = '<div class="src-item disabled">No playlists yet</div>'; }
  else {
    list.innerHTML = STATE.playlists.map((p) => `
      <div class="src-item playlist" data-id="${esc(p.id)}" data-title="${esc(p.title)}" data-source="${p.source}">
        ${p.source === "local" && p.kind === "sonic" ? sonicSvg : plSvg}${esc(p.title)}</div>`).join("");
    list.querySelectorAll(".playlist").forEach((el) => { el.onclick = () => openPlaylist(el); });
  }
  renderSyncChecklist();
  await loadDevices();
}

function renderSyncChecklist() {
  const wrap = $("playlists");
  if (!STATE.playlists.length) { wrap.innerHTML = '<div class="item muted">No playlists in the library yet.</div>'; return; }
  wrap.innerHTML = STATE.playlists.map((p) => `
    <div class="item">
      <input type="checkbox" class="pl" id="pl-${esc(p.id)}" value="${esc(p.id)}">
      <label for="pl-${esc(p.id)}">${esc(p.title)}${p.smart ? ' <span class="pill">smart</span>' : ''}${p.source === "local" ? ' <span class="pill">local</span>' : ''}</label>
      <span class="meta">${p.item_count} tracks</span>
    </div>`).join("");
}
const selectedPlaylists = () => [...document.querySelectorAll(".pl:checked")].map((c) => c.value);

async function loadDevices() {
  const vols = await api("/api/volumes");
  const list = $("device-list");
  if (!vols.length) { list.innerHTML = '<div class="src-item disabled">No device detected</div>'; return; }
  const noteSvg = '<svg viewBox="0 0 16 16"><path d="M6 2l8-1v9.2A2.5 2.5 0 1012 11V4L7 4.8V12a2.5 2.5 0 11-1-2z"/></svg>';
  list.innerHTML = vols.map((v) => `
    <div class="src-item device" data-path="${esc(v.path)}" data-ipod="${v.is_ipod}" data-free="${v.free}" data-total="${v.total}" data-name="${esc(v.name)}" data-model="${esc(v.ipod_model || "")}"${v.ipod_model ? ` title="${esc(v.ipod_model)}"` : ""}>
      ${v.is_ipod ? ipodSvg : '<svg viewBox="0 0 16 16"><rect x="1" y="3" width="14" height="10" rx="1.5" fill="none" stroke="#5f6b7e"/></svg>'}
      ${esc(v.name)}<span class="eject">${ejectSvg}</span></div>
    <div class="src-subitem device-music" data-path="${esc(v.path)}" data-ipod="${v.is_ipod}" data-name="${esc(v.name)}">
      ${noteSvg}Music on device</div>`).join("");
  list.querySelectorAll(".device").forEach((el) => {
    el.onclick = (event) => {
      if (event.target.closest(".eject")) { ejectDevice(el); return; }
      selectDevice(el);
    };
    el.ondragover = (event) => {
      if (!STATE.selected.size) return;
      event.preventDefault(); el.classList.add("drop-target");
    };
    el.ondragleave = () => el.classList.remove("drop-target");
    el.ondrop = (event) => {
      event.preventDefault(); el.classList.remove("drop-target");
      dropSelectionOnDevice(el);
    };
  });
  list.querySelectorAll(".device-music").forEach((el) => { el.onclick = () => openDeviceMusic(el); });
}

// Show what's already on a device — the iTunes "music under the device" view.
async function openDeviceMusic(el) {
  const path = el.dataset.path;
  const type = el.dataset.ipod === "true" ? "ipod" : "massstorage";
  STATE.view = { type: "device", title: el.dataset.name, path };
  $("browse-title").textContent = `${el.dataset.name} — Music`;
  $("btn-del-pl").classList.add("hidden");
  selectPane("pane-browse", el);
  STATE.tracks = []; $("browse-body").innerHTML = "";
  lcd("Reading device…", path);
  try {
    const inv = await api(`/api/device/inventory?device_path=${encodeURIComponent(path)}&device_type=${type}`);
    STATE.tracks = (inv.tracks || []).map((t, i) => ({
      rating_key: t.rating_key || `dev:${i}`,
      title: t.title, artist: t.artist, album: t.album,
      duration_ms: t.duration_ms || 0, plays: t.play_count || 0,
      rating: t.rating || 0, lossless: false, on_device: true,
    }));
    renderTracks();
    lcd("iAmped", `${STATE.tracks.length.toLocaleString()} tracks on ${el.dataset.name}`);
  } catch (e) {
    $("browse-body").innerHTML = `<div class="src-item disabled" style="padding:14px">${esc(e.message)}</div>`;
    lcd("iAmped", e.message);
  }
}

// ---------------------------------------------------------------- browser
function openLibrary() {
  STATE.view = { type: "library", title: "Music" };
  STATE.search = $("search").value.trim(); STATE.offset = 0; STATE.total = 0;
  $("browse-title").textContent = "Music"; $("btn-del-pl").classList.add("hidden");
  selectPane("pane-browse", $("src-music"));
  STATE.tracks = []; $("browse-body").innerHTML = "";
  loadMore(true);
}
async function loadMore(reset) {
  if (STATE.loading || STATE.view?.type !== "library") return;
  if (!reset && STATE.tracks.length >= STATE.total && STATE.total) return;
  STATE.loading = true;
  const r = await api(`/api/tracks?search=${encodeURIComponent(STATE.search)}&sort=${STATE.sort}&offset=${STATE.offset}&limit=300`);
  STATE.total = r.total; STATE.offset += r.tracks.length;
  const start = reset ? 0 : STATE.tracks.length;
  STATE.tracks = reset ? r.tracks : STATE.tracks.concat(r.tracks);
  if (reset) renderTracks(); else appendTracks(r.tracks, start);
  STATE.loading = false;
  // If the first page didn't fill the viewport, keep pulling so the user can
  // always scroll to reach the next page.
  const body = $("browse-body");
  if (STATE.tracks.length < STATE.total && body.scrollHeight <= body.clientHeight)
    loadMore(false);
}
async function openPlaylist(el) {
  const id = el.dataset.id;
  STATE.view = { type: "playlist", id, title: el.dataset.title, source: el.dataset.source };
  $("browse-title").textContent = el.dataset.title;
  $("btn-del-pl").classList.toggle("hidden", el.dataset.source !== "local");
  selectPane("pane-browse", el);
  const r = await api(`/api/playlist/${encodeURIComponent(id)}/tracks`);
  STATE.tracks = r.tracks; renderTracks();
}

function rowHtml(t, i) {
  return `
    <div class="songrow${STATE.selected.has(t.rating_key) ? " selected" : ""}${STATE.playing === i ? " playing" : ""}" data-idx="${i}" data-rk="${esc(t.rating_key)}">
      <div class="c-play">${STATE.playing === i ? "►" : ""}</div>
      <div class="c-name">${esc(t.title)}${t.lossless ? ' <span class="lossless-tag">FLAC</span>' : ''}</div>
      <div class="c-artist">${esc(t.artist)}</div><div class="c-album">${esc(t.album)}</div>
      <div class="c-rating">${stars(t.rating)}</div><div class="c-plays">${t.plays || 0}</div>
      <div class="c-time">${fmtDur(t.duration_ms)}</div>
    </div>`;
}
function bindRows(rows) {
  rows.forEach((row) => {
    const idx = Number(row.dataset.idx);
    row.onclick = (e) => selectRow(idx, e.metaKey || e.ctrlKey);
    row.ondblclick = () => playIndex(idx);
    row.oncontextmenu = (e) => showCtxMenu(e, idx);
    if (STATE.view?.type !== "device") {
      row.draggable = true;
      row.ondragstart = (event) => {
        if (!STATE.selected.has(row.dataset.rk)) selectRow(idx, false);
        const keys = [...STATE.selected];
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("application/x-iamped-tracks",
                                   JSON.stringify(keys));
        event.dataTransfer.setData("text/plain",
                                   `${keys.length} iAmped track(s)`);
        row.classList.add("dragging");
      };
      row.ondragend = () => row.classList.remove("dragging");
    }
  });
}
function renderTracks() {
  const body = $("browse-body");
  body.innerHTML = STATE.tracks.map((t, i) => rowHtml(t, i)).join("");
  bindRows([...body.querySelectorAll(".songrow")]);
  bottomBrowse();
}
// Append a page of rows WITHOUT rebuilding the list, so the scroll position is
// preserved and the next page can be triggered by continued scrolling.
function appendTracks(newOnes, startIdx) {
  const body = $("browse-body");
  const before = body.querySelectorAll(".songrow").length;
  body.insertAdjacentHTML("beforeend",
    newOnes.map((t, k) => rowHtml(t, startIdx + k)).join(""));
  bindRows([...body.querySelectorAll(".songrow")].slice(before));
  bottomBrowse();
}
function selectRow(idx, additive) {
  const rk = STATE.tracks[idx].rating_key;
  if (!additive) STATE.selected.clear();
  if (STATE.selected.has(rk) && additive) STATE.selected.delete(rk); else STATE.selected.add(rk);
  document.querySelectorAll("#browse-body .songrow").forEach((r) => r.classList.toggle("selected", STATE.selected.has(r.dataset.rk)));
}

// sorting
$("browse-head-row").querySelectorAll("[data-sort]").forEach((h) => {
  h.onclick = () => {
    const key = h.dataset.sort;
    document.querySelectorAll("#browse-head-row [data-sort]").forEach((x) => x.classList.remove("sorted"));
    h.classList.add("sorted"); STATE.sort = key;
    if (STATE.view?.type === "library") { STATE.offset = 0; STATE.tracks = []; loadMore(true); }
    else { sortClient(key); renderTracks(); }
  };
});
function sortClient(key) {
  const cmp = {
    title: (a, b) => (a.title || "").localeCompare(b.title || ""),
    artist: (a, b) => (a.artist || "").localeCompare(b.artist || "") || (a.album || "").localeCompare(b.album || ""),
    album: (a, b) => (a.album || "").localeCompare(b.album || ""),
    top_rated: (a, b) => (b.rating || 0) - (a.rating || 0),
    plays: (a, b) => (b.plays || 0) - (a.plays || 0),
  }[key];
  if (cmp) STATE.tracks.sort(cmp);
}
// infinite scroll
$("browse-body").onscroll = (e) => {
  const el = e.target;
  if (el.scrollTop + el.clientHeight > el.scrollHeight - 200) loadMore(false);
};
// search
let searchTimer;
$("search").oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    if (STATE.view?.type === "library") { STATE.search = $("search").value.trim(); STATE.offset = 0; STATE.tracks = []; loadMore(true); }
  }, 300);
};

// ---------------------------------------------------------------- context menu
const ctxMenu = document.createElement("div");
ctxMenu.className = "ctx hidden"; document.body.appendChild(ctxMenu);
function hideCtxMenu() { ctxMenu.classList.add("hidden"); }
document.addEventListener("click", hideCtxMenu);
document.addEventListener("scroll", hideCtxMenu, true);
window.addEventListener("blur", hideCtxMenu);

function showCtxMenu(e, idx) {
  e.preventDefault();
  const t = STATE.tracks[idx];
  if (!STATE.selected.has(t.rating_key)) selectRow(idx, false);
  const onDevice = String(t.rating_key).startsWith("dev:");
  const items = [
    { label: "▶  Play", fn: () => playIndex(idx) },
  ];
  if (!onDevice) items.push({ label: `📻  Start radio from “${t.title}”`, fn: () => songRadio(t) });
  items.push({ sep: true });
  if (t.artist) items.push({ label: `Show all by ${t.artist}`, fn: () => filterBy(t.artist) });
  if (t.album) items.push({ label: `Show album “${t.album}”`, fn: () => filterBy(t.album) });
  ctxMenu.innerHTML = items.map((it, i) => it.sep
    ? '<div class="ctx-sep"></div>'
    : `<div class="ctx-item" data-i="${i}">${esc(it.label)}</div>`).join("");
  ctxMenu.querySelectorAll(".ctx-item").forEach((el) => {
    el.onclick = (ev) => { ev.stopPropagation(); hideCtxMenu(); items[Number(el.dataset.i)].fn(); };
  });
  ctxMenu.classList.remove("hidden");
  // keep the menu on-screen
  const w = ctxMenu.offsetWidth, h = ctxMenu.offsetHeight;
  ctxMenu.style.left = `${Math.min(e.clientX, innerWidth - w - 6)}px`;
  ctxMenu.style.top = `${Math.min(e.clientY, innerHeight - h - 6)}px`;
}

// Filter the library down to a search term (artist or album) — iTunes-like
// "show me everything by this".
function filterBy(term) {
  selectPane("pane-browse", $("src-music"));
  STATE.view = { type: "library", title: term };
  $("browse-title").textContent = term;
  $("btn-del-pl").classList.add("hidden");
  $("search").value = term; STATE.search = term;
  STATE.offset = 0; STATE.total = 0; STATE.tracks = []; $("browse-body").innerHTML = "";
  loadMore(true);
}

// Build a sonically-similar "song radio" from one track (needs Plex).
async function songRadio(t) {
  lcd("Building song radio…", t.title, 0.3);
  try {
    const r = await api("/api/playlist/sonic", "POST",
      { rating_key: t.rating_key, title: `Radio: ${t.title}` });
    await loadSidebar();
    const el = document.querySelector(`.playlist[data-id="${CSS.escape(r.id)}"]`);
    if (el) openPlaylist(el);
    lcd("iAmped", `Created “${r.title}” — ${r.count} tracks${r.warning ? " · " + r.warning : ""}`);
  } catch (e) { lcd("iAmped", `Song radio failed: ${e.message}`); }
}

// ---------------------------------------------------------------- playback
const player = $("player");
function playIndex(i) {
  if (i < 0 || i >= STATE.tracks.length) return;
  STATE.playing = i;
  const t = STATE.tracks[i];
  player.src = `/api/stream/${encodeURIComponent(t.rating_key)}`;
  player.play().catch(() => {});
  lcd(t.title, `${t.artist} — ${t.album}`);
  $("lcd-prog").classList.add("seekable");
  scrub(0, 0, 0);
  setPlayIcon(true); renderTracks();
}
function togglePlay() {
  if (STATE.playing < 0) { if (STATE.tracks.length) playIndex(0); return; }
  if (player.paused) { player.play(); setPlayIcon(true); } else { player.pause(); setPlayIcon(false); }
}
function setPlayIcon(playing) {
  $("t-play").innerHTML = playing
    ? '<svg viewBox="0 0 16 16"><path d="M3 2h4v12H3zM9 2h4v12H9z"/></svg>'
    : '<svg viewBox="0 0 16 16"><path d="M3 2l11 6L3 14z"/></svg>';
}
player.ontimeupdate = () => { if (!scrubbing && player.duration) scrub(player.currentTime / player.duration, player.currentTime, player.duration); };
player.onended = () => playIndex(STATE.playing + 1);

// ---- scrubbing: the LCD progress bar doubles as an iTunes-style seek bar ----
const prog = $("lcd-prog");
let scrubbing = false;
function seekFromEvent(clientX) {
  if (!player.duration) return;
  const r = prog.getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
  scrub(frac, frac * player.duration, player.duration);   // move fill + times immediately
  player.currentTime = frac * player.duration;
}
prog.addEventListener("mousedown", (e) => {
  if (STATE.playing < 0 || !player.duration) return;
  scrubbing = true; seekFromEvent(e.clientX); e.preventDefault();
});
window.addEventListener("mousemove", (e) => { if (scrubbing) seekFromEvent(e.clientX); });
window.addEventListener("mouseup", () => { scrubbing = false; });

$("t-play").onclick = togglePlay;
$("t-next").onclick = () => playIndex(STATE.playing + 1);
$("t-prev").onclick = () => playIndex(STATE.playing - 1);

// ---------------------------------------------------------------- playlist actions
$("btn-play-sel").onclick = () => {
  const idx = STATE.selected.size ? STATE.tracks.findIndex((t) => STATE.selected.has(t.rating_key)) : 0;
  playIndex(idx < 0 ? 0 : idx);
};
$("btn-sonic").onclick = async () => {
  if (STATE.selected.size !== 1) { lcd("Sonic playlist", "Select exactly one seed track first"); return; }
  const rk = [...STATE.selected][0];
  const seed = STATE.tracks.find((t) => t.rating_key === rk);
  lcd("Building sonic playlist…", seed ? `Seed: ${seed.title}` : "");
  try {
    const r = await api("/api/playlist/sonic", "POST", { rating_key: rk });
    lcd("iAmped", `Created “${r.title}” (${r.count} tracks)${r.warning ? " — " + r.warning : ""}`);
    await loadSidebar();
    const el = $("playlist-list").querySelector(`[data-id="${CSS.escape(r.id)}"]`);
    if (el) openPlaylist(el);
  } catch (e) { lcd("Sonic playlist failed", e.message); alert("Sonic playlist: " + e.message); }
};
$("btn-newpl").onclick = async () => {
  if (!STATE.selected.size) { lcd("New playlist", "Select some tracks first"); return; }
  const name = prompt("New playlist name:", "My Playlist");
  if (!name) return;
  const r = await api("/api/playlist/local", "POST", { title: name, rating_keys: [...STATE.selected] });
  lcd("iAmped", `Created “${r.title}” (${r.count} tracks)`); await loadSidebar();
};
$("btn-add").onclick = () => {
  const menu = $("add-menu");
  if (!menu.classList.contains("hidden")) { menu.classList.add("hidden"); return; }
  if (!STATE.selected.size) { lcd("Add to playlist", "Select some tracks first"); return; }
  const locals = STATE.playlists.filter((p) => p.source === "local");
  menu.innerHTML = locals.map((p) => `<div class="mi" data-id="${p.id.split(":")[1]}">${esc(p.title)}</div>`).join("") +
    (locals.length ? '<div class="sep"></div>' : "") + '<div class="mi" data-new="1">New playlist…</div>';
  menu.classList.remove("hidden");
  menu.querySelectorAll(".mi").forEach((mi) => {
    mi.onclick = async () => {
      menu.classList.add("hidden");
      if (mi.dataset.new) { $("btn-newpl").onclick(); return; }
      await api(`/api/playlist/local/${mi.dataset.id}/add`, "POST", { rating_keys: [...STATE.selected] });
      lcd("iAmped", "Added to playlist"); await loadSidebar();
    };
  });
};
document.addEventListener("click", (e) => { if (!e.target.closest("#btn-add") && !e.target.closest("#add-menu")) $("add-menu").classList.add("hidden"); });
$("btn-del-pl").onclick = async () => {
  if (STATE.view?.type !== "playlist" || STATE.view.source !== "local") return;
  if (!confirm(`Delete playlist “${STATE.view.title}”? (Tracks stay in your library.)`)) return;
  await api(`/api/playlist/local/${STATE.view.id.split(":")[1]}`, "DELETE");
  await loadSidebar(); openLibrary();
};

// ---------------------------------------------------------------- radio & stations
let radioMode = "artist", radioStationsLoaded = false;
$("btn-radio").onclick = async () => {
  const panel = $("radio-panel");
  if (!panel.classList.contains("hidden")) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  if (!radioStationsLoaded) {
    try {
      const r = await api("/api/stations");
      $("rp-station").innerHTML = (r.stations || []).map((s) => `<option>${esc(s.title)}</option>`).join("")
        || '<option value="">No stations (needs Sonic Analysis)</option>';
      radioStationsLoaded = true;
    } catch (e) { /* leave empty; artist radio still works */ }
  }
};
document.querySelectorAll("#radio-panel .rp-tab").forEach((tab) => {
  tab.onclick = () => {
    radioMode = tab.dataset.mode;
    document.querySelectorAll("#radio-panel .rp-tab").forEach((t) => t.classList.toggle("active", t === tab));
    $("rp-artist-row").classList.toggle("hidden", radioMode !== "artist");
    $("rp-distance-row").classList.toggle("hidden", radioMode !== "artist");
    $("rp-station-row").classList.toggle("hidden", radioMode !== "station");
  };
});
$("rp-build").onclick = async () => {
  const body = {
    device_type: [...document.getElementsByName("dtype")].find((r) => r.checked)?.value || "ipod",
    transcode_lossless: $("transcode").checked,
  };
  const count = Number($("rp-count").value), mb = Number($("rp-mb").value);
  if (count) body.max_tracks = count;
  if (mb) body.max_mb = mb;
  if ($("rp-fit").checked) {
    const dp = $("device-path").value.trim();
    if (!dp) { $("rp-status").textContent = "Pick a device first"; return; }
    body.device_path = dp; body.fraction = 1.0;
  }
  let endpoint, label;
  if (radioMode === "artist") {
    const artist = $("rp-artist").value.trim();
    if (!artist) { $("rp-status").textContent = "Enter an artist"; return; }
    body.artist = artist; body.method = "sonic";
    body.max_distance = (Number($("rp-distance").value) || 25) / 100;
    endpoint = "/api/playlist/radio"; label = `${artist} radio`;
  } else {
    const station = $("rp-station").value;
    if (!station) { $("rp-status").textContent = "No station available"; return; }
    body.station = station; endpoint = "/api/playlist/station"; label = station;
  }
  $("rp-status").textContent = "Building…"; lcd(`Building ${label}…`, "");
  try {
    const r = await api(endpoint, "POST", body);
    $("rp-status").textContent = ""; $("radio-panel").classList.add("hidden");
    lcd("iAmped", `Created “${r.title}” — ${r.count} tracks, ${fmtBytes(r.total_bytes)}${r.warning ? " · " + r.warning : ""}`);
    await loadSidebar();
    const el = $("playlist-list").querySelector(`[data-id="${CSS.escape(r.id)}"]`);
    if (el) openPlaylist(el);
  } catch (e) { $("rp-status").textContent = e.message; lcd("Radio failed", e.message); }
};
document.addEventListener("click", (e) => {
  if (!e.target.closest("#btn-radio") && !e.target.closest("#radio-panel")) $("radio-panel").classList.add("hidden");
});

// ---------------------------------------------------------------- device
async function selectDevice(el) {
  selectPane("pane-device", el);
  const path = el.dataset.path, isIpod = el.dataset.ipod === "true";
  const model = el.dataset.model;
  $("device-path").value = path; $("dev-name-h").textContent = el.dataset.name;
  STATE.currentDevice = {
    path, type: isIpod ? "ipod" : "massstorage", name: el.dataset.name,
  };
  STATE.manualKeys = null; STATE.review = [];
  $("review-list").innerHTML = "";
  $("review-wrap").classList.add("hidden");
  $("review-actions").classList.add("hidden");
  $("dev-sub").textContent = (model ? `${model} · ` : "")
    + `${fmtBytes(el.dataset.free)} free of ${fmtBytes(el.dataset.total)} · ${path}`;
  for (const r of document.getElementsByName("dtype")) r.checked = r.value === (isIpod ? "ipod" : "massstorage");
  toggleDeviceType();
  renderCapacityRaw(Number(el.dataset.total), 0, 0, Number(el.dataset.free));
  lcd("iAmped", `${el.dataset.name} — ready`); $("btn-sync").disabled = true;
  try {
    const profile = await api(`/api/device/profile?device_path=${encodeURIComponent(path)}&device_type=${STATE.currentDevice.type}`);
    if (profile.reserve_mb != null) $("reserve").value = profile.reserve_mb;
    if (profile.fill_strategy) $("strategy").value = profile.fill_strategy;
    if (profile.transcode_lossless != null) $("transcode").checked = profile.transcode_lossless;
    if (profile.sync_artwork != null) $("sync-artwork").checked = profile.sync_artwork;
    if (profile.mirror != null) $("mirror").checked = profile.mirror;
    if (profile.name) $("device-name").value = profile.name;
  } catch (_) {}
  await loadBackups();
}
function toggleDeviceType() {
  const t = [...document.getElementsByName("dtype")].find((r) => r.checked).value;
  $("ipod-warn").classList.toggle("hidden", t !== "ipod");
  $("devname-wrap").classList.toggle("hidden", t !== "ipod");
}
for (const r of document.getElementsByName("dtype")) r.onchange = toggleDeviceType;
function deviceParams() {
  const t = [...document.getElementsByName("dtype")].find((r) => r.checked).value;
  const p = {
    device_path: $("device-path").value.trim(), device_type: t,
    device_name: $("device-name").value.trim() || "iPod",
    reserve_mb: Number($("reserve").value) || 0, fill_strategy: $("strategy").value,
    transcode_lossless: $("transcode").checked, playlist_ids: selectedPlaylists(),
    sync_artwork: $("sync-artwork").checked,
    mirror: $("mirror").checked,
  };
  if (STATE.manualKeys) p.mirror = false;
  if (STATE.manualKeys) p.rating_keys = STATE.manualKeys;
  if (STATE.review.length) {
    p.review_actions = [...document.querySelectorAll(".review-op:checked")]
      .map((el) => el.value);
  }
  return p;
}
function renderCapacityRaw(total, music, reserve, free) {
  const cap = total || (music + reserve + free) || 1;
  const pct = (n) => `${Math.max(0, (n / cap) * 100)}%`;
  $("capacity").innerHTML = `<i class="seg-music" style="width:${pct(music)}"></i><i class="seg-reserve" style="width:${pct(reserve)}"></i><i class="seg-free" style="width:${pct(free)}"></i>`;
  $("cap-legend").innerHTML = `<span><i class="seg-music"></i>Music ${fmtBytes(music)}</span>` +
    (reserve ? `<span><i class="seg-reserve"></i>Reserved ${fmtBytes(reserve)}</span>` : "") +
    `<span><i class="seg-free"></i>Free ${fmtBytes(free)}</span>`;
}
async function plan() {
  STATE.manualKeys = null;
  const s = $("sync-status"); s.className = "status"; const p = deviceParams();
  if (!p.device_path) { s.className = "status err"; s.textContent = "Select a device first."; return; }
  $("btn-plan").disabled = true; lcd("Planning", "Choosing tracks that fit…");
  try {
    const r = await api("/api/plan", "POST", p);
    renderPlan(r); $("btn-sync").disabled = (r.track_count + r.remove_count) === 0 && !r.pending_transaction;
    lcd("iAmped", `${r.add_count.toLocaleString()} additions, ${r.update_count} updates, ${r.remove_count} removals`);
  } catch (e) { s.className = "status err"; s.textContent = e.message; lcd("iAmped", "Planning failed"); }
  $("btn-plan").disabled = false;
}
function renderPlan(r) {
  STATE.review = r.review || [];
  renderCapacityRaw(r.capacity_bytes, r.total_bytes, r.reserve_bytes, Math.max(r.capacity_bytes - r.total_bytes - r.reserve_bytes, 0));
  const pls = r.playlists.map((p) => `<span class="pill">${esc(p.title)}: ${p.count}/${p.requested}</span>`).join("");
  $("plan-summary").innerHTML = `<b>${r.desired_track_count.toLocaleString()}</b> final tracks · <b>${fmtBytes(r.total_bytes)}</b>` +
    ` · <span class="muted">${r.keep_count} kept, ${r.add_count} added` +
    (r.update_count ? `, ${r.update_count} updated` : "") +
    (r.remove_count ? `, ${r.remove_count} removed (${fmtBytes(r.remove_bytes)})` : "") + `</span>` +
    (r.pending_transaction ? ` · <span class="muted">interrupted sync ready to resume</span>` : "") +
    (r.skipped_for_space ? ` · <span class="muted">${r.skipped_for_space} skipped (full)</span>` : "") + (pls ? `<div style="margin-top:6px;">${pls}</div>` : "");
  $("plan-meta").textContent = `Showing first ${r.preview.length} of ${r.desired_track_count} final tracks.`;
  $("preview").innerHTML = r.preview.map((t, i) => `<div class="songrow">
    <div class="c-num">${i + 1}</div><div class="c-name">${esc(t.title)}${t.lossless ? '<span class="lossless-tag">FLAC</span>' : ''}</div>
    <div class="c-artist">${esc(t.artist)}</div><div class="c-album">${esc(t.album)}</div>
    <div class="c-plays">${t.views || 0}</div><div class="c-time">${fmtBytes(t.size)}</div></div>`).join("");
  $("preview-wrap").classList.remove("hidden");
  $("review-list").innerHTML = STATE.review.map((item) => `<label class="songrow ${item.action}">
    <div class="c-check"><input class="review-op" type="checkbox" value="${esc(item.id)}" ${item.checked ? "checked" : ""}></div>
    <div class="c-action">${esc(item.action)}</div><div class="c-name">${esc(item.title)}</div>
    <div class="c-artist">${esc(item.artist)}</div><div class="c-album">${esc(item.album)}</div>
    <div class="c-size">${fmtBytes(item.size)}</div></label>`).join("");
  const hasReview = STATE.review.length > 0;
  $("review-wrap").classList.toggle("hidden", !hasReview);
  $("review-actions").classList.toggle("hidden", !hasReview);
  $("review-all").checked = true;
  document.querySelectorAll(".review-op").forEach((box) => {
    box.onchange = () => {
      $("review-all").checked = [...document.querySelectorAll(".review-op")]
        .every((el) => el.checked);
      $("btn-sync").disabled = !document.querySelector(".review-op:checked")
        && !r.pending_transaction;
    };
  });
}

async function dropSelectionOnDevice(el) {
  const keys = [...STATE.selected];
  if (!keys.length) return;
  await selectDevice(el);
  STATE.manualKeys = keys;
  const s = $("sync-status");
  s.className = "status"; s.textContent = `Planning ${keys.length} dragged track(s)…`;
  lcd("Planning drag to device", `${keys.length} selected track(s)`);
  try {
    const r = await api("/api/plan", "POST", deviceParams());
    renderPlan(r);
    $("btn-sync").disabled = (r.track_count + r.remove_count) === 0;
    lcd("iAmped", `${r.add_count} additions, ${r.update_count} updates`);
  } catch (e) {
    s.className = "status err"; s.textContent = e.message;
  }
}

$("review-all").onchange = () => {
  document.querySelectorAll(".review-op").forEach(
    (box) => { box.checked = $("review-all").checked; });
  $("btn-sync").disabled = !$("review-all").checked;
};

async function saveDeviceProfile() {
  const p = deviceParams();
  if (!p.device_path) return;
  p.name = $("device-name").value.trim() || STATE.currentDevice?.name || "Device";
  await api("/api/device/profile", "PUT", p);
  lcd("iAmped", "Device profile saved");
}

async function loadBackups() {
  const p = deviceParams();
  if (!p.device_path) return;
  try {
    const r = await api(`/api/device/backups?device_path=${encodeURIComponent(p.device_path)}&device_type=${p.device_type}`);
    $("backup-select").innerHTML = (r.backups || []).map((b) =>
      `<option value="${esc(b.id)}">${esc(b.id)} · ${b.managed_tracks} tracks</option>`
    ).join("") || '<option value="">No snapshots yet</option>';
  } catch (_) {}
}

async function restoreBackup() {
  const p = deviceParams(), id = $("backup-select").value;
  if (!id || !confirm(`Restore ${id} to ${p.device_path}? Current iAmped changes after that snapshot will be removed.`)) return;
  const { job } = await api("/api/device/restore", "POST", {
    device_path: p.device_path, device_type: p.device_type, backup_id: id });
  pollJob(job, {
    onProgress: (j) => lcd("Restoring device", j.message || j.phase, .5),
    onDone: (j) => { lcd("iAmped", j.message); plan(); },
    onError: (e) => lcd("Restore failed", e),
  });
}

async function ejectDevice(el = null) {
  if (el) await selectDevice(el);
  const p = deviceParams();
  if (!p.device_path) return;
  try {
    await api("/api/device/eject", "POST", p);
    lcd("iAmped", `${p.device_path} ejected safely`);
    await loadDevices();
  } catch (e) { lcd("Eject failed", e.message); }
}

async function matchDeviceFiles() {
  const p = deviceParams();
  $("match-status").textContent = "Matching…";
  try {
    const r = await api(`/api/device/matches?device_path=${encodeURIComponent(p.device_path)}&device_type=${p.device_type}`);
    const confident = r.matches.filter((x) => x.match?.confidence >= .88).length;
    $("match-status").textContent = `${confident}/${r.count} confident matches` +
      (r.chromaprint_available ? " · Chromaprint available" : " · metadata mode");
  } catch (e) { $("match-status").textContent = e.message; }
}
async function sync() {
  const s = $("sync-status"); s.className = "status"; const p = deviceParams();
  if (!confirm(`${p.mirror ? "Mirror" : "Add"} this selection to ${p.device_path} as a ${p.device_type === "ipod" ? "classic iPod (AAC)" : "USB player (MP3)"}?`)) return;
  $("btn-sync").disabled = true; $("btn-plan").disabled = true; s.textContent = "Starting…";
  showSyncProgress(true); setSyncProgress("Starting…", 0, 0);
  try {
    const { job } = await api("/api/sync", "POST", p);
    pollJob(job, {
      onProgress: (j) => {
        const label = { syncing: "Copying", planning: "Planning", playlists: "Playlists", finalizing: "Finalizing" }[j.phase] || j.phase;
        s.textContent = `${label}: ${j.message || ""}`;
        setSyncProgress(j.phase === "syncing" && j.message ? j.message : `${label}…`, j.done || 0, j.total || 0);
        lcd(`${label}…`, j.message || "", j.total ? j.done / j.total : 0.1);
      },
      onDone: (j) => {
        s.className = "status ok";
        const r = j.result;
        const total = r.managed_tracks_total ?? r.tracks_total ?? r.tracks ?? r.tracks_added ?? 0;
        s.textContent = `Done — ${total} managed tracks, ${r.tracks_added || 0} added, ${r.tracks_updated || 0} updated, ${r.tracks_removed || 0} removed, ${r.playlists} playlists, ${fmtBytes(r.bytes)}.` + (p.device_type === "ipod" ? " Eject safely before unplugging." : "");
        lcd("iAmped", `Synced ${r.tracks_added ?? total} tracks`);
        showSyncProgress(false); $("btn-sync").disabled = false; $("btn-plan").disabled = false;
      },
      onError: (e) => { s.className = "status err"; s.textContent = e; lcd("iAmped", "Sync failed"); showSyncProgress(false); $("btn-sync").disabled = false; $("btn-plan").disabled = false; },
    });
  } catch (e) { s.className = "status err"; s.textContent = e.message; $("btn-sync").disabled = false; $("btn-plan").disabled = false; }
}

// ---------------------------------------------------------------- readback (device → Plex)
async function readbackPreview() {
  const p = deviceParams(); const s = $("readback-status"); s.className = "status";
  if (!p.device_path) { s.className = "status err"; s.textContent = "Select a device first."; return; }
  $("btn-readback").disabled = true; s.textContent = "Reading device…"; lcd("Reading device", "Plays & ratings");
  try {
    const r = await api("/api/device/readback", "POST", {
      device_path: p.device_path, device_type: p.device_type,
      want_plays: $("rb-plays").checked, want_ratings: $("rb-ratings").checked });
    STATE.readbackPlan = r;
    $("readback-summary").innerHTML = `<b>${r.total_plays}</b> plays · <b>${r.total_rating_changes}</b> rating fills`;
    $("readback-list").innerHTML = r.plan.map((it) => `<div class="songrow">
      <div class="c-num"></div><div class="c-name">${esc(it.title)}</div>
      <div class="c-artist">${esc(it.artist)}</div>
      <div class="c-plays">${it.plays_delta ? "+" + it.plays_delta : ""}</div>
      <div class="c-rating">${it.new_rating != null ? stars(it.new_rating) : ""}</div></div>`).join("");
    $("readback-wrap").classList.toggle("hidden", r.plan.length === 0);
    $("btn-readback-apply").classList.toggle("hidden", r.plan.length === 0);
    s.className = "status ok";
    const d = r.diagnostics || {};
    s.textContent = `${r.plan.length} tracks with changes from ${r.source}.`
      + (d.foreign_tracks_ignored ? ` ${d.foreign_tracks_ignored} foreign tracks safely ignored.` : "")
      + (d.unmatched ? ` ${d.unmatched} log entries unmatched.` : "")
      + (r.notes.length ? ` ${r.notes.length} malformed entries.` : "");
    lcd("iAmped", `${r.total_plays} plays, ${r.total_rating_changes} ratings to import`);
  } catch (e) { s.className = "status err"; s.textContent = e.message; lcd("iAmped", "Readback failed"); }
  $("btn-readback").disabled = false;
}
async function applyReadback() {
  const r = STATE.readbackPlan; if (!r || !r.plan.length) return;
  const p = deviceParams(); const s = $("readback-status");
  if (!confirm(`Write ${r.total_plays} plays and ${r.total_rating_changes} ratings to Plex` + ($("rb-reset").checked ? ", then reset the device" : "") + "?")) return;
  $("btn-readback-apply").disabled = true; $("btn-readback").disabled = true;
  try {
    const { job } = await api("/api/device/readback/apply", "POST", {
      device_path: p.device_path, device_type: p.device_type,
      plan: r.plan, reset: $("rb-reset").checked });
    pollJob(job, {
      onProgress: (j) => { s.className = "status"; s.textContent = j.message || "Applying…"; lcd("Writing to Plex…", j.message || "", j.total ? j.done / j.total : 0.1); },
      onDone: (j) => { s.className = "status ok"; s.textContent = j.message; lcd("iAmped", j.message);
        $("btn-readback-apply").classList.add("hidden"); $("readback-wrap").classList.add("hidden");
        $("btn-readback").disabled = false; STATE.readbackPlan = null; refreshStats(); },
      onError: (e) => { s.className = "status err"; s.textContent = e; lcd("iAmped", "Apply failed"); $("btn-readback-apply").disabled = false; $("btn-readback").disabled = false; },
    });
  } catch (e) { s.className = "status err"; s.textContent = e.message; $("btn-readback-apply").disabled = false; $("btn-readback").disabled = false; }
}

// ---------------------------------------------------------------- wire up
$("btn-connect").onclick = connect;
$("btn-plex-oauth").onclick = startPlexOAuth;
$("btn-oauth-server").onclick = finishPlexOAuth;
$("btn-readback").onclick = readbackPreview;
$("btn-readback-apply").onclick = applyReadback;
$("btn-build").onclick = buildLibrary;
$("btn-plan").onclick = plan;
$("btn-sync").onclick = sync;
$("btn-save-profile").onclick = saveDeviceProfile;
$("btn-restore").onclick = restoreBackup;
$("btn-eject").onclick = () => ejectDevice();
$("btn-match").onclick = matchDeviceFiles;
$("src-music").onclick = openLibrary;

// ---------------------------------------------------------------- visualizer
function resizeVisualizer() {
  const canvas = $("visualizer-canvas");
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width * devicePixelRatio));
  canvas.height = Math.max(1, Math.round(rect.height * devicePixelRatio));
}
function visualizerAnimation(wave) {
  const common = { lineColor: "#78b7ff", fillColor: "#4f8fe0" };
  const style = $("visualizer-style").value;
  if (style === "shine") return new wave.animations.Shine({ lineColor: "#b9dcff" });
  if (style === "dualbars") return new wave.animations.Cubes({ ...common, count: 32 });
  if (style === "wave") return new wave.animations.Wave({ lineColor: "#80c4ff", lineWidth: 3 });
  return new wave.animations.Lines({ ...common, count: 64, lineWidth: 3 });
}
function enableVisualizer() {
  if (!window.Wave) { lcd("Visualizer unavailable", "Wave.js did not load"); return; }
  resizeVisualizer();
  if (!STATE.visualizer) STATE.visualizer = new Wave(player, $("visualizer-canvas"));
  STATE.visualizer.clearAnimations();
  STATE.visualizer.addAnimation(visualizerAnimation(STATE.visualizer));
  STATE.visualizerEnabled = true;
  $("visualizer-empty").classList.add("hidden");
  $("btn-visualizer").textContent = "Enabled";
  if (player.paused && STATE.playing >= 0) player.play().catch(() => {});
}
$("btn-visualizer").onclick = enableVisualizer;
$("visualizer-style").onchange = () => { if (STATE.visualizerEnabled) enableVisualizer(); };
window.addEventListener("resize", resizeVisualizer);

loadConfig();
loadDevices();
