"""KDE Plasma on Wayland (kwin.py): which windows are shown, which one has the pointer, the shared-memory pictures
from pxr-kwin, and the capture thread against a stand-in helper that speaks pxr-kwin's lines."""
import json
import mmap
import os
import struct
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pocketxr import kwin as K  # noqa: E402


def win(uuid, title="App", app_id="org.kde.konsole", pid=4000, state=0, x=0, y=0, w=800, h=600, resource="konsole"):
    return {"uuid": uuid, "title": title, "app_id": app_id, "resource": resource, "pid": pid, "state": state,
            "x": x, "y": y, "w": w, "h": h}


def write_shm(path, w, h, pixel=b"\x10\x20\x30\xff", cursor=None, number=1, seq=2):
    m = open(path, "wb")
    cx, cy = cursor if cursor else (-1, -1)
    m.write(K.HEADER.pack(K.MAGIC, seq, w, h, cx, cy, 1 if cursor else 0, 1 << 20, number))
    m.write(b"\0" * (K.SHM_HEADER - K.HEADER.size))
    m.write(pixel * (w * h))
    m.write(b"\0" * max(0, (1 << 20) - w * h * 4))
    m.close()


class Windows(unittest.TestCase):
    def test_own_shell_minimised_and_hidden_windows_are_left_out(self):
        ws = [win("{a}", "Konsole"),
              win("{own}", "Pocket XR Deck", pid=111),
              win("{panel}", "Pocket XR", app_id="", pid=222),
              win("{shell}", "Plasma", app_id="org.kde.plasmashell", resource="plasmashell"),
              win("{min}", "Dolphin", state=K.MINIMIZED),
              win("{skip}", "Tooltip", state=K.SKIP_TASKBAR),
              win("{tiny}", "Tiny", w=10, h=10),
              win("{b}", "Firefox", app_id="firefox", resource="firefox")]
        got = [w["uuid"] for w in K.wanted_windows(ws, own_pids={111, 222})]
        self.assertEqual(got, ["{a}", "{b}"])

    def test_at_most_the_limit(self):
        ws = [win("{%d}" % i) for i in range(20)]
        self.assertEqual(len(K.wanted_windows(ws, limit=5)), 5)

    def test_top_window_under_the_pointer(self):
        self.assertIsNone(K.top_under([], ["{a}"]))
        self.assertEqual(K.top_under(["{a}"], []), "{a}")
        self.assertEqual(K.top_under(["{a}", "{b}"], ["{b}", "{a}"]), "{a}")
        self.assertEqual(K.top_under(["{a}", "{b}"], ["{a}", "{b}"]), "{b}")


class Keys(unittest.TestCase):
    def test_combos_to_evdev(self):
        self.assertEqual(K.key_codes("alt+Left"), [56, 105])
        self.assertEqual(K.key_codes("ctrl+shift+t"), [29, 42, 20])
        self.assertEqual(K.key_codes("super+F4"), [125, 62])
        self.assertIsNone(K.key_codes("alt+NoSuchKey"))

    def test_points_to_the_desktop(self):
        m = {"x": 1280, "y": 0, "scale": 1.5}
        self.assertEqual(K.global_point(m, 300, 150), (1480.0, 100.0))
        self.assertEqual(K.global_point({"x": 0, "y": 0}, 10, 20), (10, 20))


class Shm(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "shm")

    def test_frame_and_pointer(self):
        write_shm(self.path, 4, 2, cursor=(1, 1))
        s = K.ShmFrames(self.path)
        f = s.pull(7)
        self.assertEqual((f.w, f.h, f.serial, len(f.data)), (4, 2, 7, 32))
        self.assertEqual(f.data[:4], b"\x10\x20\x30\xff")
        self.assertEqual(s.cursor, (1, 1))
        self.assertTrue(s.cursor_ever)
        self.assertIsNone(s.pull(8))  # nothing new
        s.close()

    def test_a_frame_not_yet_due_is_kept_for_later(self):
        write_shm(self.path, 4, 2)
        s = K.ShmFrames(self.path)
        self.assertIsNone(s.pull(1, picture=False))
        f = s.pull(2, picture=True)
        self.assertIsNotNone(f)
        self.assertFalse(s.cursor_ever)
        s.close()

    def test_half_written_frames_are_not_taken(self):
        write_shm(self.path, 4, 2, seq=3)
        s = K.ShmFrames(self.path)
        self.assertIsNone(s.pull(1))
        s.close()


