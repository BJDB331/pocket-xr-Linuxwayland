"""The phone's mouse, wheel, keys and text, from the control stream's input messages (moonlight-common-c's Input.h: a
big-endian size, a little-endian type, then the packet), made into Linux input through uinput, as Sunshine does on
Linux (GPLv3). SteamOS gives the signed-in user /dev/uinput.

Three devices: a keyboard, a mouse (relative moves, buttons, wheels) and an absolute pointer whose range covers the
whole desktop (every screen and virtual output; KWin maps an absolute pointer over that), so a position on the phone
lands on its PC screen's own virtual output.
"""
import os
import struct
import threading
import time

# ---- the protocol ----

KEY_DOWN, KEY_UP, ABS_MOVE, REL_MOVE, BUTTON_DOWN, BUTTON_UP, SCROLL, TEXT, HSCROLL = \
    0x03, 0x04, 0x05, 0x07, 0x08, 0x09, 0x0A, 0x17, 0x55000001


def parse(data):
    """The input packets in one control message's input payload: [(type, packet bytes)]."""
    out = []
    at = 0
    while len(data) - at >= 8:
        size = struct.unpack_from(">I", data, at)[0]  # excluding this field
        if size < 4 or at + 4 + size > len(data):
            break
        kind = struct.unpack_from("<I", data, at + 4)[0]
        out.append((kind, bytes(data[at + 8:at + 4 + size])))
        at += 4 + size
    return out


# ---- Linux input codes ----

EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
REL_X, REL_Y, REL_HWHEEL, REL_WHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0, 1, 6, 8, 11, 12
ABS_X, ABS_Y = 0, 1
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA = 0x110, 0x111, 0x112, 0x113, 0x114
ABS_RANGE = 65535

# Windows virtual keys (the phone sends US-layout key codes) to Linux key codes, as Sunshine's keyboard table.
VK_TO_KEY = {
    0x08: 14, 0x09: 15, 0x0C: 0, 0x0D: 28, 0x10: 42, 0x11: 29, 0x12: 56, 0x13: 119, 0x14: 58, 0x1B: 1, 0x20: 57,
    0x21: 104, 0x22: 109, 0x23: 107, 0x24: 102, 0x25: 105, 0x26: 103, 0x27: 106, 0x28: 108, 0x2C: 99, 0x2D: 110,
    0x2E: 111, 0x2F: 138,
    0x30: 11, 0x31: 2, 0x32: 3, 0x33: 4, 0x34: 5, 0x35: 6, 0x36: 7, 0x37: 8, 0x38: 9, 0x39: 10,
    0x41: 30, 0x42: 48, 0x43: 46, 0x44: 32, 0x45: 18, 0x46: 33, 0x47: 34, 0x48: 35, 0x49: 23, 0x4A: 36, 0x4B: 37,
    0x4C: 38, 0x4D: 50, 0x4E: 49, 0x4F: 24, 0x50: 25, 0x51: 16, 0x52: 19, 0x53: 31, 0x54: 20, 0x55: 22, 0x56: 47,
    0x57: 17, 0x58: 45, 0x59: 21, 0x5A: 44,
    0x5B: 125, 0x5C: 126, 0x5D: 127, 0x5F: 142,
    0x60: 82, 0x61: 79, 0x62: 80, 0x63: 81, 0x64: 75, 0x65: 76, 0x66: 77, 0x67: 71, 0x68: 72, 0x69: 73,
    0x6A: 55, 0x6B: 78, 0x6C: 121, 0x6D: 74, 0x6E: 83, 0x6F: 98,
    0x70: 59, 0x71: 60, 0x72: 61, 0x73: 62, 0x74: 63, 0x75: 64, 0x76: 65, 0x77: 66, 0x78: 67, 0x79: 68, 0x7A: 87,
    0x7B: 88, 0x7C: 183, 0x7D: 184, 0x7E: 185, 0x7F: 186, 0x80: 187, 0x81: 188, 0x82: 189, 0x83: 190, 0x84: 191,
    0x85: 192, 0x86: 193, 0x87: 194,
    0x90: 69, 0x91: 70,
    0xA0: 42, 0xA1: 54, 0xA2: 29, 0xA3: 97, 0xA4: 56, 0xA5: 100,
    0xA6: 158, 0xA7: 159, 0xA8: 173, 0xA9: 128, 0xAA: 217, 0xAB: 156, 0xAC: 172,
    0xAD: 113, 0xAE: 114, 0xAF: 115, 0xB0: 163, 0xB1: 165, 0xB2: 166, 0xB3: 164,
    0xBA: 39, 0xBB: 13, 0xBC: 51, 0xBD: 12, 0xBE: 52, 0xBF: 53, 0xC0: 41,
    0xDB: 26, 0xDC: 43, 0xDD: 27, 0xDE: 40, 0xE2: 86,
}

