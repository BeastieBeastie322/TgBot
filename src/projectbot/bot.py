"""Telegram adapter. Menu decisions live in Service."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, or_f
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from projectbot.config import Settings, user_is_allowed
from projectbot.menu import NEW_BUTTON, PROJECTS_BUTTON, STATUS_BUTTON, Button, Outgoing
from projectbot.service import Service, Turn
from projectbot.textutil import chunk_text
from projectbot.watch import watch_run

logger = logging.getLogger(__name__)

_tasks: set[asyncio.Task[None]] = set()


def reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=PROJECTS_BUTTON), KeyboardButton(text=NEW_BUTTON)],
            [KeyboardButton(text=STATUS_BUTTON)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def inline_markup(rows: tuple[tuple[Button, ...], ...]) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button.text, callback_data=button.data) for button in row]
            for row in rows
        ]
    )


def build_router(service: Service, settings: Settings, bot: Bot) -> Router:
    router = Router()

    def allowed(user_id: int) -> bool:
        return user_is_allowed(user_id, settings.allowed_user_ids)

    async def deny(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        await message.answer(
            "Этот бот закрыт.\n"
            f"Ваш Telegram id: {user_id}.\n"
            "Добавьте его в TELEGRAM_ALLOWED_USER_IDS и перезапустите бота."
        )

    async def show_message(message: Message, outgoing: Outgoing) -> None:
        parts = chunk_text(outgoing.text) or ["Пустой ответ."]
        markup_inline = inline_markup(outgoing.buttons)
        reply = reply_keyboard() if outgoing.show_reply_keyboard and markup_inline is None else None
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            markup = (markup_inline or reply) if last else None
            await message.answer(part, reply_markup=markup)

    async def show_callback(query: CallbackQuery, outgoing: Outgoing) -> None:
        message = query.message
        markup = inline_markup(outgoing.buttons)
        text = outgoing.text
        if message is not None and len(text) <= 4000:
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as error:
                if "message is not modified" in str(error).lower():
                    return
        if isinstance(message, Message):
            await show_message(message, outgoing)

    async def deliver_turn(message: Message, turn: Turn) -> None:
        await show_message(message, turn.message)
        if turn.watch:
            spawn_watch(bot, service, turn.watch)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not await _gate(message):
            return
        assert message.from_user
        async with service.lock(message.from_user.id):
            outgoing = service.open_home(message.from_user.id, message.chat.id, welcome=True)
        await show_message(message, outgoing)
        await message.answer(
            "Снизу: проекты, новый проект и статус.",
            reply_markup=reply_keyboard(),
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        if not await _gate(message):
            return
        await show_message(message, service.help_text())

    @router.message(or_f(Command("projects"), F.text == PROJECTS_BUTTON))
    async def projects(message: Message) -> None:
        await _menu(message, welcome=False)

    @router.message(or_f(Command("new"), F.text == NEW_BUTTON))
    async def new_project(message: Message) -> None:
        if not await _gate(message):
            return
        assert message.from_user
        async with service.lock(message.from_user.id):
            outgoing = service.begin_create(message.from_user.id, message.chat.id)
        await show_message(message, outgoing)

    @router.message(Command("cancel"))
    async def cancel(message: Message) -> None:
        if not await _gate(message):
            return
        assert message.from_user
        async with service.lock(message.from_user.id):
            outgoing = service.cancel(message.from_user.id, message.chat.id)
        await show_message(message, outgoing)

    @router.message(or_f(Command("status"), F.text == STATUS_BUTTON))
    async def status(message: Message) -> None:
        if not await _gate(message):
            return
        assert message.from_user
        async with service.lock(message.from_user.id):
            outgoing = await service.status(message.from_user.id, message.chat.id)
        await show_message(message, outgoing)

    @router.message(F.text)
    async def text(message: Message) -> None:
        if not await _gate(message):
            return
        assert message.from_user and message.text
        async with service.lock(message.from_user.id):
            turn = await service.on_text(message.from_user.id, message.chat.id, message.text)
        await deliver_turn(message, turn)

    @router.message()
    async def other(message: Message) -> None:
        if not await _gate(message):
            return
        await message.answer("Пришлите задачу текстом или откройте меню проектов.")

    @router.callback_query()
    async def callback(query: CallbackQuery) -> None:
        if query.from_user is None or query.message is None:
            await query.answer()
            return
        if query.message.chat.type != "private":
            await query.answer("Напишите боту в личные сообщения.", show_alert=True)
            return
        if not allowed(query.from_user.id):
            await query.answer("Бот закрыт для этого аккаунта.", show_alert=True)
            return
        await query.answer()
        async with service.lock(query.from_user.id):
            turn = await service.on_callback(
                query.from_user.id,
                query.message.chat.id,
                query.data or "",
            )
        await show_callback(query, turn.message)
        if turn.watch:
            spawn_watch(bot, service, turn.watch)

    async def _menu(message: Message, *, welcome: bool) -> None:
        if not await _gate(message):
            return
        assert message.from_user
        async with service.lock(message.from_user.id):
            outgoing = service.open_home(message.from_user.id, message.chat.id, welcome=welcome)
        await show_message(message, outgoing)

    async def _gate(message: Message) -> bool:
        if message.chat.type != "private":
            await message.answer("Откройте личный чат со мной — меню проектов работает там.")
            return False
        if not message.from_user or not allowed(message.from_user.id):
            await deny(message)
            return False
        return True

    return router


def spawn_watch(bot: Bot, service: Service, watch: object) -> None:
    task = asyncio.create_task(_watch(bot, service, watch))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def resume_unfinished(bot: Bot, service: Service) -> None:
    for project in service.store.unfinished_runs():
        if not project.notify_chat_id or not project.agent_id or not project.latest_run_id:
            continue
        from projectbot.service import Watch

        spawn_watch(
            bot,
            service,
            Watch(
                project_id=project.id,
                chat_id=project.notify_chat_id,
                agent_id=project.agent_id,
                run_id=project.latest_run_id,
            ),
        )


async def _watch(bot: Bot, service: Service, watch: object) -> None:
    from projectbot.service import Watch

    if not isinstance(watch, Watch):
        return
    try:
        run = await watch_run(
            service.cursor,
            agent_id=watch.agent_id,
            run_id=watch.run_id,
            notify=lambda text: _notify(bot, watch.chat_id, text),
        )
    except Exception:
        logger.exception("watch failed project=%s", watch.project_id)
        await _notify(bot, watch.chat_id, "Не удалось дождаться ответа агента.")
        return
    if run is None:
        return
    project = service.store.get_project_by_id(watch.project_id)
    if project is None or project.result_notified or project.latest_run_id != watch.run_id:
        return
    text = service.format_terminal(project, run)
    await _notify(bot, watch.chat_id, text)
    if project.latest_run_id == watch.run_id:
        service.store.update_project(
            project.id,
            run_status=run.status,
            result_notified=True,
        )


async def _notify(bot: Bot, chat_id: int, text: str) -> None:
    if not chat_id:
        return
    parts = chunk_text(text)
    for part in parts:
        try:
            await bot.send_message(chat_id, part)
        except TelegramBadRequest:
            logger.warning("could not deliver a message to chat %s", chat_id)
            return
