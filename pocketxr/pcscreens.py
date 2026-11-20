"""PC screens: up to three screens of a Windows PC in the glasses, as Pocket XR's PC screen button on Android.

The PC streams with Apollo (or Sunshine) over Moonlight's protocol. The PC side is Pocket XR's own (in the Android
repo, pc-helper/): "Set up PC screens" runs Apollo on port 47989 plus two more Apollo instances (48989 and 49989 by
default), each of which makes its own virtual display, and copies the phones paired with the main Apollo to the other
two every 10 seconds, so one pairing covers all three. The Pocket XR PC helper (pchelper.py) types a pairing PIN into
Apollo for a client, so nobody types it.

On the Deck each PC screen is Moonlight itself (Flathub's com.moonlight_stream.Moonlight, installed for this user if
missing; no password), streaming into its own window, which the Space shows as an app window like any other. Each
press of PC screen opens the next one, up to three: screen 1 from the main Apollo, 2 and 3 from the others.

Moonlight keeps its paired hosts in its QSettings file (Moonlight.conf, [hosts]), each with the host's certificate
(srvcert). Every Apollo instance on a PC has the same certificate but its own uuid, so once the main one is paired,
screens 2 and 3 are made known to Moonlight by copying the main host's entry under their uuid and port (as Pocket XR
on Android saves them with no PIN). Moonlight then asks each over HTTPS, and the PC knows the Deck's certificate.
"""
import csv
import io
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.request

from . import pchelper

FLATPAK_ID = "com.moonlight_stream.Moonlight"
FLATHUB = "https://dl.flathub.org/repo/flathub.flatpakrepo"
DEFAULT_PORT = 47989
PORTS = (47989, 48989, 49989)  # the PC setup's main Apollo and PC screens 2 and 3 (another port if one was busy)
COUNT = 3
STREAM_W, STREAM_H, FPS = 1920, 1080, 60  # the size of the screen an app window gets in the Space (xcapture)
APP = "Desktop"
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "pocket-xr-deck")
STATE = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "pocket-xr-deck")
SAVED = os.path.join(CONFIG, "pc-screens.json")
CONF_NAME = os.path.join("Moonlight Game Streaming Project", "Moonlight.conf")
FLATPAK_CONF = os.path.join(os.path.expanduser("~/.var/app"), FLATPAK_ID, "config", CONF_NAME)
NATIVE_CONF = os.path.join(os.path.expanduser("~/.config"), CONF_NAME)
PAIR_WAIT_S = 150
PIN_TRIES = 3


# ---------------------------------------------------------------- worked out apart, so they can be tested

def bitrate_kbps(w, h, fps, quality="auto"):
    """As Pocket XR on Android (PcScreenQuality): 40 Mbps at 1080p60, scaled with the pixels and with the frame rate
    to the power 0.6; "sharp" is 1.6 times, "saver" half. 8 to 150 Mbps."""
    pixels = (w * h) / (1920.0 * 1080.0)
    rate = (fps / 60.0) ** 0.6
    factor = {"sharp": 1.6, "saver": 0.5}.get(quality, 1.0)
    return int(min(150_000, max(8_000, round(40_000 * pixels * rate * factor))))


def parse_address(text):
    """(host, port) from "192.168.1.20", "192.168.1.20:48989", "pc.local", "[fe80::1]:48989" or a bare IPv6
    address; None for an empty or malformed one (as PcScreenAddress on Android)."""
    t = (text or "").strip()
    if not t:
        return None

    def port(s):
        return int(s) if s.isdigit() and 1 <= int(s) <= 65535 else None

    if t.startswith("["):
        end = t.find("]")
        if end < 2:
            return None
        host, rest = t[1:end], t[end + 1:]
        if not rest:
            return host, DEFAULT_PORT
        if not rest.startswith(":") or port(rest[1:]) is None:
            return None
        return host, port(rest[1:])
    if t.count(":") > 1:
        return t, DEFAULT_PORT
    if ":" not in t:
        return t, DEFAULT_PORT
    host, p = t.split(":", 1)
    if not host or port(p) is None:
        return None
    return host, port(p)


