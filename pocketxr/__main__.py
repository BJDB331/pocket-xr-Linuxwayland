import subprocess
import sys

try:
    from .app import main
except OSError as e:
    # A system library it loads is missing (SDL2, cairo, librsvg, OpenGL): say which, not a traceback. SteamOS has
    # them all; install.sh offers them elsewhere.
    text = f"Pocket XR can't start: {e}. On Ubuntu: sudo apt install libsdl2-2.0-0 libcairo2 librsvg2-2 libgl1 python3-tk"
    print(text, file=sys.stderr)
    try:
        subprocess.Popen(["notify-send", "-a", "Pocket XR", "Pocket XR", text])
    except OSError:
        pass
    sys.exit(1)

main()
