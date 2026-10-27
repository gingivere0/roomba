#!/usr/bin/env python3
"""A small local web app for a Roomba (Prime/V4) covering what the
roombapy-prime library can do.

Usage:
    python roomba_web.py            # this PC only: http://127.0.0.1:8765
    python roomba_web.py --lan      # also reachable from other devices on your network
    python roomba_web.py --forget   # remove the saved login, then ask for it again

What it can do:
  * start, stop, pause, resume, dock, and find (play a sound)
  * show your saved map, clean selected rooms, rename, merge, and split
    rooms, add or remove keep-out and no-mop zones, and rename, rotate, or
    delete a map
  * run, save, and delete routines (iRobot app favorites)
  * list, turn on or off, create, and delete schedules
  * set Do Not Disturb hours
  * read and change robot settings
  * show status, parts wear, cleaning history, firmware, and other details,
    and reset a part's wear counter
  * factory reset the robot
Anything that can't be undone needs a typed confirmation word, checked on
the server as well as in the page.

Every action logs in to iRobot's cloud exactly once (no retries), does its
one job, and disconnects. Only one action runs at a time.

The login is typed once when the app starts (ROOMBA_EMAIL and ROOMBA_COUNTRY
can come from the environment; the password is always typed at a hidden
prompt). On Windows the app offers to remember it in Credential Manager once
iRobot has accepted it, and uses it from then on; see roomba_login.py. It is
never printed, written to a file, or sent to the browser. Start with
--forget, or use Settings > This PC, to remove the saved login.

Every page load gets a random secret that each action must send back. That
stops other websites you visit from pressing the buttons for you, and in
--lan mode it is also the "secret link" another device needs to get in.

Install the reviewed library version first:
    pip install "git+https://github.com/johnnyh1975/roombapy-prime@672456b079b39ccc6bd7519320974441b4307b82"
"""

import argparse
import asyncio
import dataclasses
import hmac
import json
import math
import secrets
import socket
import sys
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
from roombapy_prime.models.map_bundle import PolicyZoneFeature, RoomFeature, parse_map_bundle
from roombapy_prime.models.map_editing import (
    MergeRoomsV1,
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

import roomba_login

SIMPLE_COMMANDS = ("start", "stop", "pause", "resume", "dock")
DEFAULT_PORT = 8765
MODE_LABELS = {2: "Vacuum", 32: "Vacuum and mop", 4: "Mop only"}


class ActionError(Exception):
    """A failure whose message is safe to show in the browser."""


# --- talking to the robot ---------------------------------------------------

class Robot:
    def __init__(self, choice: roomba_login.Choice) -> None:
        self.choice = choice

    async def run(self, job, *, mqtt: bool) -> Any:
        """One login, one job, then disconnect. No retries anywhere."""
        async with aiohttp.ClientSession() as session:
            try:
                # auto_refresh stays off so the library doesn't keep the
                # password around for re-logins.
                login = self.choice.login
                robot = await PrimeFactory.create_prime_robot(
                    session, login.email, login.password, login.country)
            except AuthRateLimitedError as err:
                raise ActionError("Too many active sessions or logins. Sign out of the iRobot app "
                                  f"or wait a while before trying again. iRobot said: {err}")
            except AuthCredentialsError as err:
                # The message is iRobot's own reason (it never contains the
                # password), which is the only way to tell a wrong password
                # from a locked or unverified account.
                note = roomba_login.after_rejection(self.choice, str(err))
                raise ActionError(note or f"Login rejected. iRobot said: {err}")
            except AuthError as err:
                raise ActionError(f"Login failed ({type(err).__name__}): {err}")
            note = roomba_login.after_login(self.choice)
            if note:
                print(note)
            try:
                if mqtt:
                    await robot.connect()
                return await job(robot)
            except ActionError:
                raise
            except (CloudError, aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as err:
                # Type name only: messages can carry URLs or ids.
                raise ActionError(f"iRobot's cloud returned an error: {type(err).__name__}")
            finally:
                await robot.disconnect()


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
        data = data.get("features")
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
            "ring": ring(room.geometry),
            "modes": [{"id": k, "label": MODE_LABELS.get(k, f"Mode {k}")} for k in modes],
        })

    borders = []
    for f in features(bundle, "borders"):
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or []
        polys = coords if geom.get("type") == "MultiPolygon" else [coords]
        borders.extend(p[0] for p in polys if p)

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

    dock = None
    for f in features(bundle, "dockPose"):
        coords = (f.get("geometry") or {}).get("coordinates")
        if coords:
            dock = coords[:2]

    return {"id": m.p2map_id, "name": m.name or "Map", "rooms": rooms,
            "borders": borders, "zones": zones, "dock": dock}


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
    raise ActionError("Unknown edit.")


def room_list(body: dict[str, Any]) -> list[tuple[str, int | None]]:
    """[(room id, mode)] in cleaning order, from either {"rooms": [{id, mode}]}
    or the older {"room_ids": [...], "mode": n}."""
    if isinstance(body.get("rooms"), list):
        return [(str(r["id"]), int(r["mode"]) if r.get("mode") is not None else None)
                for r in body["rooms"] if isinstance(r, dict) and r.get("id") is not None]
    mode = body.get("mode")
    return [(str(i), int(mode) if mode is not None else None) for i in body.get("room_ids", [])]


def room_regions(m, rooms: list[tuple[str, int | None]]) -> list[Region]:
    meta = {r.room_id: r for r in m.rooms_metadata}
    regions = []
    for rid, mode in rooms:
        info = meta.get(rid)
        params = info.operating_mode_defaults.get(str(mode)) if info and mode is not None else None
        if params is None:
            params = CommandParams(operating_mode=mode if mode is not None else 2)
        elif params.operating_mode is None:
            params = dataclasses.replace(params, operating_mode=mode)
        regions.append(Region(region_id=rid, region_type=RegionType.RID, params=params))
    return regions


def clean_command(robot, m, rooms: list[tuple[str, int | None]]) -> RoutineCommand:
    """A room clean when rooms are named, otherwise a whole-house clean.

    The field-confirmed room shape is START, a real map id, and non-empty
    regions. Without regions the robot cleans the whole house, so that only
    happens when no rooms were picked on purpose."""
    regions = room_regions(m, rooms)
    return RoutineCommand(command_type=MissionCommandType.START, asset_id=robot.blid,
                          map_id=m.p2map_id, ordered=1 if regions else 0,
                          regions=regions or None, initiator="rmtApp")


async def clean_rooms(robot, body: dict[str, Any]) -> str:
    rooms = room_list(body)
    if not rooms:
        raise ActionError("Pick at least one room first.")
    m = pick_map(await robot_maps(robot), body.get("map_id"))
    command = clean_command(robot, m, rooms)
    if not await robot.send_routine_command_via_cmd_topic(command):
        raise ActionError("The room clean was sent but iRobot's cloud didn't confirm delivery.")
    return f"Cleaning {len(rooms)} room{'s' if len(rooms) != 1 else ''}."


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
    payload = response.payload if isinstance(response.payload, dict) else {}
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
            "id": f.favorite_id, "name": f.name or "Routine", "color": f.color,
            # Only single-step routines map onto the editor; others still run.
            "editable": len(steps) == 1,
            "rooms": [{"id": str(r.get("region_id") or r.get("id")), "mode": region_mode(r),
                       "kind": r.get("type")} for r in regions],
        })
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

    # Rooms already in the routine keep every setting they had (suction,
    # passes, and so on); only their mode changes if a new one was picked.
    previous = {str(r.get("region_id") or r.get("id")): r for r in region_dicts(old)}
    regions = []
    for rid, mode in room_list(body):
        if rid in previous:
            region = json.loads(json.dumps(previous[rid]))
            params = region.get("params") if isinstance(region.get("params"), dict) else {}
            if mode is not None:
                params["operatingMode"] = mode
            region["params"] = params
        else:
            region = room_regions(m, [(rid, mode)])[0].to_json()
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
    return {"data": reported(await robot.get_settings())}


async def set_setting(robot, body: dict[str, Any]) -> str:
    key = str(body.get("key", "")).strip()
    if not key or len(key) > 64:
        raise ActionError("A setting name is required.")
    await robot.set_setting(key, body.get("value"))
    current = reported(await robot.get_settings()).get(key, "(not reported)")
    return f"Set {key}. The robot now reports: {json.dumps(current)}"


async def parts(robot) -> dict[str, Any]:
    return {"data": plain(await robot.get_robot_parts())}


async def status(robot) -> dict[str, Any]:
    return {"data": reported(await robot.get_state())}


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

MAP_EDITS = ("rename", "merge", "split", "zone_add", "zone_remove")

# action -> (handler(robot, body), needs the MQTT connection)
ACTIONS = {
    **{c: ((lambda c: lambda r, b: simple_command(r, c))(c), True) for c in SIMPLE_COMMANDS + ("find",)},
    "clean_rooms": (clean_rooms, True),
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
}

