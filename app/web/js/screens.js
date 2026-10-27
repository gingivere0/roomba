// screens.js: every screen in the app. Each screen is a function that
// returns an element; go("name") switches screens. Screens that show live
// data list the data they use, and redraw when it changes.
"use strict";

const SCREENS = {};
let current = { name: null, el: null, uses: [], update: null };

function go(name, back) {
  if (location.hash.slice(1) !== name) {
    if (back) history.replaceState("root", "", "#" + name); else history.pushState(null, "", "#" + name);
  }
  show(name, back);
}
function goBack(fallback) { if (history.length > 1 && history.state !== "root") history.back(); else go(fallback || "home", true); }
function show(name, back) {
  const [base, arg] = name.split("/");
  const def = SCREENS[base] || SCREENS.home;
  if (sheetDone) closeSheet(null);   // a sheet belongs to the screen that opened it
  let el;
  try { el = def.render(arg); }
  catch (e) {
    // One broken screen shouldn't blank the app: say what happened and offer Home.
    reportProblem("The " + base + " screen failed: " + e.message, e.stack);
    el = h("div", { class: "screen plain" }, topbar("Something went wrong", { center: true, backTo: "home" }),
      soon("This screen didn't load", "The details were sent to the app on your PC."),
      base !== "home" ? h("button", { class: "bigbtn", on: { click: () => go("home", true) } }, "Go to Home") : null);
  }
  if (back) el.classList.add("back-anim");
  if (current.cleanup) current.cleanup();
  current = { name, el, uses: def.uses || [], update: el._update || null, cleanup: el._cleanup || null };
  $("app").replaceChildren(el);
  if (def.nav) $("app").append(navBar(base));
  if (!back) window.scrollTo(0, 0);
  if (el._mounted) el._mounted();
}
window.addEventListener("popstate", () => show(location.hash.slice(1) || "home", true));
onData(names => {
  if (!current.uses.some(u => names.includes(u))) return;
  try {
    if (current.update) current.update(names);
    else show(current.name, true);
  } catch (e) { reportProblem("Updating the " + current.name + " screen failed: " + e.message, e.stack); }
});

function topbar(title, opts) {
  opts = opts || {};
  return h("div", { class: "topbar" + (opts.center ? " center" : "") },
    opts.back !== false ? h("button", { class: "back", "aria-label": "Back", on: { click: () => goBack(opts.backTo) } }, icon("back")) : null,
    h("h1", {}, title), opts.right || (opts.center ? h("div", { style: "width:29px" }) : null));
}
function navBar(active) {
  const tab = (key, label, ico, icoOn) => h("button", { class: active === key ? "on" : "", on: { click: () => go(key) } },
    img(active === key ? icoOn : ico), label);
  return h("nav", { class: "nav" },
    tab("home", "Home", "device_list_home_icon", "device_list_home_selected_icon"),
    tab("automations", "Automations", "device_list_schedule_icon", "device_list_shcedule_selected_icon"),
    tab("support", "Support", "device_list_support_icon", "device_list_support_selected_icon"),
    tab("store", "Store", "device_list_store_icon", "device_list_store_selected_icon"));
}
function soon(title, text, pic) {
  return h("div", { class: "soon" }, pic ? img(pic, "") : null, h("h2", {}, title), h("p", {}, text));
}
function chevron() { const c = icon("chev"); c.classList.add("chev"); return c; }

// =====================================================================
// Home
SCREENS.home = { uses: ["status", "map", "routines", "history", "estimates", "trail", "parts"], render() {
  // One page per map; swipe sideways (or tap the dots) to switch maps,
  // like the official app. Every map is preloaded, so pages show at once.
  const el = h("div", { class: "screen plain" });
  const title = h("h1", { style: "flex:none;max-width:60%" });
  const dots = h("div", { class: "dots" });
  const pager = h("div", { class: "pager" });
  const ids = () => { const m = allMaps().map(x => x.id); return m.length ? m : [null]; };
  const view = id => { const sec = S.sections.map; return id && sec && sec.views ? sec.views[id] || null : currentMap(); };
  const nameOf = id => { const m = allMaps().find(x => x.id === id); return m ? m.name : "Home"; };
  const index = () => Math.max(0, ids().indexOf(S.mapId));

  const drawHead = () => {
    title.textContent = nameOf(S.mapId);
    const list = ids();
    dots.replaceChildren(list.map((id, i) => h("button", { class: i === index() ? "on" : "", "aria-label": nameOf(id),
      on: { click: () => pager.scrollTo({ left: i * pager.clientWidth, behavior: "smooth" }) } })));
  };
  const drawPages = () => {
    pager.replaceChildren(ids().map(id => h("div", { class: "page" },
      statusCard(),
      careCard(),
      routinesCard(view(id), id),
      h("div", { class: "card tap history", on: { click: () => go("history") } },
        h("div", { class: "cardhead" }, icon("history", 17), h("span", { class: "grow" }, "View all history"), chevron()),
        h("div", { class: "big" }, lastCleanText(id))))));
    pager.scrollLeft = index() * pager.clientWidth;
  };
  // When a swipe settles, that page's map becomes the current one.
  let settle = null;
  const settled = () => {
    if (!pager.isConnected) return;   // Home was left (say, for a routine on another map)
    const i = Math.round(pager.scrollLeft / Math.max(1, pager.clientWidth));
    const id = ids()[i];
    if (id && id !== S.mapId) { S.mapId = id; drawHead(); }
  };
  pager.addEventListener("scroll", () => { clearTimeout(settle); settle = setTimeout(settled, 90); drawHeadLive(); }, { passive: true });
  // Move the dots while the finger is still moving.
  const drawHeadLive = () => {
    const i = Math.round(pager.scrollLeft / Math.max(1, pager.clientWidth));
    dots.querySelectorAll("button").forEach((d, k) => d.classList.toggle("on", k === i));
  };

  el.append(
    h("div", { class: "topbar" }, title,
      h("button", { class: "iconbtn", "aria-label": "Map options", on: { click: () => mapMenu() } }, icon("more")), h("div", { style: "flex:1" }),
      h("button", { class: "iconbtn", "aria-label": "Photo of a spot for Claude", on: { click: takePhoto } }, icon("camera")),
      h("button", { class: "iconbtn", "aria-label": "Add", on: { click: () => toast("Adding robots and maps happens in the iRobot app.") } }, icon("plus")),
      h("button", { class: "iconbtn", "aria-label": "This app", on: { click: () => go("account") } }, icon("person"))),
    dots, pager);
  drawHead();
  el._mounted = drawPages;
  // New path points only move the live map, so the pages don't redraw (or jump).
  el._update = names => {
    if (names.length === 1 && names[0] === "trail") { pager.querySelectorAll(".homemap.live").forEach(p => p._draw && p._draw()); return; }
    drawHead(); drawPages();
  };
  const onResize = () => { pager.scrollLeft = index() * pager.clientWidth; };
  window.addEventListener("resize", onResize);
  el._cleanup = () => { clearTimeout(settle); window.removeEventListener("resize", onResize); };
  return el;
} };

function statusCard() {
  const b = battery(), charging = mission().phase === "charge";
  return h("div", { class: "card status tap", on: { click: () => go(isBusy() || isPaused() ? "map" : "robot") } },
    h("div", { class: "cardhead" }, icon("ring", 17), h("span", { class: "grow" }, robotName()), chevron()),
    h("div", { class: "statusline" }, isBusy() || isPaused() ? batteryIcon(b) : charging || b === 100 ? icon("bolt", 16) : null,
      h("span", {}, b == null ? "--" : b + "%"), h("span", { style: "margin-left:7px" }, isBusy() ? "Task Running" : stateText())));
}

