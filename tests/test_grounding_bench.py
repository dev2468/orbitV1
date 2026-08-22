"""Tests for the grounding benchmark's deterministic half.

No model call is made anywhere in this file — the benchmark's whole point is
that the model is the variable, so what these tests pin is everything
AROUND the model: that fixtures have exact ground truth, that candidate sets
are answerable, that scoring counts a hit only when a point is genuinely
inside the target, and that a failed call is scored as a miss rather than
silently dropped.

The trap this guards is specific: a benchmark that quietly mis-scores
reports a number that looks fine and is wrong, and every phase after this
one makes a go/no-go decision on that number.
"""

from __future__ import annotations

import json

import pytest

from benchmarks.config import load_benchmark_config
from benchmarks.fixtures import all_targets, build_scenes
from benchmarks.grounding_bench import (
    _CROP_ORIGIN,
    _offset,
    run_one,
    summarize,
)
from benchmarks.overlay import parse_index_reply, select_candidates
from benchmarks.raster import Canvas, center_of, contains


# --- fixtures: ground truth is exact, not approximate ----------------------


def test_every_target_resolves_to_an_element_with_bounds():
    for scene in build_scenes():
        for target in scene.targets:
            bounds = scene.bounds_for(target)
            assert bounds[2] > bounds[0] and bounds[3] > bounds[1], (
                f"{target.target_id} has empty bounds {bounds}"
            )


def test_target_bounds_lie_inside_the_image():
    """A target drawn off-canvas is unanswerable, and would be scored as a
    model failure forever."""
    for scene in build_scenes():
        for target in scene.targets:
            left, top, right, bottom = scene.bounds_for(target)
            assert 0 <= left < right <= scene.canvas.width
            assert 0 <= top < bottom <= scene.canvas.height


def test_scene_generation_is_deterministic():
    """Two builds must be byte-identical, or numbers from different phases
    are not comparable — which is the entire reason this harness exists
    rather than a one-off script."""
    first = {s.scene_id: s.canvas.to_png() for s in build_scenes()}
    second = {s.scene_id: s.canvas.to_png() for s in build_scenes()}
    assert first == second


def test_element_ids_are_unique_within_a_scene():
    for scene in build_scenes():
        ids = [e.element_id for e in scene.elements]
        assert len(ids) == len(set(ids)), f"{scene.scene_id} has duplicate element ids"


# --- candidate selection ---------------------------------------------------


def test_candidate_set_always_contains_the_answer():
    """Set-of-Mark can only pick from what it is shown. A candidate set
    missing the answer measures candidate generation, not grounding — the
    two are kept separable on purpose."""
    cfg = load_benchmark_config()
    for scene, target in all_targets(build_scenes()):
        cands = select_candidates(scene, target, cfg.max_marks)
        assert target.element_id in {c.element_id for c in cands}


def test_candidate_indices_are_dense_and_one_based():
    cfg = load_benchmark_config()
    for scene, target in all_targets(build_scenes()):
        cands = select_candidates(scene, target, cfg.max_marks)
        assert [c.index for c in cands] == list(range(1, len(cands) + 1))


def test_candidate_selection_is_deterministic():
    cfg = load_benchmark_config()
    scene = build_scenes(["dense_grid"])[0]
    target = scene.targets[0]
    a = select_candidates(scene, target, cfg.max_marks)
    b = select_candidates(scene, target, cfg.max_marks)
    assert [(c.index, c.element_id) for c in a] == [(c.index, c.element_id) for c in b]


def test_answer_index_is_not_always_the_same_number():
    """If the correct index were always 1, a model that always answers 1
    would score 100% and the arm would look like a breakthrough."""
    cfg = load_benchmark_config()
    answers = set()
    for scene, target in all_targets(build_scenes()):
        cands = select_candidates(scene, target, cfg.max_marks)
        answers.add(next(c.index for c in cands if c.element_id == target.element_id))
    assert len(answers) > 1, f"correct index is always {answers}"


# --- reply parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "reply,expected",
    [
        ('{"index": 4}', 4),
        ("The answer is box 7.", 7),
        ("7", 7),
        ('```json\n{"index": 2}\n```', 2),
        ("index: 3", 3),
    ],
)
def test_index_replies_are_parsed_from_the_shapes_models_actually_emit(reply, expected):
    assert parse_index_reply(reply, set(range(1, 13))) == expected


