"""OpenSSL through ctypes, for the host: AES (ECB for pairing, CBC for sound, GCM for the control stream) and a TLS
server that asks every client for its certificate and accepts any, so the host itself decides (a paired phone, or a
401 answer as Sunshine gives). Python's own ssl module cannot accept an unknown client certificate.

SteamOS has OpenSSL 3 (libcrypto.so.3, libssl.so.3); Python on Windows ships the same as DLLs (tests only).
"""
import ctypes
import ctypes.util
import glob
import os
import sys

_crypto = None
_ssl = None


def _load(name):
    if sys.platform == "win32":
        found = glob.glob(os.path.join(sys.base_prefix, "DLLs", "lib%s-3*.dll" % name))
        if found:
            return ctypes.CDLL(found[0])
    for candidate in ("lib%s.so.3" % name, ctypes.util.find_library(name)):
        if candidate:
            try:
                return ctypes.CDLL(candidate)
            except OSError:
                pass
    raise OSError("OpenSSL's lib%s was not found" % name)


def crypto():
    global _crypto
    if _crypto is None:
        c = _load("crypto")
        vp = ctypes.c_void_p
        for fn in ("EVP_CIPHER_CTX_new", "EVP_aes_128_ecb", "EVP_aes_128_cbc", "EVP_aes_128_gcm"):
            getattr(c, fn).restype = vp
            getattr(c, fn).argtypes = []
        c.EVP_CIPHER_CTX_free.argtypes = [vp]
        c.EVP_CIPHER_CTX_set_padding.argtypes = [vp, ctypes.c_int]
        for fn in ("EVP_EncryptInit_ex", "EVP_DecryptInit_ex"):
            getattr(c, fn).argtypes = [vp, vp, vp, ctypes.c_char_p, ctypes.c_char_p]
        for fn in ("EVP_EncryptUpdate", "EVP_DecryptUpdate"):
            getattr(c, fn).argtypes = [vp, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.c_int]
        for fn in ("EVP_EncryptFinal_ex", "EVP_DecryptFinal_ex"):
            getattr(c, fn).argtypes = [vp, ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        c.EVP_CIPHER_CTX_ctrl.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        _crypto = c
    return _crypto


_GCM_SET_IVLEN, _GCM_GET_TAG, _GCM_SET_TAG = 0x9, 0x10, 0x11


def _run(encrypt, cipher, key, iv, data, padding=False, gcm_iv_len=None, tag=None):
    c = crypto()
    ctx = c.EVP_CIPHER_CTX_new()
    if not ctx:
        raise MemoryError()
    try:
        init = c.EVP_EncryptInit_ex if encrypt else c.EVP_DecryptInit_ex
        if gcm_iv_len is not None:
            init(ctx, cipher, None, None, None)
            c.EVP_CIPHER_CTX_ctrl(ctx, _GCM_SET_IVLEN, gcm_iv_len, None)
            if init(ctx, None, None, key, iv) != 1:
                raise ValueError("AES init failed")
        elif init(ctx, cipher, None, key, iv) != 1:
            raise ValueError("AES init failed")
        c.EVP_CIPHER_CTX_set_padding(ctx, 1 if padding else 0)
        out = ctypes.create_string_buffer(len(data) + 32)
        n = ctypes.c_int(0)
        update = c.EVP_EncryptUpdate if encrypt else c.EVP_DecryptUpdate
        if update(ctx, out, ctypes.byref(n), data, len(data)) != 1:
            raise ValueError("AES failed")
        total = n.value
        if tag is not None:
            buf = ctypes.create_string_buffer(tag, 16)
            c.EVP_CIPHER_CTX_ctrl(ctx, _GCM_SET_TAG, 16, buf)
        tail = ctypes.create_string_buffer(32)
        final = c.EVP_EncryptFinal_ex if encrypt else c.EVP_DecryptFinal_ex
        if final(ctx, tail, ctypes.byref(n)) != 1:
            return None  # a GCM tag that does not match, or bad padding
        result = out.raw[:total] + tail.raw[:n.value]
        if gcm_iv_len is not None and encrypt:
            t = ctypes.create_string_buffer(16)
            c.EVP_CIPHER_CTX_ctrl(ctx, _GCM_GET_TAG, 16, t)
            return result, t.raw
        return result
    finally:
        c.EVP_CIPHER_CTX_free(ctx)


def aes_ecb(key, data, encrypt):
    """AES-128-ECB with no padding (pairing); data is padded with zeros to whole blocks first."""
    data = bytes(data) + b"\0" * (-len(data) % 16)
    return _run(encrypt, crypto().EVP_aes_128_ecb(), key, None, data)


def aes_cbc_encrypt(key, iv, data):
    """AES-128-CBC with PKCS#7 padding (the sound packets)."""
    return _run(True, crypto().EVP_aes_128_cbc(), key, iv, data, padding=True)


def gcm_encrypt(key, nonce, plain):
    """(cipher text, 16-byte tag), AES-128-GCM."""
    return _run(True, crypto().EVP_aes_128_gcm(), key, nonce, plain, gcm_iv_len=len(nonce))


def gcm_decrypt(key, nonce, cipher_text, tag):
    """The plain text, or None when the tag does not match."""
    return _run(False, crypto().EVP_aes_128_gcm(), key, nonce, cipher_text, gcm_iv_len=len(nonce), tag=tag)


# ---- TLS server ----

_VERIFY_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_void_p)


