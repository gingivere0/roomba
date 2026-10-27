// core.js: talking to the server, live updates, sheets, and small helpers.
// Screens live in screens.js; the map drawing lives in map.js.
"use strict";

const S = {                 // everything the page knows
  sections: {},             // data from the server: map, routines, schedules, dnd, settings, status, history, parts
  errors: {},
  plan: null,               // the last clean started from this app (rooms and prefs), for the progress screen
  login: null,
  stay: true,
  mapId: null,              // the map being shown
  ready: false,
};

// ---------- DOM helpers
const $ = id => document.getElementById(id);
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const k in attrs || {}) {
    const v = attrs[k];
    if (v == null || v === false) continue;
    if (k === "on") for (const ev in v) e.addEventListener(ev, v[ev]);
    else if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else if (k in e && typeof v !== "string") e[k] = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return e;
}
// Let replaceChildren/append take the same things h() does: arrays, and
// null or false for "nothing here".
for (const proto of [Element.prototype, DocumentFragment.prototype]) {
  for (const m of ["replaceChildren", "append"]) {
    const orig = proto[m];
    proto[m] = function (...kids) { return orig.apply(this, kids.flat(Infinity).filter(k => k != null && k !== false)); };
  }
}
function img(name, cls) { return h("img", { src: "static/img/" + name + ".png", class: cls || "ico", alt: "", draggable: "false" }); }

// Line icons drawn in the app's style, for the few the app ships as code.
const ICONS = {
  back: '<path d="M15 5l-7 7 7 7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
  chev: '<path d="M9 5l7 7-7 7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
  play: '<path d="M8 5.5v13l11-6.5z" fill="currentColor"/>',
  camera: '<path d="M4 8.5h3l1.6-2.5h6.8L17 8.5h3v10H4z" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linejoin="round"/><circle cx="12" cy="13.2" r="3.3" fill="none" stroke="currentColor" stroke-width="1.9"/>',
  pause: '<rect x="6.5" y="5" width="4" height="14" rx="1.6" fill="currentColor"/><rect x="13.5" y="5" width="4" height="14" rx="1.6" fill="currentColor"/>',
  skip: '<path d="M5 5.5v13l10-6.5z" fill="currentColor"/><rect x="16" y="5.5" width="3.2" height="13" fill="currentColor"/>',
  x: '<path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  clock: '<circle cx="12" cy="12" r="8.6" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M12 7.5V12l3 2.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  heart: '<path d="M12 19.5s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7a4.3 4.3 0 0 1 7.5 2.5c0 5.4-7.5 10-7.5 10z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/>',
  heartfill: '<path d="M12 19.5s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7a4.3 4.3 0 0 1 7.5 2.5c0 5.4-7.5 10-7.5 10z" fill="currentColor"/>',
  mapedit: '<path d="M3.5 6.5l5-2 6 2 2-.8M3.5 6.5v13l5-2 6 2 6-2.4v-6M8.5 4.5v13M14.5 6.5v13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M15.5 6l2.5 2.5 4-6" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  more: '<circle cx="12" cy="5" r="1.9" fill="currentColor"/><circle cx="12" cy="12" r="1.9" fill="currentColor"/><circle cx="12" cy="19" r="1.9" fill="currentColor"/>',
  plus: '<path d="M12 4v16M4 12h16" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  person: '<circle cx="12" cy="8" r="4" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M5 21c0-4.5 3-7 7-7s7 2.5 7 7" fill="none" stroke="currentColor" stroke-width="1.8"/>',
  bolt: '<path d="M13.5 2.5L5.5 13.5h6l-1.5 8 8-11h-6z" fill="currentColor"/>',
  ring: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="12" cy="7.5" r="1.7" fill="currentColor"/>',
  playring: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M10 8.3v7.4l6-3.7z" fill="currentColor"/>',
  history: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3M4.5 4.5v3h3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M12 8.5V12l2.5 1.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>',
  list: '<path d="M9 7h11M9 12h11M9 17h11" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="5" cy="7" r="1.3" fill="currentColor"/><circle cx="5" cy="12" r="1.3" fill="currentColor"/><circle cx="5" cy="17" r="1.3" fill="currentColor"/>',
  rotate: '<path d="M18.5 12a6.5 6.5 0 1 1-2-4.7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><path d="M17 3.5v4h-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/>',
  trash: '<path d="M5 7h14M10 11v6M14 11v6M7 7l1 12h8l1-12M9.5 7V4.5h5V7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>',
  tune: '<path d="M5 7h9M18 7h1M5 17h3M12 17h7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="16" cy="7" r="2" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="10" cy="17" r="2" fill="none" stroke="currentColor" stroke-width="1.8"/>',
  gear: '<path d="M12 8.6a3.4 3.4 0 1 0 0 6.8 3.4 3.4 0 0 0 0-6.8z" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M10.4 3.5h3.2l.5 2.4 1.7 1 2.3-.8 1.6 2.8-1.8 1.6v2l1.8 1.6-1.6 2.8-2.3-.8-1.7 1-.5 2.4h-3.2l-.5-2.4-1.7-1-2.3.8-1.6-2.8 1.8-1.6v-2L4.3 8.9l1.6-2.8 2.3.8 1.7-1z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
  handle: '<path d="M5 9h14M5 15h14" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  question: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="M9.6 9.5a2.5 2.5 0 1 1 3.4 2.3c-.7.3-1 .8-1 1.6v.4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.8" r="1.2" fill="currentColor"/>',
};
// A sideways battery, filled to the charge level, like the official status card.
function batteryIcon(pct) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const w = Math.max(1.5, 15 * Math.min(100, Math.max(0, pct == null ? 0 : pct)) / 100);
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "battery");
  s.innerHTML = '<rect x="2" y="7" width="18" height="10" rx="2.2" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<rect x="20.6" y="10" width="1.8" height="4" rx=".6" fill="currentColor"/>' +
    '<rect x="3.5" y="8.5" width="' + w.toFixed(1) + '" height="7" rx="1" fill="currentColor"/>';
  return s;
}
function icon(name, size) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.innerHTML = ICONS[name] || "";
  if (size) { s.style.width = size + "px"; s.style.height = size + "px"; }
  return s;
}

function toast(text, bad) {
  if (!text) return;
  const box = $("toasts"), t = h("div", { class: "toast" + (bad ? " bad" : "") }, text);
  box.append(t);
  while (box.children.length > 2) box.firstElementChild.remove();
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, bad ? 6000 : 3000);
}
function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }

// ---------- server
async function api(action, body, opts) {
  opts = opts || {};
  const btn = opts.button;
  if (btn) btn.classList.add("loading");
  try {
    const r = await fetch("api/" + action, { method: "POST",
      headers: { "X-Roomba-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    if (!r.ok) { toast("The app refused that (" + r.status + "). Try reloading the page.", true); return null; }
    const data = await r.json();
    if (data.sections) applySections(data.sections);
    if (data.plan !== undefined) S.plan = data.plan;
    if (data.login) S.login = data.login;
    if (!data.ok) toast(data.message, true);
    else if (!opts.silent && data.message && data.message !== "Loaded.") toast(data.message);
    return data.ok ? data : null;
  } catch (e) {
    toast("Can't reach the app. Is it still running on your PC?", true); return null;
  } finally { if (btn) btn.classList.remove("loading"); }
}

const listeners = new Set();     // functions(changedSectionNames)
function onData(fn) { listeners.add(fn); return () => listeners.delete(fn); }
function applySections(sections) {
  const names = Object.keys(sections || {});
  if (!names.length) return;
  for (const k of names) {
    if (k === "map") mergeMap(sections.map);
    else S.sections[k] = sections[k];
  }
  if (names.includes("status") || names.includes("map")) followClean();
  for (const fn of [...listeners]) fn(names);
}
// When a clean starts (or the app opens during one), show the floor being
// cleaned. Only once per clean, so swiping to another floor still works.
let followedMission = null;
function followClean() {
  if (!isBusy() && !isPaused()) { followedMission = null; return; }
  const m = mission(), id = m.missionId || m.nMssn || "?", active = activeMapId();
  const views = (S.sections.map && S.sections.map.views) || {};
  if (followedMission === id || !active || !views[active]) return;
  S.mapId = active; followedMission = id;
}
// Maps arrive one at a time after an edit; keep every map seen so switching is instant.
function mergeMap(sec) {
  const old = S.sections.map || { views: {} };
  const views = Object.assign({}, old.views || {}, sec.views || {});
  views[sec.map.id] = sec.map;
  const ids = new Set(sec.maps.map(m => m.id));
  for (const k in views) if (!ids.has(k)) delete views[k];
  S.sections.map = { maps: sec.maps, map: sec.map, views };
  if (!S.mapId || !ids.has(S.mapId)) S.mapId = sec.map.id;
}
function currentMap() { const m = S.sections.map; return m && m.views ? m.views[S.mapId] || m.map : null; }
function allMaps() { return (S.sections.map && S.sections.map.maps) || []; }

function liveUpdates() {
  let es;
  const open = () => {
    es = new EventSource("api/events?t=" + encodeURIComponent(TOKEN));
    es.onmessage = e => {
      let ev; try { ev = JSON.parse(e.data); } catch (err) { return; }
      if (ev.type === "sections") applySections(ev.sections);
      if (ev.type === "trail") addTrail(ev);
      if (ev.type === "livemap") addLiveMap(ev);
      if (ev.type === "restarting") toast("The app on your PC is updating. Back in a moment...");
      if (ev.type === "hello" && ev.version && window.VERSION && ev.version !== window.VERSION) appUpdated();
    };
    es.onerror = () => { es.close(); setTimeout(open, 5000); };
  };
  open();
}

// After an update the page needs its new scripts: reload now unless you're
// in the middle of something, otherwise offer a button.
function appUpdated() {
  const busyEditing = sheetDone || /^(mapedit|zones|cleanzones|carpets|furniture)/.test(current && current.name || "");
  if (!busyEditing) { location.reload(); return; }
  if ($("updated")) return;
  document.body.append(h("button", { id: "updated", class: "updated", on: { click: () => location.reload() } }, "The app was updated. Tap to reload."));
}

// ---------- robot status in words
const PHASES = { run: "Cleaning", charge: "Charging", stop: "Paused", pause: "Paused", hmUsrDock: "Returning to dock",
  hmPostMsn: "Returning to dock", hmMidMsn: "Returning to recharge", evac: "Emptying bin", stuck: "Stuck", chargingerror: "Charging error" };
function status() { return (S.sections.status && S.sections.status.data) || {}; }
function mission() { return status().cleanMissionStatus || {}; }
function battery() { const b = status().batPct; return typeof b === "number" ? b : null; }
function robotName() { return status().name || (S.sections.settings && S.sections.settings.data && S.sections.settings.data.name) || "Roomba"; }
function isBusy() { const p = mission().phase; return ["run", "hmUsrDock", "hmPostMsn", "hmMidMsn", "evac", "stuck"].includes(p); }
function isPaused() { const m = mission(); return (m.phase === "stop" || m.phase === "pause") && m.cycle && m.cycle !== "none"; }
function stateText() {
  const m = mission(), p = m.phase;
  if (m.error) return "Needs attention (error " + m.error + ")";
  if (isPaused()) return "Paused";
  if (p === "charge" || !p) {
    // Like the official capsule: "clean" only when a mop pad is fitted.
    const pad = status().detectedPad;
    return typeof pad === "string" && pad && !/none|invalid|^no/i.test(pad) ? "Ready to clean" : "Ready to vacuum";
  }
  return PHASES[p] || p;
}
// History entries for one map, newest first. Entries say which map they
// cleaned in command.map_id; if none of them do, every entry counts.
function historyFor(mapId) {
  const hist = (S.sections.history && S.sections.history.history) || [];
  const mapOf = e => e.command && e.command.map_id;
  if (!mapId || !hist.some(mapOf)) return hist;
  return hist.filter(e => mapOf(e) === mapId);
}
// iRobot's timestamps are sometimes seconds and sometimes milliseconds.
function unixDate(t) { return t ? new Date(t * (t < 1e12 ? 1000 : 1)) : null; }
function lastCleanText(mapId) {
  const hist = historyFor(mapId);
  const d = unixDate(hist.length && hist[0].start_time), today = new Date();
  if (!d) return "No cleans yet";
  const days = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 864e5);
  if (days <= 0) return "Cleaned Today";
  if (days === 1) return "Cleaned Yesterday";
  if (days < 7) return "Cleaned " + d.toLocaleDateString([], { weekday: "long" });
  return "Cleaned " + d.toLocaleDateString([], { month: "short", day: "numeric" });
}

// ---------- cleaning preferences (wire values; see actions.py)
const MODES = [[32, "Vac & Mop"], [2, "Vac Only"], [4, "Mop Only"], [512, "Vac then Mop"]];
const PRESETS = {
  light: { label: "Light", suctionLevel: 1, liquid: 0, twoPass: false },
  standard: { label: "Standard", suctionLevel: 2, liquid: 1, twoPass: false },
  deep: { label: "Deep", suctionLevel: 4, liquid: 2, twoPass: true },
};
const SUCTION = { 1: "Quiet", 2: "Standard", 3: "Turbo", 4: "MAX" };
const LIQUID = { 0: "Low", 1: "Standard", 2: "High" };
function modeAvailable(v) {
  const cap = (status().cap || {}).oMode;
  return typeof cap === "number" ? (cap & v) === v : [2, 32, 4].includes(v) || v === 512;
}
function modeOptions() { return MODES.filter(([v]) => modeAvailable(v)); }
function modeName(v) { const f = MODES.find(m => m[0] === v); return f ? f[1] : "Vac Only"; }
function presetOf(p) {
  for (const k in PRESETS) {
    const q = PRESETS[k];
    const vacOk = p.mode === 4 || p.suctionLevel == null || p.suctionLevel === q.suctionLevel;
    const wetOk = p.mode === 2 || p.liquid == null || p.liquid === q.liquid;
    if (vacOk && wetOk && !!p.twoPass === q.twoPass) return k;
  }
  return "custom";
}
function withPreset(p, key) {
  const q = PRESETS[key]; if (!q) return p;
  return Object.assign({}, p, { suctionLevel: q.suctionLevel, liquid: q.liquid, twoPass: q.twoPass });
}
function prefsText(p) {
  const bits = [{ 2: "Vacuum", 4: "Mop", 32: "Vac & Mop", 512: "Vac then Mop" }[p.mode] || "Vacuum"];
  if (p.mode !== 4 && p.suctionLevel) bits.push("Suction " + SUCTION[p.suctionLevel]);
  if (p.twoPass) bits.push("Cleaning Passes X2");
  return bits.join(" | ");
}
// Only what the robot needs: drop vacuum settings for mop-only and liquid for vacuum-only.
function sendable(p) {
  const out = { mode: p.mode };
  if (p.mode !== 4 && p.suctionLevel) out.suctionLevel = p.suctionLevel;
  if (p.mode !== 2 && p.liquid != null) out.liquid = p.liquid;
  if (p.twoPass != null) out.twoPass = !!p.twoPass;
  if (p.mode !== 2 && p.swScrub != null) out.swScrub = p.swScrub;
  return out;
}

// ---------- sheets
let sheetDone = null;
function openSheet(build) {
  if (sheetDone) { const d = sheetDone; sheetDone = null; d(null); }
  const body = $("sheetbody"); body.replaceChildren();
  return new Promise(res => {
    sheetDone = res;
    build(body, v => closeSheet(v));
    $("scrim").classList.add("on"); $("sheet").classList.add("on"); $("sheet").scrollTop = 0;
  });
}
function closeSheet(v) {
  $("scrim").classList.remove("on"); $("sheet").classList.remove("on");
  const d = sheetDone; sheetDone = null; if (d) d(v);
}
$("scrim").addEventListener("click", () => closeSheet(null));
document.querySelector("#sheet .grab").addEventListener("click", () => closeSheet(null));
document.addEventListener("keydown", e => { if (e.key === "Escape" && sheetDone) closeSheet(null); });

function ask(o) {   // {title, text, ok, danger, typed}
  return openSheet((body, done) => {
    body.append(h("h2", { style: "font-size:18px" }, o.title));
    if (o.text) body.append(h("p", { class: "muted" }, o.text));
    let input = null;
    const okb = h("button", { class: "bigbtn" + (o.danger ? " danger solid" : ""), disabled: !!o.typed, on: { click: () => done(o.typed || true) } }, o.ok || "OK");
    if (o.typed) {
      input = h("input", { class: "field", placeholder: "Type " + o.typed, autocomplete: "off", autocapitalize: "characters",
        on: { input: () => { okb.disabled = input.value.trim() !== o.typed; } } });
      body.append(h("div", { class: "typed" }, h("div", { class: "muted" }, "This can't be undone. Type " + o.typed + " to confirm."), input));
    }
    body.append(okb, h("button", { class: "bigbtn outline", on: { click: () => done(null) } }, "Cancel"));
  });
}
function askText(o) {   // {title, value, label, ok, extra(body, setValue)}
  return openSheet((body, done) => {
    body.append(h("h2", { style: "font-size:18px" }, o.title));
    const input = h("input", { class: "field", value: o.value || "", maxlength: 40, placeholder: o.label || "",
      on: { keydown: e => { if (e.key === "Enter") done(input.value.trim() || null); } } });
    body.append(input);
    if (o.extra) o.extra(body, v => { input.value = v; });
    body.append(h("button", { class: "bigbtn", on: { click: () => done(input.value.trim() || null) } }, o.ok || "Save"),
      h("button", { class: "bigbtn outline", on: { click: () => done(null) } }, "Cancel"));
    setTimeout(() => input.focus(), 320);
  });
}
function dropdown(options, value, onChange) {   // options: [[value, label]]
  const sel = h("select", { on: { change: () => onChange(isNaN(Number(sel.value)) ? sel.value : Number(sel.value)) } },
    options.map(([v, l]) => h("option", { value: v, selected: String(v) === String(value) }, l)));
  return h("div", { class: "drop" }, sel);
}
function pills(options, value, onChange) {   // options: [[value, iconOff, iconOn]]
  const box = h("div", { class: "pills" });
  const draw = v => {
    box.replaceChildren(...options.map(([val, off, on]) => h("button", { class: val === v ? "on" : "", on: { click: () => { draw(val); onChange(val); } } },
      typeof off === "string" ? img(val === v ? on : off) : off(val === v))));
  };
  draw(value);
  return box;
}
function toggle(checked, onChange, disabled) {
  const input = h("input", { type: "checkbox", checked, disabled, on: { change: () => onChange(input.checked, input) } });
  return h("label", { class: "switch" }, input, h("span"));
}

// Fetch every picture once, while the page is idle, so screens open without
// waiting on images. The list comes from the server (window.IMAGES).
function preloadImages() {
  const names = (window.IMAGES || []).slice();
  const next = () => {
    for (let i = 0; i < 12 && names.length; i++) { const im = new Image(); im.src = "static/img/" + names.shift(); }
    if (names.length) setTimeout(next, 30);
  };
  (window.requestIdleCallback || setTimeout)(next);
}

// ---------- time estimates (iRobot's own, per room and per setting)
// The robot's maps in the estimates are keyed by a "smart map" id; use the one
// named after this map, or else the one that knows all these rooms.
function estimateAreas(mapId, roomIds) {
  const maps = (S.sections.estimates && S.sections.estimates.maps) || {};
  if (maps[mapId]) return maps[mapId];
  return Object.values(maps).find(a => roomIds.length && roomIds.every(id => a[id])) || null;
}
// Seconds for one room at these prefs: the closest matching estimate.
function roomSeconds(rows, prefs) {
  const mode = prefs.mode, same = (a, b) => a === b || (a === 6 && b === 32) || (a === 32 && b === 6);
  let best = null, bestScore = -1;
  for (const [secs, p] of rows) {
    const score = (mode == null || same(p.operatingMode, mode) ? 4 : 0)
      + (prefs.suctionLevel == null || p.suctionLevel === prefs.suctionLevel ? 2 : 0)
      + (!!p.twoPass === !!prefs.twoPass ? 1 : 0);
    if (score > bestScore) { best = secs; bestScore = score; }
  }
  return best;
}
function routineSeconds(r) {
  const ids = r.rooms.map(x => x.id), areas = estimateAreas(r.map_id, ids);
  if (!areas || !ids.length) return null;
  let total = 0;
  for (const x of r.rooms) {
    const rows = areas[x.id];
    if (!rows) return null;
    total += roomSeconds(rows, Object.assign({ mode: x.mode }, r.prefs || {}, x.prefs || {}));
  }
  return total;
}
function durationText(secs) {
  const m = Math.max(1, Math.round(secs / 60));
  return m < 60 ? m + " min" : Math.floor(m / 60) + " h" + (m % 60 ? ", " + (m % 60) + " min" : "");
}

// What the live map shows (Map Display in the map menu), remembered in this browser.
const MapDisplay = (() => {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("mapDisplay") || "{}"); } catch (e) { saved = {}; }
  const d = Object.assign({ paths: true, hazards: true, furniture: true }, saved);
  d.save = (k, v) => {
    d[k] = v;
    try { localStorage.setItem("mapDisplay", JSON.stringify({ paths: d.paths, hazards: d.hazards, furniture: d.furniture })); } catch (e) { /* private mode */ }
    for (const fn of [...listeners]) fn(["trail"]);
  };
  return d;
})();

