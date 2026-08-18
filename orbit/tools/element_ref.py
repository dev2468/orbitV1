"""Contract 3 — the `ElementRef` shape shared between screen-perception
(unbuilt) and windows-control. Defined here, in the shared foundation
layer, now — before screen-perception exists — so that server, whenever
it's built, returns exactly this shape from `perception_find_element`
rather than windows-control (built first) inventing its own and the two
drifting apart. The tool catalog names that drift risk explicitly as the
reason this needs to be a real shared contract, not two ad hoc shapes
that happen to look similar.

windows-control does NOT depend on screen-perception to produce one of
these. `_resolve_click_target` in `orbit/mcp_servers/windows_control_tools.py`
builds an ElementRef directly from a pywinauto UIA lookup scoped to a
known window — source="uia" — because there is no `perception_find_element`
to call yet. A future perception server's ElementRef is a drop-in
replacement: what matters is the shape, not which server produced it.
Confidence is scored with the exact same constants
(`orbit.tools.foundation.Confidence`) either way, so gating logic in
windows-control never needs to know which source produced the value it's
looking at.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

ElementSource = Literal["uia", "ocr", "vision"]


class ElementRef(BaseModel):
    # Opaque locator string a caller can hand back to re-resolve or log —
    # not guaranteed stable across process restarts (a raw HWND-based
    # locator dies with the window), so treat it as a trace id, not a key.
    element_id: str
    role: Optional[str] = None
    name: Optional[str] = None
    # (left, top, right, bottom) in screen coordinates.
    bounds: Optional[tuple[int, int, int, int]] = None
    state: Optional[dict] = None
    source: ElementSource
    confidence: float

    def center(self) -> tuple[int, int]:
        if self.bounds is None:
            raise ValueError(f"ElementRef {self.element_id!r} has no bounds to compute a center from")
        left, top, right, bottom = self.bounds
        return (left + right) // 2, (top + bottom) // 2
