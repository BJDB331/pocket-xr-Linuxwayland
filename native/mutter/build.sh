#!/bin/bash
# Builds pxr-mutter (pxr_mutter.c): the screen's picture and the pointer on GNOME on Wayland, through Mutter's screen
# cast and remote desktop interfaces. Needs gcc and the GLib and PipeWire development files (Ubuntu:
# libglib2.0-dev libpipewire-0.3-dev). Built on Ubuntu 24.04, it runs where GLib 2.80 and PipeWire 1.0 or newer are,
# which Ubuntu 24.04 and later have. Output in ../out/.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../out"
mkdir -p "$OUT"
gcc -O2 -Wall -Wextra -Wno-unused-parameter -o "$OUT/pxr-mutter" "$HERE/pxr_mutter.c" \
    $(pkg-config --cflags --libs gio-2.0 libpipewire-0.3)
ls -la "$OUT/pxr-mutter"
readelf -d "$OUT/pxr-mutter" | grep NEEDED
