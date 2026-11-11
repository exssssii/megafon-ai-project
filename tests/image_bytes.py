"""Images built in code for tests: no file of a case is ever used."""
import struct
import zlib


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def png_bytes(width: int, height: int) -> bytes:
    """A valid grayscale PNG of the given size."""
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    rows = (b"\x00" + b"\xff" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(rows))
            + _chunk(b"IEND", b""))


def jpeg_header(width: int, height: int) -> bytes:
    """SOI, an APP0 segment and a baseline frame header: enough for a header parser."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    frame = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + frame + b"\xff\xd9"


def png_animated(width: int, height: int) -> bytes:
    """A PNG with an animation control chunk before the image data."""
    plain = png_bytes(width, height)
    split = plain.index(b"IDAT") - 4
    return plain[:split] + _chunk(b"acTL", struct.pack(">II", 2, 0)) + plain[split:]


def webp_vp8x(width: int, height: int, flags: int = 0) -> bytes:
    body = bytes([flags]) + b"\x00\x00\x00" +(width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = b"VP8X" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def webp_lossy(width: int, height: int) -> bytes:
    body = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    chunk = b"VP8 " + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def webp_lossless(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    body = b"\x2f" + struct.pack("<I", bits)
    chunk = b"VP8L" + struct.pack("<I", len(body)) + body
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk
