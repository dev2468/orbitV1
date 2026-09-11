# gui/ — PySide6 dashboard (Studio design direction)

Nine modules. `theme.py` holds every token, `main.py` is the window, `step_tracker.py` the right-hand
step rail, `history_view.py` the History tab, `voice.py` the F9 voice pipeline and its modal,
`speech.py` the voice going back out, `ack_controller.py` the fast reply that voice speaks, `spend.py` the daily voice budgets, and
`stats.py` the pure arithmetic behind the analytics.

`gui/` has no `__init__.py`; `gui.main` resolves as a namespace package.

## Design direction: Studio (warm / organic)

The palette is warm-neutral (stone), not cool-neutral. Canvas `#F8F5F0`, surface `#FDFCFA`, borders
`#E8E2D9`, text `#1C1917`/`#78716C`/`#A8A29E`. Indigo `#6366F1` is the single cool hue and the brand
accent; its scarcity against all that warmth is what makes it read as "active".

**Shadows are warm too** — `theme.apply_drop_shadow` always tints them `rgba(100, 80, 40, …)`. A
neutral-black shadow over a cream surface greys the penumbra and reads as dirt rather than depth. If
you add an elevation, go through that helper rather than building a `QGraphicsDropShadowEffect` by
hand.

Full rationale in `theme.py`'s module docstring. Never hardcode a color in a widget module — every
one of them imports from `theme`.

## The QSS trap that will bite you: bare properties are inherited

`widget.setStyleSheet("border-bottom: 1px solid #E8E2D9;")` applies that border to the widget **and
every descendant**. A header styled that way underlines each of its own labels individually. This
happened three times during the redesign (drawer header, KPI wrapper, inspector header).

**Always use an ID selector on a container:** give it `setObjectName("thing")` and write
`#thing { border-bottom: ... }`. Only leaf widgets get bare property lists.

Second trap: a plain `QWidget` **ignores** a QSS `background` unless `WA_StyledBackground` is set.
Use `QFrame` + an ID selector instead — that is why `SegmentedToggle` is a QFrame.

## Layout: two columns, and progress lives outside the reading column

The Workbench is a reading column (input card, quick chips, output pane) plus a fixed 220px step rail
on the right. The rail is **always visible**, placeholder and all.

That is load-bearing, not decoration. The tracker used to sit inline above the output, so it pushed
the output down and resized it on every step — the text you were reading jumped while you read it.
As a fixed-width rail the output never reflows. `StepTracker.reset()` therefore returns to a
placeholder rather than hiding, and `test_step_tracker.py` asserts on `placeholder.isHidden()` /
`scroll_area.isHidden()` rather than on the tracker's own visibility.

Connector color encodes the **next** step's status (green once done, indigo while running, stone
while pending), so the rail reads as a filling pipe rather than a column of disconnected dots.

## Overlays are children of the central widget, never QDialogs

The voice modal and the approvals drawer are plain widgets reparented onto `central`, `raise_()`d
above a `ScrimWidget`, and positioned by `_layout_overlays()` (called from `resizeEvent` and on show).

Both reasons matter:

- **the voice modal must not take the keyboard** — F9 has to keep reaching the app's native event
  filter to stop the recording it started. A real modal dialog would swallow it.
- **the drawer used to animate a layout width**, which reflowed the whole workbench beside it on
  every frame. As an overlay it composites over a static page and animates `pos` instead.

`ScrimWidget` paints its dim in `paintEvent` rather than via QSS `rgba()`, which does not composite
reliably against sibling widgets.

## Voice has two exits and they are not the same

`VoiceController.toggle()` commits (ends the session, emits `transcript_ready` with the text).
`VoiceController.cancel()` discards (sets `_cancelled`, so the session thread emits `session_stopped`
**without** `transcript_ready`). Esc, the scrim, and the modal's Cancel button all take the second
path; F9-again and "Use transcript" take the first.

Do not collapse these into one. `transcript_ready("")` would be indistinguishable from a silent
recording the user did mean to keep, and Esc must never drop half-heard audio into the goal box.

## Task submission: one warm worker, not a subprocess per task