def address(host, port):
    """The host as Moonlight's command line takes it, always with the port (it then matches Moonlight's saved
    host at once, by its address, as NvAddress.toString writes it)."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def parse_serverinfo(xml):
    """The parts of a host's /serverinfo Pocket XR uses, from its XML; None if it is not one."""
    def tag(name):
        m = re.search(rf"<{name}>([^<]*)</{name}>", xml)
        return m.group(1).strip() if m else None

    uuid = tag("uniqueid")
    if not uuid:
        return None
    return {
        "hostname": tag("hostname") or "",
        "uuid": uuid,
        "mac": tag("mac") or "",
        "https_port": int(tag("HttpsPort") or 0) or None,
        "virtual_display": tag("VirtualDisplayCapable") == "true" and tag("VirtualDisplayDriverReady") != "false",
    }


def parse_avahi(text):
    """[(name, IPv4 address, port)] from `avahi-browse -rpt _nvstream._tcp`'s resolved lines ("=;..."), once each."""
    out = []
    for line in text.splitlines():
        f = line.split(";")
        if len(f) < 9 or f[0] != "=" or f[2] != "IPv4":
            continue
        name = f[3].replace("\\032", " ")
        name = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), name)
        try:
            item = (name, f[7], int(f[8]))
        except ValueError:
            continue
        if item not in out:
            out.append(item)
    return out


def parse_app_list(text):
    """App names from `moonlight list <host> --csv`, in order."""
    names = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[1].strip().lstrip("-").isdigit():
            names.append(row[0].strip())
    return names


def pick_app(names):
    """The app to stream: Desktop where the host has it, else the first; Desktop when the list is empty."""
    for n in names:
        if n.lower() == APP.lower():
            return n
    return names[0] if names else APP


def screen_order(info):
    """Sort key for a PC's streaming hosts: the main Apollo first, then PC screens 2 and 3 by the number in the name
    the PC setup gives them ("MIKE screen 2"), then any other by port."""
    m = re.search(r"\sscreen\s+(\d+)$", info.get("hostname", ""), re.IGNORECASE)
    return (int(m.group(1)) if m else 1, info.get("port", 0))


def next_slot(open_slots, available):
    """The first PC screen (0, 1 or 2) not open, among the [available] the PC streams; None when all are open."""
    for i in range(min(COUNT, available)):
        if i not in open_slots:
            return i
    return None


def stream_args(addr, app, w=STREAM_W, h=STREAM_H, fps=FPS, quality="auto"):
    """Moonlight's command line for a PC screen: windowed (the Space shows it as an app window), the mouse absolute
    (so the cursor moves in and out of the window as on the Deck's desktop, not captured), the PC's app quit when the
    window closes (Apollo then removes the virtual display)."""
    return ["stream", addr, app, "--resolution", f"{w}x{h}", "--fps", str(fps),
            "--bitrate", str(bitrate_kbps(w, h, fps, quality)), "--display-mode", "windowed", "--absolute-mouse",
            "--capture-system-keys", "never", "--no-game-optimization", "--quit-after", "--keep-awake"]


# ---- Moonlight's settings file (Qt's INI format), only the [hosts] array ----

def _ini_unquote(raw):
    v = raw.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return v


def _ini_quote(text):
    if text != text.strip() or any(c in text for c in ',;="\\'):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


