# Pocket XR on Ubuntu (and other Linux than SteamOS)

What this covers: the Linux app away from the Steam Deck, mainly on Ubuntu 24.04 LTS and 26.04 LTS. What was
observed and how is written down here; anything not observed is called untested.

## How it reads the screen

`pocketxr/session.py` picks one of two ways when the app starts, and logs which (`desktop: gnome on wayland, not
SteamOS; screen through mutter`):

| Session | Way | What works |
| --- | --- | --- |
| X11: SteamOS's Plasma (X11), Kubuntu's Plasma (X11), Ubuntu 24.04's "Ubuntu on Xorg", others | `xcapture.py` (X11), as on the Deck | Deck screen and App windows |
| GNOME on Wayland: Ubuntu's default, and its only GNOME session from 25.10 (GNOME 49 removed X11) | `mutter.py` and the native helper `pxr-mutter` | The main screen only |
| Any other Wayland desktop (Plasma on Wayland, others) | none: it says so and stops, as before | Nothing yet |

`POCKETXR_BACKEND=x11` or `mutter` forces one. `POCKETXR_MAIN_MONITOR=<connector>` picks the main screen on GNOME
(for testing).

### GNOME on Wayland: Mutter's own interfaces

On Wayland an app cannot read the screen or move the pointer itself. GNOME's compositor, Mutter, offers both on the
user's session bus with no dialog: `org.gnome.Mutter.ScreenCast` (a monitor's picture as a PipeWire stream, the
pointer as metadata) and `org.gnome.Mutter.RemoteDesktop` (pointer and keys, linked to that stream).
`org.gnome.Mutter.DisplayConfig` lists the monitors. These are what GNOME's own screen sharing uses.

`native/mutter/pxr_mutter.c` (built by `native/mutter/build.sh`, needs only GLib and PipeWire) speaks them and puts
the newest frame in a shared memory file; `pocketxr/mutter.py` reads it. Its stdin and stdout are described at the
top of the C file.

- The main screen: the built-in panel (eDP, LVDS, DSI), else the primary monitor. The glasses: a monitor whose maker
  or name says XREAL, VITURE and the like, else the other monitor.
- The view opens on the glasses' monitor as a native Wayland window (`SDL_VIDEODRIVER=wayland,x11`).
- When the pointer comes onto the view, it is put back on the main screen through the remote desktop session.
- GNOME shows its orange screen sharing icon in the top bar while Pocket XR runs. That is the screen cast.
- **App windows** (each app in its own place) need a list of windows Wayland gives no app. Choosing it shows the main
  screen. A small GNOME Shell extension could give that list later; not built.

### X11 with GNOME's window manager (Ubuntu on Xorg)

The Deck's X11 code works with Mutter as with KWin, with one change: GNOME's own apps (GTK 4) draw their own title
bar and a shadow round it, inside their X window. The shadow's size is in `_GTK_FRAME_EXTENTS`; the picture now
leaves it out, and an app moved onto the glasses is sized so that its visible part fills its screen.

## Installing

`install.sh` on anything but SteamOS also:

- Checks for Python's Tk, SDL2, cairo, librsvg and OpenGL. Ubuntu leaves `python3-tk` out. With `apt-get` it offers
  to install what is missing (`sudo apt-get install`), else names it. Saying no installs the rest anyway; the app then
  starts without the panel (no Tk) or says which library is missing (no SDL2), rather than a traceback.
- Offers a udev rule, `/etc/udev/rules.d/70-pocket-xr.rules`, so the person at the computer can read XREAL (3318),
  VITURE (35ca) and Nintendo (057e, Joy-Cons) USB HID devices (`TAG+="uaccess"`). The Air family and VITURE head
  tracking need it; the Aura's head tracking comes over its USB network and does not. `uninstall.sh` leaves the rule
  and says how to remove it.
- On GNOME, adds keyboard shortcuts for what the Deck's back buttons do, beside any custom shortcuts already there:
  Super+Alt+R recentre (L4 tap), Super+Alt+V Window in space or Steady 6DoF (L4 hold), Super+Alt+L launcher (L5 tap),
  Super+Alt+C Control Center (R5 tap). They run `pocket-xr --button <name> <tap|hold>`, which reaches the running app
  through a socket in `$XDG_RUNTIME_DIR` (`pocketxr/remote.py`). Other desktops can bind the same command by hand.
  `uninstall.sh` takes them out again, leaving the others.
- Skips the SteamOS-only parts: the Gaming Mode service (unless `gamescope` is there), the Decky panel, and the
  Deck-on-your-phone host (unless `kwin_wayland` is there).

## What was observed (21 Nov 2026)

All without glasses, on Mike's Windows PC in two throwaway WSL 2 distros (`pxr-u2404`: Ubuntu 24.04.1, GNOME Shell
46.0, Mutter 46.2, PipeWire 1.0.5, Python 3.12.3; `pxr-u2604`: Ubuntu 26.04, GNOME Shell 50.1, PipeWire 1.6.2, Python
3.14.4). No GPU there: GNOME ran on its software renderer.

