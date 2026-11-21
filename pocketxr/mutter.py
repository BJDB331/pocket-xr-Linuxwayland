"""The screen's picture on GNOME on Wayland (Ubuntu's default desktop), through Mutter, GNOME's compositor.

A Wayland desktop lets no app read the screen or move the pointer by itself (xcapture.py's X11 ways see only black
there). Mutter offers both on the session bus: its screen cast and remote desktop interfaces, the same ones GNOME's
own screen sharing uses, with no dialog. The native helper pxr-mutter (native/mutter/pxr_mutter.c) speaks them and
PipeWire, and puts the newest frame in shared memory; this thread reads it.

The same interface as XCapture, with one source: the main screen as one panel (the "Deck screen"). App windows each
in their own place need a window list Wayland does not give apps; they are not here yet, and asking for them shows
the main screen.
"""
import json
import mmap
import os
import struct
import subprocess
import threading
import time

from .session import MUTTER_HELPER
from .panels import Frame, Panel

HEADER = struct.Struct("<IIIIiiIII")
MAGIC = 0x4D525850
SHM_HEADER = 64
GLASSES_WORDS = ("xreal", "nreal", "viture", "rokid", "rayneo", "air", "ultra", "one pro")


def pick_monitors(monitors, env=None):
    """(the glasses' monitor, the main screen) from the helper's list of monitors that are on; either may be None.
    The glasses: one whose maker or name says so, else any monitor that is not the main one when there are two or
    more. The main screen: the built-in panel, else the primary one, else the first.
    POCKETXR_MAIN_MONITOR (a connector, such as eDP-1) picks the main screen, for testing."""
    env = os.environ if env is None else env
    on = [m for m in monitors if m.get("on")]
    if not on:
        return None, None
    forced = env.get("POCKETXR_MAIN_MONITOR")
    if forced and any(m.get("connector") == forced for m in on):
        main = next(m for m in on if m.get("connector") == forced)
        others = [m for m in on if m is not main]
        return (others[0] if others else None), main

    def says_glasses(m):
        text = " ".join((m.get("vendor", ""), m.get("product", ""), m.get("name", ""))).lower()
        return any(w in text for w in GLASSES_WORDS)

    glasses = next((m for m in on if says_glasses(m)), None)
    rest = [m for m in on if m is not glasses]
    builtin = [m for m in rest if m.get("connector", "").startswith(("eDP", "LVDS", "DSI"))]
    main = (builtin or [m for m in rest if m.get("primary")] or rest or [None])[0]
    if glasses is None:
        others = [m for m in rest if m is not main]
        glasses = others[0] if others else None
    return glasses, main


def match_sdl(monitor, sdl_displays):
    """The SDL display (index, name, x, y, w, h) that is this Mutter monitor: same place, else same size."""
    if monitor is None:
        return None
    for d in sdl_displays:
        if (d[2], d[3]) == (monitor["x"], monitor["y"]):
            return d
    scale = monitor.get("scale") or 1.0
    for d in sdl_displays:
        if (d[4], d[5]) in ((monitor["w"], monitor["h"]), (round(monitor["w"] / scale), round(monitor["h"] / scale))):
            return d
    return None


