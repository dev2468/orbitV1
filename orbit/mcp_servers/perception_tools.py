"""Tool implementations for the `screen-perception` MCP server — Prompt 2
of "Claude Code Prompts - Building the MCP Tool Layer.md". Kept separate
from perception_server.py (the FastMCP process entry point) so these are
directly unit-testable without going through the MCP stdio transport, same
split as the other *_tools.py modules.

Read-only by design (Section 11): "structured observer free and always-on,
mid-tier perception cheap, visual observer on-demand only." Every tool
here observes; none of them simulate input — that's windows-control's job,
structurally separate (Section 11 / this catalog's own framing) even
though the two servers share UIA resolution code via uia_resolver.py.

Scope note (honest, not hidden — same pattern as browser_policy_tools.py's
and windows_control_tools.py's own scope notes): this module builds the
tools that don't need anything beyond what's already installed
(pywinauto/pywin32, already required by windows-control, plus `mss` for
screenshots — a small, pure-Python, no-system-binary addition). One tool
from the catalog is still NOT built:

  - perception_read_text_region (OCR). The catalog names PaddleOCR; that
    (or any OCR engine — Tesseract needs a separate system-level binary
    install, easyocr/PaddleOCR pull in a multi-hundred-MB ML stack) is not
    installed in this environment, and installing one unattended is a
    bigger, more consequential call than adding a small pure-Python
    library — the kind of thing to confirm rather than assume. Calling
    this tool today would mean faking OCR output, which is worse than not
    having it. It is not stubbed with placeholder output — it is simply
    absent from this server's tool set, the same way browser_click/
    browser_type were honestly left out of the first browser-policy pass.

perception_vision_locate, the catalog's other previously-absent tool, IS
now built. The catalog required a grounding spike first ("the
representation decision belongs in the spike's output, not guessed here");
that spike has now been run against real screenshots captured on this
machine. Its measured outcome, and the representation decision it
produced, are recorded in the VISION TIER comment block further down this
file — next to the code they justify, not in a separate document.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import mss
import mss.tools
import win32gui

from orbit import db
from orbit.mcp_servers.candidate_source import generate_candidates
from orbit.mcp_servers.mark_overlay import draw_marks
from orbit.mcp_servers.uia_resolver import get_uia_tree, resolve_uia_element, window_snapshot
from orbit.policy import load_perception_policy
from orbit.task_manager import CancellationToken
from orbit.tools.element_ref import ElementRef
from orbit.tools.foundation import BaseTool, ClassifiedToolError, Confidence, ToolMetadata

_LOW_HEADLESS = dict(
    risk_tier="low",
    lane="headless",
    requires_confirmation=False,
    is_destructive=False,
    returns_untrusted_content=False,
)

_FALLBACK_TASK_ID = "adhoc-perception-server"
_ORBIT_TASK_ID = os.environ.get("ORBIT_TASK_ID", "").strip()


def _resolve_task_id(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    if _ORBIT_TASK_ID:
        return _ORBIT_TASK_ID
    if db.get_task(_FALLBACK_TASK_ID) is None:
        db.create_task("perception server adhoc calls", task_id=_FALLBACK_TASK_ID)
    return _FALLBACK_TASK_ID


def _foreground_or_given(window_handle: Optional[int]) -> int:
    if window_handle is not None:
        return window_handle
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        raise ClassifiedToolError("state_failure", "no window currently has foreground focus")
    return hwnd


class GetStateTool(BaseTool):
    """perception_get_state — the structured observer: active window
    title, foreground process, and (if task_id is given) that task's
    status. No model call, no vision, effectively free — this is what
    answers "what's on my screen" without spending anything."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        hwnd = win32gui.GetForegroundWindow()
        foreground = window_snapshot(hwnd) if hwnd else None

        task_status = None
        query_task_id = args.get("task_id")
        if query_task_id:
            task = db.get_task(query_task_id)
            task_status = task["status"] if task else None

        return {"foreground_window": foreground, "task_status": task_status}, Confidence.API_SUCCESS