function routinesCard(map, mapId) {
  // Routines saved for this map, plus any that aren't tied to a map.
  const routines = ((S.sections.routines && S.sections.routines.routines) || []).filter(r => !r.map_id || !mapId || r.map_id === mapId);
  // Same rule as the official card: due rooms (peach on the map) mean "need some love".
  const due = mapNeedsCleaning(map);
  const happy = due === null ? lastCleanText(mapId) === "Cleaned Today" : !due;
  // While Roomba cleans this map, the official card trades the headline and
  // routines for a big live map and the pause, skip, and stop buttons.
  const live = !!map && (isBusy() || isPaused()) && (activeMapId() || S.mapId) === mapId;
  const preview = h("div", { class: "homemap" + (live ? " live" : ""), on: { click: () => { if (mapId) S.mapId = mapId; go("map"); } } });
  const svg = document.createElementNS(SVGNS, "svg"); preview.append(svg);
  preview._draw = () => drawMap(svg, map, live ? { small: true, pad: 0.08, trail: currentTrail() } : { small: true, pad: 0.16 });
  requestAnimationFrame(preview._draw);
  const head = h("div", { class: "cardhead tap", style: "cursor:pointer", on: { click: () => go("routines") } }, icon("playring", 17), h("span", { class: "grow", style: "color:#555" }, "Routines"), chevron());
  if (live) return h("div", { class: "card routines" }, head, preview, cleanControls("big"));
  const first = routines.slice(0, 2).map(r => routineCircle(r));
  return h("div", { class: "card routines" },
    head,
    h("div", { class: "headline" }, happy ? "Great job keeping " + (map ? map.name : "your home") + " sparkling! Keep it up!" : "Your floors need some love today – time to clean"),
    map ? preview : h("div", { class: "soon", style: "padding:20px" }, "No map yet. Run a clean so Roomba can learn your home."),
    h("div", { class: "circles" },
      first,
      h("button", { class: "circle", on: { click: () => cleanEverywhere() } },
        h("span", { class: "disc smart" }, img("deviceHome_area_clean_btn", "ico lg")), h("span", {}, "Smart"), h("span", {}, "All Rooms")),
      h("button", { class: "circle", on: { click: () => go("routines") } },
        h("span", { class: "disc plain" }, icon("list", 22)), h("span", {}, "See all"), h("span", {}, "Routines"))));
}
function routineCircle(r) {
  // The official app shows the routine's emoji, or a cleaning icon if it has none.
  const emoji = r.icon && /[^\x00-\x7f]/.test(r.icon) && [...r.icon].length <= 4 ? r.icon : null;
  const secs = routineSeconds(r);
  return h("button", { class: "circle", on: { click: () => reviewRoutine(r) } },
    h("span", { class: "disc routine", style: /^#[0-9a-f]{6}$/i.test(r.color || "") ? "background:" + r.color : null }, emoji ? h("span", { class: "emoji" }, emoji) : img("deviceHome_bottomView_sweep_icon", "ico lg")),
    h("span", {}, r.name), h("span", { class: "sub" }, secs ? durationText(secs) : r.rooms.length ? plural(r.rooms.length, "room") : "All rooms"));
}
async function runRoutine(r) {
  const map = currentMap();
  const plan = { name: r.name, rooms: r.rooms.map(x => ({ id: x.id, name: roomNameOf(map, x.id), prefs: x.prefs || {} })) };
  if (await api("run_routine", { favorite_id: r.id, plan })) go("map");
}
// Like the official app, picking a routine opens it on the map first: its
// rooms are selected in order with their own settings, and you can change
// any of it before pressing Start. Multi-step routines just run.
function reviewRoutine(r) {
  const views = (S.sections.map || {}).views || {};
  if (!r.editable || isBusy() || isPaused() || (r.map_id && !views[r.map_id])) { runRoutine(r); return; }
  if (r.map_id) S.mapId = r.map_id;
  const map = currentMap();
  const base = Object.assign({ mode: defaultMode() }, MapUI.global, r.prefs || {});
  const rooms = r.rooms.length ? r.rooms.map(x => ({ id: x.kind === "zid" ? "z:" + x.id : x.id, prefs: Object.assign({}, base, x.mode ? { mode: x.mode } : {}, x.prefs || {}) }))
    : (map ? map.rooms : []).map(x => ({ id: x.id, prefs: Object.assign({}, base) }));
  MapUI.sel = rooms.map(x => x.id);
  MapUI.roomPrefs = Object.fromEntries(rooms.map(x => [x.id, x.prefs]));
  MapUI.tab = "rooms"; MapUI.tall = false;
  MapUI.routine = { r, all: !r.rooms.length, orig: routineKey() };
  go("map");
}
// What the routine would do right now, to tell whether anything was changed.
function routineKey() { return JSON.stringify(MapUI.sel.map(id => [id, sendable(prefsFor(id))])); }
function routineChanged() { return !!MapUI.routine && routineKey() !== MapUI.routine.orig; }
function endRoutine() { if (MapUI.routine) { MapUI.routine = null; MapUI.sel = []; MapUI.roomPrefs = {}; } }
async function cleanEverywhere() {
  const map = currentMap();
  if (!map) { if (await api("start", {})) go("map"); return; }
  const plan = { name: "All rooms", rooms: map.rooms.map(r => ({ id: r.id, name: r.name, prefs: MapUI.global })) };
  if (await api("clean_rooms", { map_id: map.id, everywhere: true, prefs: sendable(MapUI.global), plan })) go("map");
}
function roomNameOf(map, id) {
  if (typeof id === "string" && id.startsWith("z:")) {   // a saved clean zone
    const z = map && (map.clean_zones || []).find(x => x.id === id.slice(2));
    return z ? z.name : "Zone";
  }
  const r = map && map.rooms.find(x => x.id === id); return r ? r.name : "Room " + id;
}

// =====================================================================
// Map: choose rooms, set preferences, start, and watch progress
const MapUI = {
  sel: [],               // room ids in cleaning order
  roomPrefs: {},         // room id -> prefs, when a room has its own
  // Seth's default for a new clean: Vac Only, suction 2 of 4, two passes.
  // Routines keep their own saved settings.
  global: { mode: 2, suctionLevel: 2, liquid: 1, twoPass: true },
  tab: "rooms",
  tall: false,
  spot: null,            // {mapId, at: [x, y] in map meters, room} picked on the Spot tab
  routine: null,         // {r, all, orig} while a routine is open for review
};
// A routine room with no saved mode (only used when editing routines).
function defaultMode() { return modeAvailable(32) ? 32 : 2; }
function prefsFor(id) { return MapUI.roomPrefs[id] || MapUI.global; }

SCREENS.map = { uses: ["status", "map", "trail", "parts"], render() {
  // While Roomba cleans, show the map it's cleaning.
  const active = activeMapId(), views = (S.sections.map || {}).views || {};
  if ((isBusy() || isPaused()) && active && views[active]) S.mapId = active;
  const el = h("div", { class: "mapscreen withpanel" });
  const svg = document.createElementNS(SVGNS, "svg");
  const wrap = h("div", { class: "mapwrap" }, svg);
  const panel = h("div", { class: "panel" + (MapUI.tall ? " tall" : "") });
  const seg = h("div", { class: "seg2" });
  const capsule = h("div", { class: "capsule" });
  const careSlot = h("div", { class: "careslot" });
  const drawCare = () => careSlot.replaceChildren(careCard() || "");
  drawCare();
  let lastBusy = null;

  const drawMapPart = () => {
    const map = currentMap();
    MapUI.sel = MapUI.sel.filter(id => map && (map.rooms.some(r => r.id === id) || (map.clean_zones || []).some(z => "z:" + z.id === id)));
    const working = isBusy() || isPaused();
    const spot = MapUI.tab === "spot" && !working && map && MapUI.spot && MapUI.spot.mapId === map.id ? MapUI.spot : null;
    drawMap(svg, map, { sel: MapUI.tab === "rooms" && !working ? MapUI.sel : [],
      onRoom: (id, pt) => MapUI.tab === "spot" ? onSpotTap(id, pt) : onRoomTap(id),
      trail: working ? currentTrail() : null,
      extra: spot ? (world, px) => drawSpotPin(world, px, spot.at) : null });
  };
  const drawCapsule = () => {
    const b = battery();
    capsule.replaceChildren(icon("ring", 15), h("span", {}, robotName()), mission().phase === "charge" ? icon("bolt", 13) : null, h("span", {}, b == null ? "" : b + "%"));
  };
  const drawSeg = () => {
    seg.classList.toggle("hidden", isBusy() || isPaused());
    seg.replaceChildren(...[["rooms", "Rooms & Zones"], ["spot", "Spot"]].map(([k, l]) =>
      h("button", { class: MapUI.tab === k ? "on" : "", on: { click: () => { MapUI.tab = k; setTall(false); drawMapPart(); drawSeg(); drawPanel(); } } }, l)));
  };
  const drawPanel = () => {
    const top = panel.scrollTop;
    const grab = h("div", { class: "grab", on: { click: () => { MapUI.tall = !MapUI.tall; panel.classList.toggle("tall", MapUI.tall); } } });
    panel.replaceChildren(grab, isBusy() || isPaused() ? progressView() : MapUI.tab === "spot" ? spotView(drawAll) : roomsView(drawAll));
    panel.scrollTop = MapUI.tall ? top : 0;
  };
  const drawAll = () => { drawMapPart(); drawSeg(); drawPanel(); drawCapsule(); };

  // Tapping a room on the map adds it to the clean, or takes it out again.
  // Its own preferences are under the gear in the "Where to clean" list.
  function onRoomTap(id) {
    if (isBusy() || isPaused() || MapUI.tab !== "rooms") return;
    if (MapUI.sel.includes(id)) { MapUI.sel = MapUI.sel.filter(x => x !== id); delete MapUI.roomPrefs[id]; }
    else addRoom(id);
    drawAll();
  }
  // On the Spot tab a tap anywhere on the floor drops the pin there.
  function onSpotTap(id, pt) {
    if (isBusy() || isPaused()) return;
    const map = currentMap();
    MapUI.spot = { mapId: map.id, at: pt, room: id.startsWith("z:") ? null : id };
    drawAll();
  }

  el.append(
    h("div", { class: "topbar" }, h("button", { class: "back", "aria-label": "Back", on: { click: () => goBack() } }, icon("back")),
      h("h1", {}, (currentMap() || {}).name || "Map"),
      h("button", { class: "iconbtn", "aria-label": "Edit map", on: { click: () => mapMenu() } }, icon("mapedit", 20))),
    capsule, careSlot, wrap);
  wrap.append(seg);
  el.append(panel);
  // The sheet follows the finger like the official one: drag up to open it,
  // drag down from its top to close it. Scrolling it open works too.
  const setTall = on => { MapUI.tall = on; panel.classList.toggle("tall", on); if (!on) panel.scrollTop = 0; };
  panel.addEventListener("scroll", () => { if (panel.scrollTop > 30 && !MapUI.tall) setTall(true); });
  let startY = null, startTop = 0;
  panel.addEventListener("touchstart", e => { if (e.target.closest(".selrow .handle")) return; startY = e.touches[0].clientY; startTop = panel.scrollTop; }, { passive: true });
  panel.addEventListener("touchmove", e => {
    if (startY == null) return;
    const dy = e.touches[0].clientY - startY;
    if (!MapUI.tall && dy < -25) { setTall(true); startY = null; }
    else if (MapUI.tall && startTop <= 0 && panel.scrollTop <= 0 && dy > 45) { setTall(false); startY = null; }
  }, { passive: true });
  panel.addEventListener("touchend", () => { startY = null; }, { passive: true });
  el._mounted = () => { drawAll(); lastBusy = isBusy() || isPaused(); };
  el._update = names => {
    const busyNow = isBusy() || isPaused();
    drawCapsule();
    if (names.includes("parts")) drawCare();
    if (names.includes("map") || names.includes("trail")) drawMapPart();
    if (busyNow !== lastBusy) setTall(false);
    if (busyNow !== lastBusy || busyNow) { drawSeg(); drawPanel(); }
    lastBusy = busyNow;
  };
  // Redraw when the map area changes size (window resized, panel grown or shrunk).
  const ro = new ResizeObserver(() => drawMapPart());
  ro.observe(wrap);
  // A routine opened for review is only kept while the map screen is open.
  el._cleanup = () => { ro.disconnect(); if (location.hash.slice(1).split("/")[0] !== "map") endRoutine(); };
  return el;
} };

function roomsView(redraw) {
  const map = currentMap();
  const rt = MapUI.routine;
  const ready = MapUI.sel.length > 0;
  const secs = ready ? selectionSeconds(map) : null;
  const start = h("button", { class: "rbtn" + (ready ? " go" : ""), disabled: !ready, "aria-label": "Start", on: { click: e => rt ? startRoutine(e.currentTarget) : startRooms(e.currentTarget) } }, icon("play"));
  const unselected = (map ? map.rooms : []).filter(r => !MapUI.sel.includes(r.id))
    .concat(((map && map.clean_zones) || []).map(z => ({ id: "z:" + z.id, name: z.name, zone: true })).filter(z => !MapUI.sel.includes(z.id)));
  // For a routine, the top preferences set every room in it at once.
  const shared = rt && ready ? prefsFor(MapUI.sel[0]) : MapUI.global;
  const setShared = p => {
    if (rt) { const q = sendable(p); MapUI.sel.forEach(id => { MapUI.roomPrefs[id] = Object.assign({}, q); }); }
    else MapUI.global = p;
    redraw();
  };
  return h("div", {},
    h("div", { class: "rbtns" }, start,
      h("button", { class: "rbtn" + (ready ? " outline" : ""), disabled: !ready, "aria-label": "Schedule", on: { click: () => editSchedule(null, selectionRooms()) } }, icon("clock")),
      h("button", { class: "rbtn" + (ready ? " outline" : ""), disabled: !ready, "aria-label": rt ? "Save this routine" : "Save as routine",
        on: { click: () => rt ? saveRoutineSheet(redraw) : editRoutine(null, selectionRooms()) } }, icon("heart")),
      secs ? h("span", { class: "est" }, "~ " + durationText(secs)) : null),
    h("div", { class: "rowh" }, h("h3", {}, "Cleaning Preferences"),
      h("button", { class: "help", "aria-label": "Help", on: { click: () => prefsHelp() } }, "?")),
    prefsEditor(shared, setShared, { compact: true }),
    h("div", { class: "hint adv" }, "Scroll for advanced options"),
    h("h3", {}, "Where to clean"),
    ready ? selectedList(map, redraw) : [
      h("div", { class: "hint small" }, "Select rooms or zones from the list below"),
      h("button", { class: "btn-out", on: { click: () => { (map ? map.rooms : []).forEach(r => addRoom(r.id)); redraw(); } } }, "Select all rooms")],
    unselected.length ? h("h3", { style: "margin-top:22px" }, "Rooms & Zones") : null,
    unselected.map(r => h("div", { class: "roomrow", on: { click: () => { addRoom(r.id); redraw(); } } },
      h("span", { class: "plus" }, icon("plus", 15)), h("div", { class: "grow" }, r.name, r.zone ? h("div", { class: "meta" }, "Clean zone") : null))),
    h("h3", { style: "margin-top:22px" }, "Advanced options"),
    advancedPrefs(shared, setShared),
    h("div", { style: "height:20px" }));
}
// Adding a room to a routine under review gives it the routine's settings,
// not the everyday default.
function addRoom(id) {
  if (MapUI.sel.includes(id)) return;
  if (MapUI.routine && MapUI.sel.length) MapUI.roomPrefs[id] = Object.assign({}, prefsFor(MapUI.sel[0]));
  MapUI.sel.push(id);
}
async function startRoutine(btn) {
  const rt = MapUI.routine, map = currentMap();
  const plan = { name: rt.r.name, rooms: MapUI.sel.map(id => ({ id, name: roomNameOf(map, id), prefs: prefsFor(id) })) };
  // Unchanged: run the saved routine itself. Changed: run this one-off version.
  const ok = routineChanged()
    ? await api("clean_rooms", { map_id: map.id, rooms: selectionRooms(), plan }, { button: btn })
    : await api("run_routine", { favorite_id: rt.r.id, plan }, { button: btn });
  if (ok) endRoutine();
}
function saveRoutineSheet(redraw) {
  const rt = MapUI.routine, map = currentMap();
  openSheet((body, done) => {
    body.append(h("h2", { style: "font-size:18px" }, routineChanged() ? "Keep these changes?" : rt.r.name),
      h("p", { class: "muted" }, MapUI.sel.map(id => roomNameOf(map, id)).join(", ")),
      h("button", { class: "bigbtn", on: { click: async e => {
        const ok = await api("update_routine", { favorite_id: rt.r.id, name: rt.r.name, map_id: map.id, rooms: selectionRooms() }, { button: e.currentTarget });
        if (ok && MapUI.routine === rt) { rt.orig = routineKey(); done(true); redraw(); }
      } } }, "Update " + rt.r.name),
      h("button", { class: "bigbtn outline", on: { click: () => { done(null); editRoutine(null, selectionRooms()); } } }, "Save as a new routine"),
      h("button", { class: "bigbtn outline", on: { click: () => { done(null); editRoutine(rt.r); } } }, "Rename or delete " + rt.r.name),
      h("button", { class: "bigbtn outline", on: { click: () => done(null) } }, "Cancel"));
  });
}
// The rooms in this clean, in order. Drag the handle to reorder; the gear
// gives a room its own preferences.
function selectedList(map, redraw) {
  const box = h("div", { class: "selbox" });
  const rows = MapUI.sel.map(id => {
    const r = { name: roomNameOf(map, id) };
    const p = prefsFor(id);
    const handle = h("span", { class: "handle", "aria-label": "Drag to reorder" }, icon("handle", 18));
    const row = h("div", { class: "selrow" }, handle,
      h("div", { class: "grow" }, r.name, h("div", { class: "meta" }, MapUI.roomPrefs[id] && !MapUI.routine ? prefsText(p) : modeLabel(p.mode))),
      h("button", { class: "tune", "aria-label": "Room preferences", on: { click: () => roomSheet(id, redraw) } }, icon("gear", 18)));
    row.dataset.id = id;
    handle.addEventListener("pointerdown", e => dragRow(e, row, box, redraw));
    return row;
  });
  box.append(rows);
  return box;
}
function dragRow(e, row, box, redraw) {
  e.preventDefault();
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);
  let y0 = e.clientY;   // where the finger started, adjusted as the row changes place
  row.classList.add("dragging");
  const mid = el => el.getBoundingClientRect().top + el.offsetHeight / 2;
  const swap = (fn, ev) => { const t = row.offsetTop; fn(); y0 += row.offsetTop - t; row.style.transform = `translateY(${ev.clientY - y0}px)`; };
  const move = ev => {
    row.style.transform = `translateY(${ev.clientY - y0}px)`;
    const prev = row.previousElementSibling, next = row.nextElementSibling;
    if (prev && mid(row) < mid(prev)) swap(() => box.insertBefore(row, prev), ev);
    else if (next && mid(row) > mid(next)) swap(() => box.insertBefore(next, row), ev);
  };
  const up = () => {
    handle.removeEventListener("pointermove", move);
    handle.removeEventListener("pointerup", up);
    handle.removeEventListener("pointercancel", up);
    MapUI.sel = [...box.children].map(x => x.dataset.id);
    redraw();
  };
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", up);
  handle.addEventListener("pointercancel", up);
}
function modeLabel(mode) { const m = MODES.find(([v]) => v === mode); return m ? m[1] : ""; }
function selectionSeconds(map) {
  if (!map) return null;
  const areas = estimateAreas(map.id, MapUI.sel);
  if (!areas) return null;
  let total = 0;
  for (const id of MapUI.sel) {
    if (!areas[id]) return null;
    total += roomSeconds(areas[id], sendable(prefsFor(id)));
  }
  return total;
}
function selectionRooms() {
  return MapUI.sel.map(id => id.startsWith("z:") ? { id: id.slice(2), zone: true, prefs: sendable(prefsFor(id)) } : { id, prefs: sendable(prefsFor(id)) });
}
async function startRooms(btn) {
  const map = currentMap();
  const plan = { name: plural(MapUI.sel.length, "room"), rooms: MapUI.sel.map(id => ({ id, name: roomNameOf(map, id), prefs: prefsFor(id) })) };
  const ok = await api("clean_rooms", { map_id: map.id, rooms: selectionRooms(), plan }, { button: btn });
  if (ok) { MapUI.sel = []; MapUI.roomPrefs = {}; }
}
function spotView(redraw) {
  let mode = MapUI.global.mode === 512 ? 32 : MapUI.global.mode;
  const map = currentMap();
  const spot = map && MapUI.spot && MapUI.spot.mapId === map.id ? MapUI.spot : null;
  const where = spot ? (spot.room ? "Spot in " + roomNameOf(map, spot.room) : "Spot at the pin") : "Where Roomba is now";
  const send = e => spot
    ? api("point_clean", { map_id: map.id, point: spot.at,
        prefs: { mode, suctionLevel: MapUI.global.suctionLevel, twoPass: MapUI.global.twoPass } }, { button: e.currentTarget })
    : api("spot", { mode }, { button: e.currentTarget });
  return h("div", {},
    h("div", { class: "rbtns" }, h("button", { class: "rbtn go", "aria-label": "Start spot clean", on: { click: send } }, icon("play")),
      h("span", { class: "label" }, where)),
    h("div", { style: "margin-top:16px" }, dropdown(modeOptions().filter(([v]) => v !== 512), mode, v => { mode = v; })),
    h("p", { class: "muted", style: "margin-top:13px;line-height:1.5" }, spot
      ? "Roomba drives to the pin and cleans a small area around it. Tap somewhere else on the map to move the pin."
      : "Tap the map to choose a spot. With no pin, Spot Clean cleans a small square around where Roomba is now."),
    spot ? h("button", { class: "bigbtn outline", on: { click: () => { MapUI.spot = null; redraw(); } } }, "Clear the pin") : null,
    photoButton(),
    spot ? null : img("deviceHome_spotClean_placeHolder", "robotimg"));
}

// ---------------------------------------------------------------------
// Photo of a spot: the phone's camera takes a picture, the app shrinks it
// and saves it on the PC (roomba\photos) with a note, for Claude to find
// where it is on the map.
// Opens the phone's camera; the picture goes to photoSheet. Used by the Home
// header's camera button and the Spot tab.
function takePhoto() {
  let input = $("photoinput");
  if (!input) {
    input = h("input", { id: "photoinput", type: "file", accept: "image/*", capture: "environment", style: "display:none",
      on: { change: () => { const f = input.files && input.files[0]; input.value = ""; if (f) photoSheet(f); } } });
    document.body.append(input);
  }
  input.click();
}
function photoButton() {
  return h("div", { class: "photobox" },
    h("button", { class: "bigbtn outline photobtn", on: { click: takePhoto } }, icon("camera", 20), "Photo of a spot for Claude"),
    h("p", { class: "muted" }, "Take a picture of what needs cleaning. It's saved on your PC for Claude to place on the map."));
}
// Shrink to at most 2048 px on the long side as a JPEG (this also drops the
// photo's location data). Returns { blob, w, h }.
function shrinkPhoto(file) {
  return new Promise((res, rej) => {
    const url = URL.createObjectURL(file), pic = new Image();
    pic.onload = () => {
      const k = Math.min(1, 2048 / Math.max(pic.naturalWidth, pic.naturalHeight));
      const w = Math.round(pic.naturalWidth * k), ht = Math.round(pic.naturalHeight * k);
      const c = document.createElement("canvas"); c.width = w; c.height = ht;
      c.getContext("2d").drawImage(pic, 0, 0, w, ht);
      URL.revokeObjectURL(url);
      c.toBlob(b => b ? res({ blob: b, w, h: ht }) : rej(new Error("couldn't make a JPEG")), "image/jpeg", 0.85);
    };
    pic.onerror = () => { URL.revokeObjectURL(url); rej(new Error("couldn't read that picture")); };
    pic.src = url;
  });
}
// After sending: show what Claude is doing with the photo (photo_spot.py on
// the PC writes its progress into the photo's .json) and pin the spot.
const PHOTO_STEPS = { new: "Saved on your PC. Waiting for Claude...", thinking: "Claude is finding the spot..." };
function followPhoto(body, done, photo, src) {
  const line = h("p", { class: "photostatus" }, PHOTO_STEPS.new);
  const close = h("button", { class: "bigbtn outline", on: { click: () => done(true) } }, "Close");
  body.replaceChildren(h("h2", {}, "Photo for Claude"), h("img", { class: "photoprev", alt: "", src }), line, close);
  const started = Date.now();
  const tick = async () => {
    if (!body.isConnected || !$("sheet").classList.contains("on")) return;   // sheet closed: stop asking
    let p = null;
    try {
      const r = await fetch("api/photos", { method: "POST", headers: { "X-Roomba-Token": TOKEN, "Content-Type": "application/json" }, body: "{}" });
      p = ((await r.json()).photos || []).find(x => x.file === photo.file) || null;
    } catch (e) { /* try again next time */ }
    const st = p ? p.status : "new";
    line.className = "photostatus " + st;
    line.textContent = (p && p.message) || PHOTO_STEPS[st] || st;
    const spot = p && (p.sent || p.spot);
    if ((st === "sent" || st === "found") && spot) {
      // "sent": Roomba is on its way. "found": pinned, and Start on the Spot tab sends it.
      MapUI.spot = { mapId: spot.map_id, at: spot.point };
      S.mapId = spot.map_id;
      close.textContent = st === "found" ? "Check the spot, then Start" : "Show on the map";
      close.onclick = () => { done(true); MapUI.tab = "spot"; go("map"); };
      return;
    }
    if (st === "unsure" || st === "error") return;
    if (st === "new" && Date.now() - started > 20000) {
      line.textContent = "Saved on your PC, but nothing picked it up yet. Tell Claude it's in the photos folder."; return;
    }
    if (Date.now() - started > 6 * 60000) { line.textContent += " (still waiting; check back later)"; return; }
    setTimeout(tick, 2000);
  };
  setTimeout(tick, 1500);
}
function photoSheet(file) {
  const map = currentMap();
  openSheet((body, done) => {
    const preview = h("img", { class: "photoprev", alt: "" });
    const note = h("textarea", { class: "field", rows: 2, maxlength: 500, placeholder: "Optional note, e.g. crumbs under the table" });
    const status = h("p", { class: "muted" }, "Preparing the photo...");
    const send = h("button", { class: "bigbtn", disabled: true }, "Send to Claude");
    let shrunk = null;
    shrinkPhoto(file).then(r => { shrunk = r; preview.src = URL.createObjectURL(r.blob); send.disabled = false;
      status.textContent = (map ? "Map: " + map.name + ". " : "") + Math.round(r.blob.size / 1024) + " KB."; })
      .catch(e => { status.textContent = "This photo didn't work (" + e.message + "). Try taking it again."; });
    send.addEventListener("click", async () => {
      if (!shrunk) return;
      send.classList.add("loading"); send.disabled = true;
      const q = new URLSearchParams({ note: note.value.trim(), map: map ? map.id : "", w: shrunk.w, h: shrunk.h });
      try {
        const r = await fetch("api/photo?" + q, { method: "POST", headers: { "X-Roomba-Token": TOKEN, "Content-Type": "image/jpeg" }, body: shrunk.blob });
        const data = r.ok ? await r.json() : { ok: false, message: "The app refused the photo (" + r.status + ")." };
        if (!data.ok) { toast(data.message, true); send.disabled = false; return; }
        followPhoto(body, done, data.photo, preview.src);
      } catch (e) { toast("Can't reach the app. Is it still running on your PC?", true); send.disabled = false; }
      finally { send.classList.remove("loading"); }
    });
    body.append(h("h2", {}, "Photo for Claude"), preview, note, status, send,
      h("button", { class: "bigbtn outline", on: { click: () => done(null) } }, "Cancel"));
  });
}
// The spot pin: a ring roughly the size of Roomba, with a dot in the middle.
function drawSpotPin(world, px, at) {
  const [x, y] = at;
  world.append(sv("circle", { cx: x, cy: y, r: Math.max(0.17, 10 * px), class: "m-spotring", "stroke-width": 2.5 * px }));
  world.append(sv("circle", { cx: x, cy: y, r: 3 * px, class: "m-spotdot" }));
}

// Mode + preset dropdowns (and, unless compact, the detail pills).
function prefsEditor(prefs, onChange, opts) {
  opts = opts || {};
  const box = h("div", {});
  let p = Object.assign({}, prefs);
  const draw = () => {
    const preset = presetOf(p);
    const presetOpts = [["light", "Light"], ["standard", "Standard"], ["deep", "Deep"]];
    if (preset === "custom") presetOpts.push(["custom", "Custom"]);
    box.replaceChildren(h("div", { class: "drops" },
      dropdown(modeOptions(), p.mode, v => { p = Object.assign({}, p, { mode: v }); onChange(p); draw(); }),
      dropdown(presetOpts, preset, v => { if (v !== "custom") { p = withPreset(p, v); onChange(p); draw(); } })),
      opts.compact ? null : advancedPrefs(p, q => { p = q; onChange(p); draw(); }));
  };
  draw();
  return box;
}
function advancedPrefs(p, onChange) {
  const box = h("div", {});
  const set = (k, v) => onChange(Object.assign({}, p, { [k]: v }));
  const row = (label, valueText, control) => h("div", { class: "prefrow" }, h("div", { class: "rowh" }, h("span", {}, label), h("b", {}, valueText)), control);
  if (p.mode !== 4) box.append(row("Suction", SUCTION[p.suctionLevel || 2], pills([1, 2, 3, 4].map(n => [n, `home_sheet_action_suction_${n}_n`, `home_sheet_action_suction_${n}_s`]), p.suctionLevel || 2, v => set("suctionLevel", v))));
  if (p.mode !== 2) box.append(row("Liquid Amount", LIQUID[p.liquid == null ? 1 : p.liquid], pills([0, 1, 2].map(n => [n, `home_sheet_action_water_${n + 1}_n`, `home_sheet_action_water_${n + 1}_s`]), p.liquid == null ? 1 : p.liquid, v => set("liquid", v))));
  box.append(row("Cleaning Passes", p.twoPass ? "Two Pass" : "One Pass", pills([[false, "home_sheet_action_times_1_n", "home_sheet_action_times_1_s"], [true, "home_sheet_action_times_2_n", "home_sheet_action_times_2_s"]], !!p.twoPass, v => set("twoPass", v))));
  if (p.mode !== 2) box.append(row("SmartScrub", p.swScrub ? "On" : "Off", pills([[0, "deviceHome_sheet_smartScrub_off_n", "deviceHome_sheet_smartScrub_off_s"], [1, "deviceHome_sheet_smartScrub_on_n", "deviceHome_sheet_smartScrub_on_s"]], p.swScrub || 0, v => set("swScrub", v))));
  return box;
}
function roomSheet(id, redraw) {
  const map = currentMap();
  let p = Object.assign({}, prefsFor(id));
  openSheet((body, done) => {
    body.append(h("h2", {}, roomNameOf(map, id)), h("h3", { style: "margin-top:0;font-size:13px" }, "Cleaning Preferences"),
      prefsEditor(p, q => { p = q; }),
      h("button", { class: "bigbtn", on: { click: () => { MapUI.roomPrefs[id] = p; done(true); redraw(); } } }, "Save"),
      h("button", { class: "bigbtn outline", on: { click: () => { MapUI.sel = MapUI.sel.filter(x => x !== id); delete MapUI.roomPrefs[id]; done(true); redraw(); } } }, "Remove from this clean"));
  });
}
function prefsHelp() {
  openSheet((body, done) => {
    body.append(h("h2", { style: "font-size:18px" }, "Cleaning Preferences"),
      h("p", {}, h("b", {}, "Vac & Mop"), " vacuums and mops in one pass. ", h("b", {}, "Vac then Mop"), " vacuums the whole area first, then mops it. ", h("b", {}, "Mop Only"), " skips vacuuming."),
      h("p", {}, h("b", {}, "Light"), ", ", h("b", {}, "Standard"), ", and ", h("b", {}, "Deep"), " set suction, liquid, and passes together. Change any of them below to make a Custom setting."),
      h("p", { class: "muted" }, "Tap a selected room on the map to give it its own preferences."),
      h("button", { class: "bigbtn", on: { click: () => done(null) } }, "Got it"));
  });
}

// Pause (or resume), skip room, and end cleaning; on the map sheet and on Home.
function cleanControls(size) {
  const paused = isPaused();
  return h("div", { class: "rbtns" + (size ? " " + size : ""), style: size ? null : "margin-top:16px" },
    h("button", { class: "rbtn go", "aria-label": paused ? "Resume" : "Pause", on: { click: e => api(paused ? "resume" : "pause", {}, { button: e.currentTarget }) } }, icon(paused ? "play" : "pause")),
    h("button", { class: "rbtn outline", "aria-label": "Skip room", disabled: paused, on: { click: e => api("skip", {}, { button: e.currentTarget }) } }, icon("skip")),
    h("button", { class: "rbtn outline danger", "aria-label": "End cleaning", on: { click: async () => {
      if (await ask({ title: "End cleaning?", text: "Roomba will stop and return to its dock.", ok: "End cleaning", danger: true })) {
        if (await api("stop", {}, { silent: true })) api("dock", {});
      }
    } } }, icon("x")));
}

// Live progress while Roomba works, like the official app: "Kitchen: Vacuum",
// then a timeline of the rooms with time spent and time still to go.
function progressView() {
  const m = mission(), paused = isPaused(), map = currentMap(), trail = currentTrail();
  const id = m.missionId || m.nMssn, now = Date.now() / 1000;
  const plan = S.plan && (S.plan.mission === id || (!S.plan.mission && now - (S.plan.started || 0) < 600)) ? S.plan : null;
  const visits = roomVisits(trail, map);
  // Where Roomba is now; between rooms, the last room it was in.
  const last = visits.order.length ? map.rooms.find(r => r.id === visits.order[visits.order.length - 1]) : null;
  const here = (trail && trail.pos ? roomAt(map, trail.pos) : null) || last || null;
  const modeName = MISSION_MODES[m.operatingMode] || "";
  const rooms = plan ? plan.rooms : visits.order.map(rid => ({ id: rid, name: roomNameOf(map, rid) }));
  const hereIndex = here ? rooms.findIndex(r => r.id === here.id) : -1;
  const title = paused ? "Paused" : here ? here.name + ": " + (modeName || "Cleaning")
    : (PHASES[m.phase] || "Working") + "…";
  const started = m.mssnStrtTm ? new Date(m.mssnStrtTm * 1000) : plan && plan.started ? new Date(plan.started * 1000) : null;
  const fmt = d => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const mins = secs => Math.max(1, Math.round(secs / 60)) + " min";
  const areas = map ? estimateAreas(map.id, rooms.map(r => r.id)) : null;
  const mark = kind => h("span", { class: "mark" + (kind === "end" ? " small" : "") }, markIcon(kind));
  const row = (kind, name, right, sub) => h("div", { class: "row " + kind }, mark(kind), h("b", {}, name), h("span", { class: "t" }, right || ""),
    sub ? h("div", { class: "sub" }, sub) : null);
  return h("div", {},
    h("div", { class: "ptitle" }, title),
    cleanControls(),
    h("div", { class: "ptl" },
      row("end", "Start", started ? fmt(started) : ""),
      rooms.map((r, i) => {
        const v = visits.rooms[r.id];
        const current = here && here.id === r.id;
        const done = !current && v && (hereIndex < 0 || i < hereIndex || !plan);
        const rows = areas && areas[r.id];
        const right = current && v ? "Elapsed " + mins(now - v.first)
          : done ? mins(v.last - v.first)
          : rows ? "~ " + mins(roomSeconds(rows, sendable(Object.assign({ mode: MapUI.global.mode }, r.prefs || {})))) : "";
        return row(current ? "now" : done ? "done" : "next", r.name, right, r.prefs ? prefsText(r.prefs) : modeName);
      }),
      rooms.length ? null : row("now", m.cycle === "spot" ? "Spot" : "Cleaning", m.sqft ? m.sqft + " sq ft so far" : "", modeName),
      row("end", "End", "")));
}
// When the path first and last entered each room. Worked out once per point.
function roomVisits(trail, map) {
  if (!trail || !map) return { rooms: {}, order: [] };
  if (!trail._v || trail._v.map !== map.id) trail._v = { map: map.id, n: 0, rooms: {}, order: [] };
  const v = trail._v;
  for (; v.n < trail.points.length; v.n++) {
    const p = trail.points[v.n], r = roomAt(map, p);
    if (!r) continue;
    if (!v.rooms[r.id]) { v.rooms[r.id] = { first: p[2], last: p[2] }; v.order.push(r.id); }
    else v.rooms[r.id].last = p[2];
  }
  return v;
}
function markIcon(kind) {
  const s = document.createElementNS(SVGNS, "svg");
  s.setAttribute("viewBox", "0 0 16 16");
  s.innerHTML = {
    end: '<circle cx="8" cy="8" r="6" fill="#222"/>',
    done: '<circle cx="8" cy="8" r="7" fill="#222"/><path d="M4.8 8.2l2.2 2.2 4.2-4.4" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    now: '<circle cx="8" cy="8" r="6.6" fill="#fff" stroke="#222" stroke-width="1.4"/><path d="M8 8V2.2A5.8 5.8 0 0 1 13.8 8z" fill="#222"/>',
    next: '<circle cx="8" cy="8" r="6.4" fill="#fff" stroke="#222" stroke-width="1.6"/>',
  }[kind];
  return s;
}

// =====================================================================
// Map menu: Edit Map | Map Display
function mapMenu(tab) {
  const map = currentMap();
  if (!map) { toast("No map yet.", true); return; }
  tab = tab || "edit";
  openSheet((body, done) => {
    const draw = () => {
      const tabs = h("div", { class: "tabs2" }, [["edit", "Edit Map"], ["display", "Map Display"]].map(([k, l]) =>
        h("button", { class: tab === k ? "on" : "", on: { click: () => { tab = k; draw(); } } }, l)));
      body.replaceChildren(tabs, tab === "edit" ? editTab(map, done) : displayTab());
    };
    draw();
  });
}
function editTab(map, done) {
  const svg = document.createElementNS(SVGNS, "svg");
  const prev = h("div", { class: "preview" }, svg, h("button", { class: "rot", "aria-label": "Rotate map", on: { click: async () => {
    done(null);
    const v = await openSheet((b, d) => {
      let deg = 90;
      b.append(h("h2", { style: "font-size:18px" }, "Rotate map"),
        h("div", { class: "drops", style: "grid-template-columns:repeat(4,1fr)" }, [90, 180, 270].map(x => h("button", { class: "btn-out", on: { click: () => d(x) } }, x + "°"))),
        h("button", { class: "bigbtn outline", on: { click: () => d(null) } }, "Cancel"));
    });
    if (v) api("map_rotate", { map_id: map.id, degrees: v });
  } } }, icon("rotate", 18)));
  requestAnimationFrame(() => drawMap(svg, map, { small: true }));
  const tool = (label, pic, fn) => h("button", { class: "tool", on: { click: () => { done(null); setTimeout(fn, 250); } } }, img(pic, ""), label);
  return h("div", {}, prev,
    h("div", { class: "toolgrid" },
      tool("Merge", "map_edit_room_merge", () => go("mapedit/merge")),
      tool("Split", "map_edit_room_split", () => go("mapedit/split")),
      tool("Rename", "map_edit_room_rename", () => go("mapedit/rename")),
      tool("Keep Out Zones", "mapEdit_keep_out_btn", () => go("zones")),
      tool("Add Clean Zones", "mapEdit_clean_zone_btn", () => go("cleanzones")),
      tool("Carpets", "map_edit_carpet", () => go("carpets")),
      tool("Furniture", "map_edit_furniture", () => go("furniture")),
      tool("Edit Map Name", "deviceHome_map_rename", async () => {
        const n = await askText({ title: "Map name", value: map.name });
        if (n && n !== map.name) api("map_rename", { map_id: map.id, name: n });
      })),
    h("button", { class: "bigbtn danger", on: { click: async () => {
      done(null);
      const w = await ask({ title: "Delete " + map.name + "?", text: "Roomba will have to learn your home again.", ok: "Delete map", danger: true, typed: "DELETE" });
      if (w) { await api("map_delete", { map_id: map.id, confirm: w }); go("home"); }
    } } }, h("span", { style: "display:inline-flex;gap:7px;align-items:center" }, icon("trash", 16), "Delete")));
}
function displayTab() {
  const key = (label, fill, line, kind) => h("div", { class: "keyrow" },
    kind === "img" ? img("object_other", "swatch") : h("span", { class: "swatch" + (kind === "line" ? " line" : ""), style: `background:${fill};${kind === "box" ? "border:2px solid " + line : ""};--l:${line}` }), label);
  const k = h("div", {},
    h("div", { class: "dispcard" }, img("home_sheet_action_pathDensity_2_n"), h("span", { class: "grow" }, "Cleaning Path"),
      toggle(MapDisplay.paths, on => { MapDisplay.save("paths", on); })),
    h("div", { class: "dispcard" }, img("map_legend_obstacles"), h("span", { class: "grow" }, "Obstacles"),
      toggle(MapDisplay.hazards, on => { MapDisplay.save("hazards", on); })),
    h("div", { class: "dispcard" }, img("map_edit_furniture"), h("span", { class: "grow" }, "Furniture"),
      toggle(MapDisplay.furniture, on => { MapDisplay.save("furniture", on); })),
    h("h3", {}, "Map Keys"),
    key("Vacuum", "#bfe3cf", "#3e9a6a", "line"), key("Mop Only", "#cfe2fb", "#2f7de0", "line"), key("Vac & Mop", "#d2eef3", "#2b8fa3", "line"),
    key("Keep Out Zone", "var(--keepout)", "var(--keepout-line)", "box"), key("No-Mop Zone", "var(--nomop)", "var(--nomop-line)", "box"),
    key("Temporary obstacles", "", "", "img"));
  return k;
}

// =====================================================================
// Map edit modes: merge, split, rename
SCREENS.mapedit = { uses: ["map"], render(mode) {
  const map = currentMap();
  const el = h("div", { class: "mapscreen" });
  const svg = document.createElementNS(SVGNS, "svg");
  const hint = h("div", { class: "edithint" });
  const save = h("button", { class: "bigbtn", disabled: true }, "Save");
  let picked = [], line = [];
  const titles = { merge: "Merge Rooms", split: "Split Room", rename: "Rename Room" };
  const draw = () => {
    const m = currentMap();
    drawMap(svg, m, { sel: picked, badges: false,
      onRoom: (id, pt) => {
        if (mode === "split" && picked.length === 1 && picked[0] === id) { addPoint(pt); return; }
        if (mode === "merge") picked = picked.includes(id) ? picked.filter(x => x !== id) : [...picked, id];
        else { picked = [id]; line = []; }
        refresh();
      },
      onBackground: mode === "split" && picked.length ? pt => addPoint(pt) : null,
      extra: (world, px) => {
        line.forEach(p => world.append(sv("circle", { cx: p[0], cy: p[1], r: 9 * px, class: "m-draft" })));
        if (line.length === 2) world.append(sv("line", { x1: line[0][0], y1: line[0][1], x2: line[1][0], y2: line[1][1], class: "m-split", "stroke-width": 3 * px }));
      } });
  };
  const addPoint = pt => { line = line.length >= 2 ? [pt] : [...line, pt]; refresh(); };
  const refresh = () => {
    draw();
    if (mode === "merge") { hint.textContent = picked.length < 2 ? "Tap two or more neighboring rooms to merge." : picked.map(id => roomNameOf(map, id)).join(" + "); save.disabled = picked.length < 2; }
    if (mode === "split") { hint.textContent = !picked.length ? "Tap the room to split." : line.length < 2 ? "Tap two points to draw the dividing line." : "Save to split " + roomNameOf(map, picked[0]) + "."; save.disabled = line.length < 2; }
    if (mode === "rename") { hint.textContent = picked.length ? "Tap Save to rename " + roomNameOf(map, picked[0]) + "." : "Tap the room to rename."; save.disabled = !picked.length; }
  };
  save.addEventListener("click", async () => {
    let ok = null;
    if (mode === "merge") ok = await api("merge", { map_id: map.id, room_ids: picked }, { button: save });
    if (mode === "split") ok = await api("split", { map_id: map.id, room_id: picked[0], points: line }, { button: save });
    if (mode === "rename") {
      const name = await renameSheet(roomNameOf(map, picked[0]));
      if (name) ok = await api("rename", { map_id: map.id, room_id: picked[0], name }, { button: save });
    }
    if (ok) goBack("map");
  });
  el.append(h("div", { class: "edithead" }, h("button", { class: "back", on: { click: () => goBack("map") } }, icon("back")), h("h1", {}, titles[mode] || "Edit Map"), h("div", { style: "width:29px" })),
    h("div", { class: "mapwrap", style: "margin-top:0" }, svg), hint, h("div", { class: "editfoot" }, save));
  el._mounted = refresh;
  el._update = () => { picked = []; line = []; refresh(); };
  return el;
} };
function renameSheet(value) {
  const presets = [["Kitchen", "map_edit_room_rename_kitchen"], ["Living Room", "map_edit_room_rename_living_room"], ["Bedroom", "map_edit_room_rename_bedroom"],
    ["Bathroom", "map_edit_room_rename_toilet"], ["Dining Room", "map_edit_room_rename_dining_room"], ["Office", "map_edit_room_rename_office"],
    ["Hallway", "map_edit_room_rename_passage_way"], ["Laundry", "map_edit_room_rename_laundry"]];
  return askText({ title: "Room name", value, extra: (body, set) => body.append(h("div", { class: "namegrid" },
    presets.map(([n, pic]) => h("button", { on: { click: () => set(n) } }, img(pic, ""), n)))) });
}

// =====================================================================
// Keep-out and no-mop zone editor
// Area editors: keep-out and no-mop zones, clean zones, and carpets share one
// drag-and-resize editor; each saves its own list.
const AREA_EDITORS = {
  zones: { title: "Edit Keep Out Zone", action: "zones_set", field: "zones",
    items: map => map.zones.filter(z => z.kind !== "wall").map(z => ({ id: z.id, kind: z.kind, points: z.points })),
    chips: [["keepout", "map_legend_nogo_zone", "Keep Out Zone"], ["nomop", "map_legend_mop_restrict_zone", "No-Mop Zone"]],
    help: ["Keep Out Zones stop Roomba from going into an area. No-Mop Zones let it vacuum there but not mop."] },
  cleanzones: { title: "Clean Zones", action: "clean_zones_set", field: "zones",
    items: map => (map.clean_zones || []).map(z => ({ id: z.id, kind: "clean", name: z.name, points: z.points })),
    chips: [["clean", "mapEdit_clean_zone_btn", "Add Clean Zone"]], named: true,
    help: ["Clean zones are areas you can send Roomba to clean on their own, like under the table. They show up under Rooms & Zones on the map."] },
  carpets: { title: "Carpets", action: "carpets_set", field: "carpets",
    items: map => (map.carpets || []).filter(c => c.user).map(c => ({ id: c.id, kind: "carpet", points: c.points })),
    chips: [["carpet", "map_edit_carpet", "Add Carpet"]],
    help: ["Mark carpets so Roomba won't mop them and boosts suction there. Carpets Roomba found on its own stay as they are."] },
  furniture: { title: "Edit Furniture", action: "furniture_set", field: "furniture",
    items: map => (map.furniture || []).map(f => ({ id: f.id, kind: "furniture", name: f.type, points: f.points })),
    chips: [["furniture", "map_edit_furniture", "Add Furniture"]],
    // Pick the type first, like the official app; the piece starts at a typical size.
    onAdd: ed => pickSheet("Add Furniture", "Select the furniture you would like to add to your map.",
      FURNITURE_TYPES.map(([t]) => [t, furnitureName(t)]), null, t => ed.add("furniture", t, (FURNITURE_TYPES.find(x => x[0] === t) || [])[1])),
    toBody: items => items.map(i => ({ id: i.id, type: i.name, points: i.points })),
    help: ["Once furniture pieces are saved to your map, you'll be able to add them to your routines to tell Roomba to clean around them.",
      "Tap Add Furniture, pick what it is, then drag it into place and resize it to match."] },
};
// [type, typical size in meters]
const FURNITURE_TYPES = [["bed", [1.6, 2.0]], ["sofa", [2.0, 0.9]], ["left_l_sofa", [2.4, 1.6]], ["right_l_sofa", [2.4, 1.6]],
  ["living_chair", [0.8, 0.8]], ["dining_table", [1.6, 0.9]], ["coffee_table", [1.1, 0.6]], ["sidetable", [0.5, 0.5]],
  ["tvcabinet", [1.6, 0.45]], ["cabinet", [1.0, 0.5]], ["refrigerator", [0.8, 0.75]], ["washingmachineordryer", [0.7, 0.7]],
  ["toilet", [0.45, 0.7]], ["litter_box", [0.5, 0.6]], ["pet_bowl", [0.3, 0.3]], ["pet_bed", [0.7, 0.6]],
  ["pet_feeder", [0.35, 0.35]], ["cat_tower", [0.6, 0.6]]];
for (const [key, cfg] of Object.entries(AREA_EDITORS)) SCREENS[key] = { render() {
  const map = currentMap();
  const el = h("div", { class: "mapscreen" });
  const svg = document.createElementNS(SVGNS, "svg");
  const save = h("button", { class: "bigbtn", disabled: true }, "Save");
  let ed = null;
  save.addEventListener("click", async () => {
    let items = ed.result();
    if (cfg.named) for (const it of items) if (!it.name) {
      it.name = await askText({ title: "Name this zone", value: "Zone " + (items.indexOf(it) + 1) }) || "Zone";
    }
    if (await api(cfg.action, { map_id: map.id, [cfg.field]: cfg.toBody ? cfg.toBody(items) : items }, { button: save })) goBack("map");
  });
  el.append(
    h("div", { class: "edithead" }, h("button", { class: "back", on: { click: async () => {
      if (ed && ed.changed() && !(await ask({ title: "Discard changes?", ok: "Discard", danger: true }))) return;
      goBack("map");
    } } }, icon("back")), h("h1", {}, cfg.title),
      h("button", { class: "iconbtn", "aria-label": "Help", on: { click: () => openSheet((b, d) => b.append(h("h2", { style: "font-size:18px" }, cfg.title),
        cfg.help.map(t => h("p", {}, t)),
        h("p", { class: "muted" }, "Drag an area to move it, and drag the green dots to resize it. The X removes it. Nothing changes until you tap Save."),
        h("button", { class: "bigbtn", on: { click: () => d(null) } }, "Got it"))) } }, icon("question", 20))),
    h("div", { class: "mapwrap", style: "margin-top:0" }, svg),
    h("div", { class: "editfoot" },
      h("div", { class: "chips2" }, cfg.chips.map(([kind, pic, label]) => h("button", { class: "chip2", on: { click: () => cfg.onAdd ? cfg.onAdd(ed) : ed.add(kind) } }, img(pic), label))),
      save));
  el._mounted = () => { ed = zoneEditor(svg, map, () => { save.disabled = !ed.changed(); }, { items: cfg.items(map), hide: key }); };
  return el;
} };

// =====================================================================
// Robot page and settings
SCREENS.robot = { uses: ["status"], render() {
  const el = h("div", { class: "screen plain" });
  const draw = () => {
    const b = battery();
    el.replaceChildren(topbar(robotName(), { center: true }),
      h("div", { class: "center" }, h("span", { class: "capsule", style: "padding:7px 13px" }, icon("bolt", 13), b == null ? "--" : b + "%")),
      img("robot", "robotimg"),
      h("div", { class: "robotstate" }, stateText()),
      h("div", { class: "list" },
        li("Robot Settings", () => go("settings")),
        li("Robot Health", () => go("health"), healthSummary()),
        li("History", () => go("history")),
        li("About Robot", () => go("about")),
        li("Factory Reset & Remove Robot", () => factoryReset())),
      h("div", { class: "rbtns", style: "justify-content:center;margin-top:7px" },
        h("button", { class: "rbtn outline", "aria-label": "Send home", title: "Dock", on: { click: e => api("dock", {}, { button: e.currentTarget }) } }, img("deviceHome_bottomView_recharge")),
        h("button", { class: "rbtn outline", "aria-label": "Find robot", title: "Find", on: { click: e => api("find", {}, { button: e.currentTarget }) } }, img("deviceHome_base_station")),
        hasAutoEmpty() ? h("button", { class: "rbtn outline", "aria-label": "Empty bin", title: "Empty bin", on: { click: e => api("evac", {}, { button: e.currentTarget }) } }, img("home_sheet_station_dust")) : null),
      h("p", { class: "center muted", style: "font-size:9px" }, hasAutoEmpty() ? "Dock · Find · Empty Bin" : "Dock · Find"));
  };
  draw(); el._update = draw;
  return el;
} };
function li(label, fn, meta, right) {
  return h("div", { class: "li tap", on: { click: fn } }, h("div", { class: "grow" }, label, meta ? h("div", { class: "meta" }, meta) : null), right || chevron());
}
function robotNameRow() {
  return li("Robot Name", async () => {
    const n = await askText({ title: "Robot name", value: robotName() });
    if (n && n !== robotName()) api("set_setting", { key: "name", value: n });
  }, robotName());
}

// Robot Settings, laid out like the official list. Each row shows only when
// the robot reports that setting. Values come from the robot's settings
// shadow; dotted keys ("audio.volume") are nested there.
const SUCTION_CHOICES = [[1, "Quiet"], [2, "Standard"], [3, "Turbo"], [4, "MAX"]];
const WETNESS_CHOICES = [[1, "Low"], [2, "Standard"], [3, "High"]];
const EVAC_CHOICES = [[0, "After Every Routine"], [1, "After Every 2 Routines"], [2, "After Every 3 Routines"], [4, "Every time returning to the dock"],
  [10, "Every 10 m²"], [15, "Every 15 m²"], [25, "Every 25 m²"], [30, "Every 30 m²"], [50, "Every 50 m²"]];
const DRY_CHOICES = [[2, "2 Hours"], [3, "3 Hours"], [4, "4 Hours"], [5, "5 Hours"], [6, "6 Hours"]];
const SETTING_ROWS = [
  // [section, key, label, hint, kind, choices]. "readonly": the robot
  // reports it, but the official app never writes it (it isn't among the
  // 27 settings the app sets, see decomp/README.md), so it's shown, not changed.
  ["Cleaning", "suctionLevel", "Suction Power", "Used when a clean doesn't say otherwise", "choice", SUCTION_CHOICES],
  ["Cleaning", "padWetness.padPlate", "Mop Liquid Amount", "How wet the mop pad gets", "choice", WETNESS_CHOICES],
  ["Cleaning", "twoPass", "Two Passes", "Clean every area twice", "switch"],
  ["Cleaning", "swScrub", "SmartScrub", "Extra back-and-forth scrubbing on mopped floors", "switch"],
  ["Cleaning", "carpetBoost", "Carpet Boost", "More suction on carpets", "switch"],
  ["Cleaning", "odoaMode", "Obstacle Avoidance", "Steer around objects Roomba recognizes", "choice", [[0, "Off"], [1, "On"], [2, "Cautious"]]],
  ["Cleaning", "noAutoPasses", "Fixed Passes", "Don't add passes automatically", "readonly"],
  ["Cleaning", "vacHigh", "High Suction", "Always use maximum suction", "readonly"],
  ["Dock", "evacAllowed", "Auto-Empty", "Empty the bin at the dock", "readonly"],
  ["Dock", "autoevacFreq", "Auto-Empty Frequency", "", "choice", EVAC_CHOICES],
  ["Dock", "padWashAllowed", "Mop Auto-Wash", "Wash the mop pad at the dock", "switch"],
  ["Dock", "pwHeat", "Water Temperature", "", "choice", [[0, "Normal"], [1, "Warm"], [2, "Hot"]]],
  ["Dock", "pwTimeInterval", "Mop Wash Frequency", "Minutes between washes", "number"],
  ["Dock", "pwAreaInterval", "Mop Wash Area", "Area between washes", "number"],
  ["Dock", "padDryAllowed", "Mop Drying", "Dry the mop pad at the dock", "readonly"],
  ["Dock", "padDryDur", "Mop Dry Duration", "", "choice", DRY_CHOICES],
  ["Dock", "detergent", "Cleaning Solution", "Dispense StayClean™ solution automatically", "switch"],
  ["Dock", "sanitizationMode", "Sanitize", "", "switch"],
  ["Robot", "childLock", "Child Lock", "Lock the robot's buttons so kids and pets can't start it", "switch"],
  ["Robot", "audio.volume", "Volume", "", "volume"],
  ["Robot", "langs2.sLang", "Language", "Roomba must be on its dock to change voice language", "language"],
  ["Robot", "ecoCharge", "Eco Charging", "Charge more slowly to save energy", "readonly"],
  ["Robot", "schedHold", "Schedules Paused", "Scheduled cleans are on hold", "readonly"],
  ["Robot", "mapUploadAllowed", "Mapping Features", "Let Roomba save and upload maps", "switch"],
];
function dig(obj, key) { return key.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj); }
function robotSettings() { return (S.sections.settings && S.sections.settings.data) || {}; }
async function writeSetting(key, value, label) {
  const ok = await api("set_setting", { key, value }, { silent: true });
  if (!ok) return false;
  const now = dig(robotSettings(), key);
  if (JSON.stringify(now) !== JSON.stringify(value)) toast(label + " was sent, but the robot still reports " + JSON.stringify(now) + ".", true);
  else toast(label + " saved.");
  return true;
}
function settingRow([, key, label, hint, kind, choices]) {
  const v = dig(robotSettings(), key);
  if (v === undefined) return null;
  if (kind === "readonly") return h("div", { class: "li" }, h("div", { class: "grow" }, label,
    h("div", { class: "meta" }, (hint ? hint + ". " : "") + "Set by Roomba; the iRobot app can't change it either.")),
    h("span", { class: "meta" }, v === true || v === 1 ? "On" : v === false || v === 0 ? "Off" : String(v)));
  if (kind === "switch") return h("div", { class: "li" }, h("div", { class: "grow" }, label, hint ? h("div", { class: "meta" }, hint) : null),
    toggle(v === true || v === 1, async (on, input) => {
      input.disabled = true;
      if (!(await writeSetting(key, typeof v === "number" ? (on ? 1 : 0) : on, label))) input.checked = !on;
      input.disabled = false;
    }));
  const shown = kind === "choice" ? ((choices.find(([c]) => c === v) || [v, String(v)])[1])
    : kind === "volume" ? (v > 10 ? v + "%" : v + " of 10") : kind === "language" ? langName(v) : String(v);
  return li(label, () => {
    if (kind === "choice") return pickSheet(label, hint, choices, v, n => writeSetting(key, n, label));
    if (kind === "volume") return volumeSheet(v, n => writeSetting(key, n, label));
    if (kind === "language") return languageSheet(v, n => writeSetting(key, n, label));
    askText({ title: label, value: String(v) }).then(t => { if (t != null && t !== "" && !isNaN(Number(t))) writeSetting(key, Number(t), label); });
  }, shown);
}
function pickSheet(title, hint, choices, value, onPick) {
  openSheet((body, done) => {
    body.append(h("h2", { style: "font-size:18px" }, title), hint ? h("p", { class: "muted" }, hint) : null,
      h("div", { class: "list" }, choices.map(([c, l]) => h("div", { class: "li tap", on: { click: () => { done(c); onPick(c); } } },
        h("div", { class: "grow" }, l), c === value ? icon("chev") : null))),
      h("button", { class: "bigbtn outline", on: { click: () => done(null) } }, "Cancel"));
  });
}
function volumeSheet(value, onSet) {
  openSheet((body, done) => {
    // Some robots count volume 0 to 10, others 0 to 100.
    const max = value > 10 ? 100 : 10, unit = max === 100 ? "%" : " of 10";
    const out = h("b", {}, value + unit);
    const range = h("input", { type: "range", min: 0, max, step: max / 10, value, style: "width:100%",
      on: { input: () => { out.textContent = range.value + unit; } } });
    body.append(h("h2", { style: "font-size:18px" }, "Volume"), h("p", { class: "center" }, out), range,
      h("button", { class: "bigbtn", on: { click: () => { done(true); onSet(Number(range.value)); } } }, "Save"),
      h("button", { class: "bigbtn outline", on: { click: () => done(null) } }, "Cancel"));
  });
}
const LANG_NAMES = { "en-US": "English (United States)", "en-GB": "English (United Kingdom)", "es-ES": "Español (España)", "es-XL": "Español (XL)",
  "fr-FR": "Français (France)", "fr-CA": "Français (Canada)", "de-DE": "Deutsch (Deutschland)", "it-IT": "Italiano (Italia)", "pt-BR": "Português (Brasil)",
  "pt-PT": "Português", "nl-NL": "Nederlands", "sv-SE": "Svenska", "da-DK": "Dansk", "no-NO": "Norsk", "fi-FI": "Suomi", "pl-PL": "Polski",
  "cs-CZ": "Čeština", "ru-RU": "Русский", "tr-TR": "Türkçe", "ja-JP": "日本語（日本）", "zh-CN": "普通话", "zh-HK": "粤语", "zh-TW": "國語", "he-IL": "עברית", "ar-SA": "العربية" };
