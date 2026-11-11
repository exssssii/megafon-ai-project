"""Read the format and the size of a PNG, JPEG or WebP file from its header.

Only the structure is parsed: pixels are never decoded and nothing here opens the picture
for a model. A file whose bytes do not match one of the formats is `NotImage`.
"""
from dataclasses import dataclass
import struct
from typing import Literal

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# SOF0..SOF15 carry the frame size; C4 (tables), C8 (reserved) and CC (arithmetic tables) do not.
JPEG_FRAME_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})
END_WINDOW = 64  # the closing PNG chunk or JPEG marker must be this close to the end of the file
WEBP_ANIMATION_FLAG = 0x02


class NotImage(Exception):
    """The bytes are not a complete header of a supported image."""


class Animated(NotImage):
    """An animated picture: a screenshot is a still image, so the file is not accepted."""

    def __init__(self):
        super().__init__("анимация не поддерживается")


@dataclass(frozen=True)
class ImageInfo:
    format: Literal["png", "jpeg", "webp"]
    width: int
    height: int


def read_header(data: bytes) -> ImageInfo:
    if data.startswith(PNG_SIGNATURE):
        info = _png(data)
    elif data.startswith(b"\xff\xd8"):
        info = _jpeg(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        info = _webp(data)
    else:
        raise NotImage("unknown signature")
    if info.width < 1 or info.height < 1:
        raise NotImage("empty size")
    return info


def _png(data: bytes) -> ImageInfo:
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise NotImage("no IHDR")
    if b"IEND" not in data[-END_WINDOW:]:
        raise NotImage("PNG is cut: no IEND chunk at the end")
    width, height = struct.unpack(">II", data[16:24])
    _reject_animated_png(data)
    return ImageInfo("png", width, height)


def _reject_animated_png(data: bytes) -> None:
    """An animation control chunk (`acTL`) comes before the first image data chunk."""
    position = 8
    while position + 8 <= len(data):
        length = struct.unpack(">I", data[position:position + 4])[0]
        kind = data[position + 4:position + 8]
        if kind == b"acTL":
            raise Animated()
        if kind in (b"IDAT", b"IEND"):
            return
        position += 12 + length


def _jpeg(data: bytes) -> ImageInfo:
    if b"\xff\xd9" not in data[-END_WINDOW:]:
        raise NotImage("JPEG is cut: no end marker at the end")
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            raise NotImage("marker expected")
        marker = data[position + 1]
        if marker == 0xFF:  # fill byte before a marker
            position += 1
            continue
        if marker in JPEG_STANDALONE:
            position += 2
            continue
        length = struct.unpack(">H", data[position + 2:position + 4])[0]
        if marker in JPEG_FRAME_MARKERS:
            if length < 8 or position + 9 > len(data):
                raise NotImage("frame header cut")
            height, width = struct.unpack(">HH", data[position + 5:position + 9])
            return ImageInfo("jpeg", width, height)
        if marker == 0xDA:  # scan data starts without a frame header
            break
        position += 2 + length
    raise NotImage("no frame header")


def _webp(data: bytes) -> ImageInfo:
    kind = data[12:16]
    body = data[20:]
    if kind == b"VP8X":
        if len(body) < 10:
            raise NotImage("VP8X cut")
        if body[0] & WEBP_ANIMATION_FLAG:
            raise Animated()
        width = int.from_bytes(body[4:7], "little") + 1
        height = int.from_bytes(body[7:10], "little") + 1
    elif kind == b"VP8 ":
        if len(body) < 10 or body[3:6] != b"\x9d\x01\x2a":
            raise NotImage("VP8 frame header")
        width, height = (value & 0x3FFF for value in struct.unpack("<HH", body[6:10]))
    elif kind == b"VP8L":
        if len(body) < 5 or body[0] != 0x2F:
            raise NotImage("VP8L signature")
        bits = int.from_bytes(body[1:5], "little")
        width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    else:
        raise NotImage("unknown WebP chunk")
    return ImageInfo("webp", width, height)