// ---------- the robot's path in the current clean (from the live map stream)
// S.trail = { mission, points: [[x, y, time, modes]], pos: [x, y, heading],
//   live: { explored, coverage, passes, hazards, furniture, dock } from the live map picture }
function trailFor(mission) {
  if (!S.trail || S.trail.mission !== mission) S.trail = { mission, points: [], pos: null, live: null };
  return S.trail;
}
function addLiveMap(ev) {
  trailFor(ev.mission).live = ev;
  for (const fn of [...listeners]) fn(["trail"]);
}
function addTrail(ev) {
  trailFor(ev.mission);
  S.trail.points.push(...ev.points);
  S.trail.pos = ev.pos;
  for (const fn of [...listeners]) fn(["trail"]);
}
function currentTrail() {
  const m = mission(), id = m.missionId || m.nMssn;
  return S.trail && S.trail.mission === id ? S.trail : null;
}
// The map being cleaned: the one whose version the robot updated last.
function activeMapId() {
  const maps = status().p2maps || [];
  let best = null;
  for (const m of maps) if (!best || String(m.p2mapv_id) > String(best.p2mapv_id)) best = m;
  return best ? best.p2map_id : null;
}
const MISSION_MODES = { 2: "Vacuum", 4: "Mop", 6: "Vacuum & Mop", 32: "Vacuum & Mop", 512: "Vacuum then Mop" };
