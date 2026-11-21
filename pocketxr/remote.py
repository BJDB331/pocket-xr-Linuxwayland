"""Commands to the running app from outside it: what the Deck's back buttons do, for computers without them.

`pocket-xr --button L4 tap` sends one button press to the Pocket XR that is running, through a small socket in the
user's runtime folder. On GNOME the installer binds keyboard shortcuts to these (Super+Alt+R recentres and so on); on
other desktops the same commands can be bound by hand.

The buttons and what they do (app.py _button): L4 tap recentre, L4 hold Window in space / Steady 6DoF, R4 tap next
app window, R4 hold Deck screen / App windows, L5 tap launcher, L5 hold lock screens, R5 tap Control Center, R5 hold
adjust all.
"""
import json
import os
import socket
import threading

BUTTONS = ("L4", "R4", "L5", "R5")
HOWS = ("tap", "hold")


def path():
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join("/tmp", f"pocket-xr-{os.getuid()}")
    return os.path.join(base, "pocket-xr.sock")


def parse(argv):
    """("button", name, how) from ["--button", "L4", "tap"], or None when these are not remote arguments."""
    if len(argv) >= 2 and argv[0] == "--button" and argv[1].upper() in BUTTONS:
        how = argv[2].lower() if len(argv) > 2 else "tap"
        if how in HOWS:
            return ("button", argv[1].upper(), how)
    return None


def send(msg, where=None):
    """Sends one command to the running app. False when no Pocket XR is listening."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(json.dumps(msg).encode(), where or path())
        return True
    except OSError:
        return False
    finally:
        s.close()


class Listener(threading.Thread):
    """In the app: takes commands from the socket and hands them on (one call per command)."""

    def __init__(self, handle, log=print, where=None):
        super().__init__(daemon=True)
        self.handle = handle
        self.log = log
        self.where = where or path()
        self.sock = None

    def open(self):
        try:
            os.makedirs(os.path.dirname(self.where), mode=0o700, exist_ok=True)
            if os.path.exists(self.where):
                os.unlink(self.where)  # one app runs at a time (its lock), so an old socket is a leftover
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.sock.bind(self.where)
            os.chmod(self.where, 0o600)
            return True
        except OSError as e:
            self.log(f"remote: no command socket ({e})")
            self.sock = None
            return False

    def run(self):
        while self.sock is not None:
            try:
                data = self.sock.recv(4096)
            except OSError:
                return
            try:
                m = json.loads(data.decode())
            except ValueError:
                continue
            if isinstance(m, dict) and m.get("button") in BUTTONS and m.get("how") in HOWS:
                self.handle(m["button"], m["how"])

    def close(self):
        s, self.sock = self.sock, None
        if s is not None:
            s.close()
            try:
                os.unlink(self.where)
            except OSError:
                pass
