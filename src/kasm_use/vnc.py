"""Minimal KasmVNC input client — standard library only.

Kasm proxies each session's KasmVNC at  wss://<kasm>/desktop/<kasm_id>/vnc/websockify.
The proxy authenticates the user with the `username` + `session_token` cookies that
`request_kasm` returns, and requires an `Origin` header for the Kasm host. Behind the proxy,
KasmVNC offers classic VNC authentication and accepts an empty password.

Implemented: the RFB handshake, pointer and key events, and full-frame capture (ZRLE, which
decodes with zlib, falling back to Raw; written out as PNG). Kasm's own screenshot API only refreshes while a viewer is receiving
frames, so with nobody watching it returns a stale picture; frames read here are always live.
"""
from __future__ import annotations

import base64
import os
import zlib
import socket
import ssl
import struct
import time
import urllib.parse

# ----------------------------------------------------------------------------- websocket
class _WebSocket:
    """Just enough RFC 6455 for a binary client connection."""

    def __init__(self, url: str, headers: dict, ctx: ssl.SSLContext, timeout: float = 20):
        u = urllib.parse.urlsplit(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        self.sock = ctx.wrap_socket(raw, server_hostname=u.hostname) if u.scheme == "wss" else raw
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {u.path or '/'}{'?' + u.query if u.query else ''} HTTP/1.1",
                 f"Host: {u.hostname}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13",
                 "Sec-WebSocket-Protocol: binary"] + [f"{k}: {v}" for k, v in headers.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            resp += chunk
        head, rest = resp.split(b"\r\n\r\n", 1)
        self._pending = bytearray(rest)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            raise ConnectionError(f"websocket handshake failed: {status}")
        self._buf = bytearray()

    # Buffers are bytearrays consumed from the front with `del`, which is cheap; slicing bytes
    # objects instead made a multi-megabyte frame quadratic to receive.
    def _recv_exact(self, n: int) -> bytes:
        while len(self._pending) < n:
            chunk = self.sock.recv(max(65536, n - len(self._pending)))
            if not chunk:
                raise ConnectionError("websocket closed")
            self._pending += chunk
        out = bytes(self._pending[:n])
        del self._pending[:n]
        return out

    def _recv_frame(self) -> bytes:
        while True:
            b1, b2 = self._recv_exact(2)
            opcode, length = b1 & 0x0F, b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            data = self._recv_exact(length)
            if mask:  # servers don't mask, but handle it anyway
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError("websocket closed by server")
            if opcode == 0x9:  # ping
                self._send_frame(0xA, data)
                continue
            if opcode in (0x0, 0x1, 0x2):
                return data

    def _send_frame(self, opcode: int, data: bytes) -> None:
        mask = os.urandom(4)
        n = len(data)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._buf += self._recv_frame()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def send(self, data: bytes) -> None:
        self._send_frame(0x2, data)

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()


# ----------------------------------------------------------------------------------- DES
# VNC authentication encrypts the server's 16-byte challenge with DES (ECB), keyed by the
# password with each byte's bits reversed. Encrypt-only DES, straight from FIPS 46-3.
_IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6,
       64, 56, 48, 40, 32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
       61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7]
_FP = [40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30,
       37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
       34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25]
_E = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
      16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1]
_P = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
      2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25]
_PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3,
        60, 52, 44, 36, 63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37,
        29, 21, 13, 5, 28, 20, 12, 4]
_PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
        41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32]
_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
_S = [
    [14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13],
    [15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9],
    [10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12],
    [7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14],
    [2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3],
    [12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13],
    [4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12],
    [13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11],
]


def _permute(value: int, table: list[int], width: int) -> int:
    out = 0
    for pos in table:
        out = (out << 1) | ((value >> (width - pos)) & 1)
    return out


def _des_encrypt(key: bytes, block: bytes) -> bytes:
    k = _permute(int.from_bytes(key, "big"), _PC1, 64)
    c, d = k >> 28, k & 0x0FFFFFFF
    subkeys = []
    for s in _SHIFTS:
        c = ((c << s) | (c >> (28 - s))) & 0x0FFFFFFF
        d = ((d << s) | (d >> (28 - s))) & 0x0FFFFFFF
        subkeys.append(_permute((c << 28) | d, _PC2, 56))
    b = _permute(int.from_bytes(block, "big"), _IP, 64)
    left, right = b >> 32, b & 0xFFFFFFFF
    for sk in subkeys:
        x = _permute(right, _E, 32) ^ sk
        f = 0
        for i in range(8):
            six = (x >> (42 - 6 * i)) & 0x3F
            row, col = ((six & 0x20) >> 4) | (six & 1), (six >> 1) & 0xF
            f = (f << 4) | _S[i][row * 16 + col]
        left, right = right, left ^ _permute(f, _P, 32)
    return _permute((right << 32) | left, _FP, 64).to_bytes(8, "big")


def vnc_auth_response(challenge: bytes, password: str = "") -> bytes:
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in (password.encode() + b"\0" * 8)[:8])
    return _des_encrypt(key, challenge[:8]) + _des_encrypt(key, challenge[8:16])


