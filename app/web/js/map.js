// map.js: draws the floor map as SVG, and the keep-out/no-mop zone editor.
// Map coordinates come from the robot in meters with y pointing up; the
// drawing flips y once (the "world" group) and flips text back.
"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
function sv(tag, attrs) { const e = document.createElementNS(SVGNS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; }
function pts(ring) { return ring.map(p => p[0] + "," + p[1]).join(" "); }
function centroid(ring) {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0; i < ring.length - 1; i++) { const [x0, y0] = ring[i], [x1, y1] = ring[i + 1]; const f = x0 * y1 - x1 * y0; a += f; cx += (x0 + x1) * f; cy += (y0 + y1) * f; }
  if (Math.abs(a) < 1e-9) return [ring.reduce((s, p) => s + p[0], 0) / ring.length, ring.reduce((s, p) => s + p[1], 0) / ring.length];
  return [cx / (3 * a), cy / (3 * a)];
}
function inside(pt, ring) {
  let yes = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi) yes = !yes;
  }
  return yes;
}
function roomAt(map, pt) {
  if (!map || !pt) return null;
  return map.rooms.find(r => inside(pt, r.raw && r.raw.length ? r.raw : r.ring)) || map.rooms.find(r => inside(pt, r.ring)) || null;
}
function bbox(points) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of points) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  return [x0, y0, x1, y1];
}
function mapBounds(map) {
  const all = [];
  map.rooms.forEach(r => all.push(...r.ring));
  map.borders.forEach(b => all.push(...b));
  (map.walls || []).forEach(b => all.push(...b));
  return all.length ? bbox(all) : [0, 0, 1, 1];
}

function mapCorners(map) { const [a, b, c, d] = mapBounds(map); return [[a, b], [c, d]]; }
// A GeoJSON-style polygon ([outer, holes...]) as one SVG path.
function polyPath(rings) { return rings.map(r => "M" + r.map(p => p[0] + " " + p[1]).join("L") + "Z").join(""); }
// Obstacle pictures by the type the robot reports (P2MapHazardInfo in the official app).
const HAZARD_ICONS = { cables: "object_wire", socks: "object_socks", shoes: "object_shoes", cat: "object_pet", dog: "object_pet",
  pet_waste: "object_dropping", liquid: "object_dirty_liquid", dry_debris: "object_dirty_solid", weighing_scale: "object_weight_scale",
  trash_can: "object_dustbin", bar_stool: "object_barstool", barstool: "object_barstool" };
const HAZARD_NAMES = { cables: "Cords", socks: "Socks", shoes: "Shoes", cat: "Cat", dog: "Dog", pet_waste: "Pet waste", liquid: "Liquid",
  dry_debris: "Debris", weighing_scale: "Scale", trash_can: "Trash can", bar_stool: "Bar stool", purse: "Bag", person: "Person",
  other_toys: "Toys", blanket_or_comforter: "Blanket" };

// Rooms due for a clean are peach, like the official app; the rest are white.
// "Due" comes from iRobot's per-room dirt score (see actions.py clean_scores).
function roomNeedsCleaning(map, id) {
  const score = (map.scores || {})[id];
  return typeof score === "number" && score >= (map.needs_cleaning || 0.5);
}
function roomFill(map, id) { return roomNeedsCleaning(map, id) ? "var(--peach)" : "var(--floor)"; }
function mapNeedsCleaning(map) {
  if (!map || !map.scores || !Object.keys(map.scores).length) return null;   // unknown
  return map.rooms.some(r => roomNeedsCleaning(map, r.id));
}

