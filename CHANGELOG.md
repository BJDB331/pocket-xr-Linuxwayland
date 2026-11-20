# Changes

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
