"""The screen's and app windows' pictures on KDE Plasma on Wayland, through KWin, Plasma's compositor.

SteamOS 3.9 starts Desktop Mode on Plasma's Wayland session, as do Bazzite, CachyOS, Fedora KDE and Kubuntu. There
no app may read the screen, list other apps' windows or move the pointer by itself (xcapture.py's X11 ways see only
Xwayland's own windows). KWin offers all three to a program a desktop file names in X-KDE-Wayland-Interfaces, with no
dialog: its screen cast (a monitor's or one window's picture, as a PipeWire stream), Plasma's window list, and fake
input. The native helper pxr-kwin (native/kwin/pxr_kwin.c) speaks them and puts each picture in its own shared memory
file; this thread reads them. install.sh writes the desktop file (pocket-xr-kwin.desktop).

The same interface as XCapture. Deck screen: the main monitor as one panel. App windows: each app window as its own
panel, its picture straight from KWin wherever it is on the main screen, so nothing is moved: KWin draws a window's
picture even when another window covers it. Looking at one brings it to the front (Plasma's window list), so touch,
the trackpads and the keyboard reach it. App windows do not get their own larger screens on the glasses' monitor as
on X11 (.virtual stays False): on Wayland the view cannot let clicks through to windows under it.
"""
import json
import mmap
import os
import struct
import subprocess
import threading
import time

from .mutter import match_sdl, pick_monitors
from .panels import Frame, Panel
from .session import KWIN_HELPER

HEADER = struct.Struct("<IIIIiiIII")
MAGIC = 0x4D525850
SHM_HEADER = 64

# org_kde_plasma_window_management's state bits
ACTIVE, MINIMIZED, SKIP_TASKBAR, SKIP_SWITCHER = 1 << 0, 1 << 1, 1 << 12, 1 << 18
OWN_TITLES = ("Pocket XR Deck", "Pocket XR")
SKIP_APPS = ("plasmashell", "org.kde.plasmashell", "krunner", "org.kde.krunner", "xwaylandvideobridge",
             "org.kde.xwaylandvideobridge", "steamwebhelper-notification", "org.kde.polkit-kde-authentication-agent-1",
             "org.kde.spectacle", "kwin", "org.kde.kwin")
MAX_WINDOWS = 12
FPS_ACTIVE = 60.0
FPS_OTHER = 15.0
LIVE_HOLD_S = 1.5

# Key names (as key_to's "alt+Left") to Linux evdev key codes, which KWin's fake input takes.
EVDEV = {"esc": 1, "escape": 1, "backspace": 14, "tab": 15, "return": 28, "enter": 28, "ctrl": 29, "control_l": 29,
         "shift": 42, "shift_l": 42, "alt": 56, "alt_l": 56, "space": 57, "super": 125, "super_l": 125, "meta": 125,
         "home": 102, "up": 103, "pageup": 104, "prior": 104, "left": 105, "right": 106, "end": 107, "down": 108,
         "pagedown": 109, "next": 109, "insert": 110, "delete": 111}
