# Orbit

An **open-source personal agent for Windows** that the user owns and controls. You give it a goal —
typed or spoken — and it plans and executes end to end across whatever is on your machine: the
browser, real desktop applications, the filesystem, and its own task history. Built on Google ADK +
LiteLLM, reaching models through OpenRouter so the user picks the brain. Every tool the model can
call is served by an MCP server sitting behind an ADK safety plugin — the model never touches an
in-process tool.

## North star

Three things define the target, and every design decision should be checked against them:

1. **It sees the screen.** Vision is the USP — the ability to look at what is actually on screen and
   act on it, including UI that exposes no accessibility tree at all (games, `<canvas>` apps,
   custom-drawn controls). Everything else here is table stakes; this is the differentiator. See
   "The vision tier and the approval path" below, which is the most important section in this file.
2. **It is conversational, not fire-and-forget.** The goal is a smooth back-and-forth, not a
   one-shot command line. **This does not exist yet** — see "Distance to the north star".
3. **It is the user's, not ours.** Open source, running locally on the user's Windows box, against
   whatever LLM they choose to point it at. Model-agnostic by construction (`KNOWN_MODELS` +
   `ORBIT_MODEL`), never hardcoded to one provider.

Practical consequences: prefer generality over demo-specific special cases; keep the model swappable;
and never widen the safety layer for convenience — an agent with real mouse and keyboard control that
users are asked to trust *is* its safety story.

## Current status (2026-09-06)

Working today, verified:

- **CLI**: persistent REPL, one-shot goals, `--foreground` lane, `--serve` warm-worker mode.
- **GUI** (`gui/main.py`): full task submission, live output stream, step rail, task history with
  analytics, and the approvals drawer. Studio (warm/organic) design direction, implemented.
- **Voice input**: F9 global hotkey → mic → Deepgram Nova-3 streaming STT → live transcript → goal
  box. Modal with commit/discard exits.
- **Warm worker**: the GUI spawns one `run_task --serve` process and feeds it goals over stdin,
  removing ~7-8s of cold start per task.
- **Seven toolsets**: six MCP servers in this repo (browser-policy, memory, filesystem,
  windows-control, communication, screen-perception) plus **Dev-MCP**, which proxies a server
  living *outside* the repo and gives the agent sandboxed PowerShell and broad file access. Every
  task carries Dev-MCP, in both lanes.
- **Vision tier**: implemented, and — new — a human can now approve a single vision-guessed action.
- **354 tests collected**, all passing except opt-in live-UI ones. Some hit the network and cost
  tokens; see `tests/CLAUDE.md`.

**Voice integration exists.** Earlier revisions of this file said it did not — that referred to an
older build whose voice code was deleted on the `remove-voice-integration` branch. It was rebuilt
from scratch on `voice-integration` (Deepgram only; no TTS, no Kokoro, no second venv). If you find a
doc claiming voice is absent, the doc is stale.

## Stack

- Python **3.13.7** (`venv/`). One venv. (`venv_tts/`, the isolated 3.11 environment that existed
  only to run Kokoro TTS, is gone along with all TTS code.)
- `mcp>=1.24,<2` — mcp 2.x moved `mcp.shared.session`, which breaks google-adk 2.6.3's `MCPToolset`
  import. Installed: mcp 1.29.0, google-adk 2.6.3. **Do not unpin.**
- SQLite at `data/orbit.db` (WAL). Playwright MCP via `npx`. PySide6 for the GUI. `mss` for
  screen-perception's screenshots (pure-Python, no system binary — not the catalog's named DXCam).
- `sounddevice` + `numpy` for mic capture; `deepgram-sdk` 7.x for streaming STT.

### Models — all LLM calls go through OpenRouter

One credential, `OPENROUTER_API_KEY`, reaches every model. `DEEPGRAM_API_KEY` is separate and only
voice uses it. Both live in `.env`.

- **Committed default**: `openrouter/google/gemini-3.7-flash`.
- **Currently in the working tree, uncommitted**: `DEFAULT_MODEL` switched to
  `openrouter/openai/gpt-6-astra` — an in-flight experiment, not a settled decision.
