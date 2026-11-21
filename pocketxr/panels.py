"""What the capture threads hand the app: a picture (Frame) and a thing shown in space (Panel).

Shared by xcapture.py (X11) and mutter.py (GNOME on Wayland), without loading X11's libraries.
"""
import time


class Frame:
    """One picture: BGRA bytes, width, height, and a number that changes with each new picture."""
    __slots__ = ("data", "w", "h", "serial")

    def __init__(self, data, w, h, serial):
        self.data, self.w, self.h, self.serial = data, w, h, serial


class Panel:
    """A thing shown in space: the Deck's screen, or one app window."""

    def __init__(self, key, title):
        self.key = key  # "deck" or the X client window id
        self.title = title
        self.frame = None
        self.root_x = self.root_y = 0  # where its picture's top left is on the X screen
        self.w = self.h = 0
        self.pointer = None  # (x, y) in its picture, when the pointer is on it and nothing covers it there
        self.active = False
        self.frame_win = None  # KWin's frame around it, found with the list
        self.cls = ""  # the app's WM_CLASS class, to keep its place across runs
        self.shadow = (0, 0, 0, 0)  # an app's own shadow round its window (left, right, top, bottom), left out
        self.first_seen = time.monotonic()
