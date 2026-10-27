"""The iRobot login for the Roomba scripts, optionally remembered in Windows
Credential Manager so it doesn't have to be typed on every run.

The saved entry shows up in Control Panel > Credential Manager > Windows
Credentials as "roombapy-prime (iRobot login)". Windows encrypts it with your
user account, the same way it keeps other saved passwords; any program you run
can read it, but other users of the PC can't. Nothing is written to a file in
this folder, and the password is never printed.

Saving only happens after iRobot has accepted the login once, so a typo is
never remembered. Run a script with "forget" (roomba.bat forget) or
"--forget" (roomba_web.bat --forget), or use the button in the web app's
Settings, to remove it.

The Credential Manager bindings at the bottom (read_secret, write_secret, and
delete_secret) are shared with app/phonekey.py, which keeps the phone link's
key the same way.
"""

import ctypes
import getpass
import os
import sys
from dataclasses import dataclass, field

TARGET = "roombapy-prime (iRobot login)"


@dataclass(frozen=True)
class Login:
    email: str
    password: str = field(repr=False)
    country: str = "US"


def supported() -> bool:
    return sys.platform == "win32"


def load() -> Login | None:
    """The saved login, or None if there isn't one. Raises OSError if Windows refuses."""
    if not supported():
        return None
    found = read_secret(TARGET)
    if not found:
        return None
    email, country, password = found
    if not email or not password:
        return None
    return Login(email, password, country or "US")


def save(login: Login) -> None:
    """Saves (or replaces) the login. Raises OSError if Windows refuses."""
    if not supported():
        raise OSError("saving a login is only supported on Windows")
    # The account's country code (e.g. "US") rides along in the comment field.
    write_secret(TARGET, login.email, login.country, login.password)
    # Read it back so a silent mismatch can't leave a broken entry behind.
    if load() != login:
        forget()
        raise OSError("Windows didn't store the login correctly")


def forget() -> bool:
    """Removes the saved login. True if there was one."""
    if not supported():
        return False
    return delete_secret(TARGET)


# ---------- prompts

def ask(*, env_password: bool = False) -> Login | None:
    """Prompts for the login. None (after explaining why) if what was typed can't be right."""
    email = os.environ.get("ROOMBA_EMAIL") or input("iRobot email: ").strip()
    password = os.environ.get("ROOMBA_PASSWORD", "") if env_password else ""
    if not password:
        if sys.platform == "win32":
            print("(Ctrl+V doesn't paste into the hidden password prompt on Windows. "
                  "Type it, or right-click to paste.)")
        password = getpass.getpass("iRobot password: ")
    if any(ord(ch) < 32 for ch in password):
        print("That password contains a control character, which usually means Ctrl+V was "
              "pressed instead of pasting. Start again and type or right-click-paste it.",
              file=sys.stderr)
        return None
    if password != password.strip():
        print("Note: the password starts or ends with a space. If that's not intended, "
              "start again and enter it again.")
    country = (os.environ.get("ROOMBA_COUNTRY")
               or input("Country code of your iRobot account [US]: ").strip().upper() or "US")
    if not email or not password:
        print("An email and password are required.", file=sys.stderr)
        return None
    return Login(email, password, country)


@dataclass
class Choice:
    login: Login
    saved: bool          # it came from Credential Manager
    save_after_login: bool  # the user asked to remember it once iRobot accepts it


def get_login(*, forget_first: bool = False, env_password: bool = False) -> Choice | None:
    if forget_first:
        try:
            print("Removed the saved login from this PC." if forget()
                  else "There was no saved login on this PC.")
        except OSError as err:
            print(f"Couldn't remove the saved login: {err}", file=sys.stderr)
    if env_password and os.environ.get("ROOMBA_PASSWORD"):
        login = ask(env_password=True)
        return Choice(login, False, False) if login else None
    if supported():
        try:
            saved = load()
        except OSError as err:
            print(f"Couldn't read the saved login ({err}), so it has to be typed this time.")
            saved = None
        if saved:
            print(f"Using the login saved on this PC for {saved.email}.")
            return Choice(saved, True, False)
    login = ask()
    if not login:
        return None
    remember = False
    if supported():
        answer = input("Remember this login on this PC so you don't have to type it next time? [Y/n]: ")
        remember = answer.strip().lower() in ("", "y", "yes")
    return Choice(login, False, remember)


def after_login(choice: Choice) -> str | None:
    """Call once iRobot has accepted the login. Saves it if that was asked for."""
    if not choice.save_after_login:
        return None
    choice.save_after_login = False
    try:
        save(choice.login)
    except OSError as err:
        return f"Couldn't save the login on this PC: {err}"
    choice.saved = True
    return "Login saved on this PC. You won't have to type it next time."


def after_rejection(choice: Choice, reason: str) -> str | None:
    """Call when iRobot rejects the login. A saved login that stopped working is
    removed, and the explanation (with iRobot's reason) is returned."""
    choice.save_after_login = False
    if not choice.saved:
        return None
    try:
        forget()
    except OSError as err:
        return f"iRobot rejected the saved login ({reason}), and removing it failed: {err}"
    choice.saved = False
    return (f"iRobot rejected the saved login ({reason}), so it was removed from this PC. "
            "If the password changed, start again and type the new one.")


# ---------- Windows Credential Manager (advapi32), no extra packages
#
# Each entry is a "generic" credential: a target name, a user name, a comment,
# and a secret blob (stored as UTF-16, the way Windows itself stores passwords).

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2   # this user on this PC, survives restarts, doesn't roam
ERROR_NOT_FOUND = 1168


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _CREDENTIAL(ctypes.Structure):
    # CREDENTIALW, with fixed-size DWORDs so the layout is right on any 64-bit build.
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


_advapi32 = None


def _api():
    """The four advapi32 calls, with their argument types set up once."""
    global _advapi32
    if _advapi32 is None:
        lib = ctypes.WinDLL("advapi32", use_last_error=True)
        lib.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), ctypes.c_uint32]
        lib.CredWriteW.restype = ctypes.c_int
        lib.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
        lib.CredReadW.restype = ctypes.c_int
        lib.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        lib.CredDeleteW.restype = ctypes.c_int
        lib.CredFree.argtypes = [ctypes.c_void_p]
        lib.CredFree.restype = None
        _advapi32 = lib
    return _advapi32


def read_secret(target: str) -> tuple[str, str, str] | None:
    """(user name, comment, secret) for the entry, or None if there isn't one.
    Raises OSError if Windows refuses."""
    api = _api()
    found = ctypes.POINTER(_CREDENTIAL)()
    if not api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(found)):
        err = ctypes.get_last_error()
        if err == ERROR_NOT_FOUND:
            return None
        raise ctypes.WinError(err)
    try:
        cred = found.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        username, comment = cred.UserName or "", cred.Comment or ""
    finally:
        api.CredFree(found)
    return username, comment, blob.decode("utf-16-le")


def write_secret(target: str, username: str, comment: str, secret: str) -> None:
    """Saves (or replaces) the entry. Raises OSError if Windows refuses."""
    blob = secret.encode("utf-16-le")
    buf = ctypes.create_string_buffer(blob, len(blob))
    cred = _CREDENTIAL()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.Comment = comment
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = username
    if not _api().CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def delete_secret(target: str) -> bool:
    """Removes the entry. True if there was one. Raises OSError if Windows refuses."""
    if _api().CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        return True
    err = ctypes.get_last_error()
    if err == ERROR_NOT_FOUND:
        return False
    raise ctypes.WinError(err)
