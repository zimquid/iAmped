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
const BITRATE_PRESETS = {
  aac: [64, 96, 128, 160, 192, 256],
  mp3: [96, 128, 160, 192, 256, 320],
};

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
  playbackOffset: 0,
  bitrateByFormat: { aac: 256, mp3: 320 },
  manualPlaylistIds: null,
  transferMaxTracks: null,
  transferTitle: "",
  pendingTransferPlan: null,
  draggedPlaylist: null,
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
  $("lcd-prog").firstElementChild.style.width = `${Math.min(100, Math.max(0, (frac || 0) * 100))}%`;
  $("lcd-elapsed").textContent = fmtClock(elapsed || 0);
  $("lcd-remain").textContent = "-" + fmtClock(dur ? Math.max(0, dur - (elapsed || 0)) : 0);
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

// Top-of-pane progress bar for video sync — fills smoothly as each title encodes.
function showVideoProgress(on) { $("video-progress").classList.toggle("hidden", !on); }
function setVideoProgress(label, frac) {
  $("vp-label").textContent = label;
  $("vp-pct").textContent = `${Math.round(Math.min(1, Math.max(0, frac || 0)) * 100)}%`;
  $("vp-fill").style.width = `${Math.min(100, Math.max(0, (frac || 0) * 100))}%`;
}

// ---- panes --------------------------------------------------------------
function selectPane(paneId, srcEl) {
  document.querySelectorAll(".content > .pane").forEach((p) => p.classList.toggle("active", p.id === paneId));
  document.querySelectorAll(".src-item").forEach((s) => s.classList.remove("selected"));
  if (srcEl) srcEl.classList.add("selected");
  if (paneId === "pane-browse") bottomBrowse();
  else { $("bottom-info").classList.remove("hidden"); $("capacity").classList.add("hidden"); $("cap-legend").classList.add("hidden"); $("sync-actions").classList.add("hidden"); $("bottom-info").textContent = "—"; }
  if (paneId === "pane-video") openVideo();
}
document.querySelectorAll(".src-item[data-pane]").forEach((el) => { el.onclick = () => selectPane(el.dataset.pane, el); });

