"""The Deck host (pocketxr/host), the parts that need no Deck: OpenSSL's AES through ctypes, pairing's RSA, the five
pairing steps from the phone's side, the control stream's messages, the phone's input, the outputs' layout, the sound
packets, the helper's answers, serverinfo, RTSP and the PC Screens app's settings taken over. The whole stream on a
Deck is checked by tools/host_check.py. Keys in tests/data/host are for these tests only."""
import hashlib
import json
import os
import secrets
import shutil
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from pocketxr.host import audio, control, desktop, helper, identity, inputs, ossl, rsa, server  # noqa: E402

DATA = os.path.join(HERE, "data", "host")


def read(name):
    with open(os.path.join(DATA, name), encoding="ascii") as f:
        return f.read()


# ---- OpenSSL ----

def test_aes_ecb_is_fips_197():
    key = bytes(range(16))
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    enc = ossl.aes_ecb(key, plain, True)
    assert enc.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    assert ossl.aes_ecb(key, enc, False) == plain


def test_aes_ecb_pads_to_whole_blocks():
    key = secrets.token_bytes(16)
    assert len(ossl.aes_ecb(key, b"x" * 20, True)) == 32


def test_gcm_round_trip_and_a_wrong_tag():
    key, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    cipher, tag = ossl.gcm_encrypt(key, nonce, b"hello phone")
    assert len(tag) == 16 and cipher != b"hello phone"
    assert ossl.gcm_decrypt(key, nonce, cipher, tag) == b"hello phone"
    assert ossl.gcm_decrypt(key, nonce, cipher, bytes(16)) is None


def test_cbc_pads_with_pkcs7():
    key, iv = secrets.token_bytes(16), bytes(16)
    assert len(ossl.aes_cbc_encrypt(key, iv, b"a" * 16)) == 32
    assert len(ossl.aes_cbc_encrypt(key, iv, b"a" * 5)) == 16


# ---- RSA ----

def test_sign_and_verify():
    key = rsa.private_key(read("host-test.key"))
    der = rsa.pem_blocks(read("host-test.pem"))[0][1]
    sig = rsa.sign(key, b"secret")
    assert rsa.verify(der, b"secret", sig)
    assert not rsa.verify(der, b"secreT", sig)
    other = rsa.pem_blocks(read("phone-test.pem"))[0][1]
    assert not rsa.verify(other, b"secret", sig)


def test_certificate_signature_is_the_last_bit_string():
    der = rsa.pem_blocks(read("host-test.pem"))[0][1]
    sig = rsa.cert_signature(der)
    assert len(sig) == 128 and der.endswith(sig)


def test_der_to_pem_round_trip():
    der = rsa.pem_blocks(read("phone-test.pem"))[0][1]
    assert rsa.pem_blocks(rsa.der_to_pem(der))[0][1] == der


# ---- a host on temporary folders ----

class FakeHost:
    """server.Host's parts that a Screen uses, without sockets, encoders or devices."""

    def __init__(self, folder, codecs=("h264", "hevc")):
        self.log = lambda text: None
        os.makedirs(folder, exist_ok=True)
        shutil.copy(os.path.join(DATA, "host-test.pem"), os.path.join(folder, "cert.pem"))
        shutil.copy(os.path.join(DATA, "host-test.key"), os.path.join(folder, "key.pem"))
        self.identity = identity.Identity(folder, old=os.path.join(folder, "none"), log=self.log)
        self.name = "steamdeck"
        self.codecs = list(codecs)
        self.codec_support = 0x101
        self.pins = server.PinBox(os.path.join(folder, "pin.txt"))
        self.ports = [47989]


def make_screen(tmp, index=0, port=47989):
    return server.Screen(FakeHost(os.path.join(tmp, "host")), index, port)


def tag(xml, name):
    import re
    m = re.search(r"<%s>(.*?)</%s>" % (name, name), xml, re.S)
    return m.group(1) if m else None


