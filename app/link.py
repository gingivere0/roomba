"""How the app talks to iRobot's cloud: one login kept open, or a fresh login
per action.

Staying logged in (the default, like the official app) is what makes the app
quick and lets it show live progress: the robot connection is opened once and
reused, and the robot's status updates arrive as they happen. Logging in for
every action is the older, more cautious behavior; set "stay_logged_in" to
false in settings.json (or start with --fresh-logins) to get it back.

Either way the password stays in memory only (or in Windows Credential
Manager if you chose to save it; see roomba_login.py), it is never printed,
and a failed login is never retried in a loop: after one, the next attempt
waits at least MIN_LOGIN_GAP seconds.
"""

import asyncio
import contextlib
import time
from typing import Any, Awaitable, Callable

import aiohttp
from roombapy_prime import (
    AuthCredentialsError,
    AuthError,
    AuthRateLimitedError,
    CloudError,
    PrimeFactory,
)

import roomba_login
from actions import ActionError

MIN_LOGIN_GAP = 60.0   # seconds between login attempts after the first


class RobotLink:
    def __init__(self, choice: roomba_login.Choice, stay_logged_in: bool) -> None:
        self.choice = choice
        self.stay = stay_logged_in
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._robot = None
        self._connected = False
        self._last_login = 0.0
        self._watcher: asyncio.Task | None = None
        # Called (with no arguments) whenever the robot says something changed.
        self.on_change: Callable[[], Awaitable[None]] | None = None

    @property
    def online(self) -> bool:
        return self._robot is not None and self._connected

    def live_robot(self):
        """The connected robot, for long-running streams (the live map). Only
        when staying logged in; None otherwise."""
        return self._robot if (self.stay and self._connected) else None

    async def _login(self, session: aiohttp.ClientSession):
        wait = self._last_login + MIN_LOGIN_GAP - time.monotonic()
        if self._last_login and wait > 0:
            raise ActionError(f"Waiting before logging in again. Try again in {int(wait) + 1} seconds.")
        self._last_login = time.monotonic()
        login = self.choice.login
        try:
            # auto_refresh lets a kept-open connection renew its token before
            # it expires (about an hour) instead of dropping.
            robot = await PrimeFactory.create_prime_robot(
                session, login.email, login.password, login.country, auto_refresh=self.stay)
        except AuthRateLimitedError as err:
            raise ActionError("Too many active sessions or logins. Sign out of the iRobot app "
                              f"or wait a while before trying again. iRobot said: {err}")
        except AuthCredentialsError as err:
            note = roomba_login.after_rejection(self.choice, str(err))
            raise ActionError(note or f"Login rejected. iRobot said: {err}")
        except AuthError as err:
            raise ActionError(f"Login failed ({type(err).__name__}): {err}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ActionError(f"Couldn't reach iRobot's cloud: {type(err).__name__}")
        note = roomba_login.after_login(self.choice)
        if note:
            print(note)
        return robot

    async def run(self, job: Callable[[Any], Awaitable[Any]], *, mqtt: bool) -> Any:
        """Runs one job against the robot. One job at a time."""
        async with self._lock:
            if not self.stay:
                return await self._run_fresh(job, mqtt=mqtt)
            if self._robot is None:
                if self._session is None:
                    self._session = aiohttp.ClientSession()
                self._robot = await self._login(self._session)
            try:
                if mqtt and not self._connected:
                    await self._robot.connect()
                    self._connected = True
                    self._start_watching()
                return await job(self._robot)
            except ActionError:
                raise
            except (AuthError, CloudError, aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as err:
                # Most likely the connection went stale. Drop it so the next
                # action starts a new one (subject to MIN_LOGIN_GAP).
                await self._drop()
                raise ActionError(f"iRobot's cloud returned an error: {type(err).__name__}. "
                                  "The app will reconnect on the next try.")

    async def _run_fresh(self, job, *, mqtt: bool) -> Any:
        async with aiohttp.ClientSession() as session:
            robot = await self._login(session)
            self._last_login = 0.0   # fresh mode logs in per action by design
            try:
                if mqtt:
                    await robot.connect()
                return await job(robot)
            except ActionError:
                raise
            except (CloudError, aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as err:
                raise ActionError(f"iRobot's cloud returned an error: {type(err).__name__}")
            finally:
                await robot.disconnect()

    def _start_watching(self) -> None:
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        """Turns the robot's own change notices into status refreshes. Battery
        and mission changes come on "ro-currentstate"; the rest on the
        classic shadow."""
        await asyncio.gather(self._watch_one(None), self._watch_one("ro-currentstate"))

    async def _watch_one(self, name: str | None) -> None:
        robot = self._robot
        try:
            async with contextlib.aclosing(robot.watch_state(name)) as updates:
                async for _ in updates:
                    if self.on_change:
                        await self.on_change()
        except asyncio.CancelledError:
            raise
        except Exception as err:   # the poll loop keeps status fresh anyway
            print(f"live updates ({name or 'classic'}) stopped ({type(err).__name__}); polling instead")

    async def _drop(self) -> None:
        if self._watcher:
            self._watcher.cancel()
            self._watcher = None
        robot, self._robot, self._connected = self._robot, None, False
        if robot is not None:
            with contextlib.suppress(Exception):
                await robot.disconnect()

    async def close(self) -> None:
        async with self._lock:
            await self._drop()
            if self._session is not None:
                await self._session.close()
                self._session = None