// Draws `map` into `svg`. opts:
//   sel: [room ids] in cleaning order (shown as numbered black pills)
//   onRoom(id), onZone(zone), onBackground([x, y] in map units)
//   pad: fraction of the map size to leave around it (default .06)
//   labels: false to hide room names; small: true for previews
//   dim: [room ids] drawn faded (e.g. not part of an edit)
//   extra(world, scale): draw more on top (editors use this)
function drawMap(svg, map, opts) {
  opts = opts || {};
  svg.replaceChildren();
  if (!map) return null;
  // During a clean, the live map picture: explored area, coverage, obstacles.
  const live = opts.trail && opts.trail.live;
  const [x0, y0, x1, y1] = live && live.explored && live.explored.length
    ? bbox([...mapCorners(map), ...live.explored.flatMap(p => p[0] || [])]) : mapBounds(map);
  const w = x1 - x0 || 1, hgt = y1 - y0 || 1, pad = Math.max(w, hgt) * (opts.pad == null ? 0.06 : opts.pad);
  svg.setAttribute("viewBox", [x0 - pad, -(y1 + pad), w + 2 * pad, hgt + 2 * pad].join(" "));
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  const box = svg.getBoundingClientRect();
  // Map units per screen pixel, so strokes and text stay a steady size.
  const px = Math.max((w + 2 * pad) / (box.width || 360), (hgt + 2 * pad) / (box.height || 300));
  const world = sv("g", { transform: "scale(1,-1)" });
  svg.append(world);
  if (opts.onBackground) {
    const bg = sv("rect", { x: x0 - pad * 4, y: y0 - pad * 4, width: w + pad * 8, height: hgt + pad * 8, fill: "transparent" });
    bg.addEventListener("click", e => opts.onBackground(toMap(svg, e)));
    world.append(bg);
  }
  const sel = opts.sel || [];
  // Floor texture, like the official app's speckled floor.
  const defs = sv("defs", {}), tex = "tex" + Math.random().toString(36).slice(2, 8);
  const pat = sv("pattern", { id: tex, patternUnits: "userSpaceOnUse", width: 7 * px, height: 7 * px });
  [[1, 1], [4.5, 2.5], [2.5, 5], [6, 6]].forEach(([x, y]) => pat.append(sv("circle", { cx: x * px, cy: y * px, r: 0.55 * px, fill: "#9a9a96" })));
  defs.append(pat); svg.prepend(defs);
  // What Roomba has explored this clean, including past the saved walls.
  if (live) (live.explored || []).forEach(p => {
    world.append(sv("path", { d: polyPath(p), fill: "var(--floor)", "fill-rule": "evenodd", opacity: 0.92, "pointer-events": "none" }));
    world.append(sv("path", { d: polyPath(p), fill: `url(#${tex})`, "fill-rule": "evenodd", opacity: 0.35, "pointer-events": "none" }));
  });
  // The whole floor, white, under the rooms.
  map.borders.forEach(b => world.append(sv("polygon", { points: pts(b), fill: "var(--floor)", "pointer-events": "none" })));
  map.rooms.forEach(r => {
    const on = sel.includes(r.id), faded = opts.dim && opts.dim.includes(r.id);
    const p = sv("polygon", { points: pts(r.ring), class: "m-room" + (on ? " sel" : ""),
      fill: faded ? "#efeee9" : roomFill(map, r.id) });
    if (opts.onRoom) p.addEventListener("click", e => { e.stopPropagation(); opts.onRoom(r.id, toMap(svg, e)); });
    else if (opts.onBackground) p.addEventListener("click", e => opts.onBackground(toMap(svg, e)));
    world.append(p);
    world.append(sv("polygon", { points: pts(r.raw && r.raw.length ? r.raw : r.ring), fill: `url(#${tex})`, opacity: on ? 0.25 : 0.55, "pointer-events": "none" }));
  });
  // Furniture the robot recognized, and the floor covered so far: filled
  // green with the passes that covered it, like the official app.
  if (live && MapDisplay.furniture) (live.furniture || []).forEach(f => world.append(sv("path", { d: polyPath(f.rings), class: "m-furniture", "stroke-width": 0.8 * px })));
  if (MapDisplay.furniture) (map.furniture || []).forEach(f => {
    world.append(sv("path", { d: polyPath([f.points]), class: "m-furniture", "stroke-width": 0.8 * px }));
    if (!opts.trail) {
      const [a, b, c, d] = bbox(f.points);
      const t = sv("text", { x: (a + c) / 2, y: -(b + d) / 2, class: "m-furnlabel", "font-size": 7.5 * px, transform: "scale(1,-1)", "pointer-events": "none" });
      t.textContent = furnitureName(f.type); world.append(t);
    }
  });
  // A spot clean's square: pale green under the coverage, outlined on top.
  const spots = (live && live.spots) || [];
  spots.forEach(r => world.append(sv("path", { d: polyPath(r), class: "m-spotarea", "pointer-events": "none" })));
  const covered = live && live.coverage && live.coverage.length;
  if (covered) {
    live.coverage.forEach(c => world.append(sv("path", { d: polyPath(c.rings), class: "m-cover", "fill-rule": "evenodd", "stroke-width": 0.7 * px })));
    (MapDisplay.paths ? live.passes || [] : []).forEach(l => world.append(sv("polyline", { points: pts(l), class: "m-pass", "stroke-width": 0.6 * px })));
  } else if (opts.trail && opts.trail.points.length) {
    // No picture yet: the path itself, as wide as the robot cleans.
    const line = opts.trail.points.map(p => p[0] + "," + p[1]).join(" ");
    world.append(sv("polyline", { points: line, class: "m-trail", "stroke-width": Math.max(0.27, (opts.small ? 3 : 6) * px) }));
    if (MapDisplay.paths) world.append(sv("polyline", { points: line, class: "m-pass", "stroke-width": 0.6 * px }));
  }
  spots.forEach(r => world.append(sv("path", { d: polyPath(r), class: "m-spotedge", "stroke-width": 1.4 * px })));
  // Carpets (a soft weave) and thresholds (small gray bars), like the official map.
  if (opts.zones !== false) {
    (map.carpets || []).forEach(c => world.append(sv("polygon", { points: pts(c.points), class: "m-carpet", "stroke-width": 0.8 * px })));
    (map.thresholds || []).forEach(t => world.append(sv("polygon", { points: pts(t.points), class: "m-threshold" })));
  }
  // Walls from the floor plan (straight lines round each room), then the outer border.
  const lw = (opts.small ? 1.2 : 1.8) * px;
  (map.walls || []).forEach(b => world.append(sv("polyline", { points: pts(b), class: "m-wall", "stroke-width": lw })));
  // Without a floor plan, outline the floor instead.
  if (!(map.walls || []).length) map.borders.forEach(b => world.append(sv("polyline", { points: pts(b), class: "m-border", "stroke-width": lw })));
  if (opts.zones !== false) map.zones.forEach(z => {
    if (z.kind === "wall") { world.append(sv("polyline", { points: pts(z.points), class: "m-wallline", "stroke-width": 4 * px })); return; }
    const g = sv("g", {});
    g.append(sv("polygon", { points: pts(z.points), class: z.kind === "nomop" ? "m-nomop" : "m-keepout", "stroke-width": 1.6 * px }));
    // The official minus-in-a-circle mark.
    const [cx, cy] = centroid(z.points.length > 1 ? z.points : [[0, 0]]), k = opts.small ? 0.6 : 1;
    g.append(sv("circle", { cx, cy, r: 8 * k * px, fill: "none", stroke: "#fff", "stroke-width": 1.3 * k * px }));
    g.append(sv("line", { x1: cx - 4 * k * px, y1: cy, x2: cx + 4 * k * px, y2: cy, stroke: "#fff", "stroke-width": 1.3 * k * px }));
    if (opts.onZone) g.addEventListener("click", e => { e.stopPropagation(); opts.onZone(z); });
    else g.style.pointerEvents = "none";
    world.append(g);
  });
  // Saved clean zones: dashed outlines, tappable like rooms.
  if (opts.zones !== false) (map.clean_zones || []).forEach(z => {
    const on = sel.includes("z:" + z.id);
    const p = sv("polygon", { points: pts(z.points), class: "m-cleanzone" + (on ? " sel" : ""), "stroke-width": 1.4 * px, "stroke-dasharray": `${5 * px} ${3 * px}` });
    if (opts.onRoom) p.addEventListener("click", e => { e.stopPropagation(); opts.onRoom("z:" + z.id, toMap(svg, e)); });
    else p.style.pointerEvents = "none";
    world.append(p);
    if (opts.labels !== false) {
      const [cx, cy] = centroid(z.points), t = sv("text", { x: cx, y: -cy, transform: "scale(1,-1)", "font-size": (opts.small ? 6.5 : 9) * px, class: "m-zonelabel", "text-anchor": "middle" });
      t.textContent = on ? (sel.indexOf("z:" + z.id) + 1) + ". " + z.name : z.name; world.append(t);
    }
  });
  // Obstacles Roomba saw and went around.
  if (live && MapDisplay.hazards) (live.hazards || []).forEach(z => {
    const size = (opts.small ? 13 : 19) * px, [hx, hy] = z.at;
    const g = sv("g", { transform: `translate(${hx},${hy}) scale(1,-1)`, class: "m-hazard" });
    const im = sv("image", { href: "static/img/" + (HAZARD_ICONS[z.type] || "object_other") + ".png", x: -size / 2, y: -size / 2, width: size, height: size });
    const t = sv("title", {}); t.textContent = HAZARD_NAMES[z.type] || "Obstacle"; g.append(t, im);
    world.append(g);
  });
  // The robot, while cleaning.
  if (opts.trail && opts.trail.pos) {
    const [rx, ry] = opts.trail.pos, k = opts.small ? 0.6 : 1;
    world.append(sv("circle", { cx: rx, cy: ry, r: 8 * k * px, class: "m-robot", "stroke-width": 2.5 * k * px }));
  }
  const dockAt = (live && live.dock) || map.dock;
  if (dockAt) {
    const [dx, dy] = dockAt, k = opts.small ? 0.6 : 1;
    world.append(sv("rect", { x: dx - 9 * k * px, y: dy - 6 * k * px, width: 18 * k * px, height: 12 * k * px, rx: 4 * k * px, fill: "#3b3b3b" }));
    world.append(sv("circle", { cx: dx + 6 * k * px, cy: dy, r: 7 * k * px, fill: "#555" }));
  }
  if (opts.labels !== false) map.rooms.forEach(r => {
    if (!r.ring.length) return;
    const [cx, cy] = centroid(r.ring), n = sel.indexOf(r.id);
    const fs = (opts.small ? 7.5 : 10) * px;
    if (n >= 0 && opts.badges !== false) {
      // Black pill: numbered circle + name, like the official app.
      const name = r.name, width = (name.length * 6.1 + 34) * px, height = 25 * px;
      const g = sv("g", { class: "m-badge", transform: `translate(${cx},${cy}) scale(1,-1)` });   // upright again inside the flipped world
      const flip = sv("g", {});
      flip.append(sv("rect", { x: -width / 2, y: -height / 2, width, height, rx: height / 2, fill: "#111" }));
      flip.append(sv("circle", { cx: -width / 2 + 13 * px, cy: 0, r: 7.5 * px, fill: "#fff" }));
      const num = sv("text", { x: -width / 2 + 13 * px, y: 0, "font-size": 9 * px, "font-weight": 700, "text-anchor": "middle", "dominant-baseline": "central", fill: "#111" });
      num.textContent = n + 1;
      const t = sv("text", { x: -width / 2 + 25 * px, y: 0, "font-size": 10 * px, "font-weight": 600, "dominant-baseline": "central", fill: "#fff" });
      t.textContent = name;
      flip.append(num, t); g.append(flip); world.append(g);
      return;
    }
    const t = sv("text", { x: cx, y: -cy, class: "m-label", "font-size": fs, "stroke-width": 2.5 * px, transform: "scale(1,-1)" });
    t.textContent = r.name; world.append(t);
  });
  if (opts.extra) opts.extra(world, px);
  return { world, px };
}

