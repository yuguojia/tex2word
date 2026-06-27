"""Image probing and EMU sizing for embedded graphics.

Pure-Python (no Pillow): reads intrinsic pixel dimensions from PNG, JPEG, TIFF
and SVG headers so embedded images get a sensible on-page size. Formats Word can
embed directly are PNG/JPEG/EMF/TIFF; SVG embeds as a vector blip (see the docx
writer); vector PDF/EPS need an external converter (post-V1), so they fall back
to a placeholder + warning at the call site.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

EMU_PER_INCH = 914400
_PX_PER_INCH = 96
EMU_PER_PX = EMU_PER_INCH // _PX_PER_INCH  # 9525

#: max width on a US-Letter page with 1in margins.
_MAX_WIDTH_EMU = int(6.0 * EMU_PER_INCH)

_EMBEDDABLE = {"png", "jpg", "jpeg", "emf", "tif", "tiff"}


@dataclass
class ImageInfo:
    fmt: str
    width_px: int
    height_px: int

    @property
    def embeddable(self) -> bool:
        return self.fmt in _EMBEDDABLE


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + seg_len
    return None


def _tiff_size(data: bytes) -> tuple[int, int] | None:
    if data[:2] == b"II":
        bo = "<"
    elif data[:2] == b"MM":
        bo = ">"
    else:
        return None
    if struct.unpack(bo + "H", data[2:4])[0] != 42:
        return None
    ifd = struct.unpack(bo + "I", data[4:8])[0]
    if ifd + 2 > len(data):
        return None
    count = struct.unpack(bo + "H", data[ifd : ifd + 2])[0]
    width = height = None
    for i in range(count):
        entry = ifd + 2 + i * 12
        if entry + 12 > len(data):
            return None
        tag, typ = struct.unpack(bo + "HH", data[entry : entry + 4])
        # ImageWidth (256) / ImageLength (257); value is SHORT (3) or LONG (4).
        if typ == 4:
            value = struct.unpack(bo + "I", data[entry + 8 : entry + 12])[0]
        else:
            value = struct.unpack(bo + "H", data[entry + 8 : entry + 10])[0]
        if tag == 256:
            width = value
        elif tag == 257:
            height = value
    if width and height:
        return width, height
    return None


def _svg_size(data: bytes) -> tuple[int, int] | None:
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return None
    match = re.search(r"<svg\b[^>]*>", text, re.S)
    if match is None:
        return None
    tag = match.group(0)

    def _length(name: str) -> float | None:
        m = re.search(rf'\b{name}\s*=\s*["\']\s*([0-9.]+)\s*([a-z%]*)', tag, re.I)
        if m is None:
            return None
        try:
            num = float(m.group(1))
        except ValueError:
            return None
        unit = m.group(2).lower()
        # SVG user units default to px; convert physical units to px at 96 dpi.
        factor = {
            "": 1.0, "px": 1.0, "pt": 96 / 72, "pc": 16.0,
            "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4,
        }.get(unit)
        return num * factor if factor else None

    width = _length("width")
    height = _length("height")
    if not width or not height:
        # fall back to the viewBox aspect (w h are the 3rd/4th numbers).
        vb = re.search(r'viewBox\s*=\s*["\']\s*([-\d.eE\s]+)["\']', tag)
        if vb:
            nums = vb.group(1).split()
            if len(nums) == 4:
                try:
                    width = width or float(nums[2])
                    height = height or float(nums[3])
                except ValueError:
                    pass
    if width and height:
        return int(round(width)), int(round(height))
    return None


def probe_bytes(data: bytes, fmt: str) -> ImageInfo:
    """Return :class:`ImageInfo` for in-memory image bytes of a known format."""
    fmt = fmt.lower()
    size: tuple[int, int] | None = None
    if fmt == "png":
        size = _png_size(data)
    elif fmt in ("jpg", "jpeg"):
        size = _jpeg_size(data)
    elif fmt in ("tif", "tiff"):
        size = _tiff_size(data)
    elif fmt == "svg":
        size = _svg_size(data)
    if size is None:
        # unknown intrinsic size (e.g. emf) -> default 4 x 3 inches worth of px
        size = (4 * _PX_PER_INCH, 3 * _PX_PER_INCH)
    return ImageInfo(fmt=fmt, width_px=size[0], height_px=size[1])


def probe(path: str) -> ImageInfo | None:
    """Return :class:`ImageInfo` for an image file, or ``None`` if unreadable."""
    fmt = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536)
    except OSError:
        return None
    return probe_bytes(head, fmt)


def emu_size(info: ImageInfo, max_width_emu: int | None = None) -> tuple[int, int]:
    """Compute (cx, cy) in EMU, scaled down to fit ``max_width_emu`` (or text)."""
    limit = max_width_emu if max_width_emu is not None else _MAX_WIDTH_EMU
    cx = info.width_px * EMU_PER_PX
    cy = info.height_px * EMU_PER_PX
    if cx > limit:
        scale = limit / cx
        cx = limit
        cy = int(cy * scale)
    return cx, cy
