"""Standard-library EXIF reader.

Location estimates mostly hinge on the GPS tags a camera writes into EXIF, so
this module reads EXIF without a third-party imaging library: it walks the
TIFF/IFD structure directly. JPEG (APP1), PNG (eXIf chunk), WebP (EXIF chunk)
and TIFF are parsed from their containers; HEIC/HEIF and other ISO-BMFF
containers are handled by locating the "Exif\\0\\0" + TIFF header in the file,
which is where those formats keep their Exif item payload.
"""

from __future__ import annotations

import struct

_HEAD = 512 * 1024
_CHUNK = 4 * 1024 * 1024
_MAX_TIFF = 8 * 1024 * 1024

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 13: 4}
_SCALAR_FMT = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f", 12: "d", 13: "I"}
_TIFF_MAGIC = (b"II*\x00", b"MM\x00*")
_EXIF_HDR = b"Exif\x00\x00"

# IFD0 tags
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_SOFTWARE = 0x0131
_TAG_DATETIME = 0x0132
_TAG_OFFSET_TIME = 0x9010
_TAG_EXIF_IFD = 0x8769
_TAG_GPS_IFD = 0x8825
# Exif sub-IFD tags
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_OFFSET_TIME_ORIGINAL = 0x9011
_TAG_LENS = 0xA434


def blank() -> dict:
    return {
        "has_exif": False,
        "gps": None,
        "make": None,
        "model": None,
        "software": None,
        "lens": None,
        "datetime_original": None,
        "offset_time_original": None,
        "error": None,
    }


def read_exif(path: str) -> dict:
    """Return an EXIF summary. Never raises for unreadable or missing EXIF."""
    info = blank()
    try:
        tiff = _find_tiff(path)
    except OSError as exc:
        info["error"] = str(exc)
        return info
    if tiff is None:
        info["error"] = "no EXIF block found"
        return info
    try:
        _parse(tiff, info)
    except (struct.error, ValueError, IndexError) as exc:
        info["error"] = f"unreadable EXIF: {exc}"
    return info


def _find_tiff(path: str):
    with open(path, "rb") as fh:
        head = fh.read(_HEAD)
    if not head:
        return None
    if head[:2] == b"\xff\xd8":
        return _from_jpeg(head)
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return _from_png(head)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _from_webp(head)
    if head[:4] in _TIFF_MAGIC:
        return head[:_MAX_TIFF]

    offset = _scan_offset(head, 0)
    if offset is None:
        # Containers such as HEIC store the Exif item somewhere after the header.
        with open(path, "rb") as fh:
            offset = _scan_stream(fh)
    if offset is None:
        return None
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(_MAX_TIFF)


def _scan_offset(buf: bytes, base: int):
    """Find an "Exif\\0\\0" marker followed by a TIFF header; return TIFF start."""
    pos = buf.find(_EXIF_HDR)
    while pos != -1:
        start = pos + 6
        if buf[start:start + 4] in _TIFF_MAGIC:
            return base + start
        pos = buf.find(_EXIF_HDR, pos + 1)
    return None


def _scan_stream(fh):
    fh.seek(0)
    tail = b""
    pos = 0
    while True:
        chunk = fh.read(_CHUNK)
        if not chunk:
            return None
        buf = tail + chunk
        found = _scan_offset(buf, pos - len(tail))
        if found is not None:
            return found
        tail = buf[-16:]
        pos += len(chunk)


