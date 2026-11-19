"""PC screens, three presses end to end against stand-ins: a PC helper (TCP, on this machine), three Apollo instances
(their HTTP /serverinfo, on this machine) and a Moonlight (a script on PATH that pairs by writing its settings file
the way Moonlight does, and streams by waiting until it is closed). Linux only (process groups); no network, no PC.

What it shows: the first press pairs once (the helper is asked to type the PIN Moonlight waits with), and the next two
copy that pairing to screens 2 and 3 and stream each from its own port; a fourth press opens nothing."""
import http.server
import json
import os
import socketserver
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from pocketxr import pcscreens as P  # noqa: E402

pytestmark = pytest.mark.skipif(not hasattr(os, "killpg"), reason="needs Linux")

FAKE_MOONLIGHT = r'''#!/usr/bin/env python3
import os, sys, time, json
conf, typed, calls = os.environ["FAKE_CONF"], os.environ["FAKE_TYPED"], os.environ["FAKE_CALLS"]
with open(calls, "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
if sys.argv[1] == "pair":
    pin = sys.argv[sys.argv.index("--pin") + 1]
    for _ in range(200):  # Apollo takes the PIN once the helper types it
        if os.path.exists(typed) and open(typed).read() == pin:
            with open(conf, "w") as f:
                f.write("[General]\nuniqueid=DECK\n\n[hosts]\n1\\hostname=MIKE\n1\\localaddress=127.0.0.1\n"
                        "1\\localport=%s\n1\\srvcert=@ByteArray(-----BEGIN CERTIFICATE-----\\nX\\n)\n1\\uuid=UUID-1\n"
                        "size=1\n" % sys.argv[2].split(":")[1])
            break
        time.sleep(0.05)
    time.sleep(60)  # "Pairing completed": waits for an OK, as Moonlight does
elif sys.argv[1] == "list":
    print('Name, ID, HDR Support, App Collection Game, Hidden, Direct Launch, Boxart URL')
    print('"Desktop",1,false,false,false,false,""')
elif sys.argv[1] == "stream":
    time.sleep(60)
'''


def serve_apollo(n):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = (f"<root status_code=\"200\"><hostname>MIKE{'' if n == 1 else f' screen {n}'}</hostname>"
                    f"<uniqueid>UUID-{n}</uniqueid><mac>00:11:22:33:44:55</mac></root>").encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    s = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def serve_helper(typed, asked):
    class H(socketserver.StreamRequestHandler):
        def handle(self):
            req = json.loads(self.rfile.readline())
            asked.append(req)
            if req["op"] == "pair":
                ans = {"ok": True, "token": "T", "pc": "MIKE"}
            elif req.get("token") != "T":
                ans = {"ok": False, "why": "not paired"}
            elif req["op"] == "apollo-pin":
                with open(typed, "w") as f:
                    f.write(req["pin"])
                ans = {"ok": True}
            else:
                ans = {"ok": True}
            self.wfile.write((json.dumps(ans) + "\n").encode())

    s = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def test_three_presses(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ml = bin_dir / "moonlight"
    ml.write_text(FAKE_MOONLIGHT)
    ml.chmod(0o755)
    conf, typed, calls = tmp_path / "Moonlight.conf", tmp_path / "typed", tmp_path / "calls"
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + "/usr/bin:/bin")
    monkeypatch.setenv("FAKE_CONF", str(conf))
    monkeypatch.setenv("FAKE_TYPED", str(typed))
    monkeypatch.setenv("FAKE_CALLS", str(calls))
    apollos = [serve_apollo(n) for n in (1, 2, 3)]
    ports = tuple(s.server_address[1] for s in apollos)
    asked = []
    helper = serve_helper(str(typed), asked)
    monkeypatch.setattr(P, "PORTS", ports)
    monkeypatch.setattr(P, "DEFAULT_PORT", ports[0])
    monkeypatch.setattr(P, "NATIVE_CONF", str(conf))
    monkeypatch.setattr(P, "FLATPAK_CONF", str(tmp_path / "no-flatpak.conf"))
    monkeypatch.setattr(P, "STATE", str(tmp_path / "state"))
    monkeypatch.setattr(P, "CONFIG", str(tmp_path / "config"))
    monkeypatch.setattr(P, "SAVED", str(tmp_path / "config" / "pc-screens.json"))
    monkeypatch.setattr(P, "browse", lambda: [])
    monkeypatch.setattr(P.pchelper, "TOKEN_PATH", str(tmp_path / "config" / "pc-helper.json"))
    monkeypatch.setattr(P.pchelper.load_token, "__defaults__", (str(tmp_path / "config" / "pc-helper.json"),))
    monkeypatch.setattr(P.pchelper.save_token, "__defaults__", (str(tmp_path / "config" / "pc-helper.json"),))
    monkeypatch.setattr(P.pchelper, "find", lambda timeout=0, stop=None: ("127.0.0.1", helper.server_address[1], "MIKE"))

    said, opened = [], []
    pcs = P.PcScreens(lambda m: None, said.append, opened.append)

    def press():
        pcs.press()
        end = time.time() + 30
        while (pcs.busy or not said) and time.time() < end:
            time.sleep(0.05)
        assert not pcs.busy, said

    try:
        press()
        assert opened == [0], said
        c = P.read_conf(str(conf))
        assert c.paired("UUID-1")
        ops = [r["op"] for r in asked]
        assert ops == ["pair", "apollo-pin"], asked
        pin = next(r for r in asked if r["op"] == "apollo-pin")
        assert pin["port"] == ports[0] and pin["name"] == "Steam Deck" and pin["virtual"] is True

        press()
        press()
        assert opened == [0, 1, 2], said
        c = P.read_conf(str(conf))
        assert c.paired("UUID-2") and c.paired("UUID-3")
        assert c.find("UUID-3")[1]["manualport"] == str(ports[2])
        assert [r["op"] for r in asked] == ["pair", "apollo-pin"]  # one pairing for all three

        said.clear()
        press()
        assert said == ["All three PC screens are open."]
        assert pcs.open_count() == 3

        end = time.time() + 10  # each stand-in Moonlight writes its command line as it starts
        while time.time() < end:
            lines = [json.loads(x) for x in calls.read_text().splitlines()]
            streams = [x for x in lines if x[0] == "stream"]
            if len(streams) == 3:
                break
            time.sleep(0.1)
        assert [s[1] for s in streams] == [f"127.0.0.1:{p}" for p in ports]
        assert all(s[2] == "Desktop" and "--display-mode" in s for s in streams)
        assert sum(1 for x in lines if x[0] == "pair") == 1
    finally:
        pcs.close_all()
        for s in apollos + [helper]:
            s.shutdown()
