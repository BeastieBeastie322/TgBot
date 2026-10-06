"""Poll a cloud-agent run and return when it reaches a terminal status."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from projectbot.cursor_api import TERMINAL_STATUSES, CursorApiError, RunState

Notify = Callable[[str], Awaitable[None]]


async def watch_run(
    cursor: object,
    *,
    agent_id: str,
    run_id: str,
    notify: Notify,
    interval: float = 4.0,
    timeout: float = 1800.0,
    progress_every: float = 120.0,
) -> RunState | None:
    """Return the terminal run, or None when the wait exceeded `timeout`."""
    deadline = time.monotonic() + timeout
    next_progress = time.monotonic() + progress_every
    while True:
        try:
            run = await cursor.get_run(agent_id, run_id)
        except CursorApiError as error:
            await notify(f"Не удалось проверить задачу: {error.message or 'ошибка Cursor'}.")
            return None
        if run.status in TERMINAL_STATUSES:
            return run
        now = time.monotonic()
        if now >= deadline:
            await notify("Жду уже долго. Проверить ход можно по ссылке агента в Cursor.")
            return None
        if now >= next_progress:
            await notify("Агент всё ещё работает.")
            next_progress = now + progress_every
        await asyncio.sleep(interval)