`_ensure_worker()` spawns `orbit.run_task --serve` once and feeds it one JSON line per goal;
`[TASK:DONE <exit_code>]` on stdout ends a task without ending the process. This removes ~7-8s of
Python import + event-loop cold start from every task after the first.

The sentinel is matched in `_read_stdout` and **deliberately not appended to `_raw_buffer`**, which
`_render_final_output` re-parses — letting it through would print the marker in the rendered result.
`_task_done` guards on `self._task_running` because both the sentinel and `QProcess.finished` can
arrive for the same task (Stop kills the worker); the guard makes the UI restore exactly once.

## Progress arrives as events now, not as prose the model remembered to print

The worker interleaves `[ORBIT]{json}` lines with its normal output, one per thing that happens, and
`_handle_orbit_event` acts on them: `tool_call` opens a step, `tool_result` closes it, `text_delta`
types the answer into the pane, `result` unlocks the input. Like the `[TASK:DONE]` sentinel these
lines are kept out of `_raw_buffer`, for the same reason — the final render would print raw JSON.

**This replaced a step rail built from `[STEP:*]` markers the model was instructed to print.** Two
things were wrong with that. The markers were only as reliable as the model's compliance, and they
could not arrive at all until the task was over, because the worker used ADK's `run_debug`, which
buffers every event and returns them at the end. Measured on a two-phase task: the first tool call
was knowable at +8.5s and the GUI learned about it at +12.9s along with everything else.
`_STEP_RE` and `handle_marker` are still here so a stored transcript containing markers renders, but
nothing emits them any more — the prompt no longer asks for them.

`result` is emitted by the worker *before* it tears down its six MCP subprocesses (2-3s), and
`_on_result_ready` unlocks on it rather than waiting for `[TASK:DONE]`. Those seconds used to be
spent looking at a finished answer with a dead input box.

`_EVENT_PREFIX` is copied from `orbit.run_task.EVENT_PREFIX` rather than imported, because importing
that module would pull litellm and google-adk (10.6s of imports) into the GUI process.
`test_progress_events.py` asserts the two copies match.

**Standing rule, unchanged: this process never writes task or event rows to `orbit.db`.**
`TaskManager` owns the in-memory task/token registry and a direct write would desync it.

## The acknowledgement track: `ack_controller.py` + `speech.py`

`_submit_task` starts **two** things, and the acknowledgement goes first —
before the worker is even written to. That ordering is the whole feature: the fast reply is spoken
at ~2.0s while the worker is still connecting MCP servers for the same goal. Anything that makes
`self._ack.start(...)` wait on the work track defeats it.

`AckController` runs `orbit.ack` on a daemon thread and cancels by **generation counter**, not by
killing the thread — an HTTP read cannot be interrupted safely mid-stream, so a superseded run
finishes and its output is discarded. One wasted short completion beats a torn connection that the
*next* acknowledgement would pay to re-establish.

`SpeechPlayer` is a queue, not a function. Two costs drove its shape, both measured here:

- **the first HTTPS request costs ~0.9s** against 0.27s warm — and abandoning a response part-way
  leaves the pooled connection unusable, so `prewarm()` drains its throwaway response to the end.
  That looks wasteful and is the point.
- **`sd.RawOutputStream` open+start costs 0.90s**, so the output stream is opened once and held,
  closing after `_IDLE_CLOSE_SECONDS` of silence rather than per utterance.

Together those took first-audio from 1.39s to 0.30s. Both prewarms are skipped when
`ORBIT_DISABLE_PREWARM` is set, which `tests/conftest.py` does — otherwise every GUI test would make
a real network call and grab the machine's audio device.

Aura bills per character and **nothing else in this codebase watches that** (`db.get_daily_cost` has
no callers), so `SpendGuard` keeps its own daily budget in `data/voice_usage.json` (see `gui/spend.py`, which guards microphone seconds too). Deliberately not an
events row — see the standing rule above.

**A transcript submits itself** (`_AUTO_SUBMIT_MS`, 900ms). `_on_transcript_ready` arms a timer
rather than waiting for Enter — requiring a keypress after speaking meant every spoken goal ended at
the keyboard, which is the thing voice exists to avoid.

