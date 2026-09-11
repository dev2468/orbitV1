"""Two-track benchmark — the evidence behind the acknowledgement track.

Two questions, two modes:

  classify   How often does the acknowledgement's [CHAT]/[TASK] marker route
             a turn correctly, and how often does it make the one mistake the
             design cannot afford — calling a real task "chat", which means a
             request silently does nothing? Every utterance in
             `benchmarks/ack_utterances.json` is sent `--repeats` times,
             concurrently. Latencies from this mode are recorded but NOT
             meant for reporting: concurrent calls contend with each other.

  pipeline   How long until the user hears something, and how long until the
             answer, when the two tracks run the way the GUI runs them? Drives
             the real `orbit.run_task --serve` worker plus the real
             acknowledgement and text-to-speech calls, one turn at a time, and
             mirrors gui/main.py's deferred dispatch: the work track is sent
             the moment the acknowledgement classifies TASK, never for CHAT,
             and regardless once the dispatch guard expires.

Production code is imported, never copied — `ack.stream_ack`,
`ack.MarkerSplitter`, `ack.recent_context`, the TTS voice and sample rate —
for the reason benchmarks/CLAUDE.md gives: a benchmark of a reimplementation
measures the reimplementation.

What this does NOT measure: speech-to-text, the fixed auto-submit delay that
precedes submission on the voice path (`_AUTO_SUBMIT_MS`, 0.9s), and audio
device output latency (the GUI holds a prewarmed output stream). Every time is
measured from submission — the moment gui/main.py's `_submit_task` would run.

Rows the pipeline creates in data/orbit.db carry a conversation_id starting
`CONV-bench-`, so an analysis of that log can exclude them.

    venv\\Scripts\\python.exe -m benchmarks.two_track_bench classify
    venv\\Scripts\\python.exe -m benchmarks.two_track_bench pipeline

Both write `benchmarks/results/two_track_<mode>_<stamp>.{json,csv}`
(gitignored, like every other run artifact there).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import queue
import random
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from orbit import ack  # noqa: E402  (needs the .env loaded first)
from orbit.models import DEFAULT_MODEL  # noqa: E402

RESULTS_DIR = ROOT / "benchmarks" / "results"
UTTERANCES = ROOT / "benchmarks" / "ack_utterances.json"
EVENT_PREFIX = "[ORBIT]"

# The pipeline's goals. Local and deterministic on purpose: no browsing, so a
# Playwright cold start (~60s) or a slow website cannot masquerade as agent
# latency. Nothing here reads personal data — the workspace listing touches
# only the agent's own sandbox, and only its length is recorded.
CHAT_GOALS = [
    "hi",
    "thanks, that was great",
    "good morning orbit",
    "how are you doing today?",
    "what's your name?",
]
TASK_GOALS = [
    "what's 17 times 23?",
    "what's the capital of Australia?",
    "list the files in my workspace folder",
]
CONVERSATION_TURNS = [
    "what's 12 squared?",
    "and what is it cubed?",
    "now add 10 to that",
]


# --- small shared helpers ---------------------------------------------------


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 3)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[Optional[float], Optional[float]]:
    """95% Wilson score interval for k successes in n trials."""
    if n == 0:
        return (None, None)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 6), round(min(1.0, centre + half), 6))


def describe(values: list[Optional[float]]) -> dict[str, Any]:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0}
    arr = np.array(vals, dtype=float)
    return {
        "n": len(vals),
        "median": round(float(np.median(arr)), 3),
        "p10": round(float(np.percentile(arr, 10)), 3),
        "p90": round(float(np.percentile(arr, 90)), 3),
        "mean": round(float(arr.mean()), 3),
        "min": round(float(arr.min()), 3),
        "max": round(float(arr.max()), 3),
    }


def _meta(mode: str, **extra: Any) -> dict[str, Any]:
    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    return {
        "mode": mode,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git("rev-parse", "--short", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "ack_model": ack._resolve_model(None),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        **extra,
    }


def _write(mode: str, stamp: str, meta: dict, summary: dict, rows: list[dict]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    base = RESULTS_DIR / f"two_track_{mode}_{stamp}"
    Path(f"{base}.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "rows": rows}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    keys = sorted({k for row in rows for k in row})
    with open(f"{base}.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    return base


# --- classify ---------------------------------------------------------------


def _classify_once(text: str, client: httpx.Client) -> dict[str, Any]:
    """One acknowledgement, routed exactly as the GUI routes it.

    Routing is the GUI's rule, not a stricter one: a reply that never carries a
    marker, or a call that fails, is TASK. `marker` records separately whether
    the model actually complied, so "correct because of the fail-safe" stays
    distinguishable from "correct because the model said so".
    """
    splitter = ack.MarkerSplitter()
    raw: list[str] = []
    visible: list[str] = []
    error = None
    t0 = time.perf_counter()
    t_first = t_classified = None
    try:
        for piece in ack.stream_ack(text, client=client):
            now = time.perf_counter() - t0
            if t_first is None:
                t_first = now
            raw.append(piece)
            out = splitter.feed(piece)
            if t_classified is None and splitter.chat_only is not None:
                t_classified = now
            if out:
                visible.append(out)
        tail = splitter.flush()
        if tail:
            visible.append(tail)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"[:300]
    head = "".join(raw).lstrip().upper()
    if head.startswith(ack.MARKER_CHAT):
        marker = "CHAT"
    elif head.startswith(ack.MARKER_TASK):
        marker = "TASK"
    else:
        marker = None
    return {
        "routed": "CHAT" if splitter.chat_only else "TASK",
        "marker": marker,
        "error": error,
        "t_first_token": _r(t_first),
        "t_classified": _r(t_classified),
        "t_done": _r(time.perf_counter() - t0),
        "reply": "".join(visible).strip(),
    }


def _summarize_classify(rows: list[dict], repeats: int) -> dict[str, Any]:
    labelled = [r for r in rows if r["label"] in ("CHAT", "TASK")]
    true_task = [r for r in labelled if r["label"] == "TASK"]
    true_chat = [r for r in labelled if r["label"] == "CHAT"]
    correct = sum(r["correct"] for r in labelled)
    false_chat = sum(r["routed"] == "CHAT" for r in true_task)
    false_task = sum(r["routed"] == "TASK" for r in true_chat)

    by_item: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_item[r["id"]].append(r)
    items = []
    for item_id, rs in by_item.items():
        routes = [x["routed"] for x in rs]
        # Ties go to TASK — the same asymmetry the product uses.
        majority = "TASK" if routes.count("TASK") * 2 >= len(routes) else "CHAT"
        items.append({
            "id": item_id, "label": rs[0]["label"], "category": rs[0]["category"],
            "text": rs[0]["text"], "majority": majority,
            "task_votes": routes.count("TASK"), "votes": len(routes),
            "unanimous": len(set(routes)) == 1,
        })
    labelled_items = [i for i in items if i["label"] in ("CHAT", "TASK")]
    item_task = [i for i in labelled_items if i["label"] == "TASK"]
    item_chat = [i for i in labelled_items if i["label"] == "CHAT"]
    item_false_chat = sum(i["majority"] == "CHAT" for i in item_task)
    item_false_task = sum(i["majority"] == "TASK" for i in item_chat)
    item_correct = len(labelled_items) - item_false_chat - item_false_task

    categories: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "correct": 0})
    for r in labelled:
        categories[r["category"]]["n"] += 1
        categories[r["category"]]["correct"] += int(r["correct"])

    ambiguous = [r for r in rows if r["label"] == "AMBIGUOUS"]
    amb_task = sum(r["routed"] == "TASK" for r in ambiguous)

    return {
        "repeats": repeats,
        "calls": len(rows),
        "items": len(items),
        "call_level": {
            "n": len(labelled),
            "accuracy": round(correct / len(labelled), 6) if labelled else None,
            "accuracy_ci95": wilson(correct, len(labelled)),
            "confusion": {
                "TASK": {"TASK": len(true_task) - false_chat, "CHAT": false_chat},
                "CHAT": {"CHAT": len(true_chat) - false_task, "TASK": false_task},
            },
            "false_chat_rate": round(false_chat / len(true_task), 6) if true_task else None,
            "false_chat_ci95": wilson(false_chat, len(true_task)),
            "false_task_rate": round(false_task / len(true_chat), 6) if true_chat else None,
            "false_task_ci95": wilson(false_task, len(true_chat)),
        },
        "item_level_majority": {
            "n": len(labelled_items),
            "accuracy": round(item_correct / len(labelled_items), 6) if labelled_items else None,
            "accuracy_ci95": wilson(item_correct, len(labelled_items)),
            "false_chat": item_false_chat, "of_task_items": len(item_task),
            "false_chat_ci95": wilson(item_false_chat, len(item_task)),
            "false_task": item_false_task, "of_chat_items": len(item_chat),
            "false_task_ci95": wilson(item_false_task, len(item_chat)),
            "unanimous_items": sum(i["unanimous"] for i in items),
        },
        "marker_compliance": {
            "with_marker": sum(r["marker"] is not None for r in rows),
            "of_calls": len(rows),
            "errors": sum(r["error"] is not None for r in rows),
        },
        "per_category": {
            c: {**v, "accuracy": round(v["correct"] / v["n"], 6)}
            for c, v in sorted(categories.items())
        },
        "ambiguous": {
            "calls": len(ambiguous),
            "routed_task": amb_task,
            "routed_task_share": round(amb_task / len(ambiguous), 6) if ambiguous else None,
        },
        "false_chat_items": sorted(
            [
                {"id": i["id"], "text": i["text"], "chat_votes": i["votes"] - i["task_votes"],
                 "votes": i["votes"]}
                for i in item_task if i["task_votes"] < i["votes"]
            ],
            key=lambda d: -d["chat_votes"],
        ),
        "false_task_items": sorted(
            [
                {"id": i["id"], "text": i["text"], "task_votes": i["task_votes"],
                 "votes": i["votes"]}
                for i in item_chat if i["task_votes"] > 0
            ],
            key=lambda d: -d["task_votes"],
        ),
        "decision_latency_under_concurrency_s": describe([r["t_classified"] for r in rows]),
    }


def run_classify(repeats: int, concurrency: int, seed: int) -> Path:
    data = json.loads(UTTERANCES.read_text(encoding="utf-8"))
    items = data["items"]
    calls = [(item, rep) for item in items for rep in range(repeats)]
    random.Random(seed).shuffle(calls)

    local = threading.local()
    clients: list[httpx.Client] = []
    clients_lock = threading.Lock()

    def client() -> httpx.Client:
        c = getattr(local, "client", None)
        if c is None:
            c = httpx.Client(timeout=20.0)
            local.client = c
            with clients_lock:
                clients.append(c)
        return c

    def one(call: tuple[dict, int]) -> dict[str, Any]:
        item, rep = call
        row = _classify_once(item["text"], client())
        row.update(
            id=item["id"], text=item["text"], label=item["label"],
            category=item["category"], style=item["style"], repeat=rep,
        )
        row["correct"] = (
            row["routed"] == item["label"] if item["label"] in ("CHAT", "TASK") else None
        )
        return row

    stamp = _stamp()
    rows: list[dict] = []
    print(f"classify: {len(items)} utterances x {repeats} repeats = {len(calls)} calls, "
          f"concurrency {concurrency}, ack model {ack._resolve_model(None)}", flush=True)
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(one, call) for call in calls]
            for n, fut in enumerate(as_completed(futures), 1):
                rows.append(fut.result())
                if n % 50 == 0:
                    print(f"  {n}/{len(calls)}", flush=True)
    finally:
        for c in clients:
            c.close()
        rows.sort(key=lambda r: (r["id"], r["repeat"]))
        summary = _summarize_classify(rows, repeats) if rows else {}
        meta = _meta("classify", repeats=repeats, concurrency=concurrency, seed=seed,
                     utterance_file=str(UTTERANCES.relative_to(ROOT)),
                     utterance_version=data.get("version"))
        base = _write("classify", stamp, meta, summary, rows)
    print(json.dumps({k: summary[k] for k in (
        "call_level", "item_level_majority", "marker_compliance", "ambiguous")}, indent=2))
    print(f"wrote {base}.json / .csv")
    return base


# --- pipeline ---------------------------------------------------------------


def _gui_dispatch_guard_s() -> float:
    """The GUI's own constant, read from its source rather than copied.

    Importing gui.main would stand up PySide6 and the whole window module for
    one integer; a regex over the file keeps this benchmark from quietly
    measuring 3s after the GUI has moved on.
    """
    src = (ROOT / "gui" / "main.py").read_text(encoding="utf-8")
    m = re.search(r"^_DISPATCH_GUARD_MS\s*=\s*(\d[\d_]*)", src, re.M)
    return int(m.group(1).replace("_", "")) / 1000 if m else 3.0


class _Worker:
    """The real warm worker, spoken to the way gui/main.py speaks to it."""

    def __init__(self, log_path: Path) -> None:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        self._log = open(log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "orbit.run_task", "--serve"],
            cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._log, text=True, encoding="utf-8", errors="replace",
            bufsize=1, env=env,
        )
        self.lines: "queue.Queue[tuple[float, Optional[str]]]" = queue.Queue()
        self._send_lock = threading.Lock()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put((time.perf_counter(), line.rstrip("\r\n")))
        self.lines.put((time.perf_counter(), None))

    def send(self, obj: dict) -> None:
        with self._send_lock:
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    def next_line(self, deadline: float) -> tuple[float, Optional[str]]:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("worker produced nothing before the deadline")
        return self.lines.get(timeout=remaining)

    def close(self) -> None:
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._log.close()


class _Tts:
    """Deepgram Aura, with the voice and sample rate gui/speech.py uses."""

    def __init__(self) -> None:
        from deepgram import DeepgramClient
        from gui.speech import _DEFAULT_VOICE, _SAMPLE_RATE

        self.voice = os.environ.get("ORBIT_TTS_VOICE", "").strip() or _DEFAULT_VOICE
        self.rate = _SAMPLE_RATE
        self.client = DeepgramClient(api_key=os.environ.get("DEEPGRAM_API_KEY", ""))
        self.chars = 0

    def synth(self, text: str) -> tuple[Optional[float], float, int]:
        first = None
        nbytes = 0
        for chunk in self.client.speak.v1.audio.generate(
            text=text, model=self.voice, encoding="linear16",
            sample_rate=self.rate, container="none",
        ):
            if chunk and first is None:
                first = time.perf_counter()
            nbytes += len(chunk)
        self.chars += len(text)
        return first, time.perf_counter(), nbytes


def _collect_task(worker: _Worker, t0: float, rec: dict, timeout: float = 240.0) -> None:
    """Read worker output until this task's [TASK:DONE], stamping events."""
    deadline = time.perf_counter() + timeout
    rec.setdefault("n_tool_calls", 0)
    while True:
        ts, line = worker.next_line(deadline)
        if line is None:
            raise RuntimeError("the worker exited mid-task; see the worker log")
        rel = ts - t0
        if line.startswith(EVENT_PREFIX):
            try:
                ev = json.loads(line[len(EVENT_PREFIX):])
            except ValueError:
                continue
            kind = ev.get("type") or ev.get("kind") or ev.get("event")
            if kind == "tool_call":
                rec["n_tool_calls"] += 1
                rec.setdefault("t_first_tool", _r(rel))
            elif kind == "text_delta":
                rec.setdefault("t_first_text", _r(rel))
            elif kind == "result":
                rec["t_answer"] = _r(rel)
                rec["status"] = ev.get("status")
                # Length only: the answer can quote workspace file names.
                rec["result_chars"] = len(ev.get("text") or "")
        elif line.startswith("[TASK:DONE"):
            rec["t_task_done"] = _r(rel)
            return


