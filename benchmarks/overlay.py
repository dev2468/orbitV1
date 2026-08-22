"""Set-of-Mark overlay: draw numbered boxes on a scene so the model can
answer with an index instead of a coordinate.

The idea being tested is that picking "which of these 12 boxes" is an easier
task for a vision model than emitting a normalised coordinate pair, because
it replaces spatial regression with classification. Phase 0 measures whether
that is true here before any of it is wired into the real tool.

CANDIDATE SELECTION IS THE HONEST PART
--------------------------------------
`select_candidates` always includes the true target. That is not an accident
and it is not a fair-accuracy claim — it makes the SoM arm an UPPER BOUND:
"if candidate generation were perfect, how well would the model choose?" A
candidate set that omits the answer makes the question unanswerable, and the
resulting miss would measure candidate generation (Phase 1's job), not
grounding (Phase 2's job). Keep the two separable, and read the SoM number
as a ceiling.

Distractors are chosen deterministically and deliberately hard: half are the
target's nearest neighbours (the spike's dense-UI misses were near-misses
onto small adjacent controls, so those are the ones that matter), half are
strided across the rest of the scene so the overlay resembles what a real
generator would propose rather than one tight cluster.
"""

from __future__ import annotations

from dataclasses import dataclass

from benchmarks.fixtures import Element, Scene, Target
from benchmarks.raster import Bounds, Canvas, center_of
from orbit.mcp_servers.mark_overlay import draw_marks as _production_draw_marks
from orbit.mcp_servers.perception_tools import _VISION_SOM_PROMPT
from orbit.mcp_servers.perception_tools import _parse_index_reply as _production_parse_index


@dataclass(frozen=True)
class Candidate:
    index: int
    element_id: str
    bounds: Bounds


def select_candidates(scene: Scene, target: Target, limit: int) -> list[Candidate]:
    """Pick `limit` candidates for `target`, always including the answer.

    Deterministic by construction — no RNG — so the same fixture produces
    the same overlay on every run and across phases. Indices are assigned in
    reading order (top-to-bottom, then left-to-right) rather than by
    relevance, so the correct index is not correlated with its position in
    the list and a model that always answers "1" scores like chance.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")

    answer = scene.element(target.element_id)
    others = [e for e in scene.elements if e.element_id != answer.element_id]

    ax, ay = center_of(answer.bounds)

    def dist2(e: Element) -> int:
        ex, ey = center_of(e.bounds)
        return (ex - ax) ** 2 + (ey - ay) ** 2

    slots = max(0, limit - 1)
    by_near = sorted(others, key=lambda e: (dist2(e), e.element_id))
    near_take = min(slots // 2, len(by_near))
    chosen = by_near[:near_take]

    remaining = [e for e in by_near[near_take:]]
    spread_slots = slots - near_take
    if spread_slots > 0 and remaining:
        # Stride across the far half so the marks cover the window instead of
        # clustering on the answer.
        step = max(1, len(remaining) // spread_slots)
        for i in range(0, len(remaining), step):
            if len(chosen) - near_take >= spread_slots:
                break
            chosen.append(remaining[i])

    picked = chosen + [answer]
    picked.sort(key=lambda e: (e.bounds[1], e.bounds[0], e.element_id))
    return [Candidate(i + 1, e.element_id, e.bounds) for i, e in enumerate(picked)]


def draw_marks(scene: Scene, candidates: list[Candidate]) -> Canvas:
    """Return a COPY of the scene with numbered boxes drawn on it.

    Delegates to the PRODUCTION renderer (orbit/mcp_servers/mark_overlay.py).
    A benchmark that drew its marks with a different renderer than the tool
    uses would be measuring something the tool does not do — the numbers
    would be about this file's drawing code, not about set-of-mark.

    crop_origin is (0, 0) here because fixture candidates are already in
    image coordinates; grounding_bench applies its synthetic screen offset at
    scoring time, not at draw time.

    A copy, not the original: the freeform arm must see the unmarked image,
    and the two arms have to be looking at the same underlying scene for the
    comparison to mean anything.
    """
    c = scene.canvas.copy()
    marked = _production_draw_marks(
        bytes(c.buf),
        c.width,
        c.height,
        [{"index": cand.index, "bounds": cand.bounds} for cand in candidates],
        (0, 0),
    )
    c.buf = bytearray(marked)
    return c


# The prompt and the index parser are the PRODUCTION ones, imported rather
# than copied. If the tool's prompt changes, this benchmark must measure the
# changed prompt — a local copy would silently keep scoring the old one, and
# the whole point of the harness is to tell you whether a change to the tool
# helped.
SOM_PROMPT = _VISION_SOM_PROMPT


def parse_index_reply(text: str, valid: set[int]) -> int | None:
    """Delegates to the production parser so the benchmark cannot pass a reply
    the real tool would reject (or vice versa)."""
    return _production_parse_index(text, valid)
