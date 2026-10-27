"""Everything the app can ask the robot to do, and the data it reads.

Each handler takes a connected-or-not PrimeRobot and the JSON body the page
sent, and returns either a short message (actions) or data (readers). The
server (server.py) decides how the robot is logged in; these don't care.

Copied from roomba_web.py on 2026-10-21 and extended for the Roomba Home
style app: per-room cleaning preferences (mode, suction, liquid, passes,
SmartScrub), spot clean, and skip.
"""

import asyncio
import dataclasses
import json
import math
import sys
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from roombapy_prime import (
    AuthCredentialsError,
    AuthError,
    AuthRateLimitedError,
    CloudError,
    PrimeFactory,
)
from roombapy_prime.models.favorites import FavoriteV1
from roombapy_prime.models.geometry import LineString, Polygon
from roombapy_prime.models.map_bundle import CleanScoreResponse, PolicyZoneFeature, RoomFeature, parse_map_bundle
from roombapy_prime.models.enums_common import FurnitureType
from roombapy_prime.models.map_editing import (
    AddCleanZones,
    CleanZone,
    DeleteCleanZones,
    FloorTypeEntry,
    Furniture,
    MergeRoomsV1,
    SetFloorTypes,
    SetFurniture,
    SetRoomMetadataV1,
    SetVirtualWallsV1,
    SplitRoomV1,
    VirtualWallNoMopZoneV1,
    VirtualWallRectangleV1,
    policy_zones_to_virtual_walls,
)
from roombapy_prime.models.mission_control import (
    CommandParams,
    MissionCommandType,
    Region,
    RegionType,
    RoutineCommand,
)
from roombapy_prime.models.mission_history import parse_mission_history
from roombapy_prime.models.robot_info import parse_active_map_versions
from roombapy_prime.models.schedules_dnd import (
    DNDDailySchedule,
    DNDEndsAt,
    HouseholdSchedule,
    ScheduleFrequency,
    ScheduleOptions,
    ScheduleTime,
)

SIMPLE_COMMANDS = ("start", "stop", "pause", "resume", "dock", "skip", "spot", "find", "evac")
MODE_LABELS = {2: "Vac Only", 32: "Vac & Mop", 4: "Mop Only", 512: "Vac then Mop"}

# Raw copies of what iRobot sends, kept on this PC only, so a map or status
# that looks wrong can be checked against the real data.
DEBUG_DIR = Path(__file__).resolve().parent / "debug"


