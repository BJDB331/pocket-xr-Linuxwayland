"""Readability: the numbers, and that off puts back exactly what on changed (Desktop Mode and Gaming Mode)."""
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pocketxr import readability as R  # noqa: E402
from pocketxr import space as S  # noqa: E402


def json_round_trip(settings):
    """As the settings file keeps them, so a restart between on and off changes nothing."""
    return json.loads(json.dumps(settings))


class Numbers(unittest.TestCase):
    def test_deck_screen_is_30_degrees_wide_and_keeps_its_shape(self):
        for d in (1.0, 1.4, 2.5):
            w, h = R.deck_size_deg(d)
            self.assertEqual(w, 30.0)
            w_m, h_m = S.deg_to_m(w, d), S.deg_to_m(h, d)
            self.assertAlmostEqual(h_m / w_m, 800 / 1280, places=6)
        # about one Deck pixel to one glasses pixel: 1280 px over 30 degrees, near the Space's 44 px a degree
        self.assertLess(abs(1280 / 30.0 - S.PIXELS_PER_DEGREE), 2)

    def test_steady_canvas_draws_the_screen_at_its_own_pixels(self):
        k, rects = S.flat_layout([(1280, 800)], 1920, 1080, R.flat_canvas_scale(1080), 40)
        self.assertAlmostEqual(k, 1.0, places=6)
        self.assertEqual(R.flat_canvas_scale(None), R.flat_canvas_scale(1080))
        self.assertEqual(R.flat_canvas_scale(400), 1.0)  # kept in the slider's range

    def test_gaming_size_is_30_degrees(self):
        from pocketxr import gaming
        size = R.gaming_size(gaming.FOV_V_DEG, gaming.ASPECT)
        th = math.tan(math.radians(gaming.FOV_V_DEG) / 2) * gaming.ASPECT
        self.assertAlmostEqual(math.degrees(2 * math.atan(th * size)), 30.0, places=6)
        self.assertTrue(0.3 <= size <= 1.2)


class DesktopRoundTrip(unittest.TestCase):
    def test_off_puts_back_source_canvas_and_the_deck_screen(self):
        settings = {"source": "apps", "canvas_scale": 0.85, "readability": False, "readability_saved": None}
        pose = S.Pose(12.0, -3.0, 1.6, 44.0, 27.5, 0.2)
        original = (dict(settings), pose.to_json())
        changes, new_pose, before, after = R.desktop_on(settings, pose, 1080)
        settings.update(changes)
        self.assertTrue(settings["readability"])
        self.assertEqual(settings["source"], "deck")
        self.assertAlmostEqual(settings["canvas_scale"], 0.741)
        self.assertEqual(new_pose.wdeg, 30.0)
        self.assertEqual((new_pose.yaw, new_pose.pitch, new_pose.distance, new_pose.curve), (12.0, -3.0, 1.6, 0.2))
        self.assertEqual(pose.to_json(), original[1])  # the pose given is not changed in place
        self.assertEqual(before["source"], "apps")
        self.assertEqual(after["source"], "deck")
        settings = json_round_trip(settings)  # quit and start again in between
        changes, back = R.desktop_off(settings)
        settings.update(changes)
        self.assertEqual({k: settings[k] for k in original[0]}, original[0])
        self.assertEqual(back, original[1])

    def test_no_deck_screen_yet_means_made_afresh_after(self):
        settings = {"source": "deck", "canvas_scale": 0.8}
        changes, new_pose, _, after = R.desktop_on(settings, None, 1080)
        self.assertIsNone(new_pose)
        settings.update(changes)
        changes, back = R.desktop_off(settings)
        self.assertIsNone(back)
        self.assertEqual(changes["canvas_scale"], 0.8)

    def test_off_with_nothing_saved_changes_nothing_else(self):
        changes, back = R.desktop_off({"source": "apps", "readability": True, "readability_saved": None})
        self.assertEqual(changes, {"readability": False, "readability_saved": None})
        self.assertIs(back, False)


