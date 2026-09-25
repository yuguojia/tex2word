"""Image probing, DPI handling, conversion, and EMU sizing.

Raster images use their embedded horizontal/vertical DPI metadata to determine
their physical size in Word. A missing or invalid DPI falls back to 96 dpi.
Supported raster formats, including WebP, are embedded as-is. SVG sizing is
read from its root element.
"""

from __future__ import annotations

import io
import re
import struct
from dataclasses import dataclass
from typing import Any

from PIL import Image, UnidentifiedImageError

EMU_PER_INCH = 914400
DEFAULT_DPI = 96.0
EMU_PER_PX = int(EMU_PER_INCH / DEFAULT_DPI)  # compatibility constant: 9525

#: Nominal text width used only to lay out side-by-side sub-figures. Ordinary
#: images are not clamped to this width.
DEFAULT_TEXT_WIDTH_EMU = int(6.0 * EMU_PER_INCH)

_EMBEDDABLE = {
    "png", "jpg", "jpeg", "bmp", "dib", "gif", "webp", "emf", "tif", "tiff",
}


@dataclass
class ImageInfo:
    fmt: str
    width_px: int
    height_px: int
    dpi_x: float | None = None
    dpi_y: float | None = None

    @property
    def embeddable(self) -> bool:
        return self.fmt in _EMBEDDABLE


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)  # Pillow rationals support float().
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if number > 0 else None


def _dpi_from_image(image: Image.Image) -> tuple[float | None, float | None]:
    """Return normalized DPI metadata from a Pillow image.

    Pillow exposes JFIF, PNG pHYs, BMP pixels-per-metre and TIFF resolution as
    ``info['dpi']``. WebP commonly carries the same values in EXIF, so inspect
    the standard X/YResolution and ResolutionUnit tags as a fallback.
    """
    dpi = image.info.get("dpi")
    if isinstance(dpi, (tuple, list)) and len(dpi) >= 2:
        x, y = _positive_float(dpi[0]), _positive_float(dpi[1])
        if x or y:
            return x or y, y or x
    elif (value := _positive_float(dpi)) is not None:
        return value, value

    try:
        exif = image.getexif()
    except Exception:
        return None, None
    x = _positive_float(exif.get(282))  # XResolution
    y = _positive_float(exif.get(283))  # YResolution
    if not x and not y:
        return None, None
    unit = exif.get(296, 2)  # ResolutionUnit: 2=inches, 3=centimetres
    if unit not in (2, 3):
        return None, None
    factor = 2.54 if unit == 3 else 1.0
    source_x, source_y = x or y, y or x
    return (
        source_x * factor if source_x else None,
        source_y * factor if source_y else None,
    )


def _pillow_info(image: Image.Image, fmt: str) -> ImageInfo:
    dpi_x, dpi_y = _dpi_from_image(image)
    return ImageInfo(fmt, image.width, image.height, dpi_x, dpi_y)


def _tiff_size(data: bytes) -> tuple[int, int] | None:
    """Read the first TIFF IFD as a fallback for sparse header-only files."""
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return None
    byte_order = "<" if data[:2] == b"II" else ">"
    if struct.unpack(byte_order + "H", data[2:4])[0] != 42:
        return None
    ifd = struct.unpack(byte_order + "I", data[4:8])[0]
    if ifd + 2 > len(data):
        return None
    count = struct.unpack(byte_order + "H", data[ifd : ifd + 2])[0]
    width = height = None
    for index in range(count):
        entry = ifd + 2 + index * 12
        if entry + 12 > len(data):
            break
        tag, kind = struct.unpack(byte_order + "HH", data[entry : entry + 4])
        if kind == 4:  # LONG
            value = struct.unpack(byte_order + "I", data[entry + 8 : entry + 12])[0]
        elif kind == 3:  # SHORT
            value = struct.unpack(byte_order + "H", data[entry + 8 : entry + 10])[0]
        else:
            continue
        if tag == 256:
            width = value
        elif tag == 257:
            height = value
    return (width, height) if width and height else None


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
    if fmt == "svg":
        size = _svg_size(data)
        if size is not None:
            return ImageInfo(fmt, size[0], size[1], DEFAULT_DPI, DEFAULT_DPI)
    else:
        try:
            with Image.open(io.BytesIO(data)) as image:
                return _pillow_info(image, fmt)
        except (OSError, UnidentifiedImageError):
            pass
    if fmt in ("tif", "tiff") and (size := _tiff_size(data)) is not None:
        return ImageInfo(fmt, size[0], size[1])
    # Unknown intrinsic size (e.g. EMF) -> a conservative 4 x 3 inch fallback.
    return ImageInfo(fmt, 4 * int(DEFAULT_DPI), 3 * int(DEFAULT_DPI))


def probe(path: str) -> ImageInfo | None:
    """Return :class:`ImageInfo` for an image file, or ``None`` if unreadable."""
    fmt = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    try:
        if fmt == "svg":
            with open(path, "rb") as fh:
                return probe_bytes(fh.read(65536), fmt)
        with Image.open(path) as image:
            return _pillow_info(image, fmt)
    except OSError:
        try:
            with open(path, "rb") as fh:
                return probe_bytes(fh.read(65536), fmt)
        except OSError:
            return None


def natural_emu_size(info: ImageInfo) -> tuple[float, float]:
    """Return physical width/height in EMU using embedded DPI metadata."""
    dpi_x = info.dpi_x if info.dpi_x and info.dpi_x > 0 else DEFAULT_DPI
    dpi_y = info.dpi_y if info.dpi_y and info.dpi_y > 0 else DEFAULT_DPI
    return (
        info.width_px * EMU_PER_INCH / dpi_x,
        info.height_px * EMU_PER_INCH / dpi_y,
    )


def emu_size(info: ImageInfo, max_width_emu: int | None = None) -> tuple[int, int]:
    """Compute (cx, cy) in EMU, optionally fitting an explicit container width."""
    cx, cy = natural_emu_size(info)
    if max_width_emu is not None and cx > max_width_emu:
        scale = max_width_emu / cx
        cx = max_width_emu
        cy *= scale
    return int(round(cx)), int(round(cy))
