# PC screens on the Steam Deck

Not yet tried on a Deck. This page says how it is built and what has to be checked on real hardware. Once it has
been tried, it records what was seen.

## What it does

The **Show a PC** button (on the launcher card next to PC VR, and on the control panel) opens a screen of your
Windows PC as a window in the Space, in the glasses plugged into this Deck. Each press opens the next one, up to
three, as on Android. With no glasses plugged in, it says so and opens nothing. This Deck itself is never offered:
for glasses on a phone, the phone opens PC screens and this Deck is the computer (`docs/HOST-DECK.md`).

The PC runs Apollo plus two more Apollo instances, set up by **Set up PC screens** in the Pocket XR PC helper (the
same setup Android uses). Each instance makes its own virtual display.

On the Deck, each PC screen is Moonlight (Flathub's `com.moonlight_stream.Moonlight`) streaming into its own window,
which the Space shows like any other app window. The code is `pocketxr/pcscreens.py`.

## One press, step by step

1. **Moonlight.** If it is missing, Pocket XR installs it from Flathub for this user (`flatpak install --user`, no
   password). This takes a few minutes the first time.
2. **The PC.** The Pocket XR PC helper is found on the network (as for PC VR). With no helper, the PC saved last time,
   else the first streaming host found by mDNS (`avahi-browse _nvstream._tcp`).
3. **The PC's screens.** Each Apollo instance answers `/serverinfo` over HTTP on its port: 47989, 48989 and 49989,
   plus any port mDNS lists for that PC. The main one comes first, then "screen 2" and "screen 3" by the names the PC
   setup gives them.
4. **Pairing, once.** Pocket XR picks a 4-digit PIN and starts `moonlight pair <pc>:47989 --pin <PIN>`. The PC helper
   types that PIN into Apollo (op `apollo-pin`), so nobody types it. If the Deck is new to the helper, a Yes on the PC
   comes first. With no helper, the PIN is shown to type on Apollo's web page. Pairing is done once Moonlight has saved
   the PC's certificate in `Moonlight.conf`. Pocket XR then closes Moonlight's "Pairing completed" window.
5. **Screens 2 and 3 with no PIN.** All Apollo instances on a PC share one certificate, and the PC copies the Deck to
   the other two within about 10 seconds. So Pocket XR copies the main host's entry in `Moonlight.conf` under the
   other instance's uuid and port, as Android saves them. If Moonlight still says that screen is not paired, the next
   press pairs that instance directly.
6. **The stream.** `moonlight stream <pc>:<port> Desktop --resolution 1920x1080 --fps 60 --bitrate 40000
   --display-mode windowed --absolute-mouse --quit-after ...`. The Space switches to App windows if it was showing the
   Deck screen. Moonlight's output goes to `~/.local/state/pocket-xr-deck/moonlight-<n>.log`.

If every screen the PC streams is open and the PC has only one Apollo, a press asks the PC helper to run
**Set up PC screens** on the PC (a Yes on the PC, then Windows asks for admin rights).

Quitting Pocket XR closes the PC screens.

## The virtual display

Moonlight on Linux does not ask Apollo for a virtual display; Android's Moonlight core does. Without one, all three
instances stream the PC's main display. So the Deck sends `"virtual": true` with `apollo-pin`. The PC helper then sets
Apollo's `always_use_virtual_display` on the Deck's client (Android repo, commit 035dd99 on feature/pc-screen). The PC
setup's copy carries it to screens 2 and 3. Not yet tried with a Deck.

## To check on a Deck

- Moonlight installs from Flathub with no password, and runs while Pocket XR runs (Desktop Mode).
- The helper types the PIN in time: Moonlight from Flatpak can take some seconds to reach its PIN step. Pocket XR
  asks the helper up to 3 times, and the helper tries for about 10 seconds each time.
- Moonlight saves the paired PC as Pocket XR expects (`[hosts]`, `srvcert`), and accepts the copied entries for
  screens 2 and 3 with no PIN.
- Three Moonlight processes at once: each saves its hosts list, and none undoes the copy.
- The windows: Moonlight's window is placed on the glasses' output and resized like other apps. The picture scales,
  `--absolute-mouse` lets the cursor move in and out, and the Deck can decode three 1080p60 streams.
- The PC's sound: only the newest session gets it.
- Closing: Pocket XR sends SIGTERM to `flatpak run`'s process group. If Flatpak hard-kills Moonlight instead, the PC's
  Desktop session is not quit (`--quit-after`). Pairing is not affected: Pocket XR stops Moonlight only after the
  pairing is saved.
- Gaming Mode: not covered. Pocket XR's Gaming Mode service shows the whole Gaming Mode picture, not app windows.
