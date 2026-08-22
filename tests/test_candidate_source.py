"""Tests for vision-tier candidate generation (Phase 1).

These are geometry-and-policy tests: synthetic UIA node lists in, candidate
boxes out. Nothing here connects to a real window or a real model, because
what can silently break is the filtering — a dedupe that drops the real
control, an area filter that keeps the whole window, a "tree looks fine"
check that passes on Solitaire.

Per tests/CLAUDE.md, assertions are on the returned structures, never on
prose.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from orbit.mcp_servers.candidate_source import (
    assess_uia_tree,
    generate_candidates,
    omniparser_candidates,
    _parse_omniparser_detections,
)
from orbit.policy import load_perception_policy
from orbit.tools.foundation import ClassifiedToolError

FRAME = (0, 0, 800, 600)


def _node(bounds, name=None, role="Button", depth=3, visible=True, automation_id=None):
    return {
        "bounds": bounds,
        "name": name,
        "role": role,
        "depth": depth,
        "visible": visible,
        "automation_id": automation_id,
    }


def _policy(**overrides):
    p = load_perception_policy()
    cand = dict(p.get("candidates", {}))
    cand.update(overrides)
    return {**p, "candidates": cand}


# --- geometry filtering ----------------------------------------------------


def test_full_window_wrappers_are_dropped():
    """A box containing everything cannot answer "which box is the button".
    UIA trees are full of these — the window itself is depth 0."""
    nodes = [
        _node((0, 0, 800, 600), name="Main Window", role="Window", depth=0),
        _node((10, 10, 90, 40), name="Save"),
        _node((100, 10, 180, 40), name="Open"),
        _node((190, 10, 270, 40), name="Find"),
    ]
    a = assess_uia_tree(nodes, FRAME, _policy())
    assert a["count"] == 3, "the full-window box should not be counted as usable"


def test_slivers_are_dropped():
    nodes = [
        _node((10, 10, 90, 40), name="Save"),
        _node((10, 50, 790, 51), name="Separator"),  # 1px tall
        _node((10, 60, 12, 200), name="Edge"),  # 2px wide
    ]
    a = assess_uia_tree(nodes, FRAME, _policy())
    assert a["count"] == 1


def test_invisible_nodes_are_dropped():
    nodes = [
        _node((10, 10, 90, 40), name="Save"),
        _node((100, 10, 180, 40), name="Hidden", visible=False),
    ]
    a = assess_uia_tree(nodes, FRAME, _policy())
    assert a["count"] == 1


def test_bounds_are_clipped_to_the_window_frame():
    """A control reported partly outside the window (a scrolled-off row, a
    resize border) must be numbered at its VISIBLE rectangle — the overlay is
    drawn on a capture of the window, so an unclipped box marks pixels the
    model was never shown.

    A maximized window on this machine really does report (-9, -9, ...): see
    _window_bounds_to_region in perception_tools.py, which clamps for the
    same reason on the capture side.
    """
    from orbit.mcp_servers.candidate_source import _usable_nodes

    nodes = [_node((-50, -20, 120, 60), name="Clipped")]
    kept = _usable_nodes(nodes, FRAME, _policy()["candidates"])
    assert kept[0]["bounds"] == (0, 0, 120, 60)


def test_a_box_entirely_outside_the_frame_is_dropped_not_inverted():
    """Clipping an off-screen box yields a negative-width rectangle. Keeping
    it would put an inverted box in the candidate list, which draws as
    nothing and scores as a phantom."""
    from orbit.mcp_servers.candidate_source import _usable_nodes

    nodes = [_node((900, 700, 1000, 800), name="Offscreen")]
    assert _usable_nodes(nodes, FRAME, _policy()["candidates"]) == []


def test_identical_bounds_are_deduped_keeping_the_named_one():
    """UIA reports a control and its wrappers at the same rectangle.
    Numbering all of them makes several indices correct for one question,
    which is unscoreable."""
    nodes = [
        _node((10, 10, 90, 40), name=None, role="Pane", depth=2),
        _node((10, 10, 90, 40), name="Save", role="Button", depth=4),
        _node((100, 10, 180, 40), name="Open"),
        _node((190, 10, 270, 40), name="Find"),
    ]
    a = assess_uia_tree(nodes, FRAME, _policy())
    assert a["count"] == 3
    assert a["named_fraction"] == 1.0, "the named duplicate should have won the dedupe"


# --- the "unhelpful tree" test --------------------------------------------


def test_nameless_panes_tree_is_judged_unusable():
    """The Microsoft Solitaire case, which is the entire reason the fallback
    exists: structurally rich, semantically empty. A count-only check passes
    this tree and would never reach the fallback."""
    nodes = [_node((i * 70 + 10, 100, i * 70 + 70, 200), name=None, role="Pane") for i in range(9)]
    a = assess_uia_tree(nodes, FRAME, _policy())
    assert a["count"] == 9, "the boxes are real; this is not an empty tree"
    assert not a["usable"]
    assert "name" in a["reason"]


def test_a_tree_with_too_few_boxes_is_unusable():
    nodes = [_node((10, 10, 90, 40), name="OK")]
    a = assess_uia_tree(nodes, FRAME, _policy())
    assert not a["usable"]


def test_a_normal_named_tree_is_usable():
    nodes = [_node((10 + i * 90, 10, 80 + i * 90, 40), name=f"Btn{i}") for i in range(5)]
    a = assess_uia_tree(nodes, FRAME, _policy())
    assert a["usable"]
    assert a["named_fraction"] == 1.0


def test_empty_tree_is_unusable():
    a = assess_uia_tree([], FRAME, _policy())
    assert not a["usable"]
    assert a["count"] == 0


# --- OmniParser: disabled by default, and honest about it ------------------


def test_omniparser_is_disabled_by_default():
    """The shipped default. If this ever flips, a multi-GB AGPL-encumbered
    dependency became reachable without anyone deciding to make it so."""
    assert (load_perception_policy()["omniparser"]["mode"]) == "disabled"


def test_disabled_omniparser_raises_rather_than_returning_empty():
    """An empty list reads like "OmniParser looked and found nothing". The
    tier being absent is a different fact and has to stay distinguishable —
    the same distinction perception_find_element already draws for OCR."""
    with pytest.raises(ClassifiedToolError) as exc:
        omniparser_candidates(b"png", FRAME, _policy_with_omniparser(mode="disabled"))
    assert exc.value.kind == "state_failure"
    assert "omniparser" in str(exc.value).lower()


def test_unknown_omniparser_mode_is_rejected():
    with pytest.raises(ClassifiedToolError) as exc:
        omniparser_candidates(b"png", FRAME, _policy_with_omniparser(mode="local"))
    assert exc.value.kind == "reasoning_failure"


def test_http_mode_without_endpoint_is_rejected():
    with pytest.raises(ClassifiedToolError) as exc:
        omniparser_candidates(b"png", FRAME, _policy_with_omniparser(mode="http", endpoint=""))
    assert exc.value.kind == "reasoning_failure"


def _policy_with_omniparser(**overrides):
    p = load_perception_policy()
    op = dict(p.get("omniparser", {}))
    op.update(overrides)
    return {**p, "omniparser": op}


# --- OmniParser response parsing ------------------------------------------


def test_absolute_pixel_detections_are_offset_by_the_window_origin():
    """Detections come back relative to the captured image; candidates must be
    in screen coordinates like every other ElementRef in this codebase."""
    frame = (100, 50, 900, 650)
    body = {"detections": [
        {"bbox": [10, 20, 90, 60], "confidence": 0.9, "content": "Save"},
        {"bbox": [110, 20, 190, 60], "confidence": 0.9, "content": "Open"},
        {"bbox": [210, 20, 290, 60], "confidence": 0.9, "content": "Find"},
    ]}
    nodes = _parse_omniparser_detections(body, frame, {"min_confidence": 0.3}, _policy()["candidates"])
    bounds = {tuple(n["bounds"]) for n in nodes}
    assert (110, 70, 190, 110) in bounds


def test_normalised_detections_are_scaled_to_the_frame():
    frame = (0, 0, 800, 600)
    body = {"detections": [
        {"bbox": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9},
        {"bbox": [0.3, 0.1, 0.4, 0.2], "confidence": 0.9},
        {"bbox": [0.5, 0.1, 0.6, 0.2], "confidence": 0.9},
    ]}
    nodes = _parse_omniparser_detections(body, frame, {"min_confidence": 0.3}, _policy()["candidates"])
    bounds = {tuple(n["bounds"]) for n in nodes}
    assert (80, 60, 160, 120) in bounds


def test_low_confidence_detections_are_dropped():
    body = {"detections": [
        {"bbox": [10, 20, 90, 60], "confidence": 0.9},
        {"bbox": [110, 20, 190, 60], "confidence": 0.05},
    ]}
    nodes = _parse_omniparser_detections(body, FRAME, {"min_confidence": 0.3}, _policy()["candidates"])
    assert len(nodes) == 1


def test_malformed_omniparser_body_is_a_retryable_tool_failure():
    with pytest.raises(ClassifiedToolError) as exc:
        _parse_omniparser_detections({"nope": []}, FRAME, {}, _policy()["candidates"])
    assert exc.value.kind == "tool_failure"


# --- output shape ----------------------------------------------------------


def test_candidates_are_numbered_densely_from_one_in_reading_order():
    """Phase 1's contract with the prompt step: a list of {index, bounds},
    numbered 1..N top-to-bottom so a human reading the overlay can find box 7
    without a lookup table."""
    nodes = [
        _node((400, 300, 460, 340), name="C"),
        _node((10, 10, 70, 50), name="A"),
        _node((200, 10, 260, 50), name="B"),
    ]
    from orbit.mcp_servers.candidate_source import _dedupe, _number, _usable_nodes

    out = _number(_dedupe(_usable_nodes(nodes, FRAME, _policy()["candidates"])), "uia")
    assert [c["index"] for c in out] == [1, 2, 3]
    assert [c["name"] for c in out] == ["A", "B", "C"]
    for c in out:
        assert set(("index", "bounds")).issubset(c.keys())
        assert len(c["bounds"]) == 4


def test_max_candidates_caps_the_list():
    nodes = [_node((i * 30, 10, i * 30 + 25, 50), name=f"B{i}") for i in range(40)]
    from orbit.mcp_servers.candidate_source import (
        _dedupe,
        _interactive_first,
        _number,
        _usable_nodes,
    )

    cand_cfg = _policy()["candidates"]
    kept = _interactive_first(_dedupe(_usable_nodes(nodes, FRAME, cand_cfg)))
    kept = kept[: int(cand_cfg["max_candidates"])]
    assert len(_number(kept, "uia")) == int(cand_cfg["max_candidates"])


def test_container_roles_lose_to_real_controls_when_truncating():
    """max_candidates truncates, so the boxes that survive should be the ones
    a user could plausibly mean."""
    from orbit.mcp_servers.candidate_source import _interactive_first

    nodes = [
        {"bounds": (0, 0, 100, 100), "role": "Pane", "name": "wrap", "depth": 1},
        {"bounds": (10, 10, 60, 40), "role": "Button", "name": "Go", "depth": 3},
    ]
    assert _interactive_first(nodes)[0]["role"] == "Button"


def test_candidate_cap_matches_the_benchmark_mark_count():
    """The set-of-mark prompt numbers exactly these boxes. A cap the benchmark
    never measured is an untested configuration."""
    from benchmarks.config import load_benchmark_config

    assert int(load_perception_policy()["candidates"]["max_candidates"]) == (
        load_benchmark_config().max_marks
    )
