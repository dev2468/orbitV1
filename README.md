# Orbit

**An open-source personal agent for Windows that you own and control.**

Speak a goal. Orbit answers in about two seconds, then goes and does it — across your browser, your
real desktop applications, your files, and its own memory of what it has done before.

It runs on your machine, against whatever model you point it at.

---

## What makes it different

**It sees the screen.** Most desktop agents drive applications through the accessibility tree, which
works right up until it doesn't: games, `<canvas>` apps, and custom-drawn controls expose nothing at
all. Orbit has a vision tier that looks at actual pixels and finds the control anyway.

Measured on committed synthetic fixtures (2026-09-08), 12 targets per arm:

| prompt shape | model | hit rate | median miss |
| --- | --- | --- | --- |
| freeform point | gemma-3-27b-it | 17% | 122 px |
| set-of-mark | gemma-3-27b-it | 58% | 96 px |
| freeform point | gemini-2.5-flash | 67% | 23 px |
| **set-of-mark** | **gemini-2.5-flash** | **83%** | **34 px** |

Run it yourself: `venv\Scripts\python.exe -m benchmarks.grounding_bench`

> These are synthetic scenes drawn in code, which buys exact ground truth and reproducibility at the
> cost of real Windows chrome. Read them as *"set-of-mark beat freeform by 16 points on this set"*,
> not as a field-accuracy claim. See [`benchmarks/CLAUDE.md`](benchmarks/CLAUDE.md).

**A vision guess never moves your mouse on its own.** Every vision-located element scores below the
actuation floor, so `windows_click` refuses it — exactly as it refuses a raw `{x, y}`. The only way
through is a human looking at the screenshot and approving that one action, which mints a
single-use, short-lived token bound to that one click. Unattended runs fail closed.

**It talks back.** A spoken goal gets a real reply in ~2 seconds while the heavy machinery is still
starting, then a spoken summary when the work is done.

---

## Quick start

```bash
venv\Scripts\python.exe gui\main.py
```

Press **F9**, say what you want, and let go. That's it.

Other entry points:

```bash
venv\Scripts\python.exe -m orbit.run_task                 # interactive REPL
venv\Scripts\python.exe -m orbit.run_task find a cheap 65 inch tv   # one-shot
venv\Scripts\python.exe -m orbit.run_task --list-models   # the model catalog
venv\Scripts\python.exe -m pytest tests\ -q               # 552 tests
```

---

## Setup

1. **Windows.** Not portable, and not incidentally so — desktop control uses `pywinauto`/`pywin32`
   and the UI Automation tree.
2. **Python 3.13**, one venv at `venv/`:
   ```bash
   venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
3. **Node**, for `npx` — the browser tools spawn `@playwright/mcp` on demand.
4. **`.env` in the project root:**
   ```
   OPENROUTER_API_KEY=...     # required — every model call goes through it
   DEEPGRAM_API_KEY=...       # optional — voice in and out. Without it, Orbit is text-only
   ```

Everything else is optional and documented in [`CLAUDE.md`](CLAUDE.md) — model overrides, voice
selection, daily spend caps, timing knobs.

---

## How it works

```
  spoken goal ──▶ transcript ──▶ submits itself
      │
      ├──▶ ACK TRACK    small model, no tools, ~400-token prompt
      │      classifies the turn, replies in ~1s, spoken at ~2.2s
      │         └── purely social? the work track never runs at all
      │
      └──▶ WORK TRACK   the real agent — 44 tools, MCP servers, safety plugin
             answer in ~5-12s ──▶ spoken summary
```

Two tracks, because they answer different questions on different timescales. The fast one answers
*"what did I just hear"*; the slow one actually does the job. Neither can break the other.

Underneath, **every tool the model can call is served by an MCP server behind a safety plugin** —
there is no in-process tool and no second path. A tool name absent from
`orbit/config/risk_tiers.yaml` is blocked before tier logic even runs.

Seven toolsets: browser (behind a URL policy), memory, filesystem (sandboxed), windows-control
(real mouse and keyboard, foreground lane only), communication, screen-perception (read-only), and
Dev-MCP.

Full architecture, invariants, and the reasoning behind every non-obvious decision live in
[`CLAUDE.md`](CLAUDE.md) and the per-directory `CLAUDE.md` files.

---

## Model-agnostic by construction

Pick the brain from the GUI's model selector, or set `ORBIT_MODEL`. Everything routes through
OpenRouter, so one credential reaches every model in `orbit/models.py`.

Three models are chosen independently, because they do different jobs:

| | setting | default |
| --- | --- | --- |
| the agent | `ORBIT_MODEL` / GUI selector | `google/gemini-2.5-flash` |
| the fast reply | `ORBIT_ACK_MODEL` | `google/gemini-2.5-flash` |
| the vision tier | `_VISION_MODEL` | `google/gemini-2.5-flash` |

> Careful with reasoning models as the agent. `gemini-3.7-flash` cannot have thinking disabled on
> OpenRouter, and it costs ~3s on *every turn of the loop* — including "hi". Measure before you
> switch; `CLAUDE.md` has the numbers.

---

## Roadmap

- **Real-screenshot vision fixtures** to complement the synthetic set.
- **A real mailbox** — the communication server works against a local stand-in today.
- **Real sandboxing for `run_command`** — the PowerShell policy is a blocklist, which is a
  guard rail rather than containment.

---

## Safety

Orbit has real mouse and keyboard control, so its safety story *is* the product:

- Every tool call passes through one plugin. High-risk tools are blocked, not auto-approved.
- Anything read through a tool — web pages, files, memory rows marked external — is wrapped as
  untrusted data, never instruction.
- Nothing below the actuation confidence floor moves the mouse without a per-action human yes.
- Voice auto-submit holds the work track in the foreground lane until the spoken acknowledgement has
  landed, so a misheard goal can be cancelled with Esc before anything touches the OS.

Please don't widen any of that for convenience.

---

## License

MIT — see [LICENSE](LICENSE).
