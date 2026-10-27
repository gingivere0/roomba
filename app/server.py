"""Roomba app: a local web app modeled on iRobot's Roomba Home app.

Run it with roomba_app.bat (or: python app/server.py). Options live in
app/settings.json; command-line flags override them:
    --lan            also listen on your home network (needs the secret link)
    --local          this PC only
    --port N         port to listen on
    --fresh-logins   log in for every action instead of staying logged in
    --forget         remove the login saved on this PC, then ask for it again
    --new-key        replace the phone link's key (old links stop working)

Files:
    server.py     this file: web server, cache, live status
    link.py       logging in to iRobot and keeping the connection
    actions.py    everything the app can ask the robot to do
    web/          the page itself: index.html, app.css, and the js/ files
    settings.json your options

Every page load gets a secret that each request must send back. That stops
other websites you visit from pressing the buttons for you, and in LAN mode it
is also the "secret link" another device needs to get in. The secret is kept
in Windows Credential Manager (see phonekey.py), so the link stays the same
between runs; --new-key replaces it.
"""

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import mimetypes
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # roomba_login.py lives one folder up
sys.path.insert(0, str(HERE))

from aiohttp import web  # noqa: E402

import roomba_login  # noqa: E402
from actions import ACTIONS, DEBUG_DIR, READERS, REFRESH, ActionError, debug_dump, live_layers, parse_live_bundle, read_sections  # noqa: E402
from link import RobotLink  # noqa: E402
import phonekey  # noqa: E402

WEB = HERE / "web"
# Photos from the phone land here (C:\Users\...\roomba\photos) for Claude to read.
PHOTOS = HERE.parent / "photos"
PHOTO_MAX = 12 * 1024 * 1024
# The code the app watches for updates (see watch_code), and the exit code
# that tells roomba_app.bat to start it again.
CODE_FILES = lambda: sorted(HERE.glob("*.py")) + [HERE.parent / "roomba_login.py"]  # noqa: E731
RESTART_EXIT = 3
RESTART = {"wanted": False}


def code_stamps() -> dict[str, tuple[float, int]]:
    out = {}
    for f in CODE_FILES():
        try:
            st = f.stat()
            out[f.name] = (st.st_mtime, st.st_size)
        except OSError:
            pass
    return out


def compile_problem() -> str | None:
    for f in CODE_FILES():
        try:
            compile(f.read_text(encoding="utf-8"), str(f), "exec")
        except FileNotFoundError:
            continue
        except (SyntaxError, ValueError, OSError) as err:
            return f"{f.name}: {err}"
    return None
DEFAULTS = {
    "stay_logged_in": True,
    "lan": True,
    "port": 8766,
    # How often to re-read the robot's status while a page is open.
    "poll_seconds_cleaning": 5,
    "poll_seconds_idle": 30,
}
# Live map pacing, in seconds; see "live path while cleaning" below.
LIVE_MAP_WARMUP = 10        # quiet time after the start-up downloads
LIVE_MAP_RESTART_GAP = 120  # least time between one stream ending and the next
LIVE_MAP_VIEWER_GRACE = 120 # keep streaming this long after the last page closes
BUSY_PHASES = {"run", "hmUsrDock", "hmPostMsn", "hmMidMsn", "evac", "stuck"}


def load_settings() -> dict[str, Any]:
    settings = dict(DEFAULTS)
    path = HERE / "settings.json"
    if path.exists():
        try:
            settings.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as err:
            print(f"Ignoring settings.json ({err}); using defaults.")
    return settings