class MoonlightConf:
    """Moonlight.conf, read as lines so whatever Pocket XR does not change is written back as it was."""

    PORT_KEYS = (("localaddress", "localport"), ("remoteaddress", "remoteport"), ("manualaddress", "manualport"),
                 ("ipv6address", "ipv6port"))

    def __init__(self, text=""):
        self.lines = text.splitlines()

    # sections: (name, first line index after the header, end line index)
    def _section(self, name):
        start = None
        for i, line in enumerate(self.lines):
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                if start is not None:
                    return start, i
                if s[1:-1] == name:
                    start = i + 1
        return (start, len(self.lines)) if start is not None else None

    def _items(self, name):
        sec = self._section(name)
        if sec is None:
            return []
        out = []
        for i in range(*sec):
            line = self.lines[i]
            if "=" in line and not line.lstrip().startswith((";", "#")):
                k, v = line.split("=", 1)
                out.append((i, k.strip(), v))
        return out

    def _array(self):
        """Moonlight reads [hostsbackup] when it has hosts (an update it did not finish), else [hosts]."""
        for name in ("hostsbackup", "hosts"):
            size = next((_ini_unquote(v) for _, k, v in self._items(name) if k == "size"), "0")
            if size.isdigit() and int(size) > 0:
                return name
        return "hosts"

    def hosts(self):
        """{index: {key: raw value}} of the hosts Moonlight reads, apps left out."""
        out = {}
        for _, k, v in self._items(self._array()):
            idx, _, rest = k.partition("\\")
            if idx.isdigit() and rest and not rest.startswith("apps\\"):
                out.setdefault(int(idx), {})[rest] = v
        return out

    def find(self, uuid):
        for i, h in self.hosts().items():
            if _ini_unquote(h.get("uuid", "")).lower() == uuid.lower():
                return i, h
        return None

    def paired(self, uuid):
        """Moonlight has this host's certificate, so it is paired (Moonlight asks it over HTTPS)."""
        f = self.find(uuid)
        cert = _ini_unquote(f[1].get("srvcert", "")) if f else ""
        return cert not in ("", "@ByteArray()")

    def _set(self, name, key, raw):
        items = self._items(name)
        for i, k, _ in items:
            if k == key:
                self.lines[i] = f"{key}={raw}"
                return
        sec = self._section(name)
        if sec is None:
            if self.lines and self.lines[-1].strip():
                self.lines.append("")
            self.lines += [f"[{name}]", f"{key}={raw}"]
        else:
            # after the section's last key, before any blank lines that end it
            at = sec[1]
            while at > sec[0] and not self.lines[at - 1].strip():
                at -= 1
            self.lines.insert(at, f"{key}={raw}")

    def adopt(self, from_uuid, uuid, hostname, host, port):
        """Makes the host [uuid] (another Apollo instance on the same PC, at host:port) paired in Moonlight's eyes:
        the paired host [from_uuid]'s certificate under the other's uuid, name and port. An entry Moonlight already
        has for [uuid] (found unpaired) is updated, else the paired one's is copied. False if [from_uuid] is not
        paired."""
        src = self.find(from_uuid)
        if src is None or not self.paired(from_uuid):
            return False
        name = self._array()
        mine = self.find(uuid)
        if mine is None:
            idx = max(self.hosts()) + 1
            entry = {k: v for k, v in src[1].items() if k != "customname"}
            self._set(name, "size", str(idx))
        else:
            idx, entry = mine
            entry = dict(entry)
            entry["srvcert"] = src[1]["srvcert"]
            entry.setdefault("mac", src[1].get("mac", ""))
        entry["uuid"] = _ini_quote(uuid)
        entry["hostname"] = _ini_quote(hostname)
        for addr_key, port_key in self.PORT_KEYS:
            if _ini_unquote(entry.get(addr_key, "")):
                entry[port_key] = str(port)
        entry["manualaddress"] = _ini_quote(host)
        entry["manualport"] = str(port)
        for k, v in entry.items():
            self._set(name, f"{idx}\\{k}", v)
        return True

    def forget(self, uuid):
        """Drops the host's certificate, so Moonlight pairs it again. False if there is no such host."""
        f = self.find(uuid)
        if f is None:
            return False
        self._set(self._array(), f"{f[0]}\\srvcert", "@ByteArray()")
        return True

    def text(self):
        return "\n".join(self.lines) + "\n"


# ---------------------------------------------------------------- the Deck's side, at run time

def conf_path():
    if os.path.exists(FLATPAK_CONF) or not shutil.which("moonlight"):
        return FLATPAK_CONF
    return NATIVE_CONF


def read_conf(path=None):
    try:
        with open(path or conf_path(), encoding="utf-8") as f:
            return MoonlightConf(f.read())
    except OSError:
        return MoonlightConf()


def write_conf(conf, path=None):
    path = path or conf_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".pocketxr"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(conf.text())
    os.replace(tmp, path)


