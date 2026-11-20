"""One stream's picture: the native program pxr-host-video (native/host/pxr_host_video.c), which makes the KWin virtual
output, captures it, encodes it on the graphics card and sends Moonlight's video packets. This side starts it, reads
its lines and asks it for key frames.
"""
import os
import subprocess
import threading

from . import native

CODECS = ("h264", "hevc", "av1")


def probe(log=print):
    """The codecs this Deck encodes, e.g. ["h264", "hevc"] ([] if the program does not run)."""
    try:
        r = subprocess.run([native.video_program(), "--probe"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        log("host: the video program did not run: %s" % e)
        return []
    for line in r.stdout.splitlines():
        if line.startswith("broken"):
            log("host: encoder " + line)
        if line.startswith("codecs"):
            return [c for c in line.split()[1:] if c in CODECS]
    return []


def command(width, height, fps, kbps, codec, port, packet_size, min_fec, name, description):
    return [native.video_program(), "--width", str(width), "--height", str(height), "--fps", str(fps),
            "--kbps", str(kbps), "--codec", codec, "--port", str(port), "--packet-size", str(packet_size),
            "--min-fec", str(min_fec), "--name", name, "--description", description]


class Video:
    def __init__(self, args, log=print, on_output=None, on_client=None, on_end=None):
        self.log = log
        self.on_output, self.on_client, self.on_end = on_output, on_client, on_end
        self.output = None
        self.closing = False
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, env=os.environ.copy())
        threading.Thread(target=self._read, daemon=True, name="host-video").start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            if line.startswith("output "):
                self.output = line.split()[1]
                self.log("host: video: " + line)
                if self.on_output:
                    self.on_output(self.output)
            elif line.startswith("client ") and self.on_client:
                self.log("host: video: " + line)
                self.on_client()
            elif line:
                self.log("host: video: " + line)
        self.proc.wait()
        if self.on_end and not self.closing:
            self.on_end(self.proc.returncode)

    def _say(self, text):
        try:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def idr(self):
        self._say("idr")

    def close(self):
        self.closing = True
        self._say("quit")
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(2)
