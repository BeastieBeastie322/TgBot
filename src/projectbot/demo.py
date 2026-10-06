"""In-memory Cursor stand-in used when no API key is configured."""

from __future__ import annotations

from projectbot.cursor_api import Launch, RunState, StartedRun

SAMPLE_REPOS = (
    "https://github.com/example/ledger",
    "https://github.com/example/notes",
)


class DemoCursor:
    """Accepts the same calls as CursorApi and never leaves the process."""

    def __init__(self) -> None:
        self._seq = 0
        self._prompts: dict[str, str] = {}
        self._status: dict[str, str] = {}

    async def aclose(self) -> None:
        return None

    async def list_repositories(self) -> list[str]:
        return list(SAMPLE_REPOS)

    async def create_agent(
        self,
        *,
        name: str,
        prompt: str,
        repo_url: str | None,
    ) -> Launch:
        del name, repo_url
        self._seq += 1
        agent_id = f"bc-demo-{self._seq}"
        run_id = f"run-demo-{self._seq}"
        self._prompts[run_id] = prompt
        self._status[run_id] = "FINISHED"
        return Launch(
            agent_id=agent_id,
            run_id=run_id,
            url="",
            status="CREATING",
        )

    async def create_run(self, agent_id: str, prompt: str) -> StartedRun:
        del agent_id
        self._seq += 1
        run_id = f"run-demo-{self._seq}"
        self._prompts[run_id] = prompt
        self._status[run_id] = "FINISHED"
        return StartedRun(run_id=run_id, status="CREATING")

    async def get_run(self, agent_id: str, run_id: str) -> RunState:
        del agent_id
        prompt = self._prompts.get(run_id, "")
        status = self._status.get(run_id, "FINISHED")
        if status == "CANCELLED":
            return RunState(id=run_id, status="CANCELLED", result=None)
        preview = prompt if len(prompt) <= 500 else prompt[:500] + "…"
        return RunState(
            id=run_id,
            status="FINISHED",
            result="Демо-режим: Cursor не вызывался.\n\nЗадача:\n" + preview,
        )

    async def cancel_run(self, agent_id: str, run_id: str) -> None:
        del agent_id
        if run_id in self._status:
            self._status[run_id] = "CANCELLED"