# Text the phone types, on a US layout: (key, with shift).
_US = {}
for _c, _k in zip("1234567890", [2, 3, 4, 5, 6, 7, 8, 9, 10, 11]):
    _US[_c] = (_k, False)
for _c, _k in zip("!@#$%^&*()", [2, 3, 4, 5, 6, 7, 8, 9, 10, 11]):
    _US[_c] = (_k, True)
for _c, _k in zip("qwertyuiop", [16, 17, 18, 19, 20, 21, 22, 23, 24, 25]):
    _US[_c] = (_k, False)
    _US[_c.upper()] = (_k, True)
for _c, _k in zip("asdfghjkl", [30, 31, 32, 33, 34, 35, 36, 37, 38]):
    _US[_c] = (_k, False)
    _US[_c.upper()] = (_k, True)
for _c, _k in zip("zxcvbnm", [44, 45, 46, 47, 48, 49, 50]):
    _US[_c] = (_k, False)
    _US[_c.upper()] = (_k, True)
for _c, _k, _s in [(" ", 57, False), ("\n", 28, False), ("\t", 15, False), ("-", 12, False), ("_", 12, True),
                   ("=", 13, False), ("+", 13, True), ("[", 26, False), ("{", 26, True), ("]", 27, False),
                   ("}", 27, True), ("\\", 43, False), ("|", 43, True), (";", 39, False), (":", 39, True),
                   ("'", 40, False), ('"', 40, True), ("`", 41, False), ("~", 41, True), (",", 51, False),
                   ("<", 51, True), (".", 52, False), (">", 52, True), ("/", 53, False), ("?", 53, True)]:
    _US[_c] = (_k, _s)
KEY_LEFTSHIFT = 42


def text_keys(text):
    """(key, shift) for each character of [text] typed on a US layout; characters with no key are left out."""
    return [_US[c] for c in text if c in _US]


# ---- uinput ----

UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT, UI_SET_ABSBIT = 0x40045564, 0x40045565, 0x40045566, 0x40045567
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502


class UinputDevice:
    def __init__(self, name, keys=(), rels=(), abs_range=None):
        import fcntl  # Linux only (the tests run elsewhere too)
        self.fcntl = fcntl
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_SYN)
            if keys:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
                for k in keys:
                    fcntl.ioctl(self.fd, UI_SET_KEYBIT, k)
            if rels:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
                for r in rels:
                    fcntl.ioctl(self.fd, UI_SET_RELBIT, r)
            absmax = [0] * 64
            if abs_range:
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
                for a in (ABS_X, ABS_Y):
                    fcntl.ioctl(self.fd, UI_SET_ABSBIT, a)
                    absmax[a] = abs_range
            # struct uinput_user_dev: name[80], input_id (bustype, vendor, product, version), ff_effects_max,
            # absmax[64], absmin[64], absfuzz[64], absflat[64]
            dev = struct.pack("80sHHHHi", name.encode()[:79], 0x06, 0x1209, 0x5058, 1, 0)
            dev += struct.pack("64i", *absmax) + struct.pack("64i", *([0] * 64)) * 3
            os.write(self.fd, dev)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise

    def emit(self, events):
        """[(type, code, value)], then a sync."""
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1e6)
        data = b"".join(struct.pack("llHHi", sec, usec, t, c, v) for t, c, v in events)
        data += struct.pack("llHHi", sec, usec, EV_SYN, 0, 0)
        try:
            os.write(self.fd, data)
        except BlockingIOError:
            pass

    def close(self):
        try:
            self.fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self.fd)


_BUTTONS = {1: BTN_LEFT, 2: BTN_MIDDLE, 3: BTN_RIGHT, 4: BTN_SIDE, 5: BTN_EXTRA}