EVDEV.update({f"f{i}": 58 + i for i in range(1, 11)})
EVDEV.update({"f11": 87, "f12": 88})
EVDEV.update({c: code for row, start in (("1234567890", 2), ("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44))
              for c, code in zip(row, range(start, start + len(row)))})


def key_codes(combo):
    """"alt+Left" -> [56, 105]; None when a key has no code here."""
    codes = []
    for k in combo.split("+"):
        c = EVDEV.get(k.strip().lower())
        if c is None:
            return None
        codes.append(c)
    return codes


def wanted_windows(windows, own_pids=(), limit=MAX_WINDOWS):
    """The app windows to show, from pxr-kwin's list: not Pocket XR's own, not Plasma's shell, not minimised, not
    hidden from the task bar, not tiny. In the list's order (oldest first), at most `limit`."""
    out = []
    for w in windows:
        st = w.get("state", 0)
        app = (w.get("app_id") or "").lower()
        res = (w.get("resource") or "").lower()
        if w.get("pid") in own_pids or w.get("title") in OWN_TITLES:
            continue
        if st & (MINIMIZED | SKIP_TASKBAR):
            continue
        if app in SKIP_APPS or res in SKIP_APPS:
            continue
        if w.get("w", 0) < 32 or w.get("h", 0) < 32:
            continue
        out.append(w)
    return out[:limit]


def top_under(keys, stacking):
    """Of the windows that say the pointer is on them, the top one in KWin's stacking order (bottom to top)."""
    keys = list(keys)
    if len(keys) <= 1:
        return keys[0] if keys else None
    order = {u: i for i, u in enumerate(stacking)}
    return max(keys, key=lambda k: order.get(k, -1))


def global_point(monitor, x, y):
    """A point in a monitor's picture pixels to the desktop's logical pixels, where fake input moves the pointer."""
    scale = monitor.get("scale") or 1.0
    return monitor["x"] + x / scale, monitor["y"] + y / scale


class ShmFrames:
    """One picture's shared memory file from pxr-kwin: the newest frame and the pointer's place on it."""

    def __init__(self, path):
        with open(path, "rb") as f:
            self.m = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
        self.path = path
        self._seq = None
        self._number = None
        self.cursor = None  # (x, y) on this picture, or None when the pointer is elsewhere
        self.cursor_ever = False  # KWin has sent the pointer's place for this picture at least once

    def pull(self, serial, picture=True):
        """A new Frame (or None) and updates .cursor. The helper bumps seq around each write (odd while writing)."""
        m = self.m
        for _ in range(3):
            magic, seq, w, h, cx, cy, seen, cap, number = HEADER.unpack_from(m, 0)
            if magic != MAGIC or seq & 1:
                time.sleep(0.0005)
                continue
            if seq == self._seq:
                return None
            cursor = (cx, cy) if seen else None
            if not picture and number != self._number:
                # A new picture that is not wanted yet (its rate): left for the next pull, the pointer taken now.
                self._set_cursor(cursor, cx)
                return None
            if number == self._number or w == 0 or h == 0 or w * h * 4 > cap:
                if HEADER.unpack_from(m, 0)[1] != seq:
                    continue
                self._seq = seq
                self._set_cursor(cursor, cx)
                return None
            data = m[SHM_HEADER:SHM_HEADER + w * h * 4]
            if HEADER.unpack_from(m, 0)[1] != seq:
                continue
            self._seq = seq
            self._number = number
            self._set_cursor(cursor, cx)
            return Frame(data, w, h, serial)
        return None

    def _set_cursor(self, cursor, cx):
        self.cursor = cursor
        if cursor is not None or cx != -1:
            self.cursor_ever = True

    def close(self):
        try:
            self.m.close()
        except (BufferError, ValueError):
            pass


class KWinCapture(threading.Thread):
    def __init__(self, deck_rect=None, log=print, helper=KWIN_HELPER):
        super().__init__(daemon=True)
        self.log = log
        self.helper = helper
        self.deck_rect = deck_rect
        self.glasses_rect = None
        self.source = "deck"
        self.lock = threading.Lock()
        self.panels = []
        self.stop = False
        self.errors = 0
        self.error_kinds = {}
        self.space_pointer = False
        self.pointer_on_glasses = False
        self.pointer_at_right_edge = False
        self.restored = True
        self.composite = False
        self.panel_pid = None
        self.own_pid = os.getpid()
        self.monitors = []
        self.monitors_serial = 0
        self.windows = []
        self.stacking = []
        self.windows_serial = 0
        self.caps = {}
        self.input_ok = False
        self.ready = threading.Event()
        self.failed = None
        self._proc = None
        self._shm = {}  # stream key -> ShmFrames
        self._recording = {}  # stream key -> what ("output eDP-1", "window {uuid}")
        self._failed_keys = {}  # stream key -> when KWin refused it, so it is not asked again at once
        self._panels = {}
        self._serial = 0
        self._main = None
        self._last_frame = {}
        self._sample = {}
        self._live_until = {}
        self._said = set()

    # ---- the helper ----
    def _start_helper(self):
        try:
            self._proc = subprocess.Popen([self.helper], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                          bufsize=1)
        except OSError as e:
            self.failed = f"pxr-kwin: {e}"
            self.log("capture: " + self.failed)
            self.ready.set()
            return False
        threading.Thread(target=self._read_helper, daemon=True).start()
        return True

    def _send(self, line):
        p = self._proc
        if p is None or p.poll() is not None:
            return
        try:
            p.stdin.write(line + "\n")
            p.stdin.flush()
        except (OSError, ValueError):
            pass

    def _say_once(self, what, text):
        if what not in self._said:
            self._said.add(what)
            self.log(text)

    def _read_helper(self):
        for line in self._proc.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                self.log("capture: helper: " + line.strip())
                continue
            self._on_message(m)
        self.log("capture: the helper ended")
        if self.failed is None and not self._shm:
            self.failed = "pxr-kwin ended"
        self.ready.set()

    def _on_message(self, m):
        if "caps" in m:
            self.caps = m["caps"]
            self.input_ok = bool(self.caps.get("input"))
            self.log(f"capture: KWin offers screen cast v{self.caps.get('screencast', 0)}, window list "
                     f"v{self.caps.get('windows', 0)}, fake input v{self.caps.get('input', 0)}")
            if not self.caps.get("screencast"):
                self.failed = ("KWin did not offer its screen cast to pxr-kwin (its desktop file "
                               "pocket-xr-kwin.desktop is missing: run install.sh again)")
            if not self.caps.get("windows"):
                self.log("capture: no window list from KWin: App windows show the screen")
            if not self.caps.get("input"):
                self.log("capture: no fake input from KWin: the pointer is not kept off the glasses' monitor")
        elif "monitors" in m:
            with self.lock:
                self.monitors = m["monitors"]
                self.monitors_serial += 1
            self.log("capture: monitors " + ", ".join(
                f"{x['connector']} '{x.get('name') or x.get('product')}' {x['w']}x{x['h']} at {x['x']},{x['y']}"
                f" scale {x.get('scale', 1):g}" for x in m["monitors"]))
            self.ready.set()
        elif "windows" in m:
            with self.lock:
                self.windows = m["windows"]
                self.stacking = m.get("stacking") or []
                self.windows_serial += 1
        elif "shm" in m:
            key = m.get("key")
            try:
                shm = ShmFrames(m["shm"])
            except OSError as e:
                self.log(f"capture: shared memory {m['shm']}: {e}")
                return
            with self.lock:
                old = self._shm.pop(key, None)
                self._shm[key] = shm
            if old:
                old.close()
            self._say_once(("shm", m.get("source", "")[:7]), f"capture: picture of {m.get('source')} through KWin")
        elif "failed" in m:
            key = m["failed"]
            self.errors += 1
            self.error_kinds[m.get("error", "")[:60]] = self.error_kinds.get(m.get("error", "")[:60], 0) + 1
            self.log(f"capture: KWin refused the picture {key}: {m.get('error')}")
            with self.lock:
                self._failed_keys[key] = time.monotonic()
                self._recording.pop(key, None)
                shm = self._shm.pop(key, None)
            if shm:
                shm.close()
            if key == "deck":
                self.failed = m.get("error")
                self.ready.set()
        elif "closed" in m:
            key = m["closed"]
            with self.lock:
                self._recording.pop(key, None)
                shm = self._shm.pop(key, None)
            if shm:
                shm.close()
            if key == "deck":
                self.log("capture: KWin closed the screen's picture")
        elif "error" in m:
            self.errors += 1
            self.error_kinds[m["error"][:60]] = self.error_kinds.get(m["error"][:60], 0) + 1
            self.log("capture: " + m["error"])
            if m["error"].startswith(("Wayland", "KWin did not")):
                self.failed = m["error"]
                self.ready.set()
        elif "stream" in m:
            if m["stream"] in ("error", "unconnected") or m.get("key") == "deck":
                self.log(f"capture: stream {m.get('key')} {m['stream']}{(': ' + m['why']) if m.get('why') else ''}")
        elif "format" in m:
            if m.get("key") == "deck":
                self.log(f"capture: frames {m['format']['w']}x{m['format']['h']}, format {m['format']['format']}")

    def _record(self, key, what):
        if self._recording.get(key) == what:
            return
        t = self._failed_keys.get(key)
        if t is not None and time.monotonic() - t < 10:
            return
        self._recording[key] = what
        self._send(f"record {key} {what}")

    def _stop_stream(self, key):
        if key in self._recording:
            del self._recording[key]
            self._send(f"stop {key}")
        with self.lock:
            shm = self._shm.pop(key, None)
        if shm:
            shm.close()

    # ---- for the app (the same as XCapture) ----
    def snapshot(self):
        with self.lock:
            return list(self.panels)

    def install_handler(self):
        pass

    def activate(self, key):
        """Bring an app window to the front on the main screen, so touch, the trackpads and the keyboard reach it."""
        if isinstance(key, str) and key != "deck":
            self._send(f"activate {key}")

    def raise_window(self, key):
        # Wayland has no "on top without focus": the window is made active, as a click on it would.
        self.activate(key)

    def close_window(self, key):
        if isinstance(key, str) and key != "deck":
            self._send(f"close {key}")

    def key_to(self, key, combo):
        """Send a key combination ("alt+Left") to an app window: it is made active first."""
        codes = key_codes(combo)
        if codes is None or not self.input_ok:
            return

        def go():
            self.activate(key)
            time.sleep(0.08)
            for c in codes:
                self._send(f"key {c} 1")
            for c in reversed(codes):
                self._send(f"key {c} 0")
        threading.Thread(target=go, daemon=True).start()

    def resize_app(self, key, cw, ch):
        pass  # only for app windows on their own screens (.virtual), not on Wayland

    def restore_all(self):
        self.restored = True  # nothing was moved
        self.closing = True

    def take_motion(self):
        return [0, 0]

    def leave_space(self):
        pass

    def chrome_spot(self):
        return None

    @property
    def virtual(self):
        return False

    def split(self, sdl_displays):
        """(the glasses' SDL display, the main screen's) as KWin names the monitors."""
        with self.lock:
            mons = list(self.monitors)
        g, main = pick_monitors(mons)
        return match_sdl(g, sdl_displays), match_sdl(main, sdl_displays)

    def view_pointer(self):
        """The real pointer went onto the glasses' monitor (the view got pointer motion): put it back on the main
        screen where it last was, as XCapture keeps it off the glasses' output."""
        main = self._main
        if not self.input_ok or main is None:
            return
        shm = self._shm.get("deck")
        c = shm.cursor if shm is not None else None
        w, h = main["w"], main["h"]
        x, y = c if c is not None else getattr(self, "_last_cursor", (w // 2, h // 2))
        x, y = min(max(x, 0), w - 2), min(max(y, 0), h - 1)
        gx, gy = global_point(main, x, y)
        now = time.monotonic()
        if now - getattr(self, "_last_back", 0.0) > 0.05:
            self._last_back = now
            self._send(f"move {gx:.1f} {gy:.1f}")
            self._say_once("back", f"capture: the pointer went onto the glasses' monitor; put back on the main "
                                   f"screen at {gx:.0f},{gy:.0f}")

    # ---- the thread ----
    def _own_pids(self):
        return {p for p in (self.own_pid, self.panel_pid) if p}

    def _window_pointer(self, key, win, shm, deck_cursor):
        """Where the pointer is on this window's picture: KWin's own metadata for it, else worked out from the
        pointer on the main screen and the window's place (KWin versions that send no pointer for windows)."""
        if shm.cursor_ever:
            return shm.cursor
        p = self._panels.get(key)
        main = self._main
        if deck_cursor is None or main is None or p is None or not p.w or not win.get("w"):
            return None
        gx, gy = global_point(main, *deck_cursor)
        fx, fy, fw, fh = win["x"], win["y"], win["w"], win["h"]
        scale = main.get("scale") or 1.0
        # The picture is the whole frame (title bar and all) or the window's inside: its inside sits at the bottom
        # of the frame, with the same border on the left, right and bottom.
        cw, ch = p.w / scale, p.h / scale
        side = max(0.0, (fw - cw) / 2)
        top = max(0.0, fh - ch - side)
        u, v = (gx - fx - side) * scale, (gy - fy - top) * scale
        return (int(u), int(v)) if 0 <= u < p.w and 0 <= v < p.h else None

    def run(self):
        if not self._start_helper():
            return
        self.ready.wait(10)
        last_mons = last_wins = -1
        while not self.stop:
            now = time.monotonic()
            with self.lock:
                mserial, wserial = self.monitors_serial, self.windows_serial
                mons, wins, stacking = list(self.monitors), list(self.windows), list(self.stacking)
            if mserial != last_mons:
                last_mons = mserial
                _, self._main = pick_monitors(mons)
                if self._main:
                    self._record("deck", f"output {self._main['connector']}")
            apps_on = self.source == "apps" and bool(self.caps.get("windows"))
            if self.source == "apps" and not apps_on:
                self._say_once("noapps", "capture: no window list from KWin; showing the screen")
            want = wanted_windows(wins, self._own_pids()) if apps_on else []
            if (wserial, apps_on) != last_wins:  # a new list, or Deck screen and App windows switched
                last_wins = (wserial, apps_on)
                keys = {w["uuid"] for w in want}
                for k in [k for k in self._recording if k != "deck" and k not in keys]:
                    self._stop_stream(k)
                for w in want:
                    self._record(w["uuid"], f"window {w['uuid']}")
            # The main screen's picture only when it is shown (no app window has a picture yet); its pointer always.
            deck_shown = not (want and getattr(self, "_have_windows", False))
            if getattr(self, "_deck_pictures", True) != deck_shown:
                self._deck_pictures = deck_shown
                self._send(f"pictures deck {1 if deck_shown else 0}")
            self._frames(now, want, stacking, deck_shown)
            time.sleep(1 / 240)
        self._send("quit")

    def _frames(self, now, want, stacking, deck_shown):
        with self.lock:
            shms = dict(self._shm)
        deck_shm = shms.get("deck")
        deck = self._panels.get("deck") or Panel("deck", "Screen")
        deck.active = True
        if deck_shm is not None:
            self._serial += 1
            f = deck_shm.pull(self._serial, picture=deck_shown)
            if f is not None:
                deck.frame = f
                deck.w, deck.h = f.w, f.h
            if deck_shm.cursor is not None:
                self._last_cursor = deck_shm.cursor
        deck_cursor = deck_shm.cursor if deck_shm is not None else None
        if deck.w and deck_cursor is not None:
            inside = 0 <= deck_cursor[0] < deck.w and 0 <= deck_cursor[1] < deck.h
            deck.pointer = deck_cursor if inside else None
            self.pointer_at_right_edge = inside and deck_cursor[0] >= deck.w - 1
        else:
            deck.pointer = None
        keep = {}
        claims = {}
        for w in want:
            key = w["uuid"]
            shm = shms.get(key)
            if shm is None:
                continue
            p = self._panels.get(key) or Panel(key, w.get("title", ""))
            p.title = w.get("title", "")
            p.cls = w.get("resource") or w.get("app_id") or p.cls
            p.active = bool(w.get("state", 0) & ACTIVE)
            p.root_x, p.root_y = w.get("x", 0), w.get("y", 0)
            live = now < self._live_until.get(key, 0.0)
            rate = FPS_ACTIVE if (p.active or live) else FPS_OTHER
            due = now - self._last_frame.get(key, 0.0) >= 1.0 / rate
            self._serial += 1
            f = shm.pull(self._serial, picture=due)
            if f is not None:
                self._last_frame[key] = now
                sample = f.data[::max(4, len(f.data) // 509)]
                if self._sample.get(key) not in (None, sample):
                    self._live_until[key] = now + LIVE_HOLD_S
                self._sample[key] = sample
                p.frame = f
                p.w, p.h = f.w, f.h
            if p.frame is None:
                continue  # no picture yet: not shown
            keep[key] = p
            c = self._window_pointer(key, w, shm, deck_cursor)
            p.pointer = None
            if c is not None:
                claims[key] = c
        top = top_under(claims.keys(), stacking)
        if top is not None:
            keep[top].pointer = claims[top]
        self._have_windows = bool(keep)
        if not keep:
            # Deck screen, or no app windows to show yet: the main screen rather than nothing.
            keep = {"deck": deck} if deck.frame is not None else {}
        self._panels = dict(keep, deck=deck)
        with self.lock:
            self.panels = list(keep.values())

    def close(self):
        self.stop = True
        self._send("quit")
