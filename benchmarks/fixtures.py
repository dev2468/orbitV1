"""The grounding benchmark's fixture set: synthetic UI scenes with exact
ground truth.

WHY THESE ARE SYNTHETIC, AND WHAT THAT COSTS
--------------------------------------------
The original grounding spike (see the VISION TIER block in
`orbit/mcp_servers/perception_tools.py`) used 10 real screenshots of this
developer's actual desktop and 46 hand/UIA-labelled targets. That set was
deliberately never checked in — it captured a real desktop as it happened to
be — so it cannot be re-run, and its 76% is a historical note, not a
baseline anything here can be compared against.

These scenes are drawn by code instead. The trade, stated plainly:

  - GAINED: exact ground truth (the generator draws at the bounds it
    records, so there is no labelling error at all), byte-identical
    reproducibility on any machine, no desktop content to leak, no
    dependency on which apps happen to be installed, and a fixture set that
    can actually live in git — which is the whole point, since every phase
    after this one re-runs it.
  - LOST: real Windows chrome. A model that has seen a million real
    screenshots may ground better on a real Notepad than on a drawn
    approximation of one. So the ABSOLUTE hit-rate here is not a prediction
    of field accuracy.

That trade is acceptable because of what the benchmark is *for*: comparing
prompt shapes and models against each other on identical inputs. A relative
improvement measured here (freeform point vs. Set-of-Mark, model A vs. model
B) is a real signal even if the absolute number is not transferable. Do not
quote a number from this file as "Orbit's vision accuracy" — quote it as
"arm X beat arm Y by N points on the synthetic set".

The `custom_drawn` scene is the one exception where synthetic is arguably
*more* representative than a real screenshot: a <canvas> app or a game is
arbitrary drawn pixels with no UIA representation, which is exactly what
this scene is. That is also the column that justifies the vision tier
existing at all.

CATEGORIES mirror the original spike's split (native-like / dense /
custom-drawn / adversarial) so the shape of the report is comparable even
though the numbers are not.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from benchmarks.raster import Bounds, Canvas, center_of, text_width

# --- palette ---------------------------------------------------------------
WINDOW_BG = (246, 246, 246)
CHROME = (222, 226, 230)
BORDER = (154, 160, 166)
INK = (32, 33, 36)
MUTED = (110, 115, 120)
ACCENT = (26, 115, 232)
WHITE = (255, 255, 255)
PANEL = (238, 240, 242)

RED = (211, 47, 47)
GREEN = (56, 142, 60)
BLUE = (25, 118, 210)
ORANGE = (245, 124, 0)
SLATE = (96, 106, 116)


@dataclass(frozen=True)
class Element:
    """One thing on screen a candidate generator could plausibly propose.

    The full element list — not just the targets — is what the Set-of-Mark
    arm numbers and overlays. Handing the model only the targets would be an
    oracle candidate set and would flatter SoM enormously; the point is to
    make it choose among real distractors.
    """

    element_id: str
    bounds: Bounds
    label: str = ""


@dataclass(frozen=True)
class Target:
    """One question put to the model, plus the answer.

    `description` is the plain-language phrasing handed to the vision tier,
    matching how `perception_vision_locate` is actually called ("the red
    record button"), not a UIA locator.
    """

    target_id: str
    description: str
    element_id: str


@dataclass
class Scene:
    scene_id: str
    category: str
    canvas: Canvas
    elements: list[Element] = field(default_factory=list)
    targets: list[Target] = field(default_factory=list)

    def element(self, element_id: str) -> Element:
        for el in self.elements:
            if el.element_id == element_id:
                return el
        raise KeyError(f"scene {self.scene_id!r} has no element {element_id!r}")

    def bounds_for(self, target: Target) -> Bounds:
        return self.element(target.element_id).bounds


def _window_frame(c: Canvas, title: str) -> None:
    """Common chrome: title bar + 1px frame. Drawn by every scene so the
    model is always looking at something window-shaped, the way a real
    `perception_vision_locate` capture always is (it crops to one window)."""
    c.fill_rect((0, 0, c.width, c.height), WINDOW_BG)
    c.fill_rect((0, 0, c.width, 34), CHROME)
    c.text(12, 11, title, INK, scale=2)
    c.stroke_rect((0, 0, c.width, c.height), BORDER)
    # Window buttons, right-aligned — distractors near a common miss zone.
    for i, glyph in enumerate(("-", "#", "X")):
        bx = c.width - 34 * (3 - i)
        c.text(bx + 10, 11, glyph, MUTED, scale=2)


def scene_native_like() -> Scene:
    """A text-editor window: menu bar, toolbar, body, status bar.

    Stands in for the native apps windows-control already drives (the spike
    used Notepad, which `test_windows_control_live.py` opens for real).
    """
    c = Canvas(880, 560, WINDOW_BG)
    _window_frame(c, "NOTES - DOCUMENT1")
    els: list[Element] = []

    # Menu bar
    c.fill_rect((1, 34, c.width - 1, 68), PANEL)
    x = 14
    for name in ("FILE", "EDIT", "VIEW", "HELP"):
        w = text_width(name, 2) + 20
        b = (x, 38, x + w, 64)
        c.text(x + 10, 44, name, INK, scale=2)
        els.append(Element(f"menu_{name.lower()}", b, name))
        x += w + 8

    # Toolbar
    c.fill_rect((1, 68, c.width - 1, 118), WINDOW_BG)
    x = 16
    for name in ("NEW", "OPEN", "SAVE", "FIND"):
        b = (x, 76, x + 86, 110)
        c.fill_rect(b, WHITE)
        c.stroke_rect(b, BORDER)
        c.text(x + (86 - text_width(name, 2)) // 2, 86, name, INK, scale=2)
        els.append(Element(f"tool_{name.lower()}", b, name))
        x += 96

    # Body text
    c.fill_rect((16, 130, c.width - 16, c.height - 60), WHITE)
    c.stroke_rect((16, 130, c.width - 16, c.height - 60), BORDER)
    for i, line in enumerate(
        ("THE QUICK BROWN FOX", "JUMPS OVER THE LAZY DOG", "ORBIT BENCHMARK FIXTURE")
    ):
        c.text(30, 148 + i * 26, line, INK, scale=2)

    # Status bar
    sb = (1, c.height - 40, c.width - 1, c.height - 1)
    c.fill_rect(sb, CHROME)
    c.text(14, c.height - 31, "LN 1 COL 1", MUTED, scale=2)
    els.append(Element("status_bar", sb, "LN 1 COL 1"))

    return Scene(
        scene_id="native_like",
        category="native_like",
        canvas=c,
        elements=els,
        targets=[
            Target("nat_edit_menu", "the EDIT menu in the menu bar", "menu_edit"),
            Target("nat_save_button", "the SAVE button in the toolbar", "tool_save"),
            Target("nat_help_menu", "the HELP menu in the menu bar", "menu_help"),
        ],
    )


def scene_dense_grid() -> Scene:
    """A spreadsheet grid — the spike's weakest column (62%), where misses
    were near-misses onto small adjacent controls. Kept because a benchmark
    that only contains easy cases cannot show an improvement."""
    c = Canvas(880, 560, WINDOW_BG)
    _window_frame(c, "LEDGER - SHEET1")
    els: list[Element] = []

    cols = "ABCDEFGH"
    rows = 12
    ox, oy = 60, 78
    cw, ch = 96, 34

    # Column headers
    for i, name in enumerate(cols):
        b = (ox + i * cw, oy, ox + (i + 1) * cw, oy + ch)
        c.fill_rect(b, CHROME)
        c.stroke_rect(b, BORDER)
        c.text(b[0] + (cw - text_width(name, 2)) // 2, oy + 9, name, INK, scale=2)
        els.append(Element(f"colhdr_{name.lower()}", b, name))

    # Row headers
    for r in range(rows):
        b = (ox - 44, oy + (r + 1) * ch, ox, oy + (r + 2) * ch)
        c.fill_rect(b, CHROME)
        c.stroke_rect(b, BORDER)
        label = str(r + 1)
        c.text(b[0] + (44 - text_width(label, 2)) // 2, b[1] + 9, label, INK, scale=2)
        els.append(Element(f"rowhdr_{r + 1}", b, label))

    # Cells. Values are deterministic and one of them is unique (742) so a
    # "find the cell containing X" target has exactly one correct answer.
    unique_at = (2, 3)  # column C, row 4
    for r in range(rows):
        for i in range(len(cols)):
            b = (ox + i * cw, oy + (r + 1) * ch, ox + (i + 1) * cw, oy + (r + 2) * ch)
            c.fill_rect(b, WHITE)
            c.stroke_rect(b, (220, 223, 227))
            value = 742 if (i, r) == unique_at else 100 + (i * 13 + r * 7) % 90
            label = str(value)
            c.text(b[2] - text_width(label, 2) - 8, b[1] + 9, label, INK, scale=2)
            els.append(Element(f"cell_{cols[i].lower()}{r + 1}", b, label))

    return Scene(
        scene_id="dense_grid",
        category="dense",
        canvas=c,
        elements=els,
        targets=[
            Target("dense_col_d", "the column header labelled D", "colhdr_d"),
            Target("dense_cell_c4", "the cell at column C row 4", "cell_c4"),
            Target("dense_cell_742", "the cell containing the number 742", "cell_c4"),
        ],
    )


def scene_custom_drawn() -> Scene:
    """A mixer panel: knobs, sliders, a transport button — no text on the
    controls themselves.

    This is the column that justifies the vision tier existing. A real
    <canvas> mixer or a game exposes nothing through UI Automation, so the
    alternative to a vision answer is not a worse answer, it is no answer.
    Being synthetic costs the least here: such a UI is arbitrary drawn
    pixels either way.
    """
    c = Canvas(880, 560, (28, 30, 34))
    c.fill_rect((0, 0, c.width, 34), (44, 47, 52))
    c.text(12, 11, "MIXER", (235, 235, 235), scale=2)
    c.stroke_rect((0, 0, c.width, c.height), (70, 74, 80))
    els: list[Element] = []

    # Five vertical sliders with handles at distinct heights.
    handle_rows = (150, 220, 190, 300, 250)
    for i, hy in enumerate(handle_rows):
        cx = 90 + i * 90
        track = (cx - 6, 90, cx + 6, 400)
        c.fill_rect(track, (58, 62, 68))
        c.fill_rect((cx - 6, hy, cx + 6, 400), (70, 130, 180))
        handle = (cx - 26, hy - 12, cx + 26, hy + 12)
        c.fill_rect(handle, (222, 226, 230))
        c.stroke_rect(handle, (12, 12, 12))
        els.append(Element(f"slider_handle_{i + 1}", handle))
        els.append(Element(f"slider_track_{i + 1}", track))

    # Three knobs, distinguishable only by colour.
    for i, (col, name) in enumerate(((RED, "red"), (GREEN, "green"), (BLUE, "blue"))):
        cx, cy, r = 600 + i * 100, 170, 38
        c.fill_circle(cx, cy, r, col)
        c.fill_circle(cx, cy, r - 10, (24, 26, 30))
        c.fill_rect((cx - 3, cy - r + 4, cx + 3, cy - r + 20), (250, 250, 250))
        els.append(Element(f"knob_{name}", (cx - r, cy - r, cx + r, cy + r)))

    # Large transport button, bottom right. Deliberately AMBER, not red: it
    # was red until visual inspection caught that "the red circular knob"
    # then had two defensible answers on screen, which would have scored the
    # model wrong for being right.
    bx, by, br = 760, 440, 52
    c.fill_circle(bx, by, br, (230, 145, 30))
    c.fill_circle(bx, by, br - 14, (240, 240, 240))
    els.append(Element("transport_button", (bx - br, by - br, bx + br, by + br)))

    # A few small square pads as distractors.
    for i in range(4):
        b = (600 + i * 44, 300, 600 + i * 44 + 34, 334)
        c.fill_rect(b, (90, 96, 104))
        c.stroke_rect(b, (140, 146, 152))
        els.append(Element(f"pad_{i + 1}", b))

    return Scene(
        scene_id="custom_drawn",
        category="custom_drawn",
        canvas=c,
        elements=els,
        targets=[
            Target("custom_red_knob", "the red circular knob", "knob_red"),
            Target(
                "custom_left_slider",
                "the handle of the leftmost vertical slider",
                "slider_handle_1",
            ),
            Target(
                "custom_transport",
                "the large round button in the bottom right corner",
                "transport_button",
            ),
        ],
    )


def scene_adversarial() -> Scene:
    """Near-identical tiles, one distinguished only by a small badge, plus a
    dark overlay panel clipping the tiles beneath it.

    The adversarial pressure is sameness: eight of the nine tiles are
    pixel-identical apart from their CH label, so a model that pattern-matches
    "a tile" rather than reading the distinguishing mark lands on the wrong
    one. There is deliberately NO "the partly hidden tile" target — the
    overlay clips two tiles, not one, so that phrasing would have had two
    correct answers. Targets here are answerable from a single unambiguous
    visual feature: the badge, the label text, and the panel's own button.
    """
    c = Canvas(880, 560, WINDOW_BG)
    _window_frame(c, "CHANNELS")
    els: list[Element] = []

    ox, oy, tw, th, gap = 60, 70, 220, 140, 20
    tiles: dict[str, Bounds] = {}
    for r in range(3):
        for col in range(3):
            idx = r * 3 + col + 1
            b = (ox + col * (tw + gap), oy + r * (th + gap), ox + col * (tw + gap) + tw,
                 oy + r * (th + gap) + th)
            c.fill_rect(b, PANEL)
            c.stroke_rect(b, BORDER)
            c.fill_circle((b[0] + b[2]) // 2, (b[1] + b[3]) // 2 - 10, 22, SLATE)
            c.text(b[0] + 14, b[3] - 30, f"CH {idx}", MUTED, scale=2)
            tiles[f"tile_{idx}"] = b
            els.append(Element(f"tile_{idx}", b, f"CH {idx}"))

    # The badge — the only visual difference between tile 2 and its siblings.
    badge_host = tiles["tile_2"]
    c.fill_circle(badge_host[2] - 22, badge_host[1] + 22, 13, ORANGE)

    # Overlay panel: fully covers tile 9, partially covers tile 6 only.
    ov = (600, 330, 860, 520)
    c.fill_rect(ov, (52, 56, 62))
    c.stroke_rect(ov, (20, 20, 20), thickness=2)
    c.text(ov[0] + 16, ov[1] + 18, "NOW PLAYING", (240, 240, 240), scale=2)
    els.append(Element("overlay_panel", ov, "NOW PLAYING"))

    close_b = (ov[2] - 46, ov[1] + 12, ov[2] - 14, ov[1] + 42)
    c.fill_rect(close_b, (120, 40, 40))
    c.text(close_b[0] + 10, close_b[1] + 8, "X", (255, 255, 255), scale=2)
    els.append(Element("overlay_close", close_b, "X"))

    return Scene(
        scene_id="adversarial",
        category="adversarial",
        canvas=c,
        elements=els,
        targets=[
            Target(
                "adv_badged_tile",
                "the channel tile marked with an orange badge in its top right corner",
                "tile_2",
            ),
            Target("adv_close_button", "the X close button on the dark overlay panel",
                   "overlay_close"),
            Target(
                "adv_tile_5",
                "the channel tile labelled CH 5",
                "tile_5",
            ),
        ],
    )


_BUILDERS = (
    scene_native_like,
    scene_dense_grid,
    scene_custom_drawn,
    scene_adversarial,
)


def build_scenes(only: list[str] | None = None) -> list[Scene]:
    """Build every scene (or the named subset). Deterministic: the same code
    produces byte-identical images on every machine, which is what lets a
    later phase's numbers be compared against this one's."""
    scenes = [b() for b in _BUILDERS]
    if only:
        wanted = set(only)
        scenes = [s for s in scenes if s.scene_id in wanted]
        missing = wanted - {s.scene_id for s in scenes}
        if missing:
            raise KeyError(f"unknown scene id(s): {sorted(missing)}")
    return scenes


def all_targets(scenes: list[Scene]) -> list[tuple[Scene, Target]]:
    return [(s, t) for s in scenes for t in s.targets]


__all__ = [
    "Element",
    "Scene",
    "Target",
    "all_targets",
    "build_scenes",
    "center_of",
]
