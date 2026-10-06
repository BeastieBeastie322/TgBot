from datetime import datetime, timedelta, timezone

import pytest

from projectbot.cursor_api import CursorApiError, Launch, RunState, StartedRun
from projectbot.service import Service


class ScriptCursor:
    def __init__(self) -> None:
        self.repos = [
            "https://github.com/acme/ledger",
            "https://github.com/acme/notes",
        ]
        self.created: list[dict] = []
        self.runs: list[tuple[str, str]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.fail_repos: Exception | None = None
        self.list_calls = 0
        self.run_state = RunState(id="run-1", status="FINISHED", result="готово")
        self._seq = 0

    async def list_repositories(self) -> list[str]:
        self.list_calls += 1
        if self.fail_repos:
            raise self.fail_repos
        return list(self.repos)

    async def create_agent(self, *, name: str, prompt: str, repo_url: str | None) -> Launch:
        self._seq += 1
        self.created.append({"name": name, "prompt": prompt, "repo_url": repo_url})
        return Launch(
            agent_id=f"bc-{self._seq}",
            run_id=f"run-{self._seq}",
            url=f"https://cursor.com/agents/bc-{self._seq}",
            status="CREATING",
        )

    async def create_run(self, agent_id: str, prompt: str) -> StartedRun:
        self._seq += 1
        self.runs.append((agent_id, prompt))
        return StartedRun(run_id=f"run-{self._seq}", status="CREATING")

    async def get_run(self, agent_id: str, run_id: str) -> RunState:
        return RunState(
            id=run_id,
            status=self.run_state.status,
            result=self.run_state.result,
            pr_url=self.run_state.pr_url,
        )

    async def cancel_run(self, agent_id: str, run_id: str) -> None:
        self.cancelled.append((agent_id, run_id))


def _datas(outgoing) -> list[str]:
    return [button.data for row in outgoing.buttons for button in row]


def test_home_menu_offers_create_when_empty(store):
    service = Service(store, ScriptCursor(), demo=True)
    home = service.open_home(1, 10, welcome=True)
    assert "Демо-режим" in home.text
    assert "Проектов пока нет" in home.text
    assert "m:new" in _datas(home)
    assert "m:repos" in _datas(home)
    assert all(len(data.encode()) <= 64 for data in _datas(home))


def test_create_scratch_project_and_send_it_to_cursor(store):
    cursor = ScriptCursor()
    service = Service(store, cursor)

    named = service.begin_create(7, 70)
    assert "Как назвать" in named.text

    chosen = _run(service.on_text(7, 70, "ledger"))
    assert "n:scratch" in _datas(chosen.message)

    created = _run(service.on_callback(7, 70, "n:scratch"))
    assert "Проект создан" in created.message.text
    assert "ledger" in created.message.text
    assert store.list_projects(7)[0].kind == "scratch"
    assert cursor.created == []

    sent = _run(service.on_text(7, 70, "Сделай CLI для расходов"))
    assert sent.watch is not None
    assert cursor.created[0]["repo_url"] is None
    assert "ledger" in cursor.created[0]["prompt"]
    assert "Сделай CLI" in cursor.created[0]["prompt"]
    assert sent.watch.agent_id == "bc-1"

    cursor.run_state = RunState(id="run-1", status="FINISHED", result="готово")
    follow = _run(service.on_text(7, 70, "Добавь тесты"))
    assert cursor.runs == [("bc-1", "Добавь тесты")]
    assert follow.watch is not None
    assert len(cursor.created) == 1


def test_busy_agent_rejects_the_next_task(store):
    cursor = ScriptCursor()
    cursor.run_state = RunState(id="run-1", status="RUNNING")
    service = Service(store, cursor)
    service.begin_create(1, 1)
    _run(service.on_text(1, 1, "ledger"))
    _run(service.on_callback(1, 1, "n:scratch"))
    _run(service.on_text(1, 1, "Первая задача"))
    blocked = _run(service.on_text(1, 1, "Вторая задача"))
    assert "ещё выполняет" in blocked.message.text
    assert cursor.runs == []
    assert len(cursor.created) == 1


def test_project_pages_and_selection(store):
    service = Service(store, ScriptCursor())
    for index in range(8):
        store.insert_project(user_id=3, name=f"p{index}", kind="scratch", repo_url=None)
    first = service.open_home(3, 3, welcome=False)
    opened = [data for data in _datas(first) if data.startswith("p:open:")]
    assert len(opened) == 6
    assert "p:page:1" in _datas(first)
    second = _run(service.on_callback(3, 3, "p:page:1")).message
    assert len([data for data in _datas(second) if data.startswith("p:open:")]) == 2

    project_id = int(opened[0].removeprefix("p:open:"))
    card = _run(service.on_callback(3, 3, f"p:open:{project_id}")).message
    assert "Агент ещё не запускался" in card.text
    assert store.get_session(3).active_project_id == project_id


def test_repository_menu_caches_and_opens_a_project(store):
    cursor = ScriptCursor()
    service = Service(store, cursor)
    listed = _run(service.on_callback(4, 4, "m:repos")).message
    assert "acme/ledger" in listed.text or any("ledger" in button.text for row in listed.buttons for button in row)
    assert cursor.list_calls == 1

    again = _run(service.on_callback(4, 4, "r:refresh")).message
    assert "только что запрашивался" in again.text
    assert cursor.list_calls == 1

    opened = _run(service.on_callback(4, 4, "r:open:0")).message
    assert "добавлен" in opened.text.lower() or "Репозиторий" in opened.text
    project = store.list_projects(4)[0]
    assert project.repo_url == "https://github.com/acme/ledger"

    duplicate = _run(service.on_callback(4, 4, "r:open:0")).message
    assert "уже в списке" in duplicate.text
    assert len(store.list_projects(4)) == 1


def test_rate_limit_keeps_the_cached_repository_list(store):
    cursor = ScriptCursor()
    service = Service(store, cursor)
    stale = datetime.now(timezone.utc) - timedelta(minutes=11)
    store.replace_repos(["https://github.com/acme/ledger"], fetched_at=stale)
    store.set_meta(
        "repos_attempted_at",
        (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
    )
    cursor.fail_repos = CursorApiError(429, "rate_limit", "slow down")

    listed = _run(service.on_callback(4, 4, "m:repos")).message
    labels = [button.text for row in listed.buttons for button in row]
    assert any("ledger" in label for label in labels)
    assert "сохранённый список" in listed.text


def test_new_project_can_pick_a_repository_from_the_list(store):
    service = Service(store, ScriptCursor())
    service.begin_create(5, 5)
    _run(service.on_text(5, 5, "заметки"))
    listing = _run(service.on_callback(5, 5, "n:list")).message
    assert "n:attach:1" in _datas(listing)
    assert "w:cancel" in _datas(listing)
    created = _run(service.on_callback(5, 5, "n:attach:1")).message
    assert "Проект создан" in created.text
    project = store.list_projects(5)[0]
    assert project.name == "заметки"
    assert project.repo_url == "https://github.com/acme/notes"


def test_new_project_can_attach_a_pasted_url(store):
    cursor = ScriptCursor()
    service = Service(store, cursor)
    service.begin_create(8, 8)
    _run(service.on_text(8, 8, "billing"))
    _run(service.on_callback(8, 8, "n:url"))
    rejected = _run(service.on_text(8, 8, "не ссылка"))
    assert "Не похоже" in rejected.message.text
    accepted = _run(service.on_text(8, 8, "git@github.com:acme/billing.git"))
    assert "Проект создан" in accepted.message.text
    project = store.list_projects(8)[0]
    assert project.repo_url == "https://github.com/acme/billing"

    sent = _run(service.on_text(8, 8, "Опиши API"))
    assert cursor.created[0]["repo_url"] == "https://github.com/acme/billing"
    assert cursor.created[0]["prompt"] == "Опиши API"


def test_cancel_duplicate_name_and_delete(store):
    service = Service(store, ScriptCursor())
    service.begin_create(2, 2)
    _run(service.on_text(2, 2, "Альфа"))
    _run(service.on_callback(2, 2, "n:scratch"))

    service.begin_create(2, 2)
    duplicate = _run(service.on_text(2, 2, "альфа"))
    assert "уже есть" in duplicate.message.text

    cancelled = service.cancel(2, 2)
    assert "отменено" in cancelled.text.lower()
    assert store.get_session(2).step == "idle"

    project = store.list_projects(2)[0]
    ask = _run(service.on_callback(2, 2, f"p:askdel:{project.id}")).message
    assert "Убрать" in ask.text
    gone = _run(service.on_callback(2, 2, f"p:del:{project.id}")).message
    assert "убран" in gone.text
    assert store.list_projects(2) == []


def test_task_without_a_project_returns_to_the_menu(store):
    service = Service(store, ScriptCursor())
    turn = _run(service.on_text(9, 9, "Сделай что-нибудь"))
    assert "Сначала выберите проект" in turn.message.text
    assert "m:new" in _datas(turn.message)


def test_stop_cancels_the_active_run(store):
    cursor = ScriptCursor()
    cursor.run_state = RunState(id="run-1", status="RUNNING")
    service = Service(store, cursor)
    service.begin_create(1, 1)
    _run(service.on_text(1, 1, "ledger"))
    _run(service.on_callback(1, 1, "n:scratch"))
    sent = _run(service.on_text(1, 1, "Первая задача"))
    stopped = _run(service.on_callback(1, 1, f"p:stop:{sent.watch.project_id}"))
    assert cursor.cancelled == [("bc-1", "run-1")]
    assert "Останавливаю" in stopped.message.text


def test_stale_button(store):
    service = Service(store, ScriptCursor())
    turn = _run(service.on_callback(1, 1, "p:open:999"))
    assert "устарела" in turn.message.text


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
