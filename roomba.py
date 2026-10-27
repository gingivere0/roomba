#!/usr/bin/env python3
"""Send one basic command to a Roomba (Prime/V4, cloud-only) and disconnect.

Usage:
    python roomba.py start|stop|pause|resume|dock|evac
    python roomba.py spot "<map name>" <x> <y> [vac|mop|both]
    python roomba.py forget      # remove the login saved on this PC

Only these robot commands are allowed. `spot` sends Roomba to one point on a
map (meters, as in atlas/atlas.json) to clean around it. If the Roomba app
(roomba_app.bat) is running, `spot` goes through it, so there's no second
login to knock its connection off; otherwise it logs in itself. Each run logs in to iRobot's
cloud exactly once (no retries), sends the command, and disconnects.

The login comes from Windows Credential Manager if it was saved there (see
roomba_login.py), otherwise from prompts: the email from ROOMBA_EMAIL or a
prompt, the password from ROOMBA_PASSWORD or a hidden prompt (the prompt is
safer: it keeps the password out of your environment and shell history), and
ROOMBA_COUNTRY or a prompt for the country (default "US"). After a typed login
works, it offers to remember it. On Windows, paste into the hidden prompt with
a right-click; Ctrl+V doesn't work there. The password is never printed or
written to a file.

Install the reviewed library version first:
    pip install "git+https://github.com/johnnyh1975/roombapy-prime@672456b079b39ccc6bd7519320974441b4307b82"
"""

import asyncio
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import aiohttp

from roombapy_prime import (
    AuthCredentialsError,
    AuthError,
    AuthRateLimitedError,
    CloudError,
    PrimeFactory,
)

from roombapy_prime.models.mission_control import MissionCommandType, RoutineCommand
from roombapy_prime.models.robot_info import parse_active_map_versions

import roomba_login

ALLOWED_COMMANDS = ("start", "stop", "pause", "resume", "dock", "evac")
SPOT_MODES = {"vac": 2, "mop": 4, "both": 32}
HERE = Path(__file__).resolve().parent


def usage() -> None:
    name = os.path.basename(sys.argv[0])
    print(f"Usage: {name} {{{'|'.join(ALLOWED_COMMANDS)}|forget}}", file=sys.stderr)
    print(f'       {name} spot "<map name>" <x> <y> [{"|".join(SPOT_MODES)}]', file=sys.stderr)


def parse_spot(args: list[str]):
    """(map name or id, x, y, operating mode), or None if the arguments are wrong."""
    if len(args) not in (3, 4) or (len(args) == 4 and args[3].lower() not in SPOT_MODES):
        return None
    try:
        x, y = float(args[1]), float(args[2])
    except ValueError:
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return args[0], x, y, SPOT_MODES[args[3].lower() if len(args) == 4 else "vac"]


def map_matches(map_ref: str, map_id: str, name: str | None) -> bool:
    return map_ref == map_id or map_ref.casefold() == (name or "").casefold()


def spot_via_app(map_ref: str, x: float, y: float, mode: int) -> int | None:
    """Asks the running Roomba app to do it. None if the app isn't running."""
    try:
        port = int(json.loads((HERE / "app" / "settings.json").read_text(encoding="utf-8")).get("port", 8766))
    except (OSError, ValueError):
        port = 8766
    base = f"http://127.0.0.1:{port}/"
    try:
        page = urllib.request.urlopen(base, timeout=5).read().decode("utf-8")
    except (urllib.error.URLError, OSError):
        return None
    token = re.search(r'window\.TOKEN = "([^"]+)"', page)
    if not token:
        return None

    def call(action: str, body: dict) -> dict:
        req = urllib.request.Request(base + "api/" + action, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Roomba-Token": token.group(1), "Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))

    maps = ((call("cached", {}).get("sections") or {}).get("map") or {}).get("maps") or []
    found = next((m for m in maps if map_matches(map_ref, m.get("id"), m.get("name"))), None)
    if not found:
        print(f"No map called '{map_ref}'. Maps: {', '.join(m.get('name') or m.get('id') for m in maps)}", file=sys.stderr)
        return 1
    result = call("point_clean", {"map_id": found["id"], "point": [x, y], "mode": mode})
    print(("Via the Roomba app: " if result.get("ok") else "The Roomba app said: ") + (result.get("message") or ""),
          file=sys.stdout if result.get("ok") else sys.stderr)
    return 0 if result.get("ok") else 3