def _close_conversation(worker: _Worker, conversation_id: str, timeout: float = 90.0) -> None:
    worker.send({"close_conversation": conversation_id})
    deadline = time.perf_counter() + timeout
    while True:
        _, line = worker.next_line(deadline)
        if line is None:
            raise RuntimeError("the worker exited while closing a conversation")
        if line.startswith("[conversation] closed") and conversation_id in line:
            return


def _work_request(goal: str, conversation_id: str, work_model: str) -> dict:
    # effort "low" is the GUI's default; lane headless is its default lane.
    return {"goal": goal, "lane": "headless", "effort": "low",
            "model": work_model, "conversation_id": conversation_id}


def _two_track_turn(
    worker: _Worker, client: httpx.Client, tts: Optional[_Tts], goal: str,
    conversation_id: str, guard_s: float, work_model: str,
) -> dict[str, Any]:
    """One turn with both tracks, dispatched the way gui/main.py dispatches.

    t0 is submission. The acknowledgement starts immediately; the work request
    is written to the worker when the marker says TASK, when the reply ends
    with no marker, when the acknowledgement fails, or when the guard expires —
    whichever comes first, exactly once. A CHAT verdict cancels the guard, and
    a guard that already fired is not taken back (the GUI does not either).
    Speech starts when the acknowledgement completes, as `_on_ack_completed`
    enqueues it.
    """
    rec: dict[str, Any] = {"goal": goal, "conversation_id": conversation_id}
    lock = threading.Lock()
    state = {"dispatched": False}
    t0 = time.perf_counter()

    def dispatch(reason: str) -> None:
        with lock:
            if state["dispatched"]:
                return
            state["dispatched"] = True
        rec["t_dispatch"] = _r(time.perf_counter() - t0)
        rec["dispatch_reason"] = reason
        worker.send(_work_request(goal, conversation_id, work_model))

    guard = threading.Timer(guard_s, dispatch, args=("guard",))
    guard.daemon = True
    guard.start()

    splitter = ack.MarkerSplitter()
    visible: list[str] = []
    try:
        history = ack.recent_context(conversation_id)
        for piece in ack.stream_ack(goal, history=history, client=client):
            now = time.perf_counter() - t0
            rec.setdefault("t_ack_first_token", _r(now))
            out = splitter.feed(piece)
            if "t_classified" not in rec and splitter.chat_only is not None:
                rec["t_classified"] = _r(now)
                rec["routed"] = "CHAT" if splitter.chat_only else "TASK"
                if splitter.chat_only:
                    guard.cancel()
                else:
                    dispatch("classified")
            if out:
                visible.append(out)
        tail = splitter.flush()
        if tail:
            visible.append(tail)
    except Exception as exc:  # noqa: BLE001
        rec["ack_error"] = f"{type(exc).__name__}: {exc}"[:300]
    if "t_classified" not in rec:
        rec["t_classified"] = _r(time.perf_counter() - t0)
        rec["routed"] = "TASK"
        dispatch("ack_failed" if "ack_error" in rec else "no_marker")
    rec["t_ack_done"] = _r(time.perf_counter() - t0)
    ack_text = "".join(visible).strip()
    rec["ack_text"] = ack_text

    speaker = None
    if tts is not None and ack_text:
        def speak() -> None:
            try:
                first, done, nbytes = tts.synth(ack_text)
                rec["t_first_audio"] = _r(first - t0) if first else None
                rec["t_tts_done"] = _r(done - t0)
                rec["audio_s"] = _r(nbytes / (2 * tts.rate))
            except Exception as exc:  # noqa: BLE001
                rec["tts_error"] = f"{type(exc).__name__}: {exc}"[:300]

        speaker = threading.Thread(target=speak, daemon=True)
        speaker.start()

    if state["dispatched"]:
        _collect_task(worker, t0, rec)
    if speaker is not None:
        speaker.join(timeout=30)
    guard.cancel()
    return rec


