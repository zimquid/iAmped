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
// Deterministic placeholder artwork: a stable hue from the text + a centred glyph,
// so albums/artists without cached art still read as distinct tiles.
const artHue = (s) => { let h = 0; for (const c of String(s || "")) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
function artFallback(name) {
  const ch = String(name || "").trim()[0];
  const glyph = ch ? esc(ch.toUpperCase()) : "♪";
  return `<span class="art-fallback" style="--h:${artHue(name)}"><span>${glyph}</span></span>`;
}
// Interactive 5-star rating control for a track row (reversed DOM so CSS can fill
// up to the hovered star). data-star is the 1–5 value; Plex stores it ×2 (0–10).
function ratingStarsHtml(rk, rating) {
  const val = Number(rating || 0);
  return `<span class="row-stars" data-rk="${esc(rk)}">` +
    [5, 4, 3, 2, 1].map((s) =>
      `<span class="row-star${val >= s * 2 - 0.1 ? " on" : ""}" data-star="${s}">★</span>`).join("") +
    `</span>`;
}
const BITRATE_PRESETS = {
  aac: [64, 96, 128, 160, 192, 256],
  mp3: [96, 128, 160, 192, 256, 320],
};
const MAX_PLAYBACK_RECOVERIES = 8;

async function api(path, method = "GET", body = null, signal = null) {
  const opt = { method, headers: { "Content-Type": "application/json" }, signal };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}
function pollJob(jobId, { onProgress, onDone, onError }) {
  const tick = async () => {
    let job;
    try { job = await api(`/api/job/${jobId}`); } catch (e) { onError(e.message, {}); return; }
    onProgress(job);
    if (job.status === "running") setTimeout(tick, 600);
    else if (job.status === "done") onDone(job);
    else onError(job.error || "Job failed", job);
  };
  tick();
}

const STATE = {
  view: null,          // {type:'library'|'playlist', id, title, source}
  tracks: [],          // current queue
  selected: new Set(),
  selAnchor: null,     // last-clicked row index, anchors shift-click ranges
  sort: "artist",
  search: "",
  offset: 0, total: 0, loading: false, browseRequest: 0, browseAbort: null,
  playlists: [],
  facets: { artists: [], albums: [], genres: [] },
  playing: -1,
  shuffle: false,
  repeat: "off",
  playHistory: [],
  scrobble: { session: 0, posted: false },
  readbackPlan: null,
  plexLoginId: null,
  plexPollTimer: null,
  manualKeys: null,
  review: [],
  currentDevice: null,
  visualizer: null,
  visualizerEnabled: false,
  visualizerStyle: "bars",
  visualizerFullscreenEngine: "butterchurn",
  visualizerFullscreen: false,
  playbackOffset: 0,
  playbackRecoveryKey: null,
  playbackRecoveryCount: 0,
  nativePlaybackAvailable: false,
  nativePlaybackActive: false,
  nativePlaybackPaused: true,
  nativePlaybackPosition: 0,
  nativePlaybackDuration: 0,
  nativePlaybackTimer: null,
  nativePlaybackLastPosition: 0,
  nativeNextIndex: -1,   // queue index handed to mpv for gapless auto-advance
  bitrateByFormat: { aac: 256, mp3: 320 },
  manualPlaylistIds: null,
  transferMaxTracks: null,
  transferTitle: "",
  pendingTransferPlan: null,
  draggedPlaylist: null,
  fillStrategy: "most_played",
  activity: null,
  lcdMode: "default",
  sources: [],
  activeSourceId: "",
  sourceCapabilities: {},
};

const fmtClock = (sec) => {
  sec = Math.max(0, Math.round(sec || 0));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
};
const isDeviceTrack = (t) => String(t?.rating_key || "").startsWith("dev:");
const artworkUrl = (key) => key ? `/api/artwork?key=${encodeURIComponent(key)}` : "";
const plural = (n, word) => `${Number(n || 0).toLocaleString()} ${word}${Number(n || 0) === 1 ? "" : "s"}`;
const reserveDefaultMb = (value) => {
  const n = Number(value);
  return Number.isFinite(n) && n !== 200 ? n : 20;
};

function activeEditableTarget(e) {
  const tag = e.target?.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.target?.isContentEditable;
}

function updateTransportModes() {
  // shuffle/repeat changes alter what "next" means — re-queue mpv's gapless
  // up-next to match (harmless no-op when nothing is playing natively)
  if (typeof queueUpNext === "function" && STATE.nativePlaybackActive) queueUpNext();
  $("t-shuffle").classList.toggle("active", STATE.shuffle);
  $("t-shuffle").title = STATE.shuffle ? "Shuffle: on" : "Shuffle: off";
  $("t-shuffle").setAttribute("aria-pressed", STATE.shuffle ? "true" : "false");
  $("t-repeat").classList.toggle("active", STATE.repeat !== "off");
  $("repeat-one").classList.toggle("hidden", STATE.repeat !== "one");
  $("t-repeat").title = STATE.repeat === "one" ? "Repeat One" : STATE.repeat === "all" ? "Repeat All" : "Repeat";
}

function updateTrackRows(ratingKey, patch) {
  STATE.tracks.forEach((track) => {
    if (track.rating_key === ratingKey) Object.assign(track, patch);
  });
  document.querySelectorAll(`#browse-body .songrow[data-rk="${CSS.escape(ratingKey)}"]`).forEach((row) => {
    if (patch.plays != null) row.querySelector(".c-plays").textContent = patch.plays || 0;
    if (patch.rating != null) row.querySelector(".c-rating").innerHTML = ratingStarsHtml(ratingKey, patch.rating);
  });
}

function renderNowPlayingRating(t) {
  const wrap = $("np-rating");
  const value = Number(t?.rating || 0);
  const disabled = !t || isDeviceTrack(t);
  wrap.innerHTML = [1, 2, 3, 4, 5].map((star) => {
    const on = value >= star * 2 - 0.1;
    return `<button class="np-star${on ? " on" : ""}" data-rating="${star * 2}" ${disabled ? "disabled" : ""} title="${star} star${star === 1 ? "" : "s"}">${on ? "★" : "☆"}</button>`;
  }).join("");
  wrap.querySelectorAll(".np-star").forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      setCurrentTrackRating(Number(btn.dataset.rating));
    };
  });
}

function updateNowPlaying() {
  const t = currentTrack();
  const art = $("np-art");
  if (!t) {
    $("np-title").textContent = "Not Playing";
    $("np-sub").textContent = "Select a track to preview";
    art.classList.add("empty");
    art.style.backgroundImage = "";
    renderNowPlayingRating(null);
    return;
  }
  $("np-title").textContent = t.title || "Untitled";
  $("np-sub").textContent = [t.artist, t.album].filter(Boolean).join(" — ") || "Unknown artist";
  art.classList.toggle("empty", !t.album_thumb);
  art.style.backgroundImage = t.album_thumb ? `url("${artworkUrl(t.album_thumb)}")` : "";
  renderNowPlayingRating(t);
  updateMediaSession(t);
}

// ---- LCD + bottom bar ---------------------------------------------------
// `lcd()` drives the title/subtitle and the generic progress fill used by
// non-playback status updates (no track times shown).
function renderLcd(main, sub, frac) {
  if (main != null) $("lcd-main").textContent = main;
  if (sub != null) $("lcd-sub").textContent = sub;
  const row = $("lcd-progrow");
  row.classList.remove("with-time");        // generic progress — no clock times
  if (frac == null) row.classList.add("hidden");
  else { row.classList.remove("hidden"); $("lcd-prog").firstElementChild.style.width = `${Math.min(100, frac * 100)}%`; }
}

function lcd(main, sub, frac) {
  STATE.lcdMode = "default";
  $("lcd-copy").classList.remove("activity");
  renderLcd(main, sub, frac);
}

function hasActivePlayback() {
  return Boolean(currentTrack() && !isPlaybackPaused());
}

function showPlaybackLcd() {
  const track = currentTrack();
  if (!track) return;
  STATE.lcdMode = "playback";
  $("lcd-copy").classList.remove("activity");
  renderLcd(track.title, `${track.artist} — ${track.album}`);
  scrub(trackDuration() ? playbackPosition() / trackDuration() : 0, playbackPosition(), trackDuration());
}

// Long-running work temporarily occupies the LCD, but playback remains one
// click away. Subsequent progress updates respect that explicit choice.
function showActivity(main, sub, frac, start = false) {
  STATE.activity = { main, sub, frac };
  if (!hasActivePlayback()) { lcd(main, sub, frac); return; }
  if (start) STATE.lcdMode = "activity";
  if (STATE.lcdMode === "playback") return;
  STATE.lcdMode = "activity";
  $("lcd-copy").classList.add("activity");
  renderLcd(main, sub, frac);
}

function finishActivity(main = "iAmped", sub = "") {
  STATE.activity = null;
  if (hasActivePlayback()) { showPlaybackLcd(); return; }
  lcd(main, sub);
}

$("lcd-copy").onclick = () => {
  if (STATE.lcdMode === "activity" && hasActivePlayback()) showPlaybackLcd();
};
$("lcd-copy").onkeydown = (e) => {
  if ((e.key === "Enter" || e.key === " ") && STATE.lcdMode === "activity" && hasActivePlayback()) {
    e.preventDefault();
    showPlaybackLcd();
  }
};

// `scrub()` is the playback face of the same bar: a real iTunes-style seek bar
// flanked by elapsed / remaining time.
function scrub(frac, elapsed, dur) {
  if (STATE.lcdMode === "activity") return;
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
  document.querySelectorAll(".src-item, .src-subitem").forEach((s) => s.classList.remove("selected"));
  if (srcEl) srcEl.classList.add("selected");
  if (paneId === "pane-browse") bottomBrowse();
  else { $("bottom-info").classList.remove("hidden"); $("capacity").classList.add("hidden"); $("cap-legend").classList.add("hidden"); $("sync-actions").classList.add("hidden"); $("bottom-info").textContent = "—"; }
  if (paneId === "pane-video") openVideo();
}
document.querySelectorAll(".src-item[data-pane]").forEach((el) => { el.onclick = () => selectPane(el.dataset.pane, el); });

// ---------------------------------------------------------------- config / connect
function setSourceKind(kind) {
  const names = { plex: "Plex", jellyfin: "Jellyfin", kodi: "Kodi" };
  kind = names[kind] ? kind : "plex";
  const isPlex = kind === "plex";
  const isJellyfin = kind === "jellyfin";
  const isKodi = kind === "kodi";
  $("source-kind").value = kind;
  $("server-heading").textContent = `Media Server — ${names[kind]}`;
  $("server-lead").textContent = isPlex
    ? "iAmped works with Plex, Jellyfin, and Kodi. Connect to Plex directly — Plexamp is only a client."
    : isJellyfin
      ? "iAmped works with Plex, Jellyfin, and Kodi. Connect directly to your Jellyfin music library for device sync."
      : "iAmped works with Plex, Jellyfin, and Kodi. Connect to Kodi's JSON-RPC music library; it must expose original music files for sync.";
  $("plex-oauth-box").classList.toggle("hidden", !isPlex);
  $("login-row").classList.toggle("hidden", !(isJellyfin || isKodi));
  $("token-field").classList.toggle("hidden", isKodi);
  $("token-label").textContent = isJellyfin ? "Access token (optional with login)" : "Plex token";
  $("token").placeholder = isJellyfin ? "Jellyfin access token" : "X-Plex-Token";
  $("username-label").textContent = isKodi ? "Kodi web-server username" : "Jellyfin username";
  $("manual-label").textContent = isPlex ? "Or connect manually (optional)" : "Connection details";
}

function renderSources(cfg) {
  STATE.sources = cfg.sources || [];
  STATE.activeSourceId = cfg.active_source_id || "";
  const select = $("source-select");
  select.innerHTML = '<option value="">Add a server…</option>' + STATE.sources.map((source) =>
    `<option value="${esc(source.id)}">${esc(source.name || source.kind)} (${esc(source.kind)})</option>`).join("");
  select.value = STATE.activeSourceId || "";
  const active = STATE.sources.find((source) => source.id === STATE.activeSourceId);
  if (active) {
    $("source-kind").value = active.kind;
    $("source-name").value = active.name || "";
    $("baseurl").value = active.baseurl || "";
    $("token").value = ""; // tokens are deliberately never exposed by /api/config
    $("source-username").value = "";
    $("source-password").value = "";
    STATE.sourceCapabilities = active.capabilities || {};
    setSourceKind(active.kind);
  } else {
    STATE.sourceCapabilities = {};
    setSourceKind($("source-kind").value);
  }
}

async function loadConfig() {
  const cfg = await api("/api/config");
  renderSources(cfg);
  $("device-path").value = cfg.last_device_path || ""; $("reserve").value = reserveDefaultMb(cfg.reserve_mb);
  STATE.fillStrategy = cfg.fill_strategy || "most_played";
  $("transcode").checked = cfg.transcode_lossless !== false;
  STATE.bitrateByFormat.aac = Number(cfg.aac_bitrate_k) || 256;
  STATE.bitrateByFormat.mp3 = Number(cfg.mp3_bitrate_k) || 320;
  STATE.nativePlaybackAvailable = !!cfg.native_playback?.available;
  $("mirror").checked = cfg.mirror !== false;
  STATE.sdEnabled = !!cfg.sd_card_enabled;
  updateSdMenuState();
  STATE.autosyncEnabled = cfg.autosync_enabled !== false;
  updateAutosyncMenuState();
  if ($("ingest-dir")) {
    $("ingest-dir").value = cfg.ingest_dir || "";
    INGEST_SECTION = cfg.ingest_section || "";
    loadIngestFolders();               // populate the Plex-folder dropdown
    if (cfg.ingest_dir) validateIngest();  // show current folder's status
  }
  for (const r of document.getElementsByName("dtype")) r.checked = r.value === (cfg.last_device_type || "massstorage");
  if ($("ipod-format")) $("ipod-format").value = cfg.ipod_format || "aac";
  applyAacQualityNote(cfg);
  toggleDeviceType();   // builds the bitrate slider — needs ipod-format set first
  if (!cfg.has_ffmpeg) { $("ffmpeg-note").textContent = "(ffmpeg not found — lossless copied as-is, FLAC won't play in-app)"; $("transcode").checked = false; }
  applyCacheSettings(cfg);
  // The cached library is browsable offline; only build/play/sync need Plex.
  let st = { tracks: 0 };
  try { st = await api("/api/library/stats"); await refreshStats(); await loadSidebar(); } catch (e) {}
  const active = STATE.sources.find((source) => source.id === STATE.activeSourceId);
  if (active?.configured) activateSource(active.id, true);
  if (st.tracks > 0) openLibrary();
}

