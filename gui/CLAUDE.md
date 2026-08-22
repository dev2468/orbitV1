# gui/ — PySide6 dashboard

One file, `main.py`, ~96 lines. A `QMainWindow` holding a `QTableWidget` of tasks.

## It is read-only, and that is a design property

Per the architecture spec's Section 3 the dashboard is a "window into runtime state only". It does
not drive the agent, submit tasks, cancel them, or write to the database. It calls `db.init_db()`
and `db.list_tasks()` on a 2s `QTimer` and repaints. That is the whole program.

The refresh loop exists because tasks are normally started from a *different process* —
`orbit.run_task` (one-shot or its REPL) or `eval.run_eval` — so the dashboard has no in-process
signal to react to and polls the shared SQLite file instead. The table is also
`setEditTriggers(NoEditTriggers)`: cells cannot be edited into the DB.

**If you add a control here, do not write to `orbit.db` from this process.** Cancellation and
submission belong to `TaskManager`, which owns the in-memory task/token registry — a direct DB write
would change a row's status without touching the running coroutine, leaving the two out of sync.
Any control path has to reach the owning process, which does not exist yet.

## The confirmation channel, now built

`ConfirmationPanel` renders the oldest row in `pending_confirmations` — its stored screenshot with
the candidate box drawn over it — and offers Approve / Reject. This is the piece this file used to
say was missing, and it is here for the stated reason: the whole point is that **a human saw the
actual action**.

**Why writing to the DB is allowed here, when cancellation still is not.** The rule above stands:
task status is owned by `TaskManager`'s in-memory registry, so a direct write would desync the two.
`pending_confirmations` has no in-memory owner — the waiting process is *polling that table* for an
answer, so the table IS the channel, exactly like the task list is the channel for display. The
buttons call `db.resolve_pending_confirmation` and **nothing else**: no task rows, no event rows, no
status changes. That restriction is what keeps this safe, so do not widen it.

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

`COLUMNS` drives both the header and the per-row lookup via `task.get(key)`, so adding a column is a
one-line change *provided* the name matches a `tasks` table column exactly. A typo yields a silently
empty column rather than an error. `failure_reason` is deliberately shown — a failed task must
surface its plain-language reason, not just a red status.
