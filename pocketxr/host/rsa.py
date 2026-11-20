"""The little of X.509 and RSA that pairing needs, in plain Python: a certificate's signature bytes, RSA PKCS#1 v1.5
signatures with SHA-256 (made with the host's key, checked with a phone's certificate). Pairing does a few of these, so
speed does not matter.
"""
import base64
import hashlib
import re

_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _read(der, at):
    """(tag, start of contents, end of contents) of the DER element at [at]."""
    tag = der[at]
    n = der[at + 1]
    at += 2
    if n & 0x80:
        count = n & 0x7F
        n = int.from_bytes(der[at:at + count], "big")
        at += count
    return tag, at, at + n


def _children(der, start, end):
    out = []
    at = start
    while at < end:
        tag, s, e = _read(der, at)
        out.append((tag, s, e))
        at = e
    return out


def _int(der, s, e):
    return int.from_bytes(der[s:e], "big")


def pem_blocks(text):
    """The DER bytes of each PEM block in [text], with its label: [(label, der)]."""
    if isinstance(text, bytes):
        text = text.decode("ascii", "replace")
    return [(m.group(1), base64.b64decode("".join(m.group(2).split())))
            for m in re.finditer(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", text, re.S)]


def der_to_pem(der):
    b = base64.b64encode(der).decode()
    return "-----BEGIN CERTIFICATE-----\n" + "\n".join(b[i:i + 64] for i in range(0, len(b), 64)) + \
        "\n-----END CERTIFICATE-----\n"


def cert_signature(der):
    """A certificate's signature value (the bit string's bytes), as GameStream pairing hashes it."""
    _, s, e = _read(der, 0)
    parts = _children(der, s, e)
    tag, bs, be = parts[2]
    if tag != 0x03:
        raise ValueError("not a certificate")
    return bytes(der[bs + 1:be])  # past the bit string's unused-bits byte


def cert_public_key(der):
    """(modulus, exponent) of a certificate's RSA key."""
    _, s, e = _read(der, 0)
    tbs = _children(der, *_read(der, s)[1:])
    # tbsCertificate: [0] version (optional), serial, signature algorithm, issuer, validity, subject, key info, ...
    i = 1 if tbs[0][0] == 0xA0 else 0
    _, ks, ke = tbs[i + 5]
    alg, bits = _children(der, ks, ke)
    _, bs, be = bits
    key = der[bs + 1:be]
    _, rs, re_ = _read(key, 0)
    (_, ns, ne), (_, es, ee) = _children(key, rs, re_)[:2]
    return _int(key, ns, ne), _int(key, es, ee)


def private_key(pem):
    """(modulus, private exponent) from a PEM RSA key (PKCS#8 or PKCS#1)."""
    for label, der in pem_blocks(pem):
        if label == "PRIVATE KEY":
            _, s, e = _read(der, 0)
            parts = _children(der, s, e)
            _, os_, oe = parts[2]
            der = der[os_:oe]
        elif label != "RSA PRIVATE KEY":
            continue
        _, s, e = _read(der, 0)
        f = _children(der, s, e)
        return _int(der, *f[1][1:]), _int(der, *f[3][1:])
    raise ValueError("no RSA private key")


def _encoded(message, k):
    digest = _SHA256_PREFIX + hashlib.sha256(message).digest()
    return b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest


def sign(key, message):
    """RSA PKCS#1 v1.5 with SHA-256; key is (modulus, private exponent)."""
    n, d = key
    k = (n.bit_length() + 7) // 8
    return pow(int.from_bytes(_encoded(message, k), "big"), d, n).to_bytes(k, "big")


def verify(cert_der, message, signature):
    """Whether [signature] is the certificate's key's RSA PKCS#1 v1.5 SHA-256 signature of [message]."""
    try:
        n, e = cert_public_key(cert_der)
    except (ValueError, IndexError):
        return False
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    got = pow(int.from_bytes(signature, "big"), e, n).to_bytes(k, "big")
    return got == _encoded(message, k)
