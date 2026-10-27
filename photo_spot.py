"""Turns a phone photo from the Roomba app into a spot clean, with no API key.

The app saves photos\\photo-*.jpg plus a .json note and starts this script with
the .json path. It asks Claude Code on this PC (Seth's own login, `claude -p`)
to find the spot in atlas\\atlas.json, checks the answer against the real map
(inside a room, not in a keep-out zone), then sends the spot through the
running app. Progress goes into the photo's .json "status":
thinking -> sent | found | unsure | error
(found = located but not sent, when settings.json photo_spot.auto_send is false). Claude only gets the Read tool here; this
script does the sending.

    photo_spot.bat photos\\photo-20261021-195500.json
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PHOTOS = HERE / "photos"
ATLAS = HERE / "atlas" / "atlas.json"
DEBUG = HERE / "app" / "debug"
LOCK = PHOTOS / ".photo_spot.lock"
LOG = PHOTOS / "photo_spot.log"
SETTINGS = HERE / "app" / "settings.json"
MODES = {"vac": 2, "mop": 4, "both": 32}

PROMPT = """You are helping a Roomba spot-clean a place Seth photographed in his house.

Photo: {photo}
Seth's note: {note}
Map he was looking at in the app: {map_name} ({map_id})

The house atlas is {atlas}. It has every map (floor), its rooms with what they
look like, and landmarks with map coordinates in meters ("at": [x, y]) and
reference frames from walk-through videos in {frames} (frames/<floor>/tNN.jpg).

Do this:
1. Read the photo.
2. Read the atlas. Work out which floor and room the photo shows, and which
   landmark it is closest to. Read up to 4 reference frames to compare if
   that helps.
   Check "spot_clean" in the atlas first: its history and lesson are real
   results, and landmarks with a "confirmed" field come from real cleans
   (compare the photo with their frames).
3. Pick the point to clean: the center of the dirty or photographed floor
   area, not the furniture itself. Furniture at the top edge of a floor photo
   is usually 1.5-2.5 m beyond the spot, so put the point that far out from
   it, toward where the camera stood. Shift from the landmark by your best
   estimate of distance and direction (the robot cleans about a 1.2 m square
   around the point). Never pick a point in a keep-out area named in the
   atlas (for example a closet or shower).
4. Mode: "vac" unless the note asks to mop ("mop") or to do both ("both").

Reply with ONLY one JSON object, no other text:
{{"map_id": "...", "x": 0.0, "y": 0.0, "mode": "vac", "room": "...",
  "landmark": "landmark id or null", "confidence": "high|medium|low",
  "reason": "one short sentence Seth will see"}}
