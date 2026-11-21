# Changes

## 0.6.4 (21 Nov 2026)

- **Readability**, off by default, the same switch as in Pocket XR for Mac and Windows (from a Mac tester's change:
  one lower-resolution screen shown about 30 degrees wide, for sharp text on 1080p glasses). Not yet tried with
  glasses.
  - **Desktop Mode** (a switch on the control panel): the Deck screen alone, 30 degrees wide and in front. It keeps
    the Deck's own 1280x800, already lower than 1440x810; 1280 px across 30 degrees is about one pixel to one
    glasses pixel. In Steady 6DoF the screen is drawn at exactly its own pixels.
  - **Gaming Mode** (a switch in Quick Access): the game screen 30 degrees wide. The game sets its own resolution,
    so this can't make its text larger; it is sharpest for a game drawn 1280 pixels wide.
  - Off puts back exactly what on changed: in Desktop Mode, Deck screen or App windows, the Deck screen's size and
    place, and Steady's size; in Gaming Mode, the size. The values are kept in the settings file, so quitting in
    between changes nothing. Turning it on and off is logged with the values before and after.

## 0.6.3 (21 Nov 2026)

- **Gaming Mode: a curved screen.** Quick Access, Decky, Pocket XR has a **Curve** slider under Size, from 0 % (flat,
  as before) to 100 % (bent round you, every part as far away as the middle). The screen keeps its width along the
  curve. It's remembered, and is off by default. Asked for by Pierre Zammit. Not yet tried on a Deck or with
  glasses; the shader's curve has only been checked by a test that redoes its sums.

## 0.6.2 (20 Nov 2026)

- **XREAL XBX A01 and A01+:** with 0.6.1 an A01+ sent motion after the controller's heartbeat (522 reports a second),
  but the screen still followed the head, and the glasses' buttons were off afterwards. XR Gaming's driver was still
  running on that Deck after its plugin was removed; it reads the same glasses and sets the game screen's effect
  itself. Now:
  - The heartbeat runs only until motion flows (3 s), then stops, because the glasses kept sending without it. If
    the motion then stops, it stays on.
  - The logs carry the first 5 s of motion reports in full (`imu-raw.log`) and, for a minute after the glasses
    appear, a mode is picked or the view is recentred, where the head points once a second.
  - Not yet tried with the glasses. Whether unplugging and replugging gives the buttons back is not yet known.
- **XR Gaming's driver:** the message now says it can still run with its plugin removed, and gives the command that
  stops it: `systemctl --user disable --now xr-driver`.
- **Desktop Mode on Wayland:** the message and the README no longer suggest switching SteamOS's desktop to X11. A
  tester's Deck had trouble after that switch. They point to Gaming Mode, and the README gives the command that puts
  Wayland back: `steamosctl set-default-desktop-session plasma.desktop`.
- **Two buttons in Quick Access**, shown only when they apply, with no terminal or password:
  - **Stop XR Gaming's driver** runs `systemctl --user disable --now xr-driver` when XR Gaming's driver is running
    or set to start.
  - **Put the desktop back to Wayland** runs `steamosctl set-default-desktop-session plasma.desktop` when Desktop
    Mode is set to X11. It applies the next time you switch to Desktop.
  - Both say in the panel whether they worked, and log it. Checked on a Deck with a stand-in for XR Gaming's service.
- **Quick Access panel:** its version now follows Pocket XR's, so the installer offers the new panel when it changes.

Thanks to Pierre Zammit for testing 0.6.1 and sending logs, which led to these changes.

## 0.6.1 (20 Nov 2026)

- **XREAL XBX A01 and A01+:** 0.6.0 got no motion from an A01+: not a single byte came back on its interface 1. Now,
  if interface 1 stays silent for 6 s, Pocket XR wakes the glasses' controller on interface 0 the way XR Gaming's
  driver does (a start-up handshake, then a "heartbeat" every 100 ms), asks again, and then tries interface 2. It
  uses whichever interface sends motion and logs that choice. Not yet tried with the glasses.
- **More in the logs:** which hidraw node is which interface of the glasses, and the first bytes of what each one
  sends, answers included.
- **XR Gaming check:** when XR Gaming's driver (`xrDriver`) runs, the panel and Quick Access say "XR Gaming is running;
  turn it off for Pocket XR's head tracking (or uninstall it)", and the logs' glasses report says whether it is
  running or installed. Two programs asking the same glasses for motion get in each other's way.
- **Gaming Mode's Off** now stops the Air family's motion stream (and the XBX heartbeat) until a mode is picked again,
  so the glasses are left alone.

Thanks to Pierre Zammit for testing 0.6.0 with his XBX A01+ and sending logs, which led to these changes.

## 0.6.0 (20 Nov 2026)

- **Your Deck on your phone.** Pocket XR on the phone can show this Deck's screens as its PC screens, up to three.
  The Deck answers the phone itself in Desktop Mode: no Sunshine Flatpak (the Pocket XR PC Screens app) and no
  separate PC helper. The installer switches those off and keeps their pairings. Tried with one phone so far; more in
  `docs/HOST-DECK.md`.
- **H.264 on SteamOS.** SteamOS's HEVC encoder (Mesa 26.1.2) writes pictures that do not decode: the first phone test
  showed coloured blocks. The Deck now encodes and decodes a few test pictures with each encoder when it starts and
  leaves out any that fail, so it sends H.264. HEVC comes back by itself if a later Mesa fixes it.
- **KDE's display-switch pop-up** ("Only use external screen" and the other layouts) no longer stays on the Deck's
  screen when a phone stream starts: Pocket XR closes it. Other uses of the pop-up are left alone.
- **Show a PC.** Opens your Windows PC's screens as windows in the Space, up to three, through Moonlight and Apollo.
  Not yet tried on a Deck.
- **XREAL XBX A01 and A01+.** Head tracking read as on the other Air models, from the glasses' HID interface 1. Not
  yet tried with the glasses.
- **Desktop Mode on Wayland.** SteamOS 3.9 starts Desktop Mode on Wayland, which Pocket XR's Desktop Mode can't use.
  The message now gives the `steamosctl` command that switches it to X11 (not yet tried), or points to Gaming Mode.
  On X11 the phone's PC screens stop working.

Thanks to Pierre Zammit for his Steam Deck logs with the XREAL XBX A01+, which led to XBX A01 and A01+ head tracking.

## 0.5.1 (18 Nov 2026)

- The VITURE Beast with USB id 35CA:1201 is recognised as a Beast.