def _from_jpeg(buf: bytes):
    i = 2
    n = len(buf)
    while i + 4 <= n:
        if buf[i] != 0xFF:
            return None
        while i + 1 < n and buf[i + 1] == 0xFF:
            # Fill bytes are legal padding between JPEG segments.
            i += 1
        marker = buf[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (0xDA, 0xD9):
            return None
        (seg_len,) = struct.unpack_from(">H", buf, i + 2)
        if seg_len < 2:
            return None
        if marker == 0xE1 and buf[i + 4:i + 10] == _EXIF_HDR:
            start = i + 10
            return buf[start:start + seg_len - 2]
        i += 2 + seg_len
    return None


def _from_png(buf: bytes):
    i = 8
    while i + 8 <= len(buf):
        (length,) = struct.unpack_from(">I", buf, i)
        ctype = buf[i + 4:i + 8]
        if ctype == b"eXIf":
            return buf[i + 8:i + 8 + length]
        if ctype in (b"IDAT", b"IEND"):
            return None
        i += 8 + length + 4
    return None


def _from_webp(buf: bytes):
    i = 12
    while i + 8 <= len(buf):
        ctype = buf[i:i + 4]
        (size,) = struct.unpack_from("<I", buf, i + 4)
        data = buf[i + 8:i + 8 + size]
        if ctype == b"EXIF":
            if data[:6] == _EXIF_HDR:
                return data[6:]
            if data[:4] in _TIFF_MAGIC:
                return data
            return None
        i += 8 + size + (size & 1)
    return None


def _parse(tiff: bytes, info: dict) -> None:
    if len(tiff) < 8:
        raise ValueError("truncated TIFF header")
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        raise ValueError("bad byte order")
    (magic,) = struct.unpack_from(endian + "H", tiff, 2)
    if magic != 42:
        raise ValueError("bad TIFF magic")
    (offset,) = struct.unpack_from(endian + "I", tiff, 4)

    ifd0 = _read_ifd(tiff, offset, endian)
    info["has_exif"] = True
    info["make"] = _as_text(ifd0.get(_TAG_MAKE))
    info["model"] = _as_text(ifd0.get(_TAG_MODEL))
    info["software"] = _as_text(ifd0.get(_TAG_SOFTWARE))
    info["datetime_original"] = _as_text(ifd0.get(_TAG_DATETIME))
    info["offset_time_original"] = _as_text(ifd0.get(_TAG_OFFSET_TIME))

    exif_ptr = ifd0.get(_TAG_EXIF_IFD)
    if isinstance(exif_ptr, int):
        sub = _read_ifd(tiff, exif_ptr, endian)
        info["datetime_original"] = _as_text(sub.get(_TAG_DATETIME_ORIGINAL)) or info["datetime_original"]
        info["offset_time_original"] = _as_text(sub.get(_TAG_OFFSET_TIME_ORIGINAL)) or info["offset_time_original"]
        info["lens"] = _as_text(sub.get(_TAG_LENS))

    gps_ptr = ifd0.get(_TAG_GPS_IFD)
    if isinstance(gps_ptr, int):
        info["gps"] = _read_gps(_read_ifd(tiff, gps_ptr, endian))


def _read_ifd(tiff: bytes, offset: int, endian: str) -> dict:
    if offset <= 0 or offset + 2 > len(tiff):
        raise ValueError(f"IFD offset {offset} outside EXIF block")
    (count,) = struct.unpack_from(endian + "H", tiff, offset)
    if count > 512:
        raise ValueError(f"implausible IFD entry count {count}")
    entries: dict = {}
    for index in range(count):
        pos = offset + 2 + index * 12
        if pos + 12 > len(tiff):
            break
        tag, ftype, nvals = struct.unpack_from(endian + "HHI", tiff, pos)
        size = _TYPE_SIZE.get(ftype, 0) * nvals
        if size == 0 or nvals > 4096:
            continue
        if size <= 4:
            raw = tiff[pos + 8:pos + 8 + size]
        else:
            (value_offset,) = struct.unpack_from(endian + "I", tiff, pos + 8)
            if value_offset + size > len(tiff):
                continue
            raw = tiff[value_offset:value_offset + size]
        entries[tag] = _decode(raw, ftype, nvals, endian)
    return entries


def _decode(raw: bytes, ftype: int, count: int, endian: str):
    if ftype == 2:
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    if ftype == 7:
        return int(raw[0]) if raw else 0
    if ftype in (5, 10):
        fmt = "Ii" if ftype == 5 else "ii"
        values = []
        for index in range(count):
            num, den = struct.unpack_from(endian + fmt, raw, index * 8)
            values.append(num / den if den else 0.0)
        return values[0] if count == 1 else values
    fmt = _SCALAR_FMT.get(ftype)
    if fmt is None:
        return raw
    values = list(struct.unpack_from(endian + str(count) + fmt, raw, 0))
    return values[0] if count == 1 else values


def _as_text(value) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _read_gps(ifd: dict):
    lat = _triple(ifd.get(2))
    lon = _triple(ifd.get(4))
    if lat is None or lon is None:
        return None
    lat_value = lat[0] + lat[1] / 60.0 + lat[2] / 3600.0
    lon_value = lon[0] + lon[1] / 60.0 + lon[2] / 3600.0
    lat_ref = (_as_text(ifd.get(1)) or "N").upper()
    lon_ref = (_as_text(ifd.get(3)) or "E").upper()
    if lat_ref.startswith("S"):
        lat_value = -lat_value
    if lon_ref.startswith("W"):
        lon_value = -lon_value
    if not (-90.0 <= lat_value <= 90.0 and -180.0 <= lon_value <= 180.0):
        return None
    if abs(lat_value) < 1e-6 and abs(lon_value) < 1e-6:
        # 0,0 is what unset GPS fields look like; it is not a real fix.
        return None
    gps = {"lat": round(lat_value, 6), "lon": round(lon_value, 6)}
    altitude = ifd.get(6)
    if isinstance(altitude, float):
        gps["alt_m"] = round(-altitude if ifd.get(5) == 1 else altitude, 1)
    precision = ifd.get(31)
    if isinstance(precision, float) and precision > 0:
        gps["precision_m"] = round(precision, 1)
    return gps


def _triple(value):
    if isinstance(value, list) and len(value) == 3 and all(isinstance(v, float) for v in value):
        return value[0], value[1], value[2]
    return None