# What to re-read after a change, inside the same login as the change.
REFRESH = {
    **{a: ["map"] for a in MAP_EDITS + ("map_rename", "map_rotate", "map_delete")},
    **{a: ["routines"] for a in ("create_routine", "update_routine", "delete_routine")},
    **{a: ["schedules"] for a in ("toggle_schedule", "create_schedule", "update_schedule", "delete_schedule")},
    "set_dnd": ["dnd"],
    "set_setting": ["settings"],
    "reset_part": ["parts"],
    **{a: ["status"] for a in SIMPLE_COMMANDS + ("find", "clean_rooms", "run_routine")},
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


def make_app(robot: Robot, token: str, lan: bool, preload: bool = True) -> web.Application:
    lock = asyncio.Lock()
    cache: dict[str, Any] = {"loading": False, "sections": {}, "errors": {}, "login_error": None}

    def remember(sections: dict[str, Any], errors: dict[str, str]) -> None:
        old, new = cache["sections"].get("map"), sections.get("map")
        if old and new and "views" not in new:
            # Keep the other maps; replace only the one that was re-read.
            ids = {x["id"] for x in new["maps"]}
            kept = {k: v for k, v in old.get("views", {}).items() if k in ids}
            new["views"] = {**kept, new["map"]["id"]: new["map"]}
        cache["sections"].update(sections)
        for name in sections:
            cache["errors"].pop(name, None)
        cache["errors"].update(errors)
        cache["login_error"] = None

    async def preload_all() -> None:
        async with lock:
            cache["loading"] = True
            try:
                sections, errors = await robot.run(
                    lambda r: read_sections(r, list(READERS), {"all_maps": True}, [False]), mqtt=False)
                remember(sections, errors)
                print(f"preload: loaded {', '.join(sections) or 'nothing'}"
                      + (f"; failed {', '.join(errors)}" if errors else ""))
            except ActionError as err:
                cache["login_error"] = str(err)
                print(f"preload: {err}")
            finally:
                cache["loading"] = False

    async def start_preload(app: web.Application) -> None:
        app["preload"] = asyncio.create_task(preload_all())

    def login_info() -> dict[str, Any]:
        choice = robot.choice
        return {"supported": roomba_login.supported(), "saved": choice.saved, "email": choice.login.email}

    def save_or_forget(action: str) -> tuple[bool, str]:
        # Local only: no iRobot login needed.
        choice = robot.choice
        try:
            if action == "forget_login":
                roomba_login.forget()
                choice.saved = choice.save_after_login = False
                return True, "Forgot the saved login. The app will ask for it next time it starts."
            if cache["login_error"] or not cache["sections"]:
                return False, "Save the login once it has worked: iRobot hasn't accepted it yet."
            roomba_login.save(choice.login)
            choice.saved, choice.save_after_login = True, False
            return True, "Saved on this PC. The app won't ask for the login next time."
        except OSError as err:
            return False, f"Windows refused: {err}"

    def is_local(request: web.Request) -> bool:
        return request.host.split(":")[0] in ("127.0.0.1", "localhost")

    def token_ok(supplied: str) -> bool:
        return hmac.compare_digest(supplied.encode(), token.encode())

    async def index(request: web.Request) -> web.Response:
        # On this PC the page is open; from elsewhere it needs the secret link.
        if not is_local(request) and not token_ok(request.query.get("key", "")):
            raise web.HTTPForbidden(text="Open the full link the app printed when it started.")
        return web.Response(text=PAGE.replace("__TOKEN__", token), content_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def api(request: web.Request) -> web.Response:
        if not token_ok(request.headers.get("X-Roomba-Token", "")):
            raise web.HTTPForbidden(text="Missing or wrong token. Reload the page.")
        action = request.match_info["action"]
        if action == "cached":
            # Answered from memory: no login, no waiting.
            return web.json_response({"ok": True, "message": "", **cache, "login": login_info()})
        if action in ("save_login", "forget_login"):
            ok, message = save_or_forget(action)
            return web.json_response({"ok": ok, "message": message, "login": login_info()})
        if action == "refresh_all":
            await preload_all()
            failed = cache["login_error"] or (
                "Couldn't load: " + ", ".join(cache["errors"]) if cache["errors"] else None)
            return web.json_response({"ok": not cache["login_error"], "message": failed or "Everything is up to date.",
                                      "sections": cache["sections"], "errors": cache["errors"]})
        if action not in ACTIONS and action not in READERS:
            raise web.HTTPNotFound()
        try:
            body = await request.json() if request.can_read_body else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        if action in READERS:
            async def handler(r, b):
                return "Loaded."
            mqtt, refresh = False, [action]
        else:
            handler, mqtt = ACTIONS[action]
            refresh = REFRESH.get(action, [])

        async def job(r):
            result = await handler(r, body)
            # Re-read what the change affected while still logged in, so
            # the page updates without a second login.
            sections, errors = await read_sections(r, refresh, body, [mqtt])
            return result, sections, errors

        # Waits for any running action, including the startup preload.
        async with lock:
            try:
                result, sections, errors = await robot.run(job, mqtt=mqtt)
            except ActionError as err:
                print(f"{action}: {err}")
                return web.json_response({"ok": False, "message": str(err)})
            except (KeyError, TypeError, ValueError):
                return web.json_response({"ok": False, "message": "That request was missing something."})
            remember(sections, errors)
        if action in READERS and action in errors:
            return web.json_response({"ok": False, "message": errors[action], "errors": errors})
        reply = {"ok": True, "sections": sections, "errors": errors}
        if isinstance(result, str):
            print(f"{action}: {result}")
            return web.json_response({**reply, "message": result})
        print(f"{action}: loaded")
        return web.json_response({**reply, **result, "message": "Loaded."})

    @web.middleware
    async def host_check(request: web.Request, handler):
        # Blocks DNS-rebinding tricks against the localhost-only mode.
        if not lan and not is_local(request):
            raise web.HTTPForbidden()
        return await handler(request)

    app = web.Application(middlewares=[host_check], client_max_size=64 * 1024)
    if preload:
        app.on_startup.append(start_preload)
    app.router.add_get("/", index)
    app.router.add_post("/api/{action}", api)
    return app

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f4f5f2" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#121413" media="(prefers-color-scheme: dark)">
<title>Roomba</title>
<link rel="icon" href="data:,">
<style>
:root {
  color-scheme: light dark;
  --bg: #f4f5f2; --surface: #ffffff; --surface-2: #eef0ec; --line: #e1e4de;
  --text: #17201b; --muted: #66706a; --faint: #9aa29d;
  --accent: #1f8a5b; --accent-ink: #ffffff; --accent-soft: #dcf1e6;
  --danger: #c2412d; --danger-soft: #fbe6e1; --warn: #b7791f;
  --shadow: 0 1px 2px rgba(20,30,25,.06), 0 6px 20px rgba(20,30,25,.06);
  --radius: 18px; --tabbar: 64px;
  --room-1: #d9e2f2; --room-2: #f1e3cf; --room-3: #e9d8ee; --room-4: #f2d9d9; --room-5: #d4e7f0; --room-6: #ebe6cc;
  --wall: #4b5650; --zone: #c2412d; --nomop: #2f6fc0;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #121413; --surface: #1b1e1c; --surface-2: #232825; --line: #2d332f;
    --text: #e9ece9; --muted: #9aa49e; --faint: #6c756f;
    --accent: #3fc287; --accent-ink: #08130d; --accent-soft: #173527;
    --danger: #ef7a62; --danger-soft: #3a1f19; --warn: #e2b04a;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.25);
    --room-1: #253041; --room-2: #3a3226; --room-3: #33283a; --room-4: #3c2727; --room-5: #20343f; --room-6: #36331f;
    --wall: #b9c2bc; --zone: #ef7a62; --nomop: #7fb0ec;
  }
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; background: var(--bg); color: var(--text); }
body { font: 16px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
       padding-bottom: calc(var(--tabbar) + env(safe-area-inset-bottom) + 24px); }
button, input, select { font: inherit; color: inherit; }
button { cursor: pointer; border: 0; background: none; padding: 0; }
button:disabled { cursor: default; opacity: .45; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
svg.i { width: 22px; height: 22px; fill: none; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; flex: none; }

/* layout */
.wrap { max-width: 640px; margin: 0 auto; padding: 0 16px; }
header.top { position: sticky; top: 0; z-index: 20; background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: saturate(1.4) blur(12px); -webkit-backdrop-filter: saturate(1.4) blur(12px);
  padding: calc(env(safe-area-inset-top) + 12px) 0 10px; }
header.top .wrap { display: flex; align-items: center; gap: 12px; }
.title { flex: 1; min-width: 0; }
.title h1 { margin: 0; font-size: 22px; letter-spacing: -.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.title .sub { color: var(--muted); font-size: 14px; display: flex; gap: 8px; align-items: center; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--faint); }
.dot.ok { background: var(--accent); } .dot.busy { background: var(--warn); animation: pulse 1.4s infinite; } .dot.bad { background: var(--danger); }
@keyframes pulse { 50% { opacity: .35; } }
.iconbtn { width: 42px; height: 42px; border-radius: 50%; display: grid; place-items: center; color: var(--text); background: var(--surface); box-shadow: var(--shadow); }
.iconbtn:active { transform: scale(.94); }
.view { display: none; animation: fade .22s ease; }
.view.on { display: block; }
@keyframes fade { from { opacity: 0; transform: translateY(6px); } }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 26px 4px 10px; font-weight: 650; }
.card { background: var(--surface); border-radius: var(--radius); box-shadow: var(--shadow); }
.pad { padding: 16px; }
.muted { color: var(--muted); } .small { font-size: 14px; }

/* tab bar */
nav.tabbar { position: fixed; z-index: 30; left: 0; right: 0; bottom: 0; padding-bottom: env(safe-area-inset-bottom);
  background: color-mix(in srgb, var(--surface) 92%, transparent); backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
  border-top: 1px solid var(--line); }
nav.tabbar .wrap { display: grid; grid-template-columns: repeat(5, 1fr); height: var(--tabbar); }
nav.tabbar button { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px; color: var(--faint); font-size: 11px; font-weight: 600; }
nav.tabbar button[aria-current="page"] { color: var(--accent); }
nav.tabbar button:active svg { transform: scale(.9); }

/* buttons */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 48px; padding: 0 18px;
  border-radius: 14px; background: var(--surface-2); font-weight: 650; transition: transform .08s, background .15s; position: relative; }
.btn:active:not(:disabled) { transform: scale(.97); }
.btn.primary { background: var(--accent); color: var(--accent-ink); }
.btn.danger { background: var(--danger-soft); color: var(--danger); }
.btn.ghost { background: transparent; color: var(--accent); padding: 0 10px; }
.btn.block { width: 100%; }
.btn.lg { min-height: 60px; font-size: 18px; border-radius: 18px; }
.btn.loading { color: transparent !important; pointer-events: none; }
.btn.loading::after { content: ""; position: absolute; width: 20px; height: 20px; border-radius: 50%;
  border: 2.5px solid currentColor; border-color: var(--muted) transparent var(--muted) transparent; animation: spin .8s linear infinite; }
.btn.primary.loading::after { border-color: var(--accent-ink) transparent var(--accent-ink) transparent; }
@keyframes spin { to { transform: rotate(360deg); } }
.row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
.grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.grid3 .btn { flex-direction: column; gap: 4px; min-height: 72px; font-size: 14px; }