// ---- AAC encoder quality + cache settings -------------------------------
function applyAacQualityNote(cfg) {
  const note = $("aac-quality-note");
  if (!note) return;
  if (cfg.aac_apple_grade) {
    note.textContent = `(clean AAC via ${cfg.aac_encoder})`;
    note.classList.remove("warn");
  } else {
    note.textContent = "⚠ your ffmpeg has only the native AAC encoder, which can "
      + "stutter on older iPods — install a build with libfdk_aac, or choose ALAC / MP3";
    note.classList.add("warn");
  }
}
function cacheStatusText(status) {
  if (!status) return "";
  const used = fmtBytes(status.bytes || 0);
  return status.aggressive
    ? `${used} cached — aggressive mode (no limit, kept on exit)`
    : `${used} cached — capped at ${fmtBytes((status.limit_mb || 0) * 1024 * 1024)}, cleared on exit`;
}
function applyCacheSettings(cfg) {
  if ($("cache-limit")) $("cache-limit").value = cfg.cache_limit_mb ?? 3072;
  if ($("aggressive-cache")) $("aggressive-cache").checked = !!cfg.aggressive_cache;
  if ($("cache-limit")) $("cache-limit").disabled = !!cfg.aggressive_cache;
  const note = $("cache-status-note");
  if (note) note.textContent = cacheStatusText(cfg.cache_status);
}
async function refreshCacheStatus() {
  try { applyCacheSettings(await api("/api/config")); } catch (_) {}
}
async function saveCacheSettings() {
  const limit = Math.max(0, Number($("cache-limit").value) || 0);
  const aggressive = $("aggressive-cache").checked;
  $("cache-limit").disabled = aggressive;
  try { await api("/api/config", "POST", { cache_limit_mb: limit, aggressive_cache: aggressive }); }
  catch (_) {}
  refreshCacheStatus();
}
async function saveIpodFormat() {
  try { await api("/api/config", "POST", { ipod_format: $("ipod-format").value }); }
  catch (_) {}
}
async function clearCacheNow() {
  const s = $("clear-cache-status"); if (s) s.textContent = "Clearing…";
  try {
    const r = await api("/api/cache/clear", "POST", {});
    if (s) s.textContent = `Freed ${fmtBytes(r.freed_bytes || 0)} (${r.removed || 0} files)`;
  } catch (e) { if (s) s.textContent = "Clear failed"; }
  refreshCacheStatus();
}
if ($("ipod-format")) $("ipod-format").onchange = saveIpodFormat;
if ($("cache-limit")) $("cache-limit").onchange = saveCacheSettings;
if ($("aggressive-cache")) $("aggressive-cache").onchange = saveCacheSettings;
if ($("btn-clear-cache")) $("btn-clear-cache").onclick = clearCacheNow;

async function connect() {
  const s = $("connect-status"); s.className = "status"; s.textContent = "Connecting…";
  const kind = $("source-kind").value;
  lcd("Connecting…", `Reaching ${kind === "jellyfin" ? "Jellyfin" : kind === "kodi" ? "Kodi" : "Plex"}`);
  try {
    const res = await api("/api/sources/connect", "POST", {
      id: $("source-select").value || undefined,
      kind, name: $("source-name").value.trim(),
      baseurl: $("baseurl").value.trim(), token: $("token").value.trim(),
      username: $("source-username").value.trim(), password: $("source-password").value,
    });
    if (!res.ok) throw new Error(res.error);
    await showConnected(res);
    const cfg = await api("/api/config"); renderSources(cfg);
  } catch (e) { s.className = "status err"; s.textContent = `Could not connect: ${e.message}`; lcd("iAmped", "Not connected"); }
}

function showConnected(res) {
  const s = $("connect-status");
  s.className = "status ok";
  const name = res.provider === "jellyfin" ? "Jellyfin" : res.provider === "kodi" ? "Kodi" : "Plex";
  s.textContent = `Connected to ${res.server.name}${res.server.version ? ` (${name} ${res.server.version})` : ""}.`;
  if (STATE.playing < 0) lcd("iAmped", `Connected to ${res.server.name}`);
  $("section").innerHTML = res.sections.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("");
  $("section-wrap").classList.toggle("hidden", res.sections.length === 0);
  STATE.sourceCapabilities = res.capabilities || {};
  $("btn-sonic").classList.toggle("hidden", !STATE.sourceCapabilities.sonic_radio);
  $("btn-radio").classList.toggle("hidden", !STATE.sourceCapabilities.sonic_radio);
  $("src-video").classList.toggle("hidden", !STATE.sourceCapabilities.video);
  $("btn-build").disabled = false;
  return Promise.all([refreshStats(), loadSidebar()]);
}

