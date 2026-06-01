"""CRC algorithm tests, including round-trip against known EcoFlow values."""

from ecoflow_nut.protocol import crc8, crc16


def test_crc8_known_table_values():
    # First entries of the EcoFlow CRC-8 (poly 0x07) lookup table.
    assert crc8(b"\x00") == 0
    assert crc8(b"\x01") == 7
    assert crc8(b"\x02") == 14


def test_crc16_known_values():
    # CRC-16/ARC (poly 0xA001) reference values.
    assert crc16(b"\x00") == 0
    assert crc16(b"\x01") == 0xC0C1
    assert crc16(b"123456789") == 0xBB3D


def test_crc8_header_roundtrip():
    # A real DELTA-family header: aa 13 2c 01 -> crc8 0x29.
    header = bytes.fromhex("aa132c01")
    assert crc8(header) == 0x29


def test_crc_is_deterministic():
    payload = bytes(range(64))
    assert crc8(payload) == crc8(payload)
    assert crc16(payload) == crc16(payload)
