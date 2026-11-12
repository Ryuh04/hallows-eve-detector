"""
Roblox accounts: sign in once per account, then launch Roblox with it straight into a
private server, as many windows at once as you like.

- Sign-ins are kept as the account's .ROBLOSECURITY cookie, encrypted with Windows
  (DPAPI), so accounts.json is useless on any other PC or Windows user. The cookie is
  never printed or logged.
- Signing in happens in a small Edge (or Chrome) window with its own empty profile; the
  cookie is read from it through the browser's DevTools port once Roblox sets it. The
  password is typed into roblox.com only.
- Launching asks Roblox for a one-time join ticket and opens the roblox-player: link,
  the same thing the Play button on the website does.
- Multiple Roblox windows: Roblox closes extra copies of itself unless its single-
  instance mutex already exists, so the app holds it while it's open.
"""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import random
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOGIN_URL = "https://www.roblox.com/login"
COOKIE_NAME = ".ROBLOSECURITY"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
WINDOW_WAIT = 90  # seconds to wait for a launched Roblox window to appear


class AccountError(Exception):
    """Something went wrong with an account; the text is shown to the user as it is."""


# --------------------------------------------------------------------------- #
# Windows DPAPI: encrypt the cookie for this Windows user only
# --------------------------------------------------------------------------- #

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wt.LPCWSTR, ctypes.POINTER(_Blob),
                                      ctypes.c_void_p, ctypes.c_void_p, wt.DWORD,
                                      ctypes.POINTER(_Blob)]
_crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p,
                                        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                                        wt.DWORD, ctypes.POINTER(_Blob)]
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_UI_FORBIDDEN = 0x1
_ENTROPY = b"HallowsEveDetector accounts"


