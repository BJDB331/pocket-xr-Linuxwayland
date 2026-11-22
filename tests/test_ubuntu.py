"""Other Linux than SteamOS: which way the screen is read (session.py), which monitor is the glasses on GNOME
(mutter.py), the shared-memory frames from pxr-mutter, and the keyboard shortcuts' commands (remote.py)."""
import mmap
import os
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pocketxr import mutter as M  # noqa: E402
from pocketxr import remote as R  # noqa: E402
from pocketxr import session as S  # noqa: E402


class Backend(unittest.TestCase):
    def test_x11_sessions_use_x11(self):
        for env in ({"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "KDE"},
                    {"XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"},
                    {"DISPLAY": ":0"}):
            self.assertEqual(S.backend(env, helper=__file__), "x11")

    def test_steamos_x11_with_a_stray_wayland_display_is_x11(self):
        env = {"XDG_SESSION_TYPE": "x11", "WAYLAND_DISPLAY": "gamescope-0", "XDG_CURRENT_DESKTOP": "KDE"}
        self.assertEqual(S.backend(env, helper=__file__), "x11")

    def test_gnome_on_wayland_uses_mutter_when_the_helper_is_there(self):
        env = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}
        self.assertEqual(S.backend(env, helper=__file__), "mutter")
        self.assertIsNone(S.backend(env, helper="/nonexistent/pxr-mutter"))

    def test_other_wayland_desktops_are_not_supported_yet(self):
        for desk in ("sway", "Hyprland", ""):
            env = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": desk}
            self.assertIsNone(S.backend(env, helper=__file__, kwin_helper=__file__), desk)

    def test_plasma_on_wayland_uses_kwin_when_the_helper_is_there(self):
        env = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"}
        self.assertEqual(S.backend(env, helper=__file__, kwin_helper=__file__), "kwin")
        self.assertIsNone(S.backend(env, helper=__file__, kwin_helper="/nonexistent/pxr-kwin"))

    def test_overrides(self):
        wl = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"}
        self.assertEqual(S.backend(dict(wl, POCKETXR_ALLOW_WAYLAND="1"), helper=__file__), "x11")
        self.assertEqual(S.backend(dict(wl, POCKETXR_BACKEND="mutter"), helper=__file__), "mutter")
        self.assertEqual(S.backend(dict(wl, POCKETXR_BACKEND="kwin"), helper=__file__), "kwin")


def mon(conn, x, y, w, h, name="", vendor="", product="", primary=False, on=True, scale=1.0):
    return {"connector": conn, "x": x, "y": y, "w": w, "h": h, "name": name, "vendor": vendor, "product": product,
            "primary": primary, "on": on, "scale": scale}


class Monitors(unittest.TestCase):
    def test_glasses_by_name_and_laptop_panel_as_main(self):
        mons = [mon("eDP-1", 0, 0, 1920, 1200, "Built-in display", primary=True),
                mon("DP-2", 1920, 0, 1920, 1080, "XREAL Air 2 Ultra", vendor="MRG")]
        g, main = M.pick_monitors(mons, env={})
        self.assertEqual((g["connector"], main["connector"]), ("DP-2", "eDP-1"))

    def test_glasses_made_primary_still_count_as_glasses(self):
        mons = [mon("eDP-1", 0, 0, 1920, 1200), mon("DP-1", 1920, 0, 1920, 1080, "VITURE", primary=True)]
        g, main = M.pick_monitors(mons, env={})
        self.assertEqual((g["connector"], main["connector"]), ("DP-1", "eDP-1"))

    def test_unknown_second_monitor_is_the_glasses_and_the_primary_is_main(self):
        mons = [mon("HDMI-1", 0, 0, 2560, 1440, "Dell", primary=True), mon("DP-3", 2560, 0, 1920, 1080, "Something")]
        g, main = M.pick_monitors(mons, env={})
        self.assertEqual((g["connector"], main["connector"]), ("DP-3", "HDMI-1"))

    def test_one_monitor_has_no_glasses_and_off_monitors_do_not_count(self):
        mons = [mon("eDP-1", 0, 0, 1920, 1200), mon("DP-2", 0, 0, 1920, 1080, "XREAL One", on=False)]
        g, main = M.pick_monitors(mons, env={})
        self.assertIsNone(g)
        self.assertEqual(main["connector"], "eDP-1")

    def test_main_monitor_override(self):
        mons = [mon("Meta-0", 1920, 0, 1280, 800), mon("Meta-1", 0, 0, 1920, 1080, primary=True)]
        g, main = M.pick_monitors(mons, env={"POCKETXR_MAIN_MONITOR": "Meta-0"})
        self.assertEqual((g["connector"], main["connector"]), ("Meta-1", "Meta-0"))

    def test_sdl_display_by_place_then_by_size(self):
        sdl = [(0, "A", 0, 0, 1920, 1080), (1, "B", 1920, 0, 1280, 800)]
        self.assertEqual(M.match_sdl(mon("Meta-0", 1920, 0, 1280, 800), sdl)[0], 1)
        self.assertEqual(M.match_sdl(mon("X", 5000, 0, 1280, 800), sdl)[0], 1)
        # A monitor at scale 2: SDL sees its size in logical pixels
        self.assertEqual(M.match_sdl(mon("Y", 9999, 9, 3840, 2160, scale=2.0), sdl)[0], 0)
        self.assertIsNone(M.match_sdl(None, sdl))