async def spot_command(robot, map_ref: str, x: float, y: float, mode: int) -> RoutineCommand | None:
    maps = [m for m in parse_active_map_versions(await robot.get_active_map_versions())
            if m.robot_id in (None, robot.blid) and m.active_p2mapv_id]
    m = next((m for m in maps if map_matches(map_ref, m.p2map_id, m.name)), None)
    if not m:
        print(f"No map called '{map_ref}'. Maps: {', '.join(m.name or m.p2map_id for m in maps)}", file=sys.stderr)
        return None
    # The same point_clean shape the Roomba app sends (actions.point_clean).
    return RoutineCommand(command_type=MissionCommandType.POINT_CLEAN, asset_id=robot.blid,
                          map_id=m.p2map_id, pmap_version_id=m.active_p2mapv_id,
                          spot_geometry={"center": [round(x, 3), round(y, 3)]},
                          params={"operatingMode": mode}, initiator="rmtApp")


async def send(command: str, choice: roomba_login.Choice, spot=None) -> int:
    login = choice.login
    async with aiohttp.ClientSession() as session:
        try:
            # One login attempt, no retry loop. auto_refresh stays off so the
            # library doesn't keep the password around for re-logins.
            robot = await PrimeFactory.create_prime_robot(
                session, login.email, login.password, login.country)
        except AuthCredentialsError as err:
            print(roomba_login.after_rejection(choice, str(err))
                  or "Login rejected. Check the email and password.", file=sys.stderr)
            return 2
        except AuthRateLimitedError:
            print("Too many active sessions or logins. Sign out of the iRobot app "
                  "or wait a while before trying again.", file=sys.stderr)
            return 2
        except AuthError as err:
            print(f"Login failed: {type(err).__name__}", file=sys.stderr)
            return 2
        note = roomba_login.after_login(choice)
        if note:
            print(note)

        try:
            if spot:
                routine = await spot_command(robot, *spot)
                if routine is None:
                    return 1
                await robot.connect()
                acknowledged = await robot.send_routine_command_via_cmd_topic(routine)
            else:
                await robot.connect()
                acknowledged = await robot.send_simple_command(command)
        except CloudError as err:
            print(f"Sending '{command}' failed: {type(err).__name__}", file=sys.stderr)
            return 3
        finally:
            await robot.disconnect()

    if acknowledged:
        print(f"'{command}' delivered. Check the robot itself to confirm it reacted.")
        return 0
    print(f"'{command}' was sent but iRobot's cloud didn't confirm delivery.", file=sys.stderr)
    return 3


def main() -> int:
    command = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    spot = parse_spot(sys.argv[2:]) if command == "spot" else None
    if command == "spot":
        if not spot:
            usage()
            return 1
        from_app = spot_via_app(*spot)
        if from_app is not None:
            return from_app
    elif len(sys.argv) != 2 or command not in (*ALLOWED_COMMANDS, "forget"):
        usage()
        return 1
    if command == "forget":
        try:
            print("Removed the saved login from this PC." if roomba_login.forget()
                  else "There was no saved login on this PC.")
        except OSError as err:
            print(f"Couldn't remove the saved login: {err}", file=sys.stderr)
            return 1
        return 0

    choice = roomba_login.get_login(env_password=True)
    if not choice:
        return 1

    if sys.platform == "win32":
        # The MQTT client needs add_reader/add_writer, which Windows' default
        # Proactor event loop doesn't implement.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        return asyncio.run(send(command, choice, spot))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
