"""Tests for the stdlib EXIF reader, using a hand-built EXIF/TIFF block."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from opsec import exif

MAKE = 0x010F
MODEL = 0x0110
EXIF_IFD = 0x8769
GPS_IFD = 0x8825
DATETIME_ORIGINAL = 0x9003
OFFSET_TIME_ORIGINAL = 0x9011


def _encode(ftype: int, value) -> bytes:
    if ftype == 2:
        text = value.encode() if isinstance(value, str) else value
        return text if text.endswith(b"\x00") else text + b"\x00"
    if ftype == 3:
        values = value if isinstance(value, (list, tuple)) else [value]
        return b"".join(struct.pack("<H", item) for item in values)
    if ftype == 4:
        values = value if isinstance(value, (list, tuple)) else [value]
        return b"".join(struct.pack("<I", item) for item in values)
    if ftype == 5:
        return b"".join(struct.pack("<II", num, den) for num, den in value)
    raise AssertionError(f"unsupported type {ftype}")


def _count(ftype: int, value) -> int:
    if ftype == 2:
        return len(_encode(2, value))
    if ftype == 5:
        return len(value)
    return len(value) if isinstance(value, (list, tuple)) else 1


def _layout(entries) -> tuple[int, int]:
    data_size = 0
    for _, ftype, value in entries:
        payload = _encode(ftype, value)
        if len(payload) > 4:
            data_size += len(payload) + (len(payload) % 2)
    return 2 + 12 * len(entries) + 4, data_size


def _ifd_bytes(entries, ifd_offset: int, data_offset: int) -> tuple[bytes, bytes]:
    head = struct.pack("<H", len(entries))
    body = b""
    data = b""
    for tag, ftype, value in entries:
        payload = _encode(ftype, value)
        if len(payload) <= 4:
            field = payload.ljust(4, b"\x00")
        else:
            field = struct.pack("<I", data_offset + len(data))
            data += payload + (b"\x00" if len(payload) % 2 else b"")
        body += struct.pack("<HHI", tag, ftype, _count(ftype, value)) + field
    return head + body + struct.pack("<I", 0), data


def build_tiff(ifd0, sub_ifds, pointers: dict[int, int]) -> bytes:
    ifd0_size, ifd0_data = _layout(ifd0)
    ifd0_offset = 8
    ifd0_data_offset = ifd0_offset + ifd0_size
    cursor = ifd0_data_offset + ifd0_data
    sub_offsets = []
    for entries in sub_ifds:
        size, data_size = _layout(entries)
        sub_offsets.append(cursor)
        cursor += size + data_size

    resolved = [
        (tag, ftype, sub_offsets[pointers[tag]] if tag in pointers else value)
        for tag, ftype, value in ifd0
    ]
    blob = bytearray(b"II*\x00" + struct.pack("<I", ifd0_offset))
    ifd0_blob, ifd0_values = _ifd_bytes(resolved, ifd0_offset, ifd0_data_offset)
    blob += ifd0_blob + ifd0_values
    for entries, offset in zip(sub_ifds, sub_offsets):
        size, _ = _layout(entries)
        sub_blob, sub_values = _ifd_bytes(entries, offset, offset + size)
        blob += sub_blob + sub_values
    return bytes(blob)


def build_jpeg(lat=(48, 51, 30132), lon=(2, 17, 40132), lat_ref="N", lon_ref="E",
               offset="+02:00", make="Apple", model="iPhone 13"):
    def dms(values):
        return [(values[0], 1), (values[1], 1), (values[2], 1000)]

    tiff = build_tiff(
        [(MAKE, 2, make), (MODEL, 2, model), (EXIF_IFD, 4, 0), (GPS_IFD, 4, 0)],
        [
            [(DATETIME_ORIGINAL, 2, "2024:05:01 12:00:00"), (OFFSET_TIME_ORIGINAL, 2, offset)],
            [
                (1, 2, lat_ref),
                (2, 5, dms(lat)),
                (3, 2, lon_ref),
                (4, 5, dms(lon)),
            ],
        ],
        pointers={EXIF_IFD: 0, GPS_IFD: 1},
    )
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xd8\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + b"\xff\xd9"


class ExifTests(unittest.TestCase):
    def _read(self, blob: bytes, suffix: str = ".jpg") -> dict:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / f"sample{suffix}"
            path.write_bytes(blob)
            return exif.read_exif(str(path))

    def test_gps_and_camera_from_jpeg(self):
        info = self._read(build_jpeg())
        self.assertTrue(info["has_exif"], info["error"])
        self.assertEqual(info["make"], "Apple")
        self.assertEqual(info["model"], "iPhone 13")
        self.assertEqual(info["offset_time_original"], "+02:00")
        self.assertEqual(info["datetime_original"], "2024:05:01 12:00:00")
        self.assertIsNotNone(info["gps"])
        self.assertAlmostEqual(info["gps"]["lat"], 48.85837, places=4)
        self.assertAlmostEqual(info["gps"]["lon"], 2.294481, places=4)

    def test_southern_western_hemisphere_is_negative(self):
        info = self._read(build_jpeg(lat=(33, 51, 0), lon=(70, 40, 0), lat_ref="S", lon_ref="W"))
        self.assertLess(info["gps"]["lat"], 0)
        self.assertLess(info["gps"]["lon"], 0)
        self.assertAlmostEqual(info["gps"]["lat"], -33.85, places=3)
        self.assertAlmostEqual(info["gps"]["lon"], -70.6667, places=3)

    def test_jpeg_fill_bytes_before_app1_are_tolerated(self):
        blob = build_jpeg()
        padded = blob[:2] + b"\xff" + blob[2:]
        info = self._read(padded)
        self.assertTrue(info["has_exif"], info["error"])
        self.assertAlmostEqual(info["gps"]["lat"], 48.85837, places=4)

    def test_null_island_is_rejected(self):
        info = self._read(build_jpeg(lat=(0, 0, 0), lon=(0, 0, 0)))
        self.assertIsNone(info["gps"])

    def test_file_without_exif_reports_error_not_crash(self):
        info = self._read(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, suffix=".png")
        self.assertFalse(info["has_exif"])
        self.assertIsNone(info["gps"])
        self.assertIsNotNone(info["error"])

    def test_heic_style_exif_payload_is_found(self):
        tiff = build_tiff(
            [(MAKE, 2, "Canon"), (GPS_IFD, 4, 0)],
            [[(1, 2, "N"), (2, 5, [(51, 1), (30, 1), (0, 1)]), (3, 2, "W"), (4, 5, [(0, 1), (7, 1), (0, 1)])]],
            pointers={GPS_IFD: 0},
        )
        container = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40 + b"\x00\x00\x00\x06Exif\x00\x00" + tiff
        info = self._read(container, suffix=".heic")
        self.assertTrue(info["has_exif"], info["error"])
        self.assertAlmostEqual(info["gps"]["lat"], 51.5, places=4)
        self.assertAlmostEqual(info["gps"]["lon"], -0.116667, places=4)


if __name__ == "__main__":
    unittest.main()