def pair_as_phone(screen, pin="1234", given_pin=None):
    """The phone's side of pairing (moonlight's PairingManager), straight against Screen.pair. True if paired."""
    phone_pem, phone_key = read("phone-test.pem"), rsa.private_key(read("phone-test.key"))
    phone_der = rsa.pem_blocks(phone_pem)[0][1]
    salt = secrets.token_bytes(16)
    aes = hashlib.sha256(salt + pin.encode()).digest()[:16]
    threading.Thread(target=lambda: (time.sleep(0.3), screen.host.pins.submit(given_pin or pin, "Test phone")),
                     daemon=True).start()
    q = {"uniqueid": "PHONE1", "devicename": "roth", "phrase": "getservercert", "salt": salt.hex(),
         "clientcert": phone_pem.encode().hex()}
    x = screen.pair(q, False, False)
    if tag(x, "paired") != "1":
        return False
    server_der = rsa.pem_blocks(bytes.fromhex(tag(x, "plaincert")))[0][1]
    challenge = secrets.token_bytes(16)
    x = screen.pair({"uniqueid": "PHONE1", "clientchallenge": ossl.aes_ecb(aes, challenge, True).hex()}, False, False)
    dec = ossl.aes_ecb(aes, bytes.fromhex(tag(x, "challengeresponse")), False)
    server_response, server_challenge = dec[:32], dec[32:48]
    secret = secrets.token_bytes(16)
    digest = hashlib.sha256(server_challenge + rsa.cert_signature(phone_der) + secret).digest()
    x = screen.pair({"uniqueid": "PHONE1", "serverchallengeresp": ossl.aes_ecb(aes, digest, True).hex()}, False, False)
    ps = bytes.fromhex(tag(x, "pairingsecret"))
    assert rsa.verify(server_der, ps[:16], ps[16:])
    if server_response != hashlib.sha256(challenge + rsa.cert_signature(server_der) + ps[:16]).digest():
        return False  # a wrong PIN: the phone stops here
    x = screen.pair({"uniqueid": "PHONE1", "clientpairingsecret": (secret + rsa.sign(phone_key, secret)).hex()},
                    False, False)
    return tag(x, "paired") == "1"


def test_pairing_with_the_right_pin_pins_the_phone():
    with tempfile.TemporaryDirectory() as tmp:
        s = make_screen(tmp)
        assert pair_as_phone(s)
        der = rsa.pem_blocks(read("phone-test.pem"))[0][1]
        assert s.host.identity.knows(der)
        assert s.host.identity.clients[0]["name"] == "Test phone"  # the name the helper gave with the PIN
        # paired once is paired for every PC screen: one identity
        assert server.Screen(s.host, 1, 48989).host.identity.knows(der)


def test_pairing_with_a_wrong_pin_pins_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        s = make_screen(tmp)
        assert not pair_as_phone(s, pin="1234", given_pin="9999")
        assert s.host.identity.count() == 0


def test_pairing_out_of_order_fails():
    with tempfile.TemporaryDirectory() as tmp:
        x = make_screen(tmp).pair({"uniqueid": "X", "clientchallenge": "00" * 16}, False, False)
        assert tag(x, "paired") == "0"


def test_pin_box_takes_a_pin_only_while_one_is_wanted():
    with tempfile.TemporaryDirectory() as tmp:
        box = server.PinBox(os.path.join(tmp, "pin.txt"))
        assert not box.submit("1234")
        threading.Thread(target=lambda: (time.sleep(0.2), box.submit("12a4"), box.submit("4321", "Phone")),
                         daemon=True).start()
        assert box.wait(5) == "4321" and box.name == "Phone"


def test_pin_box_reads_the_pin_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pin.txt")
        box = server.PinBox(path)

        def give():
            time.sleep(0.3)
            with open(path, "w") as f:
                f.write("5678\n")

        threading.Thread(target=give, daemon=True).start()
        assert box.wait(5) == "5678"
        assert not os.path.exists(path)


