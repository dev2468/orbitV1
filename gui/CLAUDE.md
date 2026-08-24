# gui/ — PySide6 unified dashboard

One file, `main.py`. White background, blue accents, Fusion style. Four panels: goal input with lane
selector, live output, task history table, and the confirmation channel.

## Task submission spawns a subprocess, not an in-process call

The "Send" button spawns `venv\Scripts\python.exe -m orbit.run_task [--foreground] <goal>` via
`QProcess`. Stdout/stderr are merged and streamed into the live output panel. "Stop" kills the
process. This respects the standing rule: **do not write task/event rows to `orbit.db` from this
process.** `TaskManager` owns the in-memory task/token registry; a direct write would desync.

The one exception remains `ConfirmationPanel`, which writes to `pending_confirmations` only — see
below.

## The confirmation channel

`ConfirmationPanel` renders the oldest row in `pending_confirmations` — its stored screenshot with
the candidate box drawn over it — and offers Approve / Reject.

**Why writing to the DB is allowed here, when task submission is not.** Task status is owned by
`TaskManager`'s in-memory registry, so a direct write would desync the two. `pending_confirmations`
has no in-memory owner — the waiting process is *polling that table* for an answer, so the table IS
the channel. The buttons call `db.resolve_pending_confirmation` and **nothing else**: no task rows,
no event rows, no status changes. That restriction is what keeps this safe, so do not widen it.

Approving mints a short-lived single-use token (`approval_token_ttl_seconds`). It does **not** raise
the target's confidence or lower `min_actuation_confidence` — a second click needs a second yes.

The waiting side only listens when `approval_gui_wait_seconds` is non-zero, and it defaults to **0**.
That is deliberate: a non-zero wait makes every unattended run (eval, CI, a scheduled task) block for
that long on each confirmation before failing closed, turning a fast honest refusal into a hang. Turn
it on when a human is actually watching this window.

A `KeyError` on resolve is swallowed: it means the row was already decided — by the REPL asker, or a
second dashboard — and refreshing shows the truth. That is a race, not an error.

## Two entry points, one sys.path hack

`python gui\main.py` puts `gui/` on `sys.path` rather than the project root, so `from orbit import
db` would fail with `ModuleNotFoundError`. The explicit `sys.path.insert` of the project root at the
top is what makes both `venv\Scripts\python.exe gui\main.py` and
`venv\Scripts\python.exe -m gui.main` work. It has to stay above the `orbit` import — keep the
import ordering as-is even though it looks like something a formatter should fix.

`gui/` has no `__init__.py`; `gui.main` resolves as a namespace package.

## Framework choice

PySide6 was chosen for this build over Tauri: single language, matching the Python backend directly,
no IPC bridge to stand up. Tauri remains the better end state per the tech-stack review — revisit
when there is time to build the bridge, not as a drive-by.

## Columns

`TASK_COLUMNS` is a list of `(header, db_key)` tuples. Adding a column is a one-line append provided
the db_key matches a `tasks` table column exactly. A typo yields a silently empty column rather than
an error. `failure_reason` is deliberately shown — a failed task must surface its plain-language
reason, not just a red status. `task_id` is deliberately hidden from the table (it's noise for the
user) but still readable via the DB.
