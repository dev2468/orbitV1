"""Orbit GUI — Shared design tokens and master stylesheet.

Single source of truth for palette, spacing, radius, shadows, and the
global QSS applied at QApplication level.  Every GUI module imports from
here instead of defining its own inline constants.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Color tokens
# ---------------------------------------------------------------------------

# Canvas & surfaces
BG_CANVAS   = "#FAFAFA"
SURFACE     = "#FFFFFF"

# Borders — used sparingly
BORDER         = "#F0F0F2"   # near-invisible hairline dividers
BORDER_INPUT   = "#E4E4E7"   # input fields, stronger separators

# Primary accent — indigo
ACCENT         = "#6366F1"
ACCENT_HOVER   = "#5558E6"
ACCENT_PRESSED = "#4F46E5"
ACCENT_LIGHT   = "#EEF2FF"   # tinted backgrounds (selection, badges)
ACCENT_BORDER  = "#C7D2FE"   # subtle accent border

# Text hierarchy
TEXT_PRIMARY    = "#18181B"
TEXT_SECONDARY  = "#71717A"
TEXT_TERTIARY   = "#A1A1AA"

# Semantic — success
SUCCESS        = "#16A34A"
SUCCESS_BG     = "#F0FDF4"
SUCCESS_BORDER = "#BBF7D0"
SUCCESS_TEXT   = "#15803D"

# Semantic — danger
DANGER         = "#DC2626"
DANGER_BG      = "#FEF2F2"
DANGER_BORDER  = "#FECACA"
DANGER_TEXT    = "#B91C1C"

# Semantic — warning
WARNING        = "#D97706"
WARNING_BG     = "#FFFBEB"
WARNING_BORDER = "#FDE68A"
WARNING_TEXT   = "#B45309"

# Neutral tints
INPUT_BG       = "#F4F4F5"
ROW_HOVER      = "#F9F9FB"

# ---------------------------------------------------------------------------
# Elevation (box-shadow tokens — used with QGraphicsDropShadowEffect)
# ---------------------------------------------------------------------------

# QGraphicsDropShadowEffect params: (blur_radius, color_rgba, x_offset, y_offset)
SHADOW_SM = (6, "rgba(0, 0, 0, 0.06)", 0, 1)
SHADOW_MD = (10, "rgba(0, 0, 0, 0.08)", 0, 2)

# ---------------------------------------------------------------------------
# Radius
# ---------------------------------------------------------------------------

RADIUS_SM = 8    # buttons, inputs, pills
RADIUS_MD = 12   # cards
RADIUS_LG = 16   # outer panels, major containers

# ---------------------------------------------------------------------------
# Spacing (4 px base grid)
# ---------------------------------------------------------------------------

SP_1  = 4
SP_2  = 8
SP_3  = 12
SP_4  = 16
SP_6  = 24
SP_8  = 32
SP_12 = 48

# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------

FONT_FAMILY = '"Inter", "Segoe UI Variable", "Segoe UI", -apple-system, sans-serif'
FONT_MONO   = '"Cascadia Code", "Consolas", "JetBrains Mono", monospace'

# ---------------------------------------------------------------------------
# Master QSS stylesheet
# ---------------------------------------------------------------------------

STYLESHEET = f"""
/* ===== Base ===== */
QMainWindow, QWidget {{
    background-color: {BG_CANVAS};
    color: {TEXT_PRIMARY};
    font-family: {FONT_FAMILY};
    font-size: 13px;
}}
QMainWindow {{
    background-color: {BG_CANVAS};
}}

/* ===== Scrollbars ===== */
QScrollBar:vertical {{
    background: transparent;
    width: 6px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_INPUT};
    border-radius: 3px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {TEXT_TERTIARY};
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 6px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_INPUT};
    border-radius: 3px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {TEXT_TERTIARY};
}}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {{
    width: 0;
}}
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: transparent;
}}

/* ===== Splitter ===== */
QSplitter::handle {{
    background: {BORDER};
    width: 1px;
}}

/* ===== Status Bar ===== */
QStatusBar {{
    background: {SURFACE};
    border-top: 1px solid {BORDER};
    color: {TEXT_SECONDARY};
    font-size: 12px;
    padding: 4px 12px;
}}

/* ===== Progress Bar ===== */
#progressBar {{
    border: none;
    background: {BORDER_INPUT};
    border-radius: 2px;
    max-height: 3px;
}}
#progressBar::chunk {{
    background: {ACCENT};
    border-radius: 2px;
}}

/* ===== Tooltips ===== */
QToolTip {{
    background: {TEXT_PRIMARY};
    color: {SURFACE};
    border: none;
    border-radius: {RADIUS_SM}px;
    padding: 6px 10px;
    font-size: 12px;
}}
"""


def apply_drop_shadow(widget, preset: str = "sm"):
    """Apply a ``QGraphicsDropShadowEffect`` to *widget*.

    *preset* is ``"sm"`` or ``"md"`` mapping to ``SHADOW_SM`` / ``SHADOW_MD``.
    """
    from PySide6.QtWidgets import QGraphicsDropShadowEffect
    from PySide6.QtGui import QColor

    blur, _rgba_unused, dx, dy = SHADOW_SM if preset == "sm" else SHADOW_MD
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setColor(QColor(0, 0, 0, 15 if preset == "sm" else 20))
    eff.setOffset(dx, dy)
    widget.setGraphicsEffect(eff)
