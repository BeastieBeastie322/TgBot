"""Run the Telegram project bot."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher

from projectbot.bot import build_router, resume_unfinished
from projectbot.config import Settings, load_env_file
from projectbot.cursor_api import CursorApi, CursorApiError, explain_error
from projectbot.demo import DemoCursor
from projectbot.service import Service
from projectbot.store import Store

logger = logging.getLogger(__name__)


async def run(settings: Settings) -> None:
    store = Store(settings.database_path)
    store.migrate()
    if settings.demo:
        cursor: CursorApi | DemoCursor = DemoCursor()
        logger.warning("demo mode: tasks stay on this machine and Cursor is not called")
    else:
        cursor = CursorApi(settings.cursor_api_key)
        try:
            key_name = await cursor.whoami()
        except CursorApiError as error:
            await cursor.aclose()
            if error.status == 401:
                raise SystemExit(explain_error(error))
            logger.warning("could not read the Cursor API key profile: %s", error.message)
        else:
            logger.info("Cursor API key: %s", key_name)

    service = Service(store, cursor, demo=settings.demo)
    bot = Bot(settings.telegram_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(service, settings, bot))
    await resume_unfinished(bot, service)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await cursor.aclose()
        await bot.session.close()
        store.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env_file(Path.cwd() / ".env")
    settings = Settings.from_env()
    if not settings.telegram_token:
        print(
            "Задайте TELEGRAM_BOT_TOKEN в .env. Образец лежит в .env.example.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