def test_out_of_range_index_is_rejected_not_clamped():
    """Same reasoning as _in_normalised_range in perception_tools: a nonsense
    answer carried forward looks exactly as trustworthy as a good one."""
    assert parse_index_reply('{"index": 99}', set(range(1, 13))) is None


def test_unparseable_reply_returns_none():
    assert parse_index_reply("I cannot tell which box that is.", {1, 2, 3}) is None
    assert parse_index_reply("", {1, 2, 3}) is None


# --- scoring ---------------------------------------------------------------


class _Arm:
    def __init__(self, shape: str) -> None:
        self.id = "test"
        self.prompt_shape = shape
        self.model = "unused/in-these-tests"


def test_dry_run_scores_every_target_as_a_hit():
    """The dry run answers correctly by construction, so anything below 100%
    is a harness bug. This is the assertion that caught the real one: the
    fake answer normalised without undoing the crop origin, and picked SoM
    candidates geometrically so a slider's TRACK beat its HANDLE."""
    cfg = load_benchmark_config()
    for shape in ("point", "set_of_mark"):
        for scene, target in all_targets(build_scenes()):
            r = run_one(_Arm(shape), scene, target, cfg, dry_run=True)
            assert r.hit, f"{shape}/{target.target_id} missed on a correct answer: {r.raw_reply}"
            assert r.answered
            assert r.error is None


def test_a_wrong_point_is_scored_a_miss(monkeypatch):
    """The complement of the test above — proves the scorer can fail, which
    a 100%-passing dry run alone does not."""
    import benchmarks.grounding_bench as gb

    cfg = load_benchmark_config()
    scene = build_scenes(["native_like"])[0]
    target = scene.targets[0]
    monkeypatch.setattr(
        gb, "_fake_reply", lambda *a, **k: json.dumps({"point": [999, 999]})
    )
    r = run_one(_Arm("point"), scene, target, cfg, dry_run=True)
    assert not r.hit
    assert r.answered
    assert r.miss_px > 0


def test_a_failed_call_counts_as_a_miss_not_a_drop(monkeypatch):
    """The spike's convention: provider errors are counted as misses "rather
    than quietly dropped". An arm that errors on half its calls must not
    score as if those calls never happened."""
    import benchmarks.grounding_bench as gb

    cfg = load_benchmark_config()
    scene = build_scenes(["native_like"])[0]
    target = scene.targets[0]

    def boom(*_a, **_k):
        raise RuntimeError("HTTP 504 upstream timeout")

    monkeypatch.setattr(gb, "_fake_reply", boom)
    r = run_one(_Arm("point"), scene, target, cfg, dry_run=True)
    assert not r.hit
    assert not r.answered
    assert "504" in r.error

    s = summarize([r])["test"]
    assert s["total"] == 1 and s["hits"] == 0 and s["errors"] == 1
    assert s["rate"] == 0.0


def test_summary_separates_answered_from_total():
    """The spike reported both (35/46 overall, 35/42 of calls that answered).
    Conflating them hides provider flakiness inside an accuracy number."""
    import benchmarks.grounding_bench as gb

    rows = [
        gb.CallResult("a", "s", "c", "t1", "d", True, True, (1.0, 1.0), 0.0, 10, None, ""),
        gb.CallResult("a", "s", "c", "t2", "d", False, False, None, None, 10, "boom", ""),
    ]
    s = summarize(rows)["a"]
    assert s["rate"] == 0.5
    assert s["rate_of_answered"] == 1.0
    assert s["errors"] == 1


# --- retry policy ----------------------------------------------------------


def test_provider_connection_errors_are_treated_as_transient():
    """These two are what the endpoint actually produced on 2026-08-22, when
    23 of 24 calls died. If they stopped being retried, the benchmark would
    go back to reporting provider outages as grounding accuracy."""
    from benchmarks.grounding_bench import _is_transient

    class InternalServerError(Exception):
        pass

    class Timeout(Exception):
        pass

    assert _is_transient(InternalServerError("Nvidia_nimException - Connection error."))
    assert _is_transient(Timeout("APITimeoutError - Request timed out."))