**The work track is held until the ack classifies the turn** — `_defer_work` / `_dispatch_pending_work`
/ `_on_ack_classified`. `hold_full` is passed explicitly rather than derived from `hold_ms`, because
the two durations do not order the way the intent does: the headless guard (3s) is deliberately
*longer* than the foreground hold (2.5s), one being a rarely-reached ceiling and the other a window
that always elapses. Inferring intent from duration got this backwards and the tests caught it.

**A CHAT turn ends on `_on_ack_completed`, not on `_on_ack_classified`.** Classification fires on the
stream's first tokens with the sentence still arriving, and `_task_done` re-renders the pane
wholesale — finishing early rendered half a sentence and appended the rest underneath.

**Speech is spoken only for goals that arrived by voice.** `_on_transcript_ready` sets
`_voice_originated`; `_submit_task` latches it into `_spoken_submission` and clears it, so clearing
the box mid-task cannot change whether the reply already in flight gets spoken. The acknowledgement
itself runs for typed goals too — it just appears on screen instead.

The ack is streamed into the output pane live and **kept out of `_raw_buffer`**, so
`_render_final_output` re-adds it from `_ack_text`. Without that the fast reply vanishes the moment
the slow one lands.

## The step rail: phases, and alive from submission

Two things were wrong and both are worth not re-introducing.

**One step per tool call made an ordinary browse render ~48 rows.** Tools now map to a small set of
plain-language PHASES (`_TOOL_PHASES`), each occupying at most one row; a repeat re-activates that
row and bumps a `×N` counter. The same 41-call browse renders 5. Targets are ≤5 rows for a simple
task, 7-12 for a complex one, and `test_a_realistic_browse_stays_readable` pins the ceiling. When
adding a tool, name the **outcome** ("Reading the page"), not the mechanism ("browser_snapshot").

**The rail used to be empty for the part of a task where feedback matters most.** Events were always
live — that was fixed on 2026-09-07 — but there is nothing to show before the model's first tool
call, and that gap measured 18.5s on a cold worker. `begin_task()` now opens a running "Working out
what to do" step at submission, closed by the first real tool call. Its elapsed timer ticks every
second, so even an idle rail is visibly alive.

## The output pane is a thread

Finished turns are kept as rendered HTML in `_turn_html` and re-rendered above the live one, so a
conversation reads as a conversation. It used to `clear()` on every submission, which made
"conversational" true of the model and false of the screen.

Kept as rendered HTML rather than re-derived from the DB, because the pane should show what was
actually shown — including the streamed acknowledgement, which no table records. Bounded at
`_MAX_THREAD_TURNS` (20): `QTextEdit` re-lays out its whole document on `setHtml`, so an unbounded
thread makes every later turn slower to render. Full history lives in the History tab regardless.

Starting a new chat sends `{"close_conversation": ...}` to the worker before clearing the thread —
otherwise the abandoned conversation's runner keeps six MCP subprocesses and a browser alive with
nothing ever coming back to them.

## Resuming an earlier chat

"Recent ▾" in the nav bar lists the last 12 conversations; picking one calls `_resume_conversation`,
which points `_conversation_id` at it and rebuilds `_turn_html` from `db.conversation_turns`.

**The worker needed nothing for this.** Sending a goal with an existing conversation_id already
replayed that conversation's stored turns into the prompt (`run_task._build_conversation_context`),
which is the same bridge used after a worker restart — so resuming worked before any of this UI
existed. All the GUI adds is putting the thread back on screen.

Replayed turns go through the same `_turn_html_for` the live path uses. Building them separately is
how the two drift into looking different, which would make a resumed chat feel like a transcript
rather than the chat.

Three things that are deliberate:

- **A resumed chat is lower fidelity than one you never left.** A live conversation continues the
  real ADK session — actual tool calls, arguments, results. A resumed one gets a summary with
  results clipped to 500 chars and only the last `_MAX_CONTEXT_TURNS` (8) replayed. "Do that again"
  works; "why did that fail" will not have the error text.
- **A turn with no goal is skipped.** An interrupted task leaves a row with nothing in it, and a
  blank card in the thread reads as a rendering bug rather than an abandoned turn.
