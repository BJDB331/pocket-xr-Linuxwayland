"""The Pocket XR PC helper on the PC, as PC VR and PC screens use it (the helper is in Pocket XR's Android repo,
pc-helper/PocketXrPcHelper.cs).

UDP 47610: "POCKETXR-FIND 1" broadcast, the helper answers "POCKETXR-HERE 1 <tcp port> <PC name>". TCP: one JSON line
in, one JSON line out. The Deck is allowed once on the PC (op "pair": a Yes on the PC's screen); its token is enough
after that, kept in ~/.config/pocket-xr-deck/pc-helper.json.
"""
import json
import os
import socket
import time

PORT = 47610
TOKEN_PATH = os.path.join(os.path.expanduser("~/.config/pocket-xr-deck"), "pc-helper.json")
NAME = "Steam Deck"


def parse_here(data):
    """(tcp port, PC name) from a helper's answer, or None."""
    parts = data.decode(errors="replace").split()
    if parts[:2] != ["POCKETXR-HERE", "1"] or len(parts) < 3:
        return None
    try:
        return int(parts[2]), " ".join(parts[3:])
    except ValueError:
        return None


def find(timeout=6.0, stop=lambda: False):
    """(address, tcp port, PC name) of the first helper that answers, or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.5)
        end = time.time() + timeout
        while time.time() < end and not stop():
            try:
                s.sendto(b"POCKETXR-FIND 1", ("255.255.255.255", PORT))
                data, addr = s.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                time.sleep(0.5)
                continue
            here = parse_here(data)
            if here:
                return addr[0], here[0], here[1]
        return None
    finally:
        s.close()


def ask(host, port, req, timeout=60.0):
    with socket.create_connection((host, port), timeout=timeout) as c:
        c.sendall((json.dumps(req) + "\n").encode())
        return json.loads(c.makefile().readline() or "{}")


def load_token(path=TOKEN_PATH):
    try:
        with open(path) as f:
            return json.load(f).get("token")
    except (OSError, ValueError, AttributeError):
        return None


def save_token(token, pc, path=TOKEN_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"token": token, "pc": pc}, f)


def forget_token(path=TOKEN_PATH):
    try:
        os.remove(path)
    except OSError:
        pass


def pair(host, port):
    """Asks the PC's user to allow this Deck (a Yes on the PC's screen, up to a minute). The token, or None."""
    r = ask(host, port, {"op": "pair", "phone": NAME}, timeout=120)
    if not r.get("ok") or not r.get("token"):
        return None
    save_token(r["token"], r.get("pc"))
    return r["token"]
