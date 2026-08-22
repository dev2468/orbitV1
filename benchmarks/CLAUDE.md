# benchmarks/ — the vision grounding benchmark

Answers one question, repeatedly, across phases and models: **given a window image and a
plain-language description, how often does the vision tier land a point inside the element's real
bounds, and when it misses, by how far.**

This is not `eval/`. That harness runs whole goals through the real `run_task()` against live
websites and asserts on the agent's prose. This one makes a single model call per target against a
fixed image and asserts on geometry. Nothing here touches `run_task`, the agent, `TaskManager`, or
the DB.

```
venv\Scripts\python.exe -m benchmarks.grounding_bench --dry-run           # no spend; proves the pipeline
venv\Scripts\python.exe -m benchmarks.grounding_bench                     # live, every arm
venv\Scripts\python.exe -m benchmarks.grounding_bench --arm set_of_mark   # one arm
venv\Scripts\python.exe -m benchmarks.grounding_bench --scene dense_grid  # one scene
venv\Scripts\python.exe -m benchmarks.grounding_bench --dump-fixtures out # look at the images
```

## The original spike's dataset is gone, and that is why this exists

The VISION TIER comment block in `orbit/mcp_servers/perception_tools.py` reports 76% over 46 targets
on 10 screenshots. **Those inputs were never checked in** — the block says so explicitly, because
they captured a real desktop as it happened to be. Confirmed absent: no image files anywhere outside
`venv/`, nothing in `automation_spikes/`, and nothing matching in any commit in git history.

So that 76% is a historical note. It is **not** a baseline, and no number this harness produces
should be compared against it. What this harness measures instead is arms against each other on
identical inputs, which is the comparison every phase after Phase 0 actually needs.

## The fixtures are synthetic, deliberately, and that has a cost

Scenes are drawn in code (`fixtures.py`) rather than screenshotted. Read the module docstring there
for the full trade; the short version is that it buys exact ground truth (the generator draws at the
bounds it records, so labelling error is zero), byte-identical reproducibility, no desktop content
to leak, and a fixture set that can live in git — at the cost of real Windows chrome.

**Consequence, and the rule that follows: absolute hit-rates from this harness are not field
accuracy.** A model that has seen a million real screenshots may ground better on a real Notepad
than on a drawn one. Quote results as "arm X beat arm Y by N points on the synthetic set", never as
"Orbit's vision tier is N% accurate".

The `custom_drawn` scene is the exception where synthetic is arguably *more* representative: a
`<canvas>` app or a game is arbitrary drawn pixels with no UIA representation either way. That is
also the column that justifies the vision tier existing at all — there, the alternative to a vision
answer is not a worse answer, it is no answer.

Categories (`native_like` / `dense` / `custom_drawn` / `adversarial`) mirror the original spike's
split so the *shape* of the report stays comparable even though the numbers are not.

## What it reuses from the real tool, and why that is load-bearing

`grounding_bench.py` imports `_parse_vision_reply`, `_vision_point_to_screen`,
`_fit_for_inline_upload`, `_nim_api_key` and `_VISION_MODEL` from `perception_tools.py` rather than
reimplementing them. A benchmark that reimplements what it benchmarks measures the reimplementation
— it would report a healthy number while the real parser rejected every reply.

As of Phase 2 that extends to the whole set-of-mark path: `benchmarks/overlay.py` imports the
**prompt** (`_VISION_SOM_PROMPT`), the **index parser** (`_parse_index_reply`) and the **renderer**
(`mark_overlay.draw_marks`) from production rather than carrying copies, and `benchmarks/raster.py`
takes its digit glyphs from the same font table. A benchmark that marks its images with a different
renderer, or scores replies with a different parser, is measuring something the tool does not do —
and would keep happily scoring the old prompt after the tool's prompt changed, which defeats the one
question the harness exists to answer.

The dependency runs **benchmark → production, never the reverse**. Nothing under `orbit/` imports
from `benchmarks/`.

What is **not** exercised is the live capture step (an `mss` grab of a real window rect), because
fixtures are images rather than windows. That path has its own coverage in
`tests/test_perception_tools.py`.

**The synthetic crop origin is not decoration.** Fixtures are scored in "screen" space: ground truth
is offset by `_CROP_ORIGIN`, and answers come back through the real inverse transform. If the
crop-undo step ever breaks, every answer shifts by a constant and the hit-rate collapses here —
which is precisely the failure the VISION TIER notes call out as "plausible-looking, and far harder
to notice than an obviously wrong answer".