- **Vision tier**: `openrouter/google/gemma-3-27b-it`, its own LiteLLM call
  (`_VISION_MODEL` in `perception_tools.py`).
- Override per-run with `ORBIT_MODEL` in `.env`; `--list-models` prints the catalog and the active
  model.

⚠️ **`gpt-6-astra` is not in `KNOWN_MODELS`, and `gemini-3.7-flash`'s entry there still says
"Default."** So `--list-models` currently reports `Active: openrouter/openai/gpt-6-astra` while
labelling a different model as the default. Whichever way that experiment settles, fix the catalog
with it — `KNOWN_MODELS` is also the list of models verified to support tool calling, and one that
does not cannot drive this agent at all.

## Commands

Always `venv\Scripts\python.exe` — the venv is not on PATH, so a bare `python` is the wrong
interpreter or none at all. Everything runs from the project root.

```
venv\Scripts\python.exe gui\main.py                                      # the GUI — primary entry point
venv\Scripts\python.exe -m orbit.run_task                                # no goal -> persistent REPL: type a goal, Enter, repeat. 'exit'/Ctrl+C/Ctrl+D to leave
venv\Scripts\python.exe -m orbit.run_task find the cheapest 65 inch tv   # one-shot: single goal on the command line, exits after
venv\Scripts\python.exe -m orbit.run_task --foreground open notepad ...  # opts into lane=foreground — the ONLY way windows-control tools are reachable (one-shot only, not inside the REPL)
venv\Scripts\python.exe -m orbit.run_task --serve                        # warm-worker mode: reads one JSON goal per line from stdin. What the GUI spawns; not meant to be typed at by hand
venv\Scripts\python.exe -m orbit.run_task --list-models                  # known-good models + active one
venv\Scripts\python.exe -m pytest tests\ -q                              # 354 tests (some hit the network; a live-UI windows-control test is opt-in, see tests/CLAUDE.md)
venv\Scripts\python.exe -m eval.run_eval                                 # eval harness against live sites
```

The REPL, the one-shot form and `--serve` all call the exact same `run_task()`. None is a second
execution path — each goal is one call, one `task_id`, one row through
`TaskManager`/`SafetyPlugin`/the events table. They differ only in how a goal arrives and whether the
process stays up between goals.

## Architecture

```
  GUI (gui/main.py)              REPL (while True: input())      one-shot CLI arg
    │  goal typed or spoken (F9)         │                              │
    │  JSON line ──▶ stdin               │                              │
    ▼                                    ▼                              ▼
  orbit.run_task --serve  ───────────▶  run_task(title, goal)  ◀────────┘
  (one warm process, [TASK:DONE n])      │
                                         │  db.create_task ─▶ tasks row
                                         ▼
                        TaskManager.submit(lane)   foreground: single-flight lock
                                                   headless:  semaphore(5)
                                         │
                                         ▼
                      ADK InMemoryRunner + SafetyPlugin (policy.py)
                                         │  before_tool / after_tool / on_error / before_model
                                         ▼
                  MCPToolset ── stdio subprocess, env={ORBIT_TASK_ID}
                       ├──▶ browser-policy server ────▶ Playwright MCP subprocess
                       ├──▶ memory server ────────────▶ orbit/db.py
                       ├──▶ filesystem server ────────▶ data/fs_workspace (scoped)
                       ├──▶ windows-control server ───▶ real mouse/keyboard (lane="foreground" only)
                       ├──▶ communication server ─────▶ swappable MailBackend (today: local SQLite stand-in)
                       ├──▶ screen-perception server ─▶ read-only: UIA tree, screenshots (mss), vision
                       └──▶ Dev-MCP (EXTERNAL) ───────▶ C:\Users\HP\Desktop\MCP\server.py — outside
                                                        this repo, own venv. PowerShell + file access.

  Human-in-the-loop:  windows_control tool ──▶ orbit/confirmation.py ──▶ pending_confirmations table
                                                       │                         ▲
                                                console y/n  ────────────────────┤
                                                GUI approvals drawer ────────────┘
```

windows-control and screen-perception are separate processes (perception free/read-only, actuation
gated — Section 11) but share one UIA implementation, `orbit/mcp_servers/uia_resolver.py`, so the two
never drift into two resolvers that happen to look similar. `ElementRef`
(`orbit/tools/element_ref.py`) is the shape both produce and consume — Contract 3.

