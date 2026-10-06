"""Local page for the media-engineering vacancy assistant."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from projectbot.jobs.hh import HhError
from projectbot.jobs.model import GROUPS, SearchRequest, Vacancy
from projectbot.jobs.service import search_jobs

PAGE = Path(__file__).with_name("static") / "index.html"
app = FastAPI(title="Пульт")


class SearchIn(BaseModel):
    groups: list[str] = Field(default_factory=lambda: list(GROUPS))
    city: str = ""
    remote_only: bool = False
    with_salary: bool = False
    period_days: int = 14
    experience: str = ""


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(PAGE)


@app.post("/api/search")
async def search(body: SearchIn) -> dict[str, object]:
    try:
        request = SearchRequest(
            groups=tuple(body.groups),
            city=body.city,
            remote_only=body.remote_only,
            with_salary=body.with_salary,
            period_days=body.period_days,
            experience=body.experience,
        ).validated()
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        result = await search_jobs(request)
    except HhError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return {
        "items": [_vacancy(item) for item in result.items],
        "hidden": result.hidden,
        "source": result.source,
    }


def _vacancy(item: Vacancy) -> dict[str, object]:
    return {
        "id": item.id,
        "title": item.title,
        "company": item.company,
        "area": item.area,
        "url": item.url,
        "published": item.published,
        "schedule": item.schedule,
        "experience": item.experience,
        "salary": item.salary,
        "snippet": item.snippet,
        "tags": list(item.tags),
    }


def main() -> None:
    import uvicorn

    port = int(os.environ.get("JOBS_PORT", "47291"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