/* home hero */
.hero { display: grid; grid-template-columns: 120px 1fr; gap: 18px; align-items: center; padding: 20px; }
.ring { position: relative; width: 120px; height: 120px; }
.ring svg { width: 120px; height: 120px; transform: rotate(-90deg); }
.ring circle { fill: none; stroke-width: 10; }
.ring .track { stroke: var(--surface-2); }
.ring .val { stroke: var(--accent); stroke-linecap: round; transition: stroke-dashoffset .6s ease; }
.ring .center { position: absolute; inset: 0; display: grid; place-items: center; text-align: center; }
.ring .pct { font-size: 26px; font-weight: 750; letter-spacing: -.02em; }
.ring .lbl { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
.phase { font-size: 22px; font-weight: 700; letter-spacing: -.01em; margin: 0 0 4px; }
.err { color: var(--danger); font-weight: 600; font-size: 14px; }

/* lists */
.list { display: grid; gap: 10px; }
.item { display: flex; align-items: center; gap: 12px; padding: 14px 16px; }
.item .grow { flex: 1; min-width: 0; }
.item .name { font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.item .meta { color: var(--muted); font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tappable { cursor: pointer; transition: transform .08s; } .tappable:active { transform: scale(.985); }
.play { width: 44px; height: 44px; border-radius: 50%; background: var(--accent); color: var(--accent-ink); display: grid; place-items: center; flex: none; position: relative; }
.play.loading svg { opacity: 0; }
.play.loading::after { content: ""; position: absolute; width: 18px; height: 18px; border-radius: 50%; border: 2.5px solid var(--accent-ink); border-color: var(--accent-ink) transparent; animation: spin .8s linear infinite; }
.scroller { display: flex; gap: 10px; overflow-x: auto; scroll-snap-type: x mandatory; padding: 2px 16px 8px; margin: 0 -16px; scrollbar-width: none; }
.scroller::-webkit-scrollbar { display: none; }
.chipcard { scroll-snap-align: start; flex: 0 0 auto; width: 168px; padding: 14px; display: flex; flex-direction: column; gap: 10px; }
.chipcard .name { font-weight: 650; }
.chipcard .meta { color: var(--muted); font-size: 13px; min-height: 2.6em; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.group { overflow: hidden; }
.group > .item + .item { border-top: 1px solid var(--line); }

/* switch */
.switch { position: relative; width: 52px; height: 32px; flex: none; }
.switch input { position: absolute; opacity: 0; inset: 0; margin: 0; cursor: pointer; z-index: 1; }
.switch span { position: absolute; inset: 0; border-radius: 999px; background: var(--line); transition: background .2s; }
.switch span::after { content: ""; position: absolute; top: 3px; left: 3px; width: 26px; height: 26px; border-radius: 50%; background: #fff;
  box-shadow: 0 1px 3px rgba(0,0,0,.25); transition: transform .2s; }
.switch input:checked + span { background: var(--accent); }
.switch input:checked + span::after { transform: translateX(20px); }
.switch input:disabled + span { opacity: .5; }

/* chips & segmented */
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip { padding: 9px 14px; border-radius: 999px; background: var(--surface); box-shadow: inset 0 0 0 1.5px var(--line); font-weight: 600; font-size: 14px;
  display: inline-flex; gap: 6px; align-items: center; transition: background .15s, box-shadow .15s; }
.chip.on { background: var(--accent-soft); box-shadow: inset 0 0 0 1.5px var(--accent); color: var(--text); }
.chip .n { width: 20px; height: 20px; border-radius: 50%; background: var(--accent); color: var(--accent-ink); font-size: 12px; display: grid; place-items: center; }
.seg { display: inline-grid; grid-auto-flow: column; grid-auto-columns: 1fr; background: var(--surface-2); padding: 3px; border-radius: 12px; }
.seg button { padding: 8px 12px; border-radius: 9px; font-weight: 600; font-size: 14px; color: var(--muted); white-space: nowrap; }
.seg button[aria-pressed="true"] { background: var(--surface); color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,.12); }
.days { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
.days button { aspect-ratio: 1; border-radius: 50%; background: var(--surface-2); font-weight: 650; font-size: 14px; }
.days button[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); }

/* map */
.mapcard { position: relative; overflow: hidden; }
#svg { display: block; width: 100%; height: auto; max-height: 58vh; touch-action: manipulation; user-select: none; }
.room { stroke: var(--surface); cursor: pointer; transition: fill .15s; }
.room.sel { fill: var(--accent) !important; fill-opacity: .72; }
.border { fill: none; stroke: var(--wall); }
.zone { fill: var(--zone); fill-opacity: .18; stroke: var(--zone); stroke-dasharray: 3 2; cursor: pointer; }
.zone.nomop { fill: var(--nomop); stroke: var(--nomop); }
.zone.sel { fill-opacity: .45; }
.wallline { stroke: var(--zone); fill: none; }
.label { fill: var(--text); font-weight: 650; text-anchor: middle; dominant-baseline: middle; pointer-events: none; paint-order: stroke; stroke: var(--surface); stroke-opacity: .7; }
.badge { fill: var(--accent); } .badgetxt { fill: var(--accent-ink); font-weight: 750; text-anchor: middle; dominant-baseline: central; pointer-events: none; }
.dock { fill: var(--text); }
.draft { fill: var(--accent); } .draftline { stroke: var(--accent); fill: var(--accent); fill-opacity: .2; }
.mapname { font-weight: 700; font-size: 18px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.maptools { position: absolute; top: 10px; right: 10px; display: flex; gap: 8px; }
.banner { display: flex; gap: 10px; align-items: center; padding: 12px 14px; border-radius: 14px; background: var(--accent-soft); font-size: 14px; margin-top: 10px; }
.banner .grow { flex: 1; }
.toolgrid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.toolgrid .btn { flex-direction: column; gap: 2px; min-height: 64px; font-size: 13px; }
.actionbar { position: fixed; z-index: 25; left: 0; right: 0; bottom: calc(var(--tabbar) + env(safe-area-inset-bottom) + 10px);
  transform: translateY(140%); visibility: hidden; pointer-events: none;
  transition: transform .25s cubic-bezier(.2,.8,.2,1), visibility 0s linear .25s; }
.actionbar.on { transform: none; visibility: visible; transition: transform .25s cubic-bezier(.2,.8,.2,1); }
.view[data-view="map"] { padding-bottom: 190px; }
.actionbar .card { pointer-events: auto; padding: 12px; display: grid; gap: 10px; box-shadow: 0 10px 40px rgba(0,0,0,.18); }

/* skeleton */
.skel { border-radius: 12px; background: linear-gradient(90deg, var(--surface-2), var(--line), var(--surface-2)); background-size: 200% 100%; animation: shimmer 1.3s infinite; }
@keyframes shimmer { to { background-position: -200% 0; } }
.empty { text-align: center; color: var(--muted); padding: 24px 16px; font-size: 15px; }

/* bars */
.bar { height: 8px; border-radius: 999px; background: var(--surface-2); overflow: hidden; margin-top: 8px; }
.bar > i { display: block; height: 100%; border-radius: inherit; background: var(--accent); }
.bar.low > i { background: var(--warn); } .bar.out > i { background: var(--danger); }

/* sheets & toast */
.scrim { position: fixed; inset: 0; z-index: 50; background: rgba(0,0,0,.38); opacity: 0; pointer-events: none; transition: opacity .2s; }
.scrim.on { opacity: 1; pointer-events: auto; }
.sheet { position: fixed; z-index: 51; left: 0; right: 0; bottom: 0; max-height: 92vh; overflow: auto; background: var(--surface);
  border-radius: 22px 22px 0 0; padding: 8px 0 calc(env(safe-area-inset-bottom) + 20px); transform: translateY(105%);
  visibility: hidden; transition: transform .28s cubic-bezier(.2,.8,.2,1), visibility 0s linear .28s; box-shadow: 0 -10px 40px rgba(0,0,0,.2); }
.sheet.on { transform: none; visibility: visible; transition: transform .28s cubic-bezier(.2,.8,.2,1); }
.sheet:not(.on) { pointer-events: none; }
@media (min-width: 700px) { .sheet { left: 50%; right: auto; width: 560px; margin-left: -280px; bottom: 50%; transform: translateY(60%) scale(.96); opacity: 0;
  border-radius: 22px; max-height: 86vh; transition: transform .22s, opacity .22s, visibility 0s linear .22s; }
  .sheet.on { transform: translateY(50%); opacity: 1; transition: transform .22s, opacity .22s; } .sheet .grab { visibility: hidden; } }
.sheet .grab { width: 40px; height: 5px; border-radius: 3px; background: var(--line); margin: 4px auto 10px; }
.sheet h3 { margin: 4px 0 6px; font-size: 20px; padding-right: 40px; }
.sheetx { position: absolute; top: 12px; right: 12px; width: 36px; height: 36px; box-shadow: none; background: var(--surface-2); }
.sheet .body { padding: 0 20px; display: grid; gap: 14px; }
.sheet .foot { display: grid; gap: 10px; padding: 18px 20px 0; }
.field { display: grid; gap: 6px; } .field > span { font-size: 13px; color: var(--muted); font-weight: 600; }
.input { width: 100%; min-height: 48px; padding: 0 14px; border-radius: 12px; border: 1.5px solid var(--line); background: var(--surface); }
.input:focus { border-color: var(--accent); outline: none; }
.ordered { display: grid; gap: 8px; }
.oitem { display: grid; grid-template-columns: 28px 1fr auto auto; gap: 8px; align-items: center; padding: 10px 12px; border-radius: 14px; background: var(--surface-2); }
.oitem .n { width: 26px; height: 26px; border-radius: 50%; background: var(--accent); color: var(--accent-ink); font-size: 13px; font-weight: 750; display: grid; place-items: center; }
.oitem .seg { grid-column: 2 / -1; justify-self: start; }
.mini { width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center; background: var(--surface); }
.toasts { position: fixed; z-index: 60; left: 0; right: 0; top: calc(env(safe-area-inset-top) + 10px); display: grid; justify-items: center; gap: 8px; pointer-events: none; padding: 0 16px; }
.toast { max-width: 560px; padding: 12px 16px; border-radius: 14px; background: var(--text); color: var(--bg); box-shadow: 0 8px 30px rgba(0,0,0,.25);
  font-size: 15px; font-weight: 550; animation: tin .25s ease; pointer-events: auto; display: flex; gap: 10px; align-items: center; }
.toast.bad { background: var(--danger); color: #fff; }
.toast.out { opacity: 0; transform: translateY(-8px); transition: .25s; }
@keyframes tin { from { opacity: 0; transform: translateY(-10px); } }
details.raw summary { cursor: pointer; padding: 14px 16px; font-weight: 650; list-style: none; display: flex; justify-content: space-between; }
details.raw summary::after { content: "›"; color: var(--faint); transform: rotate(90deg); }
details.raw[open] summary::after { transform: rotate(-90deg); }
pre { margin: 0 16px 16px; padding: 12px; border-radius: 12px; background: var(--surface-2); font-size: 12px; max-height: 50vh; overflow: auto; white-space: pre-wrap; word-break: break-word; }
.hidden { display: none !important; }
.kv { display: grid; grid-template-columns: auto 1fr; gap: 6px 16px; font-size: 15px; }
.kv dt { color: var(--muted); } .kv dd { margin: 0; text-align: right; font-weight: 600; overflow-wrap: anywhere; }
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <symbol id="i-home" viewBox="0 0 24 24"><path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/></symbol>
  <symbol id="i-map" viewBox="0 0 24 24"><path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2z"/><path d="M9 4v14M15 6v14"/></symbol>
  <symbol id="i-list" viewBox="0 0 24 24"><path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/></symbol>
  <symbol id="i-gear" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></symbol>
  <symbol id="i-more" viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></symbol>
  <symbol id="i-play" viewBox="0 0 24 24"><path d="M7 4.5v15l12-7.5z" fill="currentColor"/></symbol>
  <symbol id="i-pause" viewBox="0 0 24 24"><path d="M8 5v14M16 5v14" stroke-width="3"/></symbol>
  <symbol id="i-stop" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></symbol>
  <symbol id="i-dock" viewBox="0 0 24 24"><path d="M4 20h16M7 20V9l5-4 5 4v11"/><path d="M10 20v-5h4v5"/></symbol>
  <symbol id="i-bell" viewBox="0 0 24 24"><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0"/></symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5"/></symbol>
  <symbol id="i-edit" viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16z"/></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
  <symbol id="i-up" viewBox="0 0 24 24"><path d="M6 15l6-6 6 6"/></symbol>
  <symbol id="i-down" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></symbol>
  <symbol id="i-x" viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></symbol>
  <symbol id="i-merge" viewBox="0 0 24 24"><path d="M6 4v5a6 6 0 0 0 6 6h0a6 6 0 0 1 6 6M18 4v5a6 6 0 0 1-3 5.2"/></symbol>
  <symbol id="i-split" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16" stroke-dasharray="2 2"/></symbol>
  <symbol id="i-text" viewBox="0 0 24 24"><path d="M4 7V5h16v2M12 5v14M9 19h6"/></symbol>
  <symbol id="i-block" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 4l16 16"/></symbol>
  <symbol id="i-drop" viewBox="0 0 24 24"><path d="M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z"/><path d="M4 4l16 16"/></symbol>
  <symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></symbol>
  <symbol id="i-moon" viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/></symbol>
  <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></symbol>
  <symbol id="i-key" viewBox="0 0 24 24"><circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3M16 7l3 3M14 9l2 2"/></symbol>
</svg>

<header class="top">
  <div class="wrap">
    <div class="title">
      <h1 id="robotname">Roomba</h1>
      <div class="sub"><span class="dot busy" id="conndot"></span><span id="connlabel">Connecting...</span></div>
    </div>
    <button class="iconbtn" id="refreshall" aria-label="Refresh everything"><svg class="i"><use href="#i-refresh"/></svg></button>
  </div>
</header>

<main class="wrap">
  <!-- HOME -->
  <section class="view on" data-view="home">
    <div class="card hero">
      <div class="ring" aria-hidden="true">
        <svg viewBox="0 0 120 120"><circle class="track" cx="60" cy="60" r="50"/><circle class="val" id="batring" cx="60" cy="60" r="50" stroke-dasharray="314.16" stroke-dashoffset="314.16"/></svg>
        <div class="center"><div><div class="pct" id="batpct">--</div><div class="lbl">Battery</div></div></div>
      </div>
      <div>
        <p class="phase" id="phase">Loading...</p>
        <div class="muted small" id="phasesub"></div>
        <div class="err" id="roboterr"></div>
      </div>
    </div>
    <div style="height:12px"></div>
    <button class="btn primary lg block" id="primary" data-cmd="start"><svg class="i"><use href="#i-play"/></svg><span>Clean everywhere</span></button>
    <div style="height:10px"></div>
    <div class="grid3">
      <button class="btn" data-cmd="dock"><svg class="i"><use href="#i-dock"/></svg>Dock</button>
      <button class="btn" data-cmd="stop"><svg class="i"><use href="#i-stop"/></svg>Stop</button>
      <button class="btn" data-cmd="find"><svg class="i"><use href="#i-bell"/></svg>Find</button>
    </div>

    <h2>Routines</h2>
    <div class="scroller" id="homeroutines"><div class="card chipcard skel" style="height:118px"></div><div class="card chipcard skel" style="height:118px"></div></div>

    <h2>Maintenance</h2>
    <div class="card group" id="homeparts"><div class="pad"><div class="skel" style="height:40px"></div></div></div>
  </section>

  <!-- MAP -->
  <section class="view" data-view="map">
    <div class="row" style="justify-content:space-between;margin:14px 0 10px">
      <div class="seg hidden" id="mapsel" role="group" aria-label="Map"></div>
      <div class="mapname" id="mapname"></div>
      <div style="flex:1"></div>
      <button class="btn ghost" id="mapmenu">Map options</button>
    </div>
    <div class="card mapcard">
      <svg id="svg" xmlns="http://www.w3.org/2000/svg" aria-label="Floor map"></svg>
      <div id="mapskel" class="skel" style="height:280px;margin:12px"></div>
      <div id="mapempty" class="empty hidden"></div>
      <div class="maptools"><button class="iconbtn" id="editbtn" aria-label="Edit map"><svg class="i"><use href="#i-edit"/></svg></button></div>
    </div>

    <div id="cleanui">
      <h2>Rooms <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:500">· tap to choose, in order</span></h2>
      <div class="chips" id="roomchips"></div>
    </div>

    <div id="editui" class="hidden">
      <div class="banner"><div class="grow" id="edithint">Tap a room or zone to select it.</div><button class="btn ghost" id="editdone">Done</button></div>
      <div style="height:10px"></div>
      <div class="toolgrid">
        <button class="btn" id="t-rename"><svg class="i"><use href="#i-text"/></svg>Rename</button>
        <button class="btn" id="t-merge"><svg class="i"><use href="#i-merge"/></svg>Merge</button>
        <button class="btn" id="t-split"><svg class="i"><use href="#i-split"/></svg>Split</button>
        <button class="btn" id="t-keepout"><svg class="i"><use href="#i-block"/></svg>Keep-out</button>
        <button class="btn" id="t-nomop"><svg class="i"><use href="#i-drop"/></svg>No-mop</button>
        <button class="btn danger" id="t-rmzone"><svg class="i"><use href="#i-trash"/></svg>Remove</button>
      </div>
      <p class="muted small">Edits change the map stored in iRobot's cloud. Zone editing is the library's least-tested feature, so double-check zones in the iRobot app.</p>
    </div>
  </section>

  <!-- ROUTINES -->
  <section class="view" data-view="routines">
    <div class="row" style="justify-content:space-between;margin-top:10px"><h2 style="margin:16px 4px 10px">Routines</h2>
      <button class="btn ghost" id="newroutine"><svg class="i"><use href="#i-plus"/></svg>New</button></div>
    <div class="list" id="routines"><div class="card skel" style="height:72px"></div><div class="card skel" style="height:72px"></div></div>

    <div class="row" style="justify-content:space-between"><h2>Schedules</h2>
      <button class="btn ghost" id="newschedule"><svg class="i"><use href="#i-plus"/></svg>New</button></div>
    <div class="list" id="schedules"><div class="card skel" style="height:72px"></div></div>
    <p class="muted small">New schedules assume weeks start on Sunday. Check the first one in the iRobot app.</p>
  </section>

  <!-- SETTINGS -->
  <section class="view" data-view="settings">
    <h2>Robot</h2>
    <div class="card group" id="toggles"><div class="pad"><div class="skel" style="height:96px"></div></div></div>

    <h2>Do Not Disturb</h2>
    <div class="card group">
      <div class="item tappable" id="dndrow"><svg class="i"><use href="#i-moon"/></svg>
        <div class="grow"><div class="name">Quiet hours</div><div class="meta" id="dndnow">Loading...</div></div><span class="muted">›</span></div>
    </div>

    <h2>Advanced</h2>
    <div class="card">
      <details class="raw"><summary>All robot settings</summary><pre id="settingsraw">Not loaded yet.</pre>
        <div class="pad" style="padding-top:0;display:grid;gap:10px">
          <input class="input" id="setkey" placeholder="Setting name, e.g. childLock" autocomplete="off">
          <input class="input" id="setval" placeholder='Value as JSON, e.g. true, 3, or "text"' autocomplete="off">
          <button class="btn" id="setgo">Write setting</button>
          <p class="muted small" style="margin:0">Writes straight to the robot. Unknown names or values may be ignored.</p>
        </div>
      </details>
    </div>

    <div id="loginsec" class="hidden">
      <h2>This PC</h2>
      <div class="card group"><div class="item"><svg class="i"><use href="#i-key"/></svg>
        <div class="grow"><div class="name" id="loginname">Saved login</div><div class="meta" id="loginmeta"></div></div>
        <button class="btn" id="loginbtn" style="min-height:40px"></button></div></div>
    </div>

    <h2>Danger zone</h2>
    <div class="card pad" style="display:grid;gap:10px">
      <div><b>Factory reset</b><div class="muted small">The robot will likely go offline and need setting up again in the iRobot app. The library has never tested this.</div></div>
      <button class="btn danger" id="reset">Factory reset...</button>
    </div>
  </section>

  <!-- MORE -->
  <section class="view" data-view="more">
    <h2>Parts</h2>
    <div class="card group" id="parts"><div class="pad"><div class="skel" style="height:96px"></div></div></div>

    <h2>Cleaning history</h2>
    <div class="card group" id="history"><div class="item tappable" id="loadhistory"><svg class="i"><use href="#i-clock"/></svg><div class="grow"><div class="name">Show recent cleans</div></div><span class="muted">›</span></div></div>

    <h2>Robot</h2>
    <div class="card pad" id="robotinfo"><dl class="kv" id="robotkv"><dt>Name</dt><dd>-</dd></dl></div>

    <h2>Details</h2>
    <div class="card group" id="rawlist"></div>
  </section>
</main>

<div class="actionbar" id="actionbar"><div class="wrap"><div class="card">
  <div class="row" style="justify-content:space-between">
    <div class="small"><b id="abcount">0 rooms</b> <span class="muted" id="abnames"></span></div>
    <button class="btn ghost" id="abclear" style="min-height:32px">Clear</button>
  </div>
  <div class="seg" id="abmode" role="group" aria-label="Cleaning mode"></div>
  <div class="row" style="flex-wrap:nowrap">
    <button class="btn" id="absave" style="flex:none;white-space:nowrap">Save routine</button>
    <button class="btn primary" id="abclean" style="flex:1"><svg class="i"><use href="#i-play"/></svg>Clean</button>
  </div>
</div></div></div>

<nav class="tabbar" aria-label="Sections"><div class="wrap">
  <button data-tab="home" aria-current="page"><svg class="i"><use href="#i-home"/></svg>Home</button>
  <button data-tab="map"><svg class="i"><use href="#i-map"/></svg>Map</button>
  <button data-tab="routines"><svg class="i"><use href="#i-list"/></svg>Routines</button>
  <button data-tab="settings"><svg class="i"><use href="#i-gear"/></svg>Settings</button>
  <button data-tab="more"><svg class="i"><use href="#i-more"/></svg>More</button>
</div></nav>

<div class="scrim" id="scrim"></div>
<div class="sheet" id="sheet" role="dialog" aria-modal="true"><div class="grab"></div><button class="iconbtn sheetx" id="sheetx" aria-label="Close"><svg class="i"><use href="#i-x"/></svg></button><div class="body" id="sheetbody"></div><div class="foot" id="sheetfoot"></div></div>
<div class="toasts" id="toasts" aria-live="polite"></div>

<script>
const TOKEN = "__TOKEN__";
const $ = id => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const DAYS = ["S", "M", "T", "W", "T", "F", "S"], DAYNAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MODES = [[2, "Vacuum"], [32, "Vacuum & mop"]];
const state = { map: null, maps: [], routines: [], schedules: [], settings: null, status: null, parts: null, dnd: null };
let sel = [], selZone = null, mode = 2, editing = false, drawing = null, draft = [];

// ---------- helpers
function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") e.className = v; else if (k === "on") for (const ev in v) e.addEventListener(ev, v[ev]);
    else if (k in e && k !== "style") e[k] = v; else e.setAttribute(k, v);
  }
  kids.flat().forEach(k => k != null && e.append(k));
  return e;
}
function icon(name) { const s = document.createElementNS(NS, "svg"); s.setAttribute("class", "i"); const u = document.createElementNS(NS, "use"); u.setAttribute("href", "#i-" + name); s.append(u); return s; }
function el(tag, attrs) { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; }
function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }
function roomName(id) { const r = state.map && state.map.rooms.find(r => r.id === id); return r ? r.name : "Room " + id; }
function modeName(m) { const f = MODES.find(x => x[0] === m); return f ? f[1] : (m == null ? "Default" : "Mode " + m); }

function toast(text, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, text);
  $("toasts").append(t);
  const box = $("toasts");
  while (box.children.length > 2) box.firstElementChild.remove();   // keep the two newest
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, bad ? 6000 : 3200);
}

