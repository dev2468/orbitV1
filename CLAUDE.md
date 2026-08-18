# Orbit

Voice-driven personal task agent: the user holds a hotkey and speaks a goal, and Orbit plans and
executes it end to end using browser automation plus its own task history. Built on Google ADK +
LiteLLM with the model reached through NVIDIA NIM. Every tool the model can call is served by an
MCP server sitting behind an ADK safety plugin — the model never touches an in-process tool.

## Stack

- Python **3.13.7** (`venv/`). Load-bearing: it is why TTS is isolated and why some deps are pinned.
- `mcp>=1.24,<2` — mcp 2.x moved `mcp.shared.session`, which breaks google-adk 2.6.3's `MCPToolset`
  import. Installed: mcp 1.29.0, google-adk 2.6.3. Do not unpin.
- **Two venvs.** `venv/` (3.13) runs everything. `venv_tts/` is a separate Python **3.11** holding
  only kokoro+misaki+soundfile, because Kokoro's G2P dep `misaki[en]` has never supported 3.13.
  It is spoken to as a subprocess, never imported. See `orbit/voice/CLAUDE.md` before touching it.
- SQLite at `data/orbit.db` (WAL). Playwright MCP via `npx`. PySide6 for the dashboard. `mss` for
  screen-perception's screenshots (pure-Python, no system binary — not the catalog's named DXCam).

## Commands

Always `venv\Scripts\python.exe` — the venv is not on PATH, so a bare `python` is the wrong
interpreter or none at all. Everything runs from the project root.

```
venv\Scripts\python.exe -m orbit.run_task find the cheapest 65 inch tv   # run a task (quoting optional)
venv\Scripts\python.exe -m orbit.run_task --foreground open notepad ...  # opts into lane=foreground — the ONLY way windows-control tools are reachable
venv\Scripts\python.exe -m orbit.run_task --list-models                  # known-good models + active one
venv\Scripts\python.exe gui\main.py                                      # read-only task dashboard
venv\Scripts\python.exe -m orbit.voice                                   # voice runtime; hold ctrl+space
venv\Scripts\python.exe -m pytest tests\ -q                              # 133 tests (some hit the network; a live-UI windows-control test is opt-in, see tests/CLAUDE.md)
venv\Scripts\python.exe -m eval.run_eval                                 # eval harness against live sites
```

## Architecture

```
hotkey (pynput global OS hook)  ──▶ capture (sounddevice 16k mono)
                                        │
                                        ▼
                          transcriber (Deepgram ws │ faster-whisper)
                                        │  transcript
                                        ▼
                      run_task(title, goal) ── db.create_task ─▶ tasks row
                                        │
                                        ▼
              TaskManager.submit(lane)   foreground: single-flight lock
                                         headless:  semaphore(5)
                                        │
                                        ▼
                    ADK InMemoryRunner + SafetyPlugin (policy.py)
                                        │  before / after / on_error
                                        ▼
                MCPToolset ── stdio subprocess, env={ORBIT_TASK_ID}
                     ├──▶ browser-policy server ────▶ Playwright MCP subprocess
                     ├──▶ memory server ────────────▶ orbit/db.py
                     ├──▶ filesystem server ────────▶ data/fs_workspace (scoped)
                     ├──▶ windows-control server ───▶ real mouse/keyboard (lane="foreground" only)
                     ├──▶ communication server ─────▶ swappable MailBackend (today: local SQLite stand-in)
                     └──▶ screen-perception server ─▶ read-only: UIA tree, screenshots (mss)
```

windows-control and screen-perception are separate processes (perception free/read-only, actuation
gated — Section 11) but share one UIA implementation, `orbit/mcp_servers/uia_resolver.py`, so the two
never drift into two different resolvers that happen to look similar. `ElementRef`
(`orbit/tools/element_ref.py`) is the shape both produce/consume — Contract 3.

## Invariants

These hold no matter which file you are in.

1. **Everything reaches the model through an MCP server.** A tool that is not exposed by
   `orbit/mcp_servers/` is not reachable by the agent, by construction.
2. **Every tool call goes through `SafetyPlugin`** (`orbit/policy.py`). There is no second path
   into a tool and no "just this once" bypass.
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

## Where to look

