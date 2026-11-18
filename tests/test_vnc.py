"""Offline checks for the built-in VNC pieces."""
import struct
import zlib

from kasm_use import vnc


def test_des_known_answer():
    # Classic DES test vector (FIPS 46 worked example).
    key, block = bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF")
    assert vnc._des_encrypt(key, block).hex().upper() == "85E813540F0AB405"


def test_vnc_auth_response_is_two_blocks():
    assert len(vnc.vnc_auth_response(bytes(16))) == 16


def test_zrle_solid_and_raw_tiles():
    # 2x1 rect: one tile, raw subencoding, pixels B,G,R little-endian CPIXELs.
    data = bytes([0]) + bytes([3, 2, 1]) + bytes([6, 5, 4])
    assert bytes(vnc._zrle_decode(data, 2, 1)) == bytes([1, 2, 3, 4, 5, 6])
    solid = bytes([1, 30, 20, 10])
    assert bytes(vnc._zrle_decode(solid, 2, 2)) == bytes([10, 20, 30]) * 4


def test_zrle_palette_rle():
    # palette of 2 colours (sub 130); index 0 run of 3 (128|0, run byte 2), then index 1 single.
    data = bytes([130, 0, 0, 255, 255, 0, 0, 128, 2, 1])
    assert bytes(vnc._zrle_decode(data, 4, 1)) == bytes([255, 0, 0]) * 3 + bytes([0, 0, 255])


def test_keysyms():
    assert vnc.keysym("ctrl") == 0xFFE3 and vnc.keysym("Return") == 0xFF0D and vnc.keysym("a") == ord("a")
    assert vnc.char_keysym("é") == 0xE9 and vnc.char_keysym("€") == 0x010020AC


def test_pointer_message_is_kasmvnc_11_bytes():
    sent = []
    s = vnc.Session.__new__(vnc.Session)
    s.width, s.height = 100, 100
    s.ws = type("W", (), {"send": lambda self, b: sent.append(b)})()
    s.pointer(5, 6, 1)
    assert len(sent[0]) == 11 and struct.unpack(">BHHHhh", sent[0]) == (5, 1, 5, 6, 0, 0)


def test_drag_holds_button_while_moving_then_releases(monkeypatch):
    monkeypatch.setattr(vnc.time, "sleep", lambda s: None)
    sent = []
    s = vnc.Session.__new__(vnc.Session)
    s.width, s.height = 100, 100
    s.ws = type("W", (), {"send": lambda self, b: sent.append(struct.unpack(">BHHHhh", b))})()
    s.drag(10, 10, 50, 30, steps=4)
    assert sent[0][1:4] == (0, 10, 10)                       # move to start, no button
    assert sent[1][1:4] == (1, 10, 10)                       # press at start
    assert [m[1] for m in sent[2:-1]] == [1, 1, 1, 1]        # held through every step
    assert sent[-2][2:4] == (50, 30)                         # last step reaches the target
    assert sent[-1][1:4] == (0, 50, 30)                      # release at the target


def test_session_websocket_keeps_api_port(monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def fake_ws(url, headers, ctx):
        seen["url"], seen["origin"] = url, headers["Origin"]
        raise Stop

    monkeypatch.setattr(vnc, "_WebSocket", fake_ws)
    for api_url, host in [("https://kasm.lan:6333", "kasm.lan:6333"), ("https://kasm.lan", "kasm.lan")]:
        try:
            vnc.Session(api_url, "/desktop/abc/vnc", "u", "t", None)
        except Stop:
            pass
        assert seen["url"] == f"wss://{host}/desktop/abc/vnc/websockify"
        assert seen["origin"] == f"https://{host}"