## Invariants

These hold no matter which file you are in.

1. **Everything reaches the model through an MCP server.** A tool that is not exposed by
   `orbit/mcp_servers/` is not reachable by the agent, by construction.
2. **Every tool call goes through `SafetyPlugin`** (`orbit/policy.py`). There is no second path into
   a tool and no "just this once" bypass.
3. **`orbit/config/risk_tiers.yaml` is a hard allowlist.** A tool name not listed there is blocked
   outright, before tier logic runs. There is deliberately no fallback tier — restoring a soft
   default is what let an entire policy layer be bypassed once already.
4. **Every tool body runs inside `BaseTool.execute`.** Timeout, cancellation, error classification,
   secret redaction and event logging live there so an author cannot forget them. Never call `run()`
   directly and never override `execute`.
5. **Web content and `provenance='external'` memory rows are data, never instruction** — no matter
   how authoritative or urgent they sound. Both arrive wrapped in explicit untrusted markers.
6. **Policy lives in `orbit/config/*.yaml`, read at call time.** Never hardcode a profile, tier,
   blocklist entry, or keyterm in Python.
7. **Nothing below the actuation confidence floor moves the mouse without a human's per-action yes.**
   Not a raw coordinate, not a vision guess, not a set-of-mark answer. See the next section.

## The vision tier and the approval path

**This is the product's differentiator, so read this section before touching anything near it.**

`perception_vision_locate` screenshots a window, sends it to the vision model via its own LiteLLM
call, and returns an `ElementRef` with `source="vision"`. It is the only tier that can locate a
control with **no UI Automation representation at all**.

**Vision never auto-actuates, and that has not changed.** Every `ElementRef` it produces carries
`Confidence.VISION_INFERRED` (0.50), below `windows_control_policy.yaml`'s
`min_actuation_confidence` (0.70), so `windows_click`/`windows_drag` refuse it exactly as they refuse
a raw `{x, y}`.

**What is new: a human can now approve one such action.** The confirmation channel is fully wired —
`orbit/confirmation.py` writes a `pending_confirmations` row *before* asking, then asks either the
console (`y/N`) or the GUI approvals drawer, and mints a single-use, short-lived
`approval_token`. `windows_control_tools._require_confidence` accepts that token for a below-floor
element. So a vision-guessed click **does** now have a path to the OS — through an explicit human yes,
one action at a time.

Every constraint on that path is load-bearing, and none of them relaxes the floor:

- the floor is unchanged and the element's confidence is unchanged — nothing raises a score, lowers
  the floor, or special-cases `source="vision"`;
- the token is **minted only on approval**, never on rejection;
- it is **short-lived** — consent was about a screenshot of a *moment*, and replaying it later aims a
  click at a screen that has since changed;
- it is **single-use** and **bound to one row**, so one yes can never authorise a second click;
- it is validated **inside the tool process** via `db.consume_approval_token`, not trusted from the
  caller, so a replayed copy is refused;
- unattended runs **fail closed** — `approval_gui_wait_seconds` defaults to 0, and nobody answering
  is a no.

`tests/test_perception_tools.py::test_vision_sourced_element_ref_is_still_refused_by_actuation` pins
the refusal against the real policy file and resolver, and
`test_a_set_of_mark_result_is_still_refused_by_actuation` locks the same door for set-of-mark answers
— which are *more* tempting to trust because they carry a real UIA rectangle. Do not "fix" either
refusal by raising confidence, lowering the floor, or adding a bypass. The approval channel is the
sanctioned way through, and it is sanctioned precisely because a human looked at the screenshot.

The model's self-reported confidence is recorded under
`element.state["vision"]["model_confidence"]` for debugging and is **never** promoted into the
`confidence` field the gate reads. Same for repeated-sampling agreement: grounding runs 3x on one
image and records `unanimous`/`majority`/`split` under `element.state["vision"]["agreement"]` as a
diagnostic only. Consistency is not correctness — a model can be confidently and repeatably wrong.

