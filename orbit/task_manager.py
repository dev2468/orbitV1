"""Two-lane concurrency scheduler — Section 9 of the architecture spec.

Headless lane: parallel, bounded only by a soft concurrency cap (cost
protection, not a correctness requirement — see Section 14.8).
Foreground lane: strictly single-flight, queued — one OS input-simulating
task at a time, because there's one mouse and one keyboard.

Cancellation (Section 9's "must be interruptible mid-action"): every
submitted task gets a CancellationToken. Cancelling calls both the
cooperative token (checked by the safety plugin before each tool call,
task #8) and asyncio.Task.cancel() (a hard interrupt at the current
await point) — belt and suspenders, since a tool mid-flight won't
necessarily check the token between awaits.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from orbit import db

logger = logging.getLogger("orbit.task_manager")

CoroFactory = Callable[["CancellationToken"], Awaitable[Any]]


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise asyncio.CancelledError("task cancelled by user")

    async def wait(self) -> None:
        await self._event.wait()


class TaskManager:
    def __init__(self, max_concurrent_headless: int = 5) -> None:
        self._foreground_lock = asyncio.Lock()
        self._headless_sem = asyncio.Semaphore(max_concurrent_headless)
        self._running: dict[str, tuple[asyncio.Task, CancellationToken]] = {}

    async def submit(
        self, task_id: str, lane: str, coro_factory: CoroFactory
    ) -> asyncio.Task:
        if lane not in db.LANES:
            raise ValueError(f"invalid lane: {lane!r}")
        token = CancellationToken()

        async def runner() -> Any:
            db.update_task_status(task_id, "RUNNING")
            try:
                gate = self._foreground_lock if lane == "foreground" else self._headless_sem
                async with gate:
                    token.raise_if_cancelled()
                    result = await coro_factory(token)
                db.update_task_status(task_id, "COMPLETED", result=str(result))
                return result
            except asyncio.CancelledError:
                db.update_task_status(
                    task_id, "CANCELLED", failure_reason="cancelled by user"
                )
                raise
            except Exception as exc:  # noqa: BLE001 — must always land the task
                logger.exception("task %s failed", task_id)
                db.update_task_status(task_id, "FAILED", failure_reason=str(exc))
                raise
            finally:
                self._running.pop(task_id, None)

        aio_task = asyncio.create_task(runner(), name=task_id)
        self._running[task_id] = (aio_task, token)
        return aio_task

    def cancel(self, task_id: str) -> bool:
        entry = self._running.get(task_id)
        if not entry:
            return False
        aio_task, token = entry
        token.cancel()
        aio_task.cancel()
        return True

    def token_for(self, task_id: str) -> Optional[CancellationToken]:
        entry = self._running.get(task_id)
        return entry[1] if entry else None

    def is_running(self, task_id: str) -> bool:
        return task_id in self._running