def debug_dump(name: str, data: Any) -> None:
    def safe(value):
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        if isinstance(value, dict):
            return {k: safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [safe(v) for v in value]
        return value
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / f"{name}.json").write_text(json.dumps(safe(data), indent=1, default=str), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


class ActionError(Exception):
    """A failure whose message is safe to show in the browser."""


# --- talking to the robot ---------------------------------------------------

async def simple_command(robot, command: str) -> str:
    if not await robot.send_simple_command(command):
        raise ActionError(f"'{command}' was sent but iRobot's cloud didn't confirm delivery.")
    return f"'{command}' delivered. Check the robot to confirm it reacted."


async def robot_maps(robot) -> list:
    maps = [m for m in parse_active_map_versions(await robot.get_active_map_versions())
            if m.robot_id in (None, robot.blid) and m.active_p2mapv_id]
    if not maps:
        raise ActionError("No saved map found for this robot. Run a mapping or cleaning job first.")
    return maps


def pick_map(maps: list, map_id: str | None):
    for m in maps:
        if m.p2map_id == map_id:
            return m
    if map_id:
        raise ActionError("That map no longer exists. Reload the map.")
    return maps[0]


async def load_bundle(robot, m) -> dict[str, Any]:
    link = await robot.get_map_geojson_link(m.p2map_id, m.active_p2mapv_id)
    url = link.get("map_url") if isinstance(link, dict) else None
    if not url:
        raise ActionError("iRobot didn't return a map download link.")
    return parse_map_bundle(await robot.download_map_bundle(url))


def features(bundle: dict[str, Any], key: str) -> list[dict[str, Any]]:
    data = bundle.get(key)
    if isinstance(data, dict):
        # Usually a FeatureCollection; borders comes as a single Feature.
        data = [data] if data.get("type") == "Feature" else data.get("features")
    return [f for f in data or [] if isinstance(f, dict)]


def ring(polygon: Polygon) -> list[list[float]]:
    return [[x, y] for x, y in polygon.coordinates[0]] if polygon.coordinates else []


def policy_zones(bundle: dict[str, Any]) -> list[PolicyZoneFeature]:
    zones = []
    for f in features(bundle, "policyZones"):
        try:
            zones.append(PolicyZoneFeature.from_json(f))
        except Exception:
            continue
    return zones


async def map_view(robot, map_id: str | None, every: bool = False) -> dict[str, Any]:
    """The chosen map (or the first), plus every other map when `every` is set,
    so the page can switch maps without asking again."""
    maps = await robot_maps(robot)
    m = next((x for x in maps if x.p2map_id == map_id), maps[0])
    view = {"maps": [{"id": x.p2map_id, "name": x.name or "Map"} for x in maps],
            "map": await one_map(robot, m)}
    if every:
        view["views"] = {m.p2map_id: view["map"]}
        for other in maps:
            if other.p2map_id != m.p2map_id:
                try:
                    view["views"][other.p2map_id] = await one_map(robot, other)
                except (CloudError, aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError):
                    pass   # the page loads it on demand instead
    return view


async def one_map(robot, m) -> dict[str, Any]:
    bundle = await load_bundle(robot, m)
    names = await robot.get_map_region_names(m.p2map_id, m.active_p2mapv_id)
    meta = {r.room_id: r for r in m.rooms_metadata}

    outlines = room_outlines(bundle)
    rooms = []
    for f in features(bundle, "rooms"):
        try:
            room = RoomFeature.from_json(f)
        except Exception:
            continue
        rid = str(room.feature_id)
        info = meta.get(rid)
        modes = sorted(int(k) for k in (info.operating_mode_defaults if info else {}) if k.isdigit())
        rooms.append({
            "id": rid,
            "name": names.get(rid) or room.properties.name or (info.name if info else None) or f"Room {rid}",
            # The tidy outline the official app fills, and the robot's raw
            # outline of the floor it has seen (drawn as texture).
            "ring": outlines.get(rid) or simplified_ring(f) or ring(room.geometry),
            "raw": ring(room.geometry),
            "modes": [{"id": k, "label": MODE_LABELS.get(k, f"Mode {k}")} for k in modes],
            # What the robot does in this room for each mode unless told otherwise.
            "defaults": {k: read_prefs(v.to_json() if hasattr(v, "to_json") else plain(v))
                         for k, v in ((info.operating_mode_defaults or {}) if info else {}).items()},
            "last_mode": getattr(info, "last_operating_mode", None) if info else None,
        })

    borders = [line for f in features(bundle, "borders") for line in lines_of(f.get("geometry"))]

    # The straight walls the official app draws round each room. Doorways
    # are left open.
    walls = []
    for f in features(bundle, "floorPlan"):
        kind = str((f.get("properties") or {}).get("type", "")).lower()
        if kind in ("door", "2"):
            continue
        walls.extend(lines_of(f.get("geometry")))

    zones = []
    for z in policy_zones(bundle):
        kind = z.properties.zone_type
        if kind not in ("KeepOutZone", "NoMopZone"):
            continue
        if isinstance(z.geometry, LineString):
            zones.append({"id": z.feature_id, "kind": "wall", "points": [list(p) for p in z.geometry.coordinates]})
        else:
            zones.append({"id": z.feature_id, "kind": "nomop" if kind == "NoMopZone" else "keepout",
                          "points": ring(z.geometry)})

    # Named areas Roomba can clean on their own (Add Clean Zones).
    clean_zones = []
    for f in features(bundle, "cleanZones"):
        for poly in polygons_of(f.get("geometry"))[:1]:
            clean_zones.append({"id": str(f.get("id")), "name": (f.get("properties") or {}).get("name") or "Zone", "points": poly[0]})
    # Carpets: ones you drew (userModified) and ones Roomba found. The last
    # feature without an id is a combined outline and isn't a carpet itself.
    carpets = []
    for f in features(bundle, "floorTypes"):
        props = f.get("properties") or {}
        if f.get("id") is None:
            continue
        for poly in polygons_of(f.get("geometry"))[:1]:
            carpets.append({"id": str(f["id"]), "type": props.get("type") or "carpet", "user": bool(props.get("userModified")),
                            "enabled": props.get("enabled", True), "points": poly[0]})
    thresholds = [{"points": ring_} for f in features(bundle, "policyZones")
                  if (f.get("properties") or {}).get("type") == "Threshold"
                  for ring_ in [p[0] for p in polygons_of(f.get("geometry"))[:1]]]

    # Furniture: pieces you placed (Add Furniture) and any Roomba found.
    furniture = []
    for f in features(bundle, "furniture"):
        props = f.get("properties") or {}
        for poly in polygons_of(f.get("geometry"))[:1]:
            furniture.append({"id": str(f.get("id")) if f.get("id") is not None else None, "type": str(props.get("type") or "unknown").lower(),
                              "user": bool(props.get("userModified", props.get("user_modified", props.get("source") == "user"))),
                              "points": poly[0]})

    dock = None
    for f in features(bundle, "dockPose"):
        coords = (f.get("geometry") or {}).get("coordinates")
        if coords:
            dock = coords[:2]

    scores, needs_cleaning = await clean_scores(robot, m.p2map_id)
    debug_dump(f"map_{m.p2map_id}", {"name": m.name, "files": sorted(bundle), "bundle": bundle,
                                     "region_names": names, "clean_scores": scores})
    return {"id": m.p2map_id, "name": m.name or "Map", "rooms": rooms,
            "borders": borders, "walls": walls, "zones": zones, "dock": dock,
            "clean_zones": clean_zones, "carpets": carpets, "thresholds": thresholds, "furniture": furniture,
            # Room id -> how dirty it is (0 just cleaned, 1 very), and the level
            # at which the official app calls a room due for a clean.
            "scores": scores, "needs_cleaning": needs_cleaning}


def room_outlines(bundle: dict[str, Any]) -> dict[str, list[list[float]]]:
    """Each room's straight outline, joined up from its floor-plan walls and
    doorways. This is the shape the official app fills in."""
    segments: dict[str, list] = {}
    for f in features(bundle, "floorPlan"):
        rid = str((f.get("properties") or {}).get("roomId", ""))
        for line in lines_of(f.get("geometry")):
            for a, b in zip(line, line[1:]):
                segments.setdefault(rid, []).append((tuple(round(v, 3) for v in a[:2]), tuple(round(v, 3) for v in b[:2])))
    out = {}
    for rid, segs in segments.items():
        ring_ = chain(segs)
        if ring_:
            out[rid] = ring_
    return out


def chain(segments: list) -> list[list[float]] | None:
    """Joins segments end to end into one closed ring, or None if they don't close."""
    links: dict[tuple, list[tuple]] = {}
    for a, b in segments:
        if a != b:
            links.setdefault(a, []).append(b)
            links.setdefault(b, []).append(a)
    if not links or any(len(v) != 2 for v in links.values()):
        return None
    start = next(iter(links))
    ring_, prev, here = [start], None, start
    while True:
        nxt = links[here][0] if links[here][0] != prev else links[here][1]
        if nxt == start:
            break
        ring_.append(nxt)
        prev, here = here, nxt
        if len(ring_) > len(links):
            return None
    if len(ring_) != len(links):
        return None   # more than one loop
    return [list(p) for p in ring_ + [start]]


def simplified_ring(feature: dict[str, Any]) -> list[list[float]]:
    geom = (feature.get("properties") or {}).get("simplifiedGeometry") or {}
    coords = geom.get("coordinates") or []
    if geom.get("type") == "Polygon" and coords and coords[0]:
        return [list(p[:2]) for p in coords[0]]
    return []


def lines_of(geom: Any) -> list[list[list[float]]]:
    """Every line or ring in a GeoJSON geometry, as lists of [x, y]."""
    if not isinstance(geom, dict):
        return []
    kind, coords = geom.get("type"), geom.get("coordinates") or []
    if kind == "LineString":
        return [coords]
    if kind == "MultiLineString" or kind == "Polygon":
        return [c for c in coords if c]
    if kind == "MultiPolygon":
        return [ring for poly in coords for ring in poly if ring]
    return []


def polygons_of(geom: Any) -> list[list[list[list[float]]]]:
    """Each polygon in a GeoJSON geometry as [outer ring, holes...]."""
    if not isinstance(geom, dict):
        return []
    kind, coords = geom.get("type"), geom.get("coordinates") or []
    if kind == "Polygon":
        return [[r for r in coords if r]]
    if kind == "MultiPolygon":
        return [[r for r in poly if r] for poly in coords if poly]
    return []


def rounded(rings: Any) -> Any:
    """Coordinates to the centimeter, which keeps the page's data small."""
    if isinstance(rings, (int, float)):
        return round(rings, 2)
    return [rounded(r) for r in rings[:2]] if rings and isinstance(rings[0], (int, float)) else [rounded(r) for r in rings]


def first_present(bundle: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    for n in names:
        if n in bundle:
            return features(bundle, n)
    return []


def live_layers(bundle: dict[str, Any]) -> dict[str, Any]:
    """What the official app draws on the live map during a clean: the area
    Roomba has explored so far, the floor it has covered (with the passes
    that covered it), obstacles it spotted, furniture, the dock, and the
    square of a spot clean (adHocCleanZones), drawn green like the app."""
    explored = [rounded(p) for f in features(bundle, "borders") for p in polygons_of(f.get("geometry"))]
    coverage = []
    for f in first_present(bundle, "coverage", "coverages"):
        modes = (f.get("properties") or {}).get("operatingModes") or []
        coverage += [{"rings": rounded(p), "modes": modes} for p in polygons_of(f.get("geometry"))]
    passes = [rounded(line) for f in first_present(bundle, "trajectories", "trajectory")
              for line in lines_of(f.get("geometry"))]
    hazards = []
    for f in first_present(bundle, "hazard", "hazards"):
        geom = f.get("geometry") or {}
        if geom.get("type") == "Point" and geom.get("coordinates"):
            hazards.append({"id": f.get("id"), "type": str((f.get("properties") or {}).get("type") or "unknown"),
                            "at": rounded(geom["coordinates"][:2])})
    furniture = []
    for f in features(bundle, "furniture"):
        props = f.get("properties") or {}
        for p in polygons_of(f.get("geometry")):
            furniture.append({"type": str(props.get("type") or ""), "rings": rounded(p)})
    spots = [rounded(p) for f in features(bundle, "adHocCleanZones") for p in polygons_of(f.get("geometry"))]
    dock = None
    for f in first_present(bundle, "dockPoses", "dockPose"):
        coords = (f.get("geometry") or {}).get("coordinates")
        if coords:
            dock = rounded(coords[:2])
    return {"explored": explored, "coverage": coverage, "passes": passes,
            "hazards": hazards, "furniture": furniture, "spots": spots, "dock": dock}


def parse_live_bundle(data: bytes) -> dict[str, Any]:
    """The live map arrives as the same kind of archive as a saved map; fall
    back to plain JSON in case a firmware sends that instead."""
    try:
        return parse_map_bundle(data)
    except Exception:
        pass
    try:
        parsed = json.loads(data)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    raise ValueError(f"unrecognized live map format (starts {data[:4].hex()})")


async def clean_scores(robot, map_id: str) -> tuple[dict[str, float], float | None]:
    try:
        raw = await robot.get_clean_score_raw(map_id)
    except (CloudError, aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError):
        return {}, None
    parsed = CleanScoreResponse.from_json(raw if isinstance(raw, dict) else {})
    scores = {}
    for data in parsed.clean_scores:
        if data.p2map_id and data.p2map_id != map_id:
            continue
        for region in data.regions:
            if region.region_id is not None and isinstance(region.clean_score, (int, float)):
                scores[str(region.region_id)] = float(region.clean_score)
    ranges = [r for r in parsed.clean_score_ranges if r > 0]
    return scores, (min(ranges) if ranges else None)


async def apply_edit(robot, map_id: str, command, done: str) -> str:
    result = await robot.edit_map_checked(map_id, command)
    if result.is_error:
        reason = result.error.name if result.error else (result.error_message or f"code {result.error_code}")
        raise ActionError(f"iRobot rejected the edit: {reason}")
    if result.is_partial:
        return done + " The map picture may take a while to catch up."
    return done


async def edit(robot, action: str, body: dict[str, Any]) -> str:
    m = pick_map(await robot_maps(robot), body.get("map_id"))
    if action == "rename":
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 40:
            raise ActionError("Room names need 1 to 40 characters.")
        return await apply_edit(robot, m.p2map_id, SetRoomMetadataV1(room_id=str(body["room_id"]), name=name),
                                f"Renamed the room to '{name}'.")
    if action == "merge":
        ids = [str(i) for i in body.get("room_ids", [])]
        if len(ids) < 2:
            raise ActionError("Pick at least two neighboring rooms to merge.")
        return await apply_edit(robot, m.p2map_id, MergeRoomsV1(ids=ids), "Merged the rooms.")
    if action == "split":
        points = [(float(x), float(y)) for x, y in body.get("points", [])]
        if len(points) != 2:
            raise ActionError("A split line needs exactly two points.")
        return await apply_edit(robot, m.p2map_id, SplitRoomV1(room_id=str(body["room_id"]), split_points=points),
                                "Split the room. Check the new boundary on the map.")

    if action == "clean_zones_set":
        return await set_clean_zones(robot, m, body)
    if action == "carpets_set":
        return await set_carpets(robot, m, body)
    if action == "furniture_set":
        return await set_furniture(robot, m, body)

    # Zones: the robot keeps keep-out zones, no-mop zones, and virtual walls
    # in ONE list that every write replaces, so read the current list, change
    # it, and send all of it back. Sending only the new zone would erase the
    # rest.
    bundle = await load_bundle(robot, m)
    walls = policy_zones_to_virtual_walls(policy_zones(bundle))
    raw = [f for f in features(bundle, "policyZones")
           if (f.get("properties") or {}).get("type") != "Threshold"]
    if len(walls) != len(raw) or any(len(w.to_json()) != 10 for w in walls):
        # Something in the current list couldn't be read back, and writing
        # without it would delete it from the robot. Refuse instead.
        raise ActionError("The map has a zone this app can't read, so it won't risk rewriting the "
                          "zone list. Edit zones in the iRobot app instead.")
    if action == "zone_add":
        pts = [(float(x), float(y)) for x, y in body.get("points", [])]
        if len(pts) != 4:
            raise ActionError("A zone needs four corners.")
        numeric = [int(w.wall_id) for w in walls if str(w.wall_id).isdigit()]
        new_id = str(max(numeric, default=0) + 1)
        polygon = Polygon(coordinates=[pts + [pts[0]]])
        if body.get("kind") == "nomop":
            walls.append(VirtualWallNoMopZoneV1(wall_id=new_id, polygon=polygon))
            label = "no-mop zone"
        else:
            walls.append(VirtualWallRectangleV1(wall_id=new_id, polygon=polygon))
            label = "keep-out zone"
        return await apply_edit(robot, m.p2map_id, SetVirtualWallsV1(walls=walls), f"Added the {label}.")
    if action == "zone_remove":
        zone_id = str(body.get("zone_id"))
        kept = [w for w in walls if str(w.wall_id) != zone_id]
        if len(kept) == len(walls):
            raise ActionError("That zone is already gone. Reload the map.")
        return await apply_edit(robot, m.p2map_id, SetVirtualWallsV1(walls=kept), "Removed the zone.")
    if action == "zones_set":
        # The zone editor sends the whole list it wants: existing zones by id
        # (with new corners if they were moved) and new ones without an id.
        # Virtual-wall lines aren't shown in the editor and are kept as-is.
        wanted = body.get("zones")
        if not isinstance(wanted, list) or len(wanted) > 50:
            raise ActionError("The zone list didn't come through. Try again.")
        zone_types = (VirtualWallRectangleV1, VirtualWallNoMopZoneV1)
        by_id = {str(w.wall_id): w for w in walls if isinstance(w, zone_types)}
        result = [w for w in walls if not isinstance(w, zone_types)]
        numeric = [int(w.wall_id) for w in walls if str(w.wall_id).isdigit()]
        next_id = max(numeric, default=0) + 1
        for z in wanted:
            zid = str(z.get("id")) if z.get("id") is not None else None
            old = by_id.get(zid) if zid else None
            if old is not None and not z.get("points"):
                result.append(old)
                continue
            pts = [(float(x), float(y)) for x, y in (z.get("points") or [])]
            if len(pts) != 4:
                raise ActionError("Every zone needs four corners.")
            if old is None:
                zid, next_id = str(next_id), next_id + 1
            polygon = Polygon(coordinates=[pts + [pts[0]]])
            kind = VirtualWallNoMopZoneV1 if z.get("kind") == "nomop" else VirtualWallRectangleV1
            result.append(kind(wall_id=zid, polygon=polygon))
        return await apply_edit(robot, m.p2map_id, SetVirtualWallsV1(walls=result), "Saved the zones.")
    raise ActionError("Unknown edit.")


async def apply_edit_v2(robot, map_id: str, command, done: str) -> str:
    result = await robot.edit_map_v2_checked(map_id, command)
    if result.is_error:
        reason = result.error.name if result.error else (result.error_message or f"code {result.error_code}")
        raise ActionError(f"iRobot rejected the edit: {reason}")
    return done + (" The map picture may take a while to catch up." if result.is_partial else "")


def box_points(z: dict[str, Any]) -> list[tuple[float, float]]:
    pts = [(float(x), float(y)) for x, y in z.get("points", [])]
    if len(pts) < 3:
        raise ActionError("Every area needs at least three corners.")
    return pts if pts[0] == pts[-1] else pts + [pts[0]]


async def set_clean_zones(robot, m, body: dict[str, Any]) -> str:
    """The editor sends every clean zone it wants. Like the official app:
    add or update those (an id updates), then delete the ones left out."""
    wanted = body.get("zones")
    if not isinstance(wanted, list) or len(wanted) > 30:
        raise ActionError("The zone list didn't come through. Try again.")
    bundle = await load_bundle(robot, m)
    current = {str(f.get("id")): f for f in features(bundle, "cleanZones")}
    keep, zones = set(), []
    for z in wanted:
        zid = str(z["id"]) if z.get("id") is not None else None
        if zid is not None and zid not in current:
            raise ActionError("A zone changed somewhere else. Reload the map and try again.")
        name = str(z.get("name") or (current.get(zid, {}).get("properties") or {}).get("name") or "Zone")[:40]
        if z.get("points"):
            geometry = Polygon(coordinates=[box_points(z)])
        else:
            geometry = Polygon(coordinates=polygons_of(current[zid].get("geometry"))[0])
        zones.append(CleanZone(name=name, geometry=geometry, zone_id=zid))
        if zid:
            keep.add(zid)
    gone = [zid for zid in current if zid not in keep]
    done = []
    if zones:
        done.append(await apply_edit_v2(robot, m.p2map_id, AddCleanZones(zones=zones), "Saved the clean zones."))
    if gone:
        done.append(await apply_edit_v2(robot, m.p2map_id, DeleteCleanZones(zone_ids=gone), f"Removed {len(gone)} clean zone{'s' if len(gone) != 1 else ''}."))
    return " ".join(done) or "Nothing to change."


async def set_carpets(robot, m, body: dict[str, Any]) -> str:
    """The robot keeps all carpets in one list that every write replaces, so
    the ones Roomba detected itself are sent back exactly as they are."""
    wanted = body.get("carpets")
    if not isinstance(wanted, list) or len(wanted) > 40:
        raise ActionError("The carpet list didn't come through. Try again.")
    bundle = await load_bundle(robot, m)
    entries, user_ids = [], []
    for f in features(bundle, "floorTypes"):
        props = f.get("properties") or {}
        if f.get("id") is None:
            continue
        if props.get("userModified"):
            user_ids.append(str(f["id"]))
            continue
        polys = polygons_of(f.get("geometry"))
        if polys:
            entries.append(FloorTypeEntry(floor_type_id=str(f["id"]), type_name=props.get("type") or "carpet", name=props.get("name") or "",
                                          enabled=props.get("enabled", True), user_modified=False, geometry=Polygon(coordinates=polys[0])))
    numeric = [int(i) for i in user_ids if i.isdigit()]
    next_id = max(numeric, default=99) + 1
    for c in wanted:
        cid = str(c["id"]) if c.get("id") is not None else None
        if cid is None:
            cid, next_id = str(next_id), next_id + 1
        entries.append(FloorTypeEntry(floor_type_id=cid, type_name="carpet", name="", enabled=bool(c.get("enabled", True)),
                                      user_modified=True, geometry=Polygon(coordinates=[box_points(c)])))
    return await apply_edit_v2(robot, m.p2map_id, SetFloorTypes(floor_types=entries), "Saved the carpets.")


async def set_furniture(robot, m, body: dict[str, Any]) -> str:
    """Add Furniture, like the official app: every write replaces the whole
    list, so furniture Roomba found itself is sent back as it is. Unmoved
    pieces keep their shape; the type names are the library's (FurnitureType)."""
    wanted = body.get("furniture")
    if not isinstance(wanted, list) or len(wanted) > 40:
        raise ActionError("The furniture list didn't come through. Try again.")
    bundle = await load_bundle(robot, m)
    old = {}
    for f in features(bundle, "furniture"):
        polys = polygons_of(f.get("geometry"))
        if polys and f.get("id") is not None:
            old[str(f["id"])] = (f.get("properties") or {}, polys[0])
    entries = []
    for c in wanted:
        kind = str(c.get("type", "")).upper()
        if kind not in FurnitureType.__members__:
            raise ActionError(f"Unknown furniture type: {c.get('type')}")
        fid = str(c["id"]) if c.get("id") is not None else None
        if c.get("points"):
            ring_, user = box_points(c), True
        elif fid in old:   # unchanged: same outline, same owner
            props, poly = old[fid]
            ring_ = [tuple(p) for p in poly[0]]
            user = bool(props.get("userModified", props.get("user_modified", True)))
        else:
            raise ActionError("A piece of furniture is missing its outline.")
        entries.append(Furniture(furniture_type=FurnitureType[kind], geometry=Polygon(coordinates=[ring_]),
                                 furniture_id=fid, user_modified=user))
    return await apply_edit_v2(robot, m.p2map_id, SetFurniture(furniture=entries), "Saved the furniture.")


# --- cleaning preferences ---------------------------------------------------------
#
# The page describes how to clean each room as a small "prefs" dict:
#   {"mode": 32, "suctionLevel": 4, "liquid": 2, "twoPass": true, "swScrub": 1}
# Missing keys mean "the robot's own default for that room". These are the
# wire values from the app's command parameters (see the decompile notes):
#   mode         2 vacuum only, 32 vacuum and mop, 4 mop only, 512 vacuum then mop
#   suctionLevel 1 quiet .. 4 max
#   liquid       0 low, 1 standard, 2 high (sent as padWetness)
#   twoPass      false one pass, true two passes
#   swScrub      0 off, 1 SmartScrub on

PREF_KEYS = ("mode", "suctionLevel", "liquid", "twoPass", "swScrub")


def clean_prefs(raw: Any) -> dict[str, Any]:
    """Only the known keys, with values in range; anything else is dropped."""
    prefs: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return prefs
    if raw.get("mode") in (2, 4, 32, 512):
        prefs["mode"] = int(raw["mode"])
    if raw.get("suctionLevel") in (1, 2, 3, 4):
        prefs["suctionLevel"] = int(raw["suctionLevel"])
    if raw.get("liquid") in (0, 1, 2):
        prefs["liquid"] = int(raw["liquid"])
    if isinstance(raw.get("twoPass"), bool):
        prefs["twoPass"] = raw["twoPass"]
    if raw.get("swScrub") in (0, 1):
        prefs["swScrub"] = int(raw["swScrub"])
    return prefs


def apply_prefs(params: dict[str, Any], prefs: dict[str, Any]) -> dict[str, Any]:
    """Region params (wire format) with the chosen prefs written over them.
    Everything else already in params (from a routine or the room's
    defaults) is kept."""
    params = json.loads(json.dumps(params or {}))
    if "mode" in prefs:
        params["operatingMode"] = prefs["mode"]
    if "suctionLevel" in prefs:
        params["suctionLevel"] = prefs["suctionLevel"]
    if "twoPass" in prefs:
        params["twoPass"] = prefs["twoPass"]
    if "swScrub" in prefs:
        params["swScrub"] = prefs["swScrub"]
    if "liquid" in prefs:
        level = prefs["liquid"]
        # padPlate counts from 1 where the other two pad types count from 0
        # (PadWetnessParam in the library).
        params["padWetness"] = {"disposable": level, "reusable": level, "padPlate": level + 1}
    if "operatingMode" not in params:
        params["operatingMode"] = 2
    return params


def read_prefs(params: Any) -> dict[str, Any]:
    """The prefs dict back out of region params, for the page."""
    if not isinstance(params, dict):
        return {}
    prefs: dict[str, Any] = {}
    mode = params.get("operatingMode", params.get("operating_mode"))
    if isinstance(mode, (int, float)):
        prefs["mode"] = int(mode)
    suction = params.get("suctionLevel", params.get("suctionLvl"))
    if isinstance(suction, (int, float)):
        prefs["suctionLevel"] = int(suction)
    if isinstance(params.get("twoPass"), bool):
        prefs["twoPass"] = params["twoPass"]
    if isinstance(params.get("swScrub"), (int, float)):
        prefs["swScrub"] = int(params["swScrub"])
    wet = params.get("padWetness")
    if isinstance(wet, dict):
        level = wet.get("disposable", wet.get("reusable"))
        if isinstance(level, (int, float)) and 0 <= level <= 2:
            prefs["liquid"] = int(level)
    return prefs


def room_list(body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """[(room id, prefs)] in cleaning order. Accepts {"rooms": [{id, mode, ...}]}
    (prefs keys may sit beside id or under "prefs") or {"room_ids": [...], "mode": n}.
    Body-level prefs apply to rooms that don't set their own."""
    shared = clean_prefs(body.get("prefs") or {"mode": body.get("mode")})
    out = []
    if isinstance(body.get("rooms"), list):
        for r in body["rooms"]:
            if isinstance(r, dict) and r.get("id") is not None:
                own = clean_prefs(r.get("prefs") if isinstance(r.get("prefs"), dict) else r)
                if r.get("zone"):
                    own["zone"] = True   # a saved clean zone, not a room
                out.append((str(r["id"]), {**shared, **own}))
        return out
    return [(str(i), dict(shared)) for i in body.get("room_ids", [])]


def room_defaults(m) -> dict[str, dict[str, Any]]:
    """Room id -> {mode as string: params the robot uses for that mode}."""
    out = {}
    for info in m.rooms_metadata:
        out[info.room_id] = {k: (v.to_json() if hasattr(v, "to_json") else plain(v))
                             for k, v in (info.operating_mode_defaults or {}).items()}
    return out


def region_for(defaults: dict[str, dict[str, Any]], rid: str, prefs: dict[str, Any],
               base: dict[str, Any] | None = None) -> dict[str, Any]:
    """One region in wire format. `base` is the room's existing params (from a
    routine); otherwise the room's own defaults for the chosen mode are used."""
    if base is None:
        mode = prefs.get("mode", 2)
        base = (defaults.get(rid) or {}).get(str(mode)) or {}
    kind = "zid" if prefs.get("zone") else "rid"
    prefs = {k: v for k, v in prefs.items() if k != "zone"}
    return {"region_id": rid, "type": kind, "params": apply_prefs(base, prefs)}


def clean_command(robot, m, rooms: list[tuple[str, dict[str, Any]]],
                  mission: dict[str, Any] | None = None) -> RoutineCommand:
    """A room clean when rooms are named, otherwise a whole-house clean.

    The field-confirmed room shape is START, a real map id, and non-empty
    regions. Without regions the robot cleans the whole house, so that only
    happens when no rooms were picked on purpose. For a whole-house clean the
    prefs go on the mission itself."""
    defaults = room_defaults(m)
    regions = [region_for(defaults, rid, prefs) for rid, prefs in rooms]
    params = None
    if not regions and mission:
        params = apply_prefs({}, mission)
    return RoutineCommand(command_type=MissionCommandType.START, asset_id=robot.blid,
                          map_id=m.p2map_id, ordered=1 if regions else 0,
                          regions=regions or None, params=params, initiator="rmtApp")


async def clean_rooms(robot, body: dict[str, Any]) -> str:
    rooms = room_list(body)
    whole = bool(body.get("everywhere"))
    if not rooms and not whole:
        raise ActionError("Pick at least one room first.")
    m = pick_map(await robot_maps(robot), body.get("map_id"))
    command = clean_command(robot, m, [] if whole else rooms, clean_prefs(body.get("prefs")))
    if not await robot.send_routine_command_via_cmd_topic(command):
        raise ActionError("The clean was sent but iRobot's cloud didn't confirm delivery.")
    if whole:
        return "Cleaning all rooms."
    return f"Cleaning {len(rooms)} room{'s' if len(rooms) != 1 else ''}."


async def point_clean(robot, body: dict[str, Any]) -> str:
    """Sends Roomba to one spot on a map to clean around it.

    Same shape the official app's PointCleanCommandBuilder builds: command
    point_clean, the map id and version, and geom {"center": [x, y]} in map
    meters. The robot advertises it (cap.dSpot = 1), but it hasn't been run on
    this robot from this app yet, and how big a patch it cleans is unknown."""
    point = body.get("point")
    try:
        x, y = (float(v) for v in point)
    except (TypeError, ValueError):
        raise ActionError("Tap the spot on the map first.") from None
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ActionError("Tap the spot on the map first.")
    m = pick_map(await robot_maps(robot), body.get("map_id"))
    prefs = clean_prefs(body.get("prefs") or {"mode": body.get("mode")})
    if prefs.get("mode") == 512:   # Vac then Mop is for rooms, not a spot
        prefs.pop("mode")
    # Without these the robot picks its own spot settings (top suction).
    # Seth's defaults: suction 2 of 4, two passes; a caller can override.
    prefs.setdefault("mode", 2)
    if prefs["mode"] != 4:
        prefs.setdefault("suctionLevel", 2)
    prefs.setdefault("twoPass", True)
    command = RoutineCommand(command_type=MissionCommandType.POINT_CLEAN, asset_id=robot.blid,
                             map_id=m.p2map_id, pmap_version_id=m.active_p2mapv_id,
                             spot_geometry={"center": [round(x, 3), round(y, 3)]},
                             params=apply_prefs({}, prefs), initiator="rmtApp")
    if not await robot.send_routine_command_via_cmd_topic(command):
        raise ActionError("The spot clean was sent but iRobot's cloud didn't confirm delivery.")
    return "Spot clean sent. Watch Roomba to check it went to the right place."


def confirm(body: dict[str, Any], word: str) -> None:
    # The page asks for the word too; this stops a stray request from
    # doing something permanent without it.
    if body.get("confirm") != word:
        raise ActionError(f"Not confirmed. Type {word} exactly to go ahead.")


def plain(value: Any) -> Any:
    """Library results as JSON-safe data for the page."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return json.loads(json.dumps(value, default=str))


def reported(response) -> dict[str, Any]:
    payload = getattr(response, "payload", None)
    payload = payload if isinstance(payload, dict) else {}
    return (payload.get("state") or {}).get("reported") or {}


# --- routines (favorites) ---------------------------------------------------------

def region_dicts(command) -> list[dict[str, Any]]:
    out = []
    for region in command.regions or []:
        out.append(dict(region) if isinstance(region, dict) else region.to_json())
    return out


def region_mode(region: dict[str, Any]) -> int | None:
    params = region.get("params") or {}
    mode = params.get("operatingMode", params.get("operating_mode")) if isinstance(params, dict) else None
    return int(mode) if isinstance(mode, (int, float)) else None


async def routines(robot) -> dict[str, Any]:
    favs = [f for f in await robot.get_favorites() if not f.is_deleted and not f.is_hidden]
    favs.sort(key=lambda f: (f.display_order is None, f.display_order or 0))
    items = []
    for f in favs:
        steps = mine(robot, f)
        if not f.favorite_id or not steps:
            continue
        regions = region_dicts(steps[0])
        items.append({
            "id": f.favorite_id, "name": f.name or "Routine", "color": f.color, "icon": f.icon,
            # Only single-step routines map onto the editor; others still run.
            "editable": len(steps) == 1,
            "map_id": steps[0].map_id,
            "rooms": [{"id": str(r.get("region_id") or r.get("id")), "mode": region_mode(r),
                       "kind": r.get("type"), "prefs": read_prefs(r.get("params"))} for r in regions],
            "prefs": read_prefs(steps[0].params.to_json() if hasattr(steps[0].params, "to_json")
                                else steps[0].params),
        })
    debug_dump("routines", [plain(f) for f in favs])
    return {"routines": items}


def mine(robot, favorite) -> list:
    """This robot's runnable steps in a routine. A routine can include other
    robots, and only cleaning starts are sent; anything else is skipped."""
    return [c for c in favorite.command_defs
            if c.asset_id == robot.blid and str(c.command_type) in ("start", "clean")]


async def find_routine(robot, favorite_id: str):
    favorite = next((f for f in await robot.get_favorites() if f.favorite_id == favorite_id), None)
    if favorite is None or favorite.is_deleted:
        raise ActionError("That routine no longer exists. Reload the list.")
    return favorite


async def run_routine(robot, body: dict[str, Any]) -> str:
    favorite = await find_routine(robot, str(body.get("favorite_id")))
    steps = mine(robot, favorite)
    if not steps:
        raise ActionError("That routine has nothing this app can run for this robot.")
    for command in steps:
        if not await robot.send_routine_command_via_cmd_topic(command):
            raise ActionError(f"'{favorite.name}' was sent but iRobot's cloud didn't confirm delivery.")
    return f"Routine '{favorite.name}' started. Check the robot to confirm it reacted."


def clean_name(body: dict[str, Any], what: str) -> str:
    name = str(body.get("name", "")).strip()
    if not name or len(name) > 40:
        raise ActionError(f"{what} names need 1 to 40 characters.")
    return name


async def create_routine(robot, body: dict[str, Any]) -> str:
    name = clean_name(body, "Routine")
    m = pick_map(await robot_maps(robot), body.get("map_id"))
    command = clean_command(robot, m, room_list(body))
    await robot.create_favorite(FavoriteV1(name=name, command_defs=[command]))
    return f"Saved routine '{name}'."


async def update_routine(robot, body: dict[str, Any]) -> str:
    favorite = await find_routine(robot, str(body.get("favorite_id")))
    steps = mine(robot, favorite)
    if len(steps) != 1:
        raise ActionError("This routine has several steps, so edit it in the iRobot app.")
    old = steps[0]
    name = clean_name(body, "Routine")
    m = pick_map(await robot_maps(robot), old.map_id or body.get("map_id"))

    # Rooms already in the routine keep every setting they had; only the
    # prefs the page sent are written over them.
    previous = {str(r.get("region_id") or r.get("id")): r for r in region_dicts(old)}
    defaults = room_defaults(m)
    regions = []
    for rid, prefs in room_list(body):
        if rid in previous:
            region = json.loads(json.dumps(previous[rid]))
            region["params"] = apply_prefs(region.get("params") if isinstance(region.get("params"), dict) else {},
                                           prefs)
        else:
            region = region_for(defaults, rid, prefs)
        regions.append(region)

    command = dataclasses.replace(old, map_id=old.map_id or m.p2map_id, regions=regions or None,
                                  ordered=1 if regions else 0)
    defs = [command if c is old else c for c in favorite.command_defs]
    await robot.update_favorite(favorite.favorite_id,
                                dataclasses.replace(favorite, name=name, command_defs=defs))
    return f"Saved changes to '{name}'."


async def delete_routine(robot, body: dict[str, Any]) -> str:
    confirm(body, "DELETE")
    favorite = await find_routine(robot, str(body.get("favorite_id")))
    await robot.delete_favorite(favorite.favorite_id)
    return f"Deleted routine '{favorite.name}'."


# --- schedules --------------------------------------------------------------------

async def household(robot) -> str:
    household_id = await robot.get_household_id()
    if not household_id:
        raise ActionError("Couldn't find the iRobot household this robot belongs to.")
    return household_id


def is_mine(robot, options) -> bool:
    return options.asset_id in (None, robot.robot_id, robot.blid)


async def my_schedules(robot, household_id: str) -> list[tuple[str, HouseholdSchedule]]:
    found = []
    for group in (await robot.get_schedules(household_id)).household_schedules:
        for entry in group.schedules:
            if not isinstance(entry, dict):
                continue
            schedule = HouseholdSchedule.from_json(entry)
            if group.household_schedule_id and is_mine(robot, schedule.options) and not schedule.options.deleted:
                found.append((group.household_schedule_id, schedule))
    return found


async def schedules(robot) -> dict[str, Any]:
    items = []
    for group_id, s in await my_schedules(robot, await household(robot)):
        o = s.options
        rooms = sum(len(c.get("regions") or []) if isinstance(c, dict) else len(c.regions or [])
                    for c in o.commands)
        items.append({
            "id": group_id, "name": o.name or "Schedule", "enabled": bool(o.enabled),
            "frequency": str(o.frequency) if o.frequency else None,
            "days": o.start.day if o.start else [],
            "hour": o.start.hour if o.start else None, "minute": o.start.min if o.start else None,
            "rooms": rooms,
        })
    return {"schedules": items}


async def find_schedule(robot, household_id: str, group_id: str) -> HouseholdSchedule:
    for gid, s in await my_schedules(robot, household_id):
        if gid == group_id:
            return s
    raise ActionError("That schedule no longer exists. Reload the list.")


async def toggle_schedule(robot, body: dict[str, Any]) -> str:
    household_id = await household(robot)
    group_id = str(body.get("schedule_id"))
    s = await find_schedule(robot, household_id, group_id)
    enabled = bool(body.get("enabled"))
    # Everything else about the schedule is sent back exactly as it came.
    updated = HouseholdSchedule(schedule_id=s.schedule_id,
                                options=dataclasses.replace(s.options, enabled=enabled))
    await robot.update_schedules(household_id, group_id, [updated])
    return f"Schedule '{s.options.name}' turned {'on' if enabled else 'off'}."


def when(body: dict[str, Any]) -> tuple[str, ScheduleTime]:
    name = clean_name(body, "Schedule")
    days = sorted({int(d) for d in body.get("days", []) if 0 <= int(d) <= 6})
    hour, minute = int(body.get("hour", -1)), int(body.get("minute", -1))
    if not days or not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ActionError("Pick at least one day and a time.")
    return name, ScheduleTime(day=days, hour=hour, min=minute)


async def update_schedule(robot, body: dict[str, Any]) -> str:
    name, start = when(body)
    household_id = await household(robot)
    group_id = str(body.get("schedule_id"))
    s = await find_schedule(robot, household_id, group_id)
    # What it cleans is sent back untouched; only name and timing change.
    options = dataclasses.replace(s.options, name=name, start=start)
    await robot.update_schedules(household_id, group_id,
                                 [HouseholdSchedule(schedule_id=s.schedule_id, options=options)])
    return f"Saved changes to '{name}'."


async def create_schedule(robot, body: dict[str, Any]) -> str:
    name, start = when(body)
    household_id = await household(robot)
    m = pick_map(await robot_maps(robot), body.get("map_id"))
    options = ScheduleOptions(
        asset_id=robot.robot_id, name=name, frequency=ScheduleFrequency.WEEKLY,
        start=start, commands=[clean_command(robot, m, room_list(body))], enabled=True)
    await robot.create_schedules(household_id, [options])
    return f"Created schedule '{name}'. Check it in the iRobot app, since schedule creation is lightly tested."


async def delete_schedule(robot, body: dict[str, Any]) -> str:
    confirm(body, "DELETE")
    household_id = await household(robot)
    group_id = str(body.get("schedule_id"))
    s = await find_schedule(robot, household_id, group_id)
    # The outer (household) schedule id is the one the server accepts.
    await robot.delete_schedule(household_id, group_id)
    return f"Deleted schedule '{s.options.name}'."


# --- Do Not Disturb -------------------------------------------------------------------

async def dnd(robot) -> dict[str, Any]:
    return {"data": plain(await robot.get_dnd_settings(await household(robot)))}


def clock(text: Any) -> tuple[int, int]:
    try:
        hour, minute = (int(x) for x in str(text).split(":"))
    except ValueError:
        raise ActionError("Times need to look like 22:30.")
    return hour, minute


async def set_dnd(robot, body: dict[str, Any]) -> str:
    household_id = await household(robot)
    if body.get("kind") == "daily":
        try:
            setting = DNDDailySchedule.from_clock(*clock(body.get("start")), *clock(body.get("end")))
        except ValueError:
            raise ActionError("Those aren't valid clock times.")
        await robot.set_dnd_settings(household_id, setting.to_json())
        return f"Do Not Disturb set for {body.get('start')} to {body.get('end')} every day."
    ends_at = int(body.get("ends_at", 0))
    if ends_at <= 0:
        raise ActionError("Pick when Do Not Disturb should end.")
    await robot.set_dnd_settings(household_id, DNDEndsAt(ends_at=ends_at).to_json())
    return "Do Not Disturb turned on until the time you picked."


# --- robot settings ---------------------------------------------------------------------

async def settings(robot) -> dict[str, Any]:
    data = reported(await robot.get_settings())
    debug_dump("settings", data)
    return {"data": data}


async def set_setting(robot, body: dict[str, Any]) -> str:
    key = str(body.get("key", "")).strip()
    if not key or len(key) > 64:
        raise ActionError("A setting name is required.")
    await robot.set_setting(key, body.get("value"))
    current = reported(await robot.get_settings()).get(key, "(not reported)")
    return f"Set {key}. The robot now reports: {json.dumps(current)}"


async def parts(robot) -> dict[str, Any]:
    return {"data": plain(await robot.get_robot_parts())}


def timeline_summary(events: list) -> list[dict[str, Any]]:
    """The parts of a clean's timeline the official Details page shows:
    rooms visited (with area), bin emptying, and errors, in order."""
    out = []
    for ev in events or []:
        e = plain(ev)
        item = {"start": e.get("start_time"), "end": e.get("end_time"), "type": e.get("event_type")}
        room = e.get("room") or {}
        if room:
            item.update(room=str(room.get("rid") or room.get("region_id") or ""), area=room.get("total_area") or room.get("area"))
        elif (e.get("error") or {}).get("value") is not None:
            item["error"] = e["error"]["value"]
        elif e.get("evac") is not None:
            item["evac"] = True
        elif (e.get("command") or {}).get("command"):
            item["command"] = e["command"]["command"]
        else:
            continue
        out.append(item)
    return out[:200]


async def history(robot) -> dict[str, Any]:
    entries = parse_mission_history(await robot.get_mission_history(robot.blid, max_reports=50))
    out = []
    for e in entries:
        item = {k: v for k, v in plain(e).items() if k not in ("raw", "timeline")}
        item["events"] = timeline_summary(getattr(e, "timeline", None))
        out.append(item)
    return {"history": out}


async def about(robot) -> dict[str, Any]:
    """Robot Settings > About in the official app: model, serial number,
    software, and the parts catalog (names and shop links) for Robot Health."""
    classic = reported(await robot.get_state())
    sku = classic.get("sku")
    out: dict[str, Any] = {"sku": sku, "softwareVer": classic.get("softwareVer"), "cap": classic.get("cap") or {}}
    with_errors = []
    for key, call in (("serial", lambda: robot.get_serial_number_data()),
                      ("firmware", lambda: robot.get_firmware(sku)),
                      ("catalog", lambda: robot._rest.get_parts_catalog(sku))):
        if key != "serial" and not sku:
            continue
        try:
            out[key] = plain(await call())
        except Exception as err:   # each part is optional
            with_errors.append(f"{key}: {type(err).__name__}")
    out["errors"] = with_errors
    debug_dump("about", out)
    return out


async def notifications(robot) -> dict[str, Any]:
    data = plain(await robot.get_notifications())
    debug_dump("notifications", data)
    return {"data": data}


async def robot_command(robot, body: dict[str, Any], command: str, word: str | None = None) -> str:
    if word:
        confirm(body, word)
    return await simple_command(robot, command)


async def status(robot) -> dict[str, Any]:
    """Battery, mission, and bin. On these robots most of it lives in the
    "ro-currentstate" shadow; the classic shadow has the name and features."""
    data, raw, failed = {}, {}, None
    for name in (None, "ro-currentstate"):
        try:
            got = reported(await (robot.get_state() if name is None else robot.get_named_shadow(name)))
        except (CloudError, asyncio.TimeoutError, RuntimeError) as err:
            failed = err
            continue
        raw[name or "classic"] = got
        data.update(got)
    if not data:
        raise failed or ActionError("The robot didn't report its status.")
    debug_dump("status", raw)
    return {"data": data}


async def estimates(robot) -> dict[str, Any]:
    """How long each room takes, per map and per setting, as iRobot predicts."""
    raw = await robot.get_time_estimates()
    debug_dump("estimates", raw)
    out = {}
    for smart_map in (raw or {}).get("smart_maps") or []:
        areas = {}
        for area in smart_map.get("areas") or []:
            rows = [[e.get("value"), e.get("params") or {}] for e in area.get("estimates") or []
                    if isinstance(e.get("value"), (int, float)) and e.get("unit", "seconds") == "seconds"]
            if area.get("area_id") is not None and rows:
                areas[str(area["area_id"])] = rows
        out[str(smart_map.get("smart_map_id"))] = areas
    return {"maps": out}


# --- read-only details -------------------------------------------------------------------

async def info(robot, body: dict[str, Any]) -> dict[str, Any]:
    what = body.get("what")
    if what == "status":
        data = reported(await robot.get_state())
    elif what == "robot":
        data = plain(await robot.get_serial_number_data())
    elif what == "parts":
        data = plain(await robot.get_robot_parts())
    elif what == "firmware":
        data = [plain(f) for f in await robot.get_firmware()]
    elif what == "history":
        entries = parse_mission_history(await robot.get_mission_history(robot.blid, max_reports=25))
        data = [{k: v for k, v in plain(e).items() if k not in ("raw", "timeline")} for e in entries]
    elif what == "notifications":
        data = plain(await robot.get_notifications())
    elif what == "estimates":
        data = plain(await robot.get_time_estimates())
    elif what == "automations":
        data = plain(await robot.get_automations_raw())
    elif what in ("clean_score", "profiles"):
        m = pick_map(await robot_maps(robot), body.get("map_id"))
        data = plain(await robot.get_clean_score_raw(m.p2map_id) if what == "clean_score"
                     else await robot.get_cleaning_profiles(robot.blid, m.p2map_id))
    else:
        raise ActionError("Unknown details.")
    return {"data": data}


async def reset_part(robot, body: dict[str, Any]) -> str:
    confirm(body, "RESET")
    part_id = str(body.get("part_id", ""))
    known = {p.part_id for p in (await robot.get_robot_parts()).parts}
    if part_id not in known:
        raise ActionError("That part isn't on this robot. Reload the parts list.")
    await robot.reset_robot_parts([part_id])
    return f"Reset the wear counter for part {part_id}."


# --- whole-map and robot-level actions -------------------------------------------------------

async def map_admin(robot, action: str, body: dict[str, Any]) -> str:
    m = pick_map(await robot_maps(robot), body.get("map_id"))
    if action == "map_rename":
        name = clean_name(body, "Map")
        await robot.set_map_name(m.p2map_id, name)
        return f"Renamed the map to '{name}'."
    if action == "map_rotate":
        degrees = float(body.get("degrees", 0))
        await robot.set_map_orientation(m.p2map_id, math.radians(degrees % 360))
        return f"Map rotation set to {degrees % 360:g} degrees."
    confirm(body, "DELETE")
    await robot.delete_map(m.p2map_id)
    return f"Deleted the map '{m.name or 'Map'}'. The robot will need to map your home again."


async def factory_reset(robot, body: dict[str, Any]) -> str:
    confirm(body, "RESET")
    # Never live-tested by the library. send_wipe is always sent explicitly so
    # the server's unknown default never decides how much gets erased.
    await robot.reset_robot(send_wipe=bool(body.get("wipe")))
    return "Reset sent. The robot may go offline and need setting up again in the iRobot app."


# --- web server ---------------------------------------------------------------

MAP_EDITS = ("rename", "merge", "split", "zone_add", "zone_remove", "zones_set", "clean_zones_set", "carpets_set",
             "furniture_set")

# action -> (handler(robot, body), needs the MQTT connection)
ACTIONS = {
    **{c: ((lambda c: lambda r, b: simple_command(r, c))(c), True) for c in SIMPLE_COMMANDS},
    "clean_rooms": (clean_rooms, True),
    "point_clean": (point_clean, True),
    **{e: ((lambda e: lambda r, b: edit(r, e, b))(e), False) for e in MAP_EDITS},
    **{a: ((lambda a: lambda r, b: map_admin(r, a, b))(a), False) for a in ("map_rename", "map_rotate", "map_delete")},
    "run_routine": (run_routine, True),
    "create_routine": (create_routine, False),
    "update_routine": (update_routine, False),
    "delete_routine": (delete_routine, False),
    "update_schedule": (update_schedule, False),
    "toggle_schedule": (toggle_schedule, False),
    "create_schedule": (create_schedule, False),
    "delete_schedule": (delete_schedule, False),
    "set_dnd": (set_dnd, False),
    "set_setting": (set_setting, True),
    "info": (info, True),
    "reset_part": (reset_part, False),
    "factory_reset": (factory_reset, False),
    # Asked for on purpose from their own screens, with a confirmation.
    "new_map": (lambda r, b: robot_command(r, b, "train", "MAP"), True),
    "restart": (lambda r, b: robot_command(r, b, "reset", "RESTART"), True),
}

# The data the page shows. All of it is read once at startup and kept here,
# so the page opens with everything already loaded.
READERS = {
    "map": (lambda r, b: map_view(r, b.get("map_id"), bool(b.get("all_maps"))), False),
    "routines": (lambda r, b: routines(r), False),
    "schedules": (lambda r, b: schedules(r), False),
    "dnd": (lambda r, b: dnd(r), False),
    "parts": (lambda r, b: parts(r), False),
    "settings": (lambda r, b: settings(r), True),
    "status": (lambda r, b: status(r), True),
    "history": (lambda r, b: history(r), False),
    "estimates": (lambda r, b: estimates(r), False),
    "about": (lambda r, b: about(r), True),
    "notifications": (lambda r, b: notifications(r), False),
}

# What to re-read after a change, inside the same login as the change.
REFRESH = {
    **{a: ["map"] for a in MAP_EDITS + ("map_rename", "map_rotate", "map_delete")},
    **{a: ["routines"] for a in ("create_routine", "update_routine", "delete_routine")},
    **{a: ["schedules"] for a in ("toggle_schedule", "create_schedule", "update_schedule", "delete_schedule")},
    "set_dnd": ["dnd"],
    "set_setting": ["settings"],
    "reset_part": ["parts"],
    **{a: ["status"] for a in SIMPLE_COMMANDS + ("clean_rooms", "point_clean", "run_routine", "new_map", "restart")},
}


async def read_sections(robot, names: list[str], body: dict[str, Any], connected: list[bool]):
    """Reads several sections in one session. One failing doesn't stop the rest."""
    sections, errors = {}, {}
    for name in names:
        reader, mqtt = READERS[name]
        try:
            if mqtt and not connected[0]:
                await robot.connect()
                connected[0] = True
            sections[name] = await reader(robot, body)
        except ActionError as err:
            errors[name] = str(err)
        except (CloudError, aiohttp.ClientError, asyncio.TimeoutError, RuntimeError,
                KeyError, TypeError, ValueError) as err:
            errors[name] = f"Couldn't load: {type(err).__name__}"
    return sections, errors