class GamingRoundTrip(unittest.TestCase):
    def test_off_puts_the_size_back(self):
        from pocketxr import gaming
        settings = dict(gaming.DEFAULTS, size=0.95, curve=0.4)
        self.assertFalse(settings["readability"])
        changes, before, after = R.gaming_on(settings, gaming.FOV_V_DEG, gaming.ASPECT)
        settings.update(changes)
        self.assertAlmostEqual(settings["size"], 0.438)
        self.assertEqual(settings["curve"], 0.4)  # the curve is left as it is
        settings = json_round_trip(settings)
        settings.update(R.gaming_off(settings))
        self.assertEqual(settings["size"], 0.95)
        self.assertFalse(settings["readability"])
        self.assertIsNone(settings["readability_saved"])

    def test_service_switch_logs_before_and_after(self):
        from pocketxr import gaming
        lines = []

        class G:
            pass
        g = G()
        g.settings = dict(gaming.DEFAULTS, size=0.8)
        g.log = lines.append
        g.reshade = None
        saved = []
        real = gaming.save_settings
        gaming.save_settings = saved.append
        try:
            gaming.Gaming._set_readability(g, True)
            gaming.Gaming._set_readability(g, True)  # already on: nothing more
            gaming.Gaming._set_readability(g, False)
        finally:
            gaming.save_settings = real
        self.assertEqual(len(lines), 2)
        self.assertIn("readability on (Quick Access): before {'size': 0.8}; after {'size': 0.438}", lines[0])
        self.assertIn("readability off (Quick Access): before {'size': 0.438}; after {'size': 0.8}", lines[1])
        self.assertEqual(g.settings["size"], 0.8)


class DesktopApp(unittest.TestCase):
    """App.set_readability with the Space and capture stood in (app.py loads SDL: on the Deck and Linux only)."""

    def setUp(self):
        try:
            from pocketxr import app
        except (ImportError, OSError) as e:
            self.skipTest(f"app.py does not load here ({e})")
        self.app = app

    def make(self, poses):
        lines, saved = [], []

        class Fake:
            pass
        a = Fake()
        a.settings = dict(self.app.DEFAULTS, source="apps", canvas_scale=0.9)
        a.space = Fake()
        a.space.poses = poses
        a.space.tweens = {}
        a.capture = Fake()
        a.capture.glasses_rect = (0, 0, 1920, 1080)
        a.capture.source = "apps"
        a.log = lines.append
        a.recentred = 0

        def set_source(s):
            a.settings["source"] = s
            a.capture.source = s

        def recentre():
            a.recentred += 1
        a.set_source, a.recentre, a._send_status = set_source, recentre, lambda: None
        self.app.save_settings = saved.append
        return a, lines

    def test_round_trip(self):
        deck = S.Pose(5.0, 0.0, 1.4, 40.0, 25.0, 0.0)
        a, lines = self.make({"deck": deck})
        self.app.App.set_readability(a, True)
        self.assertEqual(a.space.poses["deck"].wdeg, 30.0)
        self.assertEqual(a.capture.source, "deck")
        self.assertEqual(a.recentred, 1)
        self.assertTrue(lines[-1].startswith("readability on: before {'source': 'apps', 'canvas_scale': 0.9"))
        self.app.App.set_readability(a, False)
        self.assertEqual(a.space.poses["deck"].to_json(), deck.to_json())
        self.assertEqual((a.settings["source"], a.settings["canvas_scale"]), ("apps", 0.9))
        self.assertTrue(lines[-1].startswith("readability off: before {'source': 'deck'"))
        self.assertFalse(a.settings["readability"])

    def test_deck_screen_made_while_on_is_made_afresh_after(self):
        a, _ = self.make({})
        self.app.App.set_readability(a, True)
        a.space.poses["deck"] = S.Pose(0.0, 0.0, 1.4, 30.0, 19.0)  # the Space made it while on
        self.app.App.set_readability(a, False)
        self.assertNotIn("deck", a.space.poses)


if __name__ == "__main__":
    unittest.main()