**The original spike's dataset does not exist.** The VISION TIER comment block reports 76% over 46
targets on 10 screenshots and states outright that those inputs were never checked in. Verified
absent: nothing outside `venv/`, nothing in `automation_spikes/`, nothing in any commit. That 76% is
a historical note and **not** a baseline anything can be compared against. `benchmarks/` exists to
replace it with a re-runnable one on committed synthetic fixtures — see `benchmarks/CLAUDE.md`,
including why its absolute numbers are not field accuracy.

`perception_find_element` has a `"vision"` tier that is **opt-in only**: it fires when the caller
passes `tier_order=["uia","vision"]` and a `query.description`, never automatically on a UIA miss.
Reasons are on `FindElementTool` — chiefly that it turns a millisecond-scale local lookup into a
hosted model call, and that the two tiers do not take the same kind of input.

## Module map

Every source file and what it is for. 49 files; the ones with no entry here are `__init__.py`.
Directory-level detail lives in that directory's own `CLAUDE.md` (next section).

### `orbit/` — core runtime

| File | Purpose |
| --- | --- |
| `agent.py` | Builds the one `LlmAgent`: model selection, the instruction, and which toolsets the lane gets. `build_agent(lane=…)` is the enforcement point for the foreground gate. |
| `run_task.py` | The single execution path. REPL, one-shot CLI and `--serve` all funnel into `run_task()`. |
| `task_manager.py` | Two-lane scheduler — foreground is a single-flight lock (one mouse), headless a semaphore(5). |
| `policy.py` | `SafetyPlugin`: the allowlist check, tier gate, retry caps, failure classification, and history compaction. Every tool call passes through it. |
| `db.py` | SQLite store — tasks, events, memory, `pending_confirmations`, `ui_memory`. Owns the FTS5 triggers and the approval-token rules. |
| `confirmation.py` | Human-in-the-loop approval for actions the confidence gate refuses. Writes the row, asks console or GUI, mints the single-use token. |
| `degradation.py` | The user-facing message when the provider call dies. |
| `tools/foundation.py` | `BaseTool` / `ToolResult` / `ToolError`. Timeout, cancellation, redaction and event logging live in `execute()` so a tool author cannot skip them. |
| `tools/element_ref.py` | `ElementRef` — the shape perception produces and windows-control consumes. Carries the `confidence` the actuation gate reads. |

### `orbit/mcp_servers/` — the only surface the model can reach

Each server is a thin FastMCP wrapper; the `_tools.py` beside it holds the real `BaseTool` bodies.
That split is deliberate — the server handles protocol, the tools module handles behaviour.

| File | Purpose |
| --- | --- |
| `browser_policy_server.py` / `_tools.py` | Proxies Playwright MCP behind URL policy. Spawns a Playwright subprocess per session. |
| `windows_control_server.py` / `_tools.py` | Real mouse/keyboard actuation. Holds `_require_confidence` — the floor and the approval-token escape. |
| `perception_server.py` / `_tools.py` | Read-only screen observation: UIA tree, screenshots, and the vision tier. |
| `memory_server.py` / `_tools.py` | The agent's access to its own task history. |
| `filesystem_server.py` / `_tools.py` | Scoped read/write inside `data/fs_workspace`. |
| `communication_server.py` / `_tools.py` | Email/calendar surface. `email_send` is blocked. |
| `communication_backend.py` | The swappable `MailBackend`. Today a working local SQLite stand-in; no real inbox. |
| `uia_resolver.py` | The **one** UIA implementation, shared by perception and windows-control so they cannot drift into two lookalike resolvers. |
| `candidate_source.py` | Generates candidate boxes for the vision tier. |
| `mark_overlay.py` | Draws the numbered boxes for set-of-mark prompts, straight into the raw RGB buffer. `benchmarks/raster.py` imports it rather than copying it — one renderer, or the benchmark measures something the tool does not do. |

### `orbit/skills/` — toolset wiring

Each module is a `build_toolset(task_id)` returning a configured `MCPToolset`: which server to spawn,
which `tool_filter` to expose, and how `ORBIT_TASK_ID` reaches the subprocess. A tool absent from a
skill's `tool_filter` is invisible to the model regardless of what the server implements.