async function call(action, body, opts) {
  opts = opts || {};
  const btn = opts.button;
  if (btn) btn.classList.add("loading");
  try {
    const r = await fetch("api/" + action, { method: "POST",
      headers: { "X-Roomba-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    if (!r.ok) { toast("The app refused that (" + r.status + "). Try reloading the page.", true); return null; }
    const data = await r.json();
    apply(data.sections || {});
    if (!data.ok) toast(data.message, true);
    else if (!opts.silent && data.message && data.message !== "Loaded.") toast(data.message);
    return data.ok ? data : null;
  } catch (e) {
    toast("Can't reach the app. Is it still running on your PC?", true); setConn("bad", "Offline"); return null;
  } finally { if (btn) btn.classList.remove("loading"); }
}

// ---------- sheets
let sheetResolve = null;
function openSheet(build) {
  const body = $("sheetbody"), foot = $("sheetfoot");
  body.replaceChildren(); foot.replaceChildren();
  if (sheetResolve) { const r = sheetResolve; sheetResolve = null; r(null); }   // a sheet opened over another cancels it
  return new Promise(res => {
    sheetResolve = res;
    build(body, foot, v => closeSheet(v));
    $("scrim").classList.add("on"); $("sheet").classList.add("on");
    setTimeout(() => { const f = body.querySelector("input"); if (f) f.focus(); }, 280);
  });
}
function closeSheet(v) {
  $("scrim").classList.remove("on"); $("sheet").classList.remove("on");
  const r = sheetResolve; sheetResolve = null; if (r) r(v);
}
$("scrim").addEventListener("click", () => closeSheet(null));
$("sheetx").addEventListener("click", () => closeSheet(null));
document.addEventListener("keydown", e => { if (e.key === "Escape" && sheetResolve) closeSheet(null); });

function ask(o) {   // {title, text, ok, danger, typed}
  return openSheet((body, foot, done) => {
    body.append(h("h3", {}, o.title));
    if (o.text) body.append(h("div", { class: "muted" }, o.text));
    let input = null;
    if (o.typed) {
      input = h("input", { class: "input", placeholder: "Type " + o.typed + " to confirm", autocomplete: "off", autocapitalize: "characters" });
      body.append(h("div", { class: "muted small" }, "This can't be undone."), input);
    }
    const okb = h("button", { class: "btn block " + (o.danger ? "danger" : "primary"), disabled: !!o.typed, on: { click: () => done(o.typed || true) } }, o.ok || "OK");
    if (input) input.addEventListener("input", () => okb.disabled = input.value.trim() !== o.typed);
    foot.append(okb, h("button", { class: "btn block", on: { click: () => done(null) } }, "Cancel"));
  });
}
function askText(o) {  // {title, label, value, ok}
  return openSheet((body, foot, done) => {
    body.append(h("h3", {}, o.title));
    const input = h("input", { class: "input", value: o.value || "", maxLength: 40, autocomplete: "off",
      on: { keydown: e => { if (e.key === "Enter" && input.value.trim()) done(input.value.trim()); } } });
    body.append(h("label", { class: "field" }, h("span", {}, o.label || "Name"), input));
    foot.append(h("button", { class: "btn primary block", on: { click: () => input.value.trim() && done(input.value.trim()) } }, o.ok || "Save"),
      h("button", { class: "btn block", on: { click: () => done(null) } }, "Cancel"));
  });
}
function seg(options, value, onchange) {
  const s = h("div", { class: "seg", role: "group" });
  const draw = v => s.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.v === String(v))));
  options.forEach(([v, label]) => s.append(h("button", { type: "button", "data-v": v, on: { click: () => { draw(v); onchange(v); } } }, label)));
  draw(value);
  return s;
}