| Read this when you are touching… | File |
| --- | --- |
| `agent.py`, `run_task.py`, `task_manager.py`, `db.py`, `policy.py`, `degradation.py`, or the `lane` gate | `orbit/CLAUDE.md` |
| any tool implementation, the `BaseTool`/`ToolResult`/`ToolError` contract, or `ElementRef` | `orbit/tools/CLAUDE.md` |
| any MCP server, browser sessions, the reaper, filesystem scoping, windows-control actuation, the communication backend, screen-perception, `uia_resolver.py`, untrusted-content wrapping | `orbit/mcp_servers/CLAUDE.md` |
| `MCPToolset` wiring, `tool_filter`, how `task_id` reaches a server subprocess | `orbit/skills/CLAUDE.md` |
| any `*.yaml` under `orbit/config/`, or adding/retiering a tool | `orbit/config/CLAUDE.md` |
| anything under `orbit/voice/` — **read it before editing any threading** | `orbit/voice/CLAUDE.md` |
| the PySide6 dashboard | `gui/CLAUDE.md` |
| writing or fixing a test, or a DB-isolation surprise | `tests/CLAUDE.md` |
| the eval harness or a failing eval case | `eval/CLAUDE.md` |

Design intent and the section numbers the code cites live in
`AI Assistant - System Architecture & Design Spec.md` (Sections 1–14) and
`Claude Code Prompts - Building the MCP Tool Layer.md` (Prompts 0–8). The code refers to both by
number; when a docstring says "Section 7" or "Prompt 4", that is where it points.

## Known open issues

Do not spend a session rediscovering these.

- **Not a git repository.** No history, no branches, nothing to diff against. `.gitignore` exists
  and is ready, but `git init` has never been run.
- **`data/orbit.db`'s `memory` table holds only attack payloads.** All 3 rows are
  `provenance='external'` prompt-injection seeds written by `tests/test_adversarial.py`. Nothing
  in real use has ever written a memory row. Do not read it as representative data.
- **`browser_open` inherits the 30s default tool timeout** against a Playwright MCP cold start
  that takes ~60s (`npx -y @playwright/mcp@latest`). The 60s on the toolset's
  `StdioConnectionParams` does not cover it — the tool's own `asyncio.wait_for` is the shorter one.
- **`Confidence.gate()` is never called by the runtime.** Confidence values are recorded on every
  `ToolResult` and thrown away; the gating rule (>0.90 execute / 0.70–0.90 reverify / <0.70
  surface) exists only in the constant and its unit test. `windows_control_tools._require_confidence`
  reimplements an adjacent but not identical check (a flat `>= min_actuation_confidence` floor, not
  the three-way execute/reverify/surface split) — `gate()` itself still has no real caller.
- **`db.purge_old_events()` and `close_sessions_for_task()` have no callers.** Both are implemented
  and correct; nothing invokes them, so retention never runs.
- **`tasks.source_urls` is never written.** It is created as `'[]'` and read back by
  `memory_search_tasks`, so every past task reports no sources.
- **screen-perception has no OCR or vision tier.** `perception_find_element` only resolves the UIA
  tier — a control with no UI Automation representation (some custom-drawn UI) simply cannot be
  resolved by `perception_find_element` or targeted by `windows_click`/`windows_drag` in this build.
  `perception_read_text_region` (OCR) and `perception_vision_locate` are not implemented at all: no
  OCR engine is installed (Tesseract needs a system binary; PaddleOCR/EasyOCR are multi-hundred-MB ML
  stacks — a bigger, more consequential install than the small pure-Python `mss` library actually
  added), and the vision-tier tool's own catalog entry explicitly says not to guess its signature
  ahead of a grounding spike that has never run in this build. See
  `orbit/mcp_servers/perception_tools.py`'s module docstring.
- **The communication server has no real mailbox connected.** `LocalMailBackend`
  (`orbit/mcp_servers/communication_backend.py`) is a genuinely-working local SQLite stand-in, not a
  pile of stubs, but nothing sent through it reaches a real inbox and nothing read through it came from
  one — it's seeded only by what the agent itself drafts/sends via this same tool layer. Connecting a
  real backend (Gmail/Calendar API via OAuth, or IMAP/SMTP via an app password) needs a human to
  provision credentials first; that's account access, not something buildable unattended. `email_send`
  is blocked regardless either way — see that module's docstring.
