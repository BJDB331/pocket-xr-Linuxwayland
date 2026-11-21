"""Which desktop Pocket XR is on, and so how it reads the screen.

  "x11"     an X11 session (SteamOS's Plasma X11, Kubuntu's Plasma X11, Ubuntu 24.04's "Ubuntu on Xorg", others):
            xcapture.py, with app windows.
  "mutter"  GNOME on Wayland (Ubuntu's default, and its only session from 25.10): mutter.py, through GNOME's own
            screen cast and remote desktop interfaces, the screen only.
  None      a Wayland desktop that is not GNOME (Plasma on Wayland, others): not supported yet.

POCKETXR_BACKEND=x11 or mutter picks one; POCKETXR_ALLOW_WAYLAND=1 (older, for testing) means x11.
"""
import os
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _native(name):
    """In native/ when installed, in native/out/ in a checkout after native/mutter/build.sh."""
    for folder in (os.path.join(ROOT, "native"), os.path.join(ROOT, "native", "out")):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            return p
    return os.path.join(ROOT, "native", name)


MUTTER_HELPER = _native("pxr-mutter")


def steamos():
    """SteamOS, where steamosctl picks Desktop Mode's session."""
    return bool(shutil.which("steamosctl"))


def session_type(env=None):
    """ "wayland", "x11", or "" when nothing says."""
    env = os.environ if env is None else env
    kind = env.get("XDG_SESSION_TYPE", "")
    if kind in ("wayland", "x11"):
        return kind
    # SteamOS's X11 desktop says x11; a stray WAYLAND_DISPLAY there (from Gaming Mode) must not count.
    return "wayland" if env.get("WAYLAND_DISPLAY") else ("x11" if env.get("DISPLAY") else "")


def desktop(env=None):
    """ "gnome", "kde" or "" (other), from XDG_CURRENT_DESKTOP ("ubuntu:GNOME", "KDE", ...)."""
    env = os.environ if env is None else env
    parts = [p.strip().lower() for p in env.get("XDG_CURRENT_DESKTOP", "").split(":")]
    if "gnome" in parts or "gnome-classic" in parts:
        return "gnome"
    if "kde" in parts:
        return "kde"
    return ""


def backend(env=None, helper=None):
    env = os.environ if env is None else env
    forced = env.get("POCKETXR_BACKEND", "")
    if forced in ("x11", "mutter"):
        return forced
    if env.get("POCKETXR_ALLOW_WAYLAND") == "1":
        return "x11"
    kind = session_type(env)
    if kind != "wayland":
        return "x11"
    if desktop(env) == "gnome" and os.path.exists(MUTTER_HELPER if helper is None else helper):
        return "mutter"
    return None
