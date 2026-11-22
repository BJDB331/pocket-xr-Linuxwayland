# Pocket XR on KDE Plasma's Wayland desktop

What this covers: Pocket XR's Desktop Mode view on Plasma's Wayland session. That is SteamOS 3.9's Desktop Mode
(`plasma.desktop`) and the default on Kubuntu, Fedora KDE, Bazzite, CachyOS and others. What was seen and how is
written down here; anything not seen is called untested.

## What you get

- **Deck screen**: the main screen in the glasses, as on X11.
- **App windows**: each open app as its own window in space. Its picture comes straight from KWin wherever the window
  is, even when another window covers it, so nothing is moved. Looking at a window brings it to the front on the main
  screen, so touch, the trackpads and the keyboard reach it. Close and Back on a window work too.
- The pointer is kept off the glasses' monitor: when it goes there, it is put back where it was on the main screen.
- No dialog and no screen sharing icon.

Not here yet, compared with X11:

- App windows don't get their own larger screens on the glasses' monitor (`virtual` on X11, with the space pointer).
  That needs the view on the glasses to let clicks through to windows under it, which a Wayland app can't arrange.
  Apps stay at the size they have on the main screen.
- A window can't be put on top without also getting the keyboard (no "raise" on Wayland); it is made active.

## How it works

On Wayland no app may read the screen, list other apps' windows or move the pointer. KWin, Plasma's compositor,
offers this to a program that a desktop file names in `X-KDE-Wayland-Interfaces`, with no dialog:

| KWin interface | What Pocket XR uses it for | Who else uses it |
| --- | --- | --- |
| `zkde_screencast_unstable_v1` | a monitor's or one window's picture as a PipeWire stream, the pointer's place as metadata | xdg-desktop-portal-kde (Plasma's screen sharing), Pocket XR's Deck host |
| `org_kde_plasma_window_management` | the window list: uuid, title, app, pid, place, state, stacking order; activate and close | Plasma's task manager |
| `org_kde_kwin_fake_input` | moving the pointer back off the glasses; Back (Alt+Left) | KDE Connect's remote input |
| `wl_output`, `xdg_output` | the monitors: connector, maker, model, place, size, scale | every Wayland app |

`native/kwin/pxr_kwin.c` (built by `native/kwin/build.sh`, needs only libwayland and PipeWire) speaks them. It puts
each picture in its own shared memory file, laid out like `pxr-mutter`'s. `pocketxr/kwin.py` reads them and gives the
app the same panels as `xcapture.py`. Its stdin and stdout are described at the top of the C file.

`install.sh` writes `~/.local/share/applications/pocket-xr-kwin.desktop`, hidden from menus, with
`Exec=<install folder>/native/pxr-kwin` and the three KDE interfaces, then rebuilds KDE's service cache
(`kbuildsycoca6`). KWin matches a program to its desktop file by the program's full path. Without the file, KWin
doesn't offer the interfaces, and Pocket XR says so (`capture: KWin offers screen cast v0`).

- The main screen: the built-in panel (eDP, LVDS, DSI), else the first monitor. The glasses: a monitor whose maker or
  name says XREAL, VITURE and the like, else the other monitor (`mutter.pick_monitors`).
- The view opens on the glasses' monitor as a native Wayland window (`SDL_VIDEODRIVER=wayland,x11`).
- App windows: the windows the task bar shows. Not Pocket XR's own, not Plasma's shell, not minimised, at most 12. The
  active window and any that are changing get up to 60 pictures a second, the others 15. While App windows are shown,
  the main screen's stream keeps sending only the pointer, with no picture copied.
- The pointer on a window: KWin's metadata for that window's stream, and the top window in the stacking order when
  windows overlap. For a KWin that sends no pointer with window streams, it is worked out from the pointer on the main
  screen and the window's place.

`POCKETXR_BACKEND=kwin` forces this way. `PXR_KWIN_DEBUG=1` makes `pxr-kwin` describe its first buffers.

## What was observed (21 Nov 2026)