def test_auth_and_model_errors_are_not_retried():
    """A bad key or a retired model string must fail fast and loudly. Retrying
    it three times then scoring it a miss would report a configuration
    mistake as a grounding result."""
    from benchmarks.grounding_bench import _is_transient

    class AuthenticationError(Exception):
        pass

    class BadRequestError(Exception):
        pass

    assert not _is_transient(AuthenticationError("invalid api key"))
    assert not _is_transient(BadRequestError("model not found"))


def test_retry_gives_up_after_max_retries_and_reports_a_miss(monkeypatch):
    import benchmarks.grounding_bench as gb

    cfg = load_benchmark_config()
    calls = {"n": 0}

    class Timeout(Exception):
        pass

    def always_timeout(*_a, **_k):
        calls["n"] += 1
        raise Timeout("Request timed out.")

    monkeypatch.setattr(gb, "_call_model", always_timeout)
    monkeypatch.setattr(gb.time, "sleep", lambda _s: None)

    reply, attempts, exc = gb._call_model_with_retries("b64", "prompt", "m", cfg)
    assert reply == ""
    assert exc is not None
    assert calls["n"] == cfg.max_retries + 1, "should try once then retry max_retries times"
    assert attempts == cfg.max_retries + 1


def test_retry_returns_the_first_success(monkeypatch):
    import benchmarks.grounding_bench as gb

    cfg = load_benchmark_config()
    calls = {"n": 0}

    class Timeout(Exception):
        pass

    def flaky(*_a, **_k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise Timeout("Request timed out.")
        return '{"point": [500, 500]}'

    monkeypatch.setattr(gb, "_call_model", flaky)
    monkeypatch.setattr(gb.time, "sleep", lambda _s: None)

    reply, attempts, exc = gb._call_model_with_retries("b64", "prompt", "m", cfg)
    assert exc is None
    assert attempts == 3
    assert "point" in reply


def test_non_transient_error_is_not_retried(monkeypatch):
    import benchmarks.grounding_bench as gb

    cfg = load_benchmark_config()
    calls = {"n": 0}

    class AuthenticationError(Exception):
        pass

    def bad_key(*_a, **_k):
        calls["n"] += 1
        raise AuthenticationError("invalid api key")

    monkeypatch.setattr(gb, "_call_model", bad_key)
    monkeypatch.setattr(gb.time, "sleep", lambda _s: None)

    _reply, attempts, exc = gb._call_model_with_retries("b64", "prompt", "m", cfg)
    assert calls["n"] == 1, "a bad key must not be retried"
    assert attempts == 1
    assert exc is not None


def test_default_concurrency_is_one():
    """Raising this failed 23 of 24 calls on the live endpoint while the same
    requests succeeded serially. The default is load-bearing, not a
    preference."""
    assert load_benchmark_config().concurrency == 1


# --- the crop-origin round trip -------------------------------------------


def test_ground_truth_is_offset_by_the_synthetic_crop_origin():
    """Fixtures are scored in "screen" space, not image space. If this ever
    became a no-op the benchmark would stop exercising the crop-undo step,
    and the constant-offset bug it exists to catch would pass unnoticed."""
    assert _CROP_ORIGIN != (0, 0)
    assert _offset((10, 20, 30, 40), _CROP_ORIGIN) == (
        10 + _CROP_ORIGIN[0],
        20 + _CROP_ORIGIN[1],
        30 + _CROP_ORIGIN[0],
        40 + _CROP_ORIGIN[1],
    )


# --- raster primitives -----------------------------------------------------


def test_contains_is_right_and_bottom_exclusive():
    b = (10, 10, 20, 20)
    assert contains(b, 10, 10)
    assert contains(b, 19, 19)
    assert not contains(b, 20, 20)
    assert not contains(b, 9, 15)


def test_canvas_copy_does_not_alias_the_original():
    """draw_marks returns a copy so the freeform arm sees an unmarked image.
    If the copy aliased, the two arms would silently be looking at the same
    marked picture and the comparison would be meaningless."""
    c = Canvas(20, 20, (255, 255, 255))
    clone = c.copy()
    clone.fill_rect((0, 0, 20, 20), (0, 0, 0))
    assert c.buf != clone.buf


def test_center_of_matches_element_ref_semantics():
    """ElementRef.center() uses integer floor division; the benchmark scores
    against the same midpoint so a hit here means a hit there."""
    assert center_of((0, 0, 10, 10)) == (5, 5)
    assert center_of((0, 0, 11, 11)) == (5, 5)
