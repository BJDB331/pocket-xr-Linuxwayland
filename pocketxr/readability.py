"""Readability: one screen, drawn about one screen pixel to one glasses pixel, so text is sharp on 1080p glasses.

The same switch as Pocket XR for Mac and Windows (from a Mac tester's change, November 2026: one 1440x810 screen
shown 30 degrees wide). On the Deck:

- Desktop Mode: the Deck's own screen alone ("Deck screen"), 30 degrees wide and in front. Its resolution stays the
  Deck's 1280x800: that is already below 1440x810, so its text is already as large, and changing the built-in
  panel's mode is not worth the risk. 1280 px across 30 degrees is about 43 px a degree, close to the 44 px a degree
  the Space draws app windows at (space.PIXELS_PER_DEGREE). In Steady mode, where the glasses hold a flat canvas, the
  screen is drawn at exactly its own pixels (the canvas scale that makes 800 px tall 800 canvas px).
- Gaming Mode: the game screen 30 degrees wide. The game picks its own resolution, so this cannot make its text
  larger (that is the game's resolution or interface size); it makes a game drawn 1280 px wide, the Deck's own,
  about one pixel to one glasses pixel.

Off puts back exactly what on changed, from the values saved when it was turned on (in the settings file, so a quit
or a crash in between changes nothing: it is still on at the next start, and off still puts them back).
"""
import math

WIDTH_DEG = 30.0
DECK_SCREEN = (1280, 800)
SAVED = "readability_saved"


def deck_size_deg(distance, screen=DECK_SCREEN):
    """(width, height) in degrees of the Deck screen 30 degrees wide at this distance (a flat window's height)."""
    w_m = 2 * distance * math.tan(math.radians(WIDTH_DEG) / 2)
    h_m = w_m * screen[1] / screen[0]
    return WIDTH_DEG, math.degrees(2 * math.atan(h_m / 2 / distance))


def flat_canvas_scale(out_h, screen_h=DECK_SCREEN[1]):
    """Steady mode's canvas scale (space.flat_layout) that draws the screen at its own pixels."""
    return max(0.3, min(1.0, screen_h / float(out_h or 1080)))


def gaming_size(fov_v_deg, aspect):
    """Gaming Mode's size (gaming._send_view: width = 2 atan(tan(fov_v / 2) * aspect * size)) for 30 degrees wide."""
    th = math.tan(math.radians(fov_v_deg) / 2) * aspect
    return max(0.3, min(1.2, math.tan(math.radians(WIDTH_DEG) / 2) / th))


def desktop_on(settings, deck_pose, out_h):
    """Desktop Mode, on: (what to set in settings, the Deck screen's new pose or None, before, after). deck_pose is the
    Deck screen's pose now (space.Pose) or None when it has none yet."""
    before = {"source": settings.get("source"), "canvas_scale": settings.get("canvas_scale"),
              "deck_pose": deck_pose.to_json() if deck_pose is not None else None}
    changes = {"readability": True, SAVED: before, "source": "deck", "canvas_scale": round(flat_canvas_scale(out_h), 3)}
    pose = None
    if deck_pose is not None:
        pose = deck_pose.copy()
        pose.wdeg, pose.hdeg = deck_size_deg(pose.distance)
    after = {"source": "deck", "canvas_scale": changes["canvas_scale"],
             "deck_pose": pose.to_json() if pose is not None else "30 degrees wide when it appears"}
    return changes, pose, before, after


def desktop_off(settings):
    """Desktop Mode, off: (what to set in settings, the Deck screen's pose to put back as JSON, or None to let it be
    made afresh, or False when nothing was saved and it stays as it is)."""
    saved = settings.get(SAVED) or {}
    changes = {"readability": False, SAVED: None}
    for k in ("source", "canvas_scale"):
        if saved.get(k) is not None:
            changes[k] = saved[k]
    pose = saved.get("deck_pose", False) if saved else False
    return changes, pose


def gaming_on(settings, fov_v_deg, aspect):
    before = {"size": settings.get("size")}
    size = round(gaming_size(fov_v_deg, aspect), 3)
    return {"readability": True, SAVED: before, "size": size}, before, {"size": size}


def gaming_off(settings):
    saved = settings.get(SAVED) or {}
    changes = {"readability": False, SAVED: None}
    if saved.get("size") is not None:
        changes["size"] = saved["size"]
    return changes