@_VERIFY_CB
def _accept_any(ok, store):
    return 1  # every client certificate passes TLS; the host checks it against its paired phones


def libssl():
    global _ssl
    if _ssl is None:
        crypto()
        s = _load("ssl")
        vp = ctypes.c_void_p
        s.TLS_server_method.restype = vp
        s.SSL_CTX_new.restype = vp
        s.SSL_CTX_new.argtypes = [vp]
        s.SSL_CTX_free.argtypes = [vp]
        s.SSL_CTX_use_certificate_chain_file.argtypes = [vp, ctypes.c_char_p]
        s.SSL_CTX_use_PrivateKey_file.argtypes = [vp, ctypes.c_char_p, ctypes.c_int]
        s.SSL_CTX_set_verify.argtypes = [vp, ctypes.c_int, _VERIFY_CB]
        s.SSL_CTX_set_options.restype = ctypes.c_uint64
        s.SSL_CTX_set_options.argtypes = [vp, ctypes.c_uint64]
        s.SSL_CTX_ctrl.restype = ctypes.c_long
        s.SSL_CTX_ctrl.argtypes = [vp, ctypes.c_int, ctypes.c_long, vp]
        s.SSL_new.restype = vp
        s.SSL_new.argtypes = [vp]
        s.SSL_free.argtypes = [vp]
        s.SSL_set_fd.argtypes = [vp, ctypes.c_int]
        s.SSL_accept.argtypes = [vp]
        s.SSL_read.argtypes = [vp, ctypes.c_char_p, ctypes.c_int]
        s.SSL_write.argtypes = [vp, ctypes.c_char_p, ctypes.c_int]
        s.SSL_shutdown.argtypes = [vp]
        s.SSL_get_error.argtypes = [vp, ctypes.c_int]
        s.SSL_get1_peer_certificate.restype = vp
        s.SSL_get1_peer_certificate.argtypes = [vp]
        c = _crypto
        c.X509_free.argtypes = [vp]
        c.i2d_X509.argtypes = [vp, ctypes.POINTER(ctypes.c_void_p)]
        c.CRYPTO_free.argtypes = [vp, ctypes.c_char_p, ctypes.c_int]
        _ssl = s
    return _ssl


class TlsServerContext:
    """A TLS server context with this host's certificate and key (PEM files), asking clients for theirs."""

    def __init__(self, cert_path, key_path):
        s = libssl()
        self.ctx = s.SSL_CTX_new(s.TLS_server_method())
        if not self.ctx:
            raise OSError("SSL_CTX_new failed")
        if s.SSL_CTX_use_certificate_chain_file(self.ctx, cert_path.encode()) != 1 or \
                s.SSL_CTX_use_PrivateKey_file(self.ctx, key_path.encode(), 1) != 1:  # 1: PEM
            raise OSError("the host's certificate or key did not load")
        # SSL_VERIFY_PEER: ask for the client's certificate; the callback accepts it, whatever it is.
        s.SSL_CTX_set_verify(self.ctx, 0x01, _accept_any)
        # No session resumption: every connection shows its certificate (Apollo refused resumed ones, 20 Nov 2026).
        s.SSL_CTX_set_options(self.ctx, 1 << 14)  # SSL_OP_NO_TICKET
        s.SSL_CTX_ctrl(self.ctx, 44, 0, None)  # SSL_CTRL_SET_SESS_CACHE_MODE: off

    def wrap(self, sock):
        return TlsConnection(self, sock)


class TlsConnection:
    """One accepted TLS connection over a connected socket (blocking, with the socket's timeout)."""

    def __init__(self, context, sock, timeout=150):
        s = libssl()
        self.sock = sock
        # OpenSSL reads the descriptor itself, so it must block: a Python socket timeout makes it non-blocking
        # underneath (every read then failed with "want read"). The kernel's own timeouts stand in.
        sock.settimeout(None)
        if sys.platform != "win32":
            import socket
            import struct
            tv = struct.pack("ll", int(timeout), 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, tv)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, tv)
        self.ssl = s.SSL_new(context.ctx)
        s.SSL_set_fd(self.ssl, sock.fileno())
        r = s.SSL_accept(self.ssl)
        if r != 1:
            err = s.SSL_get_error(self.ssl, r)
            self.close()
            raise OSError("TLS handshake failed (%d)" % err)

    def peer_certificate(self):
        """The client's certificate as DER bytes, or None."""
        s = libssl()
        x = s.SSL_get1_peer_certificate(self.ssl)
        if not x:
            return None
        try:
            p = ctypes.c_void_p()
            n = _crypto.i2d_X509(x, ctypes.byref(p))
            if n <= 0:
                return None
            der = ctypes.string_at(p, n)
            _crypto.CRYPTO_free(p, b"", 0)
            return der
        finally:
            _crypto.X509_free(x)

    def recv(self, n):
        buf = ctypes.create_string_buffer(n)
        r = libssl().SSL_read(self.ssl, buf, n)
        return buf.raw[:r] if r > 0 else b""

    def sendall(self, data):
        view = memoryview(data)
        while len(view):
            chunk = bytes(view[:16384])
            r = libssl().SSL_write(self.ssl, chunk, len(chunk))
            if r <= 0:
                raise OSError("TLS write failed")
            view = view[r:]

    def close(self):
        if self.ssl:
            try:
                libssl().SSL_shutdown(self.ssl)
            except Exception:
                pass
            libssl().SSL_free(self.ssl)
            self.ssl = None
