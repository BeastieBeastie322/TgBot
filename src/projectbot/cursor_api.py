"""Client for the Cursor Cloud Agents API v1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

API_ROOT = "https://api.cursor.com"
TERMINAL_STATUSES = frozenset({"FINISHED", "ERROR", "CANCELLED", "EXPIRED"})


class CursorApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message or f"HTTP {status}")


@dataclass(frozen=True)
class Launch:
    agent_id: str
    run_id: str
    url: str
    status: str


@dataclass(frozen=True)
class StartedRun:
    run_id: str
    status: str


@dataclass(frozen=True)
class RunState:
    id: str
    status: str
    result: str | None = None
    pr_url: str | None = None


def explain_error(error: CursorApiError) -> str:
    if error.status == 401:
        return "Cursor не принял API-ключ. Проверьте CURSOR_API_KEY."
    if error.status == 429:
        return "Cursor временно ограничил запросы. Подождите минуту и повторите."
    if error.status == 409 and error.code == "agent_busy":
        return "Агент ещё занят предыдущей задачей."
    if error.status == 409 and error.code == "run_not_cancellable":
        return "Задача уже завершилась."
    detail = error.message.strip()
    if detail:
        return f"Cursor ответил ошибкой ({error.status}): {detail}"
    return f"Cursor ответил ошибкой ({error.status})."


class CursorApi:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = API_ROOT,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=httpx.BasicAuth(api_key, ""),
            transport=transport,
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def whoami(self) -> str:
        payload = await self._request("GET", "/v1/me")
        return str(payload.get("apiKeyName") or "API key")

    async def list_repositories(self) -> list[str]:
        payload = await self._request("GET", "/v1/repositories")
        urls: list[str] = []
        for item in payload.get("items") or []:
            url = str(item.get("url") or "").strip()
            if url:
                urls.append(url)
        return urls

    async def create_agent(
        self,
        *,
        name: str,
        prompt: str,
        repo_url: str | None,
    ) -> Launch:
        body: dict[str, Any] = {
            "name": name[:100],
            "prompt": {"text": prompt},
            "autoCreatePR": repo_url is not None,
        }
        if repo_url:
            body["repos"] = [{"url": repo_url}]
        payload = await self._request("POST", "/v1/agents", json=body)
        agent = payload.get("agent") or {}
        run = payload.get("run") or {}
        agent_id = str(agent.get("id") or "")
        run_id = str(run.get("id") or agent.get("latestRunId") or "")
        if not agent_id or not run_id:
            raise CursorApiError(502, "bad_response", "В ответе Cursor нет id агента.")
        url = str(agent.get("url") or f"https://cursor.com/agents/{agent_id}")
        return Launch(
            agent_id=agent_id,
            run_id=run_id,
            url=url,
            status=str(run.get("status") or agent.get("status") or "CREATING"),
        )

    async def create_run(self, agent_id: str, prompt: str) -> StartedRun:
        payload = await self._request(
            "POST",
            f"/v1/agents/{agent_id}/runs",
            json={"prompt": {"text": prompt}},
        )
        run = payload.get("run") or payload
        run_id = str(run.get("id") or "")
        if not run_id:
            raise CursorApiError(502, "bad_response", "В ответе Cursor нет id задачи.")
        return StartedRun(run_id=run_id, status=str(run.get("status") or "CREATING"))

    async def get_run(self, agent_id: str, run_id: str) -> RunState:
        payload = await self._request("GET", f"/v1/agents/{agent_id}/runs/{run_id}")
        return RunState(
            id=str(payload.get("id") or run_id),
            status=str(payload.get("status") or "RUNNING").upper(),
            result=_optional_text(payload.get("result")),
            pr_url=_pull_request(payload.get("git")),
        )

    async def cancel_run(self, agent_id: str, run_id: str) -> None:
        await self._request("POST", f"/v1/agents/{agent_id}/runs/{run_id}/cancel")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise CursorApiError(0, "network", "Нет соединения с Cursor.") from error
        if response.status_code >= 400:
            status, code, message = _error_parts(response)
            raise CursorApiError(status, code, message)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as error:
            raise CursorApiError(502, "bad_response", "Cursor вернул не JSON.") from error
        if not isinstance(payload, dict):
            raise CursorApiError(502, "bad_response", "Cursor вернул неожиданный ответ.")
        return payload


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pull_request(git: object) -> str | None:
    if not isinstance(git, dict):
        return None
    branches = git.get("branches")
    if not isinstance(branches, list):
        return None
    for branch in branches:
        if isinstance(branch, dict) and branch.get("prUrl"):
            return str(branch["prUrl"])
    return None


def _error_parts(response: httpx.Response) -> tuple[int, str, str]:
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return response.status_code, "", text[:300]
    if not isinstance(body, dict):
        return response.status_code, "", ""
    error = body.get("error")
    source = error if isinstance(error, dict) else body
    if not isinstance(source, dict):
        return response.status_code, "", ""
    return (
        response.status_code,
        str(source.get("code") or ""),
        str(source.get("message") or "")[:300],
    )