def _dpapi(data, protect):
    source = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                          ctypes.POINTER(ctypes.c_char)))
    entropy = _Blob(len(_ENTROPY), ctypes.cast(ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY)),
                                               ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    if protect:
        ok = _crypt32.CryptProtectData(ctypes.byref(source), None, ctypes.byref(entropy), None,
                                       None, _UI_FORBIDDEN, ctypes.byref(out))
    else:
        ok = _crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy), None,
                                         None, _UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise AccountError("Windows couldn't unlock this saved sign-in (was it saved by another "
                           "Windows user?). Sign in again.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(text):
    return base64.b64encode(_dpapi(text.encode(), True)).decode()


def unprotect(blob):
    return _dpapi(base64.b64decode(blob), False).decode()


def clean_cookie(text):
    """A pasted cookie without the bits people copy along with it (name=, quotes, spaces)."""
    text = text.strip().strip('"').strip("'").strip()
    if text.lower().startswith(COOKIE_NAME.lower() + "="):
        text = text[len(COOKIE_NAME) + 1:]
    return text.strip().rstrip(";").strip()


# --------------------------------------------------------------------------- #
# Talking to Roblox
# --------------------------------------------------------------------------- #

class RobloxSession:
    """Requests to Roblox's web API as one account. Handles the CSRF token Roblox asks for
    on POSTs (the first one is refused with a token to use; it's then sent again)."""

    def __init__(self, cookie, opener=None):
        self.cookie = cookie
        self.csrf = ""
        self.open = opener or urllib.request.urlopen

    def call(self, method, url, body=None, headers=None):
        """(status, response headers, parsed JSON or raw bytes)."""
        for attempt in range(2):
            sent = {"Cookie": f"{COOKIE_NAME}={self.cookie}", "User-Agent": USER_AGENT,
                    "Referer": "https://www.roblox.com/", "Origin": "https://www.roblox.com",
                    "Accept": "application/json"}
            data = None
            if method != "GET":
                sent["X-CSRF-TOKEN"] = self.csrf
                sent["Content-Type"] = "application/json"
                data = json.dumps(body if body is not None else {}).encode()
            sent.update(headers or {})
            request = urllib.request.Request(url, data=data, headers=sent, method=method)
            try:
                with self.open(request, timeout=20) as response:
                    raw = response.read()
                    try:
                        parsed = json.loads(raw) if raw else {}
                    except ValueError:
                        parsed = raw
                    return response.status, response.headers, parsed
            except urllib.error.HTTPError as e:
                token = e.headers.get("x-csrf-token") if e.headers else None
                if e.code == 403 and token and attempt == 0:
                    self.csrf = token
                    continue
                if e.code == 401:
                    raise AccountError("This account is signed out (the saved sign-in expired). "
                                       "Sign in again.") from None
                if e.code == 429:
                    raise AccountError("Roblox says too many requests; wait a minute and try "
                                       "again.") from None
                raise AccountError(f"Roblox refused the request ({e.code}).") from None
            except (urllib.error.URLError, OSError) as e:
                raise AccountError(f"Couldn't reach Roblox ({getattr(e, 'reason', e)}).") from None
        raise AccountError("Roblox kept refusing the request (security token).")


def who_is(cookie, opener=None):
    """{'id', 'name', 'display'} of the account a cookie signs in to."""
    _, _, data = RobloxSession(cookie, opener).call(
        "GET", "https://users.roblox.com/v1/users/authenticated")
    if not isinstance(data, dict) or "id" not in data:
        raise AccountError("Roblox didn't accept that sign-in.")
    return {"id": int(data["id"]), "name": str(data.get("name", "")),
            "display": str(data.get("displayName") or data.get("name", ""))}


def parse_server_link(link):
    """What a pasted link points at:
    {'share': code}                      roblox.com/share?code=…&type=Server
    {'place_id': n, 'link_code': code}   roblox.com/games/n/…?privateServerLinkCode=…
    {'place_id': n, 'link_code': None}   roblox.com/games/n/… (a public server)"""
    text = (link or "").strip()
    if not text:
        raise AccountError("Paste a private server link first.")
    if "://" not in text:
        text = "https://" + text
    parts = urllib.parse.urlparse(text)
    host = parts.netloc.lower().split(":")[0]
    if not (host == "roblox.com" or host.endswith(".roblox.com")):
        raise AccountError("That isn't a roblox.com link.")
    query = urllib.parse.parse_qs(parts.query)
    if parts.path.rstrip("/").lower().endswith("/share") and query.get("code"):
        kind = (query.get("type") or ["Server"])[0]
        if kind.lower() != "server":
            raise AccountError(f"That share link is for a {kind}, not a server.")
        return {"share": query["code"][0]}
    match = re.search(r"/games/(\d+)", parts.path, re.IGNORECASE)
    if match:
        code = (query.get("privateServerLinkCode") or [None])[0]
        return {"place_id": int(match.group(1)), "link_code": code}
    raise AccountError("That link isn't a Roblox server or game link.")


def resolve_share(session, code):
    """(place ID, private server link code) behind a roblox.com/share?code=… link."""
    _, _, data = session.call("POST", "https://apis.roblox.com/sharelinks/v1/resolve-link",
                              {"linkId": code, "linkType": "Server"})
    invite = data.get("privateServerInviteData") if isinstance(data, dict) else None
    if not invite or not invite.get("placeId"):
        raise AccountError("Roblox didn't recognise that private server link.")
    status = str(invite.get("status", "Valid"))
    if status.lower() != "valid":
        raise AccountError(f"That private server link isn't usable ({status}); it may have "
                           "expired or been reset.")
    return int(invite["placeId"]), invite.get("linkCode")


def auth_ticket(session):
    """A one-time ticket that lets the Roblox player sign in as this account."""
    _, headers, _ = session.call("POST", "https://auth.roblox.com/v1/authentication-ticket/",
                                 headers={"RBXAuthenticationNegotiation": "1"})
    ticket = headers.get("rbx-authentication-ticket") if headers else None
    if not ticket:
        raise AccountError("Roblox didn't hand out a join ticket.")
    return ticket


def launch_uri(ticket, place_id, link_code=None, tracker=None):
    """The roblox-player: link that starts Roblox signed in and joins the place (the private
    server, when there's a link code)."""
    tracker = tracker or random.randint(10 ** 11, 10 ** 12 - 1)
    if link_code:
        launcher = (f"https://www.roblox.com/Game/PlaceLauncher.ashx?request=RequestPrivateGame"
                    f"&browserTrackerId={tracker}&placeId={place_id}&linkCode={link_code}")
    else:
        launcher = (f"https://www.roblox.com/Game/PlaceLauncher.ashx?request=RequestGame"
                    f"&browserTrackerId={tracker}&placeId={place_id}&isPlayTogetherGame=false")
    return ("roblox-player:1+launchmode:play+gameinfo:" + ticket
            + f"+launchtime:{int(time.time() * 1000)}"
            + "+placelauncherurl:" + urllib.parse.quote(launcher, safe="")
            + f"+browsertrackerid:{tracker}+robloxLocale:en_us+gameLocale:en_us+channel:"
            + "+LaunchExp:InApp")


def new_tracker():
    """A number for one launch. Roblox shows it on its command line and in its log, which is
    how a window is told apart when several open at the same time."""
    return str(random.randint(10 ** 11, 10 ** 12 - 1))


def process_tracker(pid):
    """The launch number a Roblox process was started with, or None if it has none (like
    Roblox's background tray process). The command line also holds the account's join ticket,
    so only the number leaves this function."""
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_string_buffer(1 << 16)
        needed = wt.ULONG()
        if _ntdll.NtQueryInformationProcess(handle, 60, buf, len(buf),  # ProcessCommandLineInformation
                                            ctypes.byref(needed)) != 0:
            return None
        length = ctypes.c_ushort.from_buffer(buf).value
        address = ctypes.c_void_p.from_buffer(buf, 8).value
        if not address or not length:
            return None
        found = TRACKER_RE.search(ctypes.wstring_at(address, length // 2))
        return found[1] if found else None
    finally:
        _kernel32.CloseHandle(handle)


TRACKER_RE = re.compile(r"browsertrackerid:(\d+)")
SHARE_KEEP = 600  # seconds a private server link's lookup is remembered
_share_lookups = {}  # share code -> (when looked up, place ID, link code)
_share_lock = threading.Lock()


def _resolve_share_once(session, code):
    """resolve_share, remembered for a while: every account launching into the same server
    needs the same answer. One at a time, so launches starting together ask only once."""
    with _share_lock:
        known = _share_lookups.get(code)
        if known and time.monotonic() - known[0] < SHARE_KEEP:
            return known[1], known[2]
        place_id, link_code = resolve_share(session, code)
        _share_lookups[code] = (time.monotonic(), place_id, link_code)
        return place_id, link_code


def join_uri(cookie, link, progress=lambda text: None, opener=None, tracker=None):
    """Everything a launch asks Roblox's website for (the server behind a share link, and a
    join ticket), ready to start: can be done while an old window is still closing."""
    target = parse_server_link(link)
    session = RobloxSession(cookie, opener)
    if "share" in target:
        progress("Reading the private server link…")
        place_id, link_code = _resolve_share_once(session, target["share"])
    else:
        place_id, link_code = target["place_id"], target["link_code"]
    progress("Getting a join ticket…")
    return launch_uri(auth_ticket(session), place_id, link_code, tracker)


def launch(cookie, link, progress=lambda text: None, opener=None, start=None, tracker=None,
           uri=None):
    """Start Roblox as this account, joining the server the link points at. `tracker` is this
    launch's number (see new_tracker); `uri` is a join_uri made for it beforehand."""
    if uri is None:
        uri = join_uri(cookie, link, progress, opener, tracker)
    progress("Starting Roblox…")
    try:
        (start or os.startfile)(uri)
    except OSError as e:
        raise AccountError(f"Windows couldn't start Roblox ({e.strerror or e}). Is Roblox "
                           "installed?") from None


def fetch_avatar(user_id, path, opener=None):
    """Save the account's avatar headshot as a PNG; False if Roblox has none to give."""
    get = opener or urllib.request.urlopen
    url = (f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={user_id}"
           "&size=150x150&format=Png&isCircular=false")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with get(request, timeout=20) as response:
            data = json.loads(response.read())
        image_url = (data.get("data") or [{}])[0].get("imageUrl")
        if not image_url:
            return False
        with get(urllib.request.Request(image_url, headers={"User-Agent": USER_AGENT}),
                 timeout=20) as response:
            png = response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return True


# --------------------------------------------------------------------------- #
# Saved accounts
# --------------------------------------------------------------------------- #

class AccountStore:
    """accounts.json: {'accounts': [{'id', 'name', 'display', 'cookie' (encrypted), 'server',
    'watch', 'added'}], 'servers': [{'id', 'name', 'link'}]}. Each account joins one of the
    saved private servers ('server' is its ID). Only used from the UI thread."""

    def __init__(self, path):
        self.path = Path(path)
        self.avatar_dir = self.path.parent / "avatars"
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.accounts = [a for a in data.get("accounts", []) if "id" in a and "cookie" in a]
            self.servers = [s for s in data.get("servers", []) if "id" in s and "link" in s]
        except (OSError, ValueError, AttributeError):
            self.accounts, self.servers = [], []
        # Accounts used to keep a link each: turn those links into saved servers.
        moved = False
        for account in self.accounts:
            link = account.pop("link", None)
            account.setdefault("server", None)
            if link is None:
                continue
            moved = True
            if link.strip() and not account.get("server"):
                server = next((s for s in self.servers if s["link"] == link.strip()), None)
                server = server or self.add_server("", link.strip(), save=False)
                account["server"] = server["id"]
        if moved:
            self.save()

    def save(self):
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps({"accounts": self.accounts, "servers": self.servers}, indent=1),
                        encoding="utf-8")
        os.replace(temp, self.path)

    def get(self, user_id):
        return next((a for a in self.accounts if a["id"] == user_id), None)

    def add(self, info, cookie):
        """Save a checked sign-in ({'id', 'name', 'display'} from who_is). Signing in to an
        account that's already there just renews its sign-in. Returns (account, new)."""
        account = self.get(info["id"])
        new = account is None
        if new:
            account = {"id": info["id"], "server": None, "added": time.strftime("%Y-%m-%d %H:%M")}
            self.accounts.append(account)
        account.update(name=info["name"], display=info["display"], cookie=protect(cookie))
        self.save()
        return account, new

    def cookie(self, account):
        return unprotect(account["cookie"])

    # Saved private servers

    def server(self, server_id):
        return next((s for s in self.servers if s["id"] == server_id), None)

    def add_server(self, name, link, save=True):
        """Save a private server; an empty name becomes "Server N". Returns it."""
        server_id = max((s["id"] for s in self.servers), default=0) + 1
        server = {"id": server_id, "name": name.strip() or f"Server {server_id}", "link": link.strip()}
        self.servers.append(server)
        if save:
            self.save()
        return server

    def rename_server(self, server_id, name):
        server = self.server(server_id)
        if server is not None and name.strip() and server["name"] != name.strip():
            server["name"] = name.strip()
            self.save()

    def remove_server(self, server_id):
        """Forget a server; accounts that joined it are left without one."""
        self.servers = [s for s in self.servers if s["id"] != server_id]
        for account in self.accounts:
            if account.get("server") == server_id:
                account["server"] = None
        self.save()

    def set_server(self, user_id, server_id):
        account = self.get(user_id)
        if account is not None and account.get("server") != server_id:
            account["server"] = server_id
            self.save()

    def server_of(self, account):
        """The saved server an account joins, or None."""
        return self.server(account.get("server"))

    def set_watch(self, user_id, on):
        """Whether the app starts watching this account's Roblox window once it's launched."""
        account = self.get(user_id)
        if account is not None and account.get("watch", True) != on:
            account["watch"] = on
            self.save()

    def set_ticked(self, user_id, on):
        """Whether the account is ticked for Launch selected."""
        account = self.get(user_id)
        if account is not None and bool(account.get("ticked")) != on:
            account["ticked"] = on
            self.save()

    def remove(self, user_id):
        self.accounts = [a for a in self.accounts if a["id"] != user_id]
        self.save()
        try:
            self.avatar_path(user_id).unlink()
        except OSError:
            pass

    def avatar_path(self, user_id):
        return self.avatar_dir / f"{user_id}.png"


# --------------------------------------------------------------------------- #
# More than one Roblox window
# --------------------------------------------------------------------------- #

_kernel32.CreateMutexW.restype = wt.HANDLE
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
_kernel32.OpenMutexW.restype = wt.HANDLE
_kernel32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
_kernel32.OpenEventW.restype = wt.HANDLE
_kernel32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
_kernel32.OpenProcess.restype = wt.HANDLE
_kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG,
                                             ctypes.POINTER(wt.ULONG)]
_lock = threading.Lock()
_app_open = None  # handle whose name tells the keeper this app is open
SYNCHRONIZE = 0x00100000
KEEPER_NAME = "HallowsEve_RobloxLockKeeper"
KEEPER_OWNS_NAME = "HallowsEve_RobloxLockOwned"
APP_OPEN_NAME = "HallowsEve_DetectorOpen"
KEEPER_WAIT = 15  # seconds a just-started keeper gets to take the lock
# Roblox clients watch the lock all the time. If whoever holds it goes away, or it's pulled out
# from under them, they take it that a newer Roblox has started and close themselves. So it's held
# by this little background process, which outlives the app and is never taken from anyone: it goes
# once the app and all Roblox windows have been gone for a while.
KEEPER_SCRIPT = f"""
$ErrorActionPreference = 'SilentlyContinue'
$single = New-Object Threading.Mutex($false, '{KEEPER_NAME}')
try {{ if (-not $single.WaitOne(0)) {{ exit }} }} catch [Threading.AbandonedMutexException] {{ }}
$owns = New-Object Threading.EventWaitHandle($false, 'ManualReset', '{KEEPER_OWNS_NAME}')
$have = $false
$lock = New-Object Threading.Mutex($true, 'ROBLOX_singletonMutex', [ref]$have)
$event = New-Object Threading.EventWaitHandle($false, 'ManualReset', 'ROBLOX_singletonEvent')
$idle = 0
while ($true) {{
  if (-not $have) {{ try {{ $have = $lock.WaitOne(0) }} catch [Threading.AbandonedMutexException] {{ $have = $true }} }}
  if ($have) {{ [void]$owns.Set() }}
  Start-Sleep -Seconds 3
  $app = $false
  try {{ [Threading.Mutex]::OpenExisting('{APP_OPEN_NAME}').Close(); $app = $true }} catch {{ }}
  $windows = @(Get-Process RobloxPlayerBeta | Where-Object {{ $_.MainWindowHandle -ne 0 }}).Count -gt 0
  if ($app -or $windows) {{ $idle = 0 }} else {{ $idle += 1; if ($idle -ge 5) {{ break }} }}
}}
if ($have) {{ $lock.ReleaseMutex() }}
"""


def _named_object_open(opener, name):
    handle = opener(SYNCHRONIZE, False, name)
    if handle:
        _kernel32.CloseHandle(handle)
    return bool(handle)


def _keeper_owns_lock():
    handle = _kernel32.OpenEventW(SYNCHRONIZE, False, KEEPER_OWNS_NAME)
    if not handle:
        return False
    try:
        return _kernel32.WaitForSingleObject(handle, 0) == 0
    finally:
        _kernel32.CloseHandle(handle)


def allow_multiple_roblox(wait=KEEPER_WAIT):
    """Make sure the keeper holds Roblox's one-window lock, so every Roblox opened from now on
    stays open next to the others. True once it does. False while something else holds it, like
    another multi-Roblox tool or a Roblox opened first; that's never taken from them, as it would
    close those windows. The keeper takes the lock as soon as it's free. Safe to call again."""
    global _app_open
    with _lock:
        if _app_open is None:
            _app_open = _kernel32.CreateMutexW(None, False, APP_OPEN_NAME)
        if not _named_object_open(_kernel32.OpenMutexW, KEEPER_NAME):
            try:
                subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", KEEPER_SCRIPT],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True,
                                 creationflags=0x08000000 | 0x00000200)  # no window, own group
            except OSError:
                return False
            end = time.monotonic() + wait
            while time.monotonic() < end and not _keeper_owns_lock():
                time.sleep(0.1)
    return _keeper_owns_lock()