class SharedFrames(unittest.TestCase):
    """The layout pxr-mutter writes (native/mutter/pxr_mutter.c): header, then BGRx rows."""

    def setUp(self):
        self.f = tempfile.NamedTemporaryFile(delete=False)
        self.f.truncate(64 + 64 * 64 * 4)
        self.f.close()
        fd = os.open(self.f.name, os.O_RDWR)
        self.m = mmap.mmap(fd, 0)
        os.close(fd)
        self.cap = M.MutterCapture(log=lambda m: None, helper="/nonexistent")
        self.cap._open_shm(self.f.name)

    def tearDown(self):
        self.m.close()
        self.cap._shm.close()
        os.unlink(self.f.name)

    def write(self, seq, w, h, cursor, number, fill=b"\x10\x20\x30\x00"):
        self.m[64:64 + w * h * 4] = fill * (w * h)
        struct.pack_into("<IIIIiiIII", self.m, 0, M.MAGIC, seq, w, h, cursor[0], cursor[1], 1, 64 * 64 * 4, number)

    def test_a_frame_then_a_pointer_move_without_a_copy(self):
        self.write(2, 8, 4, (3, 2), 1)
        f, c = self.cap._pull()
        self.assertEqual((f.w, f.h, len(f.data), c), (8, 4, 8 * 4 * 4, (3, 2)))
        self.assertEqual(self.cap._pull(), (None, None))  # nothing new
        struct.pack_into("<ii", self.m, 16, 5, 1)  # the pointer moved: seq 4, same frame number
        struct.pack_into("<I", self.m, 4, 4)
        f, c = self.cap._pull()
        self.assertIsNone(f)
        self.assertEqual(c, (5, 1))

    def test_a_frame_being_written_is_not_read(self):
        self.write(3, 8, 4, (0, 0), 1)  # odd: being written
        self.assertEqual(self.cap._pull(), (None, None))

    def test_too_big_for_the_file_is_not_read(self):
        struct.pack_into("<IIIIiiIII", self.m, 0, M.MAGIC, 2, 4096, 4096, 0, 0, 0, 64 * 64 * 4, 1)
        f, c = self.cap._pull()
        self.assertIsNone(f)


class Shortcuts(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(R.parse(["--button", "L4", "tap"]), ("button", "L4", "tap"))
        self.assertEqual(R.parse(["--button", "r5", "hold"]), ("button", "R5", "hold"))
        self.assertEqual(R.parse(["--button", "L4"]), ("button", "L4", "tap"))
        self.assertIsNone(R.parse(["--button", "A", "tap"]))
        self.assertIsNone(R.parse(["--button", "L4", "down"]))
        self.assertIsNone(R.parse([]))

    def test_round_trip_through_the_socket(self):
        where = os.path.join(tempfile.mkdtemp(), "pxr.sock")
        got = []
        done = threading.Event()
        listener = R.Listener(lambda n, k: (got.append((n, k)), done.set()), log=lambda m: None, where=where)
        self.assertTrue(listener.open())
        listener.start()
        self.assertTrue(R.send({"button": "L5", "how": "tap"}, where))
        self.assertTrue(R.send({"button": "ZZ", "how": "tap"}, where))  # sent, but not a button: ignored
        done.wait(2)
        time.sleep(0.1)
        listener.close()
        self.assertEqual(got, [("L5", "tap")])
        self.assertFalse(R.send({"button": "L5", "how": "tap"}, where))  # nobody listening


if __name__ == "__main__":
    unittest.main()