def test_serverinfo_unpaired_and_paired():
    with tempfile.TemporaryDirectory() as tmp:
        s = make_screen(tmp, 1, 48989)
        x = s.serverinfo(False, "192.168.1.23")
        assert tag(x, "PairStatus") == "0" and tag(x, "Permission") == "0"
        assert tag(x, "hostname") == "steamdeck screen 2" and tag(x, "HttpsPort") == "48984"
        assert tag(x, "appversion") == "7.1.431.-1" and tag(x, "VirtualDisplayCapable") is None
        x = s.serverinfo(True, "192.168.1.23")
        assert tag(x, "PairStatus") == "1" and int(tag(x, "Permission")) & 0x4001800 == 0x4001800
        assert tag(x, "VirtualDisplayCapable") == "true" and tag(x, "state") == "SUNSHINE_SERVER_FREE"


def test_unpaired_phone_cannot_launch():
    with tempfile.TemporaryDirectory() as tmp:
        x = make_screen(tmp).handle("/launch?appid=1&mode=1280x720x60", True, False, None, "1.2.3.4", "5.6.7.8")
        assert 'status_code="401"' in x


def test_launch_and_rtsp_setup():
    with tempfile.TemporaryDirectory() as tmp:
        s = make_screen(tmp)
        key = secrets.token_bytes(16).hex()
        x = s.handle("/launch?appid=1&mode=1784x976x120&rikey=%s&rikeyid=-5&localAudioPlayMode=0&virtualDisplay=1"
                     % key, True, True, None, "192.168.1.23", "192.168.1.17")
        assert tag(x, "sessionUrl0") == "rtsp://192.168.1.23:48010" and tag(x, "gamesession") == "1"
        sess = s.session
        assert (sess.width, sess.height, sess.fps) == (1784, 976, 120) and sess.key.hex() == key
        assert sess.key_id == 0xFFFFFFFB  # the phone's id as its 32 bits
        _, sdp, status = s.rtsp("DESCRIBE", "rtsp://x", "")
        assert status == 200 and "sprop-parameter-sets=AAAAAU" in sdp and "AV1" not in sdp
        opts, _, _ = s.rtsp("SETUP", "streamid=video/0/0", "")
        assert ("Transport", "server_port=47998") in opts
        assert dict(opts)["X-SS-Ping-Payload"] == sess.ping_payload
        opts, _, _ = s.rtsp("SETUP", "streamid=control/13/0", "")
        assert ("Transport", "server_port=47999") in opts and dict(opts)["X-SS-Connect-Data"] == str(sess.connect_data)
        opts, _, _ = s.rtsp("SETUP", "streamid=audio/0/0", "")
        assert ("Transport", "server_port=48000") in opts
        s.announce(sess, "a=x-nv-video[0].clientViewportWd:1784 \r\na=x-nv-video[0].clientViewportHt:976 \r\n"
                         "a=x-nv-video[0].maxFPS:120 \r\na=x-nv-video[0].packetSize:1392 \r\n"
                         "a=x-nv-vqos[0].bw.maximumBitrateKbps:77000 \r\na=x-nv-vqos[0].fec.minRequiredFecPackets:2 \r\n"
                         "a=x-nv-vqos[0].bitStreamFormat:2 \r\na=x-ss-general.encryptionEnabled:1 \r\n"
                         "a=x-nv-general.featureFlags:39 \r\na=x-nv-aqos.packetDuration:5 \r\n")
        assert sess.kbps == 77000 and sess.packet_size == 1392 and sess.audio_encrypted
        assert sess.codec == "h264"  # AV1 asked, which this Deck does not encode


def test_no_session_no_setup():
    with tempfile.TemporaryDirectory() as tmp:
        assert make_screen(tmp).rtsp("SETUP", "streamid=video/0/0", "")[2] == 404