async function activateSource(sourceId, quiet = false) {
  if (!sourceId) return;
  try {
    const res = await api("/api/sources/active", "POST", { source_id: sourceId });
    if (!res.ok) throw new Error(res.error || "Could not activate the media source.");
    await showConnected(res);
    STATE.activeSourceId = sourceId;
  } catch (e) {
    if (!quiet) { const s = $("connect-status"); s.className = "status err"; s.textContent = e.message; }
  }
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
    renderSources(await api("/api/config"));
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

async function buildLibrary(mode = "auto") {
  const s = $("build-status"); $("btn-build").disabled = true; s.className = "status"; s.textContent = "Starting…";
  try {
    const { job } = await api("/api/library/build", "POST",
      { section: $("section").value || undefined, mode });
    showActivity("Building library", "Preparing…", 0, true);
    pollJob(job, {
      onProgress: (j) => {
        const count = j.total
          ? `${Number(j.done || 0).toLocaleString()} of ${Number(j.total).toLocaleString()}`
          : Number(j.done || 0).toLocaleString();
        s.textContent = j.total ? `${j.message || j.phase} (${count})` : (j.message || j.phase);
        showActivity("Building library", j.total ? count : (j.message || j.phase), j.total ? j.done / j.total : null);
      },
      onDone: async (j) => {
        STATE.sourceCapabilities = j.result?.capabilities || STATE.sourceCapabilities;
        if (STATE.sourceCapabilities.original_download === false) {
          s.className = "status err";
          s.textContent = "Library cached, but Kodi did not expose downloadable originals. Enable Files.Download or mount the music share.";
        } else {
          s.className = "status ok"; s.textContent = j.message;
        }
        finishActivity("iAmped", j.message); $("btn-build").disabled = false;
        await refreshStats(); await loadSidebar(); openLibrary();
      },
      onError: (e) => { s.className = "status err"; s.textContent = e; finishActivity("iAmped", "Build failed"); $("btn-build").disabled = false; },
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
  if (isTruthy(d.is_sd)) return "sd";
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
  if (kind === "sd") {
    // A stylised SD card: notched top-left corner and gold contact pins.
    const b = bodyBox(.72, 44, 74);
    const notch = rr(b.w * .28);
    const pinW = b.w * .1, pinY = rr(b.y + b.h * .12), pinH = rr(b.h * .16);
    const pins = [0, 1, 2, 3, 4].map((i) =>
      `<rect x="${rr(b.x + b.w * .16 + i * pinW * 1.35)}" y="${pinY}" width="${rr(pinW)}" height="${pinH}" rx="1" fill="#d9b25a"/>`).join("");
    const d = `M${rr(b.x + Number(notch))} ${rr(b.y)}`
      + ` H${rr(b.x + b.w - 4)} q4 0 4 4`
      + ` V${rr(b.y + b.h - 4)} q0 4 -4 4`
      + ` H${rr(b.x + 4)} q-4 0 -4 -4`
      + ` V${rr(b.y + Number(notch))} Z`;
    return wrap(iconSvg(
      `<path d="${d}" fill="#37506b" stroke="#243247" stroke-width="1"/>
      ${pins}
      <rect x="${rr(b.x + b.w * .14)}" y="${rr(b.y + b.h * .42)}" width="${rr(b.w * .72)}" height="${rr(b.h * .34)}" rx="2" fill="#e9eef2"/>`));
  }
  return wrap(iconSvg(`<rect x="8" y="31" width="48" height="25" rx="4" fill="#d5d8dc" stroke="#6f7780"/>
    <rect x="15" y="37" width="22" height="8" rx="2" fill="#8fb0be"/><circle cx="47" cy="43.5" r="5" fill="#f1f1ee" stroke="#8d9398"/>`));
}

async function loadSidebar() {
  const [playlists, facets] = await Promise.all([
    api("/api/playlists"),
    api("/api/library/facets").catch(() => STATE.facets),
  ]);
  STATE.playlists = playlists;
  STATE.facets = facets || STATE.facets;
  const list = $("playlist-list");
  if (!STATE.playlists.length) { list.innerHTML = '<div class="src-item disabled">No playlists yet</div>'; }
  else {
    list.innerHTML = STATE.playlists.map((p) => `
      <div class="src-item playlist" data-id="${esc(p.id)}" data-title="${esc(p.title)}" data-source="${p.source}">
        ${p.source === "local" && p.kind === "sonic" ? sonicSvg : plSvg}${esc(p.title)}</div>`).join("");
    list.querySelectorAll(".playlist").forEach((el) => {
      el.onclick = () => openPlaylist(el);
      el.oncontextmenu = (event) => showPlaylistContextMenu(event, el);
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

function deviceSignature(vols) {
  return vols.map((v) => `${v.path}|${v.is_ipod}|${v.free}`).sort().join("§");
}
let DEVICE_POLL = { sig: null, paths: new Set() };
// Poll the cheap /api/volumes hot path so plugging a device in refreshes the
// sidebar on its own — no more toggling SD-card mode to make it "look". Only
// re-renders when the device set actually changes, so it never disturbs hover
// or selection during steady state. (Backend autosync.py separately watches for
// connect and fires sync-on-connect for devices with an autosync profile.)
async function pollDevices() {
  let vols;
  try { vols = await api("/api/volumes" + (STATE.sdEnabled ? "?sd=1" : "")); }
  catch (_) { return; }
  const sig = deviceSignature(vols);
  if (sig === DEVICE_POLL.sig) return;
  const firstRun = DEVICE_POLL.sig === null;
  const added = vols.filter((v) => !DEVICE_POLL.paths.has(v.path));
  DEVICE_POLL = { sig, paths: new Set(vols.map((v) => v.path)) };
  renderDevices(vols);
  if (!firstRun && added.length) {
    const v = added[0];
    lcd("Device connected", v.ipod_model || v.name || v.path);
  }
}
async function loadDevices() {
  const vols = await api("/api/volumes" + (STATE.sdEnabled ? "?sd=1" : ""));
  DEVICE_POLL = { sig: deviceSignature(vols), paths: new Set(vols.map((v) => v.path)) };
  renderDevices(vols);
}
function renderDevices(vols) {
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
  if (on) { $("view-toggle").classList.add("hidden"); $("album-hero").classList.add("hidden"); }
}
function _setFacetMode(on) {
  $("browse-head-row").style.display = on ? "none" : "";
  const acts = $("pane-browse").querySelector(".browse-actions");
  if (acts) acts.style.display = on ? "none" : "";
  // The list/grid toggle belongs to facet index views; renderFacetList hides it
  // again for genres (list-only). The album hero never shows over a facet index.
  $("view-toggle").classList.toggle("hidden", !on);
  if (on) $("album-hero").classList.add("hidden");
}

async function openDeviceVideos(el) {
  const path = el.dataset.path, ipod = el.dataset.ipod === "true";
  DEVVID.path = path; DEVVID.ipod = ipod; DEVVID.name = el.dataset.name;
  STATE.view = { type: "device-videos", title: el.dataset.name, path };
  $("browse-title").textContent = `${el.dataset.name} — Videos`;
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
function facetTitle(kind) {
  return { artists: "Artists", albums: "Albums", genres: "Genres" }[kind] || "Music";
}
function facetParam(kind) {
  return { artists: "artist", albums: "album", genres: "genre" }[kind] || "";
}
function facetSrc(kind) {
  return { artists: $("src-artists"), albums: $("src-albums"), genres: $("src-genres") }[kind] || $("src-music");
}
function facetViewMode() {
  if (!STATE.facetView) STATE.facetView = localStorage.getItem("iamped.facetView") || "grid";
  return STATE.facetView;
}
// Small 28px art tile for the facet list: cached thumb, or a generated fallback.
function facetArtHtml(item) {
  if (item.album_thumb)
    return `<div class="facet-art" style="background-image:url('${artworkUrl(item.album_thumb)}')"></div>`;
  const ch = String(item.name || "").trim()[0];
  return `<div class="facet-art art-fallback" style="--h:${artHue(item.name)}"><span>${ch ? esc(ch.toUpperCase()) : "♪"}</span></div>`;
}
function renderFacetList(kind) {
  _setBrowseVideoMode(false);
  _setFacetMode(true);
  const all = STATE.facets[kind] || [];
  // Search filters the whole cached facet list, not just what's scrolled into
  // view — type "eels" and it surfaces even if you never scrolled to it.
  const q = ($("search").value || "").trim().toLowerCase();
  const items = q
    ? all.filter((it) => (it.name || "").toLowerCase().includes(q)
        || (it.artist || "").toLowerCase().includes(q))
    : all;
  const body = $("browse-body");
  // Genres are list-only; albums/artists honour the user's list/grid choice.
  const grid = kind !== "genres" && facetViewMode() === "grid";
  const toggle = $("view-toggle");
  toggle.classList.toggle("hidden", kind === "genres");
  toggle.querySelectorAll("button").forEach((b) =>
    b.classList.toggle("active", b.dataset.vm === (grid ? "grid" : "list")));

  if (!items.length) {
    body.innerHTML = q
      ? `<div class="muted" style="padding:14px">No ${facetTitle(kind).toLowerCase()} match “${esc(q)}”.</div>`
      : `<div class="muted" style="padding:14px">No ${facetTitle(kind).toLowerCase()} cached.</div>`;
  } else if (grid) {
    const artist = kind === "artists";
    body.innerHTML = `<div class="albumgrid">` + items.map((item, i) => {
      const cover = item.album_thumb
        ? `<img class="tile-art" loading="lazy" src="${artworkUrl(item.album_thumb)}" alt="">`
        : artFallback(item.name);
      const sub = artist ? plural(item.count, "song") : (item.artist || plural(item.count, "song"));
      return `<button class="albumtile${artist ? " artist" : ""}" data-i="${i}">
        <span class="tile-cover">${cover}</span>
        <span class="tile-name">${esc(item.name)}</span>
        <span class="tile-sub">${esc(sub)}</span>
      </button>`;
    }).join("") + `</div>`;
    body.querySelectorAll(".albumtile").forEach((tile) => {
      tile.onclick = () => openFacetTracks(kind, items[Number(tile.dataset.i)]);
    });
  } else {
    body.innerHTML = items.map((item, i) => {
      const subtitle = kind === "albums"
        ? [item.artist, item.year || ""].filter(Boolean).join(" · ")
        : plural(item.count, "song");
      return `<div class="facetrow" data-i="${i}">
        ${facetArtHtml(item)}
        <div><div class="facet-name">${esc(item.name)}</div><div class="facet-sub">${esc(subtitle)}</div></div>
        <div class="facet-count">${plural(item.count, "song")}</div>
      </div>`;
    }).join("");
    body.querySelectorAll(".facetrow").forEach((row) => {
      row.onclick = () => openFacetTracks(kind, items[Number(row.dataset.i)]);
    });
  }
  $("bottom-info").classList.remove("hidden");
  $("bottom-info").textContent = `${plural(items.length, facetTitle(kind).slice(0, -1).toLowerCase())}`;
}
function openFacetIndex(kind) {
  STATE.view = { type: "facet", kind, title: facetTitle(kind) };
  STATE.selected.clear(); STATE.tracks = [];
  $("browse-title").textContent = facetTitle(kind);
  selectPane("pane-browse", facetSrc(kind));
  renderFacetList(kind);
}
function openFacetTracks(kind, item) {
  const param = facetParam(kind);
  STATE.view = {
    type: "library", title: item.name,
    facetKind: kind, facetParam: param, facetValue: item.name,
    facetExtra: kind === "albums" && item.artist ? { album_artist: item.artist } : null,
    hero: kind === "albums" ? { name: item.name, artist: item.artist, thumb: item.album_thumb } : null,
  };
  STATE.search = ""; $("search").value = ""; STATE.offset = 0; STATE.total = 0;
  $("browse-title").textContent = item.name;
  selectPane("pane-browse", facetSrc(kind));
  STATE.tracks = []; $("browse-body").innerHTML = "";
  loadMore(true);
}
function openLibrary() {
  STATE.view = { type: "library", title: "Music" };
  STATE.search = $("search").value.trim(); STATE.offset = 0; STATE.total = 0;
  $("browse-title").textContent = "Music";
  selectPane("pane-browse", $("src-music"));
  STATE.tracks = []; $("browse-body").innerHTML = "";
  loadMore(true);
}
async function loadMore(reset) {
  if (STATE.view?.type !== "library") return;
  if (!reset && STATE.tracks.length >= STATE.total && STATE.total) return;
  // A new query must win over an in-flight page from the previous query.  This
  // is especially visible with quick typing on a large cached library.
  if (reset) {
    STATE.browseAbort?.abort();
    STATE.loading = false;
    STATE.browseRequest += 1;
  }
  if (STATE.loading) return;
  const requestId = STATE.browseRequest || ++STATE.browseRequest;
  const controller = new AbortController(); STATE.browseAbort = controller;
  let fillMore = false;
  STATE.loading = true;
  const params = new URLSearchParams({
    search: STATE.search, sort: STATE.sort,
    offset: String(STATE.offset), limit: "300",
  });
  if (STATE.view?.facetParam && STATE.view?.facetValue) params.set(STATE.view.facetParam, STATE.view.facetValue);
  if (STATE.view?.facetExtra) {
    Object.entries(STATE.view.facetExtra).forEach(([key, value]) => params.set(key, value));
  }
  try {
    const r = await api(`/api/tracks?${params.toString()}`, "GET", null, controller.signal);
    if (requestId !== STATE.browseRequest) return;
    STATE.total = r.total; STATE.offset += r.tracks.length;
    const start = reset ? 0 : STATE.tracks.length;
    STATE.tracks = reset ? r.tracks : STATE.tracks.concat(r.tracks);
    if (reset) renderTracks(); else appendTracks(r.tracks, start);
    // If the first page didn't fill the viewport, keep pulling so the user can
    // always scroll to reach the next page.
    const body = $("browse-body");
    fillMore = STATE.tracks.length < STATE.total && body.scrollHeight <= body.clientHeight;
  } catch (e) {
    if (e.name !== "AbortError") throw e;
  } finally {
    if (requestId === STATE.browseRequest) {
      STATE.loading = false;
      if (STATE.browseAbort === controller) STATE.browseAbort = null;
      if (fillMore) loadMore(false);
    }
  }
}
async function openPlaylist(el) {
  const id = el.dataset.id;
  STATE.view = { type: "playlist", id, title: el.dataset.title, source: el.dataset.source };
  $("browse-title").textContent = el.dataset.title;
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
      <div class="c-rating">${ratingStarsHtml(t.rating_key, t.rating)}</div><div class="c-plays">${t.plays || 0}</div>
      <div class="c-time">${fmtDur(t.duration_ms)}</div>
    </div>`;
}
function bindRows(rows) {
  rows.forEach((row) => {
    const idx = Number(row.dataset.idx);
    row.onclick = (e) => {
      const star = e.target.closest(".row-star");
      if (star) { e.stopPropagation(); rateTrackByKey(idx, Number(star.dataset.star) * 2); return; }
      if (e.shiftKey) selectRange(idx, e.metaKey || e.ctrlKey);
      else selectRow(idx, e.metaKey || e.ctrlKey);
    };
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
function renderAlbumHero(hero) {
  const el = $("album-hero");
  if (!hero) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const art = $("hero-art");
  if (hero.thumb) { art.style.backgroundImage = `url("${artworkUrl(hero.thumb)}")`; art.innerHTML = ""; }
  else { art.style.backgroundImage = ""; art.innerHTML = artFallback(hero.name); }
  $("hero-name").textContent = hero.name || "Album";
  $("hero-artist").textContent = hero.artist || "";
  const count = STATE.total || STATE.tracks.length;
  const totalMs = STATE.tracks.reduce((s, t) => s + (Number(t.duration_ms) || 0), 0);
  $("hero-meta").textContent =
    [plural(count, "song"), totalMs ? fmtDur(totalMs) : ""].filter(Boolean).join(" · ");
}
function renderTracks() {
  _setBrowseVideoMode(false);            // restore music chrome if we left video mode
  _setFacetMode(false);
  renderAlbumHero(STATE.view?.hero);
  const body = $("browse-body");
  STATE.selAnchor = null;                // list rebuilt — old anchor is meaningless
  body.innerHTML = STATE.tracks.map((t, i) => rowHtml(t, i)).join("");
  bindRows([...body.querySelectorAll(".songrow")]);
  updateDeviceMusicActions();
  bottomBrowse();
  if (($("search").value || "").trim()
      && (STATE.view?.type === "device" || STATE.view?.type === "playlist"))
    filterVisibleRows();
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
function _paintSelection() {
  document.querySelectorAll("#browse-body .songrow").forEach(
    (r) => r.classList.toggle("selected", STATE.selected.has(r.dataset.rk)));
  if (STATE.view?.type === "device") updateDeviceMusicActions();
}
function selectRow(idx, additive) {
  const rk = STATE.tracks[idx].rating_key;
  if (!additive) STATE.selected.clear();
  if (STATE.selected.has(rk) && additive) STATE.selected.delete(rk); else STATE.selected.add(rk);
  STATE.selAnchor = idx;              // shift-click ranges anchor on the last click
  _paintSelection();
}
// Shift-click: select the contiguous range from the anchor to idx. Without a
// modifier this replaces the selection (list convention); with cmd/ctrl it adds
// the range to what's already selected.
function selectRange(idx, additive) {
  const anchor = Number.isInteger(STATE.selAnchor) ? STATE.selAnchor : idx;
  if (!additive) STATE.selected.clear();
  const [lo, hi] = anchor <= idx ? [anchor, idx] : [idx, anchor];
  for (let i = lo; i <= hi; i++) {
    const t = STATE.tracks[i];
    if (t) STATE.selected.add(t.rating_key);
  }
  _paintSelection();
}
function selectAllTracks() {
  STATE.tracks.forEach((t) => STATE.selected.add(t.rating_key));
  STATE.selAnchor = 0;
  _paintSelection();
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
// Section tied to the folder the user picked from the dropdown; empty for a
// hand-typed custom path (the backend then falls back to the music section).
let INGEST_SECTION = "";

function paintIngestHint(status) {
  const hint = $("ingest-dir-hint");
  if (!hint) return null;
  hint.textContent = status.message || "";
  hint.style.color = status.ok ? "#1a7f37"
    : status.writable ? "#9a6700"   // reachable but Plex may not watch it
    : status.path ? "#b42318" : "";
  return status;
}

// Ask the backend whether the current path is reachable/writable here and
// inside a folder Plex watches. Debounced so typing doesn't spam the server.
let _ingestValidateTimer = null;
function validateIngestDebounced() {
  clearTimeout(_ingestValidateTimer);
  _ingestValidateTimer = setTimeout(validateIngest, 350);
}
async function validateIngest() {
  const dir = $("ingest-dir").value.trim();
  if (!dir) return paintIngestHint({ path: "", ok: false, writable: false, message: "" });
  try {
    return paintIngestHint(await api("/api/ingest/validate", "POST", { ingest_dir: dir }));
  } catch (e) { return null; }
}

// Populate the dropdown from the folders Plex actually watches, flagging any
// this machine can't reach or write to (usually a mount-path mismatch).
async function loadIngestFolders() {
  const sel = $("ingest-folder-select");
  if (!sel) return;
  let r;
  try { r = await api("/api/plex/music/locations"); }
  catch (e) { sel.style.display = "none"; return; }
  if (!r.supported) { sel.style.display = "none"; return; }  // e.g. active source isn't Plex
  sel.style.display = "";
  const current = ($("ingest-dir").value || r.ingest_dir || "").trim();
  const opts = ['<option value="">— pick a Plex library folder —</option>'];
  for (const s of r.sections || []) {
    for (const f of s.folders || []) {
      const warn = f.writable ? "" : (f.exists ? "  (not writable here)" : "  (not reachable here)");
      opts.push(`<option value="${esc(f.path)}" data-section="${esc(s.title)}"`
        + (f.path === current ? " selected" : "") + ">"
        + `${esc(s.title)} — ${esc(f.path)}${esc(warn)}</option>`);
    }
  }
  opts.push('<option value="__custom__">Custom path…</option>');
  sel.innerHTML = opts.join("");
}

function onIngestFolderPick() {
  const sel = $("ingest-folder-select");
  const opt = sel.options[sel.selectedIndex];
  if (!opt || opt.value === "__custom__") { INGEST_SECTION = ""; $("ingest-dir").focus(); return; }
  if (!opt.value) { INGEST_SECTION = ""; return; }
  $("ingest-dir").value = opt.value;
  INGEST_SECTION = opt.dataset.section || "";
  validateIngest();
}

async function saveIngestDir() {
  const dir = $("ingest-dir").value.trim();
  try {
    const status = await validateIngest();  // also repaints the hint
    // Derive the section to rescan: the dropdown pick, or whatever Plex library
    // the validator found this path inside; else leave blank (backend falls
    // back to the configured music section).
    const section = INGEST_SECTION || (status && status.watched_by) || "";
    await api("/api/config", "POST", { ingest_dir: dir, ingest_section: section });
    const hint = $("ingest-dir-hint");
    if (status && status.ok) hint.textContent = "Saved — " + status.message;
    else if (status) hint.textContent = "Saved, but heads up: " + status.message;
    else hint.textContent = "Saved.";
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
// The full song list stays paged, but this small grouped result set is drawn
// from the local cache.  It makes search feel immediate without making a Plex
// request for every keypress.
let searchTimer, suggestionTimer, suggestionAbort, suggestionRequest = 0;
let searchResultItems = [], searchResultIndex = -1;
function hideSearchResults() {
  searchResultItems = []; searchResultIndex = -1;
  $("search-results").classList.add("hidden");
  $("search").setAttribute("aria-expanded", "false");
}
function renderSearchResults(results) {
  const groups = [
    { key: "artists", label: "Artists", icon: "●",
      sub: (x) => plural(x.count, "song") },
    { key: "albums", label: "Albums", icon: "▣",
      sub: (x) => [x.artist, x.year].filter(Boolean).join(" · ") || plural(x.count, "song") },
    { key: "tracks", label: "Songs", icon: "♪",
      sub: (x) => [x.artist, x.album].filter(Boolean).join(" · ") },
    { key: "genres", label: "Genres", icon: "#",
      sub: (x) => plural(x.count, "song") },
  ];
  searchResultItems = [];
  const html = groups.map((group) => {
    const items = results[group.key] || [];
    if (!items.length) return "";
    const rows = items.map((item) => {
      const index = searchResultItems.length;
      searchResultItems.push({ kind: group.key, item });
      return `<button class="search-result" type="button" role="option" aria-selected="false" data-i="${index}">
        <span class="search-result-icon">${group.icon}</span><span><div class="search-result-main">${esc(item.name || item.title)}</div>
        <div class="search-result-sub">${esc(group.sub(item))}</div></span></button>`;
    }).join("");
    return `<div class="search-group-label">${group.label}</div>${rows}`;
  }).join("");
  if (!searchResultItems.length) return hideSearchResults();
  const box = $("search-results");
  box.innerHTML = html;
  box.querySelectorAll(".search-result").forEach((row) => {
    row.onclick = () => chooseSearchResult(Number(row.dataset.i));
  });
  box.classList.remove("hidden");
  $("search").setAttribute("aria-expanded", "true");
}
async function updateSearchSuggestions() {
  const q = $("search").value.trim();
  if (!q) return hideSearchResults();
  suggestionAbort?.abort();
  const controller = new AbortController(); suggestionAbort = controller;
  const requestId = ++suggestionRequest;
  try {
    const results = await api(`/api/library/search?q=${encodeURIComponent(q)}&limit=6`, "GET", null, controller.signal);
    if (requestId === suggestionRequest && $("search").value.trim() === q) renderSearchResults(results);
  } catch (e) {
    // Aborting an obsolete request is normal while typing.  Leave the last
    // useful result visible for real transient failures instead of flashing.
    if (e.name !== "AbortError" && requestId === suggestionRequest) hideSearchResults();
  }
}
function paintSearchResultSelection() {
  $("search-results").querySelectorAll(".search-result").forEach((row, index) => {
    const active = index === searchResultIndex;
    row.classList.toggle("active", active); row.setAttribute("aria-selected", String(active));
    if (active) row.scrollIntoView({ block: "nearest" });
  });
}
function chooseSearchResult(index) {
  const result = searchResultItems[index];
  if (!result) return;
  hideSearchResults();
  const { kind, item } = result;
  if (kind === "artists") return openFacetTracks("artists", item);
  if (kind === "albums") return openFacetTracks("albums", item);
  if (kind === "genres") return openFacetTracks("genres", item);
  // A song result opens its cached matches in the normal browser.  Keeping
  // playback explicit avoids replacing the current queue on an Enter key.
  $("search").value = item.title || "";
  selectPane("pane-browse", $("src-music"));
  STATE.view = { type: "library", title: item.title || "Music" };
  $("browse-title").textContent = STATE.view.title;
  STATE.search = $("search").value.trim(); STATE.offset = 0; STATE.total = 0;
  STATE.tracks = []; $("browse-body").innerHTML = "";
  loadMore(true);
}
$("search").oninput = () => {
  clearTimeout(searchTimer); clearTimeout(suggestionTimer);
  const q = $("search").value.trim();
  if (!q) hideSearchResults();
  else suggestionTimer = setTimeout(updateSearchSuggestions, 90);
  searchTimer = setTimeout(applySearch, 180);
};
$("search").onfocus = () => {
  if ($("search").value.trim()) updateSearchSuggestions();
};
$("search").onkeydown = (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    if (!searchResultItems.length) return;
    e.preventDefault();
    const delta = e.key === "ArrowDown" ? 1 : -1;
    searchResultIndex = (searchResultIndex + delta + searchResultItems.length) % searchResultItems.length;
    paintSearchResultSelection();
  } else if (e.key === "Enter" && searchResultIndex >= 0) {
    e.preventDefault(); chooseSearchResult(searchResultIndex);
  } else if (e.key === "Escape") {
    e.preventDefault(); hideSearchResults();
  }
};
document.addEventListener("click", (e) => {
  if (!e.target.closest(".search")) hideSearchResults();
});
// Route the search box to whatever the current view is showing. The library is
// searched server-side (it's paged); everything else is already fully loaded in
// memory, so we filter it client-side rather than only the rows in view.
function applySearch() {
  const type = STATE.view?.type;
  if (type === "library") {
    STATE.search = $("search").value.trim();
    STATE.offset = 0; STATE.tracks = []; loadMore(true);
  } else if (type === "facet") {
    renderFacetList(STATE.view.kind);
  } else if (type === "device" || type === "playlist") {
    filterVisibleRows();
  }
}
// Hide track rows that don't match the search term, keeping row indices intact
// so selection/playback still line up with STATE.tracks.
function filterVisibleRows() {
  const q = ($("search").value || "").trim().toLowerCase();
  $("browse-body").querySelectorAll(".songrow").forEach((row) => {
    const t = STATE.tracks[Number(row.dataset.idx)];
    const hit = !q || !t || [t.title, t.artist, t.album]
      .some((x) => (x || "").toLowerCase().includes(q));
    row.classList.toggle("search-hidden", !hit);
  });
}

// ---------------------------------------------------------------- context menu
const ctxMenu = document.createElement("div");
ctxMenu.className = "ctx hidden"; document.body.appendChild(ctxMenu);
function hideCtxMenu() { ctxMenu.classList.add("hidden"); }
document.addEventListener("click", hideCtxMenu);
document.addEventListener("scroll", hideCtxMenu, true);
window.addEventListener("blur", hideCtxMenu);

function showContextItems(e, items) {
  ctxMenu.innerHTML = items.map((it, i) => it.sep
    ? '<div class="ctx-sep"></div>'
    : `<div class="ctx-item${it.danger ? " danger" : ""}" data-i="${i}">${esc(it.label)}</div>`).join("");
  ctxMenu.querySelectorAll(".ctx-item").forEach((el) => {
    el.onclick = (ev) => { ev.stopPropagation(); hideCtxMenu(); items[Number(el.dataset.i)].fn(); };
  });
  ctxMenu.classList.remove("hidden");
  const w = ctxMenu.offsetWidth, h = ctxMenu.offsetHeight;
  ctxMenu.style.left = `${Math.min(e.clientX, innerWidth - w - 6)}px`;
  ctxMenu.style.top = `${Math.min(e.clientY, innerHeight - h - 6)}px`;
}

function showPlaylistContextMenu(e, el) {
  e.preventDefault();
  if (el.dataset.source !== "local") return;
  const id = el.dataset.id, title = el.dataset.title || "Playlist";
  selectRowForSidebarItem(el);
  showContextItems(e, [
    { label: `Delete playlist "${title}"`, danger: true, fn: () => deleteLocalPlaylist(id, title) },
  ]);
}

function selectRowForSidebarItem(el) {
  document.querySelectorAll(".src-item, .src-subitem").forEach((s) => s.classList.remove("selected"));
  el.classList.add("selected");
}

function showCtxMenu(e, idx) {
  e.preventDefault();
  const t = STATE.tracks[idx];
  if (!STATE.selected.has(t.rating_key)) selectRow(idx, false);
  const onDevice = String(t.rating_key).startsWith("dev:");
  const items = [
    { label: "▶  Play", fn: () => playIndex(idx) },
  ];
  if (!onDevice) items.push({ label: `Start radio from “${t.title}”`, fn: () => songRadio(t) });
  if (STATE.view?.type === "device") {
    const n = _selectedDeviceTracks().length || 1;
    items.push({ sep: true });
    items.push({ label: `Ingest ${n} to Plex…`, fn: () => ingestSelectedDeviceMusic() });
    items.push({ label: `Remove ${n} from device`, danger: true, fn: () => removeSelectedDeviceMusic() });
  }
  items.push({ sep: true });
  if (t.artist) items.push({ label: `Show all by ${t.artist}`, fn: () => filterBy(t.artist) });
  if (t.album) items.push({ label: `Show album “${t.album}”`, fn: () => filterBy(t.album) });
  showContextItems(e, items);
}

// Filter the library down to a search term (artist or album) — iTunes-like
// "show me everything by this".
function filterBy(term) {
  selectPane("pane-browse", $("src-music"));
  STATE.view = { type: "library", title: term };
  $("browse-title").textContent = term;
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
player.crossOrigin = "anonymous";
function currentTrack() {
  return STATE.playing >= 0 ? STATE.tracks[STATE.playing] : null;
}
async function setCurrentTrackRating(rating) {
  const t = currentTrack();
  if (!t || isDeviceTrack(t)) return;
  try {
    const r = await api(`/api/track/${encodeURIComponent(t.rating_key)}/rating`, "POST", { rating });
    updateTrackRows(t.rating_key, { rating: r.rating });
    updateNowPlaying();
    lcd(t.title, `Rated ${stars(r.rating) || "unrated"}`);
  } catch (e) {
    lcd("Rating failed", e.message);
  }
}
// Rate any visible track straight from the list. Clicking the current rating
// clears it (iTunes behaviour). Device tracks aren't backed by Plex, so skip.
async function rateTrackByKey(idx, rating) {
  const t = STATE.tracks[idx];
  if (!t || isDeviceTrack(t)) return;
  if (Number(t.rating || 0) === rating) rating = 0;
  try {
    const r = await api(`/api/track/${encodeURIComponent(t.rating_key)}/rating`, "POST", { rating });
    updateTrackRows(t.rating_key, { rating: r.rating });
    if (currentTrack()?.rating_key === t.rating_key) updateNowPlaying();
    lcd(t.title, `Rated ${stars(r.rating) || "unrated"}`);
  } catch (e) {
    lcd("Rating failed", e.message);
  }
}
function updateMediaSession(t) {
  if (!("mediaSession" in navigator) || typeof MediaMetadata === "undefined") return;
  const artwork = t?.album_thumb ? [{ src: artworkUrl(t.album_thumb), sizes: "512x512", type: "image/jpeg" }] : [];
  navigator.mediaSession.metadata = new MediaMetadata({
    title: t?.title || "iAmped",
    artist: t?.artist || "",
    album: t?.album || "",
    artwork,
  });
  navigator.mediaSession.playbackState = t && !isPlaybackPaused() ? "playing" : "paused";
}
function maybeScrobbleCurrentTrack() {
  const t = currentTrack();
  if (!t || isDeviceTrack(t) || STATE.scrobble.posted || isPlaybackPaused()) return;
  const position = playbackPosition();
  const duration = trackDuration();
  const threshold = duration ? Math.min(240, Math.max(30, duration * 0.5)) : 30;
  if (position < threshold) return;
  STATE.scrobble.posted = true;
  api(`/api/track/${encodeURIComponent(t.rating_key)}/scrobble`, "POST", {}).then(() => {
    updateTrackRows(t.rating_key, { plays: (Number(t.plays) || 0) + 1 });
    updateNowPlaying();
  }).catch(() => {});
}
function trackDuration() {
  const metadataDuration = Number(currentTrack()?.duration_ms || 0) / 1000;
  if (metadataDuration > 0) return metadataDuration;
  if (STATE.nativePlaybackActive && STATE.nativePlaybackDuration > 0) return STATE.nativePlaybackDuration;
  return Number.isFinite(player.duration) && player.duration > 0 ? player.duration : 0;
}
function playbackPosition() {
  if (STATE.nativePlaybackActive) {
    return Math.min(trackDuration() || Infinity, STATE.nativePlaybackPosition || 0);
  }
  return Math.min(trackDuration() || Infinity, STATE.playbackOffset + (player.currentTime || 0));
}
function updatePlaybackDisplay(position = playbackPosition()) {
  const duration = trackDuration();
  scrub(duration ? position / duration : 0, position, duration);
  maybeScrobbleCurrentTrack();
  updateMediaPositionState();
}
function deviceStreamUrl(t) {
  // Tracks that live on the connected device aren't in Plex — stream the file
  // straight off the mount instead of through the library resolver.
  const path = STATE.deviceMusic?.path || "";
  return `/api/device/stream?device_path=${encodeURIComponent(path)}`
    + `&location=${encodeURIComponent(t._location || "")}`;
}
function streamUrl(t, start = 0) {
  if (isDeviceTrack(t)) return deviceStreamUrl(t);
  const base = `/api/stream/${encodeURIComponent(t.rating_key)}`;
  return start > 0 ? `${base}?start=${encodeURIComponent(start.toFixed(3))}` : base;
}
function isPlaybackPaused() {
  return STATE.nativePlaybackActive ? STATE.nativePlaybackPaused : player.paused;
}
function stopNativePlaybackTimer() {
  if (STATE.nativePlaybackTimer) clearInterval(STATE.nativePlaybackTimer);
  STATE.nativePlaybackTimer = null;
}
async function pollNativePlayback() {
  if (!STATE.nativePlaybackActive) return;
  try {
    const status = await api("/api/playback/status");
    if (!status.available) {
      STATE.nativePlaybackAvailable = false;
      STATE.nativePlaybackActive = false;
      stopNativePlaybackTimer();
      setPlayIcon(false);
      return;
    }
    STATE.nativePlaybackPaused = status.state !== "playing";
    const pos = Number(status.position || 0);
    // Gapless auto-advance: mpv rolled into the queued next track (position
    // snapped back to ~0 from near the end) — move the UI along without
    // restarting playback.
    const prevMax = STATE.nativePlaybackLastPosition;
    const dur = trackDuration();
    if (status.state === "playing" && STATE.nativeNextIndex >= 0
        && prevMax > 5 && pos + 2 < prevMax && dur > 0 && dur - prevMax <= 10) {
      advanceUiToQueuedNext(pos);
      return;
    }
    STATE.nativePlaybackPosition = pos;
    STATE.nativePlaybackDuration = Number(status.duration || 0) || trackDuration();
    STATE.nativePlaybackLastPosition = Math.max(STATE.nativePlaybackLastPosition, STATE.nativePlaybackPosition);
    updatePlaybackDisplay();
    setPlayIcon(!STATE.nativePlaybackPaused);
    if (status.state === "stopped") {
      const duration = trackDuration();
      const finished = duration > 0 && duration - STATE.nativePlaybackLastPosition <= 5;
      STATE.nativePlaybackActive = false;
      stopNativePlaybackTimer();
      if (finished) playNext(true);
      else setPlayIcon(false);
    }
  } catch (_) {}
}
function startNativePlaybackTimer() {
  stopNativePlaybackTimer();
  STATE.nativePlaybackTimer = setInterval(pollNativePlayback, 500);
  pollNativePlayback();
}
async function stopNativePlayback() {
  if (!STATE.nativePlaybackActive) return;
  STATE.nativePlaybackActive = false;
  stopNativePlaybackTimer();
  try { await api("/api/playback/stop", "POST", {}); } catch (_) {}
}
async function startNativePlayback(t, start = 0) {
  try {
    player.pause();
    player.removeAttribute("src");
    player.load();
    const status = await api("/api/playback/play", "POST", { rating_key: t.rating_key, start });
    STATE.nativePlaybackActive = true;
    STATE.nativePlaybackPaused = false;
    STATE.nativePlaybackPosition = start;
    STATE.nativePlaybackDuration = Number(status.duration || 0) || trackDuration();
    STATE.nativePlaybackLastPosition = start;
    startNativePlaybackTimer();
    queueUpNext();
    return true;
  } catch (e) {
    STATE.nativePlaybackAvailable = false;
    STATE.nativePlaybackActive = false;
    stopNativePlaybackTimer();
    lcd("Native playback unavailable", "Falling back to browser playback");
    return false;
  }
}
async function queueUpNext() {
  // Hand mpv the up-next track so the transition is gapless (album playback
  // without the classic between-track hiccup). Safe no-op in browser playback.
  if (!STATE.nativePlaybackActive) return;
  const i = nextIndex(true);
  STATE.nativeNextIndex = i;
  if (i < 0) return;
  const t = STATE.tracks[i];
  if (!t || isDeviceTrack(t)) { STATE.nativeNextIndex = -1; return; }
  try { await api("/api/playback/next", "POST", { rating_key: t.rating_key }); }
  catch (_) { STATE.nativeNextIndex = -1; }
}

function advanceUiToQueuedNext(pos) {
  const i = STATE.nativeNextIndex;
  STATE.nativeNextIndex = -1;
  if (i < 0 || i >= STATE.tracks.length) return;
  if (STATE.playing >= 0 && STATE.playing !== i) STATE.playHistory.push(STATE.playing);
  STATE.playing = i;
  const t = STATE.tracks[i];
  STATE.scrobble = { session: STATE.scrobble.session + 1, posted: false };
  STATE.playbackOffset = 0;
  STATE.playbackRecoveryKey = t.rating_key;
  STATE.playbackRecoveryCount = 0;
  STATE.nativePlaybackPosition = pos;
  STATE.nativePlaybackLastPosition = pos;
  STATE.nativePlaybackDuration = (Number(t.duration_ms) || 0) / 1000;
  showPlaybackLcd();
  updateNowPlaying(); updateFullscreenTitle(); prepareVisualizerTrack();
  updatePlaybackDisplay(pos); setPlayIcon(true); renderTracks();
  queueUpNext();
}

function nextIndex(autoAdvance = false) {
  if (!STATE.tracks.length) return -1;
  if (autoAdvance && STATE.repeat === "one" && STATE.playing >= 0) return STATE.playing;
  if (STATE.shuffle && STATE.tracks.length > 1) {
    let pick = STATE.playing;
    for (let tries = 0; tries < 8 && pick === STATE.playing; tries += 1)
      pick = Math.floor(Math.random() * STATE.tracks.length);
    return pick === STATE.playing ? (STATE.playing + 1) % STATE.tracks.length : pick;
  }
  const next = STATE.playing + 1;
  if (next < STATE.tracks.length) return next;
  return STATE.repeat === "all" ? 0 : -1;
}
function previousIndex() {
  if (!STATE.tracks.length) return -1;
  if (STATE.shuffle && STATE.playHistory.length) return STATE.playHistory.pop();
  if (STATE.playing > 0) return STATE.playing - 1;
  return STATE.repeat === "all" ? STATE.tracks.length - 1 : -1;
}
function playNext(autoAdvance = false) {
  const i = nextIndex(autoAdvance);
  if (i >= 0) playIndex(i);
  else setPlayIcon(false);
}
function playPrevious() {
  const i = previousIndex();
  if (i >= 0) playIndex(i, { fromHistory: true });
}
async function playIndex(i, opts = {}) {
  if (i < 0 || i >= STATE.tracks.length) return;
  if (!opts.fromHistory && STATE.playing >= 0 && STATE.playing !== i)
    STATE.playHistory.push(STATE.playing);
  STATE.playing = i;
  const t = STATE.tracks[i];
  STATE.scrobble = { session: STATE.scrobble.session + 1, posted: false };
  STATE.playbackOffset = 0;
  STATE.playbackRecoveryKey = t.rating_key;
  STATE.playbackRecoveryCount = 0;
  if (STATE.nativePlaybackAvailable && !isDeviceTrack(t)) {
    const nativeStarted = await startNativePlayback(t);
    if (!nativeStarted) {
      player.src = streamUrl(t);
      player.play().then(syncVisualizerToPlayback).catch(() => {});
    }
  } else {
    await stopNativePlayback();
    player.src = streamUrl(t);
    player.play().then(syncVisualizerToPlayback).catch(() => {});
  }
  showPlaybackLcd();
  updateNowPlaying();
  updateFullscreenTitle();
  prepareVisualizerTrack();
  $("lcd-prog").classList.add("seekable");
  updatePlaybackDisplay(0);
  setPlayIcon(true); renderTracks();
}
async function togglePlay() {
  if (STATE.playing < 0) { if (STATE.tracks.length) playIndex(0); return; }
  if (STATE.nativePlaybackActive) {
    if (STATE.nativePlaybackPaused) {
      try { await api("/api/playback/resume", "POST", {}); STATE.nativePlaybackPaused = false; syncVisualizerToPlayback(); setPlayIcon(true); } catch (_) {}
    } else {
      try { await api("/api/playback/pause", "POST", {}); STATE.nativePlaybackPaused = true; disconnectVisualizerSource(); setPlayIcon(false); } catch (_) {}
    }
    return;
  }
  if (player.paused) { player.play().then(syncVisualizerToPlayback).catch(() => {}); setPlayIcon(true); } else { player.pause(); setPlayIcon(false); }
}
function setPlayIcon(playing) {
  $("t-play").innerHTML = playing
    ? '<svg viewBox="0 0 16 16"><path d="M3 2h4v12H3zM9 2h4v12H9z"/></svg>'
    : '<svg viewBox="0 0 16 16"><path d="M3 2l11 6L3 14z"/></svg>';
  if ("mediaSession" in navigator) navigator.mediaSession.playbackState = playing ? "playing" : "paused";
}
player.ontimeupdate = () => { if (!scrubbing) updatePlaybackDisplay(); };
player.onloadedmetadata = () => updatePlaybackDisplay();
player.ondurationchange = () => updatePlaybackDisplay();
function resumeCurrentTrackFrom(position) {
  const t = currentTrack();
  if (!t) return;
  STATE.playbackOffset = Math.max(0, position);
  player.src = streamUrl(t, STATE.playbackOffset);
  updatePlaybackDisplay(STATE.playbackOffset);
  prepareVisualizerTrack();
  player.play().then(syncVisualizerToPlayback).catch(() => {});
}
function recoverPlaybackIfEarlyEnd() {
  const t = currentTrack();
  const position = playbackPosition();
  const duration = trackDuration();
  const endedEarly = t && duration > 0 && position > 8 && duration - position > 8;
  if (endedEarly) {
    if (STATE.playbackRecoveryKey !== t.rating_key) {
      STATE.playbackRecoveryKey = t.rating_key;
      STATE.playbackRecoveryCount = 0;
    }
    if (STATE.playbackRecoveryCount < MAX_PLAYBACK_RECOVERIES) {
      STATE.playbackRecoveryCount += 1;
      resumeCurrentTrackFrom(position);
      return true;
    }
  }
  return false;
}
function handlePlaybackEnded() {
  if (recoverPlaybackIfEarlyEnd()) return;
  STATE.playbackRecoveryCount = 0;
  playNext(true);
}
player.onended = handlePlaybackEnded;
player.onerror = () => {
  if (!recoverPlaybackIfEarlyEnd()) setPlayIcon(false);
};

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
  if (STATE.nativePlaybackActive) {
    STATE.nativePlaybackPosition = target;
    STATE.nativePlaybackLastPosition = target;
    updatePlaybackDisplay(target);
    api("/api/playback/seek", "POST", { position: target }).then((status) => {
      STATE.nativePlaybackPosition = Number(status.position || target);
      STATE.nativePlaybackDuration = Number(status.duration || 0) || trackDuration();
      syncVisualizerToPlayback();
    }).catch(() => {});
    return;
  }
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
  STATE.playbackRecoveryKey = t.rating_key;
  STATE.playbackRecoveryCount = 0;
  player.src = streamUrl(t, target);
  if (wasPlaying) player.play().then(syncVisualizerToPlayback).catch(() => {});
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
$("t-next").onclick = () => playNext(false);
$("t-prev").onclick = playPrevious;
$("t-shuffle").onclick = () => {
  STATE.shuffle = !STATE.shuffle;
  if (!STATE.shuffle) STATE.playHistory = [];
  updateTransportModes();
};
$("t-repeat").onclick = () => {
  STATE.repeat = STATE.repeat === "off" ? "all" : STATE.repeat === "all" ? "one" : "off";
  updateTransportModes();
};
$("view-toggle").querySelectorAll("button").forEach((b) => {
  b.onclick = () => {
    STATE.facetView = b.dataset.vm;
    localStorage.setItem("iamped.facetView", STATE.facetView);
    if (STATE.view?.type === "facet") renderFacetList(STATE.view.kind);
  };
});
function seekBy(delta) {
  const duration = trackDuration();
  if (STATE.playing < 0 || !duration) return;
  commitSeek(Math.min(duration, Math.max(0, playbackPosition() + delta)));
}
function setupMediaKeys() {
  if (!("mediaSession" in navigator)) return;
  const handlers = {
    play: () => { if (isPlaybackPaused()) togglePlay(); },
    pause: () => { if (!isPlaybackPaused()) togglePlay(); },
    previoustrack: playPrevious,
    nexttrack: () => playNext(false),
    seekbackward: () => seekBy(-10),
    seekforward: () => seekBy(10),
    seekto: (d) => { if (d.seekTime != null) commitSeek(d.seekTime); },
    stop: () => { if (!isPlaybackPaused()) togglePlay(); },
  };
  Object.entries(handlers).forEach(([action, handler]) => {
    try { navigator.mediaSession.setActionHandler(action, handler); } catch (_) {}
  });
}
// Publish position so the OS "Now Playing" scrubber tracks playback and lets the
// user drag-seek from the system widget (which fires our `seekto` handler).
function updateMediaPositionState() {
  if (!("mediaSession" in navigator) || !navigator.mediaSession.setPositionState) return;
  const duration = trackDuration();
  if (!duration || !Number.isFinite(duration)) return;
  try {
    navigator.mediaSession.setPositionState({
      duration,
      position: Math.min(duration, Math.max(0, playbackPosition())),
      playbackRate: 1,
    });
  } catch (_) {}
}

// ---------------------------------------------------------------- playlist actions
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
async function deleteLocalPlaylist(id, title) {
  if (!id || !confirm(`Delete playlist “${title}”? (Tracks stay in your library.)`)) return;
  await api(`/api/playlist/local/${id.split(":")[1]}`, "DELETE");
  await loadSidebar(); openLibrary();
}

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
    if (profile.reserve_mb != null) $("reserve").value = reserveDefaultMb(profile.reserve_mb);
    if (profile.fill_strategy) STATE.fillStrategy = profile.fill_strategy;
    if (profile.transcode_lossless != null) $("transcode").checked = profile.transcode_lossless;
    if (profile.target_bitrate_k) {
      STATE.bitrateByFormat[isIpod ? "aac" : "mp3"] = Number(profile.target_bitrate_k);
    }
    if (profile.sync_artwork != null) $("sync-artwork").checked = profile.sync_artwork;
    if (profile.mirror != null) $("mirror").checked = profile.mirror;
    if (profile.name) $("device-name").value = profile.name;
    if (isIpod && profile.ipod_format && $("ipod-format"))
      $("ipod-format").value = profile.ipod_format;
    $("autosync").checked = !!profile.autosync;
    $("auto-readback").checked = profile.auto_readback !== false;
  } catch (_) {}
  $("otg-list").innerHTML = ""; $("otg-status").textContent = "";
  $("otg-tool").classList.toggle("hidden", !isIpod);
  updateBitrateControl();
  await loadBackups();
  checkPendingSync();
}

// Surface an interrupted prior sync up front (not just after a failed retry) so
// the stranded files and blocked-sync state are obvious the moment you open the
// device.
async function checkPendingSync() {
  const p = deviceParams();
  if (!p.device_path) return;
  try {
    const r = await api(`/api/device/sync/pending?device_path=${encodeURIComponent(p.device_path)}&device_type=${p.device_type}`);
    if (!r.pending) return;
    const s = $("sync-status"); s.className = "status err";
    s.textContent = `A previous sync of ${r.pending.completed} track(s) was interrupted `
      + `— those files show as “Other”. Click Sync to resume or discard it.`;
  } catch (_) {}
}

function openDeviceInspector() {
  $("pane-device").classList.add("open");
  updateVisualizerMenuState();
  bottomDevice();
}
function closeDeviceInspector() {
  $("pane-device").classList.remove("open");
  updateVisualizerMenuState();
  if (document.querySelector(".content > #pane-browse.active")) bottomBrowse();
}
function toggleDeviceType() {
  const t = [...document.getElementsByName("dtype")].find((r) => r.checked).value;
  $("ipod-warn").classList.toggle("hidden", t !== "ipod");
  $("devname-wrap").classList.toggle("hidden", t !== "ipod");
  $("reclaim-tool").classList.toggle("hidden", t !== "ipod");  // iPod-only junk sweep
  $("otg-tool").classList.toggle("hidden", t !== "ipod");      // iPod-only OTG lists
  // AAC vs ALAC now lives on the bitrate slider; the select stays hidden.
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
// ALAC lives on the far-right of the slider, but only for the iPod (AAC) path —
// USB players get MP3 with no lossless option.
function alacOnSlider() { return activeFormat() === "aac"; }
function alacSelected() {
  return alacOnSlider() && $("ipod-format") && $("ipod-format").value === "alac";
}
function updateBitrateControl() {
  const format = activeFormat();
  const values = BITRATE_PRESETS[format];
  const withAlac = alacOnSlider();
  const alacIdx = values.length;              // one notch past the top preset
  const notch = $("bitrate-notch");
  const control = $("bitrate-control");
  $("bitrate-format").textContent = `${format.toUpperCase()} bitrate`;
  notch.max = String(withAlac ? alacIdx : values.length - 1);

  const ticks = values.map((value) => `<span>${value}</span>`);
  if (withAlac) ticks.push(`<span class="tick-alac">ALAC</span>`);
  $("bitrate-ticks").innerHTML = ticks.join("");

  const note = $("aac-quality-note");
  if (note) note.classList.toggle("hidden", !withAlac);

  if (alacSelected()) {
    notch.value = String(alacIdx);
    $("bitrate-value").textContent = "ALAC · Lossless";
    control.classList.add("alac-selected");
  } else {
    const current = STATE.bitrateByFormat[format] || values[values.length - 1];
    const index = Math.max(0, values.indexOf(current));
    notch.value = String(index);
    $("bitrate-value").textContent = `${values[index]} kbps`;
    control.classList.remove("alac-selected");
  }
}
$("bitrate-notch").oninput = () => {
  const format = activeFormat();
  const values = BITRATE_PRESETS[format];
  const idx = Number($("bitrate-notch").value);
  const control = $("bitrate-control");
  if (alacOnSlider() && idx >= values.length) {
    // Dragged past the top preset → Apple Lossless.
    const wasAlac = control.classList.contains("alac-selected");
    if ($("ipod-format")) $("ipod-format").value = "alac";
    $("bitrate-value").textContent = "ALAC · Lossless";
    control.classList.add("alac-selected");
    if (!wasAlac) triggerAlacGlow();          // glow once, on arrival
    saveIpodFormat();
  } else {
    const value = values[idx];
    STATE.bitrateByFormat[format] = value;
    $("bitrate-value").textContent = `${value} kbps`;
    if (control.classList.contains("alac-selected")) {
      control.classList.remove("alac-selected");
      if ($("ipod-format")) $("ipod-format").value = "aac";
      saveIpodFormat();
    }
  }
};
// Sweep a glow up the slider from the ALAC end for a couple of seconds.
let alacGlowTimer;
function triggerAlacGlow() {
  const control = $("bitrate-control");
  control.classList.remove("alac-glow");
  void control.offsetWidth;                    // restart the CSS animation
  control.classList.add("alac-glow");
  clearTimeout(alacGlowTimer);
  alacGlowTimer = setTimeout(() => control.classList.remove("alac-glow"), 2400);
}
function deviceParams() {
  const t = [...document.getElementsByName("dtype")].find((r) => r.checked).value;
  const p = {
    device_path: $("device-path").value.trim(), device_type: t,
    device_name: $("device-name").value.trim() || "iPod",
    reserve_mb: Number($("reserve").value) || 0, fill_strategy: STATE.fillStrategy || "most_played",
    transcode_lossless: $("transcode").checked, playlist_ids: selectedPlaylists(),
    target_bitrate_k: STATE.bitrateByFormat[t === "ipod" ? "aac" : "mp3"],
    sync_artwork: $("sync-artwork").checked,
    mirror: $("mirror").checked,
  };
  if (t === "ipod") {
    if ($("ipod-format")) p.ipod_format = $("ipod-format").value;
    if (STATE.currentDevice?.ipod_generation)
      p.ipod_generation = STATE.currentDevice.ipod_generation;
  }
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
    renderPlan(r);
    $("btn-sync").disabled = (r.track_count + r.remove_count + (r.artwork_update_count || 0)) === 0 && !r.pending_transaction;
    lcd("iAmped", `${r.add_count.toLocaleString()} additions, ${r.update_count} updates, ${r.artwork_update_count || 0} artwork, ${r.remove_count} removals`);
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
    (r.artwork_update_count ? `, ${r.artwork_update_count} artwork refresh` : "") +
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
        && !r.pending_transaction && !(r.artwork_update_count || 0);
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
    $("btn-sync").disabled = (r.track_count + r.remove_count + (r.artwork_update_count || 0)) === 0;
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
  p.source_id = STATE.activeSourceId || undefined;
  p.name = $("device-name").value.trim() || STATE.currentDevice?.name || "Device";
  p.autosync = $("autosync").checked;
  p.auto_readback = $("auto-readback").checked;
  await api("/api/device/profile", "PUT", p);
  lcd("iAmped", p.autosync
    ? "Device profile saved — it will sync automatically when plugged in"
    : "Device profile saved");
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
async function reclaimOther() {
  const p = deviceParams();
  if (p.device_type !== "ipod") return;
  if (!confirm("Scan the iPod for orphaned audio files (the “Other” junk "
    + "left by interrupted or older syncs) and delete them? Files that are in "
    + "the music database — everything that actually plays — are kept.")) return;
  const btn = $("btn-reclaim"); btn.disabled = true;
  $("reclaim-status").textContent = "Scanning…";
  try {
    const r = await api("/api/device/reclaim", "POST",
      { device_path: p.device_path, device_type: "ipod", device_name: p.device_name });
    if (r.error) { $("reclaim-status").textContent = r.error; return; }
    const msg = r.removed
      ? `Removed ${r.removed} junk file(s) — freed ${fmtBytes(r.freed_bytes || 0)}. Eject safely before unplugging.`
      : "No orphaned files found — nothing to reclaim.";
    $("reclaim-status").textContent = msg;
    lcd("iAmped", msg);
    await loadSidebar();
  } catch (e) { $("reclaim-status").textContent = e.message; }
  finally { btn.disabled = false; }
}
async function sync() {
  const s = $("sync-status"); s.className = "status"; const p = deviceParams();
  if (!confirm(`${p.mirror ? "Mirror" : "Add"} this selection to ${p.device_path} as a ${p.device_type === "ipod" ? "classic iPod (AAC)" : "USB player (MP3)"}?`)) return;
  $("btn-sync").disabled = true; $("btn-plan").disabled = true; s.textContent = "Starting…";
  showSyncProgress(true); setSyncProgress("Starting…", 0, 0);
  showActivity("Syncing…", "Preparing device transfer", 0, true);
  try {
    const { job } = await api("/api/sync", "POST", p);
    pollJob(job, {
      onProgress: (j) => {
        const label = { syncing: "Copying", planning: "Planning", playlists: "Playlists", finalizing: "Finalizing" }[j.phase] || j.phase;
        s.textContent = `${label}: ${j.message || ""}`;
        setSyncProgress(j.phase === "syncing" && j.message ? j.message : `${label}…`, j.done || 0, j.total || 0);
        showActivity(`${label}…`, j.message || "", j.total ? j.done / j.total : 0.1);
      },
      onDone: (j) => {
        s.className = "status ok";
        const r = j.result;
        const total = r.managed_tracks_total ?? r.tracks_total ?? r.tracks ?? r.tracks_added ?? 0;
        s.textContent = `Done — ${total} managed tracks, ${r.tracks_added || 0} added, ${r.tracks_updated || 0} updated, ${r.tracks_removed || 0} removed, ${r.playlists} playlists, ${fmtBytes(r.bytes)}.` + (p.device_type === "ipod" ? " Eject safely before unplugging." : "");
        finishActivity("iAmped", `Synced ${r.tracks_added ?? total} tracks`);
        showSyncProgress(false); $("btn-sync").disabled = false; $("btn-plan").disabled = false;
      },
      onError: (e, job) => {
        showSyncProgress(false); $("btn-sync").disabled = false; $("btn-plan").disabled = false;
        if (job && job.code === "pending_transaction") { offerPendingRecovery(job); return; }
        s.className = "status err"; s.textContent = e; finishActivity("iAmped", "Sync failed");
      },
    });
  } catch (e) { s.className = "status err"; s.textContent = e.message; finishActivity("iAmped", "Sync failed"); $("btn-sync").disabled = false; $("btn-plan").disabled = false; }
}

// A previous sync was interrupted before it wrote the device database. Its
// copied files are stranded (they show as "Other" on an iPod) and its journal
// blocks any sync with different settings. Offer the two clean ways out.
function offerPendingRecovery(job) {
  const s = $("sync-status");
  const n = job?.detail?.completed || 0;
  s.className = "status err";
  s.textContent = `A previous sync of ${n} track(s) was interrupted before finishing.`;
  const resume = confirm(
    `A previous sync of ${n} track(s) was interrupted before it finished writing the iPod's database, `
    + `so those files aren't showing as music (they count as "Other").\n\n`
    + `• OK: keep the same selection and re-run Sync to RESUME and finish it.\n`
    + `• Cancel: DISCARD the interrupted sync — deletes those stranded files to reclaim the space, then start fresh.`);
  if (resume) { sync(); return; }
  discardPendingSync();
}

async function discardPendingSync() {
  const s = $("sync-status"); const p = deviceParams();
  s.className = "status"; s.textContent = "Discarding interrupted sync…";
  try {
    const r = await api("/api/device/sync/discard", "POST",
      { device_path: p.device_path, device_type: p.device_type, device_name: p.device_name });
    if (r.error) { s.className = "status err"; s.textContent = r.error; return; }
    s.className = "status ok";
    s.textContent = `Cleared the interrupted sync — removed ${r.removed} stranded file(s), `
      + `freed ${fmtBytes(r.freed_bytes || 0)}. You can Sync again now.`;
    lcd("iAmped", `Reclaimed ${fmtBytes(r.freed_bytes || 0)} — ready to sync`);
    await loadSidebar();
  } catch (e) { s.className = "status err"; s.textContent = e.message; }
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
  showActivity("Writing to Plex…", "Preparing changes", 0, true);
  try {
    const { job } = await api("/api/device/readback/apply", "POST", {
      device_path: p.device_path, device_type: p.device_type,
      plan: r.plan, reset: $("rb-reset").checked });
    pollJob(job, {
      onProgress: (j) => { s.className = "status"; s.textContent = j.message || "Applying…"; showActivity("Writing to Plex…", j.message || "", j.total ? j.done / j.total : 0.1); },
      onDone: (j) => { s.className = "status ok"; s.textContent = j.message; finishActivity("iAmped", j.message);
        $("btn-readback-apply").classList.add("hidden"); $("readback-wrap").classList.add("hidden");
        $("btn-readback").disabled = false; STATE.readbackPlan = null; refreshStats(); },
      onError: (e) => { s.className = "status err"; s.textContent = e; finishActivity("iAmped", "Apply failed"); $("btn-readback-apply").disabled = false; $("btn-readback").disabled = false; },
    });
  } catch (e) { s.className = "status err"; s.textContent = e.message; $("btn-readback-apply").disabled = false; $("btn-readback").disabled = false; }
}

// ---------------------------------------------------------------- On-The-Go
async function checkOtg() {
  const p = deviceParams();
  if (!p.device_path) return;
  const s = $("otg-status"); s.textContent = "Reading…";
  try {
    const r = await api(`/api/device/otg?device_path=${encodeURIComponent(p.device_path)}`);
    const pls = r.playlists || [];
    s.textContent = pls.length ? "" : "No On-The-Go playlists on this iPod.";
    $("otg-list").innerHTML = pls.map((pl) => `
      <div class="otg-row" data-file="${esc(pl.file)}" style="margin:4px 0;">
        <b>${esc(pl.title)}</b>
        <span class="muted">${plural(pl.matched, "track")}${pl.unmatched ? ` · ${pl.unmatched} unmatched` : ""}</span>
        <button class="otg-import">Save as playlist</button>
        <label style="margin-left:6px;"><input type="checkbox" class="otg-plex"> also in Plex</label>
      </div>`).join("");
    $("otg-list").querySelectorAll(".otg-import").forEach((b) => {
      b.onclick = async () => {
        const row = b.closest(".otg-row");
        b.disabled = true;
        try {
          const res = await api("/api/device/otg/import", "POST", {
            device_path: p.device_path, file: row.dataset.file,
            to_plex: row.querySelector(".otg-plex").checked, clear: true,
          });
          s.textContent = `Saved “${res.title}” (${plural(res.count, "track")})`
            + (res.plex_playlist ? " — also created in Plex" : res.plex_error ? ` — Plex: ${res.plex_error}` : "");
          row.remove();
          await loadSidebar();
        } catch (e) { s.textContent = e.message; b.disabled = false; }
      };
    });
  } catch (e) { s.textContent = e.message; }
}

// ---------------------------------------------------------------- podcasts
async function loadPodcasts() {
  try {
    const r = await api("/api/podcasts");
    const pods = r.podcasts || [];
    $("pod-list").innerHTML = pods.length ? pods.map((p) => `
      <div class="pod-row" data-id="${p.id}" style="display:flex; align-items:center; gap:10px; padding:6px 4px; border-bottom:1px solid #e6e6e6;">
        <div style="flex:1; min-width:0;">
          <b>${esc(p.title)}</b>
          <div class="muted" style="font-size:11px;">${plural(p.episodes, "episode")} · ${p.unplayed} unplayed${p.author ? ` · ${esc(p.author)}` : ""}</div>
        </div>
        <button class="pod-episodes">Episodes</button>
        <button class="pod-remove" title="Unsubscribe">×</button>
      </div>
      <div class="pod-eps hidden" data-for="${p.id}" style="margin:0 0 6px 12px;"></div>`).join("")
      : '<div class="muted">No subscriptions yet — paste an RSS URL above.</div>';
    $("pod-list").querySelectorAll(".pod-remove").forEach((b) => {
      b.onclick = async () => {
        const id = b.closest(".pod-row").dataset.id;
        if (!confirm("Unsubscribe from this podcast?")) return;
        await api(`/api/podcast/${id}`, "DELETE");
        loadPodcasts();
      };
    });
    $("pod-list").querySelectorAll(".pod-episodes").forEach((b) => {
      b.onclick = async () => {
        const id = b.closest(".pod-row").dataset.id;
        const box = $("pod-list").querySelector(`.pod-eps[data-for="${id}"]`);
        if (!box.classList.contains("hidden")) { box.classList.add("hidden"); return; }
        const r2 = await api(`/api/podcast/${id}/episodes?limit=25`);
        box.innerHTML = (r2.episodes || []).map((e) => `
          <div class="muted" style="font-size:11px; padding:2px 0;">
            ${e.played ? "☑" : "☐"} ${esc(e.title)}
            ${e.duration_ms ? ` · ${fmtDur(e.duration_ms)}` : ""}
            <a href="#" class="pod-toggle" data-id="${e.id}" data-played="${e.played ? 0 : 1}">${e.played ? "mark unplayed" : "mark played"}</a>
          </div>`).join("") || '<div class="muted">No episodes.</div>';
        box.classList.remove("hidden");
        box.querySelectorAll(".pod-toggle").forEach((a) => {
          a.onclick = async (ev) => {
            ev.preventDefault();
            await api("/api/podcast/episodes/played", "POST",
              { ids: [Number(a.dataset.id)], played: a.dataset.played === "1" });
            loadPodcasts();
          };
        });
      };
    });
  } catch (e) { $("pod-status").textContent = e.message; }
}
async function addPodcast() {
  const url = $("pod-url").value.trim();
  const s = $("pod-status");
  if (!url) { s.textContent = "Enter a feed URL."; return; }
  s.className = "status"; s.textContent = "Reading feed…";
  try {
    const r = await api("/api/podcasts", "POST", { url });
    s.className = "status ok";
    s.textContent = `Subscribed to “${r.title}” (${plural(r.total_episodes, "episode")}).`;
    $("pod-url").value = "";
    loadPodcasts();
  } catch (e) { s.className = "status err"; s.textContent = e.message; }
}
async function refreshPodcasts() {
  const s = $("pod-status"); s.className = "status"; s.textContent = "Refreshing feeds…";
  try {
    const { job } = await api("/api/podcasts/refresh", "POST", {});
    pollJob(job, {
      onProgress: (j) => { s.textContent = j.message || "Refreshing…"; },
      onDone: (j) => { s.className = "status ok"; s.textContent = j.message; loadPodcasts(); },
      onError: (e) => { s.className = "status err"; s.textContent = e; },
    });
  } catch (e) { s.className = "status err"; s.textContent = e.message; }
}
async function syncPodcasts() {
  const s = $("pod-sync-status");
  const d = STATE.currentDevice;
  if (!d?.path) { s.className = "status err"; s.textContent = "Pick a device under Devices first."; return; }
  s.className = "status"; s.textContent = `Syncing to ${d.name}…`;
  showActivity("Syncing podcasts", `Preparing ${d.name}`, 0, true);
  try {
    const { job } = await api("/api/podcasts/sync", "POST", {
      device_path: d.path, device_type: d.type, device_name: d.name,
      ipod_generation: d.ipod_generation || undefined,
      per_show: Number($("pod-per-show").value) || 3,
      unplayed_only: $("pod-unplayed").checked,
    });
    pollJob(job, {
      onProgress: (j) => {
        s.textContent = j.total ? `${j.message || ""} (${j.done}/${j.total})` : (j.message || "");
        showActivity("Syncing podcasts", j.message || "", j.total ? j.done / j.total : null);
      },
      onDone: (j) => { s.className = "status ok"; s.textContent = j.message; finishActivity("iAmped", j.message); },
      onError: (e) => { s.className = "status err"; s.textContent = e; finishActivity("iAmped", "Podcast sync failed"); },
    });
  } catch (e) { s.className = "status err"; s.textContent = e.message; }
}

// ------------------------------------------------------- background jobs
// Surface work this window didn't start — an autosync kicked off by plugging
// the iPod in, or the plan-time cache warm-up — so syncs never run invisibly.
const WATCHED_JOBS = new Set();
async function pollActiveJobs() {
  try {
    const jobs = await api("/api/jobs/active");
    for (const [jid, job] of Object.entries(jobs)) {
      if (WATCHED_JOBS.has(jid)) continue;
      WATCHED_JOBS.add(jid);
      if (job.kind === "autosync_job") {
        showSyncProgress(true);
        showActivity("Auto-sync", job.message || "Device connected — syncing", 0, true);
        pollJob(jid, {
          onProgress: (j) => { setSyncProgress(j.message || "Auto-sync…", j.done, j.total); showActivity("Auto-sync", j.message || "Syncing…", j.total ? j.done / j.total : 0); },
          onDone: (j) => { showSyncProgress(false); finishActivity("iAmped", j.message || "Auto-sync finished"); loadDevices(); },
          onError: (e) => { showSyncProgress(false); finishActivity("Auto-sync failed", e); },
        });
      } else if (job.kind === "prefetch_job") {
        pollJob(jid, {
          onProgress: (j) => {
            if (j.total) $("bottom-info").textContent =
              `Preparing sync in the background… ${j.done}/${j.total} track(s) cached`;
          },
          onDone: () => { if ($("pane-device").classList.contains("open")) bottomDevice(); },
          onError: () => {},
        });
      }
    }
  } catch (_) {}
}
setInterval(pollActiveJobs, 5000);
setInterval(pollDevices, 3500);   // auto-detect device connect/disconnect

// ---------------------------------------------------------------- wire up
$("btn-connect").onclick = connect;
$("source-kind").onchange = () => setSourceKind($("source-kind").value);
$("source-select").onchange = async () => {
  const id = $("source-select").value;
  if (!id) {
    $("source-name").value = ""; $("baseurl").value = "";
    $("token").value = ""; $("source-username").value = "";
    $("source-password").value = "";
    setSourceKind($("source-kind").value);
    return;
  }
  const source = STATE.sources.find((entry) => entry.id === id);
  if (source) {
    $("source-kind").value = source.kind;
    $("source-name").value = source.name || "";
    $("baseurl").value = source.baseurl || "";
    $("token").value = ""; $("source-username").value = "";
    $("source-password").value = "";
    setSourceKind(source.kind);
  }
  await activateSource(id);
};
$("btn-plex-oauth").onclick = startPlexOAuth;
$("btn-oauth-server").onclick = finishPlexOAuth;
$("btn-readback").onclick = readbackPreview;
$("btn-readback-apply").onclick = applyReadback;
$("btn-build").onclick = () => buildLibrary("auto");
$("btn-otg").onclick = checkOtg;
$("btn-pod-add").onclick = addPodcast;
$("btn-pod-refresh").onclick = refreshPodcasts;
$("btn-pod-sync").onclick = syncPodcasts;
$("src-podcasts").addEventListener("click", loadPodcasts);
$("btn-plan").onclick = plan;
$("btn-sync").onclick = sync;
$("btn-save-profile").onclick = saveDeviceProfile;
$("btn-restore").onclick = restoreBackup;
$("btn-eject").onclick = () => ejectDevice();
$("btn-match").onclick = matchDeviceFiles;
$("btn-reclaim").onclick = reclaimOther;
$("btn-dm-select-foreign").onclick = selectForeignDeviceTracks;
$("btn-dm-ingest").onclick = ingestSelectedDeviceMusic;
$("btn-save-ingest").onclick = saveIngestDir;
if ($("ingest-folder-select")) $("ingest-folder-select").onchange = onIngestFolderPick;
if ($("ingest-dir")) $("ingest-dir").oninput = validateIngestDebounced;
$("ingest-cancel").onclick = closeIngest;
$("ingest-confirm").onclick = confirmIngest;
$("src-music").onclick = openLibrary;
$("src-artists").onclick = () => openFacetIndex("artists");
$("src-albums").onclick = () => openFacetIndex("albums");
$("src-genres").onclick = () => openFacetIndex("genres");
$("btn-close-inspector").onclick = closeDeviceInspector;
$("bitrate-info").onclick = (e) => {
  e.stopPropagation();
  $("bitrate-popover").classList.toggle("hidden");
};
document.addEventListener("click", (e) => {
  if (!e.target.closest("#bitrate-info") && !e.target.closest("#bitrate-popover")) {
    $("bitrate-popover").classList.add("hidden");
  }
});

function closeAppMenus() {
  document.querySelectorAll(".app-menu-panel").forEach((el) => el.classList.add("hidden"));
  document.querySelectorAll(".app-menu-button").forEach((el) => el.classList.remove("active"));
}
function toggleAppMenu(name) {
  const panel = $(`${name}-menu-panel`);
  const button = $(`${name}-menu-button`);
  const open = panel.classList.contains("hidden");
  closeAppMenus();
  if (open) {
    panel.classList.remove("hidden");
    button.classList.add("active");
    updateVisualizerMenuState();
  }
}
function updateVisualizerMenuState() {
  const checks = {
    "menu-check-mini": STATE.visualizerEnabled,
    "menu-check-bars": STATE.visualizerStyle === "bars",
    "menu-check-spectrum": STATE.visualizerStyle === "spectrum",
    "menu-check-led": STATE.visualizerStyle === "led",
    "menu-check-butterchurn": STATE.visualizerFullscreenEngine === "butterchurn",
  };
  Object.entries(checks).forEach(([id, on]) => $(id)?.classList.toggle("on", !!on));
  $("visualizer-dot")?.classList.toggle("active", STATE.visualizerEnabled);
  updateSdMenuState();
}
function updateSdMenuState() {
  $("menu-check-sd")?.classList.toggle("on", !!STATE.sdEnabled);
}
async function toggleSdCard() {
  STATE.sdEnabled = !STATE.sdEnabled;
  updateSdMenuState();
  try { await api("/api/config", "POST", { sd_card_enabled: STATE.sdEnabled }); }
  catch (e) {}
  await loadDevices();
  lcd("iAmped", STATE.sdEnabled
    ? "SD card mode on — plain cards now show under Devices"
    : "SD card mode off");
}
function updateAutosyncMenuState() {
  const check = $("menu-check-autosync");
  if (check) check.textContent = STATE.autosyncEnabled ? "✓" : "";
}
async function toggleAutosync() {
  STATE.autosyncEnabled = !STATE.autosyncEnabled;
  updateAutosyncMenuState();
  try { await api("/api/config", "POST", { autosync_enabled: STATE.autosyncEnabled }); }
  catch (e) {}
  lcd("iAmped", STATE.autosyncEnabled
    ? "Auto-sync on — devices with an auto-sync profile sync when plugged in"
    : "Auto-sync off");
}
function handleAppMenuAction(action) {
  if (action === "server") selectPane("pane-plex", document.querySelector('[data-pane="pane-plex"]'));
  else if (action === "music") openLibrary();
  else if (action === "toggle-sd") { toggleSdCard(); return; }
  else if (action === "toggle-autosync") { toggleAutosync(); return; }
  else if (action === "rebuild-library") {
    selectPane("pane-plex", document.querySelector('[data-pane="pane-plex"]'));
    buildLibrary("full");
    return;
  }
  else if (action === "toggle-visualizer") toggleVisualizer();
  else if (action === "fullscreen-visualizer") openFullscreenVisualizer();
  else if (action === "viz-bars") setAudioMotionStyle("bars", true);
  else if (action === "viz-spectrum") setAudioMotionStyle("spectrum", true);
  else if (action === "viz-led") setAudioMotionStyle("led", true);
  else if (action === "viz-butterchurn") {
    STATE.visualizerFullscreenEngine = "butterchurn";
    openFullscreenVisualizer();
  } else if (action === "inspector" && STATE.currentDevice) openDeviceInspector();
  updateVisualizerMenuState();
}
// The macOS menu calls this entry point from pywebview. Keeping menu behavior
// in the page avoids a separate native command implementation.
window.iAmpedNativeMenuAction = (action) => {
  closeAppMenus();
  handleAppMenuAction(action);
};
document.querySelectorAll(".app-menu-button").forEach((button) => {
  button.onclick = (e) => { e.stopPropagation(); toggleAppMenu(button.dataset.menu); };
});
document.querySelectorAll(".app-menu-row").forEach((row) => {
  row.onclick = (e) => {
    e.stopPropagation();
    const action = row.dataset.action;
    closeAppMenus();
    handleAppMenuAction(action);
  };
});
$("visualizer-dot").onclick = (e) => { e.stopPropagation(); toggleVisualizer(); };
$("btn-exit-visualizer").onclick = closeFullscreenVisualizer;
document.addEventListener("click", (e) => {
  if (!e.target.closest(".app-menubar")) closeAppMenus();
});
document.addEventListener("keydown", (e) => {
  if (STATE.visualizerFullscreen && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
    e.preventDefault();
    stepButterchurnPreset(e.key === "ArrowRight" ? 1 : -1);
    return;
  }
  if (e.key === "Escape") {
    closeAppMenus();
    if (STATE.visualizerFullscreen) closeFullscreenVisualizer();
    return;
  }
  // Cmd/Ctrl-A selects every loaded row in the browse list (but let text
  // fields keep their native select-all).
  if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey
      && e.key.toLowerCase() === "a"
      && !activeEditableTarget(e)
      && $("pane-browse").classList.contains("active")
      && STATE.tracks.length) {
    e.preventDefault(); selectAllTracks(); return;
  }
  if (activeEditableTarget(e) || e.metaKey || e.ctrlKey || e.altKey) return;
  // Delete / Backspace removes the selected tracks from the device (the macOS
  // "delete" key reports as Backspace). Only ever on a device view.
  if ((e.key === "Delete" || e.key === "Backspace")
      && STATE.view?.type === "device") {
    e.preventDefault();
    if (STATE.selected.size) removeSelectedDeviceMusic();
    return;
  }
  if (e.key === " ") {
    e.preventDefault(); togglePlay(); return;
  }
  if (e.key === "ArrowRight") {
    e.preventDefault(); seekBy(e.shiftKey ? 30 : 10); return;
  }
  if (e.key === "ArrowLeft") {
    e.preventDefault(); seekBy(e.shiftKey ? -30 : -10); return;
  }
  if (e.key === "ArrowUp") { e.preventDefault(); nudgeVolume(0.05); return; }
  if (e.key === "ArrowDown") { e.preventDefault(); nudgeVolume(-0.05); return; }
  if (e.key === "/") { e.preventDefault(); $("search").focus(); $("search").select(); return; }
  if (e.key.toLowerCase() === "n") { e.preventDefault(); playNext(false); return; }
  if (e.key.toLowerCase() === "p") { e.preventDefault(); playPrevious(); return; }
  if (e.key.toLowerCase() === "s") {
    e.preventDefault(); STATE.shuffle = !STATE.shuffle; updateTransportModes(); return;
  }
  if (e.key.toLowerCase() === "r") {
    e.preventDefault();
    STATE.repeat = STATE.repeat === "off" ? "all" : STATE.repeat === "all" ? "one" : "off";
    updateTransportModes();
  }
});

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
function visualizerState() {
  if (!STATE.visualizer) {
    STATE.visualizer = {
      audioMotion: null,
      audioCtx: null,
      sourceNode: null,
      sourceTrackKey: null,
      sourceOffset: 0,
      sourceStartedAt: 0,
      audioMotionInput: null,
      silentGain: null,
      buffer: null,
      bufferTrackKey: null,
      bufferStart: 0,
      bufferPromise: null,
      bufferAbort: null,
      bufferError: null,
      butterchurn: null,
      butterchurnConnected: false,
      butterchurnRaf: null,
      butterchurnPresetNames: [],
      butterchurnPresetIndex: -1,
      nativeFullscreen: false,
    };
  }
  return STATE.visualizer;
}
function ensureVisualizerContext(viz = visualizerState()) {
  if (viz.audioCtx) return true;
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return false;
  viz.audioCtx = new AudioCtx();
  viz.silentGain = viz.audioCtx.createGain();
  viz.silentGain.gain.value = 0;
  viz.silentGain.connect(viz.audioCtx.destination);
  return true;
}
function audioMotionOptionsForStyle(style) {
  const common = {
    bgAlpha: .16,
    connectSpeakers: false,
    fftSize: 2048,
    frequencyScale: "log",
    gradient: "steelblue",
    height: 52,
    loRes: true,
    maxFreq: 16000,
    minFreq: 30,
    overlay: true,
    reflexRatio: 0,
    showBgColor: true,
    showPeaks: false,
    showScaleX: false,
    showScaleY: false,
    smoothing: .72,
  };
  if (style === "spectrum") {
    return { ...common, alphaBars: false, fillAlpha: .45, ledBars: false, lineWidth: 1.5, mode: 10 };
  }
  if (style === "led") {
    return { ...common, colorMode: "bar-level", gradient: "prism", ledBars: true, mode: 4, trueLeds: true };
  }
  return { ...common, colorMode: "gradient", ledBars: false, mode: 4, roundBars: true };
}
function configureAudioMotion() {
  const viz = visualizerState();
  if (!viz.audioMotion) return;
  viz.audioMotion.setOptions(audioMotionOptionsForStyle(STATE.visualizerStyle));
}
function ensureVisualizerAudio() {
  const viz = visualizerState();
  if (!window.AudioMotionAnalyzer) {
    lcd("Visualizer unavailable", "audioMotion could not be loaded");
    return false;
  }
  try {
    if (!ensureVisualizerContext(viz)) return false;
    if (!viz.audioMotion) {
      viz.audioMotion = new window.AudioMotionAnalyzer($("visualizer-inline"), {
        ...audioMotionOptionsForStyle(STATE.visualizerStyle),
        audioCtx: viz.audioCtx,
        start: false,
      });
      viz.audioCtx = viz.audioMotion.audioCtx;
    }
    configureAudioMotion();
    if (viz.audioCtx?.state === "suspended") viz.audioCtx.resume().catch(() => {});
    return true;
  } catch (e) {
    lcd("Visualizer failed", e.message || "Could not start audio analyzer");
    return false;
  }
}
function visualizerTrackKey() {
  return currentTrack()?.rating_key || null;
}
function visualizerStreamUrl(t, start = playbackPosition()) {
  if (isDeviceTrack(t)) return deviceStreamUrl(t);
  const params = new URLSearchParams({ duration: "45" });
  if (start > 0) params.set("start", start.toFixed(3));
  return `/api/stream/${encodeURIComponent(t.rating_key)}?${params.toString()}`;
}
async function prepareVisualizerTrack() {
  const track = currentTrack();
  if (!track) return null;
  const viz = visualizerState();
  if (!ensureVisualizerContext(viz)) return null;
  const key = track.rating_key;
  const start = Math.max(0, playbackPosition());
  if (viz.buffer && viz.bufferTrackKey === key && start >= viz.bufferStart && start < viz.bufferStart + viz.buffer.duration - .5) {
    return viz.buffer;
  }
  if (viz.bufferPromise && viz.bufferTrackKey === key && Math.abs(start - viz.bufferStart) < .5) return viz.bufferPromise;
  if (viz.bufferAbort) viz.bufferAbort.abort();
  viz.bufferAbort = new AbortController();
  viz.bufferTrackKey = key;
  viz.bufferStart = start;
  viz.buffer = null;
  viz.bufferError = null;
  viz.bufferPromise = fetch(visualizerStreamUrl(track, start), { signal: viz.bufferAbort.signal })
    .then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.arrayBuffer();
    })
    .then((data) => viz.audioCtx.decodeAudioData(data))
    .then((buffer) => {
      if (viz.bufferTrackKey !== key || viz.bufferStart !== start) return buffer;
      viz.buffer = buffer;
      viz.bufferPromise = null;
      syncVisualizerToPlayback();
      return buffer;
    })
    .catch((e) => {
      if (e.name !== "AbortError") {
        viz.bufferError = e.message || "Could not decode visualizer audio";
        lcd("Visualizer audio unavailable", viz.bufferError);
      }
      if (viz.bufferTrackKey === key) viz.bufferPromise = null;
      return null;
    });
  return viz.bufferPromise;
}
function disconnectVisualizerSource() {
  const viz = visualizerState();
  if (!viz.sourceNode) return;
  try { viz.audioMotion?.disconnectInput(viz.audioMotionInput || viz.sourceNode); } catch (_) {}
  try { if (viz.butterchurnConnected) viz.butterchurn?.disconnectAudio(viz.sourceNode); } catch (_) {}
  try { viz.sourceNode.disconnect(viz.silentGain); } catch (_) {}
  try { viz.sourceNode.stop(); } catch (_) {}
  viz.sourceNode = null;
  viz.audioMotionInput = null;
  viz.butterchurnConnected = false;
  viz.sourceTrackKey = null;
}
function startVisualizerSource() {
  const viz = visualizerState();
  if (!ensureVisualizerAudio()) return false;
  const key = visualizerTrackKey();
  if (!key) return false;
  if (!viz.buffer || viz.bufferTrackKey !== key) {
    prepareVisualizerTrack();
    return false;
  }
  const absolutePosition = Math.max(0, playbackPosition());
  if (absolutePosition < viz.bufferStart || absolutePosition >= viz.bufferStart + viz.buffer.duration - .05) {
    prepareVisualizerTrack();
    return false;
  }
  const offset = Math.min(Math.max(0, absolutePosition - viz.bufferStart), Math.max(0, viz.buffer.duration - .05));
  const currentOffset = viz.bufferStart + viz.sourceOffset + (viz.audioCtx.currentTime - viz.sourceStartedAt);
  if (viz.sourceNode && viz.sourceTrackKey === key && Math.abs(currentOffset - absolutePosition) < .35) return true;
  disconnectVisualizerSource();
  const source = viz.audioCtx.createBufferSource();
  source.buffer = viz.buffer;
  viz.audioMotionInput = viz.audioMotion.connectInput(source);
  if (viz.butterchurn) {
    viz.butterchurn.connectAudio(source);
    viz.butterchurnConnected = true;
  }
  source.connect(viz.silentGain);
  source.start(0, offset);
  viz.sourceNode = source;
  viz.sourceTrackKey = key;
  viz.sourceOffset = offset;
  viz.sourceStartedAt = viz.audioCtx.currentTime;
  source.onended = () => {
    if (viz.sourceNode !== source) return;
    disconnectVisualizerSource();
    if ((STATE.visualizerEnabled || STATE.visualizerFullscreen) && !isPlaybackPaused()) {
      viz.buffer = null;
      prepareVisualizerTrack().then(syncVisualizerToPlayback);
    }
  };
  return true;
}
function syncVisualizerToPlayback() {
  const active = STATE.visualizerEnabled || STATE.visualizerFullscreen;
  if (!active || isPlaybackPaused()) {
    disconnectVisualizerSource();
    return;
  }
  const viz = visualizerState();
  if (viz.audioCtx?.state === "suspended") viz.audioCtx.resume().catch(() => {});
  startVisualizerSource();
}
function setAudioMotionStyle(style, showInline = false) {
  STATE.visualizerStyle = style;
  configureAudioMotion();
  if (showInline) enableVisualizer();
  updateVisualizerMenuState();
}
function enableVisualizer() {
  if (!ensureVisualizerAudio()) return;
  STATE.visualizerEnabled = true;
  $("lcd").classList.add("visualizer-on");
  $("visualizer-dot").classList.add("active");
  $("visualizer-dot").title = "Hide visualizer";
  STATE.visualizer.audioMotion.toggleAnalyzer(true);
  prepareVisualizerTrack();
  syncVisualizerToPlayback();
  requestAnimationFrame(() => STATE.visualizer?.audioMotion?.setOptions({ height: $("visualizer-inline").clientHeight || 52 }));
  updateVisualizerMenuState();
}
function disableVisualizer() {
  STATE.visualizerEnabled = false;
  $("lcd").classList.remove("visualizer-on");
  $("visualizer-dot").classList.remove("active");
  $("visualizer-dot").title = "Show visualizer";
  STATE.visualizer?.audioMotion?.toggleAnalyzer(false);
  if (!STATE.visualizerFullscreen) disconnectVisualizerSource();
  updateVisualizerMenuState();
}
function toggleVisualizer() {
  if (STATE.visualizerEnabled) disableVisualizer();
  else enableVisualizer();
}
function updateFullscreenTitle() {
  const t = STATE.tracks[STATE.playing];
  $("visualizer-fullscreen-title").textContent = t ? `${t.title} — ${t.artist || "Unknown artist"}` : "iAmped";
}
function butterchurnPresetMap() {
  const pack = window.butterchurnPresetsMinimal?.getPresets
    ? window.butterchurnPresetsMinimal
    : window.butterchurnPresetsMinimal?.default || window.butterchurnPresetsMinimal;
  if (!pack) return {};
  return typeof pack.getPresets === "function" ? pack.getPresets() : pack;
}
function butterchurnApi() {
  if (window.butterchurn?.createVisualizer) return window.butterchurn;
  if (window.butterchurn?.default?.createVisualizer) return window.butterchurn.default;
  return null;
}
function loadButterchurnPreset(index, blendTime = 1.25) {
  const viz = visualizerState();
  if (!viz.butterchurn || !viz.butterchurnPresetNames.length) return false;
  const presets = butterchurnPresetMap();
  const count = viz.butterchurnPresetNames.length;
  const normalized = ((index % count) + count) % count;
  const name = viz.butterchurnPresetNames[normalized];
  if (!presets[name]) return false;
  viz.butterchurnPresetIndex = normalized;
  viz.butterchurn.loadPreset(presets[name], blendTime);
  return true;
}
function stepButterchurnPreset(direction) {
  const viz = visualizerState();
  if (!ensureButterchurn()) return;
  const start = viz.butterchurnPresetIndex >= 0 ? viz.butterchurnPresetIndex : 0;
  loadButterchurnPreset(start + direction);
}
function ensureButterchurn() {
  const api = butterchurnApi();
  if (!ensureVisualizerAudio() || !api) return false;
  const viz = visualizerState();
  const canvas = $("butterchurn-canvas");
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(640, Math.round((rect.width || window.innerWidth) * devicePixelRatio));
  const height = Math.max(360, Math.round((rect.height || window.innerHeight) * devicePixelRatio));
  canvas.width = width;
  canvas.height = height;
  if (!viz.butterchurn) {
    viz.butterchurn = api.createVisualizer(viz.audioCtx, canvas, {
      height,
      meshHeight: 48,
      meshWidth: 64,
      pixelRatio: devicePixelRatio,
      textureRatio: 1,
      width,
    });
    const presets = butterchurnPresetMap();
    viz.butterchurnPresetNames = Object.keys(presets);
    if (viz.butterchurnPresetNames.length) {
      const idx = Math.floor(Math.random() * viz.butterchurnPresetNames.length);
      loadButterchurnPreset(idx, 0);
    }
  }
  if (!viz.butterchurnConnected && viz.sourceNode) {
    viz.butterchurn.connectAudio(viz.sourceNode);
    viz.butterchurnConnected = true;
  }
  viz.butterchurn.setRendererSize(width, height);
  return true;
}
function renderButterchurn() {
  const viz = visualizerState();
  if (!STATE.visualizerFullscreen || !viz.butterchurn) return;
  ensureButterchurn();
  viz.butterchurn.render();
  viz.butterchurnRaf = requestAnimationFrame(renderButterchurn);
}
async function openFullscreenVisualizer() {
  if (!ensureVisualizerAudio() || !butterchurnApi()) {
    lcd("Visualizer unavailable", "Butterchurn could not be loaded");
    return;
  }
  STATE.visualizerFullscreen = true;
  updateFullscreenTitle();
  $("visualizer-fullscreen").classList.remove("hidden");
  const viz = visualizerState();
  if (viz.butterchurnRaf) cancelAnimationFrame(viz.butterchurnRaf);
  await new Promise((resolve) => requestAnimationFrame(resolve));
  if (!ensureButterchurn()) {
    closeFullscreenVisualizer();
    lcd("Visualizer unavailable", "Butterchurn could not be loaded");
    return;
  }
  prepareVisualizerTrack();
  syncVisualizerToPlayback();
  renderButterchurn();
  try {
    await $("visualizer-fullscreen").requestFullscreen?.();
    viz.nativeFullscreen = !!document.fullscreenElement;
  } catch (_) {
    viz.nativeFullscreen = false;
  }
  updateVisualizerMenuState();
}
function closeFullscreenVisualizer() {
  const viz = visualizerState();
  STATE.visualizerFullscreen = false;
  $("visualizer-fullscreen").classList.add("hidden");
  if (viz.butterchurnRaf) cancelAnimationFrame(viz.butterchurnRaf);
  viz.butterchurnRaf = null;
  if (!STATE.visualizerEnabled) disconnectVisualizerSource();
  if (document.fullscreenElement && viz.nativeFullscreen) document.exitFullscreen().catch(() => {});
  viz.nativeFullscreen = false;
  updateVisualizerMenuState();
}
player.addEventListener("play", () => {
  if (STATE.visualizerEnabled || STATE.visualizerFullscreen) {
    prepareVisualizerTrack();
    syncVisualizerToPlayback();
  }
  if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "playing";
});
player.addEventListener("pause", () => {
  if (STATE.visualizerEnabled || STATE.visualizerFullscreen) disconnectVisualizerSource();
  if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "paused";
});
player.addEventListener("seeked", () => {
  if (STATE.visualizerEnabled || STATE.visualizerFullscreen) syncVisualizerToPlayback();
});
player.addEventListener("loadedmetadata", updateFullscreenTitle);
$("volume").oninput = () => {
  const volume = Number($("volume").value);
  player.volume = volume;
  if (STATE.nativePlaybackActive) api("/api/playback/volume", "POST", { volume }).catch(() => {});
};
// Keyboard volume nudge — drive the slider so its oninput logic stays the single source.
function nudgeVolume(delta) {
  const el = $("volume");
  el.value = Math.min(1, Math.max(0, Number(el.value) + delta)).toFixed(2);
  el.dispatchEvent(new Event("input"));
}
window.addEventListener("resize", () => {
  if (STATE.visualizer?.audioMotion && STATE.visualizerEnabled) configureAudioMotion();
  if (STATE.visualizerFullscreen) ensureButterchurn();
});
document.addEventListener("fullscreenchange", () => {
  if (STATE.visualizer?.nativeFullscreen && !document.fullscreenElement && STATE.visualizerFullscreen) {
    closeFullscreenVisualizer();
  }
});

// The device surface is a persistent optional inspector, not a replacement
// page. Move it beside the main content at runtime to keep existing form IDs
// and sync behavior intact.
document.querySelector(".body").appendChild($("pane-device"));
$("pane-device").classList.add("device-inspector");

updateTransportModes();
updateNowPlaying();
setupMediaKeys();
loadConfig();
loadDevices();