GNOME on Wayland: `gnome-shell --headless --wayland --virtual-monitor 1280x800 --virtual-monitor 1920x1080` in its
own D-Bus session with PipeWire and WirePlumber. The second monitor stood in for the glasses, which Pocket XR then
treats as unknown glasses (no head tracking, the picture shown flat).

- 24.04 and 26.04: `pxr-mutter` listed both monitors, recorded the main one with no dialog, and frames arrived
  (BGRx, 1920x1080 and 1280x800). The same binary, built on 24.04, ran on 26.04.
- Pointer moves sent through the remote desktop session landed where sent, read back from the stream's pointer data
  (600,400; 610,410; 1200,100). Mutter only sends the pointer data when the client's size range for it reaches
  Mutter's (it asks room for a 384x384 pointer); a smaller range left it out.
- 24.04 and 26.04: the app started (`screen through mutter`, `SDL video driver wayland`), opened its view full screen
  on the stand-in glasses, and the view showed the live main screen, a terminal's clock ticking and the control panel
  (Tk through Xwayland) in it. Screenshots of the stand-in glasses' monitor were taken through a second `pxr-mutter`.
- 24.04: with the pointer moved onto the stand-in glasses, the app logged it and put it back on the main screen (the
  main stream's pointer then read 960,540). In later runs on both releases the test's pointer move did not reach the
  view, so this is not shown to work reliably; untested with a real mouse.
- Install from a release-shaped folder: on 24.04 everything present; on 26.04 with `python3-tk` and `libsdl2-2.0-0`
  missing, it named them (no terminal, so it did not ask). The four shortcuts were added once (a second install kept
  them, no duplicates); `pocket-xr --button L5 tap` and `L4 tap` reached the running app (launcher, recentre);
  `uninstall.sh` left the shortcut list empty again. Before SDL2 was installed the app stopped with a traceback; it
  now names the library instead (that message itself untested).
- All of `tests/` pass on both (Python 3.12 and 3.14).

GNOME on X11 (24.04's Ubuntu on Xorg): `gnome-shell --x11` in Xvfb, one 3200x1080 screen, the left 1280x800 standing
in for the main screen and the rest for the glasses' output; `XCapture` driven directly, without SDL.

- Mutter lists the EWMH calls Pocket XR uses (`_NET_MOVERESIZE_WINDOW`, `_NET_RESTACK_WINDOW`,
  `_NET_ACTIVE_WINDOW`, `_NET_CLOSE_WINDOW`, `_NET_CLIENT_LIST_STACKING`).
- Deck screen: grabbed. App windows: xterm and GNOME's Text Editor listed, moved onto the stand-in glasses at
  1920x1080 (less GNOME's top bar, which a separate glasses monitor would not have), grabbed, and put back exactly
  where they were (Text Editor 822x642 at -11,241 before and after).
- Text Editor's picture first had a black shadow margin (61, 61, 55, 67); left out since.
- While GNOME's Overview was showing, a GTK 4 window moved to a new size had not redrawn; after leaving the Overview
  it had.

## Untested

- Everything with real glasses on Ubuntu: head tracking, the XREAL Aura's USB network under Ubuntu's NetworkManager,
  the udev rule, the glasses' monitor names, sharpness, Steady 6DoF.
- A real GNOME session on hardware (GPU, a real mouse and touchpad), fractional scaling, and the Wayland view's
  frame rate. Each frame is copied through shared memory, as `XGetImage` copies on X11.
- Kubuntu (Plasma X11 or Wayland), other distributions, GNOME's Xorg session with a real second monitor.
- PC VR on Ubuntu: `libpxr_pcvr.so` links SteamOS's `libavcodec.so.61`; 26.04 has FFmpeg 7.1 (the same soname),
  24.04 has 6.1 (`.60`), so it cannot load there.
- Show a PC and the Deck-on-your-phone host on GNOME.

## Testing it again

The headless GNOME session used above, as a sketch (as a normal user in a throwaway WSL distro with `gnome-shell`,
`xwayland`, `pipewire`, `wireplumber`, `dbus-user-session`; WSLg's read-only `/tmp/.X11-unix` must be swapped for a
writable one first, as root):

```bash
export XDG_RUNTIME_DIR=/tmp/xdg-$(id -u) XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=ubuntu:GNOME
dbus-run-session -- bash -c 'pipewire & wireplumber & gnome-shell --headless --wayland \
  --virtual-monitor 1280x800 --virtual-monitor 1920x1080 & sleep 5; export WAYLAND_DISPLAY=wayland-0 DISPLAY=:0 \
  XAUTHORITY=$(ls $XDG_RUNTIME_DIR/.mutter-Xwaylandauth.*); python3 -m pocketxr'
```

`pxr-mutter` on its own: `record Meta-1` then `move 600 400` on its stdin; the frame and pointer are in
`/dev/shm/pxr-mutter-<pid>`. GNOME starts in the Overview; `key 65307 1` and `key 65307 0` (Escape) leave it.
