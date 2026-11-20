"""The host's sound (Sunshine's audioBroadcastThread, GPLv3, as Pocket XR for Windows' HostAudio): what the Deck plays,
recorded from a sink of the stream's own (parec, 48 kHz stereo), encoded to Opus at a constant bitrate in packets of
the client's duration (5 ms), each sent as RTP payload type 97, AES-CBC encrypted when the client asks (key: the
launch's key; IV: the big-endian launch key id plus the sequence number). After every four packets, two Reed-Solomon
parity packets (payload type 127) with GameStream's own parity matrix.
"""
import ctypes
import ctypes.util
import socket
import struct
import subprocess
import threading

from . import native, ossl

RATE, CHANNELS, BITRATE = 48000, 2, 192000
OPUS_APPLICATION_RESTRICTED_LOWDELAY = 2051
OPUS_SET_BITRATE, OPUS_SET_VBR, OPUS_SET_COMPLEXITY = 4002, 4006, 4010

_opus = None


def opus():
    global _opus
    if _opus is None:
        lib = ctypes.CDLL(ctypes.util.find_library("opus") or "libopus.so.0")
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_encode.restype = ctypes.c_int
        lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        _opus = lib
    return _opus


def rtp(payload_type, seq, timestamp, payload):
    return struct.pack(">BBHII", 0x80, payload_type, seq & 0xFFFF, timestamp & 0xFFFFFFFF, 0) + payload


def parity_packet(index, seq, base_seq, base_timestamp, shard):
    """A parity packet: RTP header (payload type 127), then GameStream's 12-byte FEC header, then the shard."""
    head = struct.pack(">BBHII", 0x80, 127, seq & 0xFFFF, 0, 0)
    fec = struct.pack(">BBHII", index, 97, base_seq & 0xFFFF, base_timestamp & 0xFFFFFFFF, 0)
    return head + fec + shard


def iv_for(key_id, seq):
    return struct.pack(">I", (key_id + seq) & 0xFFFFFFFF) + b"\0" * 12


class Packetizer:
    """Opus packets to RTP, encrypted and with parity, as the phone expects them (no sockets: testable)."""

    def __init__(self, key, key_id, encrypt, duration_ms, send):
        self.key, self.key_id, self.encrypt = key, key_id, encrypt
        self.duration_ms = duration_ms
        self.send = send
        self.seq = 0
        self.timestamp = 0
        self.shards = [b""] * 4
        self.base_seq = self.base_timestamp = 0
        self.sent = self.parity_sent = 0

    def packet(self, opus_bytes):
        payload = ossl.aes_cbc_encrypt(self.key, iv_for(self.key_id, self.seq), opus_bytes) if self.encrypt \
            else bytes(opus_bytes)
        self.send(rtp(97, self.seq, self.timestamp, payload))
        self.sent += 1
        slot = self.seq % 4
        if slot == 0:
            self.base_seq, self.base_timestamp = self.seq, self.timestamp
        self.shards[slot] = payload
        if slot == 3 and len({len(s) for s in self.shards}) == 1:
            parity = native.audio_parity(self.shards, len(payload))
            if parity:
                for x in range(2):
                    self.send(parity_packet(x, self.seq + x + 1, self.base_seq, self.base_timestamp, parity[x]))
                    self.parity_sent += 1
        self.seq = (self.seq + 1) & 0xFFFF
        self.timestamp += self.duration_ms


class Audio:
    def __init__(self, port, key, key_id, encrypt, duration_ms, monitor, log=print):
        self.log = log
        self.duration_ms = duration_ms if duration_ms in (5, 10, 20) else 5
        self.frame = RATE * self.duration_ms // 1000
        self.monitor = monitor
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)  # EF: voice
        except OSError:
            pass
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(0.5)
        self.client = None
        self.stopping = False
        self.proc = None
        self.packets = Packetizer(key, key_id, encrypt, self.duration_ms, self._send)
        threading.Thread(target=self._pings, daemon=True, name="host-audio-ping").start()

    def _send(self, packet):
        to = self.client
        if to:
            try:
                self.sock.sendto(packet, to)
            except OSError:
                pass

    def _pings(self):
        while not self.stopping:
            try:
                data, addr = self.sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) >= 4:
                if self.client is None:
                    self.log("host: sound client at %s:%d" % addr)
                self.client = addr

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="host-audio").start()

    def _run(self):
        try:
            err = ctypes.c_int(0)
            enc = opus().opus_encoder_create(RATE, CHANNELS, OPUS_APPLICATION_RESTRICTED_LOWDELAY, ctypes.byref(err))
            if not enc:
                self.log("host: no sound: Opus %d" % err.value)
                return
            opus().opus_encoder_ctl(ctypes.c_void_p(enc), OPUS_SET_BITRATE, ctypes.c_int(BITRATE))
            opus().opus_encoder_ctl(ctypes.c_void_p(enc), OPUS_SET_VBR, ctypes.c_int(0))
            opus().opus_encoder_ctl(ctypes.c_void_p(enc), OPUS_SET_COMPLEXITY, ctypes.c_int(5))
        except OSError as e:
            self.log("host: no sound: %s" % e)
            return
        try:
            self.proc = subprocess.Popen(["parec", "--device=" + self.monitor, "--rate=%d" % RATE,
                                          "--channels=%d" % CHANNELS, "--format=s16le", "--raw",
                                          "--latency-msec=%d" % self.duration_ms, "--process-time-msec=%d" % self.duration_ms],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            self.log("host: no sound: parec: %s" % e)
            opus().opus_encoder_destroy(enc)
            return
        self.log("host: sound from %s, Opus %d kbps in %d ms packets, %s" % (
            self.monitor, BITRATE // 1000, self.duration_ms, "encrypted" if self.packets.encrypt else "not encrypted"))
        need = self.frame * CHANNELS * 2
        out = ctypes.create_string_buffer(1400)
        pending = b""
        try:
            while not self.stopping:
                chunk = self.proc.stdout.read1(65536) if hasattr(self.proc.stdout, "read1") else self.proc.stdout.read(need)
                if not chunk:
                    break
                pending += chunk
                while len(pending) >= need:
                    pcm, pending = pending[:need], pending[need:]
                    if self.client is None:
                        continue
                    n = opus().opus_encode(enc, pcm, self.frame, out, 1400)
                    if n > 0:
                        self.packets.packet(out.raw[:n])
        finally:
            opus().opus_encoder_destroy(enc)

    @property
    def sent(self):
        return self.packets.sent

    def close(self):
        self.stopping = True
        if self.proc:
            self.proc.terminate()
        try:
            self.sock.close()
        except OSError:
            pass
