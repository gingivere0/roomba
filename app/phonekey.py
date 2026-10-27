"""The phone link's secret key, kept in Windows Credential Manager so the link
stays the same every time the app starts.

The entry shows up in Credential Manager > Windows Credentials as
"roomba app (phone link key)". Run roomba_app.bat --new-key to replace it,
which makes every old link stop working. Off Windows, or if Windows refuses,
the app falls back to a new key on each run.
"""

import ctypes
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import roomba_login as rl   # noqa: E402  (reuses its Credential Manager bindings)

TARGET = "roomba app (phone link key)"


def _load() -> str | None:
    _, read, _, free = rl._api()
    found = ctypes.POINTER(rl._CREDENTIAL)()
    if not read(TARGET, rl.CRED_TYPE_GENERIC, 0, ctypes.byref(found)):
        err = ctypes.get_last_error()
        if err == rl.ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(err)
    try:
        cred = found.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
    finally:
        free(found)
    return blob.decode("utf-16-le") or None


def _save(key: str) -> None:
    write, _, _, _ = rl._api()
    blob = key.encode("utf-16-le")
    buf = ctypes.create_string_buffer(blob, len(blob))
    cred = rl._CREDENTIAL()
    cred.Type = rl.CRED_TYPE_GENERIC
    cred.TargetName = TARGET
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = rl.CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "phone"
    if not write(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())
    if _load() != key:
        raise OSError("Windows didn't store the key correctly")


def get_key(new: bool = False) -> tuple[str, bool]:
    """(key, saved). saved is False when the key only lasts for this run."""
    if not rl.supported():
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