Use "confidence": "low" with your best guess if you can't tell where it is.
"""


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def update(info_path: Path, **fields) -> dict:
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info.update(fields)
    tmp = info_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, indent=1), encoding="utf-8")
    os.replace(tmp, info_path)
    return info


def settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8")).get("photo_spot") or {}
    except (OSError, ValueError):
        return {}


def take_lock(wait: float = 300) -> bool:
    """One photo at a time; later ones wait their turn."""
    end = time.time() + wait
    while time.time() < end:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - LOCK.stat().st_mtime > 600:  # left behind by a crash
                    LOCK.unlink()
                    continue
            except OSError:
                pass
            time.sleep(2)
    return False


def ask_claude(prompt: str, cfg: dict) -> dict:
    exe = cfg.get("claude") or shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")
    if not exe:
        raise RuntimeError("Claude Code (claude) isn't on this PC's PATH")
    cmd = [exe, "-p", "--output-format", "json",
           "--allowedTools", "Read",
           "--disallowedTools", "Bash", "PowerShell", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch"]
    if cfg.get("model", "sonnet"):
        cmd += ["--model", cfg.get("model", "sonnet")]
    run = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                         timeout=int(cfg.get("timeout", 240)), input=prompt)  # stdin: cmd.exe would mangle a multi-line argument
    if run.returncode != 0:
        raise RuntimeError(f"claude exited {run.returncode}: {(run.stderr or run.stdout)[-400:]}")
    try:
        text = json.loads(run.stdout).get("result") or ""
    except ValueError:
        text = run.stdout
    found = re.findall(r"\{.*\}", text, re.S)
    if not found:
        raise RuntimeError(f"no answer from Claude: {text[-300:]}")
    return json.loads(found[-1])


def rings(geom: dict) -> list[list]:
    t, c = geom.get("type"), geom.get("coordinates") or []
    return [c[0]] if t == "Polygon" else [p[0] for p in c] if t == "MultiPolygon" else []


def inside(pt, ring) -> bool:
    x, y, hit = pt[0], pt[1], False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def check_point(map_id: str, x: float, y: float) -> str | None:
    """Problem with the point, or None. Skips the check if no map copy exists."""
    f = DEBUG / f"map_{map_id}.json"
    if not f.exists():
        return None
    bundle = json.loads(f.read_text(encoding="utf-8")).get("bundle") or {}
    for z in (bundle.get("policyZones") or {}).get("features", []):
        if (z.get("properties") or {}).get("type") == "KeepOutZone" and any(inside((x, y), r) for r in rings(z["geometry"])):
            return "that point is in a keep-out zone"
    rooms = (bundle.get("rooms") or {}).get("features", [])
    if rooms and not any(inside((x, y), r) for f_ in rooms for r in rings(f_["geometry"])):
        # Rooms trace where the robot has driven; allow a little slack at the edges.
        near = any(inside((x + dx, y + dy), r) for f_ in rooms for r in rings(f_["geometry"])
                   for dx in (-0.4, 0, 0.4) for dy in (-0.4, 0, 0.4))
        if not near:
            return "that point is outside every room the robot knows"
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    info_path = Path(sys.argv[1])
    if not info_path.is_absolute():
        info_path = (HERE / info_path) if (HERE / info_path).exists() else PHOTOS / info_path.name
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if info.get("status") not in (None, "new"):
        log(f"{info_path.name}: already {info.get('status')}, skipping")
        return 0
    cfg = settings()
    if cfg.get("enabled") is False:
        log(f"{info_path.name}: photo_spot is turned off in settings.json")
        return 0
    if not take_lock():
        update(info_path, status="error", message="Another photo was still being handled. Try again.")
        return 1
    try:
        update(info_path, status="thinking", message="Claude is finding the spot...")
        log(f"{info_path.name}: asking Claude")
        started = time.time()
        atlas = json.loads(ATLAS.read_text(encoding="utf-8"))
        prompt = PROMPT.format(photo=PHOTOS / info["file"], note=info.get("note") or "(none)",
                               map_name=info.get("map_name") or "?", map_id=info.get("map_id") or "?",
                               atlas=ATLAS, frames=ATLAS.parent / "frames")
        ans = ask_claude(prompt, cfg)
        took = round(time.time() - started)
        log(f"{info_path.name}: Claude said {json.dumps(ans)} ({took}s)")
        map_id = ans.get("map_id")
        if map_id not in atlas.get("maps", {}):
            update(info_path, status="unsure", answer=ans, message=f"Claude couldn't place this photo: {ans.get('reason')}")
            return 0
        x, y = float(ans["x"]), float(ans["y"])
        mode = MODES.get(str(ans.get("mode", "vac")).lower(), 2)
        where = f"{atlas['maps'][map_id]['name']}, {ans.get('room') or '?'} ({x:.1f}, {y:.1f})"
        problem = check_point(map_id, x, y)
        if problem:
            update(info_path, status="unsure", answer=ans, message=f"Not sent: {problem}. Claude's guess: {where}. {ans.get('reason')}")
            return 0
        if ans.get("confidence") == "low" and not cfg.get("send_low_confidence", False):
            update(info_path, status="unsure", answer=ans,
                   message=f"Not sent, Claude wasn't sure. Best guess: {where}. {ans.get('reason')}")
            return 0
        spot = {"map_id": map_id, "point": [x, y], "mode": mode}
        if not cfg.get("auto_send", True):
            update(info_path, status="found", answer=ans, spot=spot, seconds=took,
                   message=f"Found it: {where}. Tap Start to send the Roomba. {ans.get('reason')}")
            log(f"{info_path.name}: found {where} (auto_send off)")
            return 0
        sys.path.insert(0, str(HERE))
        import roomba  # noqa: E402  (same venv as the app)
        rc = roomba.spot_via_app(map_id, x, y, mode)
        if rc != 0:
            update(info_path, status="error", answer=ans,
                   message=f"Found it ({where}) but sending failed" + (" (Roomba app not running)." if rc is None else "."))
            return 1
        update(info_path, status="sent", answer=ans, sent=spot,
               message=f"Sent the Roomba to {where}. {ans.get('reason')}", seconds=took)
        log(f"{info_path.name}: sent to {where}")
        return 0
    except Exception as err:  # noqa: BLE001 - every failure should land in the .json for Seth
        log(f"{info_path.name}: error {err!r}")
        update(info_path, status="error", message=f"Something went wrong: {err}")
        return 1
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
