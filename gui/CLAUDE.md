# gui/ — PySide6 dashboard (Studio design direction)

Six modules. `theme.py` holds every token, `main.py` is the window, `step_tracker.py` the right-hand
step rail, `history_view.py` the History tab, `voice.py` the F9 voice pipeline and its modal, and
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

**Standing rule, unchanged: this process never writes task or event rows to `orbit.db`.**
`TaskManager` owns the in-memory task/token registry and a direct write would desync it.

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

The waiting side only listens when `approval_gui_wait_seconds` is non-zero, and it defaults to **0**,
so every unattended run (eval, CI, a scheduled task) fails closed fast instead of hanging. Turn it on
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