// Screen point of a pointer event -> map coordinates.
function toMap(svg, e) {
  const world = svg.querySelector("g");
  const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
  const m = world.getScreenCTM();
  const p = pt.matrixTransform(m.inverse());
  return [p.x, p.y];
}

// ---------- zone editor
// zones: [{id|null, kind: "keepout"|"nomop", box: [x0,y0,x1,y1], points|null, moved}]
// Existing zones keep their exact corners unless moved or resized.
const AREA_CLASS = { nomop: "m-nomop", keepout: "m-keepout", clean: "m-cleanzone", carpet: "m-carpet", furniture: "m-furn" };
// Furniture types the robot understands (the library's FurnitureType), with
// the official app's names for them.
const FURNITURE_NAMES = { bed: "Bed", sofa: "Sofa", dining_table: "Dining Table", coffee_table: "Coffee Table", toilet: "Toilet",
  living_chair: "Armchair", left_l_sofa: "Sectional (Left)", right_l_sofa: "Sectional (Right)", cabinet: "Cabinet",
  refrigerator: "Refrigerator", sidetable: "Side Table", tvcabinet: "TV Cabinet", washingmachineordryer: "Washer or Dryer",
  litter_box: "Cat Litter Box", pet_bowl: "Pet Bowl", pet_bed: "Pet Bed", pet_feeder: "Pet Feeder", cat_tower: "Cat Tower", unknown: "Furniture" };