| File | Wraps | Loaded in lane |
| --- | --- | --- |
| `memory.py` | memory — the agent's own task history | both |
| `screen_perception.py` | screen-perception — read-only observation | both |
| `devmcp.py` | **an external server outside this repo** — PowerShell + file access | both |
| `windows_control.py` | windows-control — real mouse/keyboard | **foreground only** |
| `research_product.py` | browser-policy — Playwright web research | **headless only** |
| `filesystem.py` | filesystem — scoped `fs_workspace` sandbox | headless only |
| `communication.py` | communication — email/calendar | headless only |

**The two lanes load genuinely different agents, not the same one with a flag.** Foreground adds
windows-control and then *drops* browser-policy, filesystem and communication — deliberately, for two
different reasons. Browser: a foreground task is told to drive real Chrome through windows-control,
so the 14 Playwright tool declarations would burn ~5K tokens per call on tools the instruction
forbids. Filesystem: desktop work uses Dev-MCP against the user's real files, not the scoped
sandbox, so both would be redundant *and* ambiguous about which one to reach for.

`windows_control.py` being absent from headless is the visibility half of the lane gate — a headless
agent has no function declaration for `windows_click` at all, so it cannot call it, full stop.
`devmcp.py` is the one to know about; see Distance to the north star.

### `gui/` — PySide6 dashboard

| File | Purpose |
| --- | --- |
| `main.py` | The window: nav, input card, output pane, overlays, warm-worker plumbing. |
| `theme.py` | Every design token and the master QSS. Widgets import from here; none defines its own colors. |
| `step_tracker.py` | The 220px right-hand step rail. |
| `history_view.py` | History tab — KPI tiles, filters, task list, inspector. |
| `stats.py` | Pure arithmetic behind the KPIs and every duration string. No Qt, so it is testable without a widget tree. |
| `voice.py` | F9 hotkey, Deepgram session, the orb, and the voice modal. |

### `benchmarks/` and `eval/`

`grounding_bench.py` runs the vision accuracy benchmark over `fixtures.py`'s synthetic scenes, drawn
by `raster.py` + `overlay.py`, configured by `config.py`. `eval/run_eval.py` is the separate
end-to-end harness against live sites.

## Where to look

| Read this when you are touching… | File |
| --- | --- |
| `agent.py`, `run_task.py`, `task_manager.py`, `db.py`, `policy.py`, `degradation.py`, `confirmation.py`, or the `lane` gate | `orbit/CLAUDE.md` |
| any tool implementation, the `BaseTool`/`ToolResult`/`ToolError` contract, or `ElementRef` | `orbit/tools/CLAUDE.md` |
| any MCP server, browser sessions, the reaper, filesystem scoping, windows-control actuation, the communication backend, screen-perception, `uia_resolver.py`, untrusted-content wrapping | `orbit/mcp_servers/CLAUDE.md` |
| `MCPToolset` wiring, `tool_filter`, how `task_id` reaches a server subprocess | `orbit/skills/CLAUDE.md` |
| any `*.yaml` under `orbit/config/`, or adding/retiering a tool | `orbit/config/CLAUDE.md` |
| the GUI, the theme tokens, the step rail, voice, or the analytics arithmetic | `gui/CLAUDE.md` |
| writing or fixing a test, or a DB-isolation surprise | `tests/CLAUDE.md` |
| the eval harness or a failing eval case | `eval/CLAUDE.md` |
| vision grounding accuracy, prompt-shape or model comparisons | `benchmarks/CLAUDE.md` |

Design intent and the section numbers the code cites live in
`AI Assistant - System Architecture & Design Spec.md` (Sections 1–14) and
`Claude Code Prompts - Building the MCP Tool Layer.md` (Prompts 0–8). When a docstring says
"Section 7" or "Prompt 4", that is where it points.

## Distance to the north star

Not bugs — the named gaps between what exists and what this is meant to become. Roughly ordered by
how much they block the goal.

- **There is no conversation.** Every goal is an isolated session: `run_task()` builds a fresh
  `InMemoryRunner` and a session keyed by `task_id`, so nothing carries from one goal to the next.
  "Do that again but in Chrome" cannot work today. This is the single largest gap against north-star
  #2, and it is an architectural change rather than a feature — session reuse, a turn history that
  survives a task, and a GUI that shows a thread rather than a one-shot output pane. Note that
  `SafetyPlugin.before_model_callback` already compacts history *within* a task; that machinery is
  the right starting point, not a second one.
