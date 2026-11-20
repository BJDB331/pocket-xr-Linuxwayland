"""The Pocket XR PC helper, built into Pocket XR for Linux and the Steam Deck (it replaces pc-helper/unix and the PC
Screens app's helper): the phone finds the Deck and pairs with nothing to type,
as with the PC helper on Windows, macOS and Linux (pocket-xr-pcscreen/pc-helper). Same port and protocol:

  UDP 47610  "POCKETXR-FIND 1"  ->  "POCKETXR-HERE 1 47610 <name> <ports>" (the PC screens' ports, comma-separated:
             SteamOS refuses mDNS announcements, so the phone learns them here)
  TCP 47610  one JSON line in, one out:
    {"op":"pair","phone"}                    a Yes on the Deck (kdialog); {"ok":true,"token","pc"}
    {"op":"apollo-pin","token","pin","name"} the phone's pairing PIN, given straight to the host
    {"op":"screen-prepare"}                  {"ok":true}: each stream makes its own virtual output
    {"op":"screens-setup"}                   {"ok":true,"ports"}: the screens are always there
    {"op":"status"|"start"}                  {"ok":true,"os":"linux-virtual","virtual":true,...}: the phone sends
                                             positions and asks for its window's shape

Phones allowed once are kept in ~/.config/pocketxr/pc-helper.json, the file the earlier helper used, so a phone
allowed before stays allowed.
"""
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time

PORT = 47610
TOKENS = os.path.join(os.path.expanduser("~/.config/pocketxr"), "pc-helper.json")


class Helper:
    def __init__(self, host, tokens=TOKENS, log=print, ask=None):
        self.host = host
        self.tokens = tokens
        self.log = log
        self.ask = ask or self._ask
        self.lock = threading.Lock()
        self.udp = self.tcp = None

    # ---- phones allowed ----

    def _phones(self):
        try:
            with open(self.tokens, encoding="utf-8") as f:
                return json.load(f).get("phones", [])
        except (OSError, ValueError, AttributeError):
            return []

    def known(self, token):
        return bool(token) and any(p.get("token") == token for p in self._phones())

    def _add(self, phone):
        with self.lock:
            phones = self._phones()
            token = secrets.token_hex(16)
            phones.append({"name": phone, "token": token})
            os.makedirs(os.path.dirname(self.tokens), exist_ok=True)
            tmp = self.tokens + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"phones": phones}, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.tokens)
            return token

    def _ask(self, question):
        """Yes or no from the Deck's user (kdialog, else zenity); no dialog tool means no."""
        try:
            if shutil.which("kdialog"):
                return subprocess.run(["kdialog", "--title", "Pocket XR", "--yesno", question], timeout=70).returncode == 0
            if shutil.which("zenity"):
                return subprocess.run(["zenity", "--question", "--title=Pocket XR", "--text=" + question,
                                       "--timeout=60"], timeout=70).returncode == 0
        except (OSError, subprocess.SubprocessError) as e:
            self.log("helper: ask: %s" % e)
        self.log("helper: no dialog tool: refused")
        return False

    # ---- requests ----

    def answer(self, request):
        op = request.get("op")
        if op == "pair":
            phone = request.get("phone") or "A phone"
            if not self.ask("%s wants to use this Steam Deck with Pocket XR (PC screens).\n\nAllow it?" % phone):
                return {"ok": False, "why": "not allowed on the Deck"}
            self.log("helper: allowed %s" % phone)
            return {"ok": True, "token": self._add(phone), "pc": self.host.name}
        if not self.known(request.get("token")):
            return {"ok": False, "why": "not paired"}
        if op == "apollo-pin":
            # The phone starts pairing a few seconds after the helper is asked: up to 40 s for it to wait for a PIN.
            pin, name = str(request.get("pin") or ""), request.get("name") or "Pocket XR"
            for _ in range(160):
                if self.host.pins.submit(pin, name):
                    self.log("helper: PIN given for %s" % name)
                    return {"ok": True}
                time.sleep(0.25)
            return {"ok": False, "why": "no phone waited for a PIN"}
        if op == "screen-prepare":
            return {"ok": True}
        if op == "screens-setup":
            # The phone asks when it sees one screen only. Here all of them are always there (the UDP answer lists
            # them): nothing to set up, so yes at once.
            return {"ok": True, "ports": list(self.host.ports)}
        if op in ("status", "start"):
            return {"ok": True, "pc": self.host.name, "steam": False, "alvr": False, "steamvr": "off",
                    "os": "linux-virtual", "virtual": True}
        return {"ok": False, "why": "unknown request"}

    def here(self):
        return "POCKETXR-HERE 1 %d %s %s" % (PORT, self.host.name, ",".join(str(p) for p in self.host.ports))

    # ---- sockets ----

    def start(self):
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp.bind(("0.0.0.0", PORT))
        self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp.bind(("0.0.0.0", PORT))
        self.tcp.listen(8)
        threading.Thread(target=self._serve_udp, daemon=True, name="helper-udp").start()
        threading.Thread(target=self._serve_tcp, daemon=True, name="helper-tcp").start()
        self.log("helper: on %d" % PORT)

    def _serve_udp(self):
        while True:
            try:
                data, sender = self.udp.recvfrom(256)
            except OSError:
                return
            if data.startswith(b"POCKETXR-FIND"):
                try:
                    self.udp.sendto(self.here().encode(), sender)
                except OSError:
                    pass

    def _serve_tcp(self):
        while True:
            try:
                conn, _ = self.tcp.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        with conn:
            try:
                conn.settimeout(130)
                line = conn.makefile("r", encoding="utf-8").readline()
                reply = self.answer(json.loads(line or "{}"))
                conn.sendall((json.dumps(reply) + "\n").encode())
            except (OSError, ValueError) as e:
                self.log("helper: %s" % e)

    def stop(self):
        for s in (self.udp, self.tcp):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