// ---------------------------------------------------------------- config / connect
async function loadConfig() {
  const cfg = await api("/api/config");
  $("baseurl").value = cfg.plex_baseurl || ""; $("token").value = cfg.plex_token || "";
  $("device-path").value = cfg.last_device_path || ""; $("reserve").value = cfg.reserve_mb ?? 200;
  $("strategy").value = cfg.fill_strategy || "most_played";
  $("transcode").checked = cfg.transcode_lossless !== false;
  STATE.bitrateByFormat.aac = Number(cfg.aac_bitrate_k) || 256;
  STATE.bitrateByFormat.mp3 = Number(cfg.mp3_bitrate_k) || 320;
  $("mirror").checked = cfg.mirror !== false;
  if ($("ingest-dir")) $("ingest-dir").value = cfg.ingest_dir || "";
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
  if (STATE.playing < 0) lcd("iAmped", `Connected to ${res.server.name}`);
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
const ejectSvg = '<svg viewBox="0 0 16 16"><path d="M8 3l5 6H3zM3 11h10v2H3z"/></svg>';
const filmSvg = '<svg viewBox="0 0 16 16"><rect x="1.5" y="3" width="13" height="10" rx="1.5" fill="none" stroke="#5f6b7e"/><path d="M6.5 6l4 2-4 2z"/></svg>';
const plSvg = '<svg viewBox="0 0 16 16"><path d="M1 3h10v1.6H1zM1 6h10v1.6H1zM1 9h6v1.6H1zM12 7l3 2-3 2z"/></svg>';
const sonicSvg = '<svg viewBox="0 0 16 16"><path d="M1 8h2l1-4 2 8 2-10 2 12 2-6h2"/><path d="M1 8h2l1-4 2 8 2-10 2 12 2-6h2" fill="none" stroke="#5f6b7e" stroke-width="1.1"/></svg>';

function deviceText(d) {
  return [d.ipod_model, d.ipod_generation, d.model, d.name, d.transport]
    .filter(Boolean).join(" ").toLowerCase();
}
function isTruthy(v) { return v === true || v === "true"; }
function deviceIconKind(d) {
  const s = deviceText(d);
  if (isTruthy(d.is_ipod)) {
    if (s.includes("touch")) return "ipod-touch";
    if (s.includes("shuffle")) {
      if (s.includes("1st")) return "shuffle-1";
      if (s.includes("3rd")) return "shuffle-3";
      if (s.includes("4th")) return "shuffle-4";
      return "shuffle-2";
    }
    if (s.includes("nano")) {
      if (s.includes("1st")) return "nano-1";
      if (s.includes("2nd")) return "nano-2";
      if (s.includes("3rd")) return "nano-3";
      if (s.includes("4th")) return "nano-4";
      if (s.includes("5th")) return "nano-5";
      if (s.includes("6th")) return "nano-6";
      if (s.includes("7th")) return "nano-7";
      return "nano-4";
    }
    if (s.includes("mini")) return "mini";
    if (s.includes("classic")) return "classic";
    if (s.includes("photo") || s.includes("colour") || s.includes("color")) return "photo";
    if (s.includes("5.5") || s.includes("5th") || s.includes("video")) return "video";
    if (s.includes("4th")) return "ipod-4";
    if (s.includes("3rd")) return "ipod-3";
    if (s.includes("2nd")) return "ipod-2";
    if (s.includes("1st")) return "ipod-1";
    return "classic";
  }
  if (s.includes("creative") || s.includes("zen") || s.includes("muvo") || s.includes("nomad")) {
    if (s.includes("vision")) return "creative-vision";
    if (s.includes("micro")) return "creative-micro";
    if (s.includes("stone")) return "creative-stone";
    if (s.includes("x-fi") || s.includes("xfi")) return "creative-xfi";
    if (s.includes("muvo")) return "creative-muvo";
    if (s.includes("nomad") || s.includes("jukebox")) return "creative-nomad";
    return "creative-zen";
  }
  return "usb";
}
function iconSvg(body, className = "") {
  return `<svg class="device-icon ${className}" viewBox="0 0 64 88" aria-hidden="true">${body}</svg>`;
}
function bodyBox(aspect, maxW = 56, maxH = 82) {
  let h = maxH, w = h * aspect;
  if (w > maxW) { w = maxW; h = w / aspect; }
  return { x: 32 - w / 2, y: 44 - h / 2, w, h };
}
const rr = (n) => Number(n.toFixed(2));
function rectAttrs(b) {
  return `x="${rr(b.x)}" y="${rr(b.y)}" width="${rr(b.w)}" height="${rr(b.h)}"`;
}
function screenRect(b, left, top, width, height) {
  return {
    x: b.x + b.w * left, y: b.y + b.h * top,
    w: b.w * width, h: b.h * height,
  };
}
function wheelPod({
  aspect = .60, body = "#f3f3ee", screen = "#b7c6bf", wheel = .24,
  screenTop = .08, screenW = .58, screenH = .25, controls = "", center,
  radius = 5, maxW = 56, maxH = 82,
} = {}) {
  const b = bodyBox(aspect, maxW, maxH);
  const s = screenRect(b, (1 - screenW) / 2, screenTop, screenW, screenH);
  const wr = Math.min(b.w, b.h) * wheel;
  const cx = 32, cy = b.y + b.h * .72;
  return iconSvg(`<rect ${rectAttrs(b)} rx="${radius}" fill="${body}" stroke="#5f656b"/>
    <rect ${rectAttrs(s)} rx="2" fill="${screen}" stroke="#677681"/>
    ${controls}<circle cx="${cx}" cy="${rr(cy)}" r="${rr(wr)}" fill="#f7f7f3" stroke="#9c9c9c"/>
    <circle cx="${cx}" cy="${rr(cy)}" r="${rr(wr * .32)}" fill="${center || body}" stroke="#b6b6b2"/>`);
}
function deviceIcon(d, size = "sidebar") {
  const kind = deviceIconKind(d);
  const scaleClass = size === "inspector" ? "device-icon-large" : "device-icon-small";
  const color = {
    "nano-2": "#8db8d9", "nano-4": "#b25c84", "nano-5": "#d68535",
    "nano-7": "#6aa7c8", "mini": "#a8c784", "shuffle-2": "#83aecd",
    "shuffle-4": "#d7848d", "creative-micro": "#55afa5",
  }[kind];
  const wrap = (svg) => svg.replace("device-icon ", `device-icon ${scaleClass} `);
  if (kind === "ipod-1" || kind === "ipod-2") {
    const buttons = '<circle cx="20.8" cy="45" r="1.9" fill="#929292"/><circle cx="43.2" cy="45" r="1.9" fill="#929292"/><circle cx="32" cy="34.7" r="1.9" fill="#929292"/><circle cx="32" cy="55.3" r="1.9" fill="#929292"/>';
    return wrap(wheelPod({ aspect: .61, screenH: .20, wheel: .30, controls: buttons }));
  }
  if (kind === "ipod-3") {
    return wrap(wheelPod({ aspect: .60, screenH: .21, wheel: .27,
      controls: '<rect x="19" y="35" width="26" height="4.6" rx="2.3" fill="#d7d7d2"/><circle cx="22.5" cy="37.3" r="1.3" fill="#888"/><circle cx="29" cy="37.3" r="1.3" fill="#888"/><circle cx="35.5" cy="37.3" r="1.3" fill="#888"/><circle cx="42" cy="37.3" r="1.3" fill="#888"/>' }));
  }
  if (kind === "ipod-4" || kind === "photo") {
    return wrap(wheelPod({ aspect: .60, screen: kind === "photo" ? "#98c6de" : "#b8c4bb", wheel: .26 }));
  }
  if (kind === "video" || kind === "classic") {
    const classic = kind === "classic";
    return wrap(wheelPod({ aspect: .60, body: classic ? "#cfd3d5" : "#f3f3ee",
      screen: "#20262d", screenW: .64, screenH: .31, wheel: .25,
      center: classic ? "#cfd3d5" : "#f3f3ee" }));
  }
  if (kind === "mini") {
    return wrap(wheelPod({ aspect: .56, body: color, screen: "#bdcbb9", screenW: .58,
      screenH: .21, wheel: .27, center: color, radius: 6 }));
  }
  if (kind === "nano-1") {
    return wrap(wheelPod({ aspect: .46, body: "#f5f5ef", screen: "#bad0df",
      screenW: .68, screenH: .23, wheel: .28, radius: 4 }));
  }
  if (kind === "nano-2") {
    return wrap(wheelPod({ aspect: .46, body: color, screen: "#bed5df",
      screenW: .68, screenH: .24, wheel: .28, center: color, radius: 4 }));
  }
  if (kind === "nano-3") {
    return wrap(wheelPod({ aspect: .74, body: "#91abc4", screen: "#1f252b",
      screenTop: .11, screenW: .74, screenH: .35, wheel: .20, center: "#91abc4", radius: 6 }));
  }
  if (kind === "nano-4" || kind === "nano-5") {
    return wrap(wheelPod({ aspect: .42, body: color, screen: "#1f252b",
      screenW: .74, screenH: kind === "nano-5" ? .36 : .31, wheel: .31,
      center: color, radius: 8, controls: kind === "nano-5" ? '<circle cx="39.5" cy="74" r="1.6" fill="#34383e"/>' : "" }));
  }
  if (kind === "nano-6") {
    const b = bodyBox(1.04, 50, 48);
    const s = screenRect(b, .12, .12, .76, .68);
    return wrap(iconSvg(`<rect ${rectAttrs(b)} rx="7" fill="#434a52" stroke="#242a30"/>
      <rect ${rectAttrs(s)} rx="2" fill="#9cc5dd"/><rect x="${rr(b.x + b.w * .12)}" y="${rr(b.y + b.h + 3)}" width="${rr(b.w * .76)}" height="5" rx="2.5" fill="#a6acb2" stroke="#646a70"/>`));
  }
  if (kind === "nano-7") {
    const b = bodyBox(.52, 46, 82);
    const s = screenRect(b, .12, .08, .76, .73);
    return wrap(iconSvg(`<rect ${rectAttrs(b)} rx="5" fill="${color}" stroke="#53646d"/>
      <rect ${rectAttrs(s)} rx="2" fill="#1e242b"/><circle cx="32" cy="${rr(b.y + b.h * .91)}" r="3" fill="#e8edf0"/>`));
  }
  if (kind === "shuffle-1") {
    const b = bodyBox(.30, 26, 82);
    return wrap(iconSvg(`<rect ${rectAttrs(b)} rx="3" fill="#f5f5ef" stroke="#777"/>
      <circle cx="32" cy="${rr(b.y + b.h * .42)}" r="${rr(b.w * .47)}" fill="#ececea" stroke="#a0a0a0"/>
      <circle cx="32" cy="${rr(b.y + b.h * .42)}" r="3" fill="#fff"/><rect x="${rr(b.x + b.w * .2)}" y="${rr(b.y + b.h * .82)}" width="${rr(b.w * .6)}" height="7" rx="2" fill="#d8d8d8"/>`));
  }
  if (kind === "shuffle-3") {
    const b = bodyBox(.25, 20, 76);
    return wrap(iconSvg(`<rect ${rectAttrs(b)} rx="5" fill="#9aa1a8" stroke="#555b61"/>
      <rect x="${rr(b.x + b.w * .27)}" y="${rr(b.y + b.h * .12)}" width="${rr(b.w * .46)}" height="${rr(b.h * .66)}" rx="3" fill="#c9ced2"/>
      <circle cx="32" cy="${rr(b.y + b.h * .88)}" r="2" fill="#575e65"/>`));
  }
  if (kind === "shuffle-2" || kind === "shuffle-4") {
    const b = bodyBox(.92, 42, 46);
    return wrap(iconSvg(`<rect ${rectAttrs(b)} rx="6" fill="${color || "#c8ccd0"}" stroke="#687078"/>
      <circle cx="32" cy="${rr(b.y + b.h * .48)}" r="${rr(Math.min(b.w, b.h) * .32)}" fill="#edf0f1" stroke="#9da4a8"/>
      <circle cx="32" cy="${rr(b.y + b.h * .48)}" r="3" fill="${color || "#c8ccd0"}"/><rect x="${rr(b.x + b.w * .14)}" y="${rr(b.y + b.h + 3)}" width="${rr(b.w * .72)}" height="5" rx="2.5" fill="#aab0b6"/>`));
  }
  if (kind === "ipod-touch") {
    const b = bodyBox(.55, 44, 82);
    const s = screenRect(b, .08, .09, .84, .75);
    return wrap(iconSvg(`<rect ${rectAttrs(b)} rx="7" fill="#16191d" stroke="#444"/>
      <rect ${rectAttrs(s)} rx="2" fill="#202a35"/><path d="M${rr(s.x + 2)} ${rr(s.y + s.h * .72)}h${rr(s.w - 4)}v${rr(s.h * .23)}h${rr(4 - s.w)}z" fill="#476a86"/>
      <circle cx="32" cy="${rr(b.y + b.h * .93)}" r="3" fill="#0b0d10" stroke="#777"/>`));
  }
  if (kind === "creative-muvo") {
    return wrap(iconSvg(`<rect x="6" y="34" width="38" height="17" rx="4" fill="#2e3338" stroke="#111"/>
      <rect x="44" y="35.5" width="14" height="14" rx="2" fill="#b9bdc0" stroke="#6d7378"/><rect x="12" y="38" width="15" height="6" rx="1" fill="#8fb0b8"/><circle cx="35" cy="42.5" r="4.2" fill="#d5d8da"/>`));
  }
  if (kind === "creative-stone") {
    return wrap(iconSvg(`<rect x="8" y="30" width="48" height="28" rx="14" fill="#2f3439" stroke="#111"/>
      <circle cx="32" cy="44" r="10.5" fill="#d9dcde" stroke="#8b9298"/><circle cx="32" cy="44" r="3" fill="#2f3439"/>`));
  }
  if (kind === "creative-micro") {
    const b = bodyBox(.61, 48, 80);
    return wrap(iconSvg(`<rect ${rectAttrs(b)} rx="7" fill="${color}" stroke="#335d59"/>
      <rect x="${rr(b.x + b.w * .18)}" y="${rr(b.y + b.h * .12)}" width="${rr(b.w * .64)}" height="${rr(b.h * .28)}" rx="2" fill="#c4d8ce"/>
      <rect x="${rr(b.x + b.w * .38)}" y="${rr(b.y + b.h * .5)}" width="${rr(b.w * .24)}" height="${rr(b.h * .28)}" rx="5" fill="#e5f0ed"/><circle cx="32" cy="${rr(b.y + b.h * .88)}" r="2" fill="#e5f0ed"/>`));
  }
  if (kind === "creative-vision") {
    const b = bodyBox(.596, 52, 82);
    const s = screenRect(b, .14, .12, .66, .36);
    return wrap(iconSvg(`<rect ${rectAttrs(b)} rx="5" fill="#23272d" stroke="#0d0f12"/>
      <rect ${rectAttrs(s)} rx="2" fill="#5d83a1"/>
      <rect x="${rr(b.x + b.w * .84)}" y="${rr(b.y + b.h * .18)}" width="${rr(b.w * .08)}" height="${rr(b.h * .42)}" rx="2" fill="#d6d8d9"/>
      <circle cx="${rr(b.x + b.w * .38)}" cy="${rr(b.y + b.h * .78)}" r="${rr(b.w * .16)}" fill="#d6d8d9"/>
      <circle cx="${rr(b.x + b.w * .69)}" cy="${rr(b.y + b.h * .73)}" r="2" fill="#74a5d3"/><circle cx="${rr(b.x + b.w * .76)}" cy="${rr(b.y + b.h * .67)}" r="2" fill="#74a5d3"/><circle cx="${rr(b.x + b.w * .78)}" cy="${rr(b.y + b.h * .79)}" r="2" fill="#74a5d3"/>`));
  }
  if (kind === "creative-nomad") {
    const b = bodyBox(.95, 56, 60);
    return wrap(iconSvg(`<rect ${rectAttrs(b)} rx="8" fill="#b8bab8" stroke="#606468"/>
      <rect x="${rr(b.x + b.w * .14)}" y="${rr(b.y + b.h * .17)}" width="${rr(b.w * .72)}" height="${rr(b.h * .25)}" rx="2" fill="#9ba899"/>
      <circle cx="${rr(b.x + b.w * .31)}" cy="${rr(b.y + b.h * .72)}" r="${rr(b.h * .12)}" fill="#34383c"/><circle cx="${rr(b.x + b.w * .69)}" cy="${rr(b.y + b.h * .72)}" r="${rr(b.h * .12)}" fill="#34383c"/>`));
  }
  if (kind === "creative-zen" || kind === "creative-xfi") {
    const b = bodyBox(.69, 52, 76);
    const s = screenRect(b, .12, .11, .76, .48);
    return wrap(iconSvg(`<rect ${rectAttrs(b)} rx="5" fill="#1d2228" stroke="#0b0d10"/>
      <rect ${rectAttrs(s)} rx="2" fill="#5b86a8"/><rect x="${rr(b.x + b.w * .18)}" y="${rr(b.y + b.h * .72)}" width="${rr(b.w * .39)}" height="${rr(b.h * .14)}" rx="2" fill="#d6d9dc"/><circle cx="${rr(b.x + b.w * .76)}" cy="${rr(b.y + b.h * .79)}" r="${rr(b.w * .12)}" fill="#d6d9dc"/>`));
  }
  return wrap(iconSvg(`<rect x="8" y="31" width="48" height="25" rx="4" fill="#d5d8dc" stroke="#6f7780"/>
    <rect x="15" y="37" width="22" height="8" rx="2" fill="#8fb0be"/><circle cx="47" cy="43.5" r="5" fill="#f1f1ee" stroke="#8d9398"/>`));
}

async function loadSidebar() {
  STATE.playlists = await api("/api/playlists");
  const list = $("playlist-list");
  if (!STATE.playlists.length) { list.innerHTML = '<div class="src-item disabled">No playlists yet</div>'; }
  else {
    list.innerHTML = STATE.playlists.map((p) => `
      <div class="src-item playlist" data-id="${esc(p.id)}" data-title="${esc(p.title)}" data-source="${p.source}">
        ${p.source === "local" && p.kind === "sonic" ? sonicSvg : plSvg}${esc(p.title)}</div>`).join("");
    list.querySelectorAll(".playlist").forEach((el) => {
      el.onclick = () => openPlaylist(el);
      el.draggable = true;
      el.ondragstart = (event) => {
        STATE.draggedPlaylist = {
          id: el.dataset.id, title: el.dataset.title,
        };
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData(
          "application/x-iamped-playlist", JSON.stringify(STATE.draggedPlaylist));
        event.dataTransfer.setData("text/plain", `iAmped playlist: ${el.dataset.title}`);
        el.classList.add("dragging");
      };
      el.ondragend = () => {
        el.classList.remove("dragging");
        STATE.draggedPlaylist = null;
      };
    });
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
    <div class="src-item device" data-path="${esc(v.path)}" data-ipod="${v.is_ipod}" data-free="${v.free}" data-total="${v.total}" data-name="${esc(v.name)}" data-ipod-model="${esc(v.ipod_model || "")}" data-generation="${esc(v.ipod_generation || "")}" data-device-model="${esc(v.model || "")}" data-busloc="${esc(v.mtp_busloc || "")}" data-transport="${esc(v.transport || "")}"${(v.ipod_model || v.model) ? ` title="${esc(v.ipod_model || v.model)}"` : ""}>
      ${deviceIcon(v)}
      ${esc(v.name)}<span class="eject">${ejectSvg}</span></div>
    <div class="src-subitem device-music" data-path="${esc(v.path)}" data-ipod="${v.is_ipod}" data-name="${esc(v.name)}">
      ${noteSvg}Music on device</div>
    <div class="src-subitem device-videos" data-path="${esc(v.path)}" data-ipod="${v.is_ipod}" data-name="${esc(v.name)}">
      ${filmSvg}Videos on device</div>`).join("");
  list.querySelectorAll(".device").forEach((el) => {
    el.onclick = (event) => {
      if (event.target.closest(".eject")) { ejectDevice(el); return; }
      selectDevice(el);
    };
    el.ondragover = (event) => {
      const types = [...(event.dataTransfer?.types || [])];
      if (!STATE.selected.size && !STATE.draggedPlaylist
          && !types.includes("application/x-iamped-playlist")) return;
      event.preventDefault(); el.classList.add("drop-target");
    };
    el.ondragleave = () => el.classList.remove("drop-target");
    el.ondrop = (event) => {
      event.preventDefault(); el.classList.remove("drop-target");
      const rawPlaylist = event.dataTransfer.getData("application/x-iamped-playlist");
      const playlist = rawPlaylist ? JSON.parse(rawPlaylist) : STATE.draggedPlaylist;
      if (playlist) {
        dropPlaylistOnDevice(el, playlist);
      } else {
        dropSelectionOnDevice(el);
      }
    };
  });
  list.querySelectorAll(".device-music").forEach((el) => { el.onclick = () => openDeviceMusic(el); });
  list.querySelectorAll(".device-videos").forEach((el) => { el.onclick = () => openDeviceVideos(el); });
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
    STATE.deviceMusic = { path, type, name: el.dataset.name };
    STATE.tracks = (inv.tracks || []).filter((t) => t.media !== "video").map((t, i) => ({
      rating_key: `dev:${i}`,            // stable selection id (device tracks aren't all in Plex)
      title: t.title, artist: t.artist, album: t.album,
      duration_ms: t.duration_ms || 0, plays: t.play_count || 0,
      rating: t.stars || 0, lossless: false, on_device: true,
      _origin: t.origin, _track_id: t.track_id, _location: t.location,
      _size: t.size || 0, _plex_rk: t.rating_key || null,
    }));
    renderTracks();
    updateDeviceMusicActions();
    lcd("iAmped", `${STATE.tracks.length.toLocaleString()} tracks on ${el.dataset.name}`);
  } catch (e) {
    $("browse-body").innerHTML = `<div class="src-item disabled" style="padding:14px">${esc(e.message)}</div>`;
    lcd("iAmped", e.message);
  }
}

// ---- Videos already on a device: list + remove -------------------------
const DEVVID = { path: null, ipod: false, name: "", videos: [] };

// pane-browse is music-shaped; in video mode we hide the song header + the
// music-only action buttons and render a self-contained video manager.
function _setBrowseVideoMode(on) {
  const pane = $("pane-browse");
  pane.classList.toggle("video-mode", on);
  $("browse-head-row").style.display = on ? "none" : "";
  const acts = pane.querySelector(".browse-actions");
  if (acts) acts.style.display = on ? "none" : "";
}

async function openDeviceVideos(el) {
  const path = el.dataset.path, ipod = el.dataset.ipod === "true";
  DEVVID.path = path; DEVVID.ipod = ipod; DEVVID.name = el.dataset.name;
  STATE.view = { type: "device-videos", title: el.dataset.name, path };
  $("browse-title").textContent = `${el.dataset.name} — Videos`;
  $("btn-del-pl").classList.add("hidden");
  selectPane("pane-browse", el);
  _setBrowseVideoMode(true);
  $("browse-body").innerHTML = `<div class="muted" style="padding:14px">Reading device…</div>`;
  lcd("Reading device…", path);
  try {
    const type = ipod ? "ipod" : "massstorage";
    const r = await api(`/api/device/videos?device_path=${encodeURIComponent(path)}&device_type=${type}`);
    DEVVID.videos = r.videos || [];
    renderDeviceVideos();
    lcd("iAmped", `${r.count} video(s) on ${el.dataset.name} · ${fmtBytes(r.total_bytes || 0)}`);
  } catch (e) {
    $("browse-body").innerHTML = `<div class="src-item disabled" style="padding:14px">${esc(e.message)}</div>`;
    lcd("iAmped", e.message);
  }
}

function _dvRow(v) {
  const isEp = !!v.show;
  const tag = isEp
    ? `S${String(v.season_number || 0).padStart(2, "0")}E${String(v.episode_number || 0).padStart(2, "0")}`
    : "Movie";
  const title = isEp ? (v.subtitle || v.title) : v.title;
  return `<div class="dv-row">
    <span class="dv-tag">${esc(tag)}</span>
    <span class="dv-title">${esc(title || "Untitled")}</span>
    <span class="dv-size">${fmtBytes(v.size || 0)}</span>
    <button class="dv-remove" data-id="${v.track_id != null ? v.track_id : ""}" data-loc="${esc(v.location || "")}" title="Remove from device">Remove</button>
  </div>`;
}

function renderDeviceVideos() {
  const vids = DEVVID.videos;
  if (!vids.length) {
    $("browse-body").innerHTML = `<div class="muted" style="padding:18px">No videos synced to this device yet. Use the <b>Video</b> tab to sync movies or TV shows.</div>`;
    return;
  }
  const movies = vids.filter((v) => !v.show);
  const shows = {};
  vids.filter((v) => v.show).forEach((v) => { (shows[v.show] ||= []).push(v); });
  let html = "";
  if (movies.length) {
    html += `<div class="dv-group"><div class="dv-group-head">Movies <span class="dv-count">${movies.length}</span></div>` +
      movies.slice().sort((a, b) => (a.title || "").localeCompare(b.title || "")).map(_dvRow).join("") + `</div>`;
  }
  Object.keys(shows).sort().forEach((show) => {
    const eps = shows[show].slice().sort((a, b) =>
      (a.season_number - b.season_number) || (a.episode_number - b.episode_number));
    html += `<div class="dv-group"><div class="dv-group-head">${esc(show)} <span class="dv-count">${eps.length}</span></div>` +
      eps.map(_dvRow).join("") + `</div>`;
  });
  $("browse-body").innerHTML = html;
  $("browse-body").querySelectorAll(".dv-remove").forEach((b) => { b.onclick = () => removeDeviceVideo(b); });
}

async function removeDeviceVideo(btn) {
  if (!confirm("Remove this video from the device? The file is deleted from the device; your Plex library is untouched.")) return;
  btn.disabled = true; btn.textContent = "Removing…";
  const body = { device_path: DEVVID.path,
    device_type: DEVVID.ipod ? "ipod" : "massstorage", device_name: DEVVID.name };
  if (DEVVID.ipod) body.track_ids = [Number(btn.dataset.id)];
  else body.locations = [btn.dataset.loc];
  try {
    const r = await api("/api/device/video/remove", "POST", body);
    if (r.error) { alert(r.error); btn.disabled = false; btn.textContent = "Remove"; return; }
    lcd("iAmped", `Removed — freed ${fmtBytes(r.freed_bytes || 0)}.` +
      (DEVVID.ipod ? " Eject safely before unplugging." : ""));
    const type = DEVVID.ipod ? "ipod" : "massstorage";
    const rr = await api(`/api/device/videos?device_path=${encodeURIComponent(DEVVID.path)}&device_type=${type}`);
    DEVVID.videos = rr.videos || []; renderDeviceVideos();
    await loadSidebar();
  } catch (e) { alert(e.message); btn.disabled = false; btn.textContent = "Remove"; }
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
      <div class="c-check"><input type="checkbox" tabindex="-1" ${STATE.selected.has(t.rating_key) ? "checked" : ""}></div>
      <div class="c-num">${i + 1}</div>
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
  _setBrowseVideoMode(false);            // restore music chrome if we left video mode
  const body = $("browse-body");
  body.innerHTML = STATE.tracks.map((t, i) => rowHtml(t, i)).join("");
  bindRows([...body.querySelectorAll(".songrow")]);
  updateDeviceMusicActions();
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
  if (STATE.view?.type === "device") updateDeviceMusicActions();
}

// ---- device music: remove / ingest action bar ---------------------------
function _selectedDeviceTracks() {
  return STATE.tracks.filter((t) => STATE.selected.has(t.rating_key));
}
function updateDeviceMusicActions() {
  const bar = $("device-music-actions");
  if (!bar) return;
  const on = STATE.view?.type === "device";
  bar.classList.toggle("hidden", !on);
  if (!on) return;
  const foreign = STATE.tracks.filter((t) => t._origin === "foreign").length;
  const sel = _selectedDeviceTracks();
  $("dm-summary").textContent = sel.length
    ? `${sel.length} selected`
    : `${STATE.tracks.length} tracks · ${foreign} not from Plex`;
  $("btn-dm-remove").disabled = !sel.length;
  $("btn-dm-ingest").disabled = !sel.length;
}
function selectForeignDeviceTracks() {
  STATE.selected.clear();
  STATE.tracks.forEach((t) => { if (t._origin === "foreign") STATE.selected.add(t.rating_key); });
  document.querySelectorAll("#browse-body .songrow").forEach((r) => r.classList.toggle("selected", STATE.selected.has(r.dataset.rk)));
  updateDeviceMusicActions();
}
function _deviceRemoveBody(tracks) {
  const dm = STATE.deviceMusic;
  const body = { device_path: dm.path, device_type: dm.type, device_name: dm.name };
  if (dm.type === "ipod") body.track_ids = tracks.map((t) => t._track_id).filter((x) => x != null);
  else body.locations = tracks.map((t) => t._location).filter(Boolean);
  return body;
}
async function removeSelectedDeviceMusic() {
  const sel = _selectedDeviceTracks();
  if (!sel.length) return;
  if (!confirm(`Remove ${sel.length} track(s) from the device? The files are deleted from the device; your Plex library is untouched.`)) return;
  $("btn-dm-remove").disabled = true;
  try {
    const r = await api("/api/device/music/remove", "POST", _deviceRemoveBody(sel));
    if (r.error) { alert(r.error); return; }
    lcd("iAmped", `Removed ${r.removed} — freed ${fmtBytes(r.freed_bytes || 0)}.` +
      (STATE.deviceMusic.type === "ipod" ? " Eject safely before unplugging." : ""));
    STATE.selected.clear();
    await reloadDeviceMusic();
    await loadSidebar();
  } catch (e) { alert(e.message); }
  finally { updateDeviceMusicActions(); }
}
async function reloadDeviceMusic() {
  const dm = STATE.deviceMusic; if (!dm) return;
  const inv = await api(`/api/device/inventory?device_path=${encodeURIComponent(dm.path)}&device_type=${dm.type}`);
  STATE.tracks = (inv.tracks || []).filter((t) => t.media !== "video").map((t, i) => ({
    rating_key: `dev:${i}`, title: t.title, artist: t.artist, album: t.album,
    duration_ms: t.duration_ms || 0, plays: t.play_count || 0, rating: t.stars || 0,
    lossless: false, on_device: true, _origin: t.origin, _track_id: t.track_id,
    _location: t.location, _size: t.size || 0, _plex_rk: t.rating_key || null,
  }));
  renderTracks(); updateDeviceMusicActions();
}

// ---- device music: ingest back into Plex --------------------------------
let INGEST = { items: [] };
async function ingestSelectedDeviceMusic() {
  const sel = _selectedDeviceTracks();
  if (!sel.length) return;
  const dm = STATE.deviceMusic;
  $("btn-dm-ingest").disabled = true;
  lcd("Ingest", "Checking which tracks Plex already has…");
  try {
    const body = { device_path: dm.path, device_type: dm.type };
    if (dm.type === "ipod") body.track_ids = sel.map((t) => t._track_id).filter((x) => x != null);
    else body.locations = sel.map((t) => t._location).filter(Boolean);
    const r = await api("/api/device/ingest/preview", "POST", body);
    INGEST.items = r.items || [];
    $("ingest-wizard-summary").textContent =
      `${r.ingest_count} track(s) to add to Plex (${fmtBytes(r.ingest_bytes)})` +
      (r.skip_count ? ` · ${r.skip_count} already in Plex (skipped)` : "");
    $("ingest-list").innerHTML = INGEST.items.map((it) => {
      const skip = it.status === "skip_exists";
      return `<div class="songrow" style="opacity:${skip ? .55 : 1}">
        <div class="c-name">${esc(it.title || "Untitled")}${skip ? ' <span class="lossless-tag">in Plex</span>' : ''}</div>
        <div class="c-artist">${esc(it.artist || "")}</div>
        <div class="c-time">${fmtBytes(it.size || 0)}</div></div>`;
    }).join("") || `<div class="muted" style="padding:12px">Nothing selected.</div>`;
    $("ingest-status").textContent = "";
    $("ingest-confirm").disabled = r.ingest_count === 0;
    $("ingest-wizard").classList.remove("hidden");
    lcd("iAmped", `${r.ingest_count} to ingest, ${r.skip_count} already in Plex`);
  } catch (e) {
    alert(e.message); lcd("iAmped", e.message);
  } finally { updateDeviceMusicActions(); }
}
function closeIngest() { $("ingest-wizard").classList.add("hidden"); }
async function confirmIngest() {
  const dm = STATE.deviceMusic;
  const s = $("ingest-status"); s.className = "status"; s.textContent = "Starting…";
  $("ingest-confirm").disabled = true;
  try {
    const { job } = await api("/api/device/ingest", "POST", {
      device_path: dm.path, device_type: dm.type, device_name: dm.name,
      items: INGEST.items });
    pollJob(job, {
      onProgress: (j) => { s.textContent = j.message || "Working…"; lcd("Ingest", j.message || ""); },
      onDone: async (j) => {
        const r = j.result || {};
        s.className = "status ok"; s.textContent = j.message;
        lcd("iAmped", j.message);
        STATE.selected.clear();
        await reloadDeviceMusic(); await loadSidebar(); await refreshStats();
        if (!(r.unconfirmed || []).length) setTimeout(closeIngest, 1500);
      },
      onError: (e) => { s.className = "status err"; s.textContent = e; $("ingest-confirm").disabled = false; },
    });
  } catch (e) { s.className = "status err"; s.textContent = e.message; $("ingest-confirm").disabled = false; }
}
async function saveIngestDir() {
  const dir = $("ingest-dir").value.trim();
  try {
    await api("/api/config", "POST", { ingest_dir: dir });
    const hint = $("ingest-dir-hint");
    hint.textContent = "Saved.";
    try {
      const r = await api("/api/plex/music/locations");
      const locs = (r.sections || []).flatMap((s) => s.locations);
      if (locs.length) hint.textContent = "Saved. Plex watches: " + locs.join(", ");
    } catch (e) {}
  } catch (e) { alert(e.message); }
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
function currentTrack() {
  return STATE.playing >= 0 ? STATE.tracks[STATE.playing] : null;
}
function trackDuration() {
  const metadataDuration = Number(currentTrack()?.duration_ms || 0) / 1000;
  if (metadataDuration > 0) return metadataDuration;
  return Number.isFinite(player.duration) && player.duration > 0 ? player.duration : 0;
}
function playbackPosition() {
  return Math.min(trackDuration() || Infinity, STATE.playbackOffset + (player.currentTime || 0));
}
function updatePlaybackDisplay(position = playbackPosition()) {
  const duration = trackDuration();
  scrub(duration ? position / duration : 0, position, duration);
}
function streamUrl(t, start = 0) {
  const base = `/api/stream/${encodeURIComponent(t.rating_key)}`;
  return start > 0 ? `${base}?start=${encodeURIComponent(start.toFixed(3))}` : base;
}
function playIndex(i) {
  if (i < 0 || i >= STATE.tracks.length) return;
  STATE.playing = i;
  const t = STATE.tracks[i];
  STATE.playbackOffset = 0;
  player.src = streamUrl(t);
  player.play().catch(() => {});
  lcd(t.title, `${t.artist} — ${t.album}`);
  $("lcd-prog").classList.add("seekable");
  updatePlaybackDisplay(0);
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
player.ontimeupdate = () => { if (!scrubbing) updatePlaybackDisplay(); };
player.onloadedmetadata = () => updatePlaybackDisplay();
player.ondurationchange = () => updatePlaybackDisplay();
player.onended = () => playIndex(STATE.playing + 1);

// ---- scrubbing: the LCD progress bar doubles as an iTunes-style seek bar ----
const prog = $("lcd-prog");
let scrubbing = false;
let pendingSeek = null;
function seekFromEvent(clientX) {
  const duration = trackDuration();
  if (!duration) return;
  const r = prog.getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
  const target = frac * duration;
  pendingSeek = target;
  updatePlaybackDisplay(target);
}

function commitSeek(target) {
  if (target == null) return;
  // Range-capable/native streams can seek in place. A live ffmpeg stream has
  // an infinite/unknown media duration, so restart it at the requested offset.
  if (Number.isFinite(player.duration) && player.duration > 0) {
    const localTarget = target - STATE.playbackOffset;
    if (localTarget >= 0 && localTarget <= player.duration) {
      player.currentTime = localTarget;
      return;
    }
  }
  const t = currentTrack();
  if (!t) return;
  const wasPlaying = !player.paused;
  STATE.playbackOffset = target;
  player.src = streamUrl(t, target);
  if (wasPlaying) player.play().catch(() => {});
}
prog.addEventListener("mousedown", (e) => {
  if (STATE.playing < 0 || !trackDuration()) return;
  scrubbing = true; pendingSeek = null; seekFromEvent(e.clientX); e.preventDefault();
});
window.addEventListener("mousemove", (e) => { if (scrubbing) seekFromEvent(e.clientX); });
window.addEventListener("mouseup", () => {
  if (!scrubbing) return;
  scrubbing = false;
  commitSeek(pendingSeek);
  pendingSeek = null;
});

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
    target_bitrate_k: STATE.bitrateByFormat[activeFormat()],
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
  document.querySelectorAll(".src-item").forEach((s) => s.classList.remove("selected"));
  el.classList.add("selected");
  openDeviceInspector();
  const path = el.dataset.path, isIpod = el.dataset.ipod === "true";
  const model = el.dataset.ipodModel || el.dataset.deviceModel;
  $("device-path").value = path; $("dev-name-h").textContent = el.dataset.name;
  STATE.currentDevice = {
    path, type: isIpod ? "ipod" : "massstorage", name: el.dataset.name,
    is_ipod: isIpod, ipod_model: el.dataset.ipodModel,
    ipod_generation: el.dataset.generation, model: el.dataset.deviceModel,
    transport: el.dataset.transport || "",
    busloc: el.dataset.busloc || "", generation: el.dataset.generation || "",
  };
  $("dev-icon").innerHTML = deviceIcon(STATE.currentDevice, "inspector");
  if (document.getElementById("pane-video")?.classList.contains("active")) updateVideoTarget();
  STATE.manualKeys = null; STATE.manualPlaylistIds = null;
  STATE.transferMaxTracks = null; STATE.review = [];
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
    if (profile.target_bitrate_k) {
      STATE.bitrateByFormat[isIpod ? "aac" : "mp3"] = Number(profile.target_bitrate_k);
    }
    if (profile.sync_artwork != null) $("sync-artwork").checked = profile.sync_artwork;
    if (profile.mirror != null) $("mirror").checked = profile.mirror;
    if (profile.name) $("device-name").value = profile.name;
  } catch (_) {}
  updateBitrateControl();
  await loadBackups();
}

function openDeviceInspector() {
  $("pane-device").classList.add("open");
  $("view-inspector").classList.add("active");
  bottomDevice();
}
function closeDeviceInspector() {
  $("pane-device").classList.remove("open");
  $("view-inspector").classList.remove("active");
  if (document.querySelector(".content > #pane-browse.active")) bottomBrowse();
}
function toggleDeviceType() {
  const t = [...document.getElementsByName("dtype")].find((r) => r.checked).value;
  $("ipod-warn").classList.toggle("hidden", t !== "ipod");
  $("devname-wrap").classList.toggle("hidden", t !== "ipod");
  updateBitrateControl();
}
for (const r of document.getElementsByName("dtype")) r.onchange = toggleDeviceType;
function activeFormat() {
  return [...document.getElementsByName("dtype")].find((r) => r.checked)?.value === "ipod"
    ? "aac" : "mp3";
}
function setBitrate(format, bitrate) {
  const values = BITRATE_PRESETS[format];
  const closest = values.reduce((a, b) =>
    Math.abs(b - bitrate) < Math.abs(a - bitrate) ? b : a);
  STATE.bitrateByFormat[format] = closest;
  if (activeFormat() === format) updateBitrateControl();
}
function updateBitrateControl() {
  const format = activeFormat();
  const values = BITRATE_PRESETS[format];
  const current = STATE.bitrateByFormat[format] || values[values.length - 1];
  const index = Math.max(0, values.indexOf(current));
  $("bitrate-format").textContent = `${format.toUpperCase()} bitrate`;
  $("bitrate-value").textContent = `${values[index]} kbps`;
  $("bitrate-notch").max = String(values.length - 1);
  $("bitrate-notch").value = String(index);
  $("bitrate-ticks").innerHTML = values.map((value) => `<span>${value}</span>`).join("");
}
$("bitrate-notch").oninput = () => {
  const format = activeFormat();
  const value = BITRATE_PRESETS[format][Number($("bitrate-notch").value)];
  STATE.bitrateByFormat[format] = value;
  $("bitrate-value").textContent = `${value} kbps`;
};
function deviceParams() {
  const t = [...document.getElementsByName("dtype")].find((r) => r.checked).value;
  const p = {
    device_path: $("device-path").value.trim(), device_type: t,
    device_name: $("device-name").value.trim() || "iPod",
    reserve_mb: Number($("reserve").value) || 0, fill_strategy: $("strategy").value,
    transcode_lossless: $("transcode").checked, playlist_ids: selectedPlaylists(),
    target_bitrate_k: STATE.bitrateByFormat[t === "ipod" ? "aac" : "mp3"],
    sync_artwork: $("sync-artwork").checked,
    mirror: $("mirror").checked,
  };
  if (STATE.manualKeys) p.mirror = false;
  if (STATE.manualKeys) p.rating_keys = STATE.manualKeys;
  if (STATE.manualPlaylistIds) {
    p.mirror = false;
    p.playlist_ids = STATE.manualPlaylistIds;
    p.playlist_only = true;
  }
  if (STATE.transferMaxTracks) p.max_tracks = STATE.transferMaxTracks;
  if (STATE.manualKeys || STATE.manualPlaylistIds) p.transfer_request = true;
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
  STATE.manualKeys = null; STATE.manualPlaylistIds = null;
  STATE.transferMaxTracks = null; STATE.transferTitle = "";
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
  const actionMeta = {
    keep: ["✓", "Keep"], add: ["+", "Add"],
    update: ["↻", "Update"], remove: ["−", "Remove"],
  };
  const grouped = ["keep", "add", "update", "remove"].map((action) => {
    const items = STATE.review.filter((item) => item.action === action);
    const count = action === "keep" ? r.keep_count : items.length;
    const [icon, label] = actionMeta[action];
    const rows = items.length ? items.map((item, index) => `<label class="songrow ${item.action}${index >= 5 ? " review-extra hidden" : ""}">
      <div class="c-check"><input class="review-op" type="checkbox" value="${esc(item.id)}" ${item.checked ? "checked" : ""}></div>
      <div class="c-name">${esc(item.title)}</div><div class="c-artist">${esc(item.artist)}</div>
      <div class="c-size">${fmtBytes(item.size)}</div></label>`).join("")
      : `<div class="review-empty">No items to ${action}</div>`;
    return `<section class="review-group ${action}">
      <div class="review-group-head"><span class="review-icon">${icon}</span>
        <b>${label} (${count})</b><span>${action === "remove" ? "—" : "✓"}</span></div>
      <div class="review-columns"><span>Name</span><span>Artist</span><span>Size</span></div>
      ${rows}${items.length > 5 ? `<button class="review-more" type="button">and ${items.length - 5} more…</button>` : ""}</section>`;
  }).join("");
  $("review-list").innerHTML = grouped;
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
  document.querySelectorAll(".review-more").forEach((button) => {
    button.onclick = () => {
      const group = button.closest(".review-group");
      const extras = [...group.querySelectorAll(".review-extra")];
      const opening = extras.some((row) => row.classList.contains("hidden"));
      extras.forEach((row) => row.classList.toggle("hidden", !opening));
      button.textContent = opening ? "Show fewer" : `and ${extras.length} more…`;
    };
  });
}

async function dropSelectionOnDevice(el) {
  const keys = [...STATE.selected];
  if (!keys.length) return;
  await selectDevice(el);
  STATE.manualKeys = keys;
  STATE.manualPlaylistIds = null;
  STATE.transferMaxTracks = null;
  STATE.transferTitle = `${keys.length} selected track${keys.length === 1 ? "" : "s"}`;
  await planDraggedTransfer();
}

async function dropPlaylistOnDevice(el, playlist) {
  if (!playlist?.id) return;
  await selectDevice(el);
  STATE.manualKeys = null;
  STATE.manualPlaylistIds = [playlist.id];
  STATE.transferMaxTracks = null;
  STATE.transferTitle = playlist.title || "Playlist";
  await planDraggedTransfer();
}

async function planDraggedTransfer(offerWizard = true) {
  const s = $("sync-status");
  s.className = "status"; s.textContent = `Planning “${STATE.transferTitle}”…`;
  lcd("Planning drag to device", STATE.transferTitle);
  try {
    const r = await api("/api/plan", "POST", deviceParams());
    renderPlan(r);
    $("btn-sync").disabled = (r.track_count + r.remove_count) === 0;
    lcd("iAmped", `${r.add_count} additions · ${r.target_bitrate_k} kbps ${r.target_format.toUpperCase()}`);
    if (offerWizard && r.skipped_for_space > 0) openTransferWizard(r);
  } catch (e) {
    s.className = "status err"; s.textContent = e.message;
  }
}

function closeTransferWizard() {
  $("transfer-wizard").classList.add("hidden");
  STATE.pendingTransferPlan = null;
}

function openTransferWizard(planResult) {
  STATE.pendingTransferPlan = planResult;
  const requested = planResult.requested_track_count || planResult.desired_track_count;
  $("transfer-wizard-summary").textContent =
    `“${STATE.transferTitle}” needs ${fmtBytes(planResult.requested_bytes)}, but ${fmtBytes(planResult.budget_bytes)} is available after headroom.`;

  const lowerFits = (planResult.bitrate_options || [])
    .filter((option) => option.fits && option.bitrate_k < planResult.target_bitrate_k)
    .sort((a, b) => b.bitrate_k - a.bitrate_k);
  const bitrateChoice = document.querySelector('input[name="transfer-choice"][value="bitrate"]');
  const tracksChoice = document.querySelector('input[name="transfer-choice"][value="tracks"]');
  const bitrateOption = $("transfer-bitrate-option");
  if (lowerFits.length) {
    bitrateChoice.disabled = false; bitrateChoice.checked = true;
    bitrateOption.classList.remove("unavailable");
    $("transfer-bitrate").disabled = false;
    $("transfer-bitrate").innerHTML = lowerFits.map((option) =>
      `<option value="${option.bitrate_k}">${option.bitrate_k} kbps · ${fmtBytes(option.requested_bytes)}</option>`
    ).join("");
    $("transfer-bitrate-help").textContent =
      `${lowerFits[0].bitrate_k} kbps is the highest setting that fits all ${requested} songs.`;
  } else {
    bitrateChoice.disabled = true; tracksChoice.checked = true;
    bitrateOption.classList.add("unavailable");
    $("transfer-bitrate").disabled = true;
    const lowest = (planResult.bitrate_options || [])[0];
    $("transfer-bitrate").innerHTML = lowest
      ? `<option>${lowest.bitrate_k} kbps</option>` : "<option>No smaller preset</option>";
    $("transfer-bitrate-help").textContent =
      lowest ? `Even ${lowest.bitrate_k} kbps fits only ${lowest.fitting_tracks} songs.`
        : "No lower bitrate is available.";
  }
  $("transfer-track-count").max = String(Math.max(1, requested));
  $("transfer-track-count").value = String(Math.max(1, planResult.desired_track_count));
  $("transfer-wizard").classList.remove("hidden");
}

function cancelTransfer() {
  closeTransferWizard();
  STATE.manualKeys = null; STATE.manualPlaylistIds = null;
  STATE.transferMaxTracks = null;
  $("btn-sync").disabled = true;
  $("sync-status").textContent = "Transfer cancelled.";
}
$("transfer-cancel").onclick = cancelTransfer;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("transfer-wizard").classList.contains("hidden")) cancelTransfer();
});
$("transfer-apply").onclick = async () => {
  const choice = document.querySelector('input[name="transfer-choice"]:checked')?.value;
  if (choice === "bitrate") {
    setBitrate(activeFormat(), Number($("transfer-bitrate").value));
    $("transcode").checked = true;
    STATE.transferMaxTracks = null;
  } else {
    STATE.transferMaxTracks = Math.max(1, Number($("transfer-track-count").value) || 1);
  }
  closeTransferWizard();
  await planDraggedTransfer(false);
};

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
$("btn-dm-select-foreign").onclick = selectForeignDeviceTracks;
$("btn-dm-remove").onclick = removeSelectedDeviceMusic;
$("btn-dm-ingest").onclick = ingestSelectedDeviceMusic;
$("btn-save-ingest").onclick = saveIngestDir;
$("ingest-cancel").onclick = closeIngest;
$("ingest-confirm").onclick = confirmIngest;
$("src-music").onclick = openLibrary;
$("btn-close-inspector").onclick = closeDeviceInspector;
$("view-inspector").onclick = () => {
  if ($("pane-device").classList.contains("open")) closeDeviceInspector();
  else if (STATE.currentDevice) openDeviceInspector();
};
$("view-list").onclick = () => {
  $("view-list").classList.add("active");
  openLibrary();
};
$("view-visualizer").onclick = () => toggleVisualizer();

// ---------------------------------------------------------------- video sync
const VIDEO = { loaded: false, sections: [], kind: "movie",
                selected: new Map(), shows: {} };

async function openVideo() {
  updateVideoTarget();
  if (VIDEO.loaded) return;
  try {
    const r = await api("/api/video/sections");
    VIDEO.sections = r.sections || [];
  } catch (e) {
    $("video-list").innerHTML = `<div class="muted">${esc(e.message)}</div>`;
    return;
  }
  VIDEO.loaded = true;
  const sel = $("video-section");
  if (!VIDEO.sections.length) {
    sel.innerHTML = "";
    $("video-list").innerHTML = '<div class="muted">No movie or TV libraries on this Plex server.</div>';
    return;
  }
  sel.innerHTML = VIDEO.sections.map((s) =>
    `<option value="${esc(s.title)}" data-type="${esc(s.type)}">${esc(s.title)}${s.type === "show" ? " (TV)" : ""}</option>`).join("");
  sel.onchange = loadVideoItems;
  loadVideoItems();
}

async function loadVideoItems() {
  const sel = $("video-section");
  const section = sel.value;
  const opt = sel.options[sel.selectedIndex];
  VIDEO.kind = opt?.dataset.type === "show" ? "show" : "movie";
  VIDEO.selected.clear();
  VIDEO.shows = {};
  updateVideoCount();
  $("video-list").innerHTML = '<div class="muted">Loading…</div>';
  try {
    const r = await api(`/api/video/items?section=${encodeURIComponent(section)}&kind=${VIDEO.kind}`);
    if (r.kind === "show") renderShows(r.items);
    else renderMovies(r.items);
  } catch (e) {
    $("video-list").innerHTML = `<div class="muted">${esc(e.message)}</div>`;
  }
}

const thumbUrl = (key) => key ? `/api/video/thumb?key=${encodeURIComponent(key)}` : "";
const videoMeta = (it) => [it.year, it.width && it.height ? `${it.width}×${it.height}` : null,
  (it.video_codec || "").toUpperCase(), it.size ? fmtBytes(it.size) : null].filter(Boolean).join(" · ");

function renderMovies(items) {
  if (!items.length) { $("video-list").innerHTML = '<div class="muted">No movies in this library.</div>'; return; }
  $("video-list").innerHTML = items.map((m) => `
    <label class="video-row" data-rk="${esc(m.rating_key)}">
      <input type="checkbox" class="video-pick" value="${esc(m.rating_key)}">
      ${m.thumb ? `<img class="video-thumb" loading="lazy" src="${thumbUrl(m.thumb)}" alt="">` : '<span class="video-thumb noimg"></span>'}
      <span class="video-info"><span class="video-title">${esc(m.title)}</span><span class="video-sub">${esc(videoMeta(m))}</span></span>
    </label>`).join("");
  $("video-list").querySelectorAll(".video-pick").forEach((c) => {
    c.onchange = () => { setVideoSel(c.value, c.checked, c.closest(".video-row").querySelector(".video-title").textContent); };
  });
}

function renderShows(items) {
  if (!items.length) { $("video-list").innerHTML = '<div class="muted">No shows in this library.</div>'; return; }
  $("video-list").innerHTML = items.map((s) => `
    <div class="video-show" data-key="${esc(s.key)}">
      <div class="video-show-head">
        ${s.thumb ? `<img class="video-thumb" loading="lazy" src="${thumbUrl(s.thumb)}" alt="">` : '<span class="video-thumb noimg"></span>'}
        <span class="video-info"><span class="video-title">${esc(s.title)}</span><span class="video-sub">${s.episode_count || 0} episodes${s.year ? " · " + s.year : ""}</span></span>
        <span class="video-expand">▸</span>
      </div>
      <div class="video-eps hidden"></div>
    </div>`).join("");
  $("video-list").querySelectorAll(".video-show-head").forEach((h) => {
    h.onclick = () => toggleShow(h.closest(".video-show"));
  });
}

async function toggleShow(showEl) {
  const eps = showEl.querySelector(".video-eps");
  const arrow = showEl.querySelector(".video-expand");
  if (!eps.classList.contains("hidden")) { eps.classList.add("hidden"); arrow.textContent = "▸"; return; }
  arrow.textContent = "▾";
  eps.classList.remove("hidden");
  if (eps.dataset.loaded) return;
  eps.innerHTML = '<div class="muted">Loading episodes…</div>';
  try {
    const r = await api(`/api/video/episodes?show_key=${encodeURIComponent(showEl.dataset.key)}`);
    eps.dataset.loaded = "1";
    eps.innerHTML = (r.items || []).map((e) => {
      const tag = `S${String(e.season_number).padStart(2, "0")}E${String(e.episode_number).padStart(2, "0")}`;
      return `<label class="video-row ep" data-rk="${esc(e.rating_key)}">
        <input type="checkbox" class="video-pick" value="${esc(e.rating_key)}">
        <span class="video-tag">${tag}</span>
        <span class="video-info"><span class="video-title">${esc(e.title)}</span><span class="video-sub">${esc(videoMeta(e))}</span></span></label>`;
    }).join("") || '<div class="muted">No episodes.</div>';
    eps.querySelectorAll(".video-pick").forEach((c) => {
      c.onchange = () => { const row = c.closest(".video-row");
        setVideoSel(c.value, c.checked, showEl.querySelector(".video-title").textContent + " – " + row.querySelector(".video-title").textContent); };
    });
  } catch (e) {
    eps.innerHTML = `<div class="muted">${esc(e.message)}</div>`;
  }
}

function setVideoSel(rk, on, label) {
  if (on) VIDEO.selected.set(rk, label); else VIDEO.selected.delete(rk);
  updateVideoCount();
}

function updateVideoCount() {
  $("video-count").textContent = `${VIDEO.selected.size} selected`;
  refreshVideoSyncBtn();
}

async function updateVideoTarget() {
  const d = STATE.currentDevice;
  const tgt = $("video-target");
  if (!d) { tgt.textContent = "No device selected — pick one under Devices."; tgt.className = "video-target"; refreshVideoSyncBtn(false); return; }
  tgt.textContent = `Target: ${d.name}…`;
  try {
    const q = new URLSearchParams({ device_path: d.path || "", device_type: d.type || "",
      mtp_busloc: d.busloc || "", transport: d.transport || "", ipod_generation: d.generation || "" });
    const r = await api(`/api/video/device?${q}`);
    VIDEO.support = r.video_support;
    if (r.video_support) {
      tgt.textContent = `Target: ${d.name} · ${r.profile}`;
      tgt.className = "video-target ok";
    } else {
      tgt.textContent = `${d.name} can't play video iAmped can sync.`;
      tgt.className = "video-target bad";
    }
  } catch (_) { VIDEO.support = false; }
  refreshVideoSyncBtn();
}

