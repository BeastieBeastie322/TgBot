import base64
import json

import httpx
import pytest

from projectbot.cursor_api import CursorApi, CursorApiError, explain_error


def _client(handler) -> CursorApi:
    return CursorApi("secret", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_create_scratch_agent_omits_repos():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(
            200,
            json={
                "agent": {
                    "id": "bc-1",
                    "url": "https://cursor.com/agents/bc-1",
                    "status": "ACTIVE",
                },
                "run": {"id": "run-1", "status": "CREATING"},
            },
        )

    launch = await _client(handler).create_agent(name="ledger", prompt="Сделай CLI", repo_url=None)

    assert "repos" not in seen["body"]
    assert seen["body"]["autoCreatePR"] is False
    assert seen["body"]["name"] == "ledger"
    assert seen["body"]["prompt"]["text"] == "Сделай CLI"
    assert seen["auth"] == "Basic " + base64.b64encode(b"secret:").decode()
    assert launch.agent_id == "bc-1"
    assert launch.run_id == "run-1"
    assert launch.url == "https://cursor.com/agents/bc-1"


@pytest.mark.asyncio
async def test_create_repo_agent_includes_the_url():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "agent": {"id": "bc-2", "status": "ACTIVE"},
                "run": {"id": "run-2", "status": "CREATING"},
            },
        )

    launch = await _client(handler).create_agent(
        name="ledger",
        prompt="Добавь README",
        repo_url="https://github.com/acme/ledger",
    )

    assert seen["body"]["repos"] == [{"url": "https://github.com/acme/ledger"}]
    assert seen["body"]["autoCreatePR"] is True
    assert launch.url == "https://cursor.com/agents/bc-2"


@pytest.mark.asyncio
async def test_get_run_reads_result_and_pull_request():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "run-1",
                "status": "finished",
                "result": "Готово",
                "git": {"branches": [{"prUrl": "https://github.com/acme/ledger/pull/3"}]},
            },
        )

    run = await _client(handler).get_run("bc-1", "run-1")
    assert run.status == "FINISHED"
    assert run.result == "Готово"
    assert run.pr_url == "https://github.com/acme/ledger/pull/3"


@pytest.mark.asyncio
async def test_unauthorized_key_is_explained_in_russian():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "nope"}})

    with pytest.raises(CursorApiError) as caught:
        await _client(handler).whoami()
    assert caught.value.status == 401
    assert "API-ключ" in explain_error(caught.value)


@pytest.mark.asyncio
async def test_list_repositories_returns_urls():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/repositories"
        return httpx.Response(
            200,
            json={"items": [{"url": "https://github.com/acme/ledger"}, {"url": ""}]},
        )

    assert await _client(handler).list_repositories() == ["https://github.com/acme/ledger"]