function langName(code) { if (typeof code !== "string") return String(code); return LANG_NAMES[code] || LANG_NAMES[code.replace("_", "-")] || code; }
function languageSheet(value, onSet) {
  const l = dig(robotSettings(), "langs2") || {};
  // dLangs is what the robot can speak; fall back to the common ones.
  const raw = l.dLangs && (Array.isArray(l.dLangs) ? l.dLangs : l.dLangs.langs || Object.keys(l.dLangs));
  const codes = (raw && raw.length ? raw : Object.keys(LANG_NAMES)).map(x => typeof x === "string" ? x : x.lang || x.name).filter(Boolean);
  pickSheet("Language", "Roomba must be on its dock to change its voice language.", codes.map(c => [c, langName(c)]), value, onSet);
}
function hasAutoEmpty() { const c = (status().cap || {}); return Number(c.autoevac) > 0 || "evacAllowed" in robotSettings(); }

SCREENS.settings = { uses: ["settings", "dnd", "map"], render() {
  const el = h("div", { class: "screen plain" });
  const draw = () => {
    const sections = [];
    for (const name of ["Cleaning", "Dock", "Robot"]) {
      const rows = SETTING_ROWS.filter(r => r[0] === name).map(settingRow).filter(Boolean);
      if (name === "Robot") rows.unshift(robotNameRow());
      if (rows.length) sections.push(h("div", { class: "sect" }, name), h("div", { class: "list" }, rows));
    }
    el.replaceChildren(topbar("Robot Settings", { center: true }), sections,
      h("div", { class: "sect" }, "Maps"),
      h("div", { class: "list" }, li("Map Manager", () => go("maps"), plural(allMaps().length, "map")), li("Edit Map", () => mapMenu("edit"))),
      h("div", { class: "sect" }, "Do Not Disturb"),
      h("div", { class: "list" }, li("Quiet hours", () => dndSheet(), dndText())),
      h("div", { class: "sect" }, "Device"),
      h("div", { class: "list" },
        li("Locate", () => api("find", {}), "Roomba plays a sound so you can find it"),
        li("Restart Robot", async () => {
          if (await ask({ title: "Restart " + robotName() + "?", text: "Roomba turns off and back on. It stops any clean in progress and goes offline for a minute or two.", ok: "Restart", danger: true }))
            api("restart", { confirm: "RESTART" });
        }),
        li("About Robot", () => go("about")),
        li("Factory Reset & Remove Robot", () => factoryReset())),
      h("div", { class: "sect" }, "Advanced"),
      h("div", { class: "list" }, li("All robot settings", () => rawSettings())),
      h("div", { class: "sect" }, "This app"),
      h("div", { class: "list" }, li("Notifications", () => go("messages")), li("Login and connection", () => go("account"))));
  };
  draw(); el._update = draw;
  return el;
} };
function hm(m) { const d = new Date(); d.setHours(Math.floor(m / 60), m % 60, 0, 0); return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
function dndText() {
  const d = (S.sections.dnd && S.sections.dnd.data) || {}, parts = [];
  if (d.daily_start != null && d.daily_end != null && d.daily_start !== d.daily_end) parts.push("Every day, " + hm(d.daily_start) + " to " + hm(d.daily_end));
  if (d.ends_at && d.ends_at > Date.now()) parts.push("Until " + new Date(d.ends_at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }));
  return parts.join(" · ") || "Off";
}
function dndSheet() {
  const d = (S.sections.dnd && S.sections.dnd.data) || {};
  const toHHMM = m => String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
  let kind = "daily";
  openSheet((body, done) => {
    const start = h("input", { class: "field", type: "time", value: d.daily_start != null ? toHHMM(d.daily_start) : "22:00" });
    const end = h("input", { class: "field", type: "time", value: d.daily_end != null ? toHHMM(d.daily_end) : "07:00" });
    const until = h("input", { class: "field", type: "datetime-local" });
    const daily = h("div", { class: "drops" }, h("label", {}, h("span", { class: "muted" }, "From"), start), h("label", {}, h("span", { class: "muted" }, "To"), end));
    const once = h("label", { class: "hidden" }, h("span", { class: "muted" }, "Quiet until"), until);
    const save = h("button", { class: "bigbtn", on: { click: async () => {
      let ok;
      if (kind === "daily") ok = await api("set_dnd", { kind: "daily", start: start.value, end: end.value }, { button: save });
      else { if (!until.value) { toast("Pick when quiet time should end.", true); return; } ok = await api("set_dnd", { kind: "until", ends_at: new Date(until.value).getTime() }, { button: save }); }
      if (ok) done(true);
    } } }, "Save");
    body.append(h("h2", { style: "font-size:18px" }, "Do Not Disturb"), h("p", { class: "muted" }, "Roomba won't start scheduled or automatic jobs during quiet hours."),
      pills([["daily", () => h("span", {}, "Every day")], ["until", () => h("span", {}, "Just for now")]].map(([v, f]) => [v, on => { const s = f(); s.style.color = on ? "#fff" : ""; s.style.fontWeight = 600; return s; }]), kind, v => {
        kind = v; daily.classList.toggle("hidden", v !== "daily"); once.classList.toggle("hidden", v === "daily");
      }), h("div", { style: "height:11px" }), daily, once, save, h("button", { class: "bigbtn outline", on: { click: () => done(null) } }, "Cancel"));
  });
}
function rawSettings() {
  const s = (S.sections.settings && S.sections.settings.data) || {};
  openSheet((body, done) => {
    const key = h("input", { class: "field", placeholder: "Setting name, e.g. childLock", autocomplete: "off" });
    const val = h("input", { class: "field", placeholder: 'Value as JSON, e.g. true, 3, or "text"', autocomplete: "off" });
    body.append(h("h2", { style: "font-size:18px" }, "All robot settings"),
      h("pre", { style: "background:#f2f2ef;border-radius:9px;padding:9px;font-size:8px;overflow:auto;max-height:40vh" }, JSON.stringify(s, null, 2)),
      h("h3", {}, "Write a setting"), key, val,
      h("button", { class: "bigbtn", on: { click: async () => {
        let value; try { value = JSON.parse(val.value); } catch (e) { toast('The value must be JSON, like true, 3, or "text".', true); return; }
        if (!key.value.trim()) return;
        if (await api("set_setting", { key: key.value.trim(), value })) done(true);
      } } }, "Write setting"),
      h("button", { class: "bigbtn outline", on: { click: () => done(null) } }, "Close"));
  });
}
async function factoryReset() {
  let wipe = false;
  const word = await openSheet((body, done) => {
    const input = h("input", { class: "field", placeholder: "Type RESET to confirm", autocomplete: "off", autocapitalize: "characters" });
    const go2 = h("button", { class: "bigbtn danger solid", disabled: true, on: { click: () => done(input.value.trim()) } }, "Factory reset");
    input.addEventListener("input", () => { go2.disabled = input.value.trim() !== "RESET"; });
    body.append(h("h2", { style: "font-size:18px" }, "Factory reset?"),
      h("p", { class: "muted" }, "Roomba will probably go offline and need setting up again in the iRobot app. This app will lose contact until then."),
      h("div", { class: "li", style: "padding:9px 0" }, h("div", { class: "grow" }, "Also wipe the robot's data", h("div", { class: "meta" }, "Maps and history on the robot")), toggle(false, v => { wipe = v; })),
      input, go2, h("button", { class: "bigbtn outline", on: { click: () => done(null) } }, "Cancel"));
  });
  if (word === "RESET") api("factory_reset", { confirm: word, wipe });
}