# ------------------------------------------------------------------------------- keysyms
_NAMED = {
    "return": 0xFF0D, "enter": 0xFF0D, "tab": 0xFF09, "escape": 0xFF1B, "esc": 0xFF1B,
    "backspace": 0xFF08, "delete": 0xFFFF, "del": 0xFFFF, "insert": 0xFF63, "home": 0xFF50,
    "end": 0xFF57, "page_up": 0xFF55, "pageup": 0xFF55, "prior": 0xFF55, "page_down": 0xFF56,
    "pagedown": 0xFF56, "next": 0xFF56, "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "space": 0x20, "ctrl": 0xFFE3, "control": 0xFFE3, "control_l": 0xFFE3, "shift": 0xFFE1,
    "shift_l": 0xFFE1, "alt": 0xFFE9, "alt_l": 0xFFE9, "super": 0xFFEB, "super_l": 0xFFEB,
    "meta": 0xFFE7, "win": 0xFFEB, "menu": 0xFF67, "print": 0xFF61,
    **{f"f{i}": 0xFFBE + i - 1 for i in range(1, 13)},
}


def keysym(name: str) -> int:
    n = name.lower()
    if n in _NAMED:
        return _NAMED[n]
    if len(name) == 1:
        return char_keysym(name)
    raise ValueError(f"unknown key name {name!r}")


def char_keysym(ch: str) -> int:
    if ch == "\n":
        return 0xFF0D
    if ch == "\t":
        return 0xFF09
    cp = ord(ch)
    return cp if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF else 0x01000000 + cp


