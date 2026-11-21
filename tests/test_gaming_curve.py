"""Gaming Mode's curved screen: the value the effect gets, and the effect reading it."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pocketxr import gaming  # noqa: E402

FX = os.path.join(os.path.dirname(__file__), "..", "pocketxr", "reshade", "PocketXR.fx")


def curved_uv(dx, dy, half_w, half_h, bend):
    """PocketXR.fx's curved branch, line for line, for a ray (dx, dy, -1)."""
    r = 1.0 / bend
    cz = r - 1.0
    a = dx * dx + 1.0
    b = -1.0 * cz
    t = (b + math.sqrt(max(b * b - a * (cz * cz - r * r), 0.0))) / a
    hx, hy, hz = dx * t, dy * t, -t
    across = math.atan2(hx, cz - hz) * r
    return 0.5 + across / (2 * half_w), 0.5 - hy / (2 * half_h)


class CurveTest(unittest.TestCase):
    def test_value_is_kept_in_range(self):
        self.assertEqual(gaming.curve_of(0.5), 0.5)
        self.assertEqual(gaming.curve_of(-1), 0.0)
        self.assertEqual(gaming.curve_of(3), gaming.CURVE_MAX)
        self.assertEqual(gaming.curve_of("x"), 0.0)
        self.assertEqual(gaming.curve_of(None), 0.0)
        self.assertEqual(gaming.curve_of(float("nan")), 0.0)

    def test_flat_by_default(self):
        self.assertEqual(gaming.DEFAULTS["curve"], 0.0)

    def test_effect_reads_the_curve(self):
        with open(FX, encoding="utf-8") as f:
            self.assertIn("pxr_mode.z", f.read())

    def test_middle_stays_and_ends_meet_the_edges(self):
        half_w, half_h = 0.6, 0.34
        for bend in (0.25, 0.5, 1.0):
            u, v = curved_uv(0.0, 0.0, half_w, half_h, bend)
            self.assertAlmostEqual(u, 0.5)
            self.assertAlmostEqual(v, 0.5)
            # the arc's right end, half the screen's width along the curve from the middle
            r = 1.0 / bend
            cz = r - 1.0
            ang = half_w / r
            ex, ez = r * math.sin(ang), cz - r * math.cos(ang)
            u, _ = curved_uv(ex / -ez, 0.0, half_w, half_h, bend)
            self.assertAlmostEqual(u, 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