function refreshVideoSyncBtn(support) {
  const ok = (support === undefined ? VIDEO.support : support) && VIDEO.selected.size > 0;
  $("btn-video-sync").disabled = !ok;
}

async function syncVideo() {
  const d = STATE.currentDevice;
  if (!d || !VIDEO.selected.size) return;
  const keys = [...VIDEO.selected.keys()];
  const labels = [...VIDEO.selected.values()];
  if (!confirm(`Sync ${keys.length} video(s) to ${d.name}?\n\n${labels.slice(0, 8).join("\n")}${labels.length > 8 ? "\n…" : ""}`)) return;
  const s = $("video-status");
  $("btn-video-sync").disabled = true;
  s.textContent = "Starting…";
  showVideoProgress(true);
  setVideoProgress("Preparing…", 0);
  const params = { rating_keys: keys, device_path: d.path, device_type: d.type,
    mtp_busloc: d.busloc || undefined, transport: d.transport || undefined,
    ipod_generation: d.generation || undefined };
  try {
    const { job, error } = await api("/api/video/sync", "POST", params);
    if (error) { s.textContent = error; showVideoProgress(false); refreshVideoSyncBtn(); return; }
    pollJob(job, {
      onProgress: (j) => {
        // Overall fraction = finished files + the current file's encode fraction,
        // so the bar advances smoothly through a long single transcode.
        const total = j.total || 0;
        const frac = total ? Math.min(1, ((j.done || 0) + (j.item_progress || 0)) / total) : 0;
        const enc = j.encoder ? ` · ${j.encoder}` : "";
        setVideoProgress(`${j.message || j.phase}${total ? `  (${j.done}/${total})` : ""}${enc}`, frac);
        s.textContent = "";
      },
      onDone: (r) => {
        showVideoProgress(false);
        s.textContent = `Done — ${r.videos_added} added` +
        (r.videos_skipped ? `, ${r.videos_skipped} already present` : "") +
        ` (${r.profile}).` + (d.type === "ipod" ? " Eject safely before unplugging." : "");
        VIDEO.selected.clear();
        document.querySelectorAll("#video-list .video-pick").forEach((c) => { c.checked = false; });
        updateVideoCount(); refreshVideoSyncBtn(); },
      onError: (e) => { showVideoProgress(false); s.textContent = `Error: ${e}`; refreshVideoSyncBtn(); },
    });
  } catch (e) { showVideoProgress(false); s.textContent = `Error: ${e.message}`; refreshVideoSyncBtn(); }
}
$("btn-video-sync").onclick = syncVideo;

