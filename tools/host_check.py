#!/usr/bin/env python3
"""Checks a running Deck host (python3 -m pocketxr.host) from the client's side, as the phone's Moonlight core talks
to it: pairing (the PIN given through the pin file), serverinfo, the app list, launch, RTSP, the control stream (ENet,
AES-GCM: a pointer move and a key frame request), the video packets (FEC headers, frames put back together, H.264 or
HEVC key frames with their parameter sets) and the sound (decrypted and decoded with libopus), then cancel.

Run on the Deck, from the app's folder (it uses native/out/libpxr_host.so and SteamOS's openssl and libopus):
  PXR_HOST_CONFIG=<the host's config folder> python3 tools/host_check.py [--host 127.0.0.1] [--port 47989]
      [--size 1280x720] [--fps 60] [--kbps 10000] [--codec h264|hevc] [--seconds 8]
"""
import argparse
import ctypes
import ctypes.util
import hashlib
import http.client
import os
import re
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from pocketxr.host import native, ossl, rsa  # noqa: E402


def tag(xml, name):
    m = re.search(r"<%s>(.*?)</%s>" % (name, name), xml, re.S)
    return m.group(1) if m else None


class Client:
    def __init__(self, host, port, folder):
        self.host, self.port = host, port
        self.id = secrets.token_hex(8).upper()
        self.key_path = os.path.join(folder, "client.key")
        self.cert_path = os.path.join(folder, "client.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30", "-subj",
                        "/CN=NVIDIA GameStream Client", "-keyout", self.key_path, "-out", self.cert_path],
                       check=True, capture_output=True)
        with open(self.cert_path) as f:
            self.cert_pem = f.read()
        with open(self.key_path) as f:
            self.key = rsa.private_key(f.read())
        self.cert_der = rsa.pem_blocks(self.cert_pem)[0][1]
        self.https_port = port - 5

    def http(self, path, args, secure=False, timeout=30):
        q = "uniqueid=%s&uuid=%s&" % (self.id, secrets.token_hex(16)) + urllib.parse.urlencode(args)
        if secure:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.load_cert_chain(self.cert_path, self.key_path)
            c = http.client.HTTPSConnection(self.host, self.https_port, context=ctx, timeout=timeout)
        else:
            c = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        c.request("GET", "/%s?%s" % (path, q))
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r.status, body

    def pair(self, pin_file, pin="4321"):
        salt = secrets.token_bytes(16)
        aes = hashlib.sha256(salt + pin.encode()).digest()[:16]

        def give():
            time.sleep(1.0)
            with open(pin_file, "w") as f:
                f.write(pin)

        threading.Thread(target=give, daemon=True).start()
        base = {"devicename": "roth", "updateState": "1"}
        _, x = self.http("pair", dict(base, phrase="getservercert", salt=salt.hex(),
                                      clientcert=self.cert_pem.encode().hex()), timeout=150)
        assert tag(x, "paired") == "1", x
        server_der = rsa.pem_blocks(bytes.fromhex(tag(x, "plaincert")))[0][1]
        challenge = secrets.token_bytes(16)
        _, x = self.http("pair", dict(base, clientchallenge=ossl.aes_ecb(aes, challenge, True).hex()))
        dec = ossl.aes_ecb(aes, bytes.fromhex(tag(x, "challengeresponse")), False)
        server_response, server_challenge = dec[:32], dec[32:48]
        client_secret = secrets.token_bytes(16)
        digest = hashlib.sha256(server_challenge + rsa.cert_signature(self.cert_der) + client_secret).digest()
        _, x = self.http("pair", dict(base, serverchallengeresp=ossl.aes_ecb(aes, digest, True).hex()))
        ps = bytes.fromhex(tag(x, "pairingsecret"))
        server_secret, server_sig = ps[:16], ps[16:]
        assert rsa.verify(server_der, server_secret, server_sig), "the host's pairing secret is not signed by it"
        assert server_response == hashlib.sha256(challenge + rsa.cert_signature(server_der) + server_secret).digest(), \
            "the host's challenge response is wrong"
        secret = client_secret + rsa.sign(self.key, client_secret)
        _, x = self.http("pair", dict(base, clientpairingsecret=secret.hex()))
        assert tag(x, "paired") == "1", x
        _, x = self.http("pair", dict(base, phrase="pairchallenge"), secure=True)
        assert tag(x, "paired") == "1", x