def test_a_busy_port_leaves_that_screen_out():
    import socket
    with tempfile.TemporaryDirectory() as tmp:
        busy = socket.socket()
        busy.bind(("0.0.0.0", 0))
        busy.listen(1)
        taken = busy.getsockname()[1]
        h = FakeHost(os.path.join(tmp, "host"))
        h.screens = [server.Screen(h, 0, taken), server.Screen(h, 1, 0)]
        h.screens[1].http_port = h.screens[1].https_port = h.screens[1].rtsp_port = 0
        h.inputs = type("I", (), {"start": lambda self: None})()
        h.identity.count = lambda: 0
        server.Host.start(h)
        try:
            assert h.screens and all(s.http_port != taken for s in h.screens)
        finally:
            for s in h.screens:
                s.stop()
            busy.close()


def test_xml_escapes():
    assert server.xml(400, 'a "b" <c>', "") == \
        '<?xml version="1.0" encoding="utf-8"?><root status_code="400" status_message="a &quot;b&quot; &lt;c&gt;"></root>'


# ---- the PC Screens app's settings taken over ----

def test_identity_takes_the_sunshine_certificate_ids_and_phones():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.path.join(tmp, "pocketxr")
        os.makedirs(os.path.join(old, "sunshine-2"))
        shutil.copy(os.path.join(DATA, "host-test.pem"), os.path.join(old, "sunshine-cert.pem"))
        shutil.copy(os.path.join(DATA, "host-test.key"), os.path.join(old, "sunshine-key.pem"))
        state = {"root": {"uniqueid": "OLD-SCREEN-2", "named_devices": [
            {"name": "Mike's phone", "uuid": "U1", "cert": read("phone-test.pem")}]}}
        with open(os.path.join(old, "sunshine-2", "sunshine_state.json"), "w") as f:
            json.dump(state, f)
        ident = identity.Identity(os.path.join(tmp, "host"), old=old, log=lambda t: None)
        assert ident.cert_pem == read("host-test.pem")
        assert ident.knows(rsa.pem_blocks(read("phone-test.pem"))[0][1])
        assert ident.unique_id(48989) == "OLD-SCREEN-2"
        assert ident.unique_id(47989) != "OLD-SCREEN-2"
        assert ident.unique_id(47989) == ident.unique_id(47989)


# ---- control stream ----

def test_control_message_from_the_phone():
    key = secrets.token_bytes(16)
    plain = struct.pack("<HH", control.TYPE_IDR, 2) + b"\0\0"
    nonce = struct.pack("<I", 7) + b"\0" * 6 + b"CC"
    cipher, gtag = ossl.gcm_encrypt(key, nonce, plain)
    msg = struct.pack("<HHI", 1, 4 + 16 + len(plain), 7) + gtag + cipher
    assert control.decrypt(key, msg) == (control.TYPE_IDR, b"\0\0")
    assert control.decrypt(secrets.token_bytes(16), msg) is None


def test_control_message_to_the_phone():
    key = secrets.token_bytes(16)
    msg = control.encrypt(key, 3, control.TYPE_TERMINATION, b"\x80\x03\x00\x23")
    length, seq = struct.unpack_from("<HI", msg, 2)
    assert seq == 3 and length == len(msg) - 4
    nonce = struct.pack("<I", 3) + b"\0" * 6 + b"HC"
    plain = ossl.gcm_decrypt(key, nonce, msg[24:], msg[8:24])
    assert plain == struct.pack("<HH", control.TYPE_TERMINATION, 4) + b"\x80\x03\x00\x23"


# ---- input ----

def packet(kind, body):
    return struct.pack(">I", 4 + len(body)) + struct.pack("<I", kind) + body


def test_input_parse():
    data = packet(inputs.ABS_MOVE, struct.pack(">hhhhh", 10, 20, 0, 99, 49)) + packet(inputs.BUTTON_DOWN, b"\x01")
    assert inputs.parse(data) == [(inputs.ABS_MOVE, struct.pack(">hhhhh", 10, 20, 0, 99, 49)),
                                  (inputs.BUTTON_DOWN, b"\x01")]
    assert inputs.parse(b"\x00\x00\x00\x40junk") == []


