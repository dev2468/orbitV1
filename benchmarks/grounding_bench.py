"""The grounding benchmark runner.

Measures one thing: given a window image and a plain-language description,
how often does the vision tier land a point inside the element's true
bounds, and when it misses, by how far. Every arm defined in
`orbit/config/vision_benchmark.yaml` is run against the identical fixture
set so the arms can be compared to each other.

WHAT IT REUSES, AND WHY THAT MATTERS
------------------------------------
The parse and coordinate-translation steps are imported from
`orbit.mcp_servers.perception_tools`, not reimplemented here. A benchmark
that reimplements the code it is benchmarking measures the reimplementation
— it would happily report a healthy number while the real tool's parser
rejected every reply. `_parse_vision_reply`, `_vision_point_to_screen` and
`_fit_for_inline_upload` are the real ones.

What is NOT exercised is the live capture step (`mss` grab of a real window
rect): fixtures are images, not windows. That is the deliberate trade
documented in `benchmarks/fixtures.py`. The capture path has its own tests
in `tests/test_perception_tools.py`.

THE SYNTHETIC CROP ORIGIN
-------------------------
Fixtures are fed through the same crop -> resize -> normalise -> invert
chain a real capture goes through, with a non-zero `_CROP_ORIGIN`. Ground
truth is offset by the same origin. If the inverse transform ever loses the
crop-undo step, every answer shifts by a constant and the hit-rate collapses
here — which is exactly the failure mode the VISION TIER notes call out as
"plausible-looking, and far harder to notice than an obviously wrong
answer".

Run it:
    venv\\Scripts\\python.exe -m benchmarks.grounding_bench --dry-run
    venv\\Scripts\\python.exe -m benchmarks.grounding_bench
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.config import Arm, BenchmarkConfig, load_benchmark_config
from benchmarks.fixtures import Scene, Target, all_targets, build_scenes
from benchmarks.overlay import (
    SOM_PROMPT,
    Candidate,
    draw_marks,
    parse_index_reply,
    select_candidates,
)
from benchmarks.raster import Bounds, center_of, contains
from orbit.mcp_servers.perception_tools import (
    _VISION_MODEL,
    _VISION_PROMPT,
    _fit_for_inline_upload,
    _nim_api_key,
    _parse_vision_reply,
    _vision_point_to_screen,
)

# Non-zero on purpose — see module docstring.
_CROP_ORIGIN = (100, 50)

# Which env var each provider needs, mirroring orbit/agent.py's
# _REQUIRED_KEY_BY_PREFIX rather than inventing a second convention. Phase 6
# adds arms on providers this project has never called, and a missing key
# must fail with the exact line to add to .env — not as a run of timeouts
# that looks like the model grounding badly.
_REQUIRED_KEY_BY_PREFIX = {
    "nvidia_nim/": "NVIDIA_NIM_API_KEY",
    "groq/": "GROQ_API_KEY",
    "deepseek/": "DEEPSEEK_API_KEY",
    "anthropic/": "ANTHROPIC_API_KEY",
    "openrouter/": "OPENROUTER_API_KEY",
    "together_ai/": "TOGETHER_API_KEY",
}


def _api_key_for(model: str) -> str:
    """Resolve the credential this model needs, or say exactly what is missing.

    NVIDIA keeps going through perception_tools._nim_api_key so the benchmark
    and the live tool read the key the same way (it also loads .env, which
    matters in a subprocess). Everything else reads the environment directly.
    """
    if model.startswith("nvidia_nim/"):
        return _nim_api_key()
    for prefix, env_var in _REQUIRED_KEY_BY_PREFIX.items():
        if model.startswith(prefix):
            key = os.environ.get(env_var, "").strip()
            if not key:
                try:
                    from dotenv import load_dotenv

                    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
                    key = os.environ.get(env_var, "").strip()
                except Exception:
                    pass
            if not key:
                raise RuntimeError(
                    f"{env_var} is not set, but arm model {model!r} needs it.\n"
                    f"Add this line to the .env file in the project root:\n"
                    f"    {env_var}=your-key-here"
                )
            return key
    raise RuntimeError(
        f"no provider key mapping for model {model!r} — add its prefix to "
        "_REQUIRED_KEY_BY_PREFIX in benchmarks/grounding_bench.py"
    )


@dataclass
class CallResult:
    arm: str
    scene_id: str
    category: str
    target_id: str
    description: str
    hit: bool
    answered: bool
    point: tuple[float, float] | None
    miss_px: float | None
    latency_ms: int
    error: str | None
    raw_reply: str
    attempts: int = 1
    model: str = ""


def _offset(bounds: Bounds, origin: tuple[int, int]) -> Bounds:
    return (
        bounds[0] + origin[0],
        bounds[1] + origin[1],
        bounds[2] + origin[0],
        bounds[3] + origin[1],
    )


def _is_transient(exc: Exception) -> bool:
    """Is this a provider hiccup worth retrying, or a real answer about the
    request?

    Deliberately narrow — an auth failure or a bad model string must fail
    fast and loudly rather than being retried three times and reported as a
    grounding miss. The two names below are what this endpoint actually
    produced on 2026-08-22 (`Nvidia_nimException - Connection error` and
    `APITimeoutError`), not a guess at the space of possible errors.
    """
    name = type(exc).__name__
    if name in {"Timeout", "APITimeoutError", "InternalServerError", "ServiceUnavailableError",
                "RateLimitError", "APIConnectionError"}:
        return True
    text = str(exc).lower()
    return "connection error" in text or "timed out" in text


def _call_model_with_retries(
    image_b64: str, prompt: str, model: str, cfg: BenchmarkConfig
) -> tuple[str, int, Exception | None]:
    """Call, retrying transient provider failures. Returns (reply, attempts,
    final_error).

    Retrying does NOT soften the scoring: a reply that arrives and is wrong
    is still a miss, and an error that survives every attempt is still a
    miss. What it prevents is reporting NVIDIA's connection errors as the
    model's grounding accuracy — the failure mode that made the first live
    run unusable.
    """
    last: Exception | None = None
    for attempt in range(1, cfg.max_retries + 2):
        try:
            return _call_model(image_b64, prompt, model, cfg.request_timeout_s), attempt, None
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            last = exc
            if not _is_transient(exc) or attempt > cfg.max_retries:
                return "", attempt, exc
            time.sleep(cfg.retry_backoff_s * attempt)
    return "", cfg.max_retries + 1, last


def _call_model(image_b64: str, prompt: str, model: str, timeout_s: float) -> str:
    """One grounding call. Mirrors `VisionLocateTool._call_vision_model`'s
    shape (same provider, same message layout, same max_tokens) so the
    benchmark is measuring the same request the tool would make."""
    import litellm

    response = litellm.completion(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            }
        ],
        api_key=_api_key_for(model),
        max_tokens=400,
        timeout=timeout_s,
    )
    return response.choices[0].message.content or ""


def _fake_reply(
    shape: str,
    candidates: list[Candidate] | None,
    element_id: str,
    gt: Bounds,
    image_w: int,
    image_h: int,
) -> str:
    """Deterministic stand-in used by --dry-run.

    Answers correctly so a dry run proves the *pipeline* end to end — image
    build, encode, parse, translate, score — without spending a cent. A dry
    run reporting anything below 100% is a bug in the harness, not a result;
    that is the whole point of having it.

    The correct SoM answer is the candidate whose element_id IS the target,
    looked up by id rather than by geometry. Geometry is wrong here: scenes
    contain nested elements (a slider handle sits inside its track, the
    close button inside its panel), so "first candidate containing the
    target's centre" returns the enclosing box and scores a miss.
    """
    if shape == "set_of_mark":
        assert candidates is not None
        for cand in candidates:
            if cand.element_id == element_id:
                return json.dumps({"index": cand.index})
        raise AssertionError(f"candidate set for {element_id!r} omits the answer")

    # Ground truth is in "screen" space (image coords + crop origin). Undo the
    # origin before normalising, because that is the space the model answers
    # in — feeding screen coords straight into the 0-1000 conversion is the
    # exact off-by-a-constant bug _vision_point_to_screen exists to prevent.
    cx, cy = center_of(gt)
    ix, iy = cx - _CROP_ORIGIN[0], cy - _CROP_ORIGIN[1]
    return json.dumps({"point": [round(iy * 1000 / image_h), round(ix * 1000 / image_w)]})


def run_one(
    arm: Arm,
    scene: Scene,
    target: Target,
    cfg: BenchmarkConfig,
    dry_run: bool,
) -> CallResult:
    gt_bounds = _offset(scene.bounds_for(target), _CROP_ORIGIN)
    gt_center = center_of(gt_bounds)
    model = arm.model or _VISION_MODEL

    candidates: list[Candidate] | None = None
    if arm.prompt_shape == "set_of_mark":
        candidates = select_candidates(scene, target, cfg.max_marks)
        canvas = draw_marks(scene, candidates)
        prompt = SOM_PROMPT.format(description=target.description)
    else:
        canvas = scene.canvas
        prompt = _VISION_PROMPT.format(description=target.description)

    png = canvas.to_png()
    sent_png, scale_factor = _fit_for_inline_upload(
        png, bytes(canvas.buf), canvas.width, canvas.height
    )
    sent_w = canvas.width // scale_factor if scale_factor > 1 else canvas.width
    sent_h = canvas.height // scale_factor if scale_factor > 1 else canvas.height
    image_b64 = base64.b64encode(sent_png).decode("ascii")

    started = time.monotonic()
    error: str | None = None
    reply = ""
    attempts = 1
    if dry_run:
        try:
            reply = _fake_reply(
                arm.prompt_shape,
                candidates,
                target.element_id,
                gt_bounds,
                canvas.width,
                canvas.height,
            )
        except Exception as exc:  # noqa: BLE001 - every failure is a miss, and why matters
            error = f"{type(exc).__name__}: {exc}"
    else:
        reply, attempts, exc = _call_model_with_retries(image_b64, prompt, model, cfg)
        if exc is not None:
            error = f"{type(exc).__name__}: {exc}"
    latency_ms = int((time.monotonic() - started) * 1000)

    point: tuple[float, float] | None = None
    if error is None:
        if arm.prompt_shape == "set_of_mark":
            assert candidates is not None
            chosen = parse_index_reply(reply, {c.index for c in candidates})
            if chosen is not None:
                box = next(c.bounds for c in candidates if c.index == chosen)
                point = tuple(float(v) for v in center_of(_offset(box, _CROP_ORIGIN)))
        else:
            parsed = _parse_vision_reply(reply)
            if parsed["kind"] == "point":
                y, x = parsed["point"]
                point = _vision_point_to_screen(
                    y,
                    x,
                    sent_width=sent_w,
                    sent_height=sent_h,
                    scale_factor=scale_factor,
                    crop_origin=_CROP_ORIGIN,
                )
            elif parsed["kind"] == "box_2d":
                y0, x0, y1, x1 = parsed["box"]
                left, top = _vision_point_to_screen(
                    y0, x0, sent_width=sent_w, sent_height=sent_h,
                    scale_factor=scale_factor, crop_origin=_CROP_ORIGIN,
                )
                right, bottom = _vision_point_to_screen(
                    y1, x1, sent_width=sent_w, sent_height=sent_h,
                    scale_factor=scale_factor, crop_origin=_CROP_ORIGIN,
                )
                point = ((left + right) / 2, (top + bottom) / 2)

    hit = point is not None and contains(gt_bounds, point[0], point[1])
    miss_px = (
        math.dist(point, gt_center) if point is not None else None
    )

    return CallResult(
        arm=arm.id,
        scene_id=scene.scene_id,
        category=scene.category,
        target_id=target.target_id,
        description=target.description,
        hit=hit,
        answered=point is not None,
        point=point,
        miss_px=miss_px,
        latency_ms=latency_ms,
        error=error,
        raw_reply=(reply or "")[:500],
        attempts=attempts,
        model=model,
    )


def summarize(results: list[CallResult]) -> dict:
    """Aggregate into the shape the spike reported: overall hit-rate, a
    hit-rate over calls that actually answered, per-category splits, and
    latency. Errors count as misses (the spike's convention — "counted as
    misses below rather than quietly dropped") but are reported separately
    so an arm losing to provider flakiness is distinguishable from an arm
    losing to bad grounding."""
    out: dict = {}
    for arm in sorted({r.arm for r in results}):
        rows = [r for r in results if r.arm == arm]
        answered = [r for r in rows if r.answered]
        misses = [r for r in answered if not r.hit]
        by_cat: dict[str, dict] = {}
        for cat in sorted({r.category for r in rows}):
            crows = [r for r in rows if r.category == cat]
            by_cat[cat] = {
                "hits": sum(1 for r in crows if r.hit),
                "total": len(crows),
                "rate": (sum(1 for r in crows if r.hit) / len(crows)) if crows else 0.0,
            }
        lat = sorted(r.latency_ms for r in rows)
        out[arm] = {
            "model": rows[0].model if rows else None,
            "total_latency_s": round(sum(r.latency_ms for r in rows) / 1000, 1),
            "hits": sum(1 for r in rows if r.hit),
            "total": len(rows),
            "rate": (sum(1 for r in rows if r.hit) / len(rows)) if rows else 0.0,
            "answered": len(answered),
            "errors": sum(1 for r in rows if r.error),
            "unparsed": sum(1 for r in rows if r.error is None and not r.answered),
            "rate_of_answered": (
                sum(1 for r in answered if r.hit) / len(answered) if answered else 0.0
            ),
            "median_miss_px": (
                round(statistics.median([r.miss_px for r in misses]), 1) if misses else None
            ),
            "median_latency_ms": int(statistics.median(lat)) if lat else 0,
            "max_latency_ms": lat[-1] if lat else 0,
            "by_category": by_cat,
        }
    return out


def _print_report(summary: dict, results: list[CallResult]) -> None:
    cats = sorted({r.category for r in results})
    print()
    print("=" * 78)
    print("GROUNDING BENCHMARK")
    print("=" * 78)
    head = f"{'arm':<16}{'overall':>12}{'answered':>10}{'miss px':>10}{'med ms':>9}"
    print(head)
    print("-" * 78)
    for arm, s in summary.items():
        overall = f"{s['hits']}/{s['total']} = {s['rate'] * 100:.0f}%"
        miss = "-" if s["median_miss_px"] is None else f"{s['median_miss_px']:.0f}"
        print(
            f"{arm:<16}{overall:>12}{s['answered']:>10}{miss:>10}{s['median_latency_ms']:>9}"
        )
    print()
    print(f"{'by category':<16}" + "".join(f"{c:>16}" for c in cats))
    print("-" * 78)
    for arm, s in summary.items():
        cells = ""
        for c in cats:
            e = s["by_category"].get(c)
            cells += f"{(str(e['hits']) + '/' + str(e['total'])):>16}" if e else f"{'-':>16}"
        print(f"{arm:<16}" + cells)
    print()
    for arm, s in summary.items():
        if s["errors"] or s["unparsed"]:
            print(f"  {arm}: {s['errors']} call error(s), {s['unparsed']} unparseable reply(ies)")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Vision grounding benchmark")
    ap.add_argument("--dry-run", action="store_true",
                    help="no model calls; deterministic correct answers to prove the pipeline")
    ap.add_argument("--arm", action="append", default=None, help="run only this arm id (repeatable)")
    ap.add_argument("--scene", action="append", default=None, help="run only this scene id")
    ap.add_argument("--max-targets-per-scene", type=int, default=None,
                    help="cap targets per scene. At ~9 min/call on this endpoint the full set is "
                         "a multi-hour run; 1 gives a directional read in ~an hour.")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="override vision_benchmark.yaml's concurrency (default 1; raising it "
                         "failed 23/24 calls on this endpoint)")
    ap.add_argument("--dump-fixtures", metavar="DIR",
                    help="write fixture PNGs (and one marked variant) for inspection, then exit")
    args = ap.parse_args()

    cfg = load_benchmark_config()
    scenes = build_scenes(args.scene or cfg.scenes or None)
    if args.max_targets_per_scene:
        for scene in scenes:
            scene.targets = scene.targets[: args.max_targets_per_scene]

    if args.dump_fixtures:
        out = Path(args.dump_fixtures)
        out.mkdir(parents=True, exist_ok=True)
        for scene in scenes:
            (out / f"{scene.scene_id}.png").write_bytes(scene.canvas.to_png())
            target = scene.targets[0]
            marked = draw_marks(scene, select_candidates(scene, target, cfg.max_marks))
            (out / f"{scene.scene_id}_marked.png").write_bytes(marked.to_png())
        print(f"wrote {len(scenes) * 2} PNGs to {out}")
        return 0

    arms = [a for a in cfg.arms if not args.arm or a.id in set(args.arm)]
    if not arms:
        print(f"no arms matched {args.arm}")
        return 1

    jobs = [(arm, scene, target) for arm in arms for scene, target in all_targets(scenes)]
    print(
        f"{len(jobs)} calls: {len(arms)} arm(s) x {len(all_targets(scenes))} targets"
        + (" [DRY RUN]" if args.dry_run else f"  model={_VISION_MODEL}"),
        flush=True,
    )

    started = time.monotonic()
    workers = 1 if args.dry_run else max(1, args.concurrency or cfg.concurrency)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, a, s, t, cfg, args.dry_run) for a, s, t in jobs]
        results: list[CallResult] = []
        for i, f in enumerate(futures, 1):
            r = f.result()
            results.append(r)
            flag = "HIT " if r.hit else ("ERR " if r.error else "MISS")
            # flush: a live run can sit for minutes per call, and buffered
            # progress on a slow benchmark is indistinguishable from a hang.
            print(f"  [{i:>3}/{len(futures)}] {flag} {r.arm:<14} {r.target_id:<20} "
                  f"{r.latency_ms / 1000:>6.1f}s", flush=True)
    elapsed = time.monotonic() - started

    summary = summarize(results)
    _print_report(summary, results)

    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = cfg.results_dir / f"grounding_{stamp}{'_dryrun' if args.dry_run else ''}.json"
    path.write_text(
        json.dumps(
            {
                "generated_utc": stamp,
                "model": _VISION_MODEL,
                "dry_run": args.dry_run,
                "concurrency": workers,
                "elapsed_s": round(elapsed, 1),
                "crop_origin": list(_CROP_ORIGIN),
                "max_marks": cfg.max_marks,
                "summary": summary,
                "results": [asdict(r) for r in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
