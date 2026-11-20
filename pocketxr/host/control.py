"""The host's control stream (Sunshine's control_server_t, as Pocket XR for Windows' HostControl): an ENet server on
the control port. Every message is AES-GCM with the launch's key (CONTROL_V2: a 12-byte nonce of the sequence number,
then 'C','C' from the client or 'H','C' from the host). Handled: key frame requests (an invalidation is taken as one),
input, and the end of the stream. The ENet calls all run on this class's own thread.
"""
import ctypes
import struct
import threading

from . import native, ossl

TYPE_IDR, TYPE_INVALIDATE, TYPE_LOSS_STATS, TYPE_INPUT = 0x0302, 0x0301, 0x0201, 0x0206
TYPE_TERMINATION, TYPE_PERIODIC_PING = 0x0109, 0x0200
ENCRYPTED = 0x0001


def decrypt(key, message):
    """(type, payload) of one encrypted control message, or None."""
    if len(message) < 8 + 16 + 4 or struct.unpack_from("<H", message)[0] != ENCRYPTED:
        return None
    length, seq = struct.unpack_from("<HI", message, 2)
    plain_len = length - 4 - 16
    if plain_len < 4 or 4 + length > len(message):
        return None
    nonce = struct.pack("<I", seq) + b"\0" * 6 + b"CC"
    plain = ossl.gcm_decrypt(key, nonce, bytes(message[24:24 + plain_len]), bytes(message[8:24]))
    if plain is None:
        return None
    return struct.unpack_from("<H", plain)[0], plain[4:]


def encrypt(key, seq, kind, payload):
    plain = struct.pack("<HH", kind, len(payload)) + payload
    nonce = struct.pack("<I", seq) + b"\0" * 6 + b"HC"
    cipher, tag = ossl.gcm_encrypt(key, nonce, plain)
    return struct.pack("<HHI", ENCRYPTED, 4 + 16 + len(plain), seq) + tag + cipher


class Control:
    def __init__(self, port, key, log=print, on_input=None, on_idr=None, on_gone=None):
        self.log = log
        self.key = key
        self.on_input, self.on_idr, self.on_gone = on_input, on_idr, on_gone
        self.host = native.lib().PxrHostEnetStart(port, 4)
        if not self.host:
            raise OSError("the control port %d could not be opened" % port)
        self.peer = None
        self.seq = 0
        self.connected = False
        self.stopping = False
        self.terminate_wanted = False
        self.inputs = 0
        self.thread = threading.Thread(target=self._run, daemon=True, name="host-control")
        self.thread.start()

    def _run(self):
        lib = native.lib()
        buf = ctypes.create_string_buffer(65536)
        peer = ctypes.c_void_p()
        data = ctypes.c_uint()
        n = ctypes.c_int()
        while not self.stopping:
            kind = lib.PxrHostEnetService(self.host, 50, ctypes.byref(peer), ctypes.byref(data), buf, len(buf),
                                          ctypes.byref(n))
            if kind == native.EVENT_CONNECT:
                self.peer = peer.value
                self.connected = True
                self.log("host: control stream connected")
            elif kind == native.EVENT_DISCONNECT:
                self.log("host: control stream disconnected")
                self.connected = False
                self.peer = None
                if self.on_gone and not self.stopping:
                    threading.Thread(target=self.on_gone, daemon=True).start()
            elif kind == native.EVENT_RECEIVE:
                self._received(buf.raw[:min(n.value, len(buf))])
            if self.terminate_wanted and self.peer:
                self.terminate_wanted = False
                self._send(TYPE_TERMINATION, b"\x80\x03\x00\x23")  # ended by the host, gracefully (big-endian)
        lib.PxrHostEnetStop(self.host)
        self.host = None

    def _received(self, message):
        got = decrypt(self.key, message)
        if got is None:
            self.log("host: a control message did not decrypt")
            return
        kind, payload = got
        if kind in (TYPE_IDR, TYPE_INVALIDATE):
            self.log("host: the phone asked for a key frame (0x%04x)" % kind)
            if self.on_idr:
                self.on_idr()
        elif kind == TYPE_INPUT:
            if self.inputs == 0:
                self.log("host: first input from the phone")
            self.inputs += 1
            if self.on_input:
                self.on_input(payload)

    def _send(self, kind, payload):
        msg = encrypt(self.key, self.seq, kind, payload)
        self.seq += 1
        native.lib().PxrHostEnetSend(self.host, self.peer, msg, len(msg))

    def terminate(self):
        """Tells the phone the stream has ended (sent from the control thread)."""
        self.terminate_wanted = True

    def close(self):
        self.stopping = True
        self.thread.join(2)