class FakeDevice:
    def __init__(self):
        self.events = []

    def emit(self, events):
        self.events.append(events)


def fake_inputs(desktop=(0, 0, 2549, 900)):
    i = inputs.Inputs(lambda t: None, desktop=lambda: desktop)
    i.keyboard, i.mouse, i.pointer = FakeDevice(), FakeDevice(), FakeDevice()
    return i


def test_position_lands_on_its_screen():
    i = fake_inputs()
    area = (949, 0, 1600, 900)  # the virtual output beside the Deck's panel
    i.handle(packet(inputs.ABS_MOVE, struct.pack(">hhhhh", 800, 450, 0, 1600, 900)), area)
    (_, _, ax), (_, _, ay) = i.pointer.events[-1]
    assert abs(ax / inputs.ABS_RANGE * 2548 - (949 + 800 * 1599 / 1600)) < 1.5
    assert abs(ay / inputs.ABS_RANGE * 899 - 450 * 899 / 900) < 1.5


def test_the_same_position_again_still_moves():
    i = fake_inputs()
    i.put(100, 100)
    i.put(100, 100)
    assert len(i.pointer.events) == 3  # the second time: one unit off first, then the position


def test_relative_moves_stay_on_the_screen():
    i = fake_inputs()
    area = (949, 0, 1600, 900)
    for _ in range(50):
        i.handle(packet(inputs.REL_MOVE, struct.pack(">hh", 100, -100)), area)
    assert i.pos == (949 + 1599, 0)


def test_keys_buttons_wheel_and_text():
    i = fake_inputs()
    i.handle(packet(inputs.KEY_DOWN, b"\x00" + struct.pack("<h", 0x41) + b"\x00"), (0, 0, 10, 10))
    assert i.keyboard.events[-1] == [(inputs.EV_KEY, 30, 1)]  # A
    i.handle(packet(inputs.BUTTON_UP, b"\x03"), (0, 0, 10, 10))
    assert i.pointer.events[-1] == [(inputs.EV_KEY, inputs.BTN_RIGHT, 0)]
    i.handle(packet(inputs.SCROLL, struct.pack(">h", 60)), (0, 0, 10, 10))
    assert i.mouse.events[-1] == [(inputs.EV_REL, inputs.REL_WHEEL_HI_RES, 60)]
    i.handle(packet(inputs.SCROLL, struct.pack(">h", 60)), (0, 0, 10, 10))
    assert (inputs.EV_REL, inputs.REL_WHEEL, 1) in i.mouse.events[-1]
    i.handle(packet(inputs.TEXT, "Hi!".encode()), (0, 0, 10, 10))
    assert i.keyboard.events[-3][0] == (inputs.EV_KEY, inputs.KEY_LEFTSHIFT, 1)  # H, shifted
    assert inputs.text_keys("é") == []


# ---- the outputs ----

def test_logical_size_of_the_turned_panel():
    assert desktop.logical_size({"size": {"width": 1280, "height": 800}, "scale": 1.35, "rotation": 8}) == (949, 593)


def test_virtual_outputs_side_by_side():
    outs = [{"name": "eDP-1", "x": 0, "y": 0, "w": 949, "h": 593},
            {"name": "Virtual-pxr-48989", "x": 949, "y": 0, "w": 1280, "h": 800},
            {"name": "Virtual-pxr-47989", "x": 949, "y": 0, "w": 1920, "h": 1080}]
    assert desktop.arrange(outs) == ["output.Virtual-pxr-48989.position.2869,0"]
    assert desktop.bounds(outs) == (0, 0, 2869, 1080)