# ---------------------------------------------------------------------------------- ZRLE
def _zrle_decode(data: bytes, w: int, h: int) -> bytearray:
    """Decode one ZRLE rectangle (RFC 6143 §7.7.6) into packed RGB.

    With our pixel format, a CPIXEL is 3 bytes in little-endian order: B, G, R.
    """
    out = bytearray(w * h * 3)
    pos = 0

    def cpixels(n):
        nonlocal pos
        raw = data[pos:pos + 3 * n]
        pos += 3 * n
        rgb = bytearray(3 * n)
        rgb[0::3], rgb[1::3], rgb[2::3] = raw[2::3], raw[1::3], raw[0::3]
        return rgb

    def run_length():
        nonlocal pos
        n = 1
        while True:
            b = data[pos]
            pos += 1
            n += b
            if b != 255:
                return n

    for ty in range(0, h, 64):
        th = min(64, h - ty)
        for tx in range(0, w, 64):
            tw = min(64, w - tx)
            sub = data[pos]
            pos += 1
            if sub == 0:  # raw
                tile = cpixels(tw * th)
            elif sub == 1:  # solid
                tile = cpixels(1) * (tw * th)
            elif sub <= 16:  # packed palette
                pal = cpixels(sub)
                bits = 1 if sub == 2 else 2 if sub <= 4 else 4
                per_byte, mask = 8 // bits, (1 << bits) - 1
                row_bytes = (tw + per_byte - 1) // per_byte
                tile = bytearray()
                for _ in range(th):
                    row = data[pos:pos + row_bytes]
                    pos += row_bytes
                    for i in range(tw):
                        idx = (row[i // per_byte] >> (8 - bits * (i % per_byte + 1))) & mask
                        tile += pal[3 * idx:3 * idx + 3]
            elif sub == 128:  # plain RLE
                tile = bytearray()
                while len(tile) < tw * th * 3:
                    px = cpixels(1)
                    tile += px * run_length()
            elif sub >= 130:  # palette RLE
                pal = cpixels(sub - 128)
                tile = bytearray()
                while len(tile) < tw * th * 3:
                    idx = data[pos]
                    pos += 1
                    px = pal[3 * (idx & 127):3 * (idx & 127) + 3]
                    tile += px * (run_length() if idx & 128 else 1)
            else:
                raise ConnectionError(f"bad ZRLE subencoding {sub}")
            for r in range(th):
                start = ((ty + r) * w + tx) * 3
                out[start:start + tw * 3] = tile[r * tw * 3:(r + 1) * tw * 3]
    return out


# ----------------------------------------------------------------------------------- RFB
class Session:
    """One input connection to a Kasm session's desktop. Use as a context manager."""

    KEY_DELAY = 0.03  # KasmVNC drops keys sent back-to-back with no gap

    def __init__(self, api_url: str, vnc_path: str, username: str, session_token: str, ctx: ssl.SSLContext):
        host = urllib.parse.urlsplit(api_url).hostname
        self.ws = _WebSocket(f"wss://{host}/{vnc_path.strip('/')}/websockify",
                             {"Cookie": f"username={username}; session_token={session_token}",
                              "Origin": f"https://{host}"}, ctx)
        ws = self.ws
        if not ws.read(12).startswith(b"RFB "):
            raise ConnectionError("not a VNC server")
        ws.send(b"RFB 003.008\n")
        types = list(ws.read(ws.read(1)[0]))
        if 1 in types:
            ws.send(b"\x01")
        elif 2 in types:
            ws.send(b"\x02")
            ws.send(vnc_auth_response(ws.read(16)))
        else:
            raise ConnectionError(f"unsupported VNC security types {types}")
        if struct.unpack(">I", ws.read(4))[0] != 0:
            raise PermissionError("VNC authentication rejected")
        ws.send(b"\x01")  # shared: the owner can stay connected while we drive
        self.width, self.height = struct.unpack(">HH", ws.read(4))
        ws.read(16)  # server pixel format; we set our own below
        ws.read(struct.unpack(">I", ws.read(4))[0])  # desktop name
        # 32bpp little-endian true colour (bytes arrive as B, G, R, X). ZRLE preferred, Raw fallback.
        ws.send(struct.pack(">B3xBBBBHHHBBB3x", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        ws.send(struct.pack(">BxHii", 2, 2, 16, 0))
        self._bpp = 4
        self._zlib = zlib.decompressobj()  # ZRLE uses one zlib stream for the whole connection

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            if exc[0] is None:
                self.sync()
        finally:
            self.ws.close()

    def sync(self) -> None:
        """Round trip so the server has processed everything we sent before we disconnect.

        KasmVNC drops input that is still queued when the connection closes, so ask for a
        1x1 framebuffer update and wait for it — the server answers in order.
        """
        self._update(0, 0, 1, 1)

    def screenshot(self) -> bytes:
        """The whole desktop as a PNG, read live from the framebuffer."""
        frame = bytearray(self.width * self.height * 3)
        stride = self.width * 3
        for x, y, w, h, rgb in self._update(0, 0, self.width, self.height):
            for row in range(h):
                start = (y + row) * stride + x * 3
                frame[start:start + w * 3] = rgb[row * w * 3:(row + 1) * w * 3]
        raw = b"".join(b"\0" + bytes(frame[r * stride:(r + 1) * stride]) for r in range(self.height))

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))

    def _update(self, x: int, y: int, w: int, h: int) -> list:
        """Request a (non-incremental) framebuffer update; returns rectangles as packed RGB."""
        ws = self.ws
        ws.send(struct.pack(">BBHHHH", 3, 0, x, y, w, h))
        while True:
            msg = ws.read(1)[0]
            if msg == 0:  # FramebufferUpdate
                rects = []
                for _ in range(struct.unpack(">xH", ws.read(3))[0]):
                    rx, ry, rw, rh, enc = struct.unpack(">HHHHi", ws.read(12))
                    if enc == 0:
                        px = ws.read(rw * rh * self._bpp)
                        rgb = bytearray(rw * rh * 3)
                        rgb[0::3], rgb[1::3], rgb[2::3] = px[2::4], px[1::4], px[0::4]
                    elif enc == 16:
                        data = self._zlib.decompress(ws.read(struct.unpack(">I", ws.read(4))[0]))
                        rgb = _zrle_decode(data, rw, rh)
                    else:
                        raise ConnectionError(f"unexpected VNC encoding {enc}")
                    rects.append((rx, ry, rw, rh, rgb))
                return rects
            if msg == 1:  # SetColourMapEntries
                ws.read(6 * struct.unpack(">xHH", ws.read(5))[1])
            elif msg == 2:  # Bell
                continue
            elif msg == 3:  # ServerCutText
                ws.read(struct.unpack(">3xI", ws.read(7))[0])
            else:
                raise ConnectionError(f"unexpected VNC message {msg}")

    def pointer(self, x: int, y: int, buttons: int = 0, scroll_x: int = 0, scroll_y: int = 0) -> None:
        # KasmVNC's PointerEvent is 11 bytes, not RFB's 6: U16 button mask and two S16 scroll
        # deltas (see its web client's pointerEvent). Sending the standard 6-byte form
        # desynchronises the stream and the server drops the connection ("unknown message type").
        x = max(0, min(int(x), self.width - 1))
        y = max(0, min(int(y), self.height - 1))
        self.ws.send(struct.pack(">BHHHhh", 5, buttons, x, y, scroll_x, scroll_y))

    def click(self, x: int, y: int, button: int = 1, count: int = 1) -> None:
        mask = 1 << (button - 1)
        self.pointer(x, y)
        time.sleep(0.05)
        for _ in range(count):
            self.pointer(x, y, mask)
            time.sleep(0.05)
            self.pointer(x, y, 0)
            time.sleep(0.08)

    def scroll(self, x: int, y: int, direction: str, amount: int) -> None:
        button = {"up": 4, "down": 5, "left": 6, "right": 7}[direction]
        self.click(x, y, button, amount)

    def key(self, sym: int, down: bool) -> None:
        self.ws.send(struct.pack(">BBxxI", 4, 1 if down else 0, sym))
        time.sleep(self.KEY_DELAY)

    def combo(self, syms: list[int]) -> None:
        for s in syms:
            self.key(s, True)
        for s in reversed(syms):
            self.key(s, False)

    def type(self, text: str) -> None:
        for ch in text:
            self.combo([char_keysym(ch)])