class GetUiaTreeTool(BaseTool):
    """perception_get_uia_tree — the tier that "answers most questions for
    free" (per the build plan): most native apps expose full structure
    through UIA without needing OCR or vision. Depth/node-capped (see
    uia_resolver.get_uia_tree) so one call can't flood the model's context
    with an entire app's control tree."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        window_handle = _foreground_or_given(args.get("window_handle"))
        max_depth = args.get("max_depth", 6)
        max_nodes = args.get("max_nodes", 200)
        nodes = get_uia_tree(window_handle, max_depth=max_depth, max_nodes=max_nodes)
        return (
            {"window_handle": window_handle, "nodes": nodes, "truncated": len(nodes) >= max_nodes},
            Confidence.API_SUCCESS,
        )


class FindElementTool(BaseTool):
    """perception_find_element — the unified resolution entry point named
    explicitly in the build plan: tries tiers in order, stopping at the
    first one that produces a match.

    TWO of the three tiers are real in this build: "uia" and "vision".
    "ocr" remains genuinely unimplemented (no OCR engine is installed — see
    this module's docstring) and is still reported back in
    tiers_unavailable rather than silently skipped, so a caller can always
    tell "no match" apart from "that tier does not exist yet".

    VISION IS OPT-IN, NOT AN AUTOMATIC FALLBACK — decision, and why:
    "vision" is attempted only when the caller explicitly lists it in
    tier_order. It does NOT fire automatically when the uia tier misses.
    Three reasons, in order of weight:
      1. Cost and latency are real and unbounded from the caller's point of
         view. The uia tier is a local API call measured in milliseconds;
         the vision tier is a hosted multimodal model call that the
         grounding spike measured at anywhere from tens of seconds to
         several minutes. Silently turning a free lookup into that, on a
         miss the caller may well have expected, is the kind of surprise
         that gets a tool called in a loop by accident.
      2. The two tiers do not take the same input. uia resolves a LOCATOR
         (automation_id/name); vision needs a plain-language DESCRIPTION.
         A caller that only supplied a locator has given nothing the vision
         tier could act on, so an automatic fallback would either fail
         anyway or — worse — quietly reuse the locator string as a visual
         description and return a confident-looking guess.
      3. Confidence differs by more than a number. A uia hit is a real
         handle to a real control; a vision hit is a coordinate guess that
         cannot be actuated at all in this build. Making that substitution
         invisibly, inside a call the caller thinks is doing UIA lookup, is
         precisely the kind of false-completeness the rest of this
         codebase's config comments keep warning about.
    So: pass tier_order=["uia", "vision"] and query.description to get the
    fallback behaviour, and you get it because you asked for it.

    Returns an ElementRef (orbit.tools.element_ref) — the exact shape
    windows_click/windows_drag's target argument already accepts directly
    (Contract 3). Note that this only actually WORKS for a uia-tier result:
    a vision-tier ElementRef carries Confidence.VISION_INFERRED and is
    refused by the actuation gate by design."""

    # Has to cover the SLOWEST tier this tool can dispatch to, not the
    # fastest: with tier_order=["uia","vision"] it may end up waiting on
    # VisionLocateTool's own 300s hosted-model call, and an outer 30s
    # default would kill that before the inner tool's timeout ever applied.
    # The uia-only path still returns in milliseconds — this ceiling costs
    # it nothing.
    default_timeout_s = 930.0

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        query = args["query"]
        tier_order = args.get("tier_order") or ["uia"]
        # "ocr" (and anything else unknown) is still honestly reported as a
        # tier this build does not have. Only uia and vision are real.
        unavailable = [t for t in tier_order if t not in ("uia", "vision")]

        window_handle = _foreground_or_given(query.get("window_handle"))
        automation_id = query.get("automation_id")
        name = query.get("name")
        control_type = query.get("control_type")
        description = (query.get("description") or "").strip()
        has_locator = bool(automation_id or name)

        tried: list[str] = []
        uia_error: Optional[str] = None

        for tier in tier_order:
            if tier == "uia":
                if not has_locator:
                    uia_error = (
                        "the uia tier needs automation_id or name, and neither was given"
                    )
                    continue
                tried.append("uia")
                try:
                    element = resolve_uia_element(
                        window_handle,
                        automation_id=automation_id,
                        name=name,
                        control_type=control_type,
                    )
                except ClassifiedToolError as exc:
                    uia_error = str(exc)
                    continue
                return (
                    {
                        "element": element.model_dump(),
                        "tiers_tried": tried,
                        "tiers_unavailable": unavailable,
                    },
                    element.confidence,
                )

            if tier == "vision":
                if not description:
                    continue
                tried.append("vision")
                # Candidate generation happens INSIDE VisionLocateTool, which
                # is the tool that actually makes the model call and therefore
                # the one that needs the marks. This tool reports what that
                # one found rather than generating its own set — two walks of
                # the same UIA tree would be pure waste, and worse, could
                # disagree if the window changed between them.
                #
                # Nested execute(), never .run() — Invariant 4. The vision
                # tier keeps its OWN timeout, error classification, redaction
                # and event row this way; calling run() directly would
                # silently borrow this tool's shorter ones instead.
                result = await vision_locate_tool.execute(
                    {"window_handle": window_handle, "target_description": description},
                    task_id=_resolve_task_id(args.get("task_id")),
                    token=token,
                )
                if not result.ok:
                    raise ClassifiedToolError(
                        result.error.kind, result.error.message, retryable=result.error.retryable
                    )
                return (
                    {
                        "element": result.data["element"],
                        "tiers_tried": tried,
                        "tiers_unavailable": unavailable,
                        "vision_raw_reply": result.data.get("raw_reply"),
                        "candidates": result.data.get("candidates", []),
                        "candidate_source": result.data.get("candidate_source"),
                        "candidate_uia_assessment": result.data.get(
                            "candidate_uia_assessment"
                        ),
                        "candidate_fallback_error": result.data.get(
                            "candidate_fallback_error"
                        ),
                        "agreement": result.data.get("agreement"),
                    },
                    result.confidence,
                )

        if not has_locator and "vision" not in tier_order:
            raise ClassifiedToolError(
                "reasoning_failure",
                "query needs automation_id or name to locate an element — the uia tier "
                "requires a locator, not a free-text description. To search visually "
                "instead, pass tier_order=['uia', 'vision'] with query.description, or "
                "call perception_vision_locate directly.",
            )
        if not has_locator and not description:
            raise ClassifiedToolError(
                "reasoning_failure",
                "the vision tier was requested but query.description is empty — it needs a "
                "plain-language description of what to look for.",
            )
        raise ClassifiedToolError(
            "state_failure",
            f"no tier resolved the element (tried: {tried or 'none'}; "
            f"unavailable in this build: {unavailable or 'none'})."
            + (f" uia tier said: {uia_error}" if uia_error else ""),
        )


class CaptureScreenshotTool(BaseTool):
    """perception_capture_screenshot — captures the screen or a region via
    `mss` (pure-Python, no system binary — see module docstring for why
    this isn't DXCam/Windows.Graphics.Capture as the catalog names). Never
    fired by continuous polling — on-demand only, same "hotkey or a cheap
    change-detection trigger" framing the catalog uses. Returns PNG bytes
    base64-encoded plus the captured region, not a file path — nothing is
    written to disk by this tool."""

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        png_bytes, monitor, _size = _grab_png(args.get("region"))
        return (
            {
                "region": {"left": monitor["left"], "top": monitor["top"], "width": monitor["width"], "height": monitor["height"]},
                "image_base64": base64.b64encode(png_bytes).decode("ascii"),
                "format": "png",
            },
            Confidence.API_SUCCESS,
        )


def _grab_png(region: Optional[dict]) -> tuple[bytes, dict, tuple[int, int]]:
    """THE screenshot mechanism for this server. perception_capture_screenshot
    and perception_vision_locate both go through here rather than each
    opening their own `mss` session — one capture path, so a fix to how the
    screen is read can never apply to only one of them.

    Returns (png_bytes, the monitor/region actually grabbed, (width, height)).
    """
    with mss.MSS() as sct:
        monitor = _region_to_monitor(region, sct)
        shot = sct.grab(monitor)
        return mss.tools.to_png(shot.rgb, shot.size), monitor, (shot.width, shot.height)


def _grab_raw_rgb(region: Optional[dict]) -> bytes:
    """Raw RGB bytes of the same grab _grab_png encodes. Only the vision
    tier needs this: its downscaler filters actual pixels, and decoding the
    PNG back would mean adding an image library this project does not have."""
    with mss.MSS() as sct:
        return bytes(sct.grab(_region_to_monitor(region, sct)).rgb)


def _region_to_monitor(region: Optional[dict], sct: "mss.base.MSSBase") -> dict:
    if region:
        return {
            "left": int(region["left"]),
            "top": int(region["top"]),
            "width": int(region["width"]),
            "height": int(region["height"]),
        }
    primary = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
    return primary


# ===========================================================================
# VISION TIER — what the grounding spike measured, and what it decided
# ===========================================================================
# Spike run 2026-08-18/19 on this machine (1920x1080, 125% DPI scaling),
# model nvidia_nim/google/gemma-4-31b-it via LiteLLM. 10 real screenshots,
# 46 targets. The harness, the screenshots and the raw per-call replies are
# not checked in (they contain this desktop as it happened to be); the
# numbers recorded here are what it produced.
#
# Screenshot mix: 3 native apps of the kind windows-control already drives
# (Notepad — what test_windows_control_live.py opens — Calculator, File
# Explorer), 3 dense (a real Excel sheet, Control Panel small-icons view,
# System32 in Details view), 2 with NO UI Automation representation at all
# (Microsoft Solitaire Collection, and a purpose-built HTML <canvas> mixer
# UI), 2 adversarial (near-identical channel tiles with an overlay
# occluding some of them and one clipped by the viewport; two real
# overlapping windows).
#
# Ground truth was NOT eyeballed except where it could not be otherwise:
# exact UIA rects for the native/dense shots, and for the canvas UIs an
# exact colour-map pass (the page repaints every target as one flat unique
# colour, screenshotted at identical window geometry, bounds recovered by a
# pixel scan). Only the Solitaire targets are hand-labelled, because that
# app exposes no per-element UIA at all — which is exactly why it is in the
# set.
#
# REPRESENTATION — the decision this spike existed to make:
#   The model was asked, deliberately, WITHOUT any output format being
#   imposed ("Locate this UI element: <desc>. Report where it is in the
#   image."). It answered in Gemma's native pointing format every single
#   time: {"point": [y, x]} with BOTH values normalised to 0-1000, y first.
#   Not pixels, not 0-1, not a bounding box. So that is what this code
#   parses. It is not always clean JSON — it arrives bare, inside a ```json
#   fence, or wrapped in a sentence ("The File menu is located at {...}.")
#   — so the point object is matched wherever it appears rather than
#   requiring the whole reply to parse as JSON. A box_2d parser is kept
#   because this model family emits that shape for extent queries, but the
#   spike never saw one; points are the path actually exercised.
#   No alternate representation (an overlay marking candidate regions) was
#   built: that was explicitly contingent on the first one underperforming,
#   and it did not underperform.
#
# MEASURED RESULT — 46 calls, 4 of them killed by provider-side HTTP 504s
#   (counted as misses below rather than quietly dropped):
#
#     overall                                35/46  = 76%   (35/42 = 83% of
#                                                             calls that answered)
#     adversarial (occlusion, near-dupes)     8/9   = 89%
#     custom-drawn / no UIA at all            8/10  = 80%   (8/8 = 100% of
#                                                             calls that answered)
#     native apps windows-control drives     11/14  = 79%
#     dense UI (spreadsheet, small controls)  8/13  = 62%
#
#   A "hit" is the returned point landing INSIDE the target's true bounds.
#   The custom-drawn column is the one that justifies this tier existing:
#   those elements have no UIA representation, so the alternative is not a
#   worse answer, it is no answer at all. Dense UI is the weak column —
#   62%, with the misses being near-misses on small adjacent controls (one
#   was off by a single pixel at a box edge). That is exactly the case where
#   the uia tier already works well, which is why the agent instruction
#   tells the model to try uia first and reach for vision only when uia
#   genuinely cannot see the element.
#
#   Latency, same 46 calls: min 5.2s, median 54.6s, p90 640.4s, max 816.6s.
#   The spread is queueing on the shared hosted tier, not compute — the same
#   image and prompt returned in 15.5s and 158.8s on two different calls.
#   Do not tune anything to these numbers; treat them as "this is not a fast
#   call" and nothing more precise than that.
#
#   Two failure modes worth knowing, both handled in the parser below:
#     - 1 reply in 42 came back as `{"point: [73, 895],` — the key's closing
#       quote misplaced, so the JSON is invalid but the coordinates are fine.
#     - 2 replies in 42 returned coordinates outside the 0-1000 range
#       entirely (e.g. y=7478). Those are rejected rather than translated:
#       an out-of-range point becomes a screen coordinate far off the
#       display, wrapped in an ElementRef that looks as trustworthy as a
#       good one.
#
#   Separately verified end to end, against a real custom-drawn <canvas>
#   window whose true control bounds were recovered exactly from on-screen
#   registration markers: 5/5 of the tool's answers landed inside the real
#   control, in TRUE SCREEN COORDINATES. That is the check that proves the
#   crop -> resize -> normalise -> invert chain is right, not just that the
#   plumbing runs.
#
# IMAGE SIZE — the one hard limit here that is NOT a guess:
#   NVIDIA's hosted NIM endpoints document a ~180,000-character ceiling on
#   an inline base64 image. Measured against it, 9 of the 10 window crops
#   were comfortably under (47k-142k chars). The Solitaire capture was
#   1,748,552 chars — nearly 10x over — because photographic game art
#   defeats PNG. Honest detail: that oversized payload was NOT rejected by
#   this endpoint; it went through, but its calls ran 54s/614s/615s and two
#   more died with 504s, against a 54.6s median overall. So step 4
#   downscales to stay inside the documented ceiling rather than relying on
#   an undocumented tolerance that happens to work today.
#
# DPI, found the hard way during the spike:
#   `import mss` does NOT make the process DPI-aware; instantiating
#   mss.MSS() does. Before that, GetWindowRect returns LOGICAL coordinates
#   (1536x864 on this 125%-scaled 1920x1080 display) while an mss grab is
#   always PHYSICAL (1920x1080) — so reading window bounds before the first
#   screenshot crops the wrong rectangle, off by a consistent ~20%.
#   _ensure_physical_screen_coords() forces the ordering rather than
#   leaving it to whichever tool happened to run first.
# ===========================================================================

_VISION_MODEL = "nvidia_nim/google/gemma-4-31b-it"

# NVIDIA's documented inline-image ceiling for hosted NIM endpoints. Not a
# number this codebase invented — see the IMAGE SIZE note above.
_NIM_MAX_INLINE_B64_CHARS = 180_000

# When the model returns a POINT rather than a box (which, per the spike, is
# every time so far) there is no extent to report. A zero-area box would
# fail bounds checks elsewhere, so the point is widened into a small fixed
# square. 40x40 physical pixels: the median smaller-dimension of the 46 real
# targets measured in the spike was exactly 40px, and 40px is also Windows'
# conventional minimum comfortable click target. It is a placeholder meaning
# "somewhere around here", NOT a claim about the element's real size, and is
# deliberately small enough that it cannot be mistaken for a measured extent.
_VISION_POINT_BOX_PX = 40

# Gemma emits its pointing format bare, fenced, or mid-sentence — match the
# object wherever it lands rather than requiring the whole reply to parse.
# The closing quote on the key is OPTIONAL because the spike caught the model
# emitting `{"point: [73, 895],` — key quote misplaced, JSON invalid — in 1
# reply out of 26. A strict parser drops an answer that is perfectly
# well-formed apart from one character.
_VISION_POINT_RE = re.compile(r'"point"?\s*:\s*\[\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*\]')
_VISION_BOX_RE = re.compile(
    r'"box_2d"?\s*:\s*\[\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*,'
    r'\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*\]'
)
_VISION_CONF_RE = re.compile(r'"confidence"\s*:\s*(-?\d+\.?\d*)')

_VISION_PROMPT = (
    "This is a screenshot of an application window.\n"
    "Locate this UI element: {description}\n"
    "Report where it is in the image."
)

# SET-OF-MARK — the second representation, added after the spike
# ---------------------------------------------------------------
# The spike deliberately imposed no output format and the model answered in
# points every time, so points is what this tool parsed. The spike also said
# an overlay representation "was explicitly contingent on the first one
# underperforming, and it did not underperform" — that was a decision about
# what to BUILD, not a measurement that points beat marks, because the two
# were never compared.
#
# This is that comparison, run as a real alternative rather than a
# replacement: asking "which numbered box" turns coordinate regression into
# classification over a short list, which is a strictly easier question when
# the list contains the answer. When it does not — too few candidates, or
# none at all — the freeform-point prompt is still the path taken, because
# a window with no UIA representation is exactly the case that produces no
# candidates and is also exactly the case this tier exists for.
_VISION_SOM_PROMPT = (
    "This is a screenshot of an application window. Numbered boxes have been "
    "drawn on it, each marking one candidate UI element.\n"
    "Which numbered box is: {description}\n"
    'Answer with only the number, as JSON: {{"index": N}}'
)

# Matched the same way `_VISION_POINT_RE` is: wherever it appears, in prose or
# a fence, rather than requiring the whole reply to parse as JSON. Same
# reasoning — a well-formed answer wrapped in a sentence is a correct answer,
# and a strict parser throws it away.
_VISION_INDEX_JSON_RE = re.compile(r'"(?:index|box|mark)"?\s*:\s*(\d+)')
_VISION_INDEX_BARE_RE = re.compile(r"\b(\d{1,3})\b")


def _parse_index_reply(text: str, valid: set[int]) -> Optional[int]:
    """Pull the chosen box index out of the reply, or None.

    An index outside the candidate range is rejected rather than clamped,
    for exactly the reason `_in_normalised_range` rejects an out-of-range
    point: a nonsense answer translated into a real-looking coordinate is
    carried by an ElementRef that looks as trustworthy as a good one.
    """
    text = text or ""
    match = _VISION_INDEX_JSON_RE.search(text)
    if match:
        n = int(match.group(1))
        return n if n in valid else None
    for match in _VISION_INDEX_BARE_RE.finditer(text):
        n = int(match.group(1))
        if n in valid:
            return n
    return None


def _summarize_agreement(samples: list[Optional[int]]) -> dict:
    """Compare repeated grounding answers on the identical image.

    Returns the winning index and a label for how much the samples agreed.
    This is a DIAGNOSTIC SIGNAL ONLY. It is recorded under
    element.state["vision"]["agreement"] and is never promoted into the
    `confidence` field — that number is what `windows_control_tools`'
    actuation gate reads, and three self-consistent guesses from one model on
    one image are still guesses. Consistency is not correctness: a model can
    be confidently and repeatably wrong, and this tier has no ground truth to
    check itself against.
    """
    answered = [s for s in samples if s is not None]
    if not answered:
        return {"chosen": None, "agreement": "no_answer", "distinct": 0,
                "samples": samples, "votes": 0}

    counts: dict[int, int] = {}
    for value in answered:
        counts[value] = counts.get(value, 0) + 1
    chosen, votes = max(counts.items(), key=lambda kv: (kv[1], -kv[0]))

    if len(samples) == 1:
        label = "single_sample"
    elif len(counts) == 1:
        label = "unanimous"
    elif votes > len(answered) / 2:
        label = "majority"
    else:
        label = "split"
    return {
        "chosen": chosen,
        "agreement": label,
        "distinct": len(counts),
        "samples": samples,
        "votes": votes,
    }

_dpi_ready = False


def _ensure_physical_screen_coords() -> None:
    """Make this process DPI-aware BEFORE any window rectangle is read.

    Instantiating mss.MSS() is what actually sets the awareness flag (a bare
    `import mss` does not). Until it is set, win32gui.GetWindowRect reports
    logical coordinates while an mss grab returns physical pixels, and the
    crop in step 3 would be silently wrong by the display's scale factor.
    Cheap and idempotent, so it is simply always called first."""
    global _dpi_ready
    if not _dpi_ready:
        with mss.MSS():
            pass
        _dpi_ready = True


def _nim_api_key() -> str:
    """Read NVIDIA_NIM_API_KEY, loading the project .env if it is not set.

    This matters here specifically: the MCP servers are spawned with
    env={"ORBIT_TASK_ID": ...} (orbit/skills/*.py), and mcp's
    StdioServerParameters uses that dict INSTEAD OF inheriting the parent
    environment — so unlike orbit/agent.py, which runs in the parent process
    where load_dotenv() has already run, this server subprocess starts with
    no API key in its environment at all. The .env path is resolved from
    this file rather than the cwd, because the subprocess's working
    directory is not something this tool controls."""
    key = os.environ.get("NVIDIA_NIM_API_KEY", "").strip()
    if key:
        return key
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except Exception:
        pass
    key = os.environ.get("NVIDIA_NIM_API_KEY", "").strip()
    if not key:
        raise ClassifiedToolError(
            "state_failure",
            "NVIDIA_NIM_API_KEY is not set, so the vision tier cannot run. Add it to the "
            ".env file in the project root — this is a second model string on the provider "
            "the orchestrating model already uses, not a new credential.",
        )
    return key


def _box_downscale(rgb: bytes, width: int, height: int, factor: int) -> tuple[bytes, int, int]:
    """Integer box-filter downscale of a raw RGB buffer. Pure Python on
    purpose: Pillow is not a dependency of this project and the vision tier
    is not a good enough reason to make it one. INTEGER factors only, which
    is what keeps the inverse transform in _vision_point_to_screen exact
    rather than approximately right."""
    new_w, new_h = width // factor, height // factor
    out = bytearray(new_w * new_h * 3)
    area = factor * factor
    for ny in range(new_h):
        y0 = ny * factor
        for nx in range(new_w):
            x0 = nx * factor
            r = g = b = 0
            for dy in range(factor):
                base = ((y0 + dy) * width + x0) * 3
                for dx in range(factor):
                    i = base + dx * 3
                    r += rgb[i]
                    g += rgb[i + 1]
                    b += rgb[i + 2]
            o = (ny * new_w + nx) * 3
            out[o] = r // area
            out[o + 1] = g // area
            out[o + 2] = b // area
    return bytes(out), new_w, new_h


def _fit_for_inline_upload(png: bytes, raw_rgb: bytes, width: int, height: int) -> tuple[bytes, int]:
    """Step 4. Downscale ONLY if the encoded image exceeds NVIDIA's
    documented inline ceiling — most window crops do not (the spike measured
    47k-142k chars for 9 of its 10 shots) and are sent untouched at native
    resolution. Returns (png_to_send, scale_factor); factor 1 means the
    image was left exactly as captured."""
    if len(base64.b64encode(png)) <= _NIM_MAX_INLINE_B64_CHARS:
        return png, 1
    for factor in (2, 3, 4, 5, 6):
        small, new_w, new_h = _box_downscale(raw_rgb, width, height, factor)
        candidate = mss.tools.to_png(small, (new_w, new_h))
        if len(base64.b64encode(candidate)) <= _NIM_MAX_INLINE_B64_CHARS:
            return candidate, factor
    raise ClassifiedToolError(
        "state_failure",
        f"this window capture is {len(base64.b64encode(png)):,} base64 chars and will not fit "
        f"under NVIDIA NIM's ~{_NIM_MAX_INLINE_B64_CHARS:,}-char inline image limit even "
        "downscaled 6x. Target a smaller window, or route it through the NVCF asset API "
        "(not implemented in this build).",
    )


def _vision_point_to_screen(
    y_norm: float,
    x_norm: float,
    *,
    sent_width: int,
    sent_height: int,
    scale_factor: int,
    crop_origin: tuple[int, int],
) -> tuple[float, float]:
    """Translate one model-returned point back to true screen coordinates.

    Reverses preprocessing steps 3-4 in the exact opposite order they were
    applied:
      a. de-normalise 0-1000 against the image ACTUALLY SENT
      b. undo the resize  (multiply by scale_factor)
      c. undo the crop    (add the crop origin)
    Getting (c) wrong produces answers that are wrong by a small CONSTANT
    offset — plausible-looking, and far harder to notice than an obviously
    wrong answer — which is why test_perception_tools.py round-trips this
    against a synthetic crop/resize rather than trusting it by inspection."""
    x_sent = x_norm / 1000.0 * sent_width
    y_sent = y_norm / 1000.0 * sent_height
    x_crop = x_sent * scale_factor
    y_crop = y_sent * scale_factor
    return crop_origin[0] + x_crop, crop_origin[1] + y_crop


def _parse_vision_reply(text: str) -> dict:
    """Pull the located point/box out of whatever prose the model wrapped it
    in. Values stay in the model's own 0-1000 normalised space; the caller
    translates them to screen coordinates."""
    text = text or ""
    point_match = _VISION_POINT_RE.search(text)
    box_match = _VISION_BOX_RE.search(text)
    conf_match = _VISION_CONF_RE.search(text)
    model_confidence = float(conf_match.group(1)) if conf_match else None

    if point_match:
        y, x = (float(v) for v in point_match.groups())
        if _in_normalised_range(y, x):
            return {"kind": "point", "point": (y, x), "box": None,
                    "model_confidence": model_confidence}
    if box_match:
        y0, x0, y1, x1 = (float(v) for v in box_match.groups())
        if _in_normalised_range(y0, x0, y1, x1):
            return {"kind": "box_2d", "point": None, "box": (y0, x0, y1, x1),
                    "model_confidence": model_confidence}
    return {"kind": None, "point": None, "box": None, "model_confidence": model_confidence}


def _in_normalised_range(*values: float) -> bool:
    """Every coordinate must be inside the model's own 0-1000 space.

    Not defensive boilerplate — the spike caught this twice in 42 answered
    calls: replies like {"point": [7478, 237]} whose y is seven times the
    top of the range. Translated blindly, that becomes a screen coordinate
    thousands of pixels off the display, carried by an ElementRef that
    looks exactly as trustworthy as a good one. Out of range means the
    model did not really locate anything, so it is reported as no result
    rather than passed on as a confident wrong answer."""
    return all(0.0 <= v <= 1000.0 for v in values)


def _window_bounds_to_region(bounds) -> dict:
    """Window rect -> an mss grab region, clamped to the virtual screen.

    Clamping is not cosmetic: a maximized window on this machine reports
    (-9, -9, 1929, 1089) — its invisible resize border sits outside the
    desktop — and grabbing that rectangle either fails outright or pulls in
    a strip of whatever is behind it."""
    left, top, right, bottom = bounds
    with mss.MSS() as sct:
        virtual = sct.monitors[0]
    left = max(int(left), virtual["left"])
    top = max(int(top), virtual["top"])
    right = min(int(right), virtual["left"] + virtual["width"])
    bottom = min(int(bottom), virtual["top"] + virtual["height"])
    if right - left <= 0 or bottom - top <= 0:
        raise ClassifiedToolError(
            "state_failure",
            f"window bounds {bounds!r} enclose no on-screen area to capture (minimized?)",
        )
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


class VisionLocateTool(BaseTool):
    """perception_vision_locate — the on-demand visual observer Section 11
    describes ("visual observer on-demand only"), and the ONLY tier in this
    build that can locate an element with no UI Automation representation at
    all: a game, a <canvas> app, any custom-drawn control. Verified in the
    spike against Microsoft Solitaire, whose entire UI surfaces through UIA
    as a stack of nameless Panes.

    This is a READ. It reports where something appears to be; it does not
    click it, and its output deliberately CANNOT be used to click it. Every
    ElementRef it returns is scored Confidence.VISION_INFERRED (0.50), below
    windows_control_policy.yaml's min_actuation_confidence (0.70), so
    windows_click/windows_drag refuse it exactly as they refuse a raw
    {x, y}. That is not an oversight to tidy up later: there is no
    confirmation channel in this build through which a human could approve a
    visually-guessed click, so a guessed click has no safe path to the OS.
    Pinned by test_vision_sourced_element_ref_is_still_refused_by_actuation.

    The model's OWN stated confidence, if it volunteers one, is recorded
    under element.state["vision"] for debugging. It is never used as the
    ElementRef.confidence value — that number is what the actuation gate
    reads, and an unvalidated self-report is exactly the kind of guess this
    tier exists to be honest about."""

    # This tool makes its own call and waits on a hosted endpoint; the
    # orchestrating agent's model is not in this path. The spike measured a
    # very wide latency spread on the shared hosted tier (tens of seconds to
    # several minutes for identical work — queueing, not compute), so the
    # 30s BaseTool default would time out even successful calls. A ceiling,
    # not a target.
    # Set-of-mark calls the model `grounding_samples` times (default 3), so
    # the ceiling has to cover the whole batch, not one call. 900s = 3 x the
    # 300s a single call was already allowed.
    default_timeout_s = 900.0

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        description = (args.get("target_description") or "").strip()
        if not description:
            raise ClassifiedToolError(
                "reasoning_failure",
                "target_description is required — this tier takes a plain-language description "
                "of what to look for (e.g. 'the red record button'), not a UIA locator.",
            )

        # 1. window resolution, same helper every other tool here uses
        _ensure_physical_screen_coords()
        window_handle = _foreground_or_given(args.get("window_handle"))

        # 2-3. capture through THE shared mss path, cropped to this window's
        #      bounds rather than the whole (possibly multi-monitor) desktop
        snapshot = window_snapshot(window_handle)
        region = _window_bounds_to_region(snapshot["bounds"])
        png, monitor, (cap_w, cap_h) = _grab_png(region)
        crop_origin = (monitor["left"], monitor["top"])
        token.raise_if_cancelled()

        # 4. downscale only if NVIDIA's documented inline limit demands it
        raw_rgb = _grab_raw_rgb(region)
        sent_png, scale_factor = _fit_for_inline_upload(
            png, raw_rgb, cap_w, cap_h
        )
        sent_w = cap_w // scale_factor if scale_factor > 1 else cap_w
        sent_h = cap_h // scale_factor if scale_factor > 1 else cap_h

        # 4b. Candidate generation, then set-of-mark IF there is enough to
        #     choose between. Never fatal: a window that yields no candidates
        #     falls through to the freeform-point prompt below, which is the
        #     path that works on the no-UIA windows this tier exists for.
        vision_cfg = load_perception_policy().get("vision", {})
        min_for_som = int(vision_cfg.get("min_candidates_for_som", 3))
        samples_wanted = max(1, int(vision_cfg.get("grounding_samples", 3)))

        try:
            candidate_info = generate_candidates(window_handle)
        except Exception as exc:  # noqa: BLE001 - degraded, never fatal
            candidate_info = {"candidates": [], "source": None,
                              "uia_assessment": None,
                              "fallback_error": f"{type(exc).__name__}: {exc}"}
        candidates = candidate_info["candidates"]
        use_som = len(candidates) >= min_for_som

        agreement: Optional[dict] = None
        chosen_candidate: Optional[dict] = None

        if use_som:
            marked_rgb = draw_marks(
                raw_rgb, cap_w, cap_h, candidates, crop_origin,
                scale=int(vision_cfg.get("mark_scale", 2)),
            )
            marked_png = mss.tools.to_png(marked_rgb, (cap_w, cap_h))
            som_png, som_scale = _fit_for_inline_upload(
                marked_png, marked_rgb, cap_w, cap_h
            )
            som_b64 = base64.b64encode(som_png).decode("ascii")
            valid = {c["index"] for c in candidates}

            started = time.monotonic()
            samples: list[Optional[int]] = []
            replies: list[str] = []
            for _ in range(samples_wanted):
                token.raise_if_cancelled()
                sample_reply = await asyncio.to_thread(
                    self._call_vision_model, som_b64, description, _VISION_SOM_PROMPT
                )
                replies.append(sample_reply)
                samples.append(_parse_index_reply(sample_reply, valid))
            latency_ms = int((time.monotonic() - started) * 1000)

            agreement = _summarize_agreement(samples)
            reply = " | ".join(replies)
            if agreement["chosen"] is not None:
                chosen_candidate = next(
                    c for c in candidates if c["index"] == agreement["chosen"]
                )
            else:
                # Every sample was unparseable or out of range. Fall back to
                # the point prompt rather than failing: the marked image not
                # working tells us nothing about whether the plain one will.
                use_som = False
                agreement["fell_back_to_point"] = True

        if not use_som:
            # 5. base64 PNG — the same encoding perception_capture_screenshot returns
            image_b64 = base64.b64encode(sent_png).decode("ascii")

            token.raise_if_cancelled()
            started = time.monotonic()
            reply = await asyncio.to_thread(self._call_vision_model, image_b64, description)
            latency_ms = int((time.monotonic() - started) * 1000)

            parsed = _parse_vision_reply(reply)
            if parsed["kind"] is None:
                raise ClassifiedToolError(
                    "tool_failure",
                    f"the vision model returned no locatable point for {description!r}. "
                    f"Raw reply: {(reply or '')[:300]!r}",
                    retryable=True,
                )
        else:
            parsed = {"kind": "set_of_mark", "point": None, "box": None,
                      "model_confidence": _parse_vision_reply(reply)["model_confidence"]}

        if parsed["kind"] == "set_of_mark":
            # The chosen candidate's bounds are ALREADY screen coordinates —
            # candidate_source produces them that way, clipped to the window.
            # No inverse transform is applied here on purpose: translating
            # coordinates we did not have to normalise in the first place
            # would be a round trip that can only lose precision. The
            # crop/resize/translate chain below is untouched and still the
            # path every point-mode answer takes.
            assert chosen_candidate is not None
            bounds = tuple(int(v) for v in chosen_candidate["bounds"])
            bounds_basis = (
                f"set-of-mark box {chosen_candidate['index']} "
                f"(source={chosen_candidate.get('source')})"
            )
        elif parsed["kind"] == "point":
            y, x = parsed["point"]
            cx, cy = _vision_point_to_screen(
                y, x, sent_width=sent_w, sent_height=sent_h,
                scale_factor=scale_factor, crop_origin=crop_origin,
            )
            half = _VISION_POINT_BOX_PX // 2
            bounds = (round(cx) - half, round(cy) - half, round(cx) + half, round(cy) + half)
            bounds_basis = f"point widened to a fixed {_VISION_POINT_BOX_PX}px box"
        else:
            y0, x0, y1, x1 = parsed["box"]
            left, top = _vision_point_to_screen(
                y0, x0, sent_width=sent_w, sent_height=sent_h,
                scale_factor=scale_factor, crop_origin=crop_origin,
            )
            right, bottom = _vision_point_to_screen(
                y1, x1, sent_width=sent_w, sent_height=sent_h,
                scale_factor=scale_factor, crop_origin=crop_origin,
            )
            bounds = (round(left), round(top), round(right), round(bottom))
            bounds_basis = "model-returned box_2d"

        element = ElementRef(
            element_id=f"vision:{window_handle}/{description[:60]}",
            role=None,
            name=description,
            bounds=bounds,
            state={
                "vision": {
                    # Diagnostics only. model_confidence is NEVER promoted
                    # into the confidence field — see this class's docstring.
                    "model": _VISION_MODEL,
                    "model_confidence": parsed["model_confidence"],
                    "reply_format": parsed["kind"],
                    "scale_factor": scale_factor,
                    "crop_origin": list(crop_origin),
                    "sent_image_size": [sent_w, sent_h],
                    "bounds_basis": bounds_basis,
                    "latency_ms": latency_ms,
                    # Repeated-sampling agreement. Diagnostic ONLY — see
                    # _summarize_agreement. Sits beside model_confidence
                    # precisely because both are things the model said about
                    # itself, and neither is allowed anywhere near the
                    # `confidence` field the actuation gate reads.
                    "agreement": agreement,
                    "candidate_source": candidate_info["source"],
                    "candidate_count": len(candidates),
                    "candidate_fallback_error": candidate_info["fallback_error"],
                }
            },
            source="vision",
            confidence=Confidence.VISION_INFERRED,
        )

        return (
            {
                "element": element.model_dump(),
                "window_handle": window_handle,
                "window_title": snapshot["title"],
                "raw_reply": reply,
                "candidates": candidates,
                "candidate_source": candidate_info["source"],
                "candidate_uia_assessment": candidate_info["uia_assessment"],
                "candidate_fallback_error": candidate_info["fallback_error"],
                "agreement": agreement,
            },
            Confidence.VISION_INFERRED,
        )

    def _call_vision_model(
        self, image_b64: str, description: str, prompt: str = _VISION_PROMPT
    ) -> str:
        """Its OWN LiteLLM call, on the same provider as the orchestrating
        agent but a different model string — the coordinator's model is not
        multimodal and is not in this path at all."""
        import litellm

        api_key = _nim_api_key()
        try:
            response = litellm.completion(
                model=_VISION_MODEL,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": prompt.format(description=description)},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                ]}],
                api_key=api_key,
                max_tokens=400,
            )
        except Exception as exc:
            raise ClassifiedToolError(
                "tool_failure", f"vision model call failed: {exc}", retryable=True
            ) from exc
        return response.choices[0].message.content or ""


class WaitForVisualChangeTool(BaseTool):
    """perception_wait_for_visual_change — blocks until a pixel-diff is
    detected in `region`, or times out. The perception-side equivalent of
    browser_wait_for — needed because native UI has no DOM-mutation event
    to hook the way a browser does. Compares raw RGB bytes of successive
    `mss` grabs; any byte difference counts as a change (no fuzz
    threshold) — cheap and correct for "did anything change", not
    sensitive to *how much*."""

    default_timeout_s = 90.0  # same reasoning as WaitTool in
    # windows_control_tools.py: this polls on its own deadline, so needs
    # headroom beyond the 30s in-process default.

    _POLL_INTERVAL_S = 0.3
    _MAX_TIMEOUT_S = 60.0

    async def run(self, args: dict, token: CancellationToken) -> tuple[Any, Optional[float]]:
        import asyncio

        region = args.get("region")
        timeout = min(float(args.get("timeout", 10.0)), self._MAX_TIMEOUT_S)

        with mss.MSS() as sct:
            monitor = _region_to_monitor(region, sct)
            baseline = bytes(sct.grab(monitor).rgb)

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                token.raise_if_cancelled()
                current = bytes(sct.grab(monitor).rgb)
                if current != baseline:
                    return {"changed": True, "region": monitor}, Confidence.API_SUCCESS
                await asyncio.sleep(self._POLL_INTERVAL_S)

        return {"changed": False, "region": monitor}, Confidence.API_SUCCESS


def _metadata(name: str, description: str, **overrides) -> ToolMetadata:
    fields = {**_LOW_HEADLESS, **overrides}
    return ToolMetadata(name=name, description=description, **fields)


get_state_tool = GetStateTool(
    _metadata(
        "perception_get_state",
        "Return the active window's title/process and (if task_id given) "
        "that task's status. No model call, effectively free — call this "
        "before anything else when you need to know what's on screen.",
    )
)
get_uia_tree_tool = GetUiaTreeTool(
    _metadata(
        "perception_get_uia_tree",
        "Return the UI Automation tree for a window (default: the "
        "foreground window) as a flat, depth-labeled node list — role, "
        "name, automation_id, bounds. Capped at max_nodes (default 200); "
        "check `truncated` before assuming you saw the whole tree.",
    )
)
find_element_tool = FindElementTool(
    _metadata(
        "perception_find_element",
        "Resolve a UI element to an ElementRef. query: {window_handle?, "
        "automation_id?, name?, control_type?, description?}. The 'uia' "
        "tier (default) needs automation_id or name and is effectively "
        "free. Pass tier_order=['uia','vision'] plus query.description to "
        "fall back to the vision tier when UIA cannot find it — that costs "
        "a real model call, so it is opt-in, never automatic. 'ocr' is not "
        "implemented and is reported in tiers_unavailable. A uia-tier "
        "result can be fed straight into windows_click/windows_drag; a "
        "vision-tier result CANNOT (it is refused by the confidence gate).",
    )
)
vision_locate_tool = VisionLocateTool(
    _metadata(
        "perception_vision_locate",
        "Locate a UI element from a plain-language description "
        "(target_description) by sending a screenshot of the window to a "
        "vision model. The only tier that can find controls with NO UI "
        "Automation representation (games, canvas/custom-drawn UI). "
        "Returns an ElementRef with source='vision' and a deliberately low "
        "confidence: it is for SEEING and describing what is on screen, "
        "and windows_click/windows_drag will refuse the result. Slow and "
        "costs a model call — use it only after perception_find_element's "
        "uia tier has actually failed.",
    )
)
capture_screenshot_tool = CaptureScreenshotTool(
    _metadata(
        "perception_capture_screenshot",
        "Capture the screen (or `region`: {left, top, width, height}) as "
        "a base64-encoded PNG. On-demand only — never call this in a "
        "polling loop; use perception_wait_for_visual_change instead.",
    )
)
wait_for_visual_change_tool = WaitForVisualChangeTool(
    _metadata(
        "perception_wait_for_visual_change",
        "Block until any pixel in `region` (default: primary monitor) "
        "changes, or `timeout` seconds elapse (capped at 60). Returns "
        "{changed: bool}.",
    )
)
