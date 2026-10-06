import pytest

from projectbot.cursor_api import CursorApiError, RunState
from projectbot.watch import watch_run


class SequenceCursor:
    def __init__(self, states: list[RunState | Exception]):
        self.states = list(states)
        self.calls = 0

    async def get_run(self, agent_id: str, run_id: str) -> RunState:
        self.calls += 1
        item = self.states.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.asyncio
async def test_watch_returns_the_terminal_run_and_reports_progress():
    cursor = SequenceCursor(
        [
            RunState(id="run-1", status="RUNNING"),
            RunState(id="run-1", status="FINISHED", result="ок"),
        ]
    )
    notes: list[str] = []
    run = await watch_run(
        cursor,
        agent_id="bc-1",
        run_id="run-1",
        notify=_collect(notes),
        interval=0,
        timeout=30,
        progress_every=0,
    )
    assert run is not None
    assert run.result == "ок"
    assert notes == ["Агент всё ещё работает."]


@pytest.mark.asyncio
async def test_watch_stops_when_the_deadline_is_already_due():
    cursor = SequenceCursor([RunState(id="run-1", status="RUNNING")])
    notes: list[str] = []
    run = await watch_run(
        cursor,
        agent_id="bc-1",
        run_id="run-1",
        notify=_collect(notes),
        interval=0,
        timeout=0,
        progress_every=10_000,
    )
    assert run is None
    assert notes == ["Жду уже долго. Проверить ход можно по ссылке агента в Cursor."]


@pytest.mark.asyncio
async def test_watch_reports_an_api_error_once():
    cursor = SequenceCursor([CursorApiError(0, "network", "Нет соединения с Cursor.")])
    notes: list[str] = []
    run = await watch_run(
        cursor,
        agent_id="bc-1",
        run_id="run-1",
        notify=_collect(notes),
        interval=0,
        timeout=10,
    )
    assert run is None
    assert "Нет соединения" in notes[0]


def _collect(notes: list[str]):
    async def notify(text: str) -> None:
        notes.append(text)

    return notify