def rtsp(host, port, verb, target, cseq, body=""):
    s = socket.create_connection((host, port), timeout=10)
    req = "%s %s RTSP/1.0\r\nCSeq: %d\r\nX-GS-ClientVersion: 14\r\n" % (verb, target, cseq)
    if body:
        req += "Content-type: application/sdp\r\nContent-length: %d\r\n" % len(body)
    s.sendall((req + "\r\n" + body).encode())
    data = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        data += c
    s.close()
    return data.decode()


def header(text, name):
    m = re.search(r"(?im)^%s:\s*(.*?)\r?$" % re.escape(name), text)
    return m.group(1).strip() if m else None


class Opus:
    def __init__(self):
        lib = ctypes.CDLL(ctypes.util.find_library("opus") or "libopus.so.0")
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
                                    ctypes.c_int]
        self.lib = lib
        err = ctypes.c_int()
        self.dec = lib.opus_decoder_create(48000, 2, ctypes.byref(err))
        self.pcm = (ctypes.c_int16 * (5760 * 2))()

    def decode(self, data):
        return self.lib.opus_decode(self.dec, data, len(data), self.pcm, 5760, 0)


def nal_types(data, hevc):
    out = []
    i = 0
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= len(data):
            return out
        b = data[i + 3]
        out.append((b >> 1) & 0x3F if hevc else b & 0x1F)
        i += 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=47989)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--kbps", type=int, default=10000)
    ap.add_argument("--codec", default="h264")
    ap.add_argument("--packet-size", type=int, default=1024)
    ap.add_argument("--seconds", type=float, default=8)
    ap.add_argument("--dump", help="write the frames received to this file (Annex-B), to look at")
    ap.add_argument("--cursor", help="a command that prints KWin's pointer position, run after the pointer move")
    a = ap.parse_args()
    folder = os.environ.get("PXR_HOST_CONFIG") or os.path.expanduser("~/.config/pocket-xr-deck/host")
    w, h = (int(v) for v in a.size.split("x"))
    hevc = a.codec == "hevc"
    results = []

    def ok(what, good, detail=""):
        results.append(good)
        print("%s %s%s" % ("PASS" if good else "FAIL", what, (": " + detail) if detail else ""), flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        c = Client(a.host, a.port, tmp)
        _, x = c.http("serverinfo", {})
        ok("serverinfo over HTTP", tag(x, "PairStatus") == "0" and tag(x, "appversion") == "7.1.431.-1",
           "%s, codecs %s" % (tag(x, "hostname"), tag(x, "ServerCodecModeSupport")))
        t0 = time.time()
        c.pair(os.path.join(folder, "pin.txt"))
        ok("pairing (five steps, the PIN through the pin file)", True, "%.1f s" % (time.time() - t0))
        _, x = c.http("serverinfo", {}, secure=True)
        ok("serverinfo over HTTPS, paired", tag(x, "PairStatus") == "1" and tag(x, "VirtualDisplayCapable") == "true",
           "permission %s" % tag(x, "Permission"))
        _, x = c.http("applist", {}, secure=True)
        ok("app list", tag(x, "AppTitle") == "Desktop")
        key = secrets.token_bytes(16)
        key_id = secrets.randbelow(2 ** 31)
        _, x = c.http("launch", {"appid": 1, "mode": "%dx%dx%d" % (w, h, a.fps), "additionalStates": 1, "sops": 0,
                                 "rikey": key.hex(), "rikeyid": key_id, "localAudioPlayMode": 0,
                                 "surroundAudioInfo": 196610, "remoteControllersBitmap": 0, "gcmap": 0,
                                 "virtualDisplay": 1}, secure=True)
        url = tag(x, "sessionUrl0")
        ok("launch", bool(url) and tag(x, "gamesession") == "1", url or x)
        rport = int(url.rsplit(":", 1)[1])
        base = "rtsp://%s:%d" % (a.host, rport)
        r = rtsp(a.host, rport, "OPTIONS", base, 1)
        ok("RTSP OPTIONS", r.startswith("RTSP/1.0 200"))
        r = rtsp(a.host, rport, "DESCRIBE", base, 2)
        ok("RTSP DESCRIBE", "encryptionSupported" in r, "HEVC offered" if "AAAAAU" in r else "H.264 only")
        ports, payloads = {}, {}
        connect_data = 0
        for i, kind in enumerate(("audio", "video", "control")):
            r = rtsp(a.host, rport, "SETUP", "streamid=%s/0/0" % kind, 3 + i)
            ports[kind] = int(re.search(r"server_port=(\d+)", r).group(1))
            if kind == "control":
                connect_data = int(header(r, "X-SS-Connect-Data"))
            else:
                payloads[kind] = header(r, "X-SS-Ping-Payload")
        ok("RTSP SETUP x3", len(ports) == 3, str(ports))
        sdp = "".join("a=%s:%s \r\n" % kv for kv in [
            ("x-nv-video[0].clientViewportWd", w), ("x-nv-video[0].clientViewportHt", h),
            ("x-nv-video[0].maxFPS", a.fps), ("x-nv-video[0].packetSize", a.packet_size),
            ("x-nv-vqos[0].bw.maximumBitrateKbps", a.kbps), ("x-nv-vqos[0].fec.minRequiredFecPackets", 2),
            ("x-nv-vqos[0].bitStreamFormat", 1 if hevc else 0), ("x-ss-general.encryptionEnabled", 5),
            ("x-nv-general.featureFlags", 0x27), ("x-nv-aqos.packetDuration", 5)])
        r = rtsp(a.host, rport, "ANNOUNCE", "streamid=control/13/0", 6, sdp)
        ok("RTSP ANNOUNCE", r.startswith("RTSP/1.0 200"))

        # The streams: pings first, as the client does.
        vs = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        vs.settimeout(0.2)
        vs.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        au = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        au.settimeout(0.2)
        stop = threading.Event()

        def pinger():
            n = 0
            while not stop.is_set():
                for s, kind in ((vs, "video"), (au, "audio")):
                    s.sendto(payloads[kind].encode() + struct.pack(">I", n), (a.host, ports[kind]))
                n += 1
                stop.wait(0.5)

        r = rtsp(a.host, rport, "PLAY", "/", 7)
        ok("RTSP PLAY", r.startswith("RTSP/1.0 200"))
        threading.Thread(target=pinger, daemon=True).start()

        # Control: ENet, connect data from SETUP; a pointer move to the middle and a key frame request.
        lib = native.lib()
        lib.PxrHostEnetClient.restype = ctypes.c_void_p
        lib.PxrHostEnetConnect.restype = ctypes.c_void_p
        lib.PxrHostEnetConnect.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint]
        enet = lib.PxrHostEnetClient()
        peer = lib.PxrHostEnetConnect(enet, a.host.encode(), ports["control"], connect_data)
        ev_peer, ev_data, n = ctypes.c_void_p(), ctypes.c_uint(), ctypes.c_int()
        buf = ctypes.create_string_buffer(65536)
        connected = False
        end = time.time() + 5
        while time.time() < end and not connected:
            connected = lib.PxrHostEnetService(enet, 50, ctypes.byref(ev_peer), ctypes.byref(ev_data), buf, 65536,
                                               ctypes.byref(n)) == native.EVENT_CONNECT
        ok("control stream connected (ENet)", connected)
        seq = [0]

        def control_send(kind, payload):
            plain = struct.pack("<HH", kind, len(payload)) + payload
            nonce = struct.pack("<I", seq[0]) + b"\0" * 6 + b"CC"
            cipher, gtag = ossl.gcm_encrypt(key, nonce, plain)
            msg = struct.pack("<HHI", 1, 4 + 16 + len(plain), seq[0]) + gtag + cipher
            seq[0] += 1
            lib.PxrHostEnetSend(enet, peer, msg, len(msg))

        frames, keys, bad, packets, parity = {}, 0, 0, 0, 0
        first_key_nals = None
        audio_ok = audio_bad = 0
        opus = Opus()
        t_start = time.time()
        sent_input = sent_idr = False
        while time.time() - t_start < a.seconds:
            lib.PxrHostEnetService(enet, 0, ctypes.byref(ev_peer), ctypes.byref(ev_data), buf, 65536, ctypes.byref(n))
            if not sent_input and time.time() - t_start > 1.5:
                move = struct.pack(">hhhhh", w // 2, h // 2, 0, w - 1, h - 1)
                control_send(0x0206, struct.pack(">I", 4 + len(move)) + struct.pack("<I", 0x05) + move)
                sent_input = True
            if sent_input and a.cursor and time.time() - t_start > 2.5:
                where = subprocess.run(a.cursor, shell=True, capture_output=True, text=True).stdout.strip()
                print("     pointer after the move to the middle: %s" % where, flush=True)
                a.cursor = None
            if not sent_idr and time.time() - t_start > 3:
                control_send(0x0302, b"\0" * 16)
                sent_idr = True
            try:
                data = vs.recv(65536)
                packets += 1
                fec_info = struct.unpack_from("<I", data, 28)[0]
                index, shards = (fec_info >> 12) & 0x3FF, (fec_info >> 22) & 0x3FF
                frame = struct.unpack_from("<I", data, 20)[0]
                block = (data[27] >> 4) & 3
                last_block = data[27] >> 6
                if index >= shards:
                    parity += 1
                else:
                    frames.setdefault(frame, {})[(block, index)] = (data[32:], last_block, shards)
            except socket.timeout:
                pass
            try:
                ad = au.recv(2048)
                if ad[1] == 97:
                    aseq = struct.unpack_from(">H", ad, 2)[0]
                    iv = struct.pack(">I", (key_id + aseq) & 0xFFFFFFFF) + b"\0" * 12
                    dec = ossl._run(False, ossl.crypto().EVP_aes_128_cbc(), key, iv, ad[12:], padding=True)
                    if dec and opus.decode(dec) == 240:
                        audio_ok += 1
                    else:
                        audio_bad += 1
            except socket.timeout:
                pass
        stop.set()
        whole = 0
        stream = a.dump or os.path.join(tmp, "frames.bin")
        if os.path.exists(stream):
            os.remove(stream)
        started = False  # frames go to the file from the first key frame on, as a decoder needs them
        for fi in sorted(frames):
            parts = frames[fi]
            blocks = max(v[1] for v in parts.values()) + 1
            shards = {b: v[2] for (b, i), v in parts.items()}
            if any(b not in shards or any((b, i) not in parts for i in range(shards[b])) for b in range(blocks)):
                continue  # not all of it came (the first frames, before the pings were heard)
            data = b"".join(parts[k][0] for k in sorted(parts))
            if data[0] != 1:
                bad += 1
                continue
            whole += 1
            started = started or data[3] == 2
            if started:
                last_len = struct.unpack_from("<H", data, 4)[0]
                per = a.packet_size - 16
                n_shards = len(parts)
                frame_bytes = data[:per * (n_shards - 1) + last_len][8:]
                with open(stream, "ab") as f:
                    f.write(frame_bytes)
            if data[3] == 2:
                keys += 1
                if first_key_nals is None:
                    first_key_nals = nal_types(data[8:8 + 4096], hevc)
        ok("video: packets and whole frames", whole > 0 and bad == 0,
           "%d packets (%d parity), %d frames whole, %d bad, %.1f frames/s" % (packets, parity, whole, bad,
                                                                               whole / a.seconds))
        want = (32, 33, 34) if hevc else (7, 8)
        ok("video: a key frame with its parameter sets", keys >= 1 and first_key_nals is not None and
           all(t in first_key_nals for t in want), "%d key frames, first one's NAL types %s" % (keys, first_key_nals))
        ok("video: the key frame asked for came", keys >= 2, "%d key frames" % keys)
        # Whole frames can still hold a stream no decoder takes (the Deck's hevc_vaapi, 20 Nov 2026): decode them.
        if shutil.which("ffmpeg") and os.path.exists(stream):
            r = subprocess.run(["ffmpeg", "-v", "error", "-f", "hevc" if hevc else "h264", "-i", stream, "-f", "null",
                                "-"], capture_output=True, text=True)
            errors = [ln for ln in r.stderr.splitlines()
                      if ln.strip() and not ln.startswith("[null") and "Last message repeated" not in ln]
            ok("video: the frames decode (FFmpeg)", r.returncode == 0 and not errors,
               "%d errors%s" % (len(errors), (", first: " + errors[0]) if errors else ""))
        else:
            print("     (no ffmpeg here: the frames were not decoded)", flush=True)
        ok("sound: packets decrypted and decoded", audio_ok > 0 and audio_bad == 0,
           "%d decoded, %d bad" % (audio_ok, audio_bad))
        _, x = c.http("cancel", {}, secure=True)
        ok("cancel", tag(x, "cancel") == "1")
        lib.PxrHostEnetStop(enet)
    print("%d of %d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
