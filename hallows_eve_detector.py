"""
Hallows' Eve detector for Roblox.

Captures one or more Roblox windows and watches the top-middle event banner for
the "Hallows' Eve" title, reading the "Event Duration: m:ss" line under it too.

What the screenshots have in common (and what detection relies on):
  * The title is horizontally centred, directly under the SHOPS / PLOT / FISHING
    buttons, roughly 5-9 % of the window height from the top.
  * Under it are two small description lines, then a green "Event Duration: m:ss".
  * Everything sits in the top ~15 % and middle ~40 % of the window, so only that
    band is OCR'd; the "Got 2x ..." catch popups below it are cut off.
  * OCR reads the apostrophe/space loosely ("Hallows'Eve", "Hallows' Eve"), so the
    match ignores punctuation and spacing and tolerates a letter or two being wrong.

Windows are captured with Windows Graphics Capture (the windows-capture
package), so they can sit behind other windows or on any monitor. Minimised
windows can't be captured. If Graphics Capture can't start, the window's area is
grabbed off the screen instead, which needs the top of the window visible.

Usage:
    python hallows_eve_detector.py               # pick windows in a GUI, then monitor
    python hallows_eve_detector.py --all         # monitor every Roblox window, console only
    python hallows_eve_detector.py --list        # list Roblox windows and exit
    python hallows_eve_detector.py --test a.png  # run detection on screenshots
    python hallows_eve_detector.py --discord-test  # check the Discord setup

Discord: when the event starts, the Roblox player-count bot posts in #event-ping
tagging the @ps-ping role with each window's private server link (ping_role in
discord_config.json, created on first run; put the bot's token in it, or set
DISCORD_TOKEN). One ping per event, even when several windows show it.
"""

import argparse
import atexit
import csv
import ctypes
import ctypes.wintypes as wt
import difflib
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import mss
import numpy as np
from PIL import Image

if getattr(sys, "frozen", False):
    # The exe keeps its settings and logs in AppData, so the exe itself can sit anywhere
    # (Desktop, Downloads) and be replaced by an update.
    APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Hallows Eve Detector"
    APP_DIR.mkdir(parents=True, exist_ok=True)
else:
    APP_DIR = Path(__file__).parent
DEBUG_DIR = APP_DIR / "debug_captures"
ROBLOX_EXE = "robloxplayerbeta.exe"
TARGET = "hallowseve"
# Search band, as fractions of the window (left, top, right, bottom).
BAND = (0.30, 0.0, 0.70, 0.20)
DURATION_RE = re.compile(r"(\d{1,2}):(\d{2})")

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32


def make_dpi_aware():
    # Otherwise window sizes come back scaled down on high-DPI displays.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def open_screen():
    # mss 10 renamed mss.mss to mss.MSS; support both.
    return getattr(mss, "MSS", mss.mss)()


# --------------------------------------------------------------------------- #
# Finding & capturing Roblox windows
# --------------------------------------------------------------------------- #

def process_name(pid):
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def client_size(hwnd):
    rect = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    return rect.right, rect.bottom


def find_roblox_windows():
    """Visible top-level windows belonging to the Roblox player: [(hwnd, pid, title)]."""
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if process_name(pid.value) == ROBLOX_EXE:
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            w, h = client_size(hwnd)
            if w > 100 and h > 100:  # skip helper/tool windows
                found.append((hwnd, pid.value, title.value))
        return True

    user32.EnumWindows(callback, 0)
    # EnumWindows lists by z-order, which changes as windows are clicked; sorting by
    # process ID keeps "Window 1" and "Window 2" the same windows between scans.
    return sorted(found, key=lambda w: w[1])


user32.GetWindow.restype = wt.HWND
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
psapi = ctypes.windll.psapi
psapi.EmptyWorkingSet.argtypes = [wt.HANDLE]


def trim_roblox_memory():
    """The F6 hotkey of 'F6 ram dump.ahk': for every visible Roblox window, empty its
    process's working set, so Windows pages out the memory it isn't using right now.

    Returns (windows found, windows whose process could be trimmed).
    """
    pids = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if process_name(pid.value) == ROBLOX_EXE:
                pids.append(pid.value)
        return True

    user32.EnumWindows(callback, 0)
    trimmed = 0
    for pid in pids:
        handle = kernel32.OpenProcess(0x1F0FFF, False, pid)  # PROCESS_ALL_ACCESS, as the script
        if handle:
            if psapi.EmptyWorkingSet(handle):
                trimmed += 1
            kernel32.CloseHandle(handle)
    return len(pids), trimmed


class WindowStream:
    """Keeps the latest frame of one window using Windows Graphics Capture.

    Unlike grabbing the screen, this works while the window is behind other
    windows (but not minimised: Windows stops delivering frames then).
    Frames arrive on the library's own thread, at most every `interval_ms`.
    """

    STALE_AFTER = 5.0  # seconds without a new frame before the window counts as unseen

    def __init__(self, hwnd, interval_ms=250):
        from windows_capture import WindowsCapture
        self.hwnd = hwnd
        self.frame = None  # BGRA numpy array of the whole window, title bar included
        self.received = 0.0
        self.closed = False
        self._lock = threading.Lock()
        try:
            capture = WindowsCapture(cursor_capture=False, draw_border=False,
                                     minimum_update_interval=interval_ms, window_hwnd=hwnd)
        except Exception:
            # Hiding the yellow capture border needs Windows 11.
            capture = WindowsCapture(cursor_capture=False,
                                     minimum_update_interval=interval_ms, window_hwnd=hwnd)
        capture.event(self.on_frame_arrived)
        capture.event(self.on_closed)
        self.control = capture.start_free_threaded()

    def on_frame_arrived(self, frame, _control):
        bgra = frame.frame_buffer.copy()  # the buffer is reused once this returns
        with self._lock:
            self.frame, self.received = bgra, time.monotonic()

    def on_closed(self):
        self.closed = True

    def latest(self):
        """Client area of the newest frame as an RGB image, or None if there's no fresh one."""
        with self._lock:
            frame, received = self.frame, self.received
        if frame is None or self.closed or time.monotonic() - received > self.STALE_AFTER:
            return None
        # The frame covers the window's visible bounds; cut it down to the client
        # area so banner positions match a plain screenshot of the game.
        bounds = wt.RECT()
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wt.HWND(self.hwnd), 9, ctypes.byref(bounds),
                                                   ctypes.sizeof(bounds))  # EXTENDED_FRAME_BOUNDS
        origin = wt.POINT(0, 0)
        user32.ClientToScreen(self.hwnd, ctypes.byref(origin))
        w, h = client_size(self.hwnd)
        x, y = max(0, origin.x - bounds.left), max(0, origin.y - bounds.top)
        client = frame[y:y + h, x:x + w, 2::-1]  # BGRA -> RGB
        if client.size == 0:
            return None
        return Image.fromarray(np.ascontiguousarray(client))


# windows-capture crashes the whole process (access violation in
# start_free_threaded) when a capture starts after every earlier capture was
# stopped and OCR has run in between. So captures are never stopped: each
# window gets one shared stream for the life of the program, and a slow capture
# of the main monitor keeps a session alive even if every Roblox window closes.
_streams = {}  # hwnd -> WindowStream, or None if Graphics Capture failed for it
_streams_lock = threading.Lock()
_keepalive = None


def get_stream(hwnd):
    """The shared WindowStream for a window, or None if Graphics Capture isn't available."""
    global _keepalive
    with _streams_lock:
        if _keepalive is None:
            _keepalive = _start_keepalive()
        stream = _streams.get(hwnd, False)
        if stream is False or (stream and stream.closed and user32.IsWindow(hwnd)):
            try:
                stream = WindowStream(hwnd)
            except Exception as e:
                print(f"Graphics Capture unavailable for window {hwnd} ({e}); "
                      "falling back to screen capture, so keep it visible.", flush=True)
                stream = None
            _streams[hwnd] = stream
        return stream


def _start_keepalive():
    try:
        from windows_capture import WindowsCapture
        capture = WindowsCapture(cursor_capture=False, draw_border=False,
                                 minimum_update_interval=5000, monitor_index=1)
        # event() picks the slot by function name, so set the no-op handlers directly.
        capture.frame_handler = lambda frame, control: None
        capture.closed_handler = lambda: None
        return capture.start_free_threaded()
    except Exception:
        return False  # no Graphics Capture at all; window streams fall back too


def snapshot(hwnd, sct, timeout=1.5):
    """The newest frame of a window (for thumbnails), or None."""
    stream = get_stream(hwnd)
    if stream is None:
        return grab_from_screen(hwnd, sct)
    deadline = time.monotonic() + timeout
    while (img := stream.latest()) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    return img


def grab_from_screen(hwnd, sct):
    """Fallback capture: the window's client area as it appears on screen.

    None if minimised, closed, or if another window covers the banner area.
    """
    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return None
    w, h = client_size(hwnd)
    origin = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    band = (origin.x + int(w * BAND[0]), origin.y + int(h * BAND[1]),
            origin.x + int(w * BAND[2]), origin.y + int(h * BAND[3]))
    if covered(hwnd, band):
        return None
    shot = sct.grab({"left": origin.x, "top": origin.y, "width": w, "height": h})
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def covered(hwnd, area):
    """True if a window above `hwnd` overlaps `area` (screen coords: left, top, right, bottom).

    WindowFromPoint isn't reliable for this: it can report Roblox while another
    app is drawn over it.
    """
    above = user32.GetWindow(hwnd, 3)  # GW_HWNDPREV: next window up the z-order
    while above:
        if user32.IsWindowVisible(above) and not user32.IsIconic(above) and not cloaked(above):
            exstyle = user32.GetWindowLongW(above, -20)  # GWL_EXSTYLE
            r = wt.RECT()
            user32.GetWindowRect(above, ctypes.byref(r))
            overlaps = (r.left < area[2] and r.right > area[0]
                        and r.top < area[3] and r.bottom > area[1])
            # Click-through overlays (WS_EX_TRANSPARENT) usually draw nothing there.
            if overlaps and not exstyle & 0x20:
                return True
        above = user32.GetWindow(above, 3)
    return False


def cloaked(hwnd):
    """Windows on other virtual desktops or suspended UWP apps are 'cloaked': invisible."""
    value = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), 14, ctypes.byref(value),
                                               ctypes.sizeof(value))  # DWMWA_CLOAKED
    return value.value != 0


# --------------------------------------------------------------------------- #
# Hiding windows: parked at the screen edge, where they're still captured
# --------------------------------------------------------------------------- #

# Windows stops drawing a window that is entirely off-screen (and minimised ones),
# so a "hidden" window keeps a thin strip on the rightmost monitor and sits behind
# every other window. Positions are saved, so a crash can't strand a window.
HIDDEN_PATH = APP_DIR / "hidden_windows.json"
PEEK = 8  # pixels of the window left on screen
_hidden = {}  # hwnd -> {"rect": [left, top, right, bottom], "maximized": bool}
_hidden_lock = threading.Lock()

user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010
HWND_BOTTOM = wt.HWND(1)


def is_hidden(hwnd):
    return hwnd in _hidden


def hide_window(hwnd):
    """Park a window at the right edge of the rightmost monitor, behind everything."""
    if not user32.IsWindow(hwnd) or hwnd in _hidden:
        return
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    maximized = bool(user32.IsZoomed(hwnd))
    with _hidden_lock:
        _hidden[hwnd] = {"rect": [rect.left, rect.top, rect.right, rect.bottom],
                         "maximized": maximized, "pid": window_pid(hwnd)}
        _save_hidden()
    if maximized or user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE: un-maximise/un-minimise, no focus
    # The visible frame starts a few pixels inside GetWindowRect (an invisible
    # resize border), so measure it to leave exactly PEEK real pixels showing.
    now = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(now))
    bounds = wt.RECT()
    ctypes.windll.dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), 9, ctypes.byref(bounds),
                                               ctypes.sizeof(bounds))  # EXTENDED_FRAME_BOUNDS
    border = max(0, bounds.left - now.left)
    with open_screen() as sct:
        mon = max(sct.monitors[1:], key=lambda m: m["left"] + m["width"])
    x = mon["left"] + mon["width"] - PEEK - border
    y = mon["top"] + mon["height"] // 4
    # Same size as before hiding, so the banner reads the same.
    user32.SetWindowPos(hwnd, HWND_BOTTOM, x, y, rect.right - rect.left, rect.bottom - rect.top,
                        SWP_NOACTIVATE)