class Inputs:
    """The three devices, made at first use and shared by every PC screen."""

    def __init__(self, log=print, desktop=lambda: (0, 0, 1920, 1080)):
        self.log = log
        self.desktop = desktop  # the whole desktop's (x, y, w, h), logical pixels
        self.lock = threading.Lock()
        self.keyboard = self.mouse = self.pointer = None
        self.failed = False
        self.sent = 0
        self.wheel = [0, 0]
        self.pos = None  # where the pointer was put last, desktop pixels
        self.last_abs = None

    def start(self):
        """The devices made now, at the host's start: KWin takes a moment to open a new device, and what it is sent
        before then is lost."""
        with self.lock:
            return self._devices()

    def _devices(self):
        if self.keyboard or self.failed:
            return not self.failed
        try:
            self.keyboard = UinputDevice("Pocket XR host keyboard", keys=sorted(set(VK_TO_KEY.values()) - {0}))
            self.mouse = UinputDevice("Pocket XR host mouse", keys=list(_BUTTONS.values()),
                                      rels=[REL_X, REL_Y, REL_WHEEL, REL_HWHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES])
            self.pointer = UinputDevice("Pocket XR host pointer", keys=list(_BUTTONS.values()), abs_range=ABS_RANGE)
            self.log("host: input devices made (uinput)")
            return True
        except OSError as e:
            self.failed = True
            self.log("host: no input: /dev/uinput: %s" % e)
            return False

    def close(self):
        for d in (self.keyboard, self.mouse, self.pointer):
            if d:
                d.close()
        self.keyboard = self.mouse = self.pointer = None

    def put(self, x, y):
        """The pointer to desktop position (x, y), logical pixels."""
        dx, dy, dw, dh = self.desktop()
        ax = max(0, min(ABS_RANGE, int(round((x - dx) * ABS_RANGE / max(1, dw - 1)))))
        ay = max(0, min(ABS_RANGE, int(round((y - dy) * ABS_RANGE / max(1, dh - 1)))))
        if (ax, ay) == self.last_abs:
            # The kernel drops a position equal to the device's last one, but the pointer may have moved since (the
            # Deck's own touchpad): one unit off first, so this one counts (20 Nov 2026: a centring that repeated the
            # last one did nothing).
            self.pointer.emit([(EV_ABS, ABS_X, ax - 1 if ax > 0 else ax + 1)])
        self.pointer.emit([(EV_ABS, ABS_X, ax), (EV_ABS, ABS_Y, ay)])
        self.last_abs = (ax, ay)
        self.pos = (x, y)

    def handle(self, payload, area):
        """One control message's input payload, for a PC screen whose output is at [area] (x, y, w, h)."""
        with self.lock:
            if not self._devices():
                return
            for kind, p in parse(payload):
                try:
                    self._one(kind, p, area)
                    self.sent += 1
                except (OSError, struct.error) as e:
                    self.log("host: input 0x%x: %s" % (kind, e))

    def centre(self, area):
        with self.lock:
            if self._devices():
                x, y, w, h = area
                self.put(x + w / 2.0, y + h / 2.0)

    def _one(self, kind, p, area):
        ax, ay, aw, ah = area
        if kind == ABS_MOVE and len(p) >= 10:
            x, y, _, w, h = struct.unpack_from(">hhhhh", p)
            w, h = max(2, w + 1), max(2, h + 1)
            self.put(ax + x * (aw - 1) / (w - 1), ay + y * (ah - 1) / (h - 1))
        elif kind == REL_MOVE and len(p) >= 4:
            dx, dy = struct.unpack_from(">hh", p)
            # Kept inside this PC screen's output, so the pointer never wanders onto another screen.
            if self.pos and ax <= self.pos[0] < ax + aw and ay <= self.pos[1] < ay + ah:
                x, y = self.pos
            else:
                x, y = ax + aw / 2.0, ay + ah / 2.0
            self.put(min(max(x + dx, ax), ax + aw - 1), min(max(y + dy, ay), ay + ah - 1))
        elif kind in (BUTTON_DOWN, BUTTON_UP) and len(p) >= 1:
            b = _BUTTONS.get(p[0])
            if b:
                self.pointer.emit([(EV_KEY, b, 1 if kind == BUTTON_DOWN else 0)])
        elif kind in (SCROLL, HSCROLL) and len(p) >= 2:
            amount = struct.unpack_from(">h", p)[0]
            i = 0 if kind == SCROLL else 1
            hi, lo = (REL_WHEEL_HI_RES, REL_WHEEL) if kind == SCROLL else (REL_HWHEEL_HI_RES, REL_HWHEEL)
            self.wheel[i] += amount
            events = [(EV_REL, hi, amount)]
            whole = int(self.wheel[i] / 120)
            if whole:
                self.wheel[i] -= whole * 120
                events.append((EV_REL, lo, whole))
            self.mouse.emit(events)
        elif kind in (KEY_DOWN, KEY_UP) and len(p) >= 4:
            vk = struct.unpack_from("<h", p, 1)[0] & 0xFF
            key = VK_TO_KEY.get(vk)
            if key:
                self.keyboard.emit([(EV_KEY, key, 1 if kind == KEY_DOWN else 0)])
        elif kind == TEXT and p:
            for key, shift in text_keys(p.decode("utf-8", "replace")):
                ev = []
                if shift:
                    ev.append((EV_KEY, KEY_LEFTSHIFT, 1))
                ev += [(EV_KEY, key, 1), (EV_KEY, key, 0)]
                if shift:
                    ev.append((EV_KEY, KEY_LEFTSHIFT, 0))
                self.keyboard.emit(ev)