- **Vision is the USP but has no trustworthy accuracy number.** The only figure that exists (76%) is
  from a dataset that was never committed. `benchmarks/` is the intended replacement and should be
  run and reported before anyone claims a number publicly.
- **Dev-MCP hardcodes a path outside the repo, and every task loads it.**
  `orbit/skills/devmcp.py` spawns `C:\Users\HP\Desktop\MCP\server.py` — a server that is not in this
  repository, has its own Python 3.14 venv, and exists only on the author's machine. It is wired into
  **both** lanes in `build_agent`, so a fresh clone gets a toolset it cannot start, and the tools it
  exposes (`run_command` — PowerShell — plus `read_file`/`write_file` over arbitrary paths) are among
  the most powerful in the system. Its four tools *are* registered in `risk_tiers.yaml`, so invariant
  3 holds and nothing is bypassing policy. But for the open-source goal this is a hard blocker in two
  ways: the path must be vendored or made configurable, and the capability needs documenting rather
  than arriving as a surprise. This is the largest single obstacle to anyone else running Orbit.
- **No LICENSE and no README.** The project is meant to be open source and currently has neither, so
  it is not actually publishable. A license choice is the user's call, not one to make by default.
- **The communication server has no real mailbox.** `LocalMailBackend` is a genuinely working local
  SQLite stand-in, not stubs — but nothing sent through it reaches a real inbox. Connecting Gmail /
  IMAP needs a human to provision credentials first; that is account access, not something buildable
  unattended. `email_send` is blocked regardless.
- **Model choice is not exposed in the UI.** North star #3 says the user picks the brain, but that
  means editing `.env` today. The GUI has an effort selector and no model selector.
- **Windows-control is one-shot-CLI only.** `--foreground` is not reachable from inside the REPL, and
  the GUI's lane toggle is the only other way in. For an agent whose pitch is "controls your apps",
  that is a narrow door.

## Known open issues

Do not spend a session rediscovering these.

- **`data/orbit.db`'s `memory` table holds only attack payloads.** All rows are
  `provenance='external'` prompt-injection seeds written by `tests/test_adversarial.py`. Nothing in
  real use has ever written a memory row. Do not read it as representative data.
- **`browser_open` inherits the 30s default tool timeout** against a Playwright MCP cold start that
  takes ~60s (`npx -y @playwright/mcp@latest`). The 60s on the toolset's `StdioConnectionParams`
  does not cover it — the tool's own `asyncio.wait_for` is the shorter one.
- **`Confidence.gate()` is never called by the runtime.** Confidence values are recorded on every
  `ToolResult` and thrown away; the three-way rule (>0.90 execute / 0.70–0.90 reverify / <0.70
  surface) exists only in the constant and its unit test.
  `windows_control_tools._require_confidence` implements an adjacent but different check — a flat
  floor plus the approval-token escape, not the three-way split.
- **`db.purge_old_events()` and `close_sessions_for_task()` have no callers.** Both implemented and
  correct; nothing invokes them, so retention never runs.
- **`tasks.source_urls` is never written.** Created as `'[]'` and read back by
  `memory_search_tasks`, so every past task reports no sources.
- **`db.get_daily_cost` has no callers.** Its one caller was the old voice runtime's daily
  transcription cost cap, removed with that code. The rebuilt voice integration does **not** cap
  spend — worth knowing, since Deepgram bills per minute of audio.
- **screen-perception has no OCR tier.** `perception_read_text_region` is not implemented: no OCR
  engine is installed (Tesseract needs a system binary; PaddleOCR/EasyOCR are multi-hundred-MB ML
  stacks). `perception_find_element` honestly reports `"ocr"` in `tiers_unavailable` rather than
  pretending otherwise. The vision tier partly covers the same ground.
- **`run_task.py`'s top-level `except Exception` reports every failure as a provider outage.** A tool
  bug, a DB error and a cancelled coroutine all surface to the user as "the model provider call
  failed". Narrow the catch before trusting that message.