// =====================================================================
// Routines and schedules
SCREENS.routines = { uses: ["routines", "schedules"], render() {
  const el = h("div", { class: "screen plain" });
  const draw = () => {
    const routines = (S.sections.routines && S.sections.routines.routines) || [];
    const schedules = (S.sections.schedules && S.sections.schedules.schedules) || [];
    el.replaceChildren(topbar("Routines"),
      h("div", { class: "list" },
        routines.length ? routines.map(r => h("div", { class: "li tap", on: { click: () => editRoutine(r) } },
          h("span", { class: "disc", style: `width:30px;height:30px;border-radius:50%;display:grid;place-items:center;background:${(r.color || "#e4f0c9")}33` }, img("deviceHome_bottomView_sweep_icon")),
          h("div", { class: "grow" }, r.name, h("div", { class: "meta" }, r.rooms.length ? r.rooms.map(x => roomNameOf(currentMap(), x.id)).join(", ") : "All rooms")),
          h("button", { class: "rbtn go", style: "width:32px;height:32px", "aria-label": "Run", on: { click: e => { e.stopPropagation(); reviewRoutine(r); } } }, icon("play", 15))))
        : h("div", { class: "li muted" }, "No routines yet. Pick rooms on the map and tap the heart to save one.")),
      h("button", { class: "bigbtn outline", on: { click: () => editRoutine(null, []) } }, "New routine"),
      h("div", { class: "sect", style: "margin-top:22px" }, "Schedules"),
      h("div", { class: "list" },
        schedules.length ? schedules.map(s => h("div", { class: "li tap", on: { click: () => editSchedule(s) } },
          h("div", { class: "grow" }, timeText(s.hour, s.minute), h("div", { class: "meta" }, s.name + " · " + daysText(s.days) + (s.rooms ? " · " + plural(s.rooms, "room") : " · All rooms"))),
          h("span", { on: { click: e => e.stopPropagation() } }, toggle(s.enabled, async (on, input) => {
            input.disabled = true;
            const ok = await api("toggle_schedule", { schedule_id: s.id, enabled: on }, { silent: true });
            if (!ok) input.checked = !on;
            input.disabled = false;
          }))))
        : h("div", { class: "li muted" }, "No schedules.")),
      h("button", { class: "bigbtn outline", on: { click: () => editSchedule(null, selectionRooms()) } }, "New schedule"));
  };
  draw(); el._update = draw;
  return el;
} };
const DAYNAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
function timeText(hh, mm) { const d = new Date(); d.setHours(hh || 0, mm || 0, 0, 0); return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
function daysText(days) {
  const d = [...(days || [])].sort();
  if (d.length === 7) return "Every day";
  if (d.join() === "1,2,3,4,5") return "Weekdays";
  if (d.join() === "0,6") return "Weekends";
  return d.map(i => DAYNAMES[i] || i).join(", ");
}

// Routine editor: name, rooms in order, and each room's preferences.
function editRoutine(r, rooms) {
  const map = currentMap();
  const isNew = !r;
  let name = r ? r.name : "";
  let list = (r ? r.rooms.map(x => ({ id: x.id, prefs: Object.assign({ mode: x.mode || defaultMode() }, x.prefs || {}) }))
    : (rooms || []).map(x => ({ id: x.id, prefs: Object.assign({}, x.prefs) })));
  openSheet((body, done) => {
    const draw = () => {
      const nameIn = h("input", { class: "field", value: name, maxlength: 40, placeholder: "e.g. After dinner", on: { input: () => { name = nameIn.value; } } });
      body.replaceChildren(h("h2", { style: "font-size:18px" }, isNew ? "New routine" : "Edit routine"), nameIn,
        h("h3", {}, "Rooms, in cleaning order"),
        list.length ? list.map((x, i) => h("div", { class: "roomrow on", style: "padding:11px 12px;cursor:default" },
          h("span", { class: "plus" }, String(i + 1)),
          h("div", { class: "grow" }, roomNameOf(map, x.id), h("div", { class: "meta" }, prefsText(x.prefs))),
          h("button", { class: "tune", "aria-label": "Move up", disabled: i === 0, on: { click: () => { [list[i - 1], list[i]] = [list[i], list[i - 1]]; draw(); } } }, "↑"),
          h("button", { class: "tune", "aria-label": "Preferences", on: { click: () => routineRoomPrefs(x, draw) } }, icon("tune", 15)),
          h("button", { class: "tune", "aria-label": "Remove", on: { click: () => { list.splice(i, 1); draw(); } } }, icon("x", 13))))
          : h("p", { class: "muted" }, "No rooms: the whole house gets cleaned."),
        map ? h("div", { class: "drop" }, h("select", { on: { change: e => { if (e.target.value) { list.push({ id: e.target.value, prefs: Object.assign({}, MapUI.global) }); draw(); } } } },
          h("option", { value: "" }, "Add a room…"), map.rooms.filter(rm => !list.some(x => x.id === rm.id)).map(rm => h("option", { value: rm.id }, rm.name)))) : null,
        h("button", { class: "bigbtn", on: { click: async e => {
          const payload = { name: name.trim(), map_id: map && map.id, rooms: list.map(x => ({ id: x.id, prefs: sendable(x.prefs) })) };
          const ok = isNew ? await api("create_routine", payload, { button: e.currentTarget })
            : await api("update_routine", Object.assign({ favorite_id: r.id }, payload), { button: e.currentTarget });
          if (ok) done(true);
        } } }, isNew ? "Save routine" : "Save changes"),
        isNew ? null : h("button", { class: "bigbtn outline", on: { click: () => { done(null); runRoutine(r); } } }, "Run now"),
        isNew ? null : h("button", { class: "bigbtn danger", on: { click: async () => {
          done(null);
          const w = await ask({ title: "Delete " + r.name + "?", ok: "Delete routine", danger: true, typed: "DELETE" });
          if (w) api("delete_routine", { favorite_id: r.id, confirm: w });
        } } }, "Delete routine"),
        r && !r.editable ? h("p", { class: "muted" }, "This routine has several steps, so only the iRobot app can edit it.") : null);
    };
    draw();
  });
}
function routineRoomPrefs(x, back) {
  // A nested editor: reopen the routine editor afterwards.
  const holder = h("div", {});
  let p = Object.assign({}, x.prefs);
  holder.append(prefsEditor(p, q => { p = q; }));
  const prevBody = Array.from($("sheetbody").childNodes);
  $("sheetbody").replaceChildren(h("h2", {}, roomNameOf(currentMap(), x.id)), holder,
    h("button", { class: "bigbtn", on: { click: () => { x.prefs = p; back(); } } }, "Done"),
    h("button", { class: "bigbtn outline", on: { click: () => { $("sheetbody").replaceChildren(...prevBody); } } }, "Cancel"));
  $("sheet").scrollTop = 0;
}
function editSchedule(s, rooms) {
  const isNew = !s;
  let days = s ? [...s.days] : [1, 2, 3, 4, 5];
  openSheet((body, done) => {
    const name = h("input", { class: "field", value: s ? s.name : "", maxlength: 40, placeholder: "e.g. Weekday mornings" });
    const time = h("input", { class: "field", type: "time", value: s && s.hour != null ? String(s.hour).padStart(2, "0") + ":" + String(s.minute).padStart(2, "0") : "09:00" });
    const dayRow = h("div", { style: "display:grid;grid-template-columns:repeat(7,1fr);gap:4px;margin:7px 0" });
    const drawDays = () => dayRow.replaceChildren(...DAYNAMES.map((d, i) => h("button", { style: `aspect-ratio:1;border-radius:50%;font-weight:600;font-size:9px;${days.includes(i) ? "background:#111;color:#fff" : "background:#efeeea"}`,
      on: { click: () => { days = days.includes(i) ? days.filter(x => x !== i) : [...days, i]; drawDays(); } } }, d[0])));
    drawDays();
    const map = currentMap();
    body.append(h("h2", { style: "font-size:18px" }, isNew ? "New schedule" : "Edit schedule"), name, time, dayRow,
      isNew ? h("p", { class: "muted" }, rooms && rooms.length ? "Cleans " + rooms.map(x => roomNameOf(map, x.id)).join(", ") + "." : "Cleans the whole house.") : null,
      h("button", { class: "bigbtn", on: { click: async e => {
        const [hh, mm] = (time.value || "09:00").split(":").map(Number);
        const payload = { name: name.value.trim(), hour: hh, minute: mm, days };
        const ok = isNew ? await api("create_schedule", Object.assign(payload, { map_id: map && map.id, rooms: rooms || [] }), { button: e.currentTarget })
          : await api("update_schedule", Object.assign(payload, { schedule_id: s.id }), { button: e.currentTarget });
        if (ok) done(true);
      } } }, isNew ? "Create schedule" : "Save changes"),
      isNew ? null : h("button", { class: "bigbtn danger", on: { click: async () => {
        done(null);
        const w = await ask({ title: "Delete " + s.name + "?", ok: "Delete schedule", danger: true, typed: "DELETE" });
        if (w) api("delete_schedule", { schedule_id: s.id, confirm: w });
      } } }, "Delete schedule"));
  });
}

// =====================================================================
// History
const DONE = { ok: "Finished", busy: "Finished", returnHomeEnd: "Finished", dndEnd: "Stopped for quiet hours", timeboxEnd: "Time limit reached",
  cncl: "Canceled", usrSlp: "Canceled", usrEnd: "Ended early", usrSpt: "Canceled", plcDoc: "Put on the dock", batcncl: "Battery ran low" };
SCREENS.history = { uses: ["history"], render() {
  const el = h("div", { class: "screen plain" });
  const draw = () => {
    const items = (S.sections.history && S.sections.history.history) || [];
    const sum = k => items.reduce((a, m) => a + (Number(m[k]) || 0), 0);
    const totals = h("div", { class: "totals" },
      [["Total area", sum("square_feet_covered").toLocaleString() + " sq ft"], ["Total time", durationText(sum("duration_m") * 60)], ["Cleans", String(items.length)]]
        .map(([l, v]) => h("div", {}, h("b", {}, v), h("span", {}, l))));
    el.replaceChildren(topbar("History"), items.length ? totals : null,
      h("div", { class: "list" }, items.length ? items.map(m => {
        const t = unixDate(m.start_time);
        const bits = [];
        if (m.duration_m != null) bits.push(m.duration_m + " min");
        if (m.square_feet_covered != null) bits.push(m.square_feet_covered + " sq ft");
        const ok = !m.done_code || ["ok", "busy", "returnHomeEnd"].includes(m.done_code);
        return h("div", { class: "li tap", on: { click: () => historyDetail(m) } }, img(ok ? "record_task_ok" : "record_task_cancelled"),
          h("div", { class: "grow" }, t ? t.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "Clean",
            h("div", { class: "meta" }, [bits.join(" · "), m.done_code ? DONE[m.done_code] || m.done_code : ""].filter(Boolean).join(" · "))));
      }) : h("div", { class: "li muted" }, "No cleaning history yet.")));
  };
  draw(); el._update = draw;
  return el;
} };

// =====================================================================
// Robot Health: wear on each part, with the catalog's names and shop links
function partsList() {
  const counters = ((S.sections.parts && S.sections.parts.data) || {}).parts || [];
  const catalog = (((S.sections.about || {}).catalog) || {}).parts || [];
  return counters.map(c => Object.assign({}, catalog.find(x => x.part_id === c.part_id) || {}, c));
}
// How much life is left, as a percent and as words, from whichever counter the part uses.
function partLife(p) {
  const total = p.count_remaining != null && p.count_used != null ? p.count_remaining + p.count_used : null;
  if (p.count_type && p.count_remaining != null) {
    const unit = /mission|routine/i.test(p.count_type) ? "routines" : /min/i.test(p.count_type) ? "min" : "left";
    return { pct: total ? Math.round(100 * p.count_remaining / total) : null, text: p.count_remaining + " " + (unit === "left" ? "left" : unit + " left") };
  }
  if (p.minutes_remaining != null) {
    const hrs = Math.round(p.minutes_remaining / 60);
    return { pct: null, text: p.minutes_remaining <= 0 ? "Replace now" : hrs >= 1 ? plural(hrs, "hour") + " left" : p.minutes_remaining + " min left" };
  }
  return { pct: null, text: "" };
}
const PART_NAMES = { sidebrush: "Edge-Sweeping Brush", edgebrush: "Edge-Sweeping Brush", filter: "High-Efficiency Filter", mainbrush: "Main Brushes", brush: "Main Brushes",
  dirtbag: "Dust Bag", bag: "Dust Bag", pad: "Mop Pad", mop: "Mop Pad", wheel: "Front Caster Wheel", sensors: "Cliff Sensors", detergent: "Cleaning Solution" };
function partName(p) {
  if (p.part_name && !/^[a-z0-9_]+$/.test(p.part_name)) return p.part_name;
  const k = String(p.part_name_id || p.part_name || p.counter_category || p.part_id || "").toLowerCase().replace(/[^a-z]/g, "");
  const hit = Object.keys(PART_NAMES).find(x => k.includes(x));
  return hit ? PART_NAMES[hit] : (p.part_id || "Part");
}
// The official Home and map screens show a care card for the part that needs
// attention soonest: "Cliff Sensors, 3 Tasks Left", with a bar of how much of
// its interval is used up. X hides it until the count changes.
function careDue() {
  let best = null;
  for (const p of partsList()) {
    // Only parts counted in cleaning tasks (the official card skipped a brush with 9% of its minutes left).
    if (p.counter_enabled === false || p.count_remaining == null || p.count_used == null || !/mission|routine/i.test(p.count_type || "")) continue;
    const total = p.count_remaining + p.count_used;
    if (!total) continue;
    const left = p.count_remaining, frac = left / total;
    if (!(left <= 3 || frac <= 0.2)) continue;
    if (!best || frac < best.frac) best = { p, left, frac, total };
  }
  return best;
}
function careCard() {
  const due = careDue();
  if (!due) return null;
  const { p, left, frac } = due;
  const key = "careHidden:" + p.part_id;
  try { if (localStorage.getItem(key) === String(left)) return null; } catch (e) { /* private mode */ }
  const text = left <= 0 ? "Due now" : left + (left === 1 ? " Task Left" : " Tasks Left");
  const card = h("div", { class: "card care tap", on: { click: () => go("health") } },
    img("robot", "careimg"),
    h("div", { class: "grow" }, h("div", { class: "carehead" }, h("b", {}, partName(p)), chevron()),
      h("div", { class: "meta" }, text),
      h("div", { class: "carebar" }, h("span", { style: "width:" + Math.round(100 * Math.min(1, 1 - frac)) + "%" }))),
    h("button", { class: "iconbtn careclose", "aria-label": "Hide", on: { click: e => {
      e.stopPropagation();
      try { localStorage.setItem(key, String(left)); } catch (err) { /* private mode */ }
      card.remove();
    } } }, icon("x", 14)));
  return card;
}
function healthSummary() {
  const worn = partsList().filter(p => p.minutes_remaining != null && p.minutes_remaining <= 0 || p.count_remaining === 0);
  return worn.length ? plural(worn.length, "part") + " to replace" : "";
}
SCREENS.health = { uses: ["parts", "about"], render() {
  const el = h("div", { class: "screen plain" });
  const draw = () => {
    const parts = partsList(), buy = ((S.sections.about || {}).catalog || {}).buy_parts_url;
    el.replaceChildren(topbar("Robot Health", { center: true }),
      img("robot", "robotimg"),
      h("div", { class: "list" }, parts.length ? parts.map(p => {
        const life = partLife(p);
        return h("div", { class: "li" },
          h("div", { class: "grow" }, partName(p),
            life.pct != null ? h("div", { class: "lifebar" }, h("span", { style: `width:${Math.max(3, life.pct)}%` + (life.pct < 15 ? ";background:var(--crimson)" : "") })) : null,
            life.text ? h("div", { class: "meta" }, life.text) : null),
          p.guide_url ? h("a", { class: "btn-out small", href: p.guide_url, target: "_blank", rel: "noopener" }, "Guide") : null,
          h("button", { class: "btn-out small", on: { click: async e => {
            if (await ask({ title: "Have you replaced or cleaned this part?", text: partName(p) + "'s wear counter starts over.", ok: "Reset counter" }))
              api("reset_part", { part_id: p.part_id, confirm: "RESET" }, { button: e.currentTarget });
          } } }, "Reset"));
      }) : h("div", { class: "li muted" }, "The robot didn't report any parts yet.")),
      buy ? h("a", { class: "bigbtn outline", href: buy, target: "_blank", rel: "noopener", style: "display:block;text-align:center;margin-top:13px" }, "Shop Parts") : null);
  };
  draw(); el._update = draw;
  return el;
} };

// =====================================================================
// About Robot
const MODEL_NAMES = { "105C": "Roomba® 105 Series Combo Robot", "105V": "Roomba® 105 Series Vac Robot", "115C": "Roomba® 110 Series Combo Robot",
  "125V": "Roomba® 125 Series Robot", "205C": "Roomba® 200 DustCompactor Series Combo Robot", "205V": "Roomba® 200 DustCompactor Series Vac Robot",
  "405C": "Roomba® Plus 400 Series Combo Robot", "415C": "Roomba® Plus 410 Series Combo Robot", "507C": "Roomba® Plus 500 Series Combo Robot",
  "517C": "Roomba® Plus 510 Series Combo Robot", "575C": "Roomba® Plus 570 Series Combo Robot", "705C": "Roomba® Max 700 series Robot",
  "705V": "Roomba® Max 700 Series Vac Robot", "715V": "Roomba® Max 715 Series Robot", "725C": "Roomba® 725 Series Robot",
  "775C": "Roomba® Max 770 Series Combo Robot", "815C": "Roomba® Max 810 Series Combo Robot", "925": "Roomba® 925 Series Robot" };
function modelName(a) {
  const ser = a.serial || {};
  const key = Object.keys(MODEL_NAMES).find(k => [ser.series, ser.family_variant, ser.family, ser.name].some(v => v && String(v).toUpperCase().includes(k)));
  return key ? MODEL_NAMES[key] : ser.name || ser.series || "Roomba";
}
SCREENS.about = { uses: ["about", "status"], render() {
  const el = h("div", { class: "screen plain" });
  const draw = () => {
    const a = S.sections.about || {}, st = status(), ser = a.serial || {};
    const fw = (a.firmware || []).slice().sort((x, y) => String(y.release_date || "").localeCompare(String(x.release_date || "")))[0];
    const rt = st.runtimeStats || {};
    const row = (label, value) => value ? h("div", { class: "li" }, h("div", { class: "grow" }, label), h("span", { class: "muted" }, value)) : null;
    const dock = st.dock || {};
    el.replaceChildren(topbar("About Robot", { center: true }),
      h("div", { class: "list" },
        robotNameRow(),
        row("Model", modelName(a)),
        row("SKU", a.sku || st.sku),
        row("Serial Number", ser.serial_number),
        row("Robot Software Version", a.softwareVer || st.softwareVer || (fw && fw.version)),
        row("Dock Software Version", dock.fwVer),
        row("Last Robot Software Update", fw && fw.release_date ? new Date(fw.release_date).toLocaleDateString([], { year: "numeric", month: "long", day: "numeric" }) : null),
        row("Birthday", st.regDate ? new Date(st.regDate + "T12:00:00").toLocaleDateString([], { year: "numeric", month: "long", day: "numeric" }) : null),
        row("Time Cleaning", rt.hr != null ? plural(rt.hr, "hour") + (rt.min ? ", " + rt.min + " min" : "") : null)),
      fw && fw.notes ? h("div", {}, h("div", { class: "sect" }, "What's new"), h("p", { class: "muted", style: "white-space:pre-wrap;font-size:11px;margin:0 3px" }, fw.notes)) : null);
  };
  draw(); el._update = draw;
  return el;
} };

// =====================================================================
// Map Manager: every map, with rename, rotate, delete, and Create new map
SCREENS.maps = { uses: ["map"], render() {
  const el = h("div", { class: "screen plain" });
  const draw = () => {
    const views = (S.sections.map && S.sections.map.views) || {};
    el.replaceChildren(topbar("Map Manager", { center: true }),
      h("div", { class: "mapcards" }, allMaps().map(m => {
        const svg = document.createElementNS(SVGNS, "svg");
        const card = h("div", { class: "card tap", on: { click: () => { S.mapId = m.id; mapMenu("edit"); } } },
          h("div", { class: "cardhead" }, h("span", { class: "grow" }, m.name), chevron()), h("div", { class: "homemap" }, svg));
        requestAnimationFrame(() => drawMap(svg, views[m.id] || null, { small: true, labels: false }));
        return card;
      })),
      h("button", { class: "bigbtn", on: { click: () => newMap() } }, "Create new map"),
      h("p", { class: "muted center", style: "font-size:10px" }, "Roomba can keep up to " + ((status().cap || {}).mapMax || 3) + " maps."));
  };
  draw(); el._update = draw;
  return el;
} };
async function newMap() {
  const ok = await ask({ title: "Create a new map?", text: "Place Roomba on its dock on the floor you want to map, open doors, and pick up obstacles. Roomba explores without cleaning, then returns to the dock. Don't move it while it maps.", ok: "Start mapping" });
  if (ok && await api("new_map", { confirm: "MAP" })) go("map");
}

// =====================================================================
// Notifications from iRobot (the official app's Messages)
SCREENS.messages = { uses: ["notifications"], render() {
  const el = h("div", { class: "screen plain" });
  const draw = () => {
    const d = ((S.sections.notifications || {}).data) || {};
    const list = Array.isArray(d) ? d : d.notifications || d.messages || d.items || [];
    el.replaceChildren(topbar("Notifications", { center: true }),
      h("div", { class: "list" }, list.length ? list.map(n => {
        const t = n.timestamp || n.time || n.created_at || n.ts;
        const when = t ? new Date(typeof t === "number" && t < 1e12 ? t * 1000 : t) : null;
        return h("div", { class: "li" }, h("div", { class: "grow" }, n.title || n.message || n.type || n.id || "Notification",
          h("div", { class: "meta" }, [n.body || n.text || n.description, when && !isNaN(when) ? when.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : ""].filter(Boolean).join(" · "))));
      }) : h("div", { class: "li muted" }, "No notifications.")));
  };
  draw(); el._update = draw;
  return el;
} };

// One clean, like the official Details page: how it started, the rooms it
// did (with area), bin emptying, and anything that went wrong.
function historyDetail(m) {
  const t = unixDate(m.start_time), cmd = m.command || {};
  const how = { localApp: "App", schedule: "Scheduled", manual: "Robot button", rmtApp: "App", admin: "iRobot", localSchedule: "Scheduled" }[cmd.initiator] || cmd.initiator;
  const views = (S.sections.map && S.sections.map.views) || {}, map = views[cmd.map_id] || null;
  const fmt = d => d ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  const row = (label, value) => value != null && value !== "" ? h("div", { class: "li" }, h("div", { class: "grow" }, label), h("span", { class: "muted" }, String(value))) : null;
  const events = (m.events || []).map(e => {
    if (e.room) return h("div", { class: "li" }, h("div", { class: "grow" }, roomNameOf(map, e.room), h("div", { class: "meta" }, e.area ? e.area + " sq ft" : "")), h("span", { class: "muted" }, fmt(unixDate(e.start))));
    if (e.error) return h("div", { class: "li" }, h("div", { class: "grow" }, "Problem (error " + e.error + ")"), h("span", { class: "muted" }, fmt(unixDate(e.start))));
    if (e.evac) return h("div", { class: "li" }, h("div", { class: "grow" }, "Emptied the bin"), h("span", { class: "muted" }, fmt(unixDate(e.start))));
    return null;
  }).filter(Boolean);
  openSheet((body, done) => body.append(
    h("h2", { style: "font-size:18px" }, t ? t.toLocaleString([], { weekday: "long", month: "long", day: "numeric", hour: "numeric", minute: "2-digit" }) : "Clean"),
    h("div", { class: "list" },
      row("Result", m.done_code ? DONE[m.done_code] || m.done_code : null),
      row("Started by", how),
      row("Map", map ? map.name : null),
      row("Clean Time", m.duration_m != null ? durationText(m.duration_m * 60) : null),
      row("Clean Area", m.square_feet_covered != null ? m.square_feet_covered + " sq ft" : null),
      row("Bin emptied", m.number_of_evacuations || null),
      row("Dirt Detect", m.number_of_dirt_detects ? plural(m.number_of_dirt_detects, "time") : null)),
    events.length ? h("div", {}, h("div", { class: "sect" }, "Timeline"), h("div", { class: "list" }, events)) : null,
    h("button", { class: "bigbtn outline", on: { click: () => done(null) } }, "Close")));
}

// =====================================================================
// This app: saved login and connection mode
SCREENS.account = { render() {
  const el = h("div", { class: "screen plain" });
  const draw = () => {
    const l = S.login || {};
    el.replaceChildren(topbar("This app", { center: true }),
      h("div", { class: "sect" }, "Login"),
      h("div", { class: "list" }, h("div", { class: "li" }, h("div", { class: "grow" }, l.saved ? "Login saved on this PC" : "Login not saved", h("div", { class: "meta" }, l.saved ? l.email : "Typed each time the app starts")),
        l.supported ? h("button", { class: "btn-out", style: "padding:7px 15px;font-size:11px;margin:0", on: { click: async e => {
          const d = await api(l.saved ? "forget_login" : "save_login", {}, { button: e.currentTarget });
          if (d) { S.login = d.login; draw(); }
        } } }, l.saved ? "Forget" : "Save") : null)),
      h("div", { class: "sect" }, "Connection"),
      h("div", { class: "list" }, h("div", { class: "li" }, h("div", { class: "grow" }, S.stay ? "Stays logged in while the app runs" : "Logs in for each action",
        h("div", { class: "meta" }, S.stay ? "Fast, with live progress. To change it, set stay_logged_in in app\\settings.json." : "Slower, and progress updates only every minute. Set stay_logged_in to true in app\\settings.json for live updates.")))),
      h("div", { class: "list" }, li("Reload everything from iRobot", async () => { await api("refresh_all", {}); }, "Re-reads maps, routines, schedules, and settings")));
  };
  draw();
  return el;
} };

// =====================================================================
// Placeholder tabs for parts of the official app this one doesn't do yet
const SOON = {
  automations: ["Automations", "Schedules live under Routines for now. Automations are coming later.", "order_schedule_empty"],
  support: ["Support", "For help with your Roomba, use iRobot's support site or the iRobot app.", "device_list_support_icon"],
  store: ["Store", "The store isn't part of this app.", "device_list_store_icon"],
};
for (const [key, [t, text, pic]] of Object.entries(SOON)) {
  SCREENS[key] = { nav: true, render() { return h("div", { class: "screen" }, h("div", { class: "topbar" }, h("h1", {}, t)), soon(t, text, pic)); } };
}

// =====================================================================
// Start-up: show what the server already has, then keep it fresh.
async function boot() {
  history.replaceState("root", "", location.hash || "#home");
  const waiting = setTimeout(() => { if (!S.ready) $("app").replaceChildren(soon("Still waiting for the app on your PC", "If this doesn't change, check that roomba_app is still running, then reload.")); }, 8000);
  for (;;) {
    let data;
    try { const r = await fetch("api/cached", { method: "POST", headers: { "X-Roomba-Token": TOKEN } }); data = await r.json(); }
    catch (e) { document.body.append(h("div", { class: "soon" }, "Can't reach the app. Is it still running on your PC?")); return; }
    S.login = data.login; S.stay = data.stay_logged_in; S.plan = data.plan; if (data.trail) S.trail = data.trail; if (data.livemap) trailFor(data.livemap.mission).live = data.livemap;
    for (const k in data.sections) if (k !== "map") S.sections[k] = data.sections[k];
    if (data.sections.map) { mergeMap(data.sections.map); }
    if (!S.ready) { S.ready = true; show(location.hash.slice(1) || "home"); }
    else for (const fn of [...listeners]) fn(Object.keys(data.sections));
    if (data.login_error) { toast(data.login_error, true); break; }
    if (!data.loading) {
      const failed = Object.keys(data.errors || {});
      if (failed.length) toast("Couldn't load " + failed.join(", ") + ". Try Reload everything in the person menu.", true);
      break;
    }
    await new Promise(r => setTimeout(r, 700));
  }
  clearTimeout(waiting);
  liveUpdates();
  preloadImages();
}
boot().catch(e => {
  reportProblem("Starting the app failed: " + e.message, e.stack);
  if (!S.ready) { S.ready = true; try { show("home"); } catch (e2) { /* the message above is showing */ } }
});