def show_window(hwnd, activate=True):
    """Put a hidden window back where it was."""
    with _hidden_lock:
        saved = _hidden.pop(hwnd, None)
        _save_hidden()
    if not saved or not user32.IsWindow(hwnd):
        return
    left, top, right, bottom = saved["rect"]
    user32.SetWindowPos(hwnd, None, left, top, right - left, bottom - top,
                        SWP_NOZORDER | SWP_NOACTIVATE)
    if saved["maximized"]:
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
    if activate:
        user32.SetForegroundWindow(hwnd)


def show_all_hidden(activate=False):
    for hwnd in list(_hidden):
        show_window(hwnd, activate)


def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def restore_from_last_run():
    """Bring back windows a previous run left hidden (e.g. if it crashed).

    Window handles get reused, so a saved one only counts if it still belongs
    to the same Roblox process.
    """
    try:
        saved = json.loads(HIDDEN_PATH.read_text())
    except (OSError, ValueError):
        return
    with _hidden_lock:
        for hwnd, info in saved.items():
            hwnd = int(hwnd)
            if user32.IsWindow(hwnd) and window_pid(hwnd) == info.get("pid"):
                _hidden[hwnd] = info
        _save_hidden()
    show_all_hidden()


def _save_hidden():
    try:
        if _hidden:
            HIDDEN_PATH.write_text(json.dumps({str(h): v for h, v in _hidden.items()}))
        else:
            HIDDEN_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Hotbar: equip a character while the event is on
# --------------------------------------------------------------------------- #

# Slot 10 is the '0' key. Scan codes are what a real keyboard sends for '1'..'0'.
SLOT_KEYS = {slot: (0x30 + slot % 10, 0x01 + slot) for slot in range(1, 11)}  # slot -> (vk, scan)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    # The union has to include MOUSEINPUT, the largest member, or SendInput rejects the size.
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetForegroundWindow.restype = wt.HWND
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
_input_lock = threading.Lock()  # one key press at a time, across all windows


def focus_window(hwnd, timeout=0.5):
    """Make `hwnd` the foreground window. Returns True once it is.

    Windows refuses SetForegroundWindow from background apps, unless the calling
    thread shares input with the current foreground window, so attach to it first.
    """
    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return False
    current = user32.GetForegroundWindow()
    if current == hwnd:
        return True
    ours = kernel32.GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(current, None) if current else 0
    attached = bool(theirs and theirs != ours and user32.AttachThreadInput(ours, theirs, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, theirs, False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.02)
    return False


def press_hotbar(hwnd, slot):
    """Press the hotbar key for `slot` (1-10, 10 = the '0' key) in a Roblox window.

    Roblox only reads the keyboard while focused, so the window is focused for a
    moment and focus then goes back to whatever had it. Raises RuntimeError if
    the window couldn't be focused; the key is never sent anywhere else.
    """
    vk, scan = SLOT_KEYS[slot]
    with _input_lock:
        previous = user32.GetForegroundWindow()
        if not focus_window(hwnd):
            raise RuntimeError("couldn't bring the window to the front (minimised or closed?)")
        try:
            time.sleep(0.08)  # let Roblox notice it has focus before the key arrives
            for flags in (0, 0x0002):  # key down, then KEYEVENTF_KEYUP
                if user32.GetForegroundWindow() != hwnd:
                    raise RuntimeError("lost focus before the key could be pressed")
                event = INPUT(type=1)  # INPUT_KEYBOARD
                event.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags)
                user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
                time.sleep(0.05)
        finally:
            if previous and previous != hwnd and user32.IsWindow(previous):
                focus_window(previous)
            if is_hidden(hwnd):  # focusing raised it; tuck it back behind everything
                user32.SetWindowPos(hwnd, HWND_BOTTOM, 0, 0, 0, 0,
                                    SWP_NOSIZE | 0x0002 | SWP_NOACTIVATE)  # 0x2 = SWP_NOMOVE


class HotbarEquipper:
    """Equips a character in each window while its event is on, and unequips after.

    Each window can have several chosen slots. The first chosen one whose character
    doesn't have Dracula yet is held; when it gets Dracula, it's swapped for the
    next one. Roblox hotbar keys toggle, so a slot's key is pressed again to
    unequip it. When the banner's countdown was read, the event is over as soon as
    the countdown has run out and the banner is gone. Otherwise it counts as over
    only after END_GRACE seconds without the banner, so a few checks where OCR
    misses it mid-event don't unequip and re-equip.

    Every check also says which character the hotbar shows as held (`held`). In a
    window with slots chosen to auto equip, a character held with no event is
    unequipped straight away, whoever equipped it.
    """

    END_GRACE = 5.0
    RETRY_AFTER = 15.0  # after a failed press, so a stuck window doesn't steal focus every second
    SETTLE = 2.0  # after a key press, the hotbar may not show it yet: trust it again after this
    UNSEEN = object()  # `held` when the hotbar wasn't seen this check

    def __init__(self, press=press_hotbar, on_action=None):
        self.press = press
        self.on_action = on_action or (lambda ok, text: print(text, flush=True))
        self.slots = {}  # hwnd -> chosen slots (1-10), in priority order (the order picked)
        self.mutated = {}  # hwnd -> slots last seen with Dracula
        self.equipped = {}  # hwnd -> slot that was pressed to equip
        self.last_active = {}
        self.ends_at = {}  # hwnd -> when the banner's countdown runs out, from the last reading
        self.failed_at = {}
        self.pressed_at = {}
        self.all_mutated_logged = set()
        self._lock = threading.Lock()

    def set_slots(self, hwnd, slots):
        with self._lock:
            self.slots[hwnd] = list(dict.fromkeys(slots or []))

    def observe(self, hwnd, label, active, mutated=None, seconds_left=None, held=UNSEEN):
        """Call after every check of a window (active is None if it couldn't be seen).

        `mutated` is the set of slots showing Dracula, when it was read this
        check; otherwise the last reading is kept. `seconds_left` is the banner's
        countdown, when it was read. `held` is the slot of the character the hotbar
        shows as held (None if none is), or UNSEEN if the hotbar wasn't seen.
        """
        now = time.monotonic()
        if active:
            self.last_active[hwnd] = now
            if seconds_left is not None:
                self.ends_at[hwnd] = now + seconds_left
            else:  # countdown not read this time: don't trust an older reading
                self.ends_at.pop(hwnd, None)
        with self._lock:
            if mutated is not None:
                self.mutated[hwnd] = set(mutated)
        if active is None:
            return  # can't tell; keep things as they are
        with self._lock:
            event_on = bool(active) or (
                now - self.last_active.get(hwnd, -self.END_GRACE) < self.END_GRACE
                # banner gone with its countdown (nearly) out: it ended, no need to wait
                and now < self.ends_at.get(hwnd, float("inf")) - 0.5)
            chosen = self.slots.get(hwnd, [])
            done = self.mutated.get(hwnd, set())
            fresh = [slot for slot in chosen if slot not in done]
            want = fresh[0] if event_on and fresh else None
            have = self.equipped.get(hwnd)
            all_mutated = bool(event_on and chosen and not fresh)
        if (held is not self.UNSEEN and chosen and not event_on
                and now - self.pressed_at.get(hwnd, -self.SETTLE) >= self.SETTLE
                and now - self.failed_at.get(hwnd, -self.RETRY_AFTER) >= self.RETRY_AFTER):
            # The hotbar shows what's really held. A slot it doesn't show isn't held, so
            # don't press it (that would equip it); one held by hand gets unequipped.
            if have is not None and have != held:
                with self._lock:
                    self.equipped.pop(hwnd, None)
                have = None
            if held is not None and have is None:
                self._press(hwnd, label, held, "unequip", "held with no event")
                return
        self._switch(hwnd, label, want, have, event_on, done, now)
        if all_mutated and hwnd not in self.all_mutated_logged:
            self.all_mutated_logged.add(hwnd)
            self.on_action(True, f"{label}: every chosen character has Dracula now; "
                                 "nothing left to equip")
        elif not all_mutated:
            self.all_mutated_logged.discard(hwnd)

    def _switch(self, hwnd, label, want, have, event_on, done, now):
        """Press keys to go from holding `have` to holding `want` (either may be None)."""
        if want == have or now - self.failed_at.get(hwnd, -self.RETRY_AFTER) < self.RETRY_AFTER:
            return
        if have is not None:
            if not event_on:
                why = "event over"
            elif have in done:
                why = "it has Dracula now"
            else:
                why = "slot choice changed"
            if not self._press(hwnd, label, have, "unequip", why):
                return
        if want is not None:
            self._press(hwnd, label, want, "equip", "Hallows' Eve")

    def _press(self, hwnd, label, slot, what, why):
        key = slot % 10
        self.pressed_at[hwnd] = time.monotonic()
        try:
            self.press(hwnd, slot)
        except RuntimeError as e:
            self.failed_at[hwnd] = time.monotonic()
            self.on_action(False, f"{label}: couldn't press {key} to {what} ({e}); "
                                  f"retrying in {self.RETRY_AFTER:.0f} s")
            return False
        self.failed_at.pop(hwnd, None)
        with self._lock:
            if what == "equip":
                self.equipped[hwnd] = slot
            else:
                self.equipped.pop(hwnd, None)
        self.on_action(True, f"{label}: pressed {key} to {what} slot {slot} ({why})")
        return True