def serverinfo(host, port, timeout=1.5):
    try:
        with urllib.request.urlopen(f"http://{address(host, port)}/serverinfo", timeout=timeout) as r:
            return parse_serverinfo(r.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


def browse():
    """Streaming hosts on the network by mDNS (_nvstream._tcp, as Moonlight finds them), if avahi is there."""
    if not shutil.which("avahi-browse"):
        return []
    try:
        out = subprocess.run(["avahi-browse", "-rpt", "_nvstream._tcp"], capture_output=True, text=True, timeout=8)
        return parse_avahi(out.stdout)
    except (OSError, subprocess.SubprocessError):
        return []


class PcScreens:
    """The PC screen button's work, on its own thread: one press at a time."""

    def __init__(self, log, say, opened=lambda slot: None):
        self.log = log
        self.say = say  # a short message for the user (a toast in the glasses), from any thread
        self.opened = opened
        self.slots = [None] * COUNT  # Moonlight's process for each PC screen
        self.busy = False
        self.direct = set()  # uuids of screens 2 and 3 to pair on their own (the copied pairing did not hold)
        self.lock = threading.Lock()
        self.stop = False
        os.makedirs(STATE, exist_ok=True)

    # ---- for the app ----
    def open_count(self):
        return sum(1 for p in self.slots if p is not None and p.poll() is None)

    def press(self):
        with self.lock:
            if self.busy:
                self.say("Still opening a PC screen...")
                return
            self.busy = True
        threading.Thread(target=self._press, daemon=True, name="pc-screen").start()

    def close_all(self):
        self.stop = True
        for p in self.slots:
            if p is not None and p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except (OSError, AttributeError):
                    p.terminate()

    # ---- one press ----
    def _press(self):
        try:
            self._open_next()
        except Exception as e:  # noqa: BLE001 - a bug here must not take the app down
            self.log(f"pc screens: {type(e).__name__}: {e}")
            self.say("Could not open a PC screen. The log has the details.")
        finally:
            self.busy = False

    def _open_next(self):
        open_slots = {i for i, p in enumerate(self.slots) if p is not None and p.poll() is None}
        if len(open_slots) >= COUNT:
            self.say("All three PC screens are open.")
            return
        cmd = self._moonlight()
        if not cmd:
            return
        pc = self._find_pc()
        if pc is None:
            self.say("No PC found. Start Apollo and the Pocket XR PC helper on your PC, on the same network.")
            return
        host, name, helper = pc
        insts = self._instances(host)
        if not insts:
            self.say(f"{name} is not streaming. Start Apollo (or Sunshine) on it.")
            return
        slot = next_slot(open_slots, len(insts))
        if slot is None:
            self._more_screens(host, name, helper, len(insts))
            return
        main = insts[0]
        if not read_conf().paired(main["uuid"]):
            if not self._pair(cmd, host, main, name, helper):
                return
        inst = insts[slot]
        if slot > 0 and inst["uuid"] in self.direct:
            conf = read_conf()
            if conf.forget(inst["uuid"]):  # the copied certificate, so Moonlight's own pairing is waited for
                write_conf(conf)
            if not self._pair(cmd, host, inst, name, helper):
                return
            self.direct.discard(inst["uuid"])
        elif slot > 0 and not read_conf().paired(inst["uuid"]):
            conf = read_conf()
            if conf.adopt(main["uuid"], inst["uuid"], inst["hostname"] or name, host, inst["port"]):
                write_conf(conf)
                self.log(f"pc screens: {inst['hostname']} ({inst['port']}) known to Moonlight from the main pairing")
        self._stream(cmd, slot, host, inst, name)

    def _moonlight(self):
        """Moonlight's command, installing it from Flathub (for this user, no password) if it is missing."""
        if shutil.which("flatpak"):
            if subprocess.run(["flatpak", "info", FLATPAK_ID], capture_output=True).returncode == 0:
                return ["flatpak", "run", FLATPAK_ID]
        if shutil.which("moonlight"):
            return ["moonlight"]
        if not shutil.which("flatpak"):
            self.say("PC screens need Moonlight: install it from your app store.")
            return None
        self.say("Installing Moonlight for PC screens. This takes a few minutes...")
        self.log("pc screens: installing Moonlight from Flathub")
        steps = [["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB],
                 ["flatpak", "install", "--user", "-y", "--noninteractive", "flathub", FLATPAK_ID]]
        for step in steps:
            r = subprocess.run(step, capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                self.log(f"pc screens: {' '.join(step)} failed: {(r.stderr or r.stdout).strip()[-400:]}")
                self.say("Could not install Moonlight. Is the Deck online?")
                return None
        self.log("pc screens: Moonlight installed")
        return ["flatpak", "run", FLATPAK_ID]

    def _find_pc(self):
        """(address, PC name, (helper port) or None): the PC helper first, else the PC saved last time, else the
        first streaming host mDNS finds. Never this machine: its own host shows it on a phone's glasses."""
        found = pchelper.find(4.0, stop=lambda: self.stop)
        if found:
            host, port, name = found
            self._save({"host": host, "pc": name})
            self.log(f"pc screens: PC helper on {name} ({host})")
            return host, name, port
        saved = self._saved()
        if saved.get("host") and not pchelper.is_local(saved["host"]) and serverinfo(saved["host"], DEFAULT_PORT):
            return saved["host"], saved.get("pc") or saved["host"], None
        for name, host, _ in browse():
            if pchelper.is_local(host):
                continue  # this machine's own host is for a phone's glasses, not these
            self._save({"host": host, "pc": name})
            self.log(f"pc screens: found {name} ({host}) by mDNS; no PC helper")
            return host, name, None
        return None

    def _instances(self, host):
        """The streaming hosts on the PC, by port: the main Apollo first."""
        ports = set(PORTS)
        ports |= {p for _, h, p in browse() if h == host}
        out = []
        for p in sorted(ports):
            info = serverinfo(host, p)
            if info and all(i["uuid"] != info["uuid"] for i in out):
                info["port"] = p
                out.append(info)
        out.sort(key=screen_order)
        self.log("pc screens: " + (", ".join(f"{i['hostname']} on {i['port']}" for i in out) or "no hosts"))
        return out[:COUNT]

    def _more_screens(self, host, name, helper, have):
        """Every screen the PC streams is open. With the PC helper, a PC with one Apollo is offered the PC screens
        setup (the PC's user says Yes, then Windows asks for admin rights)."""
        if have >= COUNT:
            self.say("All three PC screens are open.")
            return
        if helper is None:
            self.say(f"{name} streams {have} screen{'s' if have > 1 else ''}. "
                     "Run Set up PC screens on it for more (from the Pocket XR PC helper).")
            return
        token = self._token(host, name, helper)
        if not token:
            return
        r = pchelper.ask(host, helper,
                         {"op": "screens-setup", "token": token, "phone": pchelper.NAME}, timeout=150)
        if r.get("ok"):
            self.say(f"Setting up more screens on {name}: say Yes on its screen. Then press PC screen again.")
        else:
            self.say(f"{name} did not set up more screens.")

    def _token(self, host, name, helper):
        token = pchelper.load_token()
        if token:
            return token
        self.say(f"Allow this Steam Deck on {name}: a question is on its screen.")
        try:
            token = pchelper.pair(host, helper)
        except OSError as e:
            self.log(f"pc screens: helper pairing: {e}")
            token = None
        if not token:
            self.say(f"{name} did not allow this Deck.")
        return token

    def _pair(self, cmd, host, inst, name, helper):
        """Pairs Moonlight with the main Apollo: Moonlight waits with a PIN Pocket XR chose, and the PC helper types
        it into Apollo. With no helper (or when it can't), the PIN is shown to type on the PC."""
        pin = f"{secrets.randbelow(10000):04d}"
        addr = address(host, inst["port"])
        self.log(f"pc screens: pairing with {inst['hostname']} at {addr}")
        log = open(os.path.join(STATE, "moonlight-pair.log"), "w")
        proc = subprocess.Popen(cmd + ["pair", addr, "--pin", pin], stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        web = f"https://{address(host, inst['port'] + 1)}"
        typed = [False]

        def type_pin():
            token = self._token(host, name, helper)
            for _ in range(PIN_TRIES):
                if not token or self.stop or proc.poll() is not None:
                    break
                try:
                    r = pchelper.ask(host, helper, {"op": "apollo-pin", "token": token, "pin": pin,
                                                    "name": pchelper.NAME, "port": inst["port"], "virtual": True},
                                     timeout=60)
                except OSError as e:
                    r = {"ok": False, "why": str(e)}
                self.log(f"pc screens: helper: {r}")
                if r.get("ok"):
                    typed[0] = True
                    return
                if r.get("why") == "not paired":  # the PC forgot this Deck: allowed again
                    pchelper.forget_token()
                    token = self._token(host, name, helper)
            self.say(f"Type the PIN {pin} on {name}: Apollo's page {web}, PIN.")

        if helper is not None:
            self.say(f"Pairing with {name}...")
            threading.Thread(target=type_pin, daemon=True).start()
        else:
            self.say(f"Type the PIN {pin} on {name}: Apollo or Sunshine's page {web}, PIN.")
        end = time.monotonic() + PAIR_WAIT_S
        ok = False
        while time.monotonic() < end and not self.stop:
            if read_conf().paired(inst["uuid"]):
                ok = True
                break
            if proc.poll() is not None:
                break
            time.sleep(1.0)
        if proc.poll() is None:
            # Moonlight's "Pairing completed" waits for an OK; asked to quit, it saves its hosts and goes.
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        log.close()
        ok = ok or read_conf().paired(inst["uuid"])
        self.log(f"pc screens: pairing {'done' if ok else 'failed'} (PIN typed by the helper: {typed[0]})")
        if not ok:
            self.say(f"Pairing with {name} did not finish. Press PC screen to try again.")
        return ok

    def _app(self, cmd, addr, inst):
        """The app to stream, from the host's list once (Desktop on Apollo and Sunshine), kept for next time."""
        saved = self._saved()
        apps = saved.get("apps", {})
        if inst["uuid"] in apps:
            return apps[inst["uuid"]]
        try:
            r = subprocess.run(cmd + ["list", addr, "--csv"], capture_output=True, text=True, timeout=45)
            app = pick_app(parse_app_list(r.stdout))
        except (OSError, subprocess.SubprocessError):
            return APP
        apps[inst["uuid"]] = app
        saved["apps"] = apps
        self._save(saved)
        return app

    def _stream(self, cmd, slot, host, inst, name):
        addr = address(host, inst["port"])
        app = self._app(cmd, addr, inst)
        args = cmd + stream_args(addr, app)
        path = os.path.join(STATE, f"moonlight-{slot + 1}.log")
        log = open(path, "w")
        self.log(f"pc screens: PC screen {slot + 1}: {' '.join(args)}")
        proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.slots[slot] = proc
        self.say(f"Opening PC screen {slot + 1} from {name}...")
        self.opened(slot)
        threading.Thread(target=self._watch, args=(slot, inst["uuid"], proc, log, path), daemon=True).start()

    def _watch(self, slot, uuid, proc, log, path):
        code = proc.wait()
        log.close()
        try:
            with open(path, errors="replace") as f:
                tail = f.read()[-4000:]
        except OSError:
            tail = ""
        self.log(f"pc screens: PC screen {slot + 1} closed ({code})")
        if self.stop:
            return
        for needle, words in (("has not been paired", "Moonlight is not paired with this screen. Press PC screen "
                                                       "to pair again."),
                              ("Failed to find application", "The PC has no Desktop to stream."),
                              ("Failed to connect", "Lost the PC.")):
            if needle in tail:
                self.say(f"PC screen {slot + 1}: {words}")
                if needle == "has not been paired":
                    self._forget_pairing(slot, uuid)
                break

    def _forget_pairing(self, slot, uuid):
        """Moonlight says a screen is not paired after all. Screen 1: Moonlight forgets the PC's certificate, so the
        next press pairs again. Screens 2 and 3: the PC did not copy the Deck to that Apollo (its PC screens task not
        running), so the next press pairs that one on its own."""
        if slot > 0:
            self.direct.add(uuid)
            return
        conf = read_conf()
        if conf.forget(uuid):
            write_conf(conf)

    # ---- the PC saved last time ----
    def _saved(self):
        try:
            with open(SAVED) as f:
                s = json.load(f)
                return s if isinstance(s, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, values):
        s = self._saved()
        s.update(values)
        os.makedirs(CONFIG, exist_ok=True)
        tmp = SAVED + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, SAVED)
