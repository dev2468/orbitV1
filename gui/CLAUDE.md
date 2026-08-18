# gui/ — PySide6 dashboard

One file, `main.py`, ~96 lines. A `QMainWindow` holding a `QTableWidget` of tasks.

## It is read-only, and that is a design property

Per the architecture spec's Section 3 the dashboard is a "window into runtime state only". It does
not drive the agent, submit tasks, cancel them, or write to the database. It calls `db.init_db()`
and `db.list_tasks()` on a 2s `QTimer` and repaints. That is the whole program.

The refresh loop exists because tasks are normally started from a *different process* —
`orbit.run_task`, `eval.run_eval`, or the voice runtime — so the dashboard has no in-process signal
to react to and polls the shared SQLite file instead. The table is also
`setEditTriggers(NoEditTriggers)`: cells cannot be edited into the DB.

**If you add a control here, do not write to `orbit.db` from this process.** Cancellation and
submission belong to `TaskManager`, which owns the in-memory task/token registry — a direct DB write
would change a row's status without touching the running coroutine, leaving the two out of sync.
Any control path has to reach the owning process, which does not exist yet.

## The confirmation channel that isn't built

This is the natural home for the missing piece: high-risk tools are currently blocked outright with
`confirmation_required` rather than queued for approval, precisely because there is no channel a
human decision can arrive on. When one is built it wires into
`SafetyPlugin.before_tool_callback` (`orbit/policy.py`) and it must originate here — the whole point
is that a human saw the actual action. Until then, do not relax the block to make the GUI simpler.

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