def _single_track_turn(worker: _Worker, goal: str, conversation_id: str,
                       work_model: str) -> dict[str, Any]:
    """The counterfactual: no acknowledgement, the agent answers everything."""
    rec: dict[str, Any] = {"goal": goal, "conversation_id": conversation_id,
                           "t_dispatch": 0.0, "dispatch_reason": "single_track"}
    t0 = time.perf_counter()
    worker.send(_work_request(goal, conversation_id, work_model))
    _collect_task(worker, t0, rec)
    return rec


def _summarize_pipeline(rows: list[dict]) -> dict[str, Any]:
    def pick(cond: str) -> list[dict]:
        return [r for r in rows if r.get("condition") == cond and not r.get("failed")]

    chat, task = pick("two_track_chat"), pick("two_track_task")
    single, conv = pick("single_track_chat"), pick("conversation")
    col = lambda rs, k: [r.get(k) for r in rs]  # noqa: E731
    gap = [
        r["t_answer"] - r["t_first_audio"]
        for r in task + conv
        if r.get("t_answer") is not None and r.get("t_first_audio") is not None
    ]
    return {
        "two_track_chat": {
            "n": len(chat),
            "routed_chat": sum(r.get("routed") == "CHAT" for r in chat),
            "work_dispatched": sum(r.get("t_dispatch") is not None for r in chat),
            "t_classified": describe(col(chat, "t_classified")),
            "t_ack_done": describe(col(chat, "t_ack_done")),
            "t_first_audio": describe(col(chat, "t_first_audio")),
        },
        "two_track_task": {
            "n": len(task),
            "misrouted_chat": sum(r.get("routed") == "CHAT" for r in task),
            "dispatch_reasons": dict(Counter(r.get("dispatch_reason") for r in task)),
            "status": dict(Counter(r.get("status") for r in task)),
            "t_classified": describe(col(task, "t_classified")),
            "t_dispatch": describe(col(task, "t_dispatch")),
            "t_ack_done": describe(col(task, "t_ack_done")),
            "t_first_audio": describe(col(task, "t_first_audio")),
            "t_first_tool": describe(col(task, "t_first_tool")),
            "t_first_text": describe(col(task, "t_first_text")),
            "t_answer": describe(col(task, "t_answer")),
            "tool_calls": describe(col(task, "n_tool_calls")),
        },
        "single_track_chat": {
            "n": len(single),
            "status": dict(Counter(r.get("status") for r in single)),
            "t_answer": describe(col(single, "t_answer")),
        },
        "conversation": {
            "first_turn_t_answer": describe([r.get("t_answer") for r in conv if r.get("turn") == 0]),
            "followup_t_answer": describe([r.get("t_answer") for r in conv if (r.get("turn") or 0) > 0]),
            "t_first_audio": describe(col(conv, "t_first_audio")),
            "misrouted_chat": sum(r.get("routed") == "CHAT" for r in conv),
        },
        "silence_covered_s": describe(gap),
        "failed_trials": sum(bool(r.get("failed")) for r in rows),
    }


