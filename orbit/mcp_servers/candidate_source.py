"""Candidate-box generation for the vision tier.

Produces the list of `{index, bounds}` boxes that the set-of-mark prompt step
numbers and overlays. Two sources, tried in order:

  1. **UIA** — `uia_resolver.get_uia_tree`, filtered to boxes that could
     plausibly be a control. Free, local, millisecond-scale, and it reuses
     the resolver both perception and windows-control already share, so
     candidate boxes and actuation targets can never come from two different
     ideas of where a control is.

  2. **OmniParser** — only when the UIA tree is empty or uninformative, and
     only when someone has deployed OmniParser and pointed this config at it.
     Disabled by default. See THE OMNIPARSER DECISION below.

Why a generator at all: the vision model is much better at "which of these
numbered boxes" than at "emit a normalised coordinate", but only if the boxes
come from somewhere. Section 11's read-only framing is unchanged by this —
generating candidates is pure observation, and nothing here can move a mouse.

WHAT "UNHELPFUL TREE" MEANS, AND WHY IT IS NOT JUST "EMPTY"
-----------------------------------------------------------
The case this whole fallback exists for is Microsoft Solitaire, which
`VisionLocateTool`'s docstring already calls out: its entire UI surfaces
through UIA as a stack of nameless Panes. That tree is not empty. It is
structurally rich and semantically worthless — you can enumerate twenty
rectangles and know nothing about what any of them IS.

So `assess_uia_tree` tests two things, not one: are there enough usable boxes
(`min_useful_candidates`), and do enough of them carry a name
(`require_named_fraction`). A tree that passes the first and fails the second
is precisely the custom-drawn case, and is exactly when the fallback earns
its keep.

THE OMNIPARSER DECISION
-----------------------
OmniParser is not installed, and `mode: disabled` is the default. That is a
decision with reasons, in the same spirit as this project's OCR decision
(which declined Tesseract/PaddleOCR/EasyOCR and said so honestly rather than
pretending an OCR tier existed):

  1. **It smuggles in exactly what OCR was refused for.** OmniParser's
     published `requirements.txt` lists BOTH `easyocr` and `paddleocr` — the
     two stacks this codebase already rejected by name as "multi-hundred-MB
     ML stacks" — on top of `torch`, `torchvision`, `ultralytics` and
     `transformers`. Installing it as published would quietly land the
     refused dependency through a side door.
  2. **Size.** The bar this project set for an acceptable new dependency was
     `mss`: pure Python, no system binary. A full PyTorch stack plus YOLO and
     Florence-2 weights is multiple gigabytes and wants CUDA.
  3. **Licensing is version-dependent and easy to get wrong.** OmniParser
     v2's icon detector is an Ultralytics YOLOv8 derivative under
     **AGPL-3.0** — viral copyleft. The v3 detector is YOLOv9-based under
     MIT, and the caption weights are MIT. Vendoring "OmniParser" without
     pinning *which* weights is a real licensing hazard, not a footnote.

Hence `mode: http` as the only enabled path: point it at an OmniParser someone
else is hosting (a container, a workstation on the LAN, a managed endpoint)
and the multi-GB stack, the GPU and the AGPL question all stay outside this
venv. There is deliberately no `local` mode — adding one would mean vendoring
the dependency this design exists to avoid.

When disabled, this module reports the tier as unavailable in the same voice
`perception_find_element` already uses for OCR: named, explained, and not
pretended away.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Optional

from orbit.mcp_servers.uia_resolver import get_uia_tree, window_snapshot
from orbit.policy import load_perception_policy
from orbit.tools.foundation import ClassifiedToolError

Bounds = tuple[int, int, int, int]

# Roles that are containers by nature. They are kept out of the candidate set
# even when they pass the area filter, because a candidate has to be a thing
# a user could click, and "the pane containing the buttons" is not one.
_CONTAINER_ROLES = {
    "Pane",
    "Group",
    "Window",
    "Dialog",
    "TitleBar",
    "MenuBar",
    "ToolBar",
    "StatusBar",
    "Tab",
    "Document",
    "ScrollBar",
}


def _cfg(policy: Optional[dict] = None) -> dict:
    return policy if policy is not None else load_perception_policy()


def _area(b: Bounds) -> int:
    return max(0, b[2] - b[0]) * max(0, b[3] - b[1])


def _clip(b: Bounds, frame: Bounds) -> Bounds:
    return (
        max(b[0], frame[0]),
        max(b[1], frame[1]),
        min(b[2], frame[2]),
        min(b[3], frame[3]),
    )


def _usable_nodes(nodes: list[dict], frame: Bounds, cand_cfg: dict) -> list[dict]:
    """Geometry filter. Everything dropped here is dropped for a reason that
    would make it a bad thing to number, not merely an uninteresting one."""
    min_px = int(cand_cfg.get("min_box_px", 8))
    max_ratio = float(cand_cfg.get("max_area_ratio", 0.9))
    frame_area = _area(frame) or 1

    out: list[dict] = []
    for node in nodes:
        bounds = node.get("bounds")
        if not bounds or len(bounds) != 4:
            continue
        if node.get("visible") is False:
            continue
        clipped = _clip(tuple(int(v) for v in bounds), frame)  # type: ignore[arg-type]
        w, h = clipped[2] - clipped[0], clipped[3] - clipped[1]
        if w < min_px or h < min_px:
            continue
        if _area(clipped) / frame_area > max_ratio:
            continue
        out.append({**node, "bounds": clipped})
    return out


def _dedupe(nodes: list[dict]) -> list[dict]:
    """Collapse boxes with identical bounds, keeping the most informative.

    UIA routinely reports a control and one or more wrappers at exactly the
    same rectangle. Numbering all of them burns indices on what is, to the
    model looking at the picture, a single box — and worse, makes several
    indices correct for the same question, which is unscoreable. Preference
    order: has a name, then has an automation_id, then deeper in the tree
    (the leaf is the real control, the ancestors are packaging).
    """
    best: dict[Bounds, dict] = {}
    for node in nodes:
        key = tuple(node["bounds"])  # type: ignore[assignment]
        rank = (
            1 if (node.get("name") or "").strip() else 0,
            1 if node.get("automation_id") else 0,
            int(node.get("depth") or 0),
        )
        current = best.get(key)  # type: ignore[arg-type]
        if current is None or rank > current["_rank"]:
            best[key] = {**node, "_rank": rank}  # type: ignore[index]
    return [{k: v for k, v in n.items() if k != "_rank"} for n in best.values()]


def _interactive_first(nodes: list[dict]) -> list[dict]:
    """Prefer non-container roles, then named elements, then smaller boxes.

    Ordering matters because `max_candidates` truncates: when a window has
    more clickable things than there are marks, the ones that survive should
    be the ones a user would plausibly mean. Smaller-before-larger as the
    final key because in a nested UI the tighter box is the more specific
    control.
    """

    def key(node: dict) -> tuple:
        role = (node.get("role") or "").strip()
        return (
            1 if role in _CONTAINER_ROLES else 0,
            0 if (node.get("name") or "").strip() else 1,
            _area(tuple(node["bounds"])),  # type: ignore[arg-type]
        )

    return sorted(nodes, key=key)


def _number(nodes: list[dict], source: str) -> list[dict]:
    """Assign indices in reading order.

    Reading order rather than the relevance order used for truncation: the
    number a box carries should track where it is on screen, so a human
    reading the overlay (and the benchmark's report) can find box 7 without
    a lookup table. Relevance decided WHICH boxes survive; position decides
    what they are called.
    """
    ordered = sorted(nodes, key=lambda n: (n["bounds"][1], n["bounds"][0]))
    return [
        {
            "index": i + 1,
            "bounds": tuple(int(v) for v in n["bounds"]),
            "role": n.get("role"),
            "name": (n.get("name") or "").strip() or None,
            "source": source,
        }
        for i, n in enumerate(ordered)
    ]


def assess_uia_tree(nodes: list[dict], frame: Bounds, policy: Optional[dict] = None) -> dict:
    """Is this tree worth using as a candidate source?

    Returns {usable, named_fraction, count, reason}. `usable` False means the
    caller should try the fallback — see the module docstring for why
    "unhelpful" is a stronger test than "empty".
    """
    cand_cfg = _cfg(policy).get("candidates", {})
    usable_nodes = _dedupe(_usable_nodes(nodes, frame, cand_cfg))
    count = len(usable_nodes)
    named = sum(1 for n in usable_nodes if (n.get("name") or "").strip())
    fraction = (named / count) if count else 0.0

    min_count = int(cand_cfg.get("min_useful_candidates", 3))
    min_fraction = float(cand_cfg.get("require_named_fraction", 0.2))

    if count < min_count:
        reason = f"only {count} usable box(es) after filtering (need {min_count})"
        return {"usable": False, "count": count, "named_fraction": fraction, "reason": reason}
    if fraction < min_fraction:
        reason = (
            f"{named}/{count} boxes carry a UIA name ({fraction:.0%}, need {min_fraction:.0%}) — "
            "structurally rich but semantically empty, the nameless-Panes case"
        )
        return {"usable": False, "count": count, "named_fraction": fraction, "reason": reason}
    return {"usable": True, "count": count, "named_fraction": fraction, "reason": "ok"}


def uia_candidates(window_handle: int, policy: Optional[dict] = None) -> tuple[list[dict], dict]:
    """Candidate boxes from the UIA tree. Returns (candidates, assessment)."""
    cfg = _cfg(policy)
    cand_cfg = cfg.get("candidates", {})
    tree_cfg = cfg.get("uia_tree", {})

    frame = tuple(int(v) for v in window_snapshot(window_handle)["bounds"])  # type: ignore[assignment]
    nodes = get_uia_tree(
        window_handle,
        max_depth=int(tree_cfg.get("max_depth", 8)),
        max_nodes=int(tree_cfg.get("max_nodes", 400)),
    )
    assessment = assess_uia_tree(nodes, frame, cfg)  # type: ignore[arg-type]

    kept = _dedupe(_usable_nodes(nodes, frame, cand_cfg))  # type: ignore[arg-type]
    kept = _interactive_first(kept)[: int(cand_cfg.get("max_candidates", 12))]
    return _number(kept, "uia"), assessment


def omniparser_candidates(
    image_png: bytes, frame: Bounds, policy: Optional[dict] = None
) -> list[dict]:
    """Candidate boxes from an OmniParser deployment.

    Raises `ClassifiedToolError("state_failure")` when no deployment is
    configured, which is the default. The message names the tier and says
    what would enable it, rather than returning an empty list that reads
    like "OmniParser looked and found nothing" — the same distinction
    `perception_find_element` already draws for the OCR tier.
    """
    op_cfg = _cfg(policy).get("omniparser", {})
    mode = (op_cfg.get("mode") or "disabled").strip().lower()

    if mode == "disabled":
        raise ClassifiedToolError(
            "state_failure",
            "the OmniParser candidate fallback is not configured (omniparser.mode: disabled in "
            "orbit/config/perception_policy.yaml). It is disabled by default on purpose: "
            "OmniParser's published requirements pull in easyocr and paddleocr — the same stacks "
            "this build already declined for the OCR tier — plus torch, torchvision, ultralytics "
            "and transformers, and its v2 icon detector is AGPL-3.0. To enable it, deploy "
            "OmniParser somewhere else and set omniparser.mode: http with an endpoint.",
        )
    if mode != "http":
        raise ClassifiedToolError(
            "reasoning_failure",
            f"omniparser.mode is {mode!r}; the only supported values are 'disabled' and 'http'. "
            "There is deliberately no in-process mode — see orbit/mcp_servers/CLAUDE.md.",
        )

    endpoint = (op_cfg.get("endpoint") or "").strip()
    if not endpoint:
        raise ClassifiedToolError(
            "reasoning_failure",
            "omniparser.mode is 'http' but omniparser.endpoint is empty.",
        )

    import base64

    payload = json.dumps(
        {"image_base64": base64.b64encode(image_png).decode("ascii")}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=float(op_cfg.get("timeout_s", 60))) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ClassifiedToolError(
            "tool_failure",
            f"OmniParser endpoint {endpoint!r} did not return usable detections: {exc}",
            retryable=True,
        ) from exc

    return _number(
        _parse_omniparser_detections(body, frame, op_cfg, _cfg(policy).get("candidates", {})),
        "omniparser",
    )


def _parse_omniparser_detections(
    body: Any, frame: Bounds, op_cfg: dict, cand_cfg: dict
) -> list[dict]:
    """Normalise an OmniParser response into node dicts.

    Accepts boxes as either absolute pixels or 0-1 normalised, because
    OmniParser's own output has shipped both ways depending on version and
    wrapper. Normalised is detected by every coordinate being <= 1.0 — a real
    pixel box on any window this tool can capture is wider than one pixel, so
    the test cannot misfire on a genuine absolute box.
    """
    detections = body.get("detections") if isinstance(body, dict) else body
    if not isinstance(detections, list):
        raise ClassifiedToolError(
            "tool_failure",
            "OmniParser response had no 'detections' list",
            retryable=True,
        )

    min_conf = float(op_cfg.get("min_confidence", 0.3))
    fw, fh = frame[2] - frame[0], frame[3] - frame[1]
    nodes: list[dict] = []
    for det in detections:
        box = det.get("bbox") or det.get("box")
        if not box or len(box) != 4:
            continue
        if float(det.get("confidence", 1.0)) < min_conf:
            continue
        x0, y0, x1, y1 = (float(v) for v in box)
        if max(x0, y0, x1, y1) <= 1.0:
            x0, x1 = x0 * fw, x1 * fw
            y0, y1 = y0 * fh, y1 * fh
        nodes.append(
            {
                "bounds": (
                    int(frame[0] + x0),
                    int(frame[1] + y0),
                    int(frame[0] + x1),
                    int(frame[1] + y1),
                ),
                "name": det.get("content") or det.get("label") or None,
                "role": det.get("type") or "Icon",
                "depth": 0,
                "visible": True,
            }
        )

    kept = _dedupe(_usable_nodes(nodes, frame, cand_cfg))
    return _interactive_first(kept)[: int(cand_cfg.get("max_candidates", 12))]


def generate_candidates(
    window_handle: int,
    *,
    image_png: Optional[bytes] = None,
    policy: Optional[dict] = None,
) -> dict:
    """The entry point: UIA first, OmniParser only if UIA is unhelpful.

    Never raises when UIA succeeds. When UIA is unhelpful AND the fallback is
    unavailable, returns whatever UIA did manage plus an explicit
    `fallback_error` — a degraded answer with its degradation stated beats
    either an exception (which would take out an otherwise-working vision
    call) or a silent empty list.
    """
    cfg = _cfg(policy)
    candidates, assessment = uia_candidates(window_handle, cfg)

    result = {
        "candidates": candidates,
        "source": "uia",
        "uia_assessment": assessment,
        "fallback_error": None,
    }
    if assessment["usable"]:
        return result

    if image_png is None:
        result["fallback_error"] = (
            "UIA tree was unhelpful and no screenshot was supplied to run the fallback on"
        )
        return result

    frame = tuple(int(v) for v in window_snapshot(window_handle)["bounds"])  # type: ignore[assignment]
    try:
        fallback = omniparser_candidates(image_png, frame, cfg)  # type: ignore[arg-type]
    except ClassifiedToolError as exc:
        result["fallback_error"] = str(exc)
        return result

    if fallback:
        result["candidates"] = fallback
        result["source"] = "omniparser"
    return result