## Set-of-Mark numbers are a CEILING, not an accuracy claim

`select_candidates` always includes the correct element. That makes the SoM arm answer the question
"*if candidate generation were perfect*, how well does the model choose?" — deliberately, so that
grounding (the model's job) stays separable from candidate generation (Phase 1's job). A candidate
set that omitted the answer would make the target unanswerable and would score candidate generation
under a grounding heading.

Read SoM results accordingly. The realistic end-to-end number is bounded above by this and by
whatever Phase 1's generator achieves; it is not this number.

Distractors are chosen deterministically and deliberately hard — half are the target's nearest
neighbours, because the spike's dense-UI misses were near-misses onto small adjacent controls, and
half are strided across the scene so the overlay resembles what a real generator would propose.
`test_answer_index_is_not_always_the_same_number` exists because if the correct index were always 1,
a model that always answers "1" would score 100% and the arm would look like a breakthrough.

## Errors count as misses

Following the spike's own convention ("counted as misses below rather than quietly dropped"), a
provider 504 or an unparseable reply scores zero. The summary reports `errors` and `unparsed`
separately so an arm losing to provider flakiness stays distinguishable from an arm losing to bad
grounding — but neither is excluded from the denominator, because an arm that only works two thirds
of the time is worse than one that always works.

## Run it serially. Concurrency does not work on this endpoint.

**Measured 2026-08-22, and the reason `concurrency: 1` is the default:** at `--concurrency 4`, 23 of
24 calls failed outright — 15 `Nvidia_nimException - Connection error`, 8 request timeouts. The one
that survived took 757s. Re-run serially, the identical request succeeded in 540s and returned a
correct answer in the documented format. So parallelism here does not buy throughput, it buys a page
of connection errors. Do not raise it without re-measuring.

**Budget the wall clock accordingly.** One call is currently ~9 minutes — this tier is running at
roughly the spike's *p90* (640s) for every call, against the spike's median of 54.6s. The full
12-target set across two arms is 24 calls, so a complete run is a multi-hour, unattended job. Use
`--max-targets-per-scene 1` for a directional read (~an hour, one target from each category) before
committing to a full run.

Latency figures are queueing, not compute: the spike saw the same image and prompt return in 15.5s
and 158.8s. Treat them as "this is not a fast call" and nothing more precise, and only compare
latency across arms from runs at the same concurrency.

## Transient provider errors are retried; they are still misses if they persist

`max_retries` / `retry_backoff_s` in the config exist because of the run above: without retries, a
bad afternoon on NVIDIA's shared tier gets reported as a 4% grounding hit-rate. Retrying does not
soften scoring — a reply that arrives and is wrong is a miss, and an error that survives every
attempt is a miss. It only stops the benchmark from attributing the provider's connection errors to
the model.

`_is_transient` is deliberately narrow. An auth failure or a retired model string must fail fast and
loudly rather than being retried three times and reported as a grounding result; the names it does
match are the ones this endpoint actually produced, not a guess at the space of possible errors.

## No dependencies were added

`raster.py` draws into a flat `bytearray` and encodes with `mss.tools.to_png` — the encoder the
perception tier already uses. Pillow is still not a dependency of this project, for the same reason
`perception_tools._box_downscale` is hand-rolled: a benchmark is an even worse reason to add one
than the vision tier was.

The 5x7 bitmap font exists because the model has to *read* labels — "the EDIT menu" is only a fair
target if EDIT is legible. Glyphs render at integer scale so edges stay crisp; a fractionally-scaled
glyph would be a different and unrepresentative reading task.

## Adding a scene or a target

Ground truth comes from the generator, so a target is correct by construction — but only if the
description has exactly one defensible answer on screen. **Look at the image before trusting a new
target** (`--dump-fixtures`). That check has already earned itself once: the mixer's transport button
was red, which gave "the red circular knob" two reasonable answers and would have scored the model
wrong for being right. It is amber now.

Config lives in `orbit/config/vision_benchmark.yaml`, read at call time by `config.py` — arms, mark
count, timeout, scene subset. Adding a model comparison means adding an arm there, not editing the
runner.

## Results

One timestamped JSON per run under `benchmarks/results/` (gitignored — they are run artifacts, and
they embed model replies). Phases are compared by diffing two of those files, which is why runs
never overwrite each other.
