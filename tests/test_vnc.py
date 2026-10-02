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
