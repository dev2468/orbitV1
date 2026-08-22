"""Numbered-box overlay drawing for the set-of-mark vision prompt.

Draws directly into the raw RGB buffer `mss` already produced, so the marked
image goes through exactly the same encode/downscale path as an unmarked one
(`_fit_for_inline_upload`) and the coordinate maths downstream is unchanged.

Pure Python, no new dependency — same reasoning as
`perception_tools._box_downscale`: Pillow is not a dependency of this project
and drawing a dozen rectangles is not a good enough reason to make it one.

THE FONT LIVES HERE, NOT IN benchmarks/
---------------------------------------
`benchmarks/raster.py` imports it from this module rather than carrying its
own copy. The dependency runs benchmark -> production, never the reverse: a
benchmark that draws its marks with a *different* renderer than the tool uses
is measuring something the tool does not do. One renderer, one font, one set
of glyph metrics.
"""

from __future__ import annotations

# 5x7 bitmap font, digits only plus a couple of separators. The overlay only
# ever draws index numbers, so the alphabet is deliberately small; the
# benchmark's fixture generator imports the same table and adds nothing to it.
FONT: dict[str, list[str]] = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    " ": ["00000"] * 7,
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    "#": ["01010", "01010", "11111", "01010", "11111", "01010", "01010"],
}

GLYPH_W = 5
GLYPH_H = 7
GLYPH_GAP = 1

# Bright magenta-red: high contrast against both light window chrome and the
# dark backgrounds custom-drawn apps tend to use. A single colour for every
# mark on purpose — varying them would imply a meaning the indices do not have.
MARK_BOX = (255, 32, 96)
MARK_TAG_BG = (255, 32, 96)
MARK_TAG_FG = (255, 255, 255)

Bounds = tuple[int, int, int, int]


class RgbCanvas:
    """A mutable view over a raw RGB buffer, with the few primitives the
    overlay needs. Clips silently at the edges — a mark drawn partly
    off-image should still draw the part that fits."""

    __slots__ = ("buf", "width", "height")

    def __init__(self, buf: bytearray, width: int, height: int) -> None:
        self.buf = buf
        self.width = width
        self.height = height

    def fill_rect(self, bounds: Bounds, color: tuple[int, int, int]) -> None:
        left = max(0, int(bounds[0]))
        top = max(0, int(bounds[1]))
        right = min(self.width, int(bounds[2]))
        bottom = min(self.height, int(bounds[3]))
        r, g, b = color
        for y in range(top, bottom):
            base = y * self.width * 3
            for x in range(left, right):
                i = base + x * 3
                self.buf[i] = r
                self.buf[i + 1] = g
                self.buf[i + 2] = b

    def stroke_rect(self, bounds: Bounds, color: tuple[int, int, int], thickness: int = 2) -> None:
        left, top, right, bottom = (int(v) for v in bounds)
        for t in range(thickness):
            self.fill_rect((left + t, top + t, right - t, top + t + 1), color)
            self.fill_rect((left + t, bottom - t - 1, right - t, bottom - t), color)
            self.fill_rect((left + t, top + t, left + t + 1, bottom - t), color)
            self.fill_rect((right - t - 1, top + t, right - t, bottom - t), color)

    def text(self, x: int, y: int, s: str, color: tuple[int, int, int], scale: int = 2) -> None:
        cursor = x
        for ch in s:
            glyph = FONT.get(ch)
            if glyph is None:
                cursor += (GLYPH_W + GLYPH_GAP) * scale
                continue
            for row, bits in enumerate(glyph):
                for col, bit in enumerate(bits):
                    if bit == "1":
                        self.fill_rect(
                            (
                                cursor + col * scale,
                                y + row * scale,
                                cursor + (col + 1) * scale,
                                y + (row + 1) * scale,
                            ),
                            color,
                        )
            cursor += (GLYPH_W + GLYPH_GAP) * scale


def text_width(s: str, scale: int = 2) -> int:
    return len(s) * (GLYPH_W + GLYPH_GAP) * scale


def draw_marks(
    rgb: bytes,
    width: int,
    height: int,
    candidates: list[dict],
    crop_origin: tuple[int, int],
    scale: int = 2,
) -> bytes:
    """Return a COPY of `rgb` with each candidate's box outlined and numbered.

    `candidates` carry SCREEN coordinates (that is what an ElementRef uses
    everywhere else in this codebase); `crop_origin` converts them into image
    coordinates. Getting that subtraction wrong draws every mark at a constant
    offset from the control it is supposed to label — the same class of
    silent, plausible-looking error `_vision_point_to_screen` exists to
    prevent, which is why it is round-tripped in the tests.

    A copy rather than in-place: the unmarked buffer is still needed if the
    caller has to fall back to the freeform-point prompt.
    """
    canvas = RgbCanvas(bytearray(rgb), width, height)
    ox, oy = crop_origin
    for cand in candidates:
        left, top, right, bottom = (int(v) for v in cand["bounds"])
        box = (left - ox, top - oy, right - ox, bottom - oy)
        canvas.stroke_rect(box, MARK_BOX, thickness=2)
        _draw_tag(canvas, box, str(cand["index"]), scale)
    return bytes(canvas.buf)


def _draw_tag(canvas: RgbCanvas, box: Bounds, label: str, scale: int) -> None:
    """Numbered tag at the box's top-left, nudged back inside the image when
    the box sits against an edge. A tag drawn off-image is an unreadable
    mark, and an unreadable mark is a candidate the model cannot choose."""
    pad = 3
    tw = text_width(label, scale) + pad * 2
    th = GLYPH_H * scale + pad * 2

    left, top = box[0], box[1] - th
    if top < 0:
        top = max(0, box[1])
    if left + tw > canvas.width:
        left = canvas.width - tw
    left = max(0, left)

    canvas.fill_rect((left, top, left + tw, top + th), MARK_TAG_BG)
    canvas.text(left + pad, top + pad, label, MARK_TAG_FG, scale=scale)