function furnitureName(t) { return FURNITURE_NAMES[t] || "Furniture"; }
function zoneEditor(svg, map, onChange, opts) {
  opts = opts || {};
  const source = opts.items || map.zones.filter(z => z.kind !== "wall").map(z => ({ id: z.id, kind: z.kind, points: z.points }));
  const zones = source.map(z => ({ id: z.id, kind: z.kind, name: z.name, box: bbox(z.points), moved: false }));
  let active = zones.length ? zones.length - 1 : -1;
  const MIN = 0.3;   // meters

  function draw() {
    // Hide the layer being edited from the map underneath.
    const hide = opts.hide || "zones";
    const shown = Object.assign({}, map, hide === "zones" ? { zones: map.zones.filter(z => z.kind === "wall") }
      : hide === "cleanzones" ? { clean_zones: [] } : hide === "furniture" ? { furniture: [] } : { carpets: (map.carpets || []).filter(c => !c.user) });
    drawMap(svg, shown, { badges: false, extra: (world, px) => {
      zones.forEach((z, i) => {
        const [x0, y0, x1, y1] = z.box;
        const r = sv("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, class: (AREA_CLASS[z.kind] || "m-keepout") + " m-zone editing",
          "stroke-width": 1.6 * px, "stroke-dasharray": z.kind === "nomop" ? `${6 * px} ${4 * px}` : "none" });
        r.addEventListener("pointerdown", e => startDrag(e, i, "move"));
        world.append(r);
        if (z.kind === "furniture") {
          const t = sv("text", { x: (x0 + x1) / 2, y: -(y0 + y1) / 2, class: "m-furnlabel", "font-size": 9 * px, transform: "scale(1,-1)", "pointer-events": "none" });
          t.textContent = furnitureName(z.name); world.append(t);
        }
      });
      const z = zones[active];
      if (!z) return;
      const [x0, y0, x1, y1] = z.box, m = 12 * px;
      world.append(sv("rect", { x: x0 - m, y: y0 - m, width: x1 - x0 + 2 * m, height: y1 - y0 + 2 * m, class: "m-frame", "stroke-width": 1.4 * px, "stroke-dasharray": `${7 * px} ${5 * px}` }));
      [[x0 - m, y0 - m, "sw"], [x1 + m, y0 - m, "se"], [x1 + m, y1 + m, "ne"]].forEach(([hx, hy, corner]) => {
        const c = sv("circle", { cx: hx, cy: hy, r: 13 * px, class: "m-handle", "stroke-width": 2 * px });
        c.addEventListener("pointerdown", e => startDrag(e, active, corner));
        world.append(c);
      });
      // delete button at the top-left corner
      const del = sv("g", { style: "cursor:pointer" });
      del.append(sv("circle", { cx: x0 - m, cy: y1 + m, r: 14 * px, fill: "#fff", stroke: "#ddd", "stroke-width": 1 * px }));
      const d = 5 * px;
      del.append(sv("path", { d: `M${x0 - m - d},${y1 + m - d}L${x0 - m + d},${y1 + m + d}M${x0 - m + d},${y1 + m - d}L${x0 - m - d},${y1 + m + d}`, stroke: "var(--crimson)", "stroke-width": 2 * px }));
      del.addEventListener("pointerdown", e => { e.stopPropagation(); zones.splice(active, 1); active = zones.length - 1; draw(); onChange(); });
      world.append(del);
      const label = sv("text", { x: (x0 + x1) / 2, y: -(y0 - m - 16 * px), class: "m-size", "font-size": 9.5 * px, transform: "scale(1,-1)" });
      label.textContent = (x1 - x0).toFixed(1) + "m*" + (y1 - y0).toFixed(1) + "m";
      world.append(label);
    } });
  }

  function startDrag(e, i, how) {
    e.preventDefault(); e.stopPropagation();
    active = i;
    const z = zones[i], start = toMap(svg, e), box0 = z.box.slice();
    const move = ev => {
      const [x, y] = toMap(svg, ev), dx = x - start[0], dy = y - start[1];
      let [x0, y0, x1, y1] = box0;
      if (how === "move") { x0 += dx; x1 += dx; y0 += dy; y1 += dy; }
      if (how === "sw") { x0 = Math.min(x0 + dx, x1 - MIN); y0 = Math.min(y0 + dy, y1 - MIN); }
      if (how === "se") { x1 = Math.max(x1 + dx, x0 + MIN); y0 = Math.min(y0 + dy, y1 - MIN); }
      if (how === "ne") { x1 = Math.max(x1 + dx, x0 + MIN); y1 = Math.max(y1 + dy, y0 + MIN); }
      z.box = [x0, y0, x1, y1]; z.moved = true; draw();
    };
    const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); onChange(); };
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
    draw();
  }

  function add(kind, name, size) {
    const [x0, y0, x1, y1] = mapBounds(map), cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    const [w, d] = size || [2, 2];
    zones.push({ id: null, kind, name, box: [cx - w / 2, cy - d / 2, cx + w / 2, cy + d / 2], moved: true });
    active = zones.length - 1; draw(); onChange();
  }

  function result() {
    return zones.map(z => {
      if (z.id != null && !z.moved) return { id: z.id, kind: z.kind, name: z.name };
      const [x0, y0, x1, y1] = z.box;
      return { id: z.id, kind: z.kind, name: z.name, points: [[x0, y0], [x1, y0], [x1, y1], [x0, y1]] };
    });
  }
  function changed() { return zones.some(z => z.moved) || zones.length !== source.length; }
  draw();
  return { add, result, changed, redraw: draw };
}