FAKE_HELPER = r'''
import json, os, sys, struct
sys.path.insert(0, {root!r})
from tests.test_kwin import write_shm
d = {dir!r}
def out(m):
    print(json.dumps(m), flush=True)
out({{"caps": {{"screencast": 6, "windows": 16, "input": 4, "xdg_output": True}}}})
out({{"monitors": [
    {{"connector": "eDP-1", "vendor": "Valve", "product": "ANX7530", "name": "Built-in", "on": True,
      "x": 0, "y": 0, "w": 1280, "h": 800, "lw": 1280, "lh": 800, "scale": 1.0, "primary": False}},
    {{"connector": "DP-1", "vendor": "MRG", "product": "XREAL One", "name": "XREAL One", "on": True,
      "x": 1280, "y": 0, "w": 1920, "h": 1080, "lw": 1920, "lh": 1080, "scale": 1.0, "primary": False}}]}})
out({{"windows": [
    {{"uuid": "{{w1}}", "title": "Konsole", "app_id": "org.kde.konsole", "resource": "konsole", "pid": 1,
      "state": 1, "x": 10, "y": 20, "w": 640, "h": 480}},
    {{"uuid": "{{w2}}", "title": "Dolphin", "app_id": "org.kde.dolphin", "resource": "dolphin", "pid": 2,
      "state": 0, "x": 100, "y": 100, "w": 640, "h": 480}}], "stacking": ["{{w2}}", "{{w1}}"]}})
n = 0
log = open(os.path.join(d, "commands"), "a")
for line in sys.stdin:
    log.write(line); log.flush()
    p = line.split()
    if p[0] == "record":
        n += 1
        path = os.path.join(d, "shm%d" % n)
        if p[2] == "output":
            write_shm(path, 8, 5, cursor=(3, 2))
        else:
            write_shm(path, 6, 4, cursor=(1, 1))
        out({{"shm": path, "key": p[1], "source": " ".join(p[2:])}})
    elif p[0] == "quit":
        break
'''


class Thread(unittest.TestCase):
    def test_against_a_stand_in_helper(self):
        d = tempfile.mkdtemp()
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        script = os.path.join(d, "fake.py")
        with open(script, "w") as f:
            f.write(FAKE_HELPER.format(root=root, dir=d))
        helper = os.path.join(d, "pxr-kwin")
        with open(helper, "w") as f:
            f.write(f"#!/bin/sh\nexec {sys.executable} {script}\n")
        os.chmod(helper, 0o755)
        logs = []
        cap = K.KWinCapture(log=logs.append, helper=helper)
        cap.start()
        self.assertTrue(cap.ready.wait(5))

        def wait_for(cond):
            end = time.monotonic() + 5
            while time.monotonic() < end and not cond():
                time.sleep(0.02)
            return cond()
        # Deck screen: the main monitor, the built-in panel, not the glasses.
        self.assertTrue(wait_for(lambda: [p.key for p in cap.snapshot()] == ["deck"]))
        deck = cap.snapshot()[0]
        self.assertEqual((deck.w, deck.h, deck.pointer), (8, 5, (3, 2)))
        sdl = [(0, "eDP-1", 0, 0, 1280, 800), (1, "DP-1", 1280, 0, 1920, 1080)]
        g, main = cap.split(sdl)
        self.assertEqual((g[1], main[1]), ("DP-1", "eDP-1"))
        # App windows: each window its own picture; both say the pointer is on them, the top one gets it.
        cap.source = "apps"
        self.assertTrue(wait_for(lambda: sorted(p.key for p in cap.snapshot()) == ["{w1}", "{w2}"]))
        ps = {p.key: p for p in cap.snapshot()}
        self.assertEqual((ps["{w1}"].w, ps["{w1}"].h, ps["{w1}"].cls, ps["{w1}"].active), (6, 4, "konsole", True))
        self.assertEqual(ps["{w1}"].pointer, (1, 1))
        self.assertIsNone(ps["{w2}"].pointer)
        cap.activate("{w2}")
        cap.view_pointer()
        cap.close()
        cap.join(3)
        time.sleep(0.2)
        with open(os.path.join(d, "commands")) as f:
            cmds = f.read().splitlines()
        self.assertIn("record deck output eDP-1", cmds)
        self.assertIn("record {w1} window {w1}", cmds)
        self.assertIn("pictures deck 0", cmds)
        self.assertIn("activate {w2}", cmds)
        self.assertIn("move 3.0 2.0", cmds)


if __name__ == "__main__":
    unittest.main()
