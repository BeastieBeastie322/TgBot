"""Process configuration loaded from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines. Existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def user_is_allowed(user_id: int, allowed: frozenset[int]) -> bool:
    """An empty allow-list means the bot is open."""
    return not allowed or user_id in allowed


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _user_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        piece = part.strip()
        if not piece:
            continue
        ids.add(int(piece))
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    cursor_api_key: str
    allowed_user_ids: frozenset[int]
    database_path: Path
    demo: bool

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        api_key = os.environ.get("CURSOR_API_KEY", "").strip()
        forced_demo = _flag("FORGE_DEMO")
        database = os.environ.get("DATABASE_PATH", "").strip() or "data/bot.sqlite"
        return cls(
            telegram_token=token,
            cursor_api_key=api_key,
            allowed_user_ids=_user_ids(os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")),
            database_path=Path(database),
            demo=forced_demo or not api_key,
        )