def run_pipeline(reps: int) -> Path:
    stamp = _stamp()
    guard_s = _gui_dispatch_guard_s()
    work_model = DEFAULT_MODEL
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tts = _Tts() if os.environ.get("DEEPGRAM_API_KEY") else None
    client = httpx.Client(timeout=20.0)
    print(f"pipeline: {reps} reps, guard {guard_s}s, ack {ack._resolve_model(None)}, "
          f"work {work_model}, tts {tts.voice if tts else 'OFF (no DEEPGRAM_API_KEY)'}", flush=True)

    # Prewarm exactly what the GUI prewarms at startup, so the first measured
    # turn does not pay one-off TLS and connection-pool setup.
    try:
        client.get("https://openrouter.ai/api/v1/models", timeout=5.0)
    except Exception:  # noqa: BLE001
        pass
    if tts is not None:
        try:
            tts.synth(".")
        except Exception:  # noqa: BLE001
            pass

    worker = _Worker(RESULTS_DIR / f"two_track_worker_{stamp}.log")
    rows: list[dict] = []

    def record(rec: dict, **tags: Any) -> dict:
        rec.update(tags)
        rows.append(rec)
        return rec

    def safely(fn, *args, **tags) -> Optional[dict]:
        try:
            return record(fn(*args), **tags)
        except Exception as exc:  # noqa: BLE001
            print(f"  trial failed ({tags}): {type(exc).__name__}: {exc}", flush=True)
            record({"failed": f"{type(exc).__name__}: {exc}"[:300]}, **tags)
            return None

    try:
        # The first LiteLLM call in a fresh process pays a one-off warm-up
        # (~18s). The GUI pays it once per session, so it is recorded here
        # and kept out of every statistic.
        conv = f"CONV-bench-{stamp}-warmup"
        safely(_single_track_turn, worker, "hello", conv, work_model, condition="warmup")
        _close_conversation(worker, conv)

        rotation = 0
        for rep in range(reps):
            for i, goal in enumerate(CHAT_GOALS):
                conv = f"CONV-bench-{stamp}-c{rep}{i}"
                rec = safely(_two_track_turn, worker, client, tts, goal, conv, guard_s,
                             work_model, condition="two_track_chat", rep=rep)
                if rec and rec.get("t_dispatch") is not None:
                    _close_conversation(worker, conv)
            for i, goal in enumerate(TASK_GOALS):
                conv = f"CONV-bench-{stamp}-t{rep}{i}"
                rec = safely(_two_track_turn, worker, client, tts, goal, conv, guard_s,
                             work_model, condition="two_track_task", rep=rep)
                if rec and rec.get("t_dispatch") is not None:
                    _close_conversation(worker, conv)
            for k in range(2):
                goal = CHAT_GOALS[rotation % len(CHAT_GOALS)]
                rotation += 1
                conv = f"CONV-bench-{stamp}-s{rep}{k}"
                safely(_single_track_turn, worker, goal, conv, work_model,
                       condition="single_track_chat", rep=rep)
                _close_conversation(worker, conv)
            conv = f"CONV-bench-{stamp}-v{rep}"
            for turn, goal in enumerate(CONVERSATION_TURNS):
                safely(_two_track_turn, worker, client, tts, goal, conv, guard_s,
                       work_model, condition="conversation", rep=rep, turn=turn)
            _close_conversation(worker, conv)
            print(f"  rep {rep + 1}/{reps} done ({len(rows)} rows)", flush=True)
    finally:
        worker.close()
        client.close()
        summary = _summarize_pipeline(rows)
        meta = _meta("pipeline", reps=reps, guard_s=guard_s, work_model=work_model,
                     tts_voice=tts.voice if tts else None,
                     tts_chars=tts.chars if tts else 0,
                     conversation_id_prefix=f"CONV-bench-{stamp}-")
        base = _write("pipeline", stamp, meta, summary, rows)
    print(json.dumps(summary, indent=2))
    print(f"wrote {base}.json / .csv")
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("classify", help="[CHAT]/[TASK] routing accuracy")
    c.add_argument("--repeats", type=int, default=3)
    c.add_argument("--concurrency", type=int, default=6)
    c.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("pipeline", help="two-track latency through the real worker")
    p.add_argument("--reps", type=int, default=6)
    args = parser.parse_args()
    if args.mode == "classify":
        run_classify(args.repeats, args.concurrency, args.seed)
    else:
        run_pipeline(args.reps)


if __name__ == "__main__":
    main()
