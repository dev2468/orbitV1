"""Raw-RGB drawing primitives for the grounding benchmark.

Pure Python for the same reason `perception_tools._box_downscale` is pure
Python: Pillow is not a dependency of this project, and a benchmark is an
even worse reason to make it one than the vision tier was. Everything here
writes into a flat `bytearray` of RGB triples and is handed to
`mss.tools.to_png` — the same encoder the perception tier already uses, so a
fixture image and a real window capture are the same kind of object by the
time a model sees them.

The 5x7 font exists because the model has to *read* the labels: a benchmark
target like "the EDIT menu" is only a fair test if EDIT is legible. Glyphs
are drawn at an integer `scale` so edges stay crisp — an anti-aliased or
fractionally-scaled glyph would be a different (harder, and not
representative) reading task than real UI text.
"""

from __future__ import annotations

from typing import Iterable

import mss.tools

from orbit.mcp_servers.mark_overlay import FONT as _PRODUCTION_FONT
from orbit.mcp_servers.mark_overlay import GLYPH_GAP as _GLYPH_GAP
from orbit.mcp_servers.mark_overlay import GLYPH_H as _GLYPH_H
from orbit.mcp_servers.mark_overlay import GLYPH_W as _GLYPH_W

Color = tuple[int, int, int]
Bounds = tuple[int, int, int, int]  # (left, top, right, bottom)

# 5x7 bitmap font. The DIGITS and separators come from the production
# renderer (orbit/mcp_servers/mark_overlay.py) rather than being copied, so a
# fixture and a real marked capture are drawn by the same glyphs. Letters are
# added here because only the benchmark needs them: the overlay draws index
# numbers, fixtures draw menu labels. Uppercase only — every fixture label is
# uppercased before drawing, which matches the all-caps look of real
# menu/toolbar chrome closely enough for this purpose.
_FONT: dict[str, list[str]] = {
    **_PRODUCTION_FONT,
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    " ": ["00000"] * 7,
}


class Canvas:
    """A fixed-size RGB image being drawn into.

    Coordinates are pixels from the top-left, matching every other
    coordinate space in this codebase (window bounds, mss regions,
    ElementRef.bounds). All drawing clips silently at the edges rather than
    raising — a fixture that draws one pixel off the edge should still
    produce an image, and the ground truth is recorded from the requested
    geometry either way.
    """

    def __init__(self, width: int, height: int, background: Color = (255, 255, 255)) -> None:
        self.width = width
        self.height = height
        self.buf = bytearray(width * height * 3)
        self.fill_rect((0, 0, width, height), background)

    def set_px(self, x: int, y: int, color: Color) -> None:
        if 0 <= x < self.width and 0 <= y < self.height:
            i = (y * self.width + x) * 3
            self.buf[i] = color[0]
            self.buf[i + 1] = color[1]
            self.buf[i + 2] = color[2]

    def fill_rect(self, bounds: Bounds, color: Color) -> None:
        left, top, right, bottom = bounds
        left = max(0, int(left))
        top = max(0, int(top))
        right = min(self.width, int(right))
        bottom = min(self.height, int(bottom))
        r, g, b = color
        for y in range(top, bottom):
            base = (y * self.width) * 3
            for x in range(left, right):
                i = base + x * 3
                self.buf[i] = r
                self.buf[i + 1] = g
                self.buf[i + 2] = b

    def stroke_rect(self, bounds: Bounds, color: Color, thickness: int = 1) -> None:
        left, top, right, bottom = (int(v) for v in bounds)
        for t in range(thickness):
            self.fill_rect((left + t, top + t, right - t, top + t + 1), color)
            self.fill_rect((left + t, bottom - t - 1, right - t, bottom - t), color)
            self.fill_rect((left + t, top + t, left + t + 1, bottom - t), color)
            self.fill_rect((right - t - 1, top + t, right - t, bottom - t), color)

    def fill_circle(self, cx: int, cy: int, radius: int, color: Color) -> None:
        r2 = radius * radius
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                if dx * dx + dy * dy <= r2:
                    self.set_px(cx + dx, cy + dy, color)

    def text(self, x: int, y: int, s: str, color: Color, scale: int = 2) -> int:
        """Draw `s` with its top-left at (x, y). Returns the width drawn.

        Unknown characters are skipped rather than substituted, so a typo in
        a fixture label shows up as a visibly missing glyph instead of a
        silent box the model would have to guess at.
        """
        cursor = x
        for ch in s.upper():
            glyph = _FONT.get(ch)
            if glyph is None:
                cursor += (_GLYPH_W + _GLYPH_GAP) * scale
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
            cursor += (_GLYPH_W + _GLYPH_GAP) * scale
        return cursor - x

    def to_png(self) -> bytes:
        return mss.tools.to_png(bytes(self.buf), (self.width, self.height))

    def copy(self) -> "Canvas":
        clone = Canvas.__new__(Canvas)
        clone.width = self.width
        clone.height = self.height
        clone.buf = bytearray(self.buf)
        return clone


def text_width(s: str, scale: int = 2) -> int:
    return len(s) * (_GLYPH_W + _GLYPH_GAP) * scale


def text_height(scale: int = 2) -> int:
    return _GLYPH_H * scale


def center_of(bounds: Bounds) -> tuple[int, int]:
    left, top, right, bottom = bounds
    return (left + right) // 2, (top + bottom) // 2


def contains(bounds: Bounds, x: float, y: float) -> bool:
    """Is (x, y) inside `bounds`? Right/bottom exclusive, matching the way
    the fixtures' rectangles are filled — a point at exactly `right` was
    never painted, so counting it as a hit would score a pixel that isn't
    part of the element."""
    left, top, right, bottom = bounds
    return left <= x < right and top <= y < bottom


def iter_rows(buf: bytes, width: int, height: int) -> Iterable[tuple[int, int, Color]]:
    for y in range(height):
        for x in range(width):
            i = (y * width + x) * 3
            yield x, y, (buf[i], buf[i + 1], buf[i + 2])
