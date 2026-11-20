"""The host's native library (libpxr_host.so, from native/host/pxr_host.c): moonlight-common-c's ENet fork for the
control stream's server and nanors' Reed-Solomon for the sound's error correction, the same code the phone's Moonlight
core speaks. The video stream is pxr-host-video, a program of its own (pocketxr/host/video.py). Both are in native/
when installed (as PC VR's libraries), in native/out/ in a checkout after native/host/build.sh.
"""
import ctypes
import os

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def path(name):
    for folder in (os.path.join(HERE, "native"), os.path.join(HERE, "native", "out")):
        p = os.path.join(folder, name)
        if os.path.exists(p):
            return p
    return os.path.join(HERE, "native", name)


EVENT_NONE, EVENT_CONNECT, EVENT_DISCONNECT, EVENT_RECEIVE = 0, 1, 2, 3

_lib = None


def lib():
    global _lib
    if _lib is None:
        l = ctypes.CDLL(path("libpxr_host.so"))
        vp = ctypes.c_void_p
        l.PxrHostEnetStart.restype = vp
        l.PxrHostEnetStart.argtypes = [ctypes.c_int, ctypes.c_int]
        l.PxrHostEnetStop.argtypes = [vp]
        l.PxrHostEnetService.argtypes = [vp, ctypes.c_int, ctypes.POINTER(vp), ctypes.POINTER(ctypes.c_uint),
                                         ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        l.PxrHostEnetSend.argtypes = [vp, vp, ctypes.c_char_p, ctypes.c_int]
        l.PxrHostEnetDisconnect.argtypes = [vp]
        l.PxrHostAudioFecEncode.argtypes = [ctypes.c_char_p, ctypes.c_int]
        _lib = l
    return _lib


def video_program():
    return path("pxr-host-video")


def audio_parity(shards, size):
    """Two Reed-Solomon parity shards for four sound packets of [size] bytes each (GameStream's own matrix)."""
    buf = ctypes.create_string_buffer(b"".join(shards) + b"\0" * (2 * size), 6 * size)
    if lib().PxrHostAudioFecEncode(buf, size) != 0:
        return None
    raw = buf.raw
    return raw[4 * size:5 * size], raw[5 * size:6 * size]