class MutterCapture(threading.Thread):
    def __init__(self, deck_rect=None, log=print, helper=MUTTER_HELPER):
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
        self.monitors = []
        self.monitors_serial = 0
        self.recording = None  # the connector being recorded
        self.input_ok = False
        self.ready = threading.Event()  # the first list of monitors has come
        self.failed = None  # why the helper could not do its work, for the log and the panel
        self._proc = None
        self._shm = None
        self._seq = None
        self._frame_number = None
        self._serial = 0
        self._cursor = None
        self._last_pull = 0.0
        self._said_apps = False

    # ---- the helper ----
    def _start_helper(self):
        try:
            self._proc = subprocess.Popen([self.helper], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                          bufsize=1)
        except OSError as e:
            self.failed = f"pxr-mutter: {e}"
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

    def _read_helper(self):
        for line in self._proc.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                self.log("capture: helper: " + line.strip())
                continue
            if "monitors" in m:
                with self.lock:
                    self.monitors = m["monitors"]
                    self.monitors_serial += 1
                self.log("capture: monitors " + ", ".join(
                    f"{x['connector']} '{x.get('name') or x.get('product')}' {x['w']}x{x['h']} at {x['x']},{x['y']}"
                    f"{' primary' if x.get('primary') else ''}{'' if x.get('on') else ' (off)'}" for x in m["monitors"]))
                self.ready.set()
            elif "shm" in m:
                self.input_ok = bool(m.get("input"))
                self._open_shm(m["shm"])
                self.log(f"capture: recording {m.get('connector')} through Mutter"
                         f"{'' if self.input_ok else ' (no pointer moves: no remote desktop session)'}")
            elif "error" in m:
                self.errors += 1
                self.error_kinds[m["error"][:60]] = self.error_kinds.get(m["error"][:60], 0) + 1
                self.log("capture: " + m["error"])
                if m["error"].startswith(("ScreenCast", "RecordMonitor", "Start", "DisplayConfig", "session bus")):
                    self.failed = m["error"]
                    self.ready.set()
            elif "stream" in m:
                self.log(f"capture: stream {m['stream']}{(': ' + m['why']) if m.get('why') else ''}")
            elif "format" in m:
                self.log(f"capture: frames {m['format']['w']}x{m['format']['h']}, format {m['format']['format']}")
            elif "closed" in m:
                self.log("capture: Mutter closed the screen cast")
                self.recording = None
        self.log("capture: the helper ended")
        self.ready.set()

    def _open_shm(self, path):
        try:
            with open(path, "rb") as f:
                self._shm = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
        except OSError as e:
            self.log(f"capture: shared memory {path}: {e}")

    def _pull(self):
        """The newest frame (when it changed) and the pointer from shared memory: (Frame or None, (x, y) or None).
        The helper bumps seq around each write (odd while writing), and the frame number only with a new picture,
        so a pointer move costs no copy of the picture."""
        shm = self._shm
        if shm is None:
            return None, None
        for _ in range(3):
            magic, seq, w, h, cx, cy, seen, cap, number = HEADER.unpack_from(shm, 0)
            if magic != MAGIC or seq & 1:
                time.sleep(0.001)
                continue
            if seq == self._seq:
                return None, None
            cursor = (cx, cy) if seen else None
            if number == self._frame_number or w == 0 or h == 0 or w * h * 4 > cap:
                if HEADER.unpack_from(shm, 0)[1] != seq:
                    continue
                self._seq = seq
                return None, cursor
            data = shm[SHM_HEADER:SHM_HEADER + w * h * 4]
            if HEADER.unpack_from(shm, 0)[1] != seq:
                continue
            self._seq = seq
            self._frame_number = number
            self._serial += 1
            return Frame(data, w, h, self._serial), cursor
        return None, None

    # ---- for the app (the same as XCapture) ----
    def snapshot(self):
        with self.lock:
            return list(self.panels)

    def install_handler(self):
        pass

    def activate(self, key):
        pass

    def raise_window(self, key):
        pass

    def close_window(self, key):
        pass

    def key_to(self, key, combo):
        pass

    def resize_app(self, key, cw, ch):
        pass

    def restore_all(self):
        self.restored = True
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
        """(the glasses' SDL display, the main screen's) as Mutter names them."""
        with self.lock:
            mons = list(self.monitors)
        g, main = pick_monitors(mons)
        return match_sdl(g, sdl_displays), match_sdl(main, sdl_displays)

    def view_pointer(self):
        """The real pointer went onto the glasses' monitor (the view got pointer motion): put it back on the main
        screen, where it was, as XCapture keeps it off the glasses' output."""
        p = self.panels[0] if self.panels else None
        w, h = (p.w, p.h) if p else (0, 0)
        if not self.input_ok or not w:
            return
        # Where it last was on the main screen, or its middle when it has not been there since the start.
        x, y = self._cursor if self._cursor is not None else (w // 2, h // 2)
        if w and h:
            x, y = min(max(x, 0), w - 2), min(max(y, 0), h - 1)
        now = time.monotonic()
        if now - getattr(self, "_last_back", 0.0) > 0.05:
            self._last_back = now
            self._send(f"move {x} {y}")
            if not getattr(self, "_said_back", False):
                self._said_back = True
                self.log(f"capture: the pointer went onto the glasses' monitor; put back on the main screen at {x},{y}")

    # ---- the thread ----
    def run(self):
        if not self._start_helper():
            return
        self.ready.wait(10)
        panel = Panel("deck", "Screen")
        panel.active = True
        last_serial = -1
        while not self.stop:
            with self.lock:
                serial = self.monitors_serial
                mons = list(self.monitors)
            if serial != last_serial:
                last_serial = serial
                _, main = pick_monitors(mons)
                want = main["connector"] if main else None
                if want and want != self.recording:
                    self.recording = want
                    self._seq = self._frame_number = None
                    self._send(f"record {want}")
            if self.source == "apps" and not self._said_apps:
                self._said_apps = True
                self.log("capture: app windows each in their own place need X11 for now; showing the screen")
            frame, cursor = self._pull()
            if frame is not None:
                panel.frame = frame
                panel.w, panel.h = frame.w, frame.h
                if not self.panels:
                    with self.lock:
                        self.panels = [panel]
            if cursor is not None:
                self._cursor = cursor
            c = self._cursor
            if c is not None and panel.w:
                inside = 0 <= c[0] < panel.w and 0 <= c[1] < panel.h
                panel.pointer = c if inside else None
                self.pointer_at_right_edge = inside and c[0] >= panel.w - 1
            time.sleep(1 / 120)
        self._send("quit")

    def close(self):
        self.stop = True
        self._send("quit")