def make_app(link: RobotLink, token: str, settings: dict[str, Any], preload: bool = True) -> web.Application:
    cache: dict[str, Any] = {"loading": False, "sections": {}, "errors": {}, "login_error": None,
                             "plan": None, "updated": None,
                             # The robot's path in the current clean, from its live map stream.
                             "trail": None, "livemap": None}
    listeners: set[asyncio.Queue] = set()

    def push(event: dict[str, Any]) -> None:
        for q in list(listeners):
            if q.qsize() < 20:
                q.put_nowait(event)

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
        cache["updated"] = time.time()

    async def read(names: list[str], body: dict[str, Any]) -> None:
        mqtt = any(READERS[n][1] for n in names)
        sections, errors = await link.run(lambda r: read_sections(r, names, body, [True]), mqtt=mqtt)
        remember(sections, errors)
        push({"type": "sections", "sections": sections})

    async def preload_all() -> None:
        cache["loading"] = True
        try:
            await read(list(READERS), {"all_maps": True})
            trail["ready_at"] = time.time() + LIVE_MAP_WARMUP   # let iRobot's rate limit breathe after the burst
            print(f"preload: loaded {', '.join(cache['sections']) or 'nothing'}"
                  + (f"; failed {', '.join(cache['errors'])}" if cache["errors"] else ""))
        except ActionError as err:
            cache["login_error"] = str(err)
            print(f"preload: {err}")
        finally:
            cache["loading"] = False

    # --- live status ------------------------------------------------------------
    nudge = asyncio.Event()

    async def on_change() -> None:
        nudge.set()

    link.on_change = on_change

    def busy() -> bool:
        st = (cache["sections"].get("status") or {}).get("data") or {}
        return (st.get("cleanMissionStatus") or {}).get("phase") in BUSY_PHASES

    # --- live path while cleaning ------------------------------------------------
    # The live map needs a REST "keep-alive" to iRobot every few seconds, and
    # iRobot rate-limits those per account (the official app on a phone counts
    # too). So there is only ever one stream; it waits for the start-up burst
    # of map downloads to finish, keeps going through a pause, stops two
    # minutes after the last page closes, and never restarts more often than
    # once every two minutes.
    trail = {"task": None, "failed": None, "ended": 0.0, "ready_at": 0.0, "last_viewer": time.time()}

    def mission() -> dict[str, Any]:
        st = (cache["sections"].get("status") or {}).get("data") or {}
        return st.get("cleanMissionStatus") or {}

    async def follow_trail(mission_id: Any) -> None:
        robot = link.live_robot()
        if robot is None:
            return
        if not cache["trail"] or cache["trail"].get("mission") != mission_id:
            cache["trail"] = {"mission": mission_id, "points": [], "pos": None}
        points, pending, last_push = cache["trail"]["points"], [], 0.0
        if not cache.get("livemap") or cache["livemap"].get("mission") != mission_id:
            cache["livemap"] = None

        # The live map picture (explored area, coverage, obstacles) comes as a
        # download link: once in the answer to the keep-alive the library
        # already sends, and again whenever the robot publishes a new one.
        # Only the newest link is fetched, at most every 10 seconds.
        latest = {"url": None, "task": None, "fetched": 0.0, "dumped": False}

        async def fetch_livemap() -> None:
            while latest["url"]:
                wait = 10 - (time.time() - latest["fetched"])
                if wait > 0:
                    await asyncio.sleep(wait)
                url, latest["url"] = latest["url"], None
                latest["fetched"] = time.time()
                try:
                    bundle = parse_live_bundle(await robot.download_map_bundle(url))
                    layers = live_layers(bundle)
                except Exception as err:   # the path still shows without it
                    print(f"live map picture skipped ({type(err).__name__}: {err})")
                    continue
                if not latest["dumped"]:
                    latest["dumped"] = True
                    debug_dump("livemap", {"files": sorted(bundle), "bundle": bundle})
                cache["livemap"] = {"mission": mission_id, **layers}
                push({"type": "livemap", **cache["livemap"]})

        def want_livemap(url: Any) -> None:
            if not isinstance(url, str) or not url:
                return
            latest["url"] = url
            if latest["task"] is None or latest["task"].done():
                latest["task"] = asyncio.create_task(fetch_livemap())

        original_ping = robot.get_live_map_stream

        async def ping_and_note_map():
            answer = await original_ping()
            want_livemap(getattr(answer, "initial_map_url", None))
            return answer

        robot.get_live_map_stream = ping_and_note_map
        try:
            async with contextlib.aclosing(robot.watch_live_map()) as stream:
                async for message in stream:
                    if getattr(message, "livemap_url", None):
                        want_livemap(message.livemap_url)
                        continue
                    updates = getattr(message, "updates", None)
                    if not updates:
                        continue
                    now = time.time()
                    for u in updates:
                        x, y = u.point[0], u.point[1]
                        point = [round(x, 3), round(y, 3), round(now, 1), u.operating_modes]
                        points.append(point)
                        pending.append(point)
                        cache["trail"]["pos"] = [round(x, 3), round(y, 3), u.orientation]
                    if len(points) > 20000:
                        del points[:5000]
                    if now - last_push >= 1:
                        push({"type": "trail", "mission": mission_id, "points": pending, "pos": cache["trail"]["pos"]})
                        pending, last_push = [], now
        except asyncio.CancelledError:
            raise
        except Exception as err:   # the clean still shows without a path
            trail["failed"] = mission_id
            print(f"live map stopped ({type(err).__name__}: {err})")
        finally:
            trail["ended"] = time.time()
            with contextlib.suppress(AttributeError):
                del robot.get_live_map_stream   # back to the library's own method
            if latest["task"]:
                latest["task"].cancel()

    def mid_clean() -> bool:
        m = mission()
        return busy() or (m.get("phase") in ("stop", "pause") and m.get("cycle") not in (None, "none"))

    def manage_trail() -> None:
        m = mission()
        mission_id = m.get("missionId") or m.get("nMssn")
        task = trail["task"]
        now = time.time()
        if listeners:
            trail["last_viewer"] = now
        watched = now - trail["last_viewer"] < LIVE_MAP_VIEWER_GRACE
        if mid_clean() and mission_id is not None and watched:
            plan = cache["plan"]
            if plan and not plan.get("mission") and abs((m.get("mssnStrtTm") or 0) - plan.get("started", 0)) < 300:
                plan["mission"] = mission_id   # this clean is the one started from this app
            if ((task is None or task.done()) and trail["failed"] != mission_id and link.live_robot() is not None
                    and not cache["loading"] and now >= trail["ready_at"] and now - trail["ended"] >= LIVE_MAP_RESTART_GAP):
                trail["task"] = asyncio.create_task(follow_trail(mission_id))
        elif task is not None and not task.done():
            task.cancel()

    async def poll_status() -> None:
        while True:
            fast = busy()
            wait = settings["poll_seconds_cleaning"] if fast else settings["poll_seconds_idle"]
            if not link.stay:
                wait = max(wait, 60)   # each poll is a whole login in this mode
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(nudge.wait(), timeout=wait)
            nudge.clear()
            if not listeners or cache["loading"] or cache["login_error"]:
                manage_trail()   # stops the live map once nobody is looking
                continue
            try:
                was_busy = busy()
                await read(["status"], {})
                manage_trail()
                if was_busy and not busy():
                    # A clean just ended: part wear (the Home care card) and history moved on.
                    await read(["parts", "history"], {})
            except ActionError as err:
                push({"type": "offline", "message": str(err)})
            # The robot can report changes many times a second while cleaning;
            # read at most once every couple of seconds.
            await asyncio.sleep(2)

    async def watch_code() -> None:
        # Restart by itself after an update, so updates land while Seth is
        # away from the PC. Only when a .py file changes (the page's files
        # are read fresh on every load already), only once the files have
        # settled, and never into code that doesn't compile.
        seen = code_stamps()
        broken = None
        while True:
            await asyncio.sleep(3)
            now = code_stamps()
            if now == seen or now == broken:
                continue
            await asyncio.sleep(5)               # let the rest of the update land
            if code_stamps() != now:
                continue
            problem = compile_problem()
            if problem:
                if broken != now:
                    print(f"An update arrived but has an error, so the app keeps running the old version: {problem}")
                broken = now
                continue
            print("\nAn update arrived. Restarting the app to use it...")
            RESTART["wanted"] = True
            push({"type": "restarting"})
            await asyncio.sleep(0.5)
            app["stop"].set()
            return

    async def on_startup(app: web.Application) -> None:
        if preload:
            app["preload"] = asyncio.create_task(preload_all())
        app["poller"] = asyncio.create_task(poll_status())
        app["watcher"] = asyncio.create_task(watch_code())

    async def on_cleanup(app: web.Application) -> None:
        app["poller"].cancel()
        app["watcher"].cancel()
        if trail["task"]:
            trail["task"].cancel()
        await link.close()

    # --- requests ---------------------------------------------------------------
    def is_local(request: web.Request) -> bool:
        return request.host.split(":")[0] in ("127.0.0.1", "localhost")

    def token_ok(supplied: str) -> bool:
        return hmac.compare_digest(supplied.encode(), token.encode())

    async def index(request: web.Request) -> web.Response:
        # On this PC the page is open; from elsewhere it needs the secret link.
        if not is_local(request) and not token_ok(request.query.get("key", "")):
            raise web.HTTPForbidden(text="Open the full link the app printed when it started.")
        # ?v= makes the browser fetch fresh scripts and styles after an update.
        version = web_version()
        page = (WEB / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", token).replace("__V__", version)
        page = page.replace("__IMAGES__", json.dumps(sorted(f.name for f in (WEB / "img").glob("*.png"))))
        return web.Response(text=page, content_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    def login_info() -> dict[str, Any]:
        choice = link.choice
        return {"supported": roomba_login.supported(), "saved": choice.saved, "email": choice.login.email}

    def save_or_forget(action: str) -> tuple[bool, str]:
        choice = link.choice
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

    def snapshot() -> dict[str, Any]:
        return {**cache, "login": login_info(), "stay_logged_in": link.stay, "online": link.online}

    async def events(request: web.Request) -> web.StreamResponse:
        if not token_ok(request.query.get("t", "")):
            raise web.HTTPForbidden()
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store"})
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        listeners.add(q)
        q.put_nowait({"type": "hello", "version": web_version()})   # lets the page notice an update
        nudge.set()   # someone is looking: refresh now
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    await resp.write(b": keep-alive\n\n")
                    continue
                await resp.write(f"data: {json.dumps(event)}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            listeners.discard(q)
        return resp

    async def api(request: web.Request) -> web.Response:
        if not token_ok(request.headers.get("X-Roomba-Token", "")):
            raise web.HTTPForbidden(text="Missing or wrong token. Reload the page.")
        action = request.match_info["action"]
        if action == "client_error":
            # The page hit a script error; keep the details for fixing it.
            try:
                body = await request.json()
            except ValueError:
                body = {}
            line = {k: str(body.get(k, ""))[:3000] for k in ("message", "detail", "page", "agent")}
            print(f"page reported a problem: {line['message'][:200]}")
            try:
                DEBUG_DIR.mkdir(exist_ok=True)
                path = DEBUG_DIR / "client_errors.log"
                if path.exists() and path.stat().st_size > 200_000:
                    path.unlink()
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **line}) + "\n")
            except OSError:
                pass
            return web.json_response({"ok": True})
        if action == "photos":
            return web.json_response({"ok": True, "message": "", "photos": recent_photos()})
        if action == "cached":
            # Answered from memory: no login, no waiting.
            return web.json_response({"ok": True, "message": "", **snapshot()})
        if action in ("save_login", "forget_login"):
            ok, message = save_or_forget(action)
            return web.json_response({"ok": ok, "message": message, "login": login_info()})
        if action == "refresh_all":
            await preload_all()
            failed = cache["login_error"] or (
                "Couldn't load: " + ", ".join(cache["errors"]) if cache["errors"] else None)
            return web.json_response({"ok": not cache["login_error"], "message": failed or "Everything is up to date.",
                                      **snapshot()})
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
            mqtt, refresh = READERS[action][1], [action]
        else:
            handler, mqtt = ACTIONS[action]
            refresh = REFRESH.get(action, [])
        mqtt = mqtt or any(READERS[n][1] for n in refresh)

        async def job(r):
            result = await handler(r, body)
            sections, errors = await read_sections(r, refresh, body, [True])
            return result, sections, errors

        try:
            result, sections, errors = await link.run(job, mqtt=mqtt)
        except ActionError as err:
            return web.json_response({"ok": False, "message": str(err)})
        remember(sections, errors)
        if action in ("clean_rooms", "run_routine") and isinstance(body.get("plan"), dict):
            cache["plan"] = {**body["plan"], "started": time.time()}
        push({"type": "sections", "sections": sections})
        manage_trail()
        nudge.set()
        message = result if isinstance(result, str) else ""
        return web.json_response({"ok": True, "message": message, "data": None if isinstance(result, str) else result,
                                  "sections": sections, "errors": errors, "plan": cache["plan"]})

    async def photo(request: web.Request) -> web.Response:
        # A photo from the phone, saved for Claude to read (no API key, so it
        # can't go to Claude directly). Each photo gets a .json note beside it.
        if not token_ok(request.headers.get("X-Roomba-Token", "")):
            raise web.HTTPForbidden(text="Missing or wrong token. Reload the page.")
        data = bytearray()
        async for chunk in request.content.iter_chunked(64 * 1024):
            data += chunk
            if len(data) > PHOTO_MAX:
                return web.json_response({"ok": False, "message": "That photo is too big (over 12 MB)."})
        if not data.startswith(b"\xff\xd8\xff"):
            return web.json_response({"ok": False, "message": "That doesn't look like a JPEG photo."})
        q = request.query
        map_id = q.get("map", "")[:80]
        views = ((cache["sections"].get("map") or {}).get("views") or {})
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"photo-{stamp}"
        n = 2
        while (PHOTOS / f"{name}.jpg").exists():
            name, n = f"photo-{stamp}-{n}", n + 1
        info = {"file": f"{name}.jpg", "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": q.get("note", "")[:500], "map_id": map_id,
                "map_name": (views.get(map_id) or {}).get("name", ""),
                "width": int(q.get("w", 0) or 0), "height": int(q.get("h", 0) or 0),
                "status": "new"}
        try:
            PHOTOS.mkdir(exist_ok=True)
            (PHOTOS / info["file"]).write_bytes(bytes(data))
            (PHOTOS / f"{name}.json").write_text(json.dumps(info, indent=1), encoding="utf-8")
        except OSError as err:
            return web.json_response({"ok": False, "message": f"Couldn't save the photo on the PC ({err})."})
        print(f"photo saved: photos\\{info['file']} ({len(data) // 1024} KB)")
        start_photo_spot(PHOTOS / f"{name}.json")
        return web.json_response({"ok": True, "message": "Photo saved on your PC.", "photo": info,
                                  "photos": recent_photos()})

    def start_photo_spot(info_path: Path) -> None:
        # photo_spot.py (next to roomba.py) asks Claude Code where the photo
        # is and sends Roomba there, writing its progress into the .json.
        # It runs on its own so the upload answers at once.
        script = HERE.parent / "photo_spot.py"
        if (settings.get("photo_spot") or {}).get("enabled") is False or not script.exists():
            return
        try:
            subprocess.Popen([sys.executable, str(script), str(info_path)], cwd=str(HERE.parent),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as err:
            print(f"couldn't start photo_spot.py: {err}")

    def recent_photos(limit: int = 8) -> list[dict[str, Any]]:
        out = []
        for f in sorted(PHOTOS.glob("photo-*.json"), reverse=True)[:limit]:
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def web_version() -> str:
        # A fingerprint of the scripts and styles, so ?v= changes exactly when they do.
        digest = hashlib.sha1()
        for f in sorted(WEB.rglob("*.js")) + sorted(WEB.rglob("*.css")):
            digest.update(f.name.encode() + f.read_bytes())
        return digest.hexdigest()[:12]

    async def static(request: web.Request) -> web.Response:
        # Files are sent whole, every time: no partial (Range) replies. iPhone
        # Safari once stitched an old partial copy of screens.js onto a new
        # one, which broke the page with a SyntaxError.
        path = (WEB / request.match_info["path"]).resolve()
        if WEB.resolve() not in path.parents or not path.is_file():
            raise web.HTTPNotFound()
        body = path.read_bytes()
        # Fixed types first: Windows' registry can map .js to text/plain.
        kind = {".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml"}.get(
            path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in (".js", ".css"):
            # Kept only under the current fingerprint; an old ?v= gets a fresh copy that isn't stored.
            keep = request.query.get("v") == web_version()
        else:
            keep = True   # pictures never change
        headers = {"Cache-Control": "max-age=604800, immutable" if keep else "no-store", "Accept-Ranges": "none"}
        return web.Response(body=body, content_type=kind, charset="utf-8" if kind.startswith("text/") or kind.endswith("javascript") else None,
                            headers=headers)

    app = web.Application(client_max_size=64 * 1024)
    app["stop"] = asyncio.Event()
    app.router.add_get("/", index)
    app.router.add_get("/api/events", events)
    app.router.add_post("/api/photo", photo)
    app.router.add_post("/api/{action}", api)
    app.router.add_get("/static/{path:.+}", static)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def lan_address() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.168.1.1", 80))   # no packet is sent; this picks the LAN interface
            return s.getsockname()[0]
        except OSError:
            return "this-pc's-address"


def tailscale_addresses() -> list[str]:
    """This PC's Tailscale name (MagicDNS) and IP, if Tailscale is running."""
    found: list[str] = []
    exe = shutil.which("tailscale") or next((p for p in (r"C:\Program Files\Tailscale\tailscale.exe",) if Path(p).exists()), None)
    if exe:
        try:
            out = subprocess.run([exe, "status", "--json", "--self", "--peers=false"], capture_output=True, text=True, timeout=5)
            me = json.loads(out.stdout).get("Self") or {}
            name = (me.get("DNSName") or "").rstrip(".")
            if name:
                found.append(name)
            found += [ip for ip in me.get("TailscaleIPs") or [] if "." in ip]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if not found:
        # Tailscale's addresses are always in 100.64.0.0/10.
        with contextlib.suppress(OSError):
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                a, b = (int(x) for x in ip.split(".")[:2])
                if a == 100 and 64 <= b <= 127 and ip not in found:
                    found.append(ip)
    return found


class ShortKeepAliveWarnings(logging.Filter):
    """The library logs a whole traceback each time a live-map keep-alive is
    refused; one line says the same thing."""
    def filter(self, record: logging.LogRecord) -> bool:
        if "keep-alive ping failed" in str(record.msg):
            record.exc_info, record.exc_text = None, None
        return True


def main() -> int:
    logging.getLogger("roombapy_prime.prime_robot").addFilter(ShortKeepAliveWarnings())
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Roomba app (Roomba Home style).")
    parser.add_argument("--lan", action="store_true", default=None, help="also listen on your home network")
    parser.add_argument("--local", action="store_true", help="this PC only")
    parser.add_argument("--port", type=int)
    parser.add_argument("--fresh-logins", action="store_true", help="log in for every action")
    parser.add_argument("--forget", action="store_true", help="remove the saved login, then ask again")
    parser.add_argument("--new-key", action="store_true", help="replace the phone link's key; old links stop working")
    args = parser.parse_args()
    lan = False if args.local else (True if args.lan else bool(settings["lan"]))
    port = args.port or int(settings["port"])
    stay = False if args.fresh_logins else bool(settings["stay_logged_in"])

    choice = roomba_login.get_login(forget_first=args.forget)
    if not choice:
        return 1
    # Check the port before logging in: a second copy would also connect to
    # iRobot and knock the first one's live connection off over and over.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0" if lan else "127.0.0.1", port))
        except OSError:
            print(f"\nThe Roomba app is already running (port {port} is in use).")
            print("Use the window that's already open, or close it first and then start the app again.")
            return 1
    print(f"Logging in as {choice.login.email}"
          + (" and staying logged in while the app runs..." if stay else " (fresh login per action)..."))

    if sys.platform == "win32":
        # The MQTT library needs add_reader/add_writer, which Windows'
        # default (Proactor) event loop doesn't implement.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    token, kept = phonekey.get_key(new=args.new_key)
    app = make_app(RobotLink(choice, stay), token, settings)
    print(f"\nRoomba app running. Open: http://127.0.0.1:{port}/")
    if lan:
        print(f"From your phone (same Wi-Fi), open: http://{lan_address()}:{port}/?key={token}")
        for host in tailscale_addresses():
            print(f"From anywhere over Tailscale, open:  http://{host}:{port}/?key={token}")
        print("Keep these links private; anyone who can reach this PC with one can use the app."
              + (" They stay the same between runs (--new-key replaces them)." if kept else ""))
    print("Press Ctrl+C to quit.\n")
    async def serve() -> None:
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "0.0.0.0" if lan else "127.0.0.1", port).start()
            await app["stop"].wait()          # set by watch_code after an update
        finally:
            await runner.cleanup()            # logs out of iRobot cleanly
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    return RESTART_EXIT if RESTART["wanted"] else 0


if __name__ == "__main__":
    sys.exit(main())