// ---------- tabs
function showTab(name) {
  document.querySelectorAll("nav.tabbar button").forEach(b => {
    if (b.dataset.tab === name) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("on", v.dataset.view === name));
  window.scrollTo({ top: 0 });
  updateActionBar();
  try { localStorage.setItem("roomba-tab", name); } catch (e) {}
}
document.querySelectorAll("nav.tabbar button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ---------- connection + status
function setConn(kind, text) { $("conndot").className = "dot " + kind; $("connlabel").textContent = text; }
const PHASES = { run: "Cleaning", charge: "Docked", stop: "Paused", pause: "Paused", hmUsrDock: "Returning to dock",
  hmPostMsn: "Returning to dock", hmMidMsn: "Returning to recharge", evac: "Emptying bin", stuck: "Stuck", chargingerror: "Charging error" };
const RENDER = {};
function apply(sections) {
  for (const k in sections) {
    if (k !== "map") state[k] = sections[k];   // the map renderer keeps its own state
    if (RENDER[k]) RENDER[k](sections[k]);
  }
}

RENDER.status = sec => {
  const d = sec.data || {}, m = d.cleanMissionStatus || {};
  if (d.name) $("robotname").textContent = d.name;
  const pct = typeof d.batPct === "number" ? d.batPct : null;
  $("batpct").textContent = pct == null ? "--" : pct + "%";
  $("batring").setAttribute("stroke-dashoffset", String(314.16 * (1 - (pct || 0) / 100)));
  const phase = m.phase;
  const label = PHASES[phase] || (phase ? phase : "Ready");
  const charging = phase === "charge" && pct != null && pct < 100;
  $("phase").textContent = phase === "charge" ? (charging ? "Charging" : "Docked, ready") : label;
  const bits = [];
  if (m.cycle && m.cycle !== "none") bits.push("Job: " + m.cycle);
  if (d.bin && d.bin.full) bits.push("Bin is full");
  if (m.sqft) bits.push(m.sqft + " sq ft so far");
  if (!bits.length && charging) bits.push("On the dock");
  $("phasesub").textContent = bits.join(" · ");
  $("roboterr").textContent = m.error ? "Error " + m.error + (m.notReady ? " (not ready " + m.notReady + ")" : "") : "";
  // The big button follows what the robot is doing.
  const p = $("primary"), lab = p.querySelector("span"), use = p.querySelector("use");
  if (phase === "run") { p.dataset.cmd = "pause"; lab.textContent = "Pause"; use.setAttribute("href", "#i-pause"); }
  else if ((phase === "stop" || phase === "pause") && m.cycle && m.cycle !== "none") { p.dataset.cmd = "resume"; lab.textContent = "Resume"; use.setAttribute("href", "#i-play"); }
  else { p.dataset.cmd = "start"; lab.textContent = "Clean everywhere"; use.setAttribute("href", "#i-play"); }
  setConn("ok", "Updated " + new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
};
document.querySelectorAll("[data-cmd]").forEach(b => b.addEventListener("click", async () => {
  call(b.dataset.cmd, {}, { button: b });
}));
$("refreshall").addEventListener("click", async () => {
  const b = $("refreshall"); b.firstElementChild.style.animation = "spin .8s linear infinite";
  setConn("busy", "Refreshing...");
  const d = await call("refresh_all", {}, { silent: true });
  b.firstElementChild.style.animation = "";
  if (d) toast("Everything is up to date.");
});

// ---------- map
const ROOMFILLS = ["--room-1", "--room-2", "--room-3", "--room-4", "--room-5", "--room-6"];
// Every map the server has sent, by id, so switching maps needs no reload.
// A map is only re-read after it changes (or from Map options > Refresh).
let mapViews = {};
function switchMap(id) {
  if (mapViews[id]) RENDER.map({ maps: state.maps, map: mapViews[id] });
  else call("map", { map_id: id });
}
RENDER.map = sec => {
  const ids = new Set(sec.maps.map(m => m.id));
  if (sec.views) Object.assign(mapViews, sec.views);
  mapViews[sec.map.id] = sec.map;
  for (const k in mapViews) if (!ids.has(k)) delete mapViews[k];
  const same = state.map && state.map.id === sec.map.id;
  state.map = sec.map; state.maps = sec.maps;
  if (same) { const ids = sec.map.rooms.map(r => r.id); sel = sel.filter(i => ids.includes(i)); if (!sec.map.zones.some(z => z.id === selZone)) selZone = null; }
  else { sel = []; selZone = null; }
  drawing = null; draft = [];
  const ms = $("mapsel"); ms.replaceChildren();
  if (sec.maps.length > 1) {
    ms.replaceWith(Object.assign(seg(sec.maps.map(m => [m.id, m.name]), sec.map.id, switchMap), { id: "mapsel" }));
  } else ms.classList.add("hidden");
  $("mapname").textContent = sec.maps.length > 1 ? "" : sec.map.name;
  $("mapskel").classList.add("hidden"); $("mapempty").classList.add("hidden");
  drawMap(); renderChips(); updateActionBar(); renderEditHint();
};
function showNoMap(text) {
  state.map = null; state.maps = []; mapViews = {}; sel = []; selZone = null;
  $("mapname").textContent = "";
  $("mapskel").classList.add("hidden");
  $("mapempty").textContent = text || "No saved map yet. Run a cleaning job so the robot can map your home.";
  $("mapempty").classList.remove("hidden");
  drawMap(); renderChips(); updateActionBar();
}

function bounds() {
  const pts = [];
  state.map.rooms.forEach(r => pts.push(...r.ring)); state.map.borders.forEach(b => pts.push(...b)); state.map.zones.forEach(z => pts.push(...z.points));
  if (!pts.length) return [0, 0, 1, 1];
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}
function centroid(ring) {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0; i < ring.length - 1; i++) { const [x0, y0] = ring[i], [x1, y1] = ring[i + 1]; const f = x0 * y1 - x1 * y0; a += f; cx += (x0 + x1) * f; cy += (y0 + y1) * f; }
  if (Math.abs(a) < 1e-9) return [ring.reduce((s, p) => s + p[0], 0) / ring.length, ring.reduce((s, p) => s + p[1], 0) / ring.length];
  return [cx / (3 * a), cy / (3 * a)];
}
let scale = 1;
function drawMap() {
  const svg = $("svg"); svg.replaceChildren();
  if (!state.map) { $("editbtn").classList.add("hidden"); return; }
  $("editbtn").classList.remove("hidden");
  const [x0, y0, x1, y1] = bounds(), span = Math.max(x1 - x0, y1 - y0) || 1, pad = span * 0.05;
  svg.setAttribute("viewBox", [x0 - pad, -(y1 + pad), x1 - x0 + 2 * pad, y1 - y0 + 2 * pad].join(" "));
  scale = span / 520;
  const g = el("g", { id: "world", transform: "scale(1,-1)" }); svg.append(g);   // map y grows up, SVG y grows down
  const pts = ring => ring.map(p => p[0] + "," + p[1]).join(" ");
  state.map.rooms.forEach((r, i) => {
    const p = el("polygon", { points: pts(r.ring), class: "room" + (sel.includes(r.id) ? " sel" : ""), "stroke-width": 3 * scale,
      style: "fill: var(" + ROOMFILLS[i % ROOMFILLS.length] + ")" });
    p.addEventListener("click", e => { if (drawing) return; e.stopPropagation(); toggleRoom(r.id); });
    g.append(p);
  });
  state.map.borders.forEach(b => g.append(el("polyline", { points: pts(b), class: "border", "stroke-width": 2.5 * scale })));
  state.map.zones.forEach(z => {
    if (z.kind === "wall") { g.append(el("polyline", { points: pts(z.points), class: "wallline", "stroke-width": 4 * scale })); return; }
    const p = el("polygon", { points: pts(z.points), "stroke-width": 1.5 * scale, class: "zone" + (z.kind === "nomop" ? " nomop" : "") + (selZone === z.id ? " sel" : "") });
    p.addEventListener("click", e => { if (drawing || !editing) return; e.stopPropagation(); selZone = selZone === z.id ? null : z.id; drawMap(); renderEditHint(); });
    g.append(p);
  });
  if (state.map.dock) {
    const [dx, dy] = state.map.dock;
    g.append(el("rect", { x: dx - 7 * scale, y: dy - 7 * scale, width: 14 * scale, height: 14 * scale, rx: 3 * scale, class: "dock" }));
  }
  state.map.rooms.forEach(r => {
    if (!r.ring.length) return;
    const [cx, cy] = centroid(r.ring), n = sel.indexOf(r.id);
    const t = el("text", { x: cx, y: -cy, class: "label", "font-size": 13 * scale, "stroke-width": 3 * scale, transform: "scale(1,-1)" });
    t.textContent = r.name; g.append(t);
    if (n >= 0 && !editing) {
      g.append(el("circle", { cx, cy: cy + 24 * scale, r: 10 * scale, class: "badge" }));
      const b = el("text", { x: cx, y: -(cy + 24 * scale), class: "badgetxt", "font-size": 11 * scale, transform: "scale(1,-1)" });
      b.textContent = n + 1; g.append(b);
    }
  });
  draft.forEach(p => g.append(el("circle", { cx: p[0], cy: p[1], r: 6 * scale, class: "draft" })));
}
function toggleRoom(id) {
  sel = sel.includes(id) ? sel.filter(x => x !== id) : [...sel, id];
  drawMap(); renderChips(); updateActionBar(); renderEditHint();
}
function renderChips() {
  const box = $("roomchips"); box.replaceChildren();
  if (!state.map) return;
  state.map.rooms.forEach(r => {
    const n = sel.indexOf(r.id);
    box.append(h("button", { class: "chip" + (n >= 0 ? " on" : ""), on: { click: () => toggleRoom(r.id) } }, n >= 0 ? h("span", { class: "n" }, String(n + 1)) : null, r.name));
  });
}
function updateActionBar() {
  const on = !editing && sel.length > 0 && document.querySelector('.view.on').dataset.view === "map";
  $("actionbar").classList.toggle("on", on);
  if (!on) return;
  $("abcount").textContent = plural(sel.length, "room");
  $("abnames").textContent = "· " + sel.map(roomName).join(" → ");
  const s = seg(MODES, mode, v => mode = Number(v)); s.id = "abmode"; $("abmode").replaceWith(s);
}
$("abclear").addEventListener("click", () => { sel = []; drawMap(); renderChips(); updateActionBar(); });
$("abclean").addEventListener("click", e => call("clean_rooms", { map_id: state.map.id, rooms: sel.map(id => ({ id, mode })) }, { button: e.currentTarget }));
$("absave").addEventListener("click", () => editRoutine(null, sel.map(id => ({ id, mode }))));

// edit mode
function setEditing(on) {
  editing = on; drawing = null; draft = [];
  if (!on) selZone = null;
  $("editui").classList.toggle("hidden", !on); $("cleanui").classList.toggle("hidden", on);
  $("editbtn").style.background = on ? "var(--accent)" : ""; $("editbtn").style.color = on ? "var(--accent-ink)" : "";
  drawMap(); renderChips(); updateActionBar(); renderEditHint();
}
$("editbtn").addEventListener("click", () => setEditing(!editing));
$("editdone").addEventListener("click", () => setEditing(false));
function renderEditHint() {
  if (!editing) return;
  const n = sel.length;
  $("edithint").textContent = drawing === "split" ? "Tap two points on the map to draw the dividing line (" + draft.length + "/2)."
    : drawing ? "Tap two opposite corners of the zone (" + draft.length + "/2)."
    : selZone ? "Zone selected." : n ? "Selected: " + sel.map(roomName).join(", ") : "Tap rooms or zones on the map to select them.";
  $("t-rename").disabled = n !== 1 || !!drawing; $("t-merge").disabled = n < 2 || !!drawing; $("t-split").disabled = n !== 1 || !!drawing;
  $("t-keepout").disabled = !!drawing; $("t-nomop").disabled = !!drawing; $("t-rmzone").disabled = !selZone || !!drawing;
}
async function mapEdit(action, body, btn) { return call(action, Object.assign({ map_id: state.map.id }, body), { button: btn }); }
$("t-rename").addEventListener("click", async e => {
  const r = state.map.rooms.find(r => r.id === sel[0]), btn = e.currentTarget;
  const name = await askText({ title: "Rename room", label: "Room name", value: r.name });
  if (name && name !== r.name) mapEdit("rename", { room_id: r.id, name }, btn);
});
$("t-merge").addEventListener("click", async e => {
  const btn = e.currentTarget;
  if (await ask({ title: "Merge rooms?", text: sel.map(roomName).join(" + ") + " will become one room. They need to be next to each other.", ok: "Merge" }))
    if (await mapEdit("merge", { room_ids: sel }, btn)) { sel = []; drawMap(); renderChips(); renderEditHint(); }
});
function startDraw(kind) { drawing = kind; draft = []; selZone = null; drawMap(); renderEditHint(); }
$("t-split").addEventListener("click", () => startDraw("split"));
$("t-keepout").addEventListener("click", () => startDraw("keepout"));
$("t-nomop").addEventListener("click", () => startDraw("nomop"));
$("t-rmzone").addEventListener("click", async e => {
  const btn = e.currentTarget;
  if (await ask({ title: "Remove this zone?", text: "Other zones stay as they are.", ok: "Remove", danger: true })) mapEdit("zone_remove", { zone_id: selZone }, btn);
});
$("svg").addEventListener("click", async e => {
  if (!drawing) return;
  const pt = $("svg").createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
  const p = pt.matrixTransform($("world").getScreenCTM().inverse());
  draft.push([+p.x.toFixed(3), +p.y.toFixed(3)]); drawMap(); renderEditHint();
  if (draft.length < 2) return;
  const kind = drawing, pts = draft;
  if (kind === "split") {
    const g = $("world"); g.append(el("polyline", { points: pts.map(p => p.join(",")).join(" "), class: "draftline", "stroke-width": 3 * scale }));
    const r = state.map.rooms.find(r => r.id === sel[0]);
    const ok = await ask({ title: "Split " + r.name + "?", text: "The room will be divided along the line you drew.", ok: "Split" });
    drawing = null; draft = [];
    if (ok) await mapEdit("split", { room_id: r.id, points: pts }); else { drawMap(); renderEditHint(); }
  } else {
    const [[ax, ay], [bx, by]] = pts, corners = [[ax, ay], [bx, ay], [bx, by], [ax, by]];
    const g = $("world"); g.append(el("polygon", { points: corners.map(p => p.join(",")).join(" "), class: "draftline", "stroke-width": 2 * scale }));
    const ok = await ask({ title: "Add this " + (kind === "nomop" ? "no-mop" : "keep-out") + " zone?", text: "Your existing zones are kept.", ok: "Add zone" });
    drawing = null; draft = [];
    if (ok) await mapEdit("zone_add", { kind, points: corners }); else { drawMap(); renderEditHint(); }
  }
});
$("mapmenu").addEventListener("click", () => {
  if (!state.map) { toast("The map hasn't loaded yet.", true); return; }
  openSheet((body, foot, done) => {
    body.append(h("h3", {}, state.map.name));
    const opt = (label, ic, fn, danger) => h("button", { class: "btn block" + (danger ? " danger" : ""), style: "justify-content:flex-start", on: { click: () => { done(null); setTimeout(fn, 300); } } }, icon(ic), label);
    body.append(opt("Rename map", "text", async () => { const n = await askText({ title: "Rename map", value: state.map.name }); if (n && n !== state.map.name) mapEdit("map_rename", { name: n }); }),
      opt("Rotate map", "refresh", async () => {
        const v = await openSheet((b2, f2, d2) => { let deg = 90; b2.append(h("h3", {}, "Rotate map"), seg([[0, "0°"], [90, "90°"], [180, "180°"], [270, "270°"]], deg, v => deg = Number(v)));
          f2.append(h("button", { class: "btn primary block", on: { click: () => d2(deg) } }, "Rotate"), h("button", { class: "btn block", on: { click: () => d2(null) } }, "Cancel")); });
        if (v != null) mapEdit("map_rotate", { degrees: v });
      }),
      opt("Refresh map", "refresh", () => call("map", { map_id: state.map.id })),
      opt("Delete map...", "trash", async () => {
        const w = await ask({ title: "Delete " + state.map.name + "?", text: "The robot will have to learn your home again.", ok: "Delete map", danger: true, typed: "DELETE" });
        const d = w && await mapEdit("map_delete", { confirm: w });
        if (d && !(d.sections && d.sections.map)) showNoMap();
      }, true));
    foot.append(h("button", { class: "btn block", on: { click: () => done(null) } }, "Close"));
  });
});

// ---------- routines
function routineSummary(r) {
  if (!r.rooms.length) return "Whole house";
  const modes = [...new Set(r.rooms.map(x => x.mode))];
  return r.rooms.map(x => roomName(x.id)).join(" → ") + (modes.length === 1 && modes[0] != null ? " · " + modeName(modes[0]) : "");
}
RENDER.routines = sec => {
  const list = $("routines"), home = $("homeroutines");
  list.replaceChildren(); home.replaceChildren();
  if (!sec.routines.length) {
    list.append(h("div", { class: "card empty" }, "No routines yet. Tap New, or pick rooms on the Map tab and save them as a routine."));
    home.append(h("div", { class: "card chipcard tappable", on: { click: () => editRoutine(null, []) } }, h("div", { class: "name" }, "New routine"), h("div", { class: "meta" }, "Save rooms you clean often"), icon("plus")));
    return;
  }
  sec.routines.forEach(r => {
    const play = () => h("button", { class: "play", "aria-label": "Run " + r.name, on: { click: e => { e.stopPropagation(); runRoutine(r, e.currentTarget); } } }, icon("play"));
    list.append(h("div", { class: "card item tappable", on: { click: () => editRoutine(r) } },
      h("div", { class: "grow" }, h("div", { class: "name" }, r.name), h("div", { class: "meta" }, routineSummary(r))), play()));
    home.append(h("div", { class: "card chipcard tappable", on: { click: () => { showTab("routines"); editRoutine(r); } } },
      h("div", { class: "name" }, r.name), h("div", { class: "meta" }, routineSummary(r)), play()));
  });
};
async function runRoutine(r, btn) { call("run_routine", { favorite_id: r.id }, { button: btn }); }
$("newroutine").addEventListener("click", () => editRoutine(null, []));

function roomPicker(rooms, onchange) {
  // rooms: [{id, mode}] in order. Renders chips to add/remove and an ordered list with modes.
  const wrap = h("div", { style: "display:grid;gap:12px" });
  const draw = () => {
    wrap.replaceChildren();
    if (!state.map) { wrap.append(h("div", { class: "muted small" }, "The map hasn't loaded, so rooms can't be picked yet.")); return; }
    wrap.append(h("div", { class: "chips" }, state.map.rooms.map(r => {
      const n = rooms.findIndex(x => x.id === r.id);
      return h("button", { type: "button", class: "chip" + (n >= 0 ? " on" : ""), on: { click: () => {
        if (n >= 0) rooms.splice(n, 1); else rooms.push({ id: r.id, mode: rooms.length ? rooms[rooms.length - 1].mode : 2 });
        draw(); onchange && onchange(); } } }, n >= 0 ? h("span", { class: "n" }, String(n + 1)) : null, r.name);
    })));
    if (!rooms.length) { wrap.append(h("div", { class: "muted small" }, "No rooms picked: the whole house gets cleaned.")); return; }
    wrap.append(h("div", { class: "ordered" }, rooms.map((x, i) => h("div", { class: "oitem" },
      h("span", { class: "n" }, String(i + 1)), h("b", {}, roomName(x.id)),
      h("button", { type: "button", class: "mini", "aria-label": "Move up", disabled: i === 0, on: { click: () => { rooms.splice(i - 1, 0, rooms.splice(i, 1)[0]); draw(); } } }, icon("up")),
      h("button", { type: "button", class: "mini", "aria-label": "Move down", disabled: i === rooms.length - 1, on: { click: () => { rooms.splice(i + 1, 0, rooms.splice(i, 1)[0]); draw(); } } }, icon("down")),
      seg(MODES.concat(x.mode != null && !MODES.some(m => m[0] === x.mode) ? [[x.mode, "Mode " + x.mode]] : []), x.mode ?? 2, v => x.mode = Number(v))))));
  };
  draw();
  return wrap;
}

function editRoutine(r, preset) {
  const isNew = !r;
  if (r && !r.editable) { toast("This routine has several steps, so it can only be run here. Edit it in the iRobot app.", true); return; }
  const rooms = (r ? r.rooms : preset || []).map(x => ({ id: x.id, mode: x.mode }));
  openSheet((body, foot, done) => {
    body.append(h("h3", {}, isNew ? "New routine" : "Edit routine"));
    const name = h("input", { class: "input", value: r ? r.name : "", maxLength: 40, placeholder: "e.g. After dinner", autocomplete: "off" });
    body.append(h("label", { class: "field" }, h("span", {}, "Name"), name));
    body.append(h("div", { class: "field" }, h("span", {}, "Rooms, in cleaning order"), roomPicker(rooms)));
    const save = h("button", { class: "btn primary block", on: { click: async () => {
      if (!name.value.trim()) { name.focus(); toast("Give the routine a name.", true); return; }
      const payload = { name: name.value.trim(), rooms, map_id: state.map && state.map.id };
      const ok = isNew ? await call("create_routine", payload, { button: save }) : await call("update_routine", Object.assign({ favorite_id: r.id }, payload), { button: save });
      if (ok) done(true);
    } } }, isNew ? "Save routine" : "Save changes");
    foot.append(save);
    if (!isNew) {
      foot.append(h("button", { class: "btn block", on: { click: e => { done(null); runRoutine(r); } } }, icon("play"), "Run now"));
      foot.append(h("button", { class: "btn danger block", on: { click: async () => {
        done(null);
        const w = await ask({ title: "Delete " + r.name + "?", ok: "Delete routine", danger: true, typed: "DELETE" });
        if (w) call("delete_routine", { favorite_id: r.id, confirm: w });
      } } }, "Delete routine"));
    }
  });
}

// ---------- schedules
function timeText(hh, mm) { const d = new Date(); d.setHours(hh || 0, mm || 0, 0, 0); return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
function daysText(days) {
  const d = [...days].sort();
  if (d.length === 7) return "Every day";
  if (d.join() === "1,2,3,4,5") return "Weekdays";
  if (d.join() === "0,6") return "Weekends";
  return d.map(i => DAYNAMES[i] || i).join(", ");
}
RENDER.schedules = sec => {
  const box = $("schedules"); box.replaceChildren();
  if (!sec.schedules.length) { box.append(h("div", { class: "card empty" }, "No schedules yet.")); return; }
  sec.schedules.forEach(s => {
    const input = h("input", { type: "checkbox", checked: s.enabled, "aria-label": "Schedule on", on: {
      click: e => e.stopPropagation(),
      change: async e => { const t = e.target; t.disabled = true; const ok = await call("toggle_schedule", { schedule_id: s.id, enabled: t.checked }); if (!ok) t.checked = !t.checked; t.disabled = false; } } });
    box.append(h("div", { class: "card item tappable", on: { click: () => editSchedule(s) } },
      h("div", { class: "grow" }, h("div", { class: "name" }, (s.hour != null ? timeText(s.hour, s.minute) + " · " : "") + s.name),
        h("div", { class: "meta" }, daysText(s.days || []) + " · " + (s.rooms ? plural(s.rooms, "room") : "Whole house"))),
      h("label", { class: "switch", on: { click: e => e.stopPropagation() } }, input, h("span"))));
  });
};
function editSchedule(s) {
  const isNew = !s, days = new Set(s ? s.days : [1, 2, 3, 4, 5]), rooms = [];
  openSheet((body, foot, done) => {
    body.append(h("h3", {}, isNew ? "New schedule" : "Edit schedule"));
    const name = h("input", { class: "input", value: s ? s.name : "", maxLength: 40, placeholder: "e.g. Weekday morning", autocomplete: "off" });
    const time = h("input", { class: "input", type: "time", value: s && s.hour != null ? String(s.hour).padStart(2, "0") + ":" + String(s.minute || 0).padStart(2, "0") : "09:00" });
    const dayrow = h("div", { class: "days" }, DAYS.map((d, i) => {
      const b = h("button", { type: "button", "aria-label": DAYNAMES[i], "aria-pressed": String(days.has(i)) }, d);
      b.addEventListener("click", () => { days.has(i) ? days.delete(i) : days.add(i); b.setAttribute("aria-pressed", String(days.has(i))); });
      return b;
    }));
    body.append(h("label", { class: "field" }, h("span", {}, "Name"), name), h("label", { class: "field" }, h("span", {}, "Time"), time), h("div", { class: "field" }, h("span", {}, "Days"), dayrow));
    if (isNew) body.append(h("div", { class: "field" }, h("span", {}, "Rooms, in cleaning order"), roomPicker(rooms)));
    else body.append(h("div", { class: "muted small" }, "Cleans " + (s.rooms ? plural(s.rooms, "room") : "the whole house") + ". To change which rooms, create a new schedule."));
    const save = h("button", { class: "btn primary block", on: { click: async () => {
      const [hh, mm] = time.value.split(":").map(Number);
      if (!name.value.trim() || !days.size || isNaN(hh)) { toast("Add a name, at least one day, and a time.", true); return; }
      const payload = { name: name.value.trim(), days: [...days], hour: hh, minute: mm };
      const ok = isNew ? await call("create_schedule", Object.assign(payload, { rooms, map_id: state.map && state.map.id }), { button: save })
                       : await call("update_schedule", Object.assign(payload, { schedule_id: s.id }), { button: save });
      if (ok) done(true);
    } } }, isNew ? "Create schedule" : "Save changes");
    foot.append(save);
    if (!isNew) foot.append(h("button", { class: "btn danger block", on: { click: async () => {
      done(null);
      const w = await ask({ title: "Delete " + s.name + "?", ok: "Delete schedule", danger: true, typed: "DELETE" });
      if (w) call("delete_schedule", { schedule_id: s.id, confirm: w });
    } } }, "Delete schedule"));
  });
}
$("newschedule").addEventListener("click", () => editSchedule(null));

// ---------- settings
const TOGGLES = { childLock: ["Child lock", "Disable the buttons on the robot"], ecoCharge: ["Eco charging", "Charge more slowly to save energy"],
  carpetBoost: ["Carpet boost", "More suction on carpets"], vacHigh: ["High suction", "Always use maximum suction"],
  twoPass: ["Two passes", "Clean every area twice"], noAutoPasses: ["Fixed passes", "Don't add passes automatically"],
  evacAllowed: ["Auto-empty", "Empty the bin at the dock"], mapUploadAllowed: ["Map uploads", "Let the robot upload maps"],
  padWashAllowed: ["Pad washing", "Wash the mop pad at the dock"], padDryAllowed: ["Pad drying", "Dry the mop pad at the dock"] };
RENDER.settings = sec => {
  const s = sec.data || {}, box = $("toggles"); box.replaceChildren();
  Object.entries(TOGGLES).forEach(([key, [label, hint]]) => {
    if (!(key in s)) return;
    const on = s[key] === true || s[key] === 1;
    const input = h("input", { type: "checkbox", checked: on, "aria-label": label, on: { change: async e => {
      const t = e.target, value = typeof s[key] === "number" ? (t.checked ? 1 : 0) : t.checked;
      t.disabled = true; const ok = await call("set_setting", { key, value }, { silent: true });
      if (!ok) { t.checked = !t.checked; t.disabled = false; return; }
      // The list was re-read after the write, so report what the robot says now.
      const now = ((state.settings || {}).data || {})[key], isOn = now === true || now === 1;
      if (isOn === t.checked) toast(label + (isOn ? " on" : " off"));
      else toast(label + " was sent, but the robot still reports it " + (isOn ? "on" : "off") + ". Tap refresh in a moment to check again.", true);
    } } });
    box.append(h("label", { class: "item", style: "cursor:pointer" }, h("div", { class: "grow" }, h("div", { class: "name" }, label), h("div", { class: "meta" }, hint)),
      h("span", { class: "switch" }, input, h("span"))));
  });
  if (!box.children.length) box.append(h("div", { class: "empty" }, "The robot didn't report any switchable settings."));
  $("settingsraw").textContent = JSON.stringify(s, null, 2);
  if (s.name) $("robotname").textContent = s.name;
};
$("setgo").addEventListener("click", async e => {
  const key = $("setkey").value.trim(); let value;
  try { value = JSON.parse($("setval").value); } catch (err) { toast('The value must be JSON, like true, 3, or "text".', true); return; }
  if (!key) return;
  if (await ask({ title: "Write " + key + "?", text: "New value: " + JSON.stringify(value), ok: "Write setting" })) call("set_setting", { key, value }, { button: e.currentTarget });
});

function hm(m) { return timeText(Math.floor(m / 60), m % 60); }
const DONE = { ok: "Finished", busy: "Finished", returnHomeEnd: "Finished", dndEnd: "Stopped for quiet hours", timeboxEnd: "Time limit reached",
  cncl: "Canceled", usrSlp: "Canceled", usrEnd: "Ended early", usrSpt: "Canceled", plcDoc: "Put on the dock", batcncl: "Battery ran low" };
RENDER.dnd = sec => {
  const d = sec.data || {}, parts = [];
  if (d.daily_start != null && d.daily_end != null && d.daily_start !== d.daily_end) parts.push("Every day, " + hm(d.daily_start) + " to " + hm(d.daily_end));
  if (d.ends_at && d.ends_at > Date.now()) parts.push("Until " + new Date(d.ends_at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }));
  $("dndnow").textContent = parts.join(" · ") || "Off";
};
$("dndrow").addEventListener("click", () => {
  const d = (state.dnd && state.dnd.data) || {};
  const toHHMM = m => String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
  let kind = "daily";
  openSheet((body, foot, done) => {
    body.append(h("h3", {}, "Do Not Disturb"), h("div", { class: "muted small" }, "The robot won't start scheduled or automatic jobs during quiet hours."));
    const start = h("input", { class: "input", type: "time", value: d.daily_start != null ? toHHMM(d.daily_start) : "22:00" });
    const end = h("input", { class: "input", type: "time", value: d.daily_end != null ? toHHMM(d.daily_end) : "07:00" });
    const until = h("input", { class: "input", type: "datetime-local" });
    const daily = h("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:10px" }, h("label", { class: "field" }, h("span", {}, "From"), start), h("label", { class: "field" }, h("span", {}, "To"), end));
    const once = h("label", { class: "field hidden" }, h("span", {}, "Quiet until"), until);
    body.append(seg([["daily", "Every day"], ["until", "Just for now"]], kind, v => { kind = v; daily.classList.toggle("hidden", v !== "daily"); once.classList.toggle("hidden", v === "daily"); }), daily, once);
    const save = h("button", { class: "btn primary block", on: { click: async () => {
      let ok;
      if (kind === "daily") ok = await call("set_dnd", { kind: "daily", start: start.value, end: end.value }, { button: save });
      else { if (!until.value) { toast("Pick when quiet time should end.", true); return; } ok = await call("set_dnd", { kind: "until", ends_at: new Date(until.value).getTime() }, { button: save }); }
      if (ok) done(true);
    } } }, "Save");
    foot.append(save, h("button", { class: "btn block", on: { click: () => done(null) } }, "Cancel"));
  });
});

$("reset").addEventListener("click", async () => {
  let wipe = false;
  const go = await openSheet((body, foot, done) => {
    body.append(h("h3", {}, "Factory reset?"), h("div", { class: "muted" }, "The robot will probably go offline and need setting up again in the iRobot app. This app will lose contact until then."));
    const cb = h("input", { type: "checkbox", on: { change: e => wipe = e.target.checked } });
    body.append(h("label", { class: "item", style: "padding:0;cursor:pointer" }, h("div", { class: "grow" }, h("div", { class: "name" }, "Also wipe the robot's data"), h("div", { class: "meta" }, "Maps and history on the robot")), h("span", { class: "switch" }, cb, h("span"))));
    const input = h("input", { class: "input", placeholder: "Type RESET to confirm", autocomplete: "off", autocapitalize: "characters" });
    body.append(input);
    const okb = h("button", { class: "btn danger block", disabled: true, on: { click: () => done("RESET") } }, "Factory reset");
    input.addEventListener("input", () => okb.disabled = input.value.trim() !== "RESET");
    foot.append(okb, h("button", { class: "btn block", on: { click: () => done(null) } }, "Cancel"));
  });
  if (go) call("factory_reset", { confirm: go, wipe });
});

// ---------- parts, history, details
function partRow(p, compact) {
  const used = p.count_used, left = p.count_remaining;
  let pct = null, text;
  if (typeof left === "number" && typeof used === "number" && left + used > 0) pct = left / (left + used);
  if (p.minutes_remaining != null) text = Math.max(0, Math.round(p.minutes_remaining / 60)) + " hours left";
  else if (left != null) text = left + " left";
  else text = "Counter " + (p.counter ?? "unknown");
  const name = (p.counter_category || "Part").replace(/_/g, " ");
  const bar = pct == null ? null : h("div", { class: "bar" + (pct < .1 ? " out" : pct < .25 ? " low" : "") }, h("i", { style: "width:" + Math.round(pct * 100) + "%" }));
  const row = h("div", { class: "item" }, h("div", { class: "grow" }, h("div", { class: "name", style: "text-transform:capitalize" }, name), h("div", { class: "meta" }, text), bar));
  if (!compact) row.append(h("button", { class: "btn", style: "min-height:40px", on: { click: async e => {
    const btn = e.currentTarget;
    const w = await ask({ title: "Mark " + name + " as replaced?", text: "Resets its wear counter. Do this after you replace or clean it.", ok: "Reset counter", danger: true, typed: "RESET" });
    if (w) call("reset_part", { part_id: p.part_id, confirm: w }, { button: btn });
  } } }, "Replaced"));
  return { row, pct };
}
RENDER.parts = sec => {
  const parts = ((sec.data || {}).parts) || [], box = $("parts"), home = $("homeparts");
  box.replaceChildren(); home.replaceChildren();
  if (!parts.length) { box.append(h("div", { class: "empty" }, "No parts reported.")); home.append(h("div", { class: "empty" }, "No parts reported.")); return; }
  parts.forEach(p => box.append(partRow(p).row));
  const sorted = parts.map(p => ({ p, x: partRow(p, true) })).sort((a, b) => (a.x.pct ?? 1) - (b.x.pct ?? 1)).slice(0, 2);
  sorted.forEach(({ x }) => home.append(x.row));
  home.append(h("div", { class: "item tappable", on: { click: () => showTab("more") } }, h("div", { class: "grow muted small" }, "All parts"), h("span", { class: "muted" }, "›")));
};

$("loadhistory").addEventListener("click", async e => {
  const row = e.currentTarget; row.querySelector(".name").textContent = "Loading...";
  const d = await call("info", { what: "history" }, { silent: true });
  const box = $("history"); box.replaceChildren();
  const items = (d && d.data) || [];
  if (!items.length) { box.append(h("div", { class: "empty" }, d ? "No recent cleans." : "Couldn't load history.")); return; }
  items.forEach(m => {
    const t = m.start_time ? new Date(m.start_time * (m.start_time < 1e12 ? 1000 : 1)) : null;
    const bits = [];
    if (m.duration_m != null) bits.push(m.duration_m + " min");
    if (m.square_feet_covered != null) bits.push(m.square_feet_covered + " sq ft");
    if (m.done_code) bits.push(DONE[m.done_code] || String(m.done_code));
    box.append(h("div", { class: "item" }, h("div", { class: "grow" },
      h("div", { class: "name" }, t ? t.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "Clean"),
      h("div", { class: "meta" }, bits.join(" · ") || "No details"))));
  });
});

const RAW = [["status", "Live status"], ["robot", "Robot info"], ["firmware", "Firmware"], ["estimates", "Time estimates"], ["clean_score", "Clean score"],
  ["profiles", "Cleaning profiles"], ["automations", "Automations"], ["notifications", "Notifications"]];
RAW.forEach(([what, label]) => {
  const pre = h("pre", {}, "Loading...");
  const det = h("details", { class: "raw" }, h("summary", {}, label), pre);
  det.addEventListener("toggle", async () => {
    if (!det.open || det.dataset.loaded) return;
    const d = await call("info", { what, map_id: state.map && state.map.id }, { silent: true });
    pre.textContent = d ? JSON.stringify(d.data, null, 2) : "Couldn't load this.";
    if (d) det.dataset.loaded = "1";
    if (d && what === "robot") fillRobot(d.data);
  });
  $("rawlist").append(det);
});
function fillRobot(r) {
  const kv = $("robotkv"); kv.replaceChildren();
  [["Name", r.name], ["Model", r.family || r.sku], ["SKU", r.sku], ["Serial", r.serial_number]].forEach(([k, v]) => { if (v) kv.append(h("dt", {}, k), h("dd", {}, v)); });
  if (r.name) $("robotname").textContent = r.name;
}

// ---------- boot
// ---------- saved login (Windows Credential Manager on the PC running the app)
function renderLogin(l) {
  $("loginsec").classList.toggle("hidden", !(l && l.supported));
  if (!l || !l.supported) return;
  $("loginmeta").textContent = l.saved ? l.email : "Typed each time the app starts";
  $("loginname").textContent = l.saved ? "Login saved on this PC" : "Login not saved";
  const b = $("loginbtn"); b.textContent = l.saved ? "Forget" : "Save"; b.dataset.saved = l.saved ? "1" : "";
}
$("loginbtn").addEventListener("click", async e => {
  const b = e.currentTarget, d = await call(b.dataset.saved ? "forget_login" : "save_login", {}, { button: b });
  if (d) renderLogin(d.login);
});

async function boot() {
  let saved = null; try { saved = localStorage.getItem("roomba-tab"); } catch (e) {}
  if (saved && document.querySelector('[data-view="' + saved + '"]')) showTab(saved);
  for (;;) {
    let data;
    try { const r = await fetch("api/cached", { method: "POST", headers: { "X-Roomba-Token": TOKEN } }); data = await r.json(); }
    catch (e) { setConn("bad", "Can't reach the app"); return; }
    apply(data.sections); renderLogin(data.login);
    if (data.login_error) { setConn("bad", "Login failed"); toast(data.login_error, true); return; }
    if (!data.loading) {
      const failed = Object.keys(data.errors || {});
      if (failed.length) { setConn("bad", "Some data didn't load"); toast("Couldn't load " + failed.join(", ") + ". Tap refresh to try again.", true); }
      else if (!data.sections.status) setConn("ok", "Connected");
      if (!data.sections.map) showNoMap(data.errors && data.errors.map);
      ["routines", "schedules"].forEach(k => { if (!data.sections[k]) RENDER[k]({ [k]: [] }); });
      return;
    }
    setConn("busy", "Loading your robot...");
    await new Promise(r => setTimeout(r, 900));
  }
}
boot();
</script>
</body>
</html>
"""


def lan_address() -> str:
    # Picks the address of the interface that would route out; nothing is sent.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
        except OSError:
            return "<this-pc's-ip>"


def main() -> int:
    parser = argparse.ArgumentParser(description="Local web app for a Roomba.")
    parser.add_argument("--lan", action="store_true",
                        help="also listen on your home network (needs the secret link)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--forget", action="store_true",
                        help="remove the login saved on this PC, then ask for it again")
    args = parser.parse_args()

    choice = roomba_login.get_login(forget_first=args.forget)
    if not choice:
        return 1
    login = choice.login
    print(f"Logging in as {login.email} ({len(login.password)}-character password, "
          f"country {login.country})...")

    if sys.platform == "win32":
        # The MQTT library needs add_reader/add_writer, which Windows'
        # default (Proactor) event loop doesn't implement.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    token = secrets.token_urlsafe(24)
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    app = make_app(Robot(choice), token, args.lan)

    print(f"\nRoomba app running. Open: http://127.0.0.1:{args.port}/")
    if args.lan:
        print(f"From your phone (same Wi-Fi), open: http://{lan_address()}:{args.port}/?key={token}")
        print("Keep that link private; anyone on your network with it can use the app.")
    print("Press Ctrl+C to quit.\n")
    try:
        web.run_app(app, host=host, port=args.port, print=None, access_log=None)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
