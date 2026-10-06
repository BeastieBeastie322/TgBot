"""SQLite catalog of projects, wizard sessions, and the repository cache."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ACTIVE_RUNS = ("CREATING", "RUNNING")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    repo_url TEXT,
    agent_id TEXT,
    agent_url TEXT,
    latest_run_id TEXT,
    run_status TEXT,
    notify_chat_id INTEGER,
    result_notified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER,
    step TEXT NOT NULL,
    draft_name TEXT,
    draft_kind TEXT,
    draft_repo_url TEXT,
    active_project_id INTEGER
);

CREATE TABLE IF NOT EXISTS repo_cache (
    url TEXT PRIMARY KEY,
    position INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id, created_at);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.isoformat()


@dataclass
class Project:
    id: int
    user_id: int
    name: str
    kind: str
    repo_url: str | None
    agent_id: str | None
    agent_url: str | None
    latest_run_id: str | None
    run_status: str | None
    notify_chat_id: int | None
    result_notified: bool
    created_at: str

    @property
    def running(self) -> bool:
        return self.run_status in ACTIVE_RUNS and bool(self.latest_run_id) and not self.result_notified


@dataclass
class Session:
    user_id: int
    chat_id: int | None = None
    step: str = "idle"
    draft_name: str | None = None
    draft_kind: str | None = None
    draft_repo_url: str | None = None
    active_project_id: int | None = None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_session(self, user_id: int) -> Session:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return Session(user_id=user_id)
        return Session(
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            step=row["step"],
            draft_name=row["draft_name"],
            draft_kind=row["draft_kind"],
            draft_repo_url=row["draft_repo_url"],
            active_project_id=row["active_project_id"],
        )

    def save_session(self, session: Session) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    user_id, chat_id, step, draft_name, draft_kind,
                    draft_repo_url, active_project_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    step = excluded.step,
                    draft_name = excluded.draft_name,
                    draft_kind = excluded.draft_kind,
                    draft_repo_url = excluded.draft_repo_url,
                    active_project_id = excluded.active_project_id
                """,
                (
                    session.user_id,
                    session.chat_id,
                    session.step,
                    session.draft_name,
                    session.draft_kind,
                    session.draft_repo_url,
                    session.active_project_id,
                ),
            )
            self._conn.commit()

    def list_projects(self, user_id: int) -> list[Project]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM projects
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._project(row) for row in rows]

    def get_project(self, user_id: int, project_id: int) -> Project | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE user_id = ? AND id = ?",
                (user_id, project_id),
            ).fetchone()
        return self._project(row) if row else None

    def get_project_by_id(self, project_id: int) -> Project | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        return self._project(row) if row else None

    def name_taken(self, user_id: int, name: str) -> bool:
        target = name.casefold()
        return any(project.name.casefold() == target for project in self.list_projects(user_id))

    def project_by_repo(self, user_id: int, repo_url: str) -> Project | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM projects WHERE user_id = ? AND repo_url = ?",
                (user_id, repo_url),
            ).fetchone()
        return self._project(row) if row else None

    def insert_project(
        self,
        *,
        user_id: int,
        name: str,
        kind: str,
        repo_url: str | None,
    ) -> Project:
        created_at = _stamp()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO projects (
                    user_id, name, kind, repo_url, result_notified, created_at
                ) VALUES (?, ?, ?, ?, 0, ?)
                """,
                (user_id, name, kind, repo_url, created_at),
            )
            self._conn.commit()
            project_id = int(cursor.lastrowid)
            row = self._conn.execute(
                "SELECT * FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
        return self._project(row)

    def update_project(self, project_id: int, **fields: object) -> None:
        allowed = {
            "agent_id",
            "agent_url",
            "latest_run_id",
            "run_status",
            "notify_chat_id",
            "result_notified",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown project fields: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [int(value) if isinstance(value, bool) else value for value in fields.values()]
        with self._lock:
            self._conn.execute(
                f"UPDATE projects SET {assignments} WHERE id = ?",
                (*values, project_id),
            )
            self._conn.commit()

    def delete_project(self, user_id: int, project_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM projects WHERE user_id = ? AND id = ?",
                (user_id, project_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def unfinished_runs(self) -> list[Project]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM projects
                WHERE result_notified = 0
                  AND latest_run_id IS NOT NULL
                  AND run_status IN ('CREATING', 'RUNNING')
                """
            ).fetchall()
        return [self._project(row) for row in rows]

    def replace_repos(self, urls: list[str], *, fetched_at: datetime | None = None) -> None:
        stamp = _stamp(fetched_at)
        with self._lock:
            self._conn.execute("DELETE FROM repo_cache")
            self._conn.executemany(
                "INSERT INTO repo_cache (url, position) VALUES (?, ?)",
                [(url, index) for index, url in enumerate(urls)],
            )
            self._conn.execute(
                """
                INSERT INTO meta (key, value) VALUES ('repos_fetched_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (stamp,),
            )
            self._conn.commit()

    def cached_repos(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT url FROM repo_cache ORDER BY position ASC"
            ).fetchall()
        return [row["url"] for row in rows]

    def repos_fetched_at(self) -> datetime | None:
        raw = self.get_meta("repos_fetched_at")
        if not raw:
            return None
        return datetime.fromisoformat(raw)

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?",
                (key,),
            ).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    @staticmethod
    def _project(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            kind=row["kind"],
            repo_url=row["repo_url"],
            agent_id=row["agent_id"],
            agent_url=row["agent_url"],
            latest_run_id=row["latest_run_id"],
            run_status=row["run_status"],
            notify_chat_id=row["notify_chat_id"],
            result_notified=bool(row["result_notified"]),
            created_at=row["created_at"],
        )