def find_hotbar_slots(img):
    """Boxes (x0, y0, x1, y1) of the hotbar slots in a screenshot, left to right; [] if none.

    Roblox scales the hotbar with the window and only shows filled slots, so fixed
    positions don't work. The row's height comes from the squares along the bottom
    of the window. Its spacing comes from those squares or from the repeating slot
    borders (squares found inside character cards can be unevenly placed). The row
    is always centred, so slots sit either on the middle of the window or half a
    slot off it; each spacing is tried, and the layout whose slots have the
    strongest borders all the way along wins.
    """
    import cv2

    w, h = img.size
    bx0, by0 = int(w * 0.10), int(h * 0.78)
    band = np.asarray(img.crop((bx0, by0, int(w * 0.90), h)).convert("RGB"))
    edges = cv2.Canny(cv2.cvtColor(band, cv2.COLOR_RGB2GRAY), 60, 160)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        # Square, and touching the bottom of the window like the hotbar does
        # (which rules out things like a checkered floor further up).
        if (h * 0.035 <= bh <= h * 0.16 and 0.8 < bw / bh < 1.25
                and by0 + y + bh >= h * 0.955):
            boxes.append((x, y, bw, bh))
    if not boxes:
        return []
    middle = w / 2 - bx0

    # The row: the biggest group of squares sharing a size and a bottom edge that
    # reaches the middle (the currency icons and pickups in the corners don't).
    def group_of(b):
        return [o for o in boxes if abs(o[3] - b[3]) <= b[3] * 0.15
                and abs((o[1] + o[3]) - (b[1] + b[3])) <= b[3] * 0.2]

    def reaches_middle(group):
        # Within ~3 slots of the middle: not every slot's border is picked up.
        return any(abs(o[0] + o[2] / 2 - middle) <= o[3] * 3 for o in group)
    groups = [g for g in (group_of(b) for b in boxes) if reaches_middle(g)]
    if not groups:
        return []
    group = max(groups, key=lambda g: (len(g), np.median([o[3] for o in g])))
    size = float(np.median([b[3] for b in group]))
    top = int(np.median([b[1] for b in group]))
    bottom = int(np.median([b[1] + b[3] for b in group]))

    # Spacing candidates, from gaps between the squares...
    centers = []  # one per slot: a slot's inner and outer borders give two boxes
    for c in sorted({round(b[0] + b[2] / 2) for b in group}):
        if not centers or c - centers[-1] >= size * 0.5:
            centers.append(c)
    candidates = [gap / k for gap in (b - a for a, b in zip(centers, centers[1:]))
                  for k in (1, 2, 3) if size * 0.9 <= gap / k <= size * 1.8]
    # ...and from the repeating slot borders around the middle. The slow variation
    # comes out first, or the shortest spacing always looks best.
    profile = edges[top:bottom].astype(float).mean(axis=0) / 255
    k = max(3, int(size))
    detail = profile - np.convolve(profile, np.ones(k) / k, mode="same")
    seg = detail[max(0, int(middle - size * 7)):min(len(detail), int(middle + size * 7))]
    lags = list(range(int(size * 0.8), min(int(size * 1.9), len(seg) // 2)))
    if len(lags) >= 3:
        score = [float(np.dot(seg[:-lag], seg[lag:])) / (len(seg) - lag) for lag in lags]
        peaks = [i for i in range(1, len(score) - 1)
                 if score[i] >= score[i - 1] and score[i] >= score[i + 1]]
        for i in sorted(peaks, key=lambda i: -score[i])[:3]:
            pitch = float(lags[i])
            a, b, c = score[i - 1], score[i], score[i + 1]
            if a - 2 * b + c:  # parabola through the peak, for sub-pixel spacing
                pitch += 0.5 * (a - c) / (a - 2 * b + c)
            candidates.append(pitch)

    def layout(pitch):
        """(count, border strength, centre, first, last) of the centred row at this spacing."""
        reach = max(3, int(pitch * 0.08))

        def border_at(x):
            x = int(round(x))
            if x - reach < 0 or x + reach >= len(profile):
                return 0.0
            return float(profile[x - reach:x + reach + 1].max())

        def strength(c0, j):
            """How clearly slot j has a border: both sides, and its top or bottom
            (a character's earnings text often covers the bottom)."""
            cx = c0 + j * pitch
            x0, x1 = int(cx - pitch * 0.45), int(cx + pitch * 0.45)
            if x0 < 0 or x1 >= edges.shape[1]:
                return 0.0
            rows = [max(edges[y, x0:x1].mean() for y in range(max(0, yy - 3), min(len(edges), yy + 4)))
                    for yy in (top, bottom)]
            # A slot's outer border sits just inside the gap to the next slot.
            left = max(border_at(cx - pitch / 2), border_at(cx - pitch * 0.45))
            right = max(border_at(cx + pitch / 2), border_at(cx + pitch * 0.45))
            return min(left, right, max(rows) / 255)

        # Odd number of slots: one sits on the middle. Even: the middle is a gap.
        odd, even = middle, middle + pitch / 2
        on_gaps = [sum(border_at(c + (j + 0.5) * pitch) for j in range(-4, 4)) for c in (odd, even)]
        c0 = odd if on_gaps[0] >= on_gaps[1] else even
        lo, hi = (0, 0) if c0 == odd else (-1, 0)
        if min(strength(c0, j) for j in range(lo, hi + 1)) <= 0.55:
            return None
        while hi - lo + 1 <= 8 and strength(c0, lo - 1) > 0.55 and strength(c0, hi + 1) > 0.55:
            lo, hi = lo - 1, hi + 1
        quality = float(np.mean([strength(c0, j) for j in range(lo, hi + 1)]))
        return hi - lo + 1, quality, c0, lo, hi, pitch

    results = [r for r in map(layout, candidates) if r]
    if not results:
        return []
    count, quality, c0, lo, hi, pitch = max(results, key=lambda r: r[0] * r[1])
    half = pitch * 0.45
    return [(bx0 + int(c0 + j * pitch - half), by0 + top, bx0 + int(c0 + j * pitch + half), by0 + bottom)
            for j in range(lo, hi + 1)]


def read_slot_labels(img, slots, only=None):
    """The text above each hotbar slot (item name, or a mutation tag like '[Dracula]').

    Each slot is read on its own and enlarged, so neighbouring names can't run
    together and small windows' tiny text is still legible. Takes ~0.15-0.5 s.
    With `only` (a slot, 1-10), just that one is read; the others come back ''.
    """
    if not slots:
        return []
    if len(slots) > 1:
        pitch = (slots[-1][0] - slots[0][0]) / (len(slots) - 1)
    else:
        pitch = (slots[0][2] - slots[0][0]) * 1.1
    labels = []
    for i, (x0, y0, x1, y1) in enumerate(slots, 1):
        if only is not None and i != only:
            labels.append("")
            continue
        cx = (x0 + x1) / 2
        crop = img.crop((max(0, int(cx - pitch * 0.55)), max(0, int(y0 - pitch * 0.75)),
                         min(img.width, int(cx + pitch * 0.55)), int(y0 + pitch * 0.05)))
        crop = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
        with EventDetector._lock:
            result = EventDetector.engine()(np.asarray(crop), use_det=True, use_cls=False,
                                            use_rec=True)
        labels.append(" ".join(result.txts) if result.txts else "")
    return labels


def is_dracula(text):
    """True if a slot label says Dracula (OCR may get a letter wrong: '[Dracvla]')."""
    return any(difflib.SequenceMatcher(None, word, "dracula").ratio() >= 0.75
               for word in re.findall(r"[a-z]+", text.lower()))


def hotbar_signature(img, slots):
    """Average colour of each slot's icon, to notice the hotbar changing (items moved,
    added or removed) without reading the labels again. Character cards animate, so
    finer fingerprints change all the time; their average colour barely does."""
    colours = []
    for x0, y0, x1, y1 in slots:
        inset = (x1 - x0) * 0.15
        icon = img.crop((int(x0 + inset), int(y0 + inset), int(x1 - inset), int(y1 - inset)))
        colours.append(np.asarray(icon.convert("RGB"), dtype=float).reshape(-1, 3).mean(0))
    return np.stack(colours)


def hotbar_changed(old, new):
    # Unchanged hotbars drift by up to ~35; moving a character shifts a slot by ~80.
    return old is None or old.shape != new.shape or float(np.abs(old - new).max()) > 45


def selected_slot(img, slots):
    """The hotbar slot (1-10) showing the held-item ring, or None.

    The held slot gets a thin solid ring in a pulsing blue (0, 138-175, 255). Water
    and some card frames come close to that colour, so a slot only counts when the
    ring is one flat shade along its top and both sides, the area further out isn't
    the same blue, and no other slot looks held too. Takes ~0.5 ms.
    """
    if not slots:
        return None
    pad = max(2, int((slots[0][2] - slots[0][0]) * 0.2))
    bx0, by0 = max(0, slots[0][0] - pad * 3), max(0, slots[0][1] - pad * 4)
    area = np.asarray(img.crop((bx0, by0, min(img.width, slots[-1][2] + pad * 3), img.height))
                      .convert("RGB"), dtype=np.int16)
    r, g, b = area[..., 0], area[..., 1], area[..., 2]
    blue = (r <= 15) & (b >= 240) & (g >= 100) & (g <= 210)
    height, width = blue.shape
    found = []
    for i, (x0, y0, x1, y1) in enumerate(slots, 1):
        x0, x1, y0, y1 = x0 - bx0, x1 - bx0, y0 - by0, min(y1 - by0, height)
        sides = [(slice(max(0, y0 - pad), y0 + pad), slice(x0, x1), 0),  # top
                 (slice(y0, y1), slice(max(0, x0 - pad), x0 + pad), 1),  # left
                 (slice(y0, y1), slice(x1 - pad, min(width, x1 + pad)), 1)]  # right
        shades = np.concatenate([g[ys, xs][blue[ys, xs]] for ys, xs, _ in sides])
        if shades.size < x1 - x0:
            continue
        flat = blue & (np.abs(g - int(np.median(shades))) <= 6)
        cover = [float(flat[ys, xs].any(axis=axis).mean()) for ys, xs, axis in sides]
        further = flat[max(0, y0 - pad * 4):max(0, y0 - pad * 2), x0:x1]
        if min(cover) >= 0.6 and (further.size == 0 or further.mean() < 0.3):
            found.append(i)
    return found[0] if len(found) == 1 else None


ITEM_WORDS = ("potion", "rod", "box")  # in the names above the hotbar's non-characters


def is_character_card(img, slots, slot, chosen=()):
    """True if hotbar slot `slot` (1-10) holds a character, not the fishing rod or an item.

    Items have their name above them ('Cash Potion', 'Fishing Rod'); characters'
    cards show earnings along the bottom ('$15/s'). In small windows the earnings
    are too small to read, so a mutation tag above ('[Lava]') or being one of the
    slots `chosen` for the event (always characters) counts too.
    """
    label = read_slot_labels(img, slots, only=slot)[slot - 1]
    if any(difflib.SequenceMatcher(None, word, item).ratio() >= 0.75
           for word in re.findall(r"[a-z]+", label.lower()) for item in ITEM_WORDS):
        return False
    if slot in chosen or re.search(r"\[\s*\w", label):
        return True
    x0, y0, x1, y1 = slots[slot - 1]
    crop = img.crop((x0, int(y0 + (y1 - y0) * 0.6), x1, y1))
    crop = crop.resize((crop.width * 3, crop.height * 3), Image.LANCZOS)
    with EventDetector._lock:
        result = EventDetector.engine()(np.asarray(crop), use_det=True, use_cls=False, use_rec=True)
    return bool(re.search(r"/\s*[s5S]\b|\$", " ".join(result.txts or [])))


class SteadyHotbar:
    """Only accepts a new hotbar layout once two checks in a row agree on it, so one
    odd frame (something slot-shaped flying past) can't renumber the slots."""

    def __init__(self):
        self.accepted = {}  # hwnd -> slot boxes
        self.pending = {}

    @staticmethod
    def same(a, b):
        return bool(a and b) and len(a) == len(b) and \
            abs(a[0][0] - b[0][0]) <= (a[0][2] - a[0][0]) * 0.3

    def update(self, hwnd, found):
        """`found` from this frame -> the layout to use for it (None if no hotbar)."""
        if not found:
            return None
        accepted = self.accepted.get(hwnd)
        if accepted is None or self.same(accepted, found) or self.same(self.pending.get(hwnd), found):
            self.accepted[hwnd] = found
            self.pending.pop(hwnd, None)
            return found
        self.pending[hwnd] = found  # new layout: wait for the next check to confirm it
        return accepted


def dracula_slots(img):
    """Slots (1-10) whose character has the Dracula mutation; None if no hotbar was found."""
    slots = find_hotbar_slots(img)
    if not slots:
        return None
    return {i for i, text in enumerate(read_slot_labels(img, slots), 1) if is_dracula(text)}


# --------------------------------------------------------------------------- #
# "Welcome Back~" popup: close it first, it hides the hotbar
# --------------------------------------------------------------------------- #

# "welcome" also matches the "Welcome <name>" line, which stays readable when another
# popup (like Roblox's Join Event card) covers the middle of the panel.
POPUP_WORDS = ("welcomeback", "whileyouwereoffline", "tripleyourcash", "offline", "triple", "welcome")


def red_close_buttons(img):
    """Candidate red close buttons (x0, y0, x1, y1), in the top right of the window."""
    import cv2

    w, h = img.size
    x_from, y_to = int(w * 0.5), int(h * 0.55)
    a = np.asarray(img.crop((x_from, 0, w, y_to)).convert("RGB")).astype(np.int16)
    mask = ((a[..., 0] > 170) & (a[..., 1] < 80) & (a[..., 2] < 80)).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    found = []
    for x, y, bw, bh, area in stats[1:n]:
        # A filled square-ish button (the white X inside leaves part of it unfilled)...
        if not (h * 0.012 <= bh <= h * 0.14 and 0.7 <= bw / bh <= 1.6
                and 0.4 <= area / (bw * bh) <= 0.95):
            continue
        # ...with a white cross in the middle, unlike red text and other red shapes.
        inner = a[y + bh // 4:y + bh - bh // 4, x + bw // 4:x + bw - bw // 4]
        if ((inner[..., 0] > 200) & (inner[..., 1] > 200) & (inner[..., 2] > 200)).mean() >= 0.15:
            found.append((x_from + int(x), int(y), x_from + int(x + bw), int(y + bh)))
    return found


def find_welcome_popup(img):
    """Centre (x, y) of the "Welcome Back~" popup's red X, in window pixels; None if
    the popup isn't showing. A red button alone isn't enough (the Backpack has one
    too): the panel next to it has to read like the welcome-back offer."""
    w, h = img.size
    for x0, y0, x1, y1 in red_close_buttons(img)[:3]:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        # The popup is centred, so its left edge mirrors the X across the middle.
        left = max(0, int(w - cx))
        if x0 - left < (x1 - x0) * 4:
            continue
        # From a little above the X, so the "Welcome Back~" title is included.
        panel = img.crop((left, max(0, int(y0 - (y1 - y0) * 3)), x0,
                          min(h, int(cy + (x0 - left) * 0.5))))
        if panel.width > 900:
            panel = panel.resize((900, int(panel.height * 900 / panel.width)))
        with EventDetector._lock:
            result = EventDetector.engine()(np.asarray(panel), use_det=True, use_cls=False,
                                            use_rec=True)
        text = normalize(" ".join(result.txts or []))
        if any(word in text for word in POPUP_WORDS):
            return int(cx), int(cy)
    return None


def looks_like_roblox_card(img):
    """Cheap first check for Roblox's own dark popup cards (like "Join Event"): a patch
    of their flat blue-grey (about RGB 39, 40, 47) in the middle of the window."""
    w, h = img.size
    a = np.asarray(img.crop((int(w * 0.2), int(h * 0.15), int(w * 0.8), int(h * 0.85)))
                   .convert("RGB")).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    card = (np.abs(r - 39) <= 6) & (np.abs(g - 40) <= 6) & (b - r >= 4) & (b - r <= 12)
    return float(card.mean()) > 0.03


def find_join_event(img):
    """Centre (x, y) of the × on Roblox's "Join Event" card (an event announcement with
    a Notify Me button, shown on top of everything when joining), in window pixels;
    None if it isn't showing. Only the × is located, never Notify Me."""
    w, h = img.size
    lines = ocr_region(img, (int(w * 0.1), int(h * 0.05), int(w * 0.9), int(h * 0.95)))
    texts = [normalize(t) for t, _ in lines]
    title = next((rect for (t, rect), n in zip(lines, texts) if similar(t, "joinevent")), None)
    if title is None or not any(similar(t, "notifyme") for t, _ in lines):
        return None
    # The × is the nearest light mark left of the title, on the same line.
    x0, y0, x1, y1 = (int(v) for v in title)
    height = y1 - y0
    x_from = max(0, int(x0 - height * 6))
    band = np.asarray(img.crop((x_from, y0, x0, y1)).convert("L")).astype(int)
    light = (band > 180).any(axis=0)
    x = len(light) - 1
    while x >= 0 and not light[x]:
        x -= 1
    if x < 0:
        return None
    right = x
    while x >= 0 and light[x]:
        x -= 1
    return int(x_from + (x + 1 + right) / 2), int((y0 + y1) / 2)


MOUSE_MOVE, MOUSE_LEFTDOWN, MOUSE_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSE_ABSOLUTE, MOUSE_VIRTUALDESK = 0x8000, 0x4000


def click_in_window(hwnd, x, y):
    """Left-click at (x, y) in a window's client area, like a real mouse would.

    Roblox only takes clicks while focused, so the window is brought to the front for
    a moment (and, if it was hidden at the screen edge, moved back where it was);
    afterwards the mouse cursor, focus and hiding are all put back. Raises
    RuntimeError if the window couldn't be focused or the point isn't on a screen.
    """
    with _input_lock:
        previous = user32.GetForegroundWindow()
        cursor = wt.POINT()
        user32.GetCursorPos(ctypes.byref(cursor))
        was_hidden = is_hidden(hwnd)
        if was_hidden:
            show_window(hwnd, activate=False)
            time.sleep(0.3)  # let it redraw where it can be clicked
        try:
            if not focus_window(hwnd):
                raise RuntimeError("couldn't bring the window to the front (minimised or closed?)")
            point = wt.POINT(int(x), int(y))
            user32.ClientToScreen(hwnd, ctypes.byref(point))
            left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
            width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
            if not (left <= point.x < left + width and top <= point.y < top + height):
                raise RuntimeError("the button is off-screen")
            def mouse(px, py, extra=0):
                event = INPUT(type=0)  # INPUT_MOUSE
                event.mi = MOUSEINPUT(
                    dx=int((px - left) * 65535 / max(1, width - 1)),
                    dy=int((py - top) * 65535 / max(1, height - 1)),
                    dwFlags=MOUSE_MOVE | MOUSE_ABSOLUTE | MOUSE_VIRTUALDESK | extra)
                user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))

            # Arrive with a small move so the game registers the hover, then click.
            mouse(point.x + 3, point.y + 3)
            time.sleep(0.05)
            mouse(point.x, point.y)
            time.sleep(0.08)
            if user32.GetForegroundWindow() != hwnd:
                raise RuntimeError("lost focus before the click")
            mouse(point.x, point.y, MOUSE_LEFTDOWN)
            time.sleep(0.06)
            mouse(point.x, point.y, MOUSE_LEFTUP)
            time.sleep(0.05)
        finally:
            user32.SetCursorPos(cursor.x, cursor.y)
            if previous and previous != hwnd and user32.IsWindow(previous):
                focus_window(previous)
            if was_hidden:
                hide_window(hwnd)


def looks_disconnected(img):
    """Cheap first check for Roblox's "Disconnected" dialog: a patch of its flat dark
    grey (about RGB 57, 59, 61) in the middle of the window."""
    w, h = img.size
    a = np.asarray(img.crop((int(w * 0.2), int(h * 0.2), int(w * 0.8), int(h * 0.8)))
                   .convert("RGB")).astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    grey = (np.abs(r - g) < 8) & (np.abs(g - b) < 8) & (r >= 45) & (r <= 75)
    return float(grey.mean()) > 0.04


def find_reconnect_button(img):
    """Centre (x, y) of the Reconnect button on Roblox's "Disconnected" dialog, in window
    pixels; None if that dialog isn't showing (or has no Reconnect, like a kick)."""
    w, h = img.size
    x0, y0 = int(w * 0.1), int(h * 0.1)
    region = img.crop((x0, y0, int(w * 0.9), int(h * 0.9)))
    scale = min(1.0, 1000 / region.width)
    if scale < 1:
        region = region.resize((int(region.width * scale), int(region.height * scale)))
    with EventDetector._lock:
        result = EventDetector.engine()(np.asarray(region), use_det=True, use_cls=False,
                                        use_rec=True)
    texts = [normalize(t) for t in (result.txts or [])]
    if not any(difflib.SequenceMatcher(None, t, "disconnected").ratio() >= 0.8 for t in texts):
        return None
    for box, text in zip(result.boxes, texts):
        if difflib.SequenceMatcher(None, text, "reconnect").ratio() >= 0.8:
            return (int(x0 + sum(x for x, _ in box) / 4 / scale),
                    int(y0 + sum(y for _, y in box) / 4 / scale))
    return None


class PopupCloser:
    """Clears the screens that block a window: the "Welcome Back~" popup (click its red
    X), Roblox's "Join Event" card that can sit on top of it (click its ×), and Roblox's
    "Disconnected" dialog (click Reconnect, then wait for the game to load, which ends
    with the Welcome Back popup).

    The colour checks are cheap and run every check; the text checks that confirm
    them only run when the colour is there, at most every CONFIRM_EVERY seconds.
    """

    CONFIRM_EVERY = 2.0
    RETRY_AFTER = 8.0
    RECONNECT_AGAIN_AFTER = 15.0  # if the Disconnected dialog is still (or back) there
    RECONNECT_WAIT = 60.0  # longest wait for the Welcome Back screen after reconnecting

    def __init__(self, click=click_in_window, on_action=None):
        self.enabled = True
        self.enabled_for = {}  # hwnd -> False where Anti Disconnect is switched off
        self.click = click
        self.on_action = on_action or (lambda ok, text: print(text, flush=True))
        self.confirmed_at = {}
        self.failed_at = {}
        self.event_checked = {}
        self.event_failed_at = {}
        self.disconnect_checked = {}
        self.reconnect_clicked = {}
        self.reconnecting = {}  # hwnd -> when Reconnect was clicked, until the game is back

    def check(self, hwnd, label, img):
        """True if this window is blocked right now (a screen was found and a click was
        attempted, or it's reconnecting): skip everything else for it this round."""
        if not self.enabled or not self.enabled_for.get(hwnd, True) or img is None:
            return False
        now = time.monotonic()
        if self._disconnected(hwnd, label, img, now):
            return True
        waiting = hwnd in self.reconnecting
        if waiting and now - self.reconnecting[hwnd] > self.RECONNECT_WAIT:
            self.reconnecting.pop(hwnd)
            self.on_action(False, f"{label}: no 'Welcome Back' screen within "
                                  f"{self.RECONNECT_WAIT:.0f} s of reconnecting; carrying on")
            waiting = False
        # The Join Event card sits on top of the Welcome Back popup: close it first.
        if self._join_event(hwnd, label, img, now):
            return True
        if self._welcome_back(hwnd, label, img, now):
            if waiting:
                self.reconnecting.pop(hwnd, None)
                self.on_action(True, f"{label}: back in the game after reconnecting")
            return True
        return waiting

    def _disconnected(self, hwnd, label, img, now):
        if not looks_disconnected(img):
            return False
        if now - self.disconnect_checked.get(hwnd, -self.CONFIRM_EVERY) < self.CONFIRM_EVERY:
            return False
        if now - self.reconnect_clicked.get(hwnd, -self.RECONNECT_AGAIN_AFTER) < \
                self.RECONNECT_AGAIN_AFTER:
            return False
        self.disconnect_checked[hwnd] = now
        spot = find_reconnect_button(img)
        if spot is None:
            return False
        self.reconnect_clicked[hwnd] = now
        try:
            self.click(hwnd, *spot)
        except RuntimeError as e:
            self.on_action(False, f"{label}: disconnected, but couldn't click Reconnect ({e}); "
                                  f"retrying in {self.RECONNECT_AGAIN_AFTER:.0f} s")
            return True
        self.reconnecting[hwnd] = now
        self.on_action(True, f"{label}: disconnected; clicked Reconnect and waiting for the "
                             "game to load")
        return True

    def _welcome_back(self, hwnd, label, img, now):
        if not red_close_buttons(img):
            return False
        if now - self.confirmed_at.get(hwnd, -self.CONFIRM_EVERY) < self.CONFIRM_EVERY:
            return False
        if now - self.failed_at.get(hwnd, -self.RETRY_AFTER) < self.RETRY_AFTER:
            return False
        self.confirmed_at[hwnd] = now
        spot = find_welcome_popup(img)
        if spot is None:
            return False
        try:
            self.click(hwnd, *spot)
        except RuntimeError as e:
            self.failed_at[hwnd] = time.monotonic()
            self.on_action(False, f"{label}: couldn't close the 'Welcome Back' popup ({e}); "
                                  f"retrying in {self.RETRY_AFTER:.0f} s")
            return True
        self.on_action(True, f"{label}: closed the 'Welcome Back' popup (it hides the hotbar)")
        return True

    def _join_event(self, hwnd, label, img, now):
        if not looks_like_roblox_card(img):
            return False
        if now - self.event_checked.get(hwnd, -self.CONFIRM_EVERY) < self.CONFIRM_EVERY:
            return False
        if now - self.event_failed_at.get(hwnd, -self.RETRY_AFTER) < self.RETRY_AFTER:
            return False
        self.event_checked[hwnd] = now
        spot = find_join_event(img)
        if spot is None:
            return False
        try:
            self.click(hwnd, *spot)
        except RuntimeError as e:
            self.event_failed_at[hwnd] = time.monotonic()
            self.on_action(False, f"{label}: couldn't close Roblox's 'Join Event' popup ({e}); "
                                  f"retrying in {self.RETRY_AFTER:.0f} s")
            return True
        self.on_action(True, f"{label}: closed Roblox's 'Join Event' popup")
        return True


# --------------------------------------------------------------------------- #
# Auto rebirth: open the Rebirth menu now and then, rebirth if the cash covers it
# --------------------------------------------------------------------------- #

REBIRTH_EVERY = 600.0  # the default (10 min); each window's slider changes its own
# The slider's stops, in seconds.
REBIRTH_INTERVALS = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300, 600, 900, 1200, 1800)
REBIRTH_LOG_PATH = APP_DIR / "rebirth_log.csv"  # every rebirth: time, window, cash, cost
# The game's number suffixes, smallest first.
MONEY_SUFFIXES = ["", "k", "m", "b", "t", "qa", "qi", "sx", "sp", "oc", "no", "dc"]
MONEY_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([a-z]{0,2})", re.IGNORECASE)


def parse_money(text):
    """'$50.3Qi' / '118.66Qi$' / '1,040,356' -> a number; None if there isn't one.

    OCR often reads the suffix's i as l or 1 ('QI', 'Ql'), so suffixes are matched
    by their first letter where that's unambiguous.
    """
    text = text.replace(" ", "")
    # OCR reads the Q of Qi as a zero or O ('868.70i'), and the S of Sx/Sp as a 5.
    text = re.sub(r"(\d)[0OoQ][il1|](?![a-z])", r"\1Qi", text)
    text = re.sub(r"(\d)[0OoQ][aA](?![a-z])", r"\1Qa", text)
    text = re.sub(r"(\d\.\d+)5([xXpP])(?![a-z])", r"\1S\2", text)
    match = MONEY_RE.search(text)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    suffix = match.group(2).lower()
    if suffix.startswith("q"):
        suffix = "qa" if suffix[1:2] == "a" else "qi"
    elif suffix.startswith("s"):
        suffix = "sp" if suffix[1:2] == "p" else "sx"
    elif suffix not in MONEY_SUFFIXES:
        suffix = suffix[:1] if suffix[:1] in MONEY_SUFFIXES else ""
    return number * 1000 ** MONEY_SUFFIXES.index(suffix)


def format_money(value):
    """1.1866e20 -> '118.66Qi' (the game's way of writing it)."""
    for i in range(len(MONEY_SUFFIXES) - 1, -1, -1):
        if value >= 1000 ** i or i == 0:
            return f"{value / 1000 ** i:,.2f}".rstrip("0").rstrip(".") + MONEY_SUFFIXES[i].title()
    return str(value)


def ocr_region(img, box, max_width=1280):
    """OCR part of a screenshot: [(text, (x0, y0, x1, y1) in window pixels)], top to bottom."""
    region = img.crop(box)
    scale = min(1.0, max_width / max(1, region.width))
    if scale < 1:
        region = region.resize((int(region.width * scale), int(region.height * scale)))
    with EventDetector._lock:
        result = EventDetector.engine()(np.asarray(region), use_det=True, use_cls=False, use_rec=True)
    found = []
    for quad, text in zip(result.boxes if result.txts else [], result.txts or []):
        xs, ys = [x for x, _ in quad], [y for _, y in quad]
        found.append((text, (box[0] + min(xs) / scale, box[1] + min(ys) / scale,
                             box[0] + max(xs) / scale, box[1] + max(ys) / scale)))
    return sorted(found, key=lambda f: (f[1][1], f[1][0]))


def centre(rect):
    return int((rect[0] + rect[2]) / 2), int((rect[1] + rect[3]) / 2)


def similar(text, word, at_least=0.8):
    return difflib.SequenceMatcher(None, normalize(text), word).ratio() >= at_least


def parse_cash(text):
    """'622,996,641,451$' / '118.66Qi$' -> a number; None if there isn't one.

    OCR often reads the thousands commas as dots ('745.853') and the $ after the
    number as S, 5 or 8, so a number made of 3-digit groups is taken whole, with
    anything after its last group taken as the $.
    """
    text = re.sub(r"\s", "", text).strip(":;").lstrip("$")
    whole = re.fullmatch(r"(\d[\d.,]*[.,]\d{3})[$Ss58]?\$?", text)
    if whole:
        return float(re.sub(r"[.,]", "", whole.group(1)))
    return parse_money(text)


def read_cash(img):
    """(cash, as written) from 'Cash: 622,996,641,451$' or 'Cash: 118.66Qi$' in the bottom
    left; (None, '') if unreadable."""
    w, h = img.size
    lines = ocr_region(img, (0, int(h * 0.82), int(w * 0.32), h))
    for text, rect in reversed(lines):  # the Cash line is the lowest one
        if "cash" not in normalize(text):
            continue
        after = re.split(r"(?i)cash\W*", text, maxsplit=1)[-1]
        if re.match(r"[a-z]{2}", after, re.IGNORECASE):
            continue  # another label, like the Cash Potion's in the hotbar
        # The amount is in the same box ('Cash: 118.66Qi$') or in one to its right on
        # the same row, never one above or below (the hotbar's 'Lvl. 1 x40').
        beside = [t for t, r in lines if r[0] > rect[0] and rect[1] < (r[1] + r[3]) / 2 < rect[3]]
        for candidate in (after, *beside):
            value = parse_cash(candidate)
            if value is not None:
                return value, candidate.strip(" :$")
    return None, ""


def find_rebirth_button(img):
    """Centre of the REBIRTH button in the left-hand menu; None if not found."""
    w, h = img.size
    for text, rect in ocr_region(img, (0, int(h * 0.1), int(w * 0.12), int(h * 0.9))):
        if similar(text, "rebirth"):
            return centre(rect)
    return None


def read_rebirth_menu(img):
    """The open Rebirth menu: {'cost', 'cost_text', 'rebirth': (x, y), 'close': (x, y)},
    or None if it isn't open."""
    w, h = img.size
    lines = ocr_region(img, (int(w * 0.12), int(h * 0.03), int(w * 0.88), int(h * 0.97)))
    heading = next((rect for text, rect in lines if similar(text, "requirements")), None)
    if heading is None:
        return None
    line_height = heading[3] - heading[1]
    heading_middle, heading_width = (heading[0] + heading[2]) / 2, heading[2] - heading[0]
    cost = cost_text = button = None
    for text, rect in lines:
        if rect[1] <= heading[1]:
            continue
        # The cost sits right under the heading, centred on it (the drop-chance list
        # on the left can be at the same height).
        under = abs((rect[0] + rect[2]) / 2 - heading_middle) < heading_width * 0.6
        if (cost is None and under and rect[1] - heading[3] < line_height * 3
                and re.search(r"\d", text) and parse_money(text) is not None):
            cost, cost_text = parse_money(text), text.strip(" $")
        if button is None and similar(text, "rebirth"):
            button = centre(rect)
    # The menu's own red X: the red button with a white cross above the heading.
    above = [b for b in red_close_buttons(img) if b[3] < heading[1]]
    close = centre(min(above, key=lambda b: b[1])) if above else None
    if cost is None or button is None:
        return None
    return {"cost": cost, "cost_text": cost_text, "rebirth": button, "close": close}


def find_purchase_prompt(img):
    """Roblox's "Buy Robux and item" purchase prompt (it pops up for "Skip Rebirth" when
    Rebirth! is pressed without enough cash): the centre of its × close button, or
    None if it isn't showing. Only the × is ever located: the prompt's Buy button
    spends real money."""
    w, h = img.size
    lines = ocr_region(img, (0, 0, w, h))  # only runs right after Rebirth!, so search it all
    texts = [normalize(t) for t, _ in lines]
    title = next((rect for (t, rect), n in zip(lines, texts)
                  if "buyrobux" in n or n.startswith("buyitem")), None)
    if title is None or not any("paymentmethod" in n or "termsofuse" in n for n in texts):
        return None
    # The × is the rightmost light mark on the title's line. Where that line ends comes
    # from the text OCR found on it (the Robux balance and the ×: "0 ×"); the dark
    # panel alone isn't enough, since the Rebirth menu behind it is dark too.
    y0, y1 = int(title[1]), int(title[3])
    height = y1 - y0
    x_from = int(title[2])
    # Read the rest of the title line again at full size: the "0 ×" is small text that
    # the shrunk whole-window read can miss.
    line_box = (int(title[2]), max(0, int(y0 - height * 0.3)),
                min(w, int(title[2] + height * 5)), min(h, int(y1 + height * 0.3)))
    on_line = [rect for _, rect in ocr_region(img, line_box, max_width=10_000)
               if rect[0] >= title[2] - 2 and rect[0] - title[2] < height * 4
               and y0 <= (rect[1] + rect[3]) / 2 <= y1]
    # OCR sometimes reads only the "0", so look a little past the text it found...
    x_to = int(max(r[2] for r in on_line) + height * 1.2) if on_line else int(title[2] + height * 4.5)
    # ...but never past the prompt's right edge, measured off its wide blue Buy bar (which
    # spans the prompt; it's only used as a ruler here, never clicked).
    buy = next((rect for (t, rect), n in zip(lines, texts) if n == "buy" and rect[1] > y1), None)
    if buy is not None:
        row = np.asarray(img.crop((0, int((buy[1] + buy[3]) / 2), w,
                                   int((buy[1] + buy[3]) / 2) + 1)).convert("RGB")).astype(int)[0]
        blue = (row[:, 2] > row[:, 0] + 50) & (row[:, 2] > 100)
        x = int(buy[2])
        while x < w - 1 and (blue[x] or blue[min(w - 1, x + 1)] or blue[min(w - 1, x + 2)]):
            x += 1
        x_to = min(x_to, x)
    band = np.asarray(img.crop((x_from, y0, min(w, x_to), y1)).convert("L")).astype(int)
    dark = (band < 50).mean(axis=0)
    end, run = len(dark), 0
    for x, share in enumerate(dark):  # the panel ends where a run of columns isn't dark
        run = run + 1 if share < 0.5 else 0
        if run > max(12, y1 - y0):
            end = x - run + 1
            break
    light = (band[:, :end] > 120).any(axis=0)
    marks = []  # (left, right, brightest) of each light mark, left to right
    x = 0
    while x < end:
        if light[x]:
            start = x
            while x < end and light[x]:
                x += 1
            marks.append((start, x - 1, int(band[:, start:x].max())))
        x += 1
    if not marks:
        return None
    # The × is grey; the Robux balance next to it is white.
    grey = [m for m in marks if m[2] < 215]
    left, right, _ = (grey or marks)[-1]
    return int(x_from + (left + right) / 2), int((y0 + y1) / 2)


def fresh_frame(hwnd, wait=0.0):
    """A screenshot of the window taken at least `wait` seconds from now."""
    stream = get_stream(hwnd)
    if stream is None:
        return None
    time.sleep(wait)
    since = time.monotonic()
    deadline = since + 2.0
    while stream.received < since and time.monotonic() < deadline:
        time.sleep(0.05)
    return stream.latest()


class AutoRebirth:
    """Every few seconds (each window's own `every`, REBIRTH_EVERY by default), for each window with it switched on: open the
    Rebirth menu from the left-hand menu, compare the cost with the cash in the
    bottom left, press Rebirth! if the cash covers it, and close the menu again.
    Once the cost has been read, the menu isn't opened again until the cash covers it.

    Runs on its own thread per window, so detection doesn't wait for it. Each
    rebirth is reported through on_action and added to REBIRTH_LOG_PATH.
    """

    MENU_WAIT = 1.2  # seconds for the menu to open
    AFTER_WAIT = 0.3  # after Rebirth!, look this often for the cash dropping...
    AFTER_CHECKS = 6  # ...this many times, then look for a purchase prompt
    CASH_RETRY_WAIT = 2.0  # if the cash can't be read, look again this much later...
    CASH_RETRIES = 3  # ...this many times before giving up until the next round

    def __init__(self, click=click_in_window, grab=fresh_frame, on_action=None, on_reading=None):
        self.click = click
        self.grab = grab
        self.on_action = on_action or (lambda ok, text: print(text, flush=True))
        self.on_reading = on_reading or (lambda hwnd, text: None)
        self.on_stopped = None  # called with hwnd if a round had to switch itself off
        self.on = {}  # hwnd -> switched on
        self.every = {}  # hwnd -> seconds between rounds (REBIRTH_EVERY if not set)
        self.last_run = {}
        self.known_cost = {}  # hwnd -> (cost, as written) of the next rebirth, once seen
        self.busy = set()
        self._lock = threading.Lock()

    def set_enabled(self, hwnd, on):
        with self._lock:
            self.on[hwnd] = on
            if on:  # first look a few seconds after switching on, then every `every`
                self.last_run[hwnd] = time.monotonic() - self.every.get(hwnd, REBIRTH_EVERY) + 5

    def maybe_start(self, hwnd, label):
        """Start a round for this window if it's switched on and due."""
        with self._lock:
            if not self.on.get(hwnd) or hwnd in self.busy:
                return False
            if time.monotonic() - self.last_run.get(hwnd, 0) < self.every.get(hwnd, REBIRTH_EVERY):
                return False
            self.busy.add(hwnd)
            self.last_run[hwnd] = time.monotonic()
        threading.Thread(target=self._round, args=(hwnd, label), daemon=True).start()
        return True

    def _round(self, hwnd, label):
        try:
            self.run_once(hwnd, label)
        except RuntimeError as e:
            self.on_action(False, f"{label}: auto rebirth couldn't click ({e})")
        finally:
            with self._lock:
                self.busy.discard(hwnd)

    def run_once(self, hwnd, label):
        img = self.grab(hwnd)
        if img is None:
            self.on_action(False, f"{label}: auto rebirth can't see the window (minimised?)")
            return
        menu = read_rebirth_menu(img)
        known = self.known_cost.get(hwnd)
        if menu is None and known is not None:
            # The cost is already known: only open the menu once the cash covers it.
            img, cash, cash_text = self._read_cash(hwnd, img)
            if cash is None:
                self.on_action(False, f"{label}: auto rebirth couldn't read the cash (tried "
                                      f"{self.CASH_RETRIES + 1} times, {self.CASH_RETRY_WAIT:.0f} s apart)")
                return
            if cash < known[0]:
                self.on_reading(hwnd, f"cash {cash_text} · next rebirth {known[1]} "
                                      f"({cash / known[0]:.0%})")
                return
        opened_it = menu is None
        if opened_it:
            button = find_rebirth_button(img)
            if button is None:
                self.on_action(False, f"{label}: auto rebirth couldn't find the REBIRTH button "
                                      "in the left-hand menu")
                return
            self.click(hwnd, *button)
            img = self.grab(hwnd, self.MENU_WAIT)
            menu = read_rebirth_menu(img) if img else None
            if menu is None:
                self.on_action(False, f"{label}: auto rebirth clicked REBIRTH but the menu "
                                      "didn't open")
                return
        self.known_cost[hwnd] = (menu["cost"], menu["cost_text"])
        img, cash, cash_text = self._read_cash(hwnd, img)
        if cash is None:
            self.on_action(False, f"{label}: auto rebirth couldn't read the cash (tried "
                                  f"{self.CASH_RETRIES + 1} times, {self.CASH_RETRY_WAIT:.0f} s apart)")
            self._close(hwnd, menu, button if opened_it else None)
            return
        enough = cash >= menu["cost"]
        self.on_reading(hwnd, f"cash {cash_text} · next rebirth {menu['cost_text']}"
                              + ("" if enough else f" ({cash / menu['cost']:.0%})"))
        if enough:
            self.click(hwnd, *menu["rebirth"])
            # The cash dropping means the rebirth went through, so close the menu straight
            # away (reading the cash is quick; reading the whole window isn't). If it
            # doesn't drop, look for a purchase prompt ("Skip Rebirth" for Robux): the
            # cash didn't cover it after all, the numbers were misread. Close that (only
            # ever its ×) and the menu.
            after, new_cash, new_text = None, None, ""
            for _ in range(self.AFTER_CHECKS):
                img = self.grab(hwnd, self.AFTER_WAIT)
                if img is None:
                    continue
                after = img
                new_cash, new_text = read_cash(img)
                if new_cash is not None and new_cash < cash * 0.9:
                    break
            went_through = new_cash is not None and new_cash < cash * 0.9
            if not went_through:
                prompt = find_purchase_prompt(after) if after is not None else None
                if prompt is not None:
                    self._dismiss_purchase(hwnd, label, prompt, cash_text, menu,
                                           button if opened_it else None)
                    return
            self._close(hwnd, menu, button if opened_it else None)
            # The new cost is read from the last frame, now that the menu is shut.
            new_menu = read_rebirth_menu(after) if went_through else None
            self._record(hwnd, label, cash_text, menu["cost_text"])
            self.on_action(True, f"{label}: REBIRTHED with {cash_text} cash "
                                 f"(cost {menu['cost_text']})")
            if new_cash is not None and not went_through:
                self.on_action(False, f"{label}: cash didn't drop after Rebirth! "
                                      f"({new_text}); it may not have gone through")
            if new_menu and new_menu["cost"] > menu["cost"]:
                self.known_cost[hwnd] = (new_menu["cost"], new_menu["cost_text"])
                if new_cash is not None:
                    self.on_reading(hwnd, f"cash {new_text} · next rebirth {new_menu['cost_text']}")
            else:  # the new cost wasn't seen: open the menu next round to read it
                self.known_cost.pop(hwnd, None)
            return
        self._close(hwnd, menu, button if opened_it else None)

    def _read_cash(self, hwnd, img):
        """(img, cash, as written); if unreadable, looks again CASH_RETRY_WAIT seconds
        later, up to CASH_RETRIES times."""
        cash, cash_text = read_cash(img)
        for _ in range(self.CASH_RETRIES):
            if cash is not None:
                break
            # Often just a passing effect over the number: look again in a moment.
            again = self.grab(hwnd, self.CASH_RETRY_WAIT)
            if again is not None:
                img = again
                cash, cash_text = read_cash(img)
        return img, cash, cash_text

    def _dismiss_purchase(self, hwnd, label, prompt, cash_text, menu, sidebar_button):
        """Close Roblox's purchase prompt with its ×, then the Rebirth menu. If the prompt
        won't go away, stop auto rebirth for this window rather than keep clicking."""
        self.click(hwnd, *prompt)
        self.on_action(False, f"{label}: rebirth wasn't affordable after all (read {cash_text} "
                              f"cash vs cost {menu['cost_text']}); closed Roblox's purchase "
                              "prompt without buying")
        check = self.grab(hwnd, 0.8)
        if check is not None and find_purchase_prompt(check) is not None:
            with self._lock:
                self.on[hwnd] = False
            self.on_action(False, f"{label}: the purchase prompt is still open, so auto rebirth "
                                  "is stopped for this window; close it yourself (don't press Buy)")
            if self.on_stopped:
                self.on_stopped(hwnd)
            return
        menu = (read_rebirth_menu(check) if check is not None else None) or menu
        self._close(hwnd, menu, sidebar_button)

    def _close(self, hwnd, menu, sidebar_button):
        """Close the menu with its X (or the REBIRTH button again if the X wasn't found)."""
        spot = menu.get("close") or sidebar_button
        if spot:
            self.click(hwnd, *spot)

    @staticmethod
    def _record(hwnd, label, cash_text, cost_text):
        try:
            upgrade_rebirth_log()
            new = not REBIRTH_LOG_PATH.exists()
            with REBIRTH_LOG_PATH.open("a", encoding="utf-8", newline="") as f:
                if new:
                    f.write(REBIRTH_LOG_HEADER + "\n")
                # Quoted where needed: the cash is often written out in full ('203,551,617').
                csv.writer(f, lineterminator="\n").writerow(
                    [time.strftime('%Y-%m-%d %H:%M:%S'), label, window_pid(hwnd), cash_text, cost_text])
        except OSError:
            pass


# The Roblox process ID tells windows apart; "Window 1" can mean a different window
# from one session to the next.
REBIRTH_LOG_HEADER = "time,window,pid,cash,cost"
REBIRTH_LOG_FIELDS = REBIRTH_LOG_HEADER.split(",")


def rebirth_log_row(line):
    """One log line -> {'time', 'window', 'pid', 'cash', 'cost'}, or None if it isn't one.

    Earlier versions wrote amounts like '203,551,617' unquoted, splitting them over
    several fields; the cash is everything between the pid and the cost (window names
    can't hold commas, and the cost is written short, like '137.98M').
    """
    parts = next(csv.reader([line]), [])
    if len(parts) > 5:
        parts = parts[:3] + [",".join(parts[3:-1]), parts[-1]]
    return dict(zip(REBIRTH_LOG_FIELDS, parts)) if len(parts) == 5 else None


def write_rebirth_log(rows):
    with REBIRTH_LOG_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, REBIRTH_LOG_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def upgrade_rebirth_log():
    """Bring a log written by an earlier version up to date: add the pid column (left
    empty there) and quote amounts that were written with unquoted commas."""
    if not REBIRTH_LOG_PATH.exists():
        return
    lines = [line for line in REBIRTH_LOG_PATH.read_text(encoding="utf-8").splitlines() if line]
    if lines and lines[0] == "time,window,cash,cost":
        lines = [REBIRTH_LOG_HEADER] + [
            ",".join(line.split(",")[:2] + [""] + line.split(",")[2:]) for line in lines[1:]]
    elif all(len(next(csv.reader([line]), [])) <= 5 for line in lines[1:]):
        return
    write_rebirth_log([row for row in map(rebirth_log_row, lines[1:]) if row])


def rebirth_row_matches(row, pid, label):
    """A log row belongs to this window: same process ID, or (rows from before process
    IDs were recorded) the same window name."""
    return row["pid"] == str(pid) or (not row["pid"] and row["window"] == label)


def clear_rebirth_log(pid, label):
    """Remove one window's rebirths from the log, keeping every other window's.
    Returns how many were removed."""
    try:
        upgrade_rebirth_log()
        lines = REBIRTH_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    keep = [lines[0]]
    for line in lines[1:]:
        row = rebirth_log_row(line)
        if row and rebirth_row_matches(row, pid, label):
            continue
        keep.append(line)
    REBIRTH_LOG_PATH.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return len(lines) - len(keep)


def read_rebirth_log(pid=None, label=None):
    """Rebirths from the log, oldest first: [{'time', 'window', 'pid', 'cash', 'cost'}].

    With `pid`, only that window's; rows from before process IDs were recorded are
    matched on the window's name (`label`) instead.
    """
    try:
        upgrade_rebirth_log()
        lines = REBIRTH_LOG_PATH.read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return []
    rows = []
    for line in lines:
        row = rebirth_log_row(line)
        if not row:
            continue
        row["cash"], row["cost"] = row["cash"].lstrip("$"), row["cost"].lstrip("$")
        if pid is None or rebirth_row_matches(row, pid, label):
            rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def normalize(text):
    return re.sub(r"[^a-z]", "", text.lower())


def is_title(text):
    # The title stands alone on its line, so longer text that merely mentions it
    # (a chat message, another app's window) doesn't count.
    norm = normalize(text)
    if len(norm) > len(TARGET) + 3:
        return False
    # OCR on the game font sometimes swaps a letter ('Hallows'Eye').
    return TARGET in norm or difflib.SequenceMatcher(None, norm, TARGET).ratio() >= 0.88


def parse_duration(text):
    """'Event Duration: 2:17' -> seconds left, or None."""
    if "duration" not in normalize(text):
        return None
    m = DURATION_RE.search(text)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


class EventDetector:
    """Looks for the Hallows' Eve banner with RapidOCR (runs locally, no internet).

    The banner area is scanned with full text detection (~250 ms). Once the
    title has been found, later checks only run recognition on that spot
    (~20 ms), falling back to a full scan if it stops matching.
    One detector per window, since each remembers where the banner was.
    """

    _engine = None
    _lock = threading.Lock()

    def __init__(self):
        self.title_box = self.duration_box = None  # fractions of the window size

    @classmethod
    def engine(cls):
        if cls._engine is None:
            from rapidocr import RapidOCR
            # RapidOCR resets its logger on startup, so quiet it on both sides.
            logging.getLogger("RapidOCR").disabled = True
            # "max" stops it upscaling the small band to 736 px, which made scans 6x slower.
            # One thread: onnxruntime's default pool busy-spins on every core between reads.
            cls._engine = RapidOCR(params={
                "EngineConfig.onnxruntime.intra_op_num_threads": 1,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "Det.limit_type": "max", "Det.limit_side_len": 960})
            logging.getLogger("RapidOCR").disabled = True
        return cls._engine

    def check(self, img):
        """Returns (active, seconds_left or None, ocr text that decided it)."""
        if self.title_box:
            txt = self._recognize(self._crop(img, self.title_box))
            if is_title(txt):
                left = None
                if self.duration_box:
                    left = parse_duration(self._recognize(self._crop(img, self.duration_box)))
                if left is not None or not self.duration_box:
                    return True, left, txt
            self.title_box = self.duration_box = None
        return self._scan(img)

    def _scan(self, img):
        w, h = img.size
        x0, y0 = int(w * BAND[0]), int(h * BAND[1])
        band = img.crop((x0, y0, int(w * BAND[2]), int(h * BAND[3])))
        with self._lock:
            result = self.engine()(np.asarray(band), use_det=True, use_cls=False, use_rec=True)
        if not result.txts:
            return False, None, ""
        title = left = None
        for box, txt in zip(result.boxes, result.txts):
            frac = self._to_fraction(box, x0, y0, w, h)
            if title is None and is_title(txt):
                title, self.title_box = txt, frac
            elif left is None and (secs := parse_duration(txt)) is not None:
                left, self.duration_box = secs, frac
        if title is None:
            self.duration_box = None
            return False, None, " | ".join(result.txts)
        return True, left, title

    def _recognize(self, crop):
        with self._lock:
            result = self.engine()(np.asarray(crop), use_det=False, use_cls=False, use_rec=True)
        return result.txts[0] if result.txts else ""

    @staticmethod
    def _to_fraction(box, x0, y0, w, h):
        """Detected text box -> padded box as fractions of the window, so it survives resizes."""
        xs = [x + x0 for x, _ in box]
        ys = [y + y0 for _, y in box]
        pad = (max(ys) - min(ys)) * 0.3
        return ((min(xs) - pad) / w, (min(ys) - pad) / h, (max(xs) + pad) / w, (max(ys) + pad) / h)

    @staticmethod
    def _crop(img, frac):
        w, h = img.size
        return img.crop((max(0, int(frac[0] * w)), max(0, int(frac[1] * h)),
                         min(w, int(frac[2] * w)), min(h, int(frac[3] * h))))


def format_left(seconds):
    return f"{seconds // 60}:{seconds % 60:02d} left" if seconds is not None else "time left unknown"


# --------------------------------------------------------------------------- #
# Settings and helpers the window (detector_ui.py) shares with the watching code
# --------------------------------------------------------------------------- #

# In an exe, bundled files are unpacked to sys._MEIPASS.
ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets"
PHOTO_PATH = ASSETS / "photo.jpg"
ICON_PATH = ASSETS / "app.ico"

THUMB_EVERY = 2.0  # seconds between live thumbnail updates per window
TEST_EVENT_SECONDS = 30
LABELS_EVERY_EVENT, LABELS_EVERY = 3.0, 15.0  # seconds between reads of the hotbar labels
MUTATED_WARNING = "You have selected an already mutated character"
UI_SETTINGS_PATH = APP_DIR / "ui_settings.json"  # remembered window size and position
PRIVATE_SERVERS_PATH = APP_DIR / "private_servers.json"  # private server links, by Roblox process ID
WINDOW_NAMES_PATH = APP_DIR / "window_names.json"  # names given to windows, by Roblox process ID


def load_window_names():
    """{pid: name} for windows the user renamed."""
    try:
        return {int(pid): str(name) for pid, name in json.loads(WINDOW_NAMES_PATH.read_text(
            encoding="utf-8")).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_window_names(names):
    try:
        WINDOW_NAMES_PATH.write_text(json.dumps({str(pid): name for pid, name in names.items()},
                                                indent=1), encoding="utf-8")
    except OSError:
        pass


def load_private_servers():
    """{pid: link} for the private servers typed in for windows."""
    try:
        return {int(pid): str(link) for pid, link in json.loads(PRIVATE_SERVERS_PATH.read_text(
            encoding="utf-8")).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_private_servers(links):
    try:
        PRIVATE_SERVERS_PATH.write_text(json.dumps({str(pid): link for pid, link in links.items()},
                                                   indent=1), encoding="utf-8")
    except OSError:
        pass


def clean_window_name(text):
    """A name typed for a window, made safe for the logs (no commas or line breaks)."""
    return re.sub(r"[,\s]+", " ", text).strip()[:30]


def slot_list(chosen):
    """'slot 4' / 'slots 4 then 7' (keys are the same numbers, except slot 10 = key 0)."""
    if len(chosen) == 1:
        return f"slot {chosen[0]}"
    return "slots " + " then ".join(str(s) for s in chosen)


def toggle_slot(chosen, slot):
    """The choice with `slot` added at the end (lowest priority), or taken out."""
    return [s for s in chosen if s != slot] if slot in chosen else [*chosen, slot]


def ordinal(n):
    return f"{n}{'tsnrhtdd'[(n // 10 % 10 != 1) * (n % 10 < 4) * n % 10::4]}"


def fmt_clock(seconds):
    return f"{seconds // 60}:{seconds % 60:02d}"


def duration(seconds):
    """60 -> '1 min', 90 -> '1.5 min', 30 -> '30 s'."""
    if seconds < 60:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:g} min"


def plural(count, word):
    return f"{count} {word}{'s' * (count != 1)}"


def run_gui(interval, debug):
    restore_from_last_run()
    atexit.register(show_all_hidden)  # also if the app exits some other way
    import detector_ui  # the Qt window; given this module so both share one copy of it
    detector_ui.run(sys.modules[__name__], interval, debug)
    quit_now()


def quit_now():
    """Exit without Python's normal shutdown, which crashes ("Fatal Python error:
    PyGILState_Release") while windows-capture threads are still running."""
    show_all_hidden()
    if sys.stdout:
        sys.stdout.flush()
    os._exit(0)


# --------------------------------------------------------------------------- #
# Discord ping (as the Roblox player-count bot)
# --------------------------------------------------------------------------- #

DISCORD_CONFIG_PATH = APP_DIR / "discord_config.json"
DISCORD_API = "https://discord.com/api/v10"
DEFAULT_DISCORD_CONFIG = {
    "bot_token": "",
    "channel": "event-ping",
    "ping_role": "ps-ping",
}
# A window counts as a new event (worth a ping) if it hasn't shown the event for
# this long. Covers OCR missing the banner for a few checks mid-event.
EVENT_GAP = 90
# Windows in the same Roblox server see the event within seconds of each other;
# don't ping again for another window that joins in this soon after a ping.
PING_COOLDOWN = 60


class DiscordError(Exception):
    pass


class DiscordNotifier:
    """Posts in Discord when Hallows' Eve starts, tagging the configured role with
    the private server link of each window that has it:

        @ps-ping Hallows' Eve has started! https://www.roblox.com/share?code=... (1:46 left)

    Talks to Discord's REST API with the player-count bot's token, so the hosted
    bot doesn't need to change. `channel` and `ping_role` in the config can be
    names ('event-ping', 'ps-ping') or numeric IDs; names are looked up in the
    servers the bot is in.
    """

    def __init__(self, token, channel, role, on_result=None):
        self.token = token
        self.channel = str(channel).lstrip("#")
        self.role = str(role).lstrip("@&")
        self.on_result = on_result or (lambda ok, text: print(text, flush=True))
        self.enabled = True
        self.link_for = lambda label: ""  # a window's private server link, by its name
        self.channel_id = self.channel_label = self.guild_id = None
        self.role_id = self.role_label = None
        self.role_mentionable = True
        self._last_seen = {}  # window label -> monotonic time it last showed the event
        self._last_ping = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, path=DISCORD_CONFIG_PATH, on_result=None):
        """A notifier, or (None, reason) if Discord isn't set up yet.

        Creates a blank config file on first run. The token can also come from
        the DISCORD_TOKEN environment variable.
        """
        if not path.exists():
            path.write_text(json.dumps(DEFAULT_DISCORD_CONFIG, indent=2) + "\n")
        try:
            config = {**DEFAULT_DISCORD_CONFIG, **json.loads(path.read_text())}
        except ValueError as e:
            return None, f"{path.name} isn't valid JSON ({e})"
        token = (config["bot_token"] or os.environ.get("DISCORD_TOKEN", "")).strip()
        if not token:
            return None, f"add the bot token to {path.name}"
        return cls(token, config["channel"], config["ping_role"], on_result), None

    # ---------------------------------------------------------------- events

    def observe(self, active_windows):
        """Call after each round of checks with [(label, seconds_left)] showing the event.

        Sends one message (on a background thread) for windows that just started
        the event, so a ping never holds up detection.
        """
        now = time.monotonic()
        with self._lock:
            started = [(label, left) for label, left in active_windows
                       if now - self._last_seen.get(label, -EVENT_GAP) >= EVENT_GAP]
            for label, _ in active_windows:
                self._last_seen[label] = now
            if not started or not self.enabled:
                return
            if self._last_ping is not None and now - self._last_ping < PING_COOLDOWN:
                return
            self._last_ping = now
        threading.Thread(target=self._send_event, args=(started,), daemon=True).start()

    def event_message(self, started):
        """'<@&role> Hallows' Eve has started! <link> (1:46 left)', one line per private
        server (windows in the same one are listed once; a window with no link typed in
        is named instead)."""
        places = {}  # link (or window name) -> seconds left, the first reading
        for label, left in started:
            place = (self.link_for(label) or "").strip() or label
            places.setdefault(place, left)
        lines = [f"{place} ({fmt_clock(left)} left)" if left is not None else place
                 for place, left in places.items()]
        return f"<@&{self.role_id}> Hallows' Eve has started! " + "\n".join(lines)

    def _send_event(self, started):
        where = ", ".join(label for label, _ in started)
        try:
            self.resolve()
            self.post(self.event_message(started), ping=True)
            self.on_result(True, f"Pinged {self.role_label} in {self.channel_label} for "
                                 f"Hallows' Eve ({where})")
        except DiscordError as e:
            self.on_result(False, f"Discord ping failed: {e}")

    # ---------------------------------------------------------------- Discord API

    def resolve(self):
        """Look up the channel and role IDs (once). Returns a one-line summary."""
        if self.channel_id is None:
            self._resolve_channel()
        if self.role_id is None:
            self._resolve_role()
        summary = f"Will ping {self.role_label} in {self.channel_label}"
        if not self.role_mentionable:
            summary += (f" (but {self.role_label} isn't mentionable: turn on 'Allow anyone to "
                        "@mention this role' for it, or give the bot 'Mention All Roles', or "
                        "nobody gets notified)")
        return summary

    def _resolve_role(self):
        if not self.guild_id:
            raise DiscordError(f"can't look up @{self.role} outside a server")
        roles = self._request("GET", f"/guilds/{self.guild_id}/roles")
        wanted = self.role.lower()
        role = next((r for r in roles if r["id"] == self.role or r["name"].lower() == wanted), None)
        if role is None:
            raise DiscordError(f"the server has no @{self.role} role; check ping_role in "
                               f"{DISCORD_CONFIG_PATH.name}")
        self.role_id, self.role_label = role["id"], f"@{role['name']}"
        self.role_mentionable = role.get("mentionable", True) or self._bot_can_mention_roles(roles)

    def _bot_can_mention_roles(self, roles):
        """True if the bot's own roles let it ping any role (Administrator or Mention All Roles)."""
        try:
            me = self._request("GET", "/users/@me")
            member = self._request("GET", f"/guilds/{self.guild_id}/members/{me['id']}")
        except DiscordError:
            return True  # can't tell: don't warn
        mine = set(member.get("roles", [])) | {self.guild_id}  # @everyone has the server's ID
        allowed = 0
        for r in roles:
            if r["id"] in mine:
                allowed |= int(r.get("permissions", 0))
        return bool(allowed & (1 << 3 | 1 << 17))  # ADMINISTRATOR, MENTION_EVERYONE

    def _resolve_channel(self):
        if self.channel.isdigit():
            channel = self._request("GET", f"/channels/{self.channel}")
            self.channel_id, self.guild_id = channel["id"], channel.get("guild_id")
            self.channel_label = f"#{channel.get('name', self.channel)}"
            return
        for guild in self._request("GET", "/users/@me/guilds"):
            for channel in self._request("GET", f"/guilds/{guild['id']}/channels"):
                # 0 = text channel, 5 = announcement channel
                if channel["type"] in (0, 5) and channel["name"].lower() == self.channel.lower():
                    self.channel_id, self.guild_id = channel["id"], guild["id"]
                    self.channel_label = f"#{channel['name']} ({guild['name']})"
                    return
        raise DiscordError(f"the bot can't see a #{self.channel} channel in any of its servers")

    def post(self, content, ping):
        allowed = {"roles": [self.role_id]} if ping else {"parse": []}
        self._request("POST", f"/channels/{self.channel_id}/messages",
                      body={"content": content, "allowed_mentions": allowed})

    def send_test(self):
        """Post a test message that mentions the role, like a real ping does."""
        self.resolve()
        self.post(f"<@&{self.role_id}> Test from the Hallows' Eve detector: real pings look like "
                  "this, with each window's private server link.", ping=True)

    def _request(self, method, path, body=None, query=None, retry=True):
        url = DISCORD_API + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bot {self.token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (hallows-eve-detector, 1.0)",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            if e.code == 429 and retry:  # rate limited: wait as told, then try once more
                try:
                    wait = float(json.loads(detail).get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                time.sleep(min(wait, 10))
                return self._request(method, path, body, query, retry=False)
            reasons = {401: "the bot token was rejected",
                       403: "the bot lacks permission (it needs View Channel and Send "
                            "Messages in that channel)",
                       404: "not found (wrong channel or user ID?)"}
            raise DiscordError(f"{reasons.get(e.code, f'HTTP {e.code}')}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            raise DiscordError(f"couldn't reach Discord ({e})") from None


# --------------------------------------------------------------------------- #
# Monitoring loop (shared by GUI and console)
# --------------------------------------------------------------------------- #

def monitor(selected, interval, debug, stop, on_status=None, on_change=None, notifier=None,
            equipper=None, simulated=None, detecting=None, popups=None, rebirther=None,
            names=None):
    """Check every selected window each `interval` seconds until `stop` is set.

    on_status(hwnd, active, seconds_left, image, mutated, slots) runs every check
    (active and image are None when the window can't be captured; `slots` is the
    hotbar found in that image; `mutated` is the set of slots with Dracula when the
    labels were read this check, else None); on_change(hwnd, label, active, seconds_left)
    runs when the event starts or ends in a window. A DiscordNotifier, if given,
    hears about every round and pings when the event starts; a HotbarEquipper
    hears about every check and presses hotbar keys. `simulated` maps a window to
    the time.monotonic() at which a test event in it ends; until then the window
    counts as showing the event (but never pings Discord). `detecting` maps a window
    to whether it's looked at (missing = yes); windows switched off there aren't
    checked for Hallows' Eve at all (a test event still runs). A PopupCloser, if given, clears the "Welcome Back~" popup and the
    "Disconnected" dialog before anything else is done with a window. An AutoRebirth,
    if given, gets the chance to start a rebirth round for each window. `names`, if
    given, maps a window to a newer name for it (renamed while running).
    """
    detectors = {hwnd: EventDetector() for hwnd, _ in selected}
    last = {hwnd: None for hwnd, _ in selected}
    hotbars = {}  # hwnd -> {"sig": icons when labels were last read, "read_at": time}
    held_cards = {}  # hwnd -> (held slot, its icon, chosen slots, whether it's a character)
    steady = SteadyHotbar()
    if debug:
        DEBUG_DIR.mkdir(exist_ok=True)
    sct = open_screen()
    while not stop.is_set():
        started = time.perf_counter()
        showing = []
        for hwnd, label in selected:
            label = names.get(hwnd, label) if names else label
            stream = get_stream(hwnd)
            img = stream.latest() if stream else grab_from_screen(hwnd, sct)
            # First priority: the "Disconnected" dialog and the "Welcome Back~" popup
            # (which covers the hotbar). Clear them, and look at this window again next
            # round; while it's reconnecting, leave it alone.
            if popups and popups.check(hwnd, label, img):
                continue
            if rebirther and img is not None:
                rebirther.maybe_start(hwnd, label)  # when due, on its own thread
            if img is None:
                active, left = None, None
            elif detecting is not None and not detecting.get(hwnd, True):
                active, left = False, None  # switched off: don't even look
            else:
                active, left, text = detectors[hwnd].check(img)
                if debug:
                    img.save(DEBUG_DIR / f"{hwnd}_{time.strftime('%H%M%S')}_{int(active)}.png")
            test_left = (simulated or {}).get(hwnd, 0) - time.monotonic()
            if test_left > 0:
                active, left = True, int(test_left + 0.999)
            # The hotbar: where its slots are, and which characters have Dracula. Slots
            # and labels come from the same screenshot, so marks line up with the
            # picture. Labels are read again as soon as the slot icons change (items
            # moved), every few seconds during the event (to notice the held character
            # getting Dracula), and now and then otherwise.
            slots = mutated = None
            held = HotbarEquipper.UNSEEN
            if img is not None and (equipper or on_status):
                found = find_hotbar_slots(img)
                slots = steady.update(hwnd, found)
                if slots is not found:
                    slots = None  # a new layout waiting to be confirmed: skip this frame
            if slots:
                state = hotbars.setdefault(hwnd, {"sig": None, "read_at": -1e9})
                sig = hotbar_signature(img, slots)
                since = time.monotonic() - state["read_at"]
                holding = equipper is not None and hwnd in equipper.equipped
                every = LABELS_EVERY_EVENT if active or holding else LABELS_EVERY
                if (hotbar_changed(state["sig"], sig) and since >= 1.5) or since >= every:
                    mutated = {i for i, text in enumerate(read_slot_labels(img, slots), 1)
                               if is_dracula(text)}
                    state.update(sig=sig, read_at=time.monotonic())
                # Which slot is held, and is it a character (not the rod or a potion)?
                # Only looked at in windows with slots chosen to auto equip. Reading
                # the card is only needed when the held slot changes.
                if equipper and equipper.slots.get(hwnd):
                    pick, held = selected_slot(img, slots), None
                else:
                    pick = None
                if pick is not None:
                    icon = hotbar_signature(img, [slots[pick - 1]])
                    chosen = tuple(equipper.slots.get(hwnd, ()))
                    known = held_cards.get(hwnd)
                    if (known is None or known[0] != pick or hotbar_changed(known[1], icon)
                            or known[2] != chosen):
                        known = held_cards[hwnd] = (pick, icon, chosen,
                                                    is_character_card(img, slots, pick, chosen))
                    held = pick if known[3] else None
            if on_status:
                on_status(hwnd, active, left, img, mutated, slots)
            if equipper:
                equipper.observe(hwnd, label, active, mutated, left, held)
            if active and test_left <= 0:
                showing.append((label, left))
            if active is not None and active != last[hwnd]:
                stamp = time.strftime("%H:%M:%S")
                test = "TEST " if test_left > 0 or (simulated and hwnd in simulated) else ""
                if active:
                    print(f"[{stamp}] {label}: {test}Hallows' Eve STARTED ({format_left(left)})",
                          flush=True)
                elif last[hwnd] is not None:
                    print(f"[{stamp}] {label}: {test}Hallows' Eve ended", flush=True)
                if on_change and (active or last[hwnd] is not None):
                    on_change(hwnd, label, active, left)
                last[hwnd] = active
        if notifier:
            notifier.observe(showing)
        stop.wait(max(0.0, interval - (time.perf_counter() - started)))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

OUTPUT_LOG_PATH = APP_DIR / "detector_output.log"  # messages and errors when there's no console


def log_to_file_without_console():
    """Started without a console (pythonw, the shortcut): messages and errors go to
    OUTPUT_LOG_PATH instead of nowhere. Started over when it passes 1 MB."""
    if sys.stdout is not None:
        return
    try:
        big = OUTPUT_LOG_PATH.exists() and OUTPUT_LOG_PATH.stat().st_size > 1_000_000
        sys.stdout = sys.stderr = open(OUTPUT_LOG_PATH, "w" if big else "a", encoding="utf-8",
                                       buffering=1)
        print(f"--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    except OSError:
        pass


def main():
    log_to_file_without_console()
    ap = argparse.ArgumentParser(description="Detect the Hallows' Eve event in Roblox windows.")
    ap.add_argument("--all", action="store_true", help="watch every Roblox window (no GUI)")
    ap.add_argument("--list", action="store_true", help="list Roblox windows and exit")
    ap.add_argument("--test", nargs="+", metavar="IMAGE", help="run detection on screenshots")
    ap.add_argument("-i", "--interval", type=float, default=1.0,
                    help="seconds between checks of each window (default 1.0)")
    ap.add_argument("--debug", action="store_true", help=f"save every capture to {DEBUG_DIR.name}/")
    ap.add_argument("--hotbar", type=int, choices=range(1, 11), metavar="SLOT", nargs="+",
                    help="with --all: hotbar slots (1-10, 10 = the 0 key) to equip in every "
                         "window while the event is on; the first given without Dracula is held")
    ap.add_argument("--discord-test", action="store_true",
                    help="check the Discord setup and post a test message that pings the role")
    ap.add_argument("--self-test", action="store_true",
                    help="check the OCR engine and the window toolkit load, then exit (for builds)")
    args = ap.parse_args()

    if args.self_test:
        # Used after building the exe: everything it needs must be bundled and load. Exits
        # with 1 (and the error in the output) instead of an error box, so builds can't hang.
        try:
            active, _, _ = EventDetector().check(Image.new("RGB", (640, 360), "white"))
            import detector_ui  # Qt and the window code
            assert detector_ui.App
            import updater
            print(f"self-test ok (OCR ran, event seen: {active}; "
                  f"version {updater.display(updater.installed())})")
            code = 0
        except Exception:
            import traceback
            traceback.print_exc()
            code = 1
        if sys.stdout:
            sys.stdout.flush()
        os._exit(code)

    make_dpi_aware()

    if args.test:
        for path in args.test:
            img = Image.open(path).convert("RGB")
            t = time.perf_counter()
            active, left, text = EventDetector().check(img)
            ms = (time.perf_counter() - t) * 1000
            print(f"{path}: {'ACTIVE  ' + format_left(left) if active else 'not active'}"
                  f"  ({ms:.0f} ms, OCR: {text!r})")
        return

    if args.discord_test:
        notifier, reason = DiscordNotifier.from_config()
        if not notifier:
            sys.exit(f"Discord isn't set up: {reason}.")
        try:
            print(notifier.resolve())
            notifier.send_test()
            print(f"Posted a test message in {notifier.channel_label}.")
        except DiscordError as e:
            sys.exit(f"Discord test failed: {e}")
        return

    windows = find_roblox_windows()
    if args.list:
        for i, (hwnd, pid, title) in enumerate(windows, 1):
            w, h = client_size(hwnd)
            print(f"#{i}  hwnd={hwnd}  PID {pid}  {w}x{h}  {title!r}")
        if not windows:
            print("No Roblox windows found.")
        return

    if args.all:
        if not windows:
            sys.exit("No Roblox windows found.")
        selected = [(hwnd, f"#{i} (PID {pid})") for i, (hwnd, pid, _) in enumerate(windows, 1)]
        print(f"Watching {len(selected)} Roblox window(s). Ctrl+C to stop.")
        notifier, reason = DiscordNotifier.from_config()
        if notifier:
            try:
                print(f"Discord ready. {notifier.resolve()}.")
            except DiscordError as e:
                print(f"Discord not ready: {e}")
        else:
            print(f"Discord pings off: {reason}.")
        stop = threading.Event()
        try:
            equipper = None
            if args.hotbar:
                equipper = HotbarEquipper()
                for hwnd, _ in selected:
                    equipper.set_slots(hwnd, args.hotbar)
                print(f"Will equip {slot_list(list(dict.fromkeys(args.hotbar)))} while the event is on "
                      "(in the order picked, skipping Dracula).")
            monitor(selected, args.interval, args.debug, stop, notifier=notifier, equipper=equipper,
                    popups=PopupCloser())
        except KeyboardInterrupt:
            print("Stopped.")
        quit_now()

    run_gui(args.interval, args.debug)


if __name__ == "__main__":
    main()
