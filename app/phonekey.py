"""The phone link's secret key, kept in Windows Credential Manager so the link
stays the same every time the app starts.

The entry shows up in Credential Manager > Windows Credentials as
"roomba app (phone link key)". Run roomba_app.bat --new-key to replace it,
which makes every old link stop working. Off Windows, or if Windows refuses,
the app falls back to a new key on each run.
"""

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import roomba_login  # noqa: E402  (shares its Credential Manager bindings)

TARGET = "roomba app (phone link key)"


def _load() -> str | None:
    found = roomba_login.read_secret(TARGET)
    return found[2] or None if found else None


def _save(key: str) -> None:
    roomba_login.write_secret(TARGET, "phone", "", key)
    if _load() != key:
        raise OSError("Windows didn't store the key correctly")


def get_key(new: bool = False) -> tuple[str, bool]:
    """(key, saved). saved is False when the key only lasts for this run."""
    if not roomba_login.supported():
        return secrets.token_urlsafe(24), False
    try:
        key = None if new else _load()
        if not key:
            key = secrets.token_urlsafe(24)
            _save(key)
        return key, True
    except OSError as err:
        print(f"Couldn't keep the phone link key in Credential Manager ({err}); this run gets a new link.")
        return secrets.token_urlsafe(24), False