def test_display_switch_popup_is_closed():
    calls, owned = [], [False, False, True]

    def run(args, timeout=10, text_in=None):
        calls.append(args)
        if "NameHasOwner" in args:
            return 0, "b true\n" if owned.pop(0) else "b false\n"
        return 0, ""

    real_run, real_which = desktop._run, desktop.shutil.which
    desktop._run, desktop.shutil.which = run, lambda name: "/usr/bin/" + name
    try:
        logged = []
        desktop.close_display_switch(logged.append, seconds=2, poll=0.01).join(3)
    finally:
        desktop._run, desktop.shutil.which = real_run, real_which
    assert [a[-1] for a in calls] == [desktop.OSD_SERVICE] * 3 + ["hideOsd"]
    assert calls[-1][3] == desktop.OSD_SERVICE and len(logged) == 1


# ---- sound ----

def test_sound_packets():
    sent = []
    key = secrets.token_bytes(16)
    p = audio.Packetizer(key, 0x7FFFFFFF, True, 5, sent.append)
    p.packet(b"opus!")
    pt, seq, ts = sent[0][1], struct.unpack_from(">H", sent[0], 2)[0], struct.unpack_from(">I", sent[0], 4)[0]
    assert sent[0][0] == 0x80 and pt == 97 and seq == 0 and ts == 0
    iv = struct.pack(">I", 0x7FFFFFFF) + bytes(12)
    assert ossl._run(False, ossl.crypto().EVP_aes_128_cbc(), key, iv, sent[0][12:], padding=True) == b"opus!"
    p.packet(b"opus2")
    assert struct.unpack_from(">HI", sent[1], 2) == (1, 5)
    assert audio.iv_for(0xFFFFFFFF, 1) == bytes(16)  # the id plus the sequence, in 32 bits


def test_parity_packet_header():
    pkt = audio.parity_packet(1, 9, 4, 20, b"xyz")
    assert pkt[1] == 127 and struct.unpack_from(">H", pkt, 2)[0] == 9
    assert pkt[12] == 1 and pkt[13] == 97 and struct.unpack_from(">HI", pkt, 14) == (4, 20) and pkt[24:] == b"xyz"


# ---- the helper ----

class HelperHost:
    name = "steamdeck"
    ports = [47989, 48989, 49989]

    def __init__(self, tmp):
        self.pins = server.PinBox(os.path.join(tmp, "pin.txt"))


def test_helper_answers():
    with tempfile.TemporaryDirectory() as tmp:
        h = helper.Helper(HelperHost(tmp), tokens=os.path.join(tmp, "pc-helper.json"), log=lambda t: None,
                          ask=lambda q: True)
        assert h.here() == "POCKETXR-HERE 1 47610 steamdeck 47989,48989,49989"
        assert h.answer({"op": "status", "token": "nope"}) == {"ok": False, "why": "not paired"}
        token = h.answer({"op": "pair", "phone": "Flip 7"})["token"]
        st = h.answer({"op": "status", "token": token})
        assert st["os"] == "linux-virtual" and st["virtual"] is True
        assert h.answer({"op": "screens-setup", "token": token}) == {"ok": True, "ports": [47989, 48989, 49989]}
        assert h.answer({"op": "screen-prepare", "token": token}) == {"ok": True}
        threading.Thread(target=lambda: h.host.pins.wait(10), daemon=True).start()
        time.sleep(0.2)
        assert h.answer({"op": "apollo-pin", "token": token, "pin": "2468", "name": "Flip 7"}) == {"ok": True}
        refused = helper.Helper(HelperHost(tmp), tokens=os.path.join(tmp, "other.json"), log=lambda t: None,
                                ask=lambda q: False)
        assert refused.answer({"op": "pair", "phone": "X"})["ok"] is False


if __name__ == "__main__":
    failed = 0
    names = [n for n in sorted(globals()) if n.startswith("test_")]
    for n in names:
        try:
            globals()[n]()
            print("ok   " + n)
        except Exception as e:  # report every test
            failed += 1
            print("FAIL %s: %r" % (n, e))
    print("%d of %d passed" % (len(names) - failed, len(names)))
    sys.exit(1 if failed else 0)