def close_roblox(pid, hwnds, timeout=15):
    """Close a Roblox client: ask its windows to close (like clicking X), and end the
    process if it's still there after `timeout` seconds. True once it's gone."""
    _user32 = ctypes.WinDLL("user32")
    _user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    process = _kernel32.OpenProcess(0x00100000 | 0x0001, False, pid)  # SYNCHRONIZE | TERMINATE
    if not process:
        return True  # already gone (or not ours to close)
    try:
        for hwnd in hwnds:
            _user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        if _kernel32.WaitForSingleObject(process, int(timeout * 1000)) == 0:
            return True
        _kernel32.TerminateProcess(process, 0)
        return _kernel32.WaitForSingleObject(process, 5000) == 0
    finally:
        _kernel32.CloseHandle(process)


_kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_kernel32.WaitForSingleObject.restype = wt.DWORD
_kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]


# --------------------------------------------------------------------------- #
# Signing in through a browser window
# --------------------------------------------------------------------------- #

def find_browser():
    """Edge (on every Windows 10/11 PC) or else Chrome; None if neither is installed."""
    roots = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for relative in (r"Microsoft\Edge\Application\msedge.exe",
                     r"Google\Chrome\Application\chrome.exe"):
        for root in roots:
            if root and (Path(root) / relative).exists():
                return Path(root) / relative
    return None


