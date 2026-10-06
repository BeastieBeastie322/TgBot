"""Project menu, creation wizard, and handoff to a Cursor cloud agent."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from projectbot.cursor_api import CursorApiError, RunState, explain_error
from projectbot.menu import (
    PAGE_SIZE,
    Button,
    Outgoing,
    page_window,
    project_buttons,
    repo_buttons,
)
from projectbot.store import Project, Session, Store
from projectbot.textutil import normalize_repo_url, repo_label

logger = logging.getLogger(__name__)

REPO_TTL = timedelta(minutes=10)
REPO_MIN_INTERVAL = timedelta(seconds=60)
NAME_LIMIT = 80

WELCOME = (
    "Выберите проект или создайте новый.\n\n"
    "Дальше просто пишите задачи: они уходят облачному агенту Cursor "
    "в выбранный проект."
)
DEMO_BANNER = (
    "Демо-режим: ключ Cursor не задан. Меню и новые проекты сохраняются, "
    "но агент Cursor не запускается."
)
HELP = (
    "Команды\n"
    "/projects — список проектов\n"
    "/new — новый проект\n"
    "/status — что выбрано сейчас\n"
    "/cancel — прервать создание\n\n"
    "Нижние кнопки делают то же самое.\n"
    "После выбора проекта любое следующее сообщение уходит его агенту."
)


@dataclass(frozen=True)
class Watch:
    project_id: int
    chat_id: int
    agent_id: str
    run_id: str


@dataclass(frozen=True)
class Turn:
    message: Outgoing
    watch: Watch | None = None


class Service:
    def __init__(self, store: Store, cursor: object, *, demo: bool = False):
        self.store = store
        self.cursor = cursor
        self.demo = demo
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    def help_text(self) -> Outgoing:
        return Outgoing(HELP, show_reply_keyboard=True)

    def open_home(self, user_id: int, chat_id: int, *, welcome: bool) -> Outgoing:
        session = self._touch(user_id, chat_id)
        self._clear_draft(session)
        return self._projects(session, 0, welcome=welcome)

    def begin_create(self, user_id: int, chat_id: int) -> Outgoing:
        session = self._touch(user_id, chat_id)
        session.step = "name"
        session.draft_name = None
        session.draft_kind = None
        session.draft_repo_url = None
        self.store.save_session(session)
        return Outgoing(
            "Как назвать проект?\n\nНазвание видно в меню этого чата.",
            ((Button("Отмена", "w:cancel"),),),
            show_reply_keyboard=True,
        )

    def cancel(self, user_id: int, chat_id: int) -> Outgoing:
        session = self._touch(user_id, chat_id)
        self._clear_draft(session)
        listing = self._projects(session, 0, welcome=False)
        return Outgoing(
            "Создание отменено.\n\n" + listing.text,
            listing.buttons,
            show_reply_keyboard=True,
        )

    async def status(self, user_id: int, chat_id: int) -> Outgoing:
        session = self._touch(user_id, chat_id)
        project = self._active(session)
        if project is None:
            return Outgoing(
                "Проект не выбран.\n\nОткройте список и выберите проект или создайте новый.",
                ((Button("К проектам", "m:projects"), Button("Новый проект", "m:new")),),
                show_reply_keyboard=True,
            )
        project = await self._refresh_run(project)
        rows: list[tuple[Button, ...]] = []
        if project.running:
            rows.append((Button("Остановить", f"p:stop:{project.id}"),))
        rows.append((Button("К списку", "m:projects"),))
        return Outgoing(
            self._card(project, heading="Сейчас выбран"),
            tuple(rows),
            show_reply_keyboard=True,
        )

    async def on_text(self, user_id: int, chat_id: int, text: str) -> Turn:
        session = self._touch(user_id, chat_id)
        cleaned = " ".join(text.split())
        if session.step == "name":
            return Turn(self._accept_name(session, cleaned))
        if session.step == "repo_url":
            return Turn(self._accept_url(session, text.strip()))
        if session.step in {"source", "pick_repo"}:
            return Turn(
                Outgoing(
                    "Выберите вариант кнопкой ниже. «Отмена» вернёт к списку проектов.",
                    ((Button("Отмена", "w:cancel"),),),
                )
            )
        return await self._send_task(session, text.strip())

    async def on_callback(self, user_id: int, chat_id: int, data: str) -> Turn:
        session = self._touch(user_id, chat_id)
        if data == "m:projects":
            self._clear_draft(session)
            return Turn(self._projects(session, 0, welcome=False))
        if data == "m:new":
            return Turn(self.begin_create(user_id, chat_id))
        if data == "w:cancel":
            return Turn(self.cancel(user_id, chat_id))
        if data in {"m:repos", "r:refresh"}:
            return Turn(await self._repos(session, 0, force=data == "r:refresh"))
        if data == "n:scratch":
            return Turn(self._finish_draft(session, kind="scratch", repo_url=None))
        if data == "n:url":
            session.step = "repo_url"
            self.store.save_session(session)
            return Turn(
                Outgoing(
                    "Пришлите ссылку на репозиторий.\n"
                    "Подойдёт https://github.com/org/name или git@github.com:org/name.git.",
                    ((Button("Отмена", "w:cancel"),),),
                )
            )
        if data == "n:list":
            session.step = "pick_repo"
            self.store.save_session(session)
            return Turn(await self._repos(session, 0, force=False))
        if data.startswith("p:page:"):
            page = _int(data.removeprefix("p:page:"))
            if page is None:
                return Turn(self._stale())
            return Turn(self._projects(session, page, welcome=False))
        if data.startswith("r:page:"):
            page = _int(data.removeprefix("r:page:"))
            if page is None:
                return Turn(self._stale())
            return Turn(await self._repos(session, page, force=False))
        if data.startswith("p:open:"):
            project_id = _int(data.removeprefix("p:open:"))
            if project_id is None:
                return Turn(self._stale())
            return Turn(self._open_project(session, project_id))
        if data.startswith("p:askdel:"):
            project_id = _int(data.removeprefix("p:askdel:"))
            if project_id is None:
                return Turn(self._stale())
            return Turn(self._ask_delete(session, project_id))
        if data.startswith("p:del:"):
            project_id = _int(data.removeprefix("p:del:"))
            if project_id is None:
                return Turn(self._stale())
            return Turn(self._delete(session, project_id))
        if data.startswith("p:stop:"):
            project_id = _int(data.removeprefix("p:stop:"))
            if project_id is None:
                return Turn(self._stale())
            return Turn(await self._stop(session, project_id))
        if data.startswith("r:open:"):
            index = _int(data.removeprefix("r:open:"))
            if index is None:
                return Turn(self._stale())
            return Turn(self._open_repo_index(session, index, rename=False))
        if data.startswith("n:attach:"):
            index = _int(data.removeprefix("n:attach:"))
            if index is None:
                return Turn(self._stale())
            return Turn(self._open_repo_index(session, index, rename=True))
        return Turn(self._stale())

    def format_terminal(self, project: Project, run: RunState) -> str:
        if run.status == "FINISHED":
            body = run.result or "Агент закончил и не прислал текст."
            lines = [f"«{project.name}»", "", body]
        elif run.status == "CANCELLED":
            lines = [f"Задача в «{project.name}» остановлена."]
        elif run.status == "EXPIRED":
            lines = [f"Задача в «{project.name}» истекла."]
        else:
            lines = [
                f"Агент в «{project.name}» остановился с ошибкой.",
                "",
                run.result or "Подробностей нет.",
            ]
        if run.pr_url:
            lines.extend(["", run.pr_url])
        elif project.agent_url:
            lines.extend(["", project.agent_url])
        return "\n".join(lines)

    def _touch(self, user_id: int, chat_id: int) -> Session:
        session = self.store.get_session(user_id)
        if session.chat_id != chat_id:
            session.chat_id = chat_id
            self.store.save_session(session)
        return session

    def _clear_draft(self, session: Session) -> None:
        session.step = "idle"
        session.draft_name = None
        session.draft_kind = None
        session.draft_repo_url = None
        self.store.save_session(session)

    def _projects(self, session: Session, page: int, *, welcome: bool) -> Outgoing:
        projects = self.store.list_projects(session.user_id)
        start, current, pages = page_window(len(projects), page, PAGE_SIZE)
        visible = projects[start : start + PAGE_SIZE]
        lines: list[str] = []
        if welcome and self.demo:
            lines.append(DEMO_BANNER)
        if welcome:
            lines.append(WELCOME)
        active = self._active(session)
        if active is not None and not welcome:
            lines.append(f"Сейчас выбран: {active.name}")
        if not projects:
            lines.append(
                "Проектов пока нет.\n\n"
                "Создайте новый или откройте репозиторий, подключённый к Cursor."
            )
        else:
            lines.append("Выберите проект.")
            if pages > 1:
                lines.append(f"Страница {current + 1} из {pages}.")
        buttons = project_buttons(
            [(item.id, item.name, active is not None and item.id == active.id) for item in visible],
            current,
            pages,
        )
        return Outgoing("\n\n".join(lines), buttons, show_reply_keyboard=True)

    async def _repos(self, session: Session, page: int, *, force: bool) -> Outgoing:
        attach = session.step == "pick_repo" and bool(session.draft_name)
        note = await self._ensure_repos(force=force)
        urls = self.store.cached_repos()
        if not urls and note:
            buttons = (
                (Button("Обновить", "r:refresh"), Button("К проектам", "m:projects")),
            )
            if attach:
                buttons = (
                    (Button("Обновить", "r:refresh"),),
                    (Button("Вставить ссылку", "n:url"), Button("Отмена", "w:cancel")),
                )
            return Outgoing(note, buttons)
        start, current, pages = page_window(len(urls), page, PAGE_SIZE)
        visible = list(enumerate(urls, start=0))[start : start + PAGE_SIZE]
        items: list[tuple[int, str, bool]] = []
        for index, url in visible:
            opened = self.store.project_by_repo(session.user_id, url) is not None
            items.append((index, repo_label(url), opened))
        intro = "Репозитории, которые Cursor видит через GitHub."
        if attach and session.draft_name:
            intro = f"Куда привязать «{session.draft_name}»?\n\n" + intro
        if pages > 1:
            intro += f"\nСтраница {current + 1} из {pages}."
        if note:
            intro += "\n\n" + note
        if not urls:
            intro += "\n\nСписок пуст. Подключите GitHub в Cursor или создайте пустой проект."
        return Outgoing(
            intro,
            repo_buttons(items, current, pages, attach=attach),
            show_reply_keyboard=True,
        )

    async def _ensure_repos(self, *, force: bool) -> str | None:
        now = datetime.now(timezone.utc)
        fetched_at = self.store.repos_fetched_at()
        fresh = fetched_at is not None and now - fetched_at < REPO_TTL
        if fresh and not force:
            return None
        attempted_raw = self.store.get_meta("repos_attempted_at")
        attempted = datetime.fromisoformat(attempted_raw) if attempted_raw else None
        if attempted is not None and now - attempted < REPO_MIN_INTERVAL:
            if self.store.cached_repos():
                return "Список только что запрашивался. Показываю сохранённую копию."
            return "Список только что запрашивался. Подождите минуту и нажмите «Обновить»."
        try:
            urls = await self.cursor.list_repositories()
        except CursorApiError as error:
            if error.status == 429:
                self.store.set_meta("repos_attempted_at", now.isoformat())
            if self.store.cached_repos():
                return explain_error(error) + " Показываю сохранённый список."
            return explain_error(error)
        self.store.replace_repos(urls, fetched_at=now)
        self.store.set_meta("repos_attempted_at", now.isoformat())
        return None

    def _accept_name(self, session: Session, name: str) -> Outgoing:
        if not name or len(name) > NAME_LIMIT:
            return Outgoing(
                "Нужно название до 80 символов.",
                ((Button("Отмена", "w:cancel"),),),
            )
        if self.store.name_taken(session.user_id, name):
            return Outgoing(
                "Такое название уже есть. Напишите другое.",
                ((Button("Отмена", "w:cancel"),),),
            )
        session.draft_name = name
        session.step = "source"
        self.store.save_session(session)
        return Outgoing(
            (
                f"Проект «{name}». Откуда взять код?\n\n"
                "Пустой проект запускает агента без репозитория. "
                "Репозиторий нужен, чтобы агент мог открыть pull request."
            ),
            (
                (Button("Пустой проект", "n:scratch"),),
                (Button("Мои репозитории", "n:list"),),
                (Button("Вставить ссылку", "n:url"),),
                (Button("Отмена", "w:cancel"),),
            ),
        )

    def _accept_url(self, session: Session, raw: str) -> Outgoing:
        url = normalize_repo_url(raw)
        if url is None:
            return Outgoing(
                "Не похоже на ссылку репозитория. Пример: https://github.com/org/name",
                ((Button("Отмена", "w:cancel"),),),
            )
        if not session.draft_name:
            self._clear_draft(session)
            return self._projects(session, 0, welcome=False)
        return self._finish_draft(session, kind="repo", repo_url=url)

    def _finish_draft(self, session: Session, *, kind: str, repo_url: str | None) -> Outgoing:
        name = session.draft_name
        if not name:
            return self._projects(session, 0, welcome=False)
        if repo_url:
            existing = self.store.project_by_repo(session.user_id, repo_url)
            if existing is not None:
                self._clear_draft(session)
                session.active_project_id = existing.id
                self.store.save_session(session)
                return self._open_project(session, existing.id, prefix="Этот репозиторий уже в списке.")
        project = self.store.insert_project(
            user_id=session.user_id,
            name=name,
            kind=kind,
            repo_url=repo_url,
        )
        self._clear_draft(session)
        session.active_project_id = project.id
        self.store.save_session(session)
        return self._open_project(session, project.id, prefix="Проект создан.")

    def _open_repo_index(self, session: Session, index: int, *, rename: bool) -> Outgoing:
        urls = self.store.cached_repos()
        if index < 0 or index >= len(urls):
            return Outgoing(
                "Список репозиториев обновился. Откройте его ещё раз.",
                ((Button("Репозитории", "m:repos"),),),
            )
        url = urls[index]
        if rename and session.draft_name:
            return self._finish_draft(session, kind="repo", repo_url=url)
        existing = self.store.project_by_repo(session.user_id, url)
        if existing is not None:
            return self._open_project(
                session,
                existing.id,
                prefix="Этот репозиторий уже в списке.",
            )
        base = repo_label(url).split("/")[-1] or "project"
        name = self._unique_name(session.user_id, base)
        project = self.store.insert_project(
            user_id=session.user_id,
            name=name,
            kind="repo",
            repo_url=url,
        )
        session.active_project_id = project.id
        self._clear_draft(session)
        session.active_project_id = project.id
        self.store.save_session(session)
        return self._open_project(session, project.id, prefix="Репозиторий добавлен в проекты.")

    def _unique_name(self, user_id: int, base: str) -> str:
        stem = base[:NAME_LIMIT]
        if not self.store.name_taken(user_id, stem):
            return stem
        number = 2
        while True:
            suffix = f" {number}"
            candidate = base[: NAME_LIMIT - len(suffix)] + suffix
            if not self.store.name_taken(user_id, candidate):
                return candidate
            number += 1

    def _open_project(self, session: Session, project_id: int, *, prefix: str | None = None) -> Outgoing:
        project = self.store.get_project(session.user_id, project_id)
        if project is None:
            return self._stale()
        session.active_project_id = project.id
        session.step = "idle"
        self.store.save_session(session)
        rows = [(Button("К списку", "m:projects"), Button("Убрать", f"p:askdel:{project.id}"))]
        if project.running:
            rows.insert(0, (Button("Остановить", f"p:stop:{project.id}"),))
        text = self._card(project, heading="Проект")
        if prefix:
            text = prefix + "\n\n" + text
        return Outgoing(text, tuple(rows), show_reply_keyboard=True)

    def _ask_delete(self, session: Session, project_id: int) -> Outgoing:
        project = self.store.get_project(session.user_id, project_id)
        if project is None:
            return self._stale()
        return Outgoing(
            (
                f"Убрать «{project.name}» из списка?\n\n"
                "Агент в Cursor, если он уже запущен, останется. "
                "Из этого меню проект пропадёт."
            ),
            (
                (Button("Убрать", f"p:del:{project.id}"),),
                (Button("Отмена", f"p:open:{project.id}"),),
            ),
        )

    def _delete(self, session: Session, project_id: int) -> Outgoing:
        project = self.store.get_project(session.user_id, project_id)
        if project is None:
            return self._stale()
        self.store.delete_project(session.user_id, project_id)
        if session.active_project_id == project_id:
            session.active_project_id = None
            self.store.save_session(session)
        listing = self._projects(session, 0, welcome=False)
        return Outgoing(
            f"«{project.name}» убран из списка.\n\n" + listing.text,
            listing.buttons,
            show_reply_keyboard=True,
        )

    async def _stop(self, session: Session, project_id: int) -> Outgoing:
        project = self.store.get_project(session.user_id, project_id)
        if project is None:
            return self._stale()
        if not project.agent_id or not project.latest_run_id or not project.running:
            return Outgoing("Останавливать нечего: задача уже завершилась.")
        try:
            await self.cursor.cancel_run(project.agent_id, project.latest_run_id)
        except CursorApiError as error:
            return Outgoing(explain_error(error))
        return Outgoing(f"Останавливаю задачу в «{project.name}». Напишу, когда агент подтвердит.")

    async def _send_task(self, session: Session, text: str) -> Turn:
        if not text:
            return Turn(Outgoing("Напишите задачу текстом."))
        if len(text) > 8000:
            return Turn(Outgoing("Слишком длинная задача. Уложитесь в 8000 символов."))
        project = self._active(session)
        if project is None:
            listing = self._projects(session, 0, welcome=False)
            return Turn(
                Outgoing(
                    "Сначала выберите проект.\n\n" + listing.text,
                    listing.buttons,
                    show_reply_keyboard=True,
                )
            )
        project = await self._refresh_run(project)
        if project.running:
            return Turn(
                Outgoing(
                    f"Агент в «{project.name}» ещё выполняет предыдущую задачу.",
                    ((Button("Остановить", f"p:stop:{project.id}"),),),
                )
            )
        prompt = text
        if project.agent_id is None and project.kind == "scratch":
            prompt = (
                f"Это новый проект «{project.name}» без существующего репозитория. "
                "Работай в пустой среде.\n\n"
                f"Задача:\n{text}"
            )
        chat_id = session.chat_id or 0
        try:
            if project.agent_id is None:
                launch = await self.cursor.create_agent(
                    name=project.name,
                    prompt=prompt,
                    repo_url=project.repo_url,
                )
                agent_id = launch.agent_id
                run_id = launch.run_id
                agent_url = launch.url or None
                status = launch.status
            else:
                started = await self.cursor.create_run(project.agent_id, prompt)
                agent_id = project.agent_id
                run_id = started.run_id
                agent_url = project.agent_url
                status = started.status
        except CursorApiError as error:
            logger.info("cursor task failed project=%s status=%s", project.id, error.status)
            return Turn(Outgoing(explain_error(error)))
        self.store.update_project(
            project.id,
            agent_id=agent_id,
            agent_url=agent_url or "",
            latest_run_id=run_id,
            run_status=status if status in {"CREATING", "RUNNING"} else "RUNNING",
            notify_chat_id=chat_id,
            result_notified=False,
        )
        logger.info("started run project=%s agent=%s run=%s chars=%s", project.id, agent_id, run_id, len(text))
        return Turn(
            Outgoing(
                (
                    f"Задача ушла в «{project.name}».\n"
                    "Ответ пришлю сюда, когда агент закончит."
                ),
                ((Button("Остановить", f"p:stop:{project.id}"),),),
            ),
            watch=Watch(
                project_id=project.id,
                chat_id=chat_id,
                agent_id=agent_id,
                run_id=run_id,
            ),
        )

    async def _refresh_run(self, project: Project) -> Project:
        if not project.running or not project.agent_id or not project.latest_run_id:
            return project
        try:
            run = await self.cursor.get_run(project.agent_id, project.latest_run_id)
        except CursorApiError:
            return project
        if run.status != project.run_status:
            self.store.update_project(project.id, run_status=run.status)
            refreshed = self.store.get_project_by_id(project.id)
            return refreshed or project
        return project

    def _active(self, session: Session) -> Project | None:
        if session.active_project_id is None:
            return None
        return self.store.get_project(session.user_id, session.active_project_id)

    def _card(self, project: Project, *, heading: str) -> str:
        if project.kind == "scratch":
            where = "Пустой проект, без репозитория."
        else:
            where = project.repo_url or "Репозиторий подключён."
        if project.running:
            state = "Агент сейчас выполняет задачу."
        elif project.agent_id:
            state = "Агент уже запускался. Следующее сообщение продолжит тот же разговор."
        else:
            state = "Агент ещё не запускался. Напишите задачу, и она уйдёт в Cursor."
        lines = [f"{heading}: {project.name}", "", where, state]
        if project.agent_url:
            lines.extend(["", project.agent_url])
        return "\n".join(lines)

    @staticmethod
    def _stale() -> Outgoing:
        return Outgoing(
            "Кнопка устарела. Откройте список проектов ещё раз.",
            ((Button("К проектам", "m:projects"),),),
        )


def _int(value: str) -> int | None:
    if not value.isdigit():
        return None
    return int(value)
