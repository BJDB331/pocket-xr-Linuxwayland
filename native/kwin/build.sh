#!/bin/bash
# Builds pxr-kwin (pxr_kwin.c): the screen's and app windows' pictures, the window list and the pointer on KDE Plasma
# on Wayland, through KWin's own Wayland interfaces. Needs gcc, wayland-scanner and the Wayland and PipeWire
# development files (Ubuntu: libwayland-dev libpipewire-0.3-dev). Built on Ubuntu 24.04, it runs where libwayland 1.22
# and PipeWire 1.0 or newer are (SteamOS 3.7 and later, Kubuntu 24.04 and later, Fedora 40, Arch). Output in ../out/.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../out"
GEN="${GEN:-$(mktemp -d)}"
mkdir -p "$OUT" "$GEN"
SRC=()
for xml in "$HERE"/third_party/kde/*.xml "$HERE"/third_party/wayland-protocols/*.xml; do
  base="$(basename "$xml" .xml)"
  wayland-scanner client-header "$xml" "$GEN/$base-client-protocol.h" 2>/dev/null
  wayland-scanner private-code "$xml" "$GEN/$base-protocol.c" 2>/dev/null
  SRC+=("$GEN/$base-protocol.c")
done
gcc -O2 -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers -o "$OUT/pxr-kwin" "$HERE/pxr_kwin.c" \
    "${SRC[@]}" -I"$GEN" $(pkg-config --cflags --libs wayland-client libpipewire-0.3) -lpthread
ls -la "$OUT/pxr-kwin"
readelf -d "$OUT/pxr-kwin" | grep NEEDED