No glasses and no Plasma 6 hardware yet. A headless KWin 5.27.11 (Ubuntu 24.04's `kwin-wayland`, `--virtual
--output-count 2`, 1280x800 each, software rendering) in its own D-Bus session with PipeWire 1.0.5 and WirePlumber.
`weston-simple-shm` and `weston-terminal` stood in for app windows. The second output stood in for the glasses.

- With the desktop file, KWin offered `pxr-kwin` screen cast v3, the window list v16 and fake input v4. With the
  desktop file naming another path, it offered none of them, and Pocket XR said so.
- Monitors listed with connectors (`Virtual-0`, `Virtual-1`), logical places and sizes.
- The window list: both windows with uuid, title, app id, resource name, pid, place, state (active bit) and stacking
  order.
- Window streams: both windows' pictures arrived as BGRA at their own sizes (250x250, 742x427), the animated one at
  about 60 a second (233 frames in 4 s), each with the pointer's place on it in the stream's metadata.
- Fake input: pointer moves landed where sent, read back from the streams' pointer metadata (410,310 on the screen;
  141,124 and then 331,214 on the terminal). Where the two windows overlapped, only the top one got the pointer.
- Activate: the other window became the active one in the list. Close: the window went away. Keys (`key_to`, as
  Back sends Alt+Left): "echo hi" and Return typed into the terminal changed its picture.
- Pointer onto the second output, then `view_pointer()`: put back on the main screen where it last was.
- The app (`python3 -m pocketxr`): `desktop: kde on wayland ... screen through kwin`, `SDL video driver wayland`,
  `view: open on` the second output, and a clean quit on SIGTERM.
- **Not shown to work there:** the main screen's own stream. KWin 5.27's virtual backend negotiated it but sent one
  empty buffer flagged corrupted and then nothing, with every pointer mode, and the same for a region stream. Its
  pointer metadata did arrive. Plasma's own screen sharing and Pocket XR's Deck host use the same output streams on
  real hardware, so this looks like the headless backend. It needs a check on a real Plasma desktop: Deck screen.
- `tests/test_kwin.py` runs `kwin.py` against a stand-in helper. All of `tests/` pass.

## Untested

- Everything on real Plasma 6 hardware: the Deck's Desktop Mode on SteamOS 3.9, Kubuntu, Fedora KDE, Bazzite,
  CachyOS. In particular the main screen's stream (above), fractional scaling, and KWin 6's window streams (their
  size: the window's inside or its whole frame, and their pointer metadata).
- Glasses: head tracking, the glasses' monitor names under KWin, Steady 6DoF.
- KWin 6.x restricting the interfaces as described (the desktop file), and whether a first run needs a log out and
  back in before KWin sees the new desktop file.
- Apps through Xwayland (Steam, older apps) as App windows: they are in KWin's window list too, so they should work.

## Testing it again

As a normal user, with `kwin-wayland`, `kwin-wayland-backend-virtual`, `pipewire`, `wireplumber` and
`libkf5service-bin` (for `kbuildsycoca5`). In a container, KWin's file capabilities may stop it from starting
("Operation not permitted"); a copy of `kwin_wayland` without them runs.

```bash
native/kwin/build.sh
mkdir -p ~/.local/share/applications && cat > ~/.local/share/applications/pocket-xr-kwin.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Pocket XR screen reader
Exec=$PWD/native/out/pxr-kwin
NoDisplay=true
X-KDE-Wayland-Interfaces=zkde_screencast_unstable_v1,org_kde_plasma_window_management,org_kde_kwin_fake_input
EOF
export XDG_RUNTIME_DIR=/tmp/xdg-$(id -u) XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=KDE
mkdir -p -m 700 $XDG_RUNTIME_DIR
dbus-run-session -- bash -c 'kbuildsycoca5; pipewire & sleep 1; wireplumber & sleep 1
  kwin_wayland --virtual --width 1280 --height 800 --output-count 2 --socket wayland-pxr --no-lockscreen &
  sleep 4; export WAYLAND_DISPLAY=wayland-pxr; weston-terminal & sleep 2; python3 -m pocketxr'
```

`pxr-kwin` on its own: `windows`, then `record deck output Virtual-0` or `record w window {uuid}`, then
`move 400 300`; each picture is in the `/dev/shm/pxr-kwin-<pid>-<n>` file it names.