class _WebSocket:
    """Just enough of a WebSocket client to talk to a local browser's DevTools port."""

    def __init__(self, port, path, timeout=5):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("the browser closed the connection")
            reply += chunk
        head, self.buffer = reply.split(b"\r\n\r\n", 1)
        if b" 101" not in head.split(b"\r\n")[0]:
            raise OSError("the browser refused the connection")
        self.next_id = 0

    def _read(self, n):
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("the browser closed the connection")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def send(self, text):
        data, mask = text.encode(), os.urandom(4)
        header = bytearray([0x81])
        if len(data) < 126:
            header.append(0x80 | len(data))
        elif len(data) < 65536:
            header += bytes([0x80 | 126]) + len(data).to_bytes(2, "big")
        else:
            header += bytes([0x80 | 127]) + len(data).to_bytes(8, "big")
        self.sock.sendall(bytes(header) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def receive(self):
        message = b""
        while True:
            first, second = self._read(2)
            size = second & 0x7F
            if size == 126:
                size = int.from_bytes(self._read(2), "big")
            elif size == 127:
                size = int.from_bytes(self._read(8), "big")
            mask = self._read(4) if second & 0x80 else None
            payload = self._read(size)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            opcode = first & 0x0F
            if opcode == 8:
                raise OSError("the browser closed the connection")
            if opcode in (0, 1, 2):
                message += payload
                if first & 0x80:
                    return message.decode("utf-8", "replace")

    def call(self, method, params=None):
        self.next_id += 1
        self.send(json.dumps({"id": self.next_id, "method": method, "params": params or {}}))
        while True:
            reply = json.loads(self.receive())
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise OSError(reply["error"].get("message", "DevTools error"))
                return reply.get("result", {})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class BrowserSignIn:
    """A small browser window on the Roblox login page, with a fresh profile of its own (so
    each sign-in can be a different account). wait() returns the account's cookie once
    Roblox has set it and it checks out, or None if the window was closed first."""

    POLL = 1.0
    OPEN_WAIT = 30  # seconds for the browser to open its DevTools port

    def __init__(self, url=LOGIN_URL, check=None, browser=None, extra_args=()):
        self.url = url
        self.check = check or (lambda cookie: who_is(cookie))  # raises if it doesn't sign in
        self.browser = browser or find_browser()
        self.extra_args = list(extra_args)
        self.profile = None
        self.process = None
        self.socket = None
        self.info = None  # what check() said about the signed-in account
        self.cancelled = threading.Event()

    def start(self):
        if self.browser is None:
            raise AccountError("Signing in needs Microsoft Edge or Google Chrome. Add the "
                               "account with its cookie instead.")
        self.profile = Path(tempfile.mkdtemp(prefix="hed-signin-"))
        args = [str(self.browser), f"--user-data-dir={self.profile}", "--remote-debugging-port=0",
                "--no-first-run", "--no-default-browser-check", "--disable-sync",
                "--window-size=520,780", *self.extra_args, f"--app={self.url}"]
        self.process = subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _connect(self):
        port_file = self.profile / "DevToolsActivePort"
        try:
            port, path = port_file.read_text().split("\n")[:2]
            return _WebSocket(int(port), path.strip())
        except (OSError, ValueError):
            return None

    def wait(self, timeout=None):
        """Cookie of the signed-in account, or None (window closed, cancelled or timed out)."""
        # Edge often restarts itself on a new profile, so the process it was started as says
        # nothing; the window counts as open while its DevTools port answers.
        started = time.monotonic()
        deadline = started + timeout if timeout else None
        last_rejected = None
        connected, misses = False, 0
        try:
            while not self.cancelled.is_set():
                if deadline and time.monotonic() > deadline:
                    return None
                if self.socket is None:
                    self.socket = self._connect()
                    if self.socket is not None:
                        connected, misses = True, 0
                    elif connected:
                        misses += 1
                        if misses >= 3:  # it answered before and doesn't now: closed
                            return None
                    elif time.monotonic() - started > self.OPEN_WAIT:
                        raise AccountError("The browser window didn't open.")
                if self.socket is not None:
                    try:
                        cookies = self.socket.call("Storage.getCookies").get("cookies", [])
                    except (OSError, ValueError):
                        self.socket.close()
                        self.socket = None
                        cookies = []
                    for cookie in cookies:
                        value = cookie.get("value", "")
                        if (cookie.get("name") == COOKIE_NAME and value and value != last_rejected
                                and cookie.get("domain", "").endswith("roblox.com")):
                            try:
                                self.info = self.check(value)
                                return value
                            except AccountError:
                                last_rejected = value  # not signed in yet; keep waiting
                self.cancelled.wait(self.POLL)
            return None
        finally:
            self.close()

    def cancel(self):
        self.cancelled.set()

    def close(self):
        """Shut the browser window and delete its profile (with the cookie in it)."""
        if self.socket is None and self.profile is not None:
            self.socket = self._connect()
        if self.socket is not None:
            try:
                self.socket.call("Browser.close")
            except (OSError, ValueError):
                pass
            self.socket.close()
            self.socket = None
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.profile is not None:
            for _ in range(20):  # the browser lets go of its files a moment after closing
                shutil.rmtree(self.profile, ignore_errors=True)
                if not self.profile.exists():
                    break
                time.sleep(0.25)
            self.profile = None