- **Resuming is refused mid-task**, and releases the previous conversation's runner on the way out —
  each one holds six MCP subprocesses and possibly a browser.

The menu is rebuilt on `aboutToShow` rather than on a timer: the query is cheap, and a stale picker
is the one thing that makes this feel broken.

## A finished task reports HOW it finished

`_status_card_html` reads `_result_status` / `_result_text`, captured from the `result` event, and
falls back to the exit code only when no event arrived. That distinction is the point: `[TASK:DONE 1]`
cannot tell a provider outage from a cancel from a tool bug, and rendering all three as
"Task failed (exit 1)" told the user none of it. A cancel is styled neutral, not red — the user asked
for it, and colouring it as an error says otherwise.

## The confirmation channel is the one deliberate DB-write exception

The approvals drawer renders the oldest `pending_confirmations` row — its stored screenshot with the
candidate box drawn over it — and offers Approve / Reject.

**Why writing is allowed here when task submission is not:** `pending_confirmations` has no in-memory
owner. The waiting process is *polling that table* for an answer, so the table IS the channel. The
buttons call `db.resolve_pending_confirmation` and **nothing else** — no task rows, no event rows, no
status changes. That restriction is what keeps this safe; do not widen it.

Approving mints a short-lived single-use token (`approval_token_ttl_seconds`). It does **not** raise
the target's confidence or lower `min_actuation_confidence` — a second click needs a second yes.

A `KeyError` on resolve is swallowed: the row was already decided by the REPL asker or a second
dashboard, and refreshing shows the truth. That is a race, not an error — and it is what stops a
REJECTED row from later being flipped to APPROVED.

The waiting side only listens when `approval_gui_wait_seconds` is non-zero. The code default is
**0**, so a config without the key fails closed immediately; the shipped YAML sets **30**, so an
unanswered request fails closed after 30 seconds. Set it back to 0 for unattended runs (eval, CI, a
scheduled task), where a hang is worse than a refusal. Keep it on
when a human is actually watching this window.

## Analytics arithmetic lives in `stats.py`, away from Qt

`compute_kpis`, `format_duration`, `relative_time` and friends import nothing from PySide6, so
`tests/test_stats.py` pins them without standing up a widget tree. Two decisions there are easy to
"fix" wrongly:

- **Success rate is over *decided* tasks (completed + failed), not all rows.** Counting a RUNNING
  task as a non-success makes the number sag mid-task and jump when it lands, which reads as a bug.
- **Average duration excludes unfinished tasks.** Their partial elapsed time would drag it toward
  zero.

Both return `None` when there is nothing to divide by, so the tile renders `—` rather than a
confident-looking `0%`.

`parse_ts` normalises naive timestamps to UTC rather than trusting the string: comparing a naive
timestamp against an aware `now()` raises `TypeError`, and one bad row would otherwise take out the
entire History tab.

## History list: titles must elide, filters must not compress

Task titles are whole goal sentences. A plain `QLabel` reports the full width as its sizeHint, the
row grows to match, and the scroll area scrolls horizontally — every title runs off the panel edge
and is cut mid-word with no ellipsis. `_ElidedLabel` elides at paint time and takes
`QSizePolicy.Ignored` horizontally so the layout may shrink it. It paints its color explicitly
because QSS `color` does not reliably reach `palette()`.

Filter pills are on **two rows** (status, then lane). Six pills plus a divider need ~400px of natural
width and the panel is splitter-resizable, so on one row `QHBoxLayout` compresses them below their
text width and labels clip mid-word.

## Two entry points, one sys.path hack

`python gui\main.py` puts `gui/` on `sys.path` rather than the project root, so `from orbit import db`
would fail. The explicit `sys.path.insert` of the project root at the top of `main.py` is what makes
both `venv\Scripts\python.exe gui\main.py` and `venv\Scripts\python.exe -m gui.main` work. It has to
stay above the `orbit` import — keep the ordering even though it looks like something a formatter
should fix.

## Framework choice

PySide6 over Tauri: one language matching the Python backend, no IPC bridge to stand up. Tauri
remains the better end state per the tech-stack review — revisit when there is time to build the
bridge, not as a drive-by.