// ---------------------------------------------------------------- visualizer
function resizeVisualizer() {
  const canvas = $("visualizer-canvas");
  if (!$("lcd").classList.contains("visualizer-on")) return;
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
function ensureVisualizerAnalyser() {
  const viz = STATE.visualizer;
  if (!viz) return;
  if (!viz._interacted && !player.paused) {
    try { viz.connectAnalyser(); } catch (_) {}
  }
  if (viz._audioContext?.state === "suspended") viz._audioContext.resume().catch(() => {});
}
function enableVisualizer() {
  if (!window.Wave) { lcd("Visualizer unavailable", "Wave.js did not load"); return; }
  STATE.visualizerEnabled = true;
  $("lcd").classList.add("visualizer-on");
  $("view-visualizer").classList.add("active");
  $("lcd-viz-toggle").textContent = "▸";
  $("lcd-viz-toggle").title = "Hide visualizer";
  $("lcd-viz-toggle").setAttribute("aria-pressed", "true");
  $("visualizer-empty").textContent = "Visualizer enabled in the playback display.";
  $("btn-visualizer").textContent = "Disable";
  resizeVisualizer();
  if (!STATE.visualizer) STATE.visualizer = new Wave(player, $("visualizer-canvas"));
  STATE.visualizer.clearAnimations();
  STATE.visualizer.addAnimation(visualizerAnimation(STATE.visualizer));
  ensureVisualizerAnalyser();
  requestAnimationFrame(() => {
    resizeVisualizer();
  });
}
function disableVisualizer() {
  if (STATE.visualizer) STATE.visualizer.clearAnimations();
  STATE.visualizerEnabled = false;
  $("lcd").classList.remove("visualizer-on");
  $("view-visualizer").classList.remove("active");
  $("lcd-viz-toggle").textContent = "◂";
  $("lcd-viz-toggle").title = "Show visualizer";
  $("lcd-viz-toggle").setAttribute("aria-pressed", "false");
  $("visualizer-empty").textContent = "The visualizer is off. Enable it here or with the small tab on the playback display.";
  $("btn-visualizer").textContent = "Enable";
}
function toggleVisualizer() {
  if (STATE.visualizerEnabled) disableVisualizer();
  else enableVisualizer();
}
$("btn-visualizer").onclick = toggleVisualizer;
$("lcd-viz-toggle").onclick = toggleVisualizer;
$("visualizer-style").onchange = () => { if (STATE.visualizerEnabled) enableVisualizer(); };
player.addEventListener("play", () => {
  if (STATE.visualizerEnabled) ensureVisualizerAnalyser();
});
$("volume").oninput = () => { player.volume = Number($("volume").value); };
window.addEventListener("resize", resizeVisualizer);

// The device surface is a persistent optional inspector, not a replacement
// page. Move it beside the main content at runtime to keep existing form IDs
// and sync behavior intact.
document.querySelector(".body").appendChild($("pane-device"));
$("pane-device").classList.add("device-inspector");

loadConfig();
loadDevices();
