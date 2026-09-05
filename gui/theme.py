"""Orbit GUI — Studio design tokens and master stylesheet.

Single source of truth for palette, spacing, radius, shadows, and the global
QSS applied at QApplication level. Every GUI module imports from here instead
of defining its own inline constants.

## Studio — Warm / Organic

The palette is warm-neutral (stone), not cool-neutral (slate/gray). Every
"white" is off-white and every gray carries a red/yellow bias: canvas #F8F5F0,
surface #FDFCFA, borders #E8E2D9. That single substitution is what stops the
UI reading as a stock enterprise dashboard — a cool #F9FAFB canvas with
#E5E7EB borders is the default of every admin template ever shipped.

**Shadows are warm too** (`rgba(100, 80, 40, …)`, not `rgba(0, 0, 0, …)`).
A neutral-black shadow over a cream surface turns the penumbra gray and
visibly fights the ground; a brown-biased one stays in the same family and
reads as depth instead of dirt. If you add an elevation, tint it.

Indigo (#6366F1) is the one cool color and it is the brand accent — it stays,
and its scarcity against all this warmth is what makes it register as
"active" wherever it appears.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Color tokens
# ---------------------------------------------------------------------------

# Canvas & surfaces — warm off-whites, never pure #FFF
BG_CANVAS   = "#F8F5F0"   # app ground (warm cream)
SURFACE     = "#FDFCFA"   # cards, panels, bars (warm white)

# Borders — warm stone, used as hairlines
BORDER         = "#E8E2D9"   # standard divider / card outline
BORDER_LIGHT   = "#F2EFE9"   # near-invisible internal divider
BORDER_INPUT   = "#D6CFC5"   # strongest separator, empty-state strokes

# Primary accent — indigo (the only cool hue in the system)
ACCENT         = "#6366F1"
ACCENT_HOVER   = "#5558E6"
ACCENT_PRESSED = "#4F46E5"
ACCENT_LIGHT   = "#EFEDFF"   # tinted backgrounds (selection, badges)
ACCENT_BORDER  = "#C7D2FE"   # subtle accent border
ACCENT_DEEP    = "#4338CA"   # orb gradient terminus
ACCENT_MID     = "#818CF8"   # orb gradient midtone
ACCENT_PALE    = "#A5B4FC"   # orb highlight, connector lines

# Text hierarchy — warm stone, not neutral gray
TEXT_PRIMARY    = "#1C1917"   # stone-900
TEXT_SECONDARY  = "#78716C"   # stone-500
TEXT_TERTIARY   = "#A8A29E"   # stone-400

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

# Semantic — warning (warm amber; the approval channel's color)
WARNING        = "#B45309"
WARNING_BG     = "#FFF7ED"
WARNING_BORDER = "#FED7AA"
WARNING_TEXT   = "#9A3412"

# Neutral tints — warm
INPUT_BG       = "#F2EFE9"   # inputs, inactive pills, toolbar buttons
ROW_HOVER      = "#F5F2EC"

# Scrim behind modal surfaces (voice overlay, approvals drawer)
SCRIM          = "rgba(28, 25, 23, 0.18)"

# ---------------------------------------------------------------------------
# Elevation — warm-tinted, see module docstring
# ---------------------------------------------------------------------------

# (blur_radius, alpha_0_255, x_offset, y_offset). The color is always the warm
# brown below; apply_drop_shadow() is the only place that should build it.
SHADOW_TINT = (100, 80, 40)

SHADOW_SM = (10, 16, 0, 1)    # resting cards
SHADOW_MD = (18, 22, 0, 2)    # raised cards, input card
SHADOW_LG = (48, 40, 0, 12)   # modal / drawer

# ---------------------------------------------------------------------------
# Radius — the design's scale
# ---------------------------------------------------------------------------

RADIUS_XS = 6    # micro badges
RADIUS_SM = 8    # small pills, toolbar buttons
RADIUS_MD = 12   # buttons, inputs, inner tiles
RADIUS_LG = 14   # cards, panels
RADIUS_XL = 18   # approval card
RADIUS_2XL = 24  # voice modal

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
# Layout constants shared across modules
# ---------------------------------------------------------------------------

NAV_HEIGHT      = 48
STATUS_HEIGHT   = 28
STEP_SIDEBAR_W  = 220   # right-hand step rail on the Workbench
DRAWER_W        = 400   # approvals drawer
VOICE_MODAL_W   = 480

# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------

# DM Sans is the design's face. It is a Google font and will not be installed
# on a stock Windows box, so the stack degrades to Segoe UI Variable — which
# shares DM Sans's low-contrast geometric-humanist character closely enough
# that the layout metrics hold. Do not put Inter first: its taller x-height
# reflows the tight 11–13px labels this design leans on.
FONT_FAMILY = '"DM Sans", "Segoe UI Variable", "Segoe UI", system-ui, sans-serif'
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
    font-size: 11px;
    padding: 4px 20px;
}}
QStatusBar::item {{ border: none; }}

/* ===== Progress Bar ===== */
#progressBar {{
    border: none;
    background: {BORDER};
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
    """Apply a warm-tinted ``QGraphicsDropShadowEffect`` to *widget*.

    *preset* is ``"sm"``, ``"md"`` or ``"lg"``. The color is always the warm
    brown in ``SHADOW_TINT`` — see the module docstring for why a neutral
    black shadow is wrong over these cream surfaces.

    Note that a QGraphicsEffect replaces any effect already on the widget,
    and that Qt does not composite an effect with the widget's own QSS
    ``box-shadow`` (which QSS does not support at all) — this function is the
    only elevation mechanism available.
    """
    from PySide6.QtWidgets import QGraphicsDropShadowEffect
    from PySide6.QtGui import QColor

    presets = {"sm": SHADOW_SM, "md": SHADOW_MD, "lg": SHADOW_LG}
    blur, alpha, dx, dy = presets.get(preset, SHADOW_SM)
    r, g, b = SHADOW_TINT
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(blur)
    eff.setColor(QColor(r, g, b, alpha))
    eff.setOffset(dx, dy)
    widget.setGraphicsEffect(eff)
    return eff
