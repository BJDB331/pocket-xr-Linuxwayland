#!/bin/bash
# Builds the Deck host's native pieces (Linux x86_64), in WSL Ubuntu or any Linux with gcc, wayland-scanner and the
# PipeWire and Wayland development files:
#   - pxr-host-video: one PC screen's picture, from a KWin virtual output to the phone (pxr_host_video.c);
#   - libpxr_host.so: the control stream's ENet server and the sound's Reed-Solomon, for the Python host (pxr_host.c).
# FFmpeg 8.1's headers (FFMPEG_SRC) with the Deck's own libavcodec.so.62, libavutil.so.60 and libavfilter.so.11, copied
# from SteamOS into ../deck-libs/ (not in git). Output in ../out/.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/../out"
LIBS="$HERE/../deck-libs"
FFMPEG_SRC="${FFMPEG_SRC:-$HOME/pxr-deck-native/ff8/ffmpeg-8.1}"
GEN="$HOME/pxr-deck-native/ff8/gen"
mkdir -p "$OUT" "$GEN/libavutil"

if [ ! -d "$FFMPEG_SRC" ]; then
  (cd "$(dirname "$FFMPEG_SRC")" && tar xf ffmpeg-8.1.tar.xz)
fi
printf '#ifndef AVUTIL_AVCONFIG_H\n#define AVUTIL_AVCONFIG_H\n#define AV_HAVE_BIGENDIAN 0\n#define AV_HAVE_FAST_UNALIGNED 1\n#endif\n' > "$GEN/libavutil/avconfig.h"

# Linked by name to the Deck's own libraries (sonames), found at run time in /usr/lib.
LINK="$GEN/link"
mkdir -p "$LINK"
cp "$LIBS/libavcodec.so.62.28.102" "$LINK/libavcodec.so.62"
cp "$LIBS/libavutil.so.60.26.102" "$LINK/libavutil.so.60"
cp "$LIBS/libavfilter.so.11.14.102" "$LINK/libavfilter.so.11"

PROTO="$GEN/proto"
mkdir -p "$PROTO"
# (Its validation warns about "deprecated-since", newer than this wayland-scanner: harmless.)
wayland-scanner client-header "$HERE/third_party/kde/zkde-screencast-unstable-v1.xml" "$PROTO/zkde-screencast-unstable-v1-client-protocol.h" 2>/dev/null
wayland-scanner private-code "$HERE/third_party/kde/zkde-screencast-unstable-v1.xml" "$PROTO/zkde-screencast-unstable-v1-protocol.c" 2>/dev/null

NANORS="$HERE/third_party/nanors"
RS_SRC="$NANORS/rs.c $NANORS/deps/obl/oblas_lite.c $NANORS/deps/obl/oblas_common.c"
RS_INC="-I$NANORS -I$NANORS/deps -I$NANORS/deps/obl"

gcc -O2 -Wall -Wno-deprecated-declarations -o "$OUT/pxr-host-video" "$HERE/pxr_host_video.c" \
    "$PROTO/zkde-screencast-unstable-v1-protocol.c" $RS_SRC \
    -I"$PROTO" -I"$GEN" -I"$FFMPEG_SRC" $RS_INC \
    $(pkg-config --cflags libpipewire-0.3 wayland-client) \
    "$LINK/libavcodec.so.62" "$LINK/libavutil.so.60" "$LINK/libavfilter.so.11" \
    $(pkg-config --libs libpipewire-0.3 wayland-client) -lpthread -lm \
    -Wl,--allow-shlib-undefined

ENET="$HERE/third_party/enet"
gcc -shared -fPIC -O2 -Wall -fvisibility=hidden -o "$OUT/libpxr_host.so" "$HERE/pxr_host.c" \
    "$ENET"/{callbacks,compress,host,list,packet,peer,protocol,unix}.c $RS_SRC \
    -I"$ENET/include" $RS_INC \
    -DHAS_FCNTL=1 -DHAS_IOCTL=1 -DHAS_POLL=1 -DHAS_GETADDRINFO=1 -DHAS_GETNAMEINFO=1 -DHAS_GETHOSTBYNAME_R=1 \
    -DHAS_GETHOSTBYADDR_R=1 -DHAS_INET_PTON=1 -DHAS_INET_NTOP=1 -DHAS_MSGHDR_FLAGS=1 -DHAS_SOCKLEN_T=1

ls -la "$OUT/pxr-host-video" "$OUT/libpxr_host.so"
readelf -d "$OUT/pxr-host-video" | grep -E "NEEDED"
nm -D --defined-only "$OUT/libpxr_host.so" | grep -E " T Pxr" || true
