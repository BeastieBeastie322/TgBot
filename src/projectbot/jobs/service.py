"""Search hh.ru and keep the roles this assistant is built for."""

from __future__ import annotations

from dataclasses import dataclass

from projectbot.jobs.hh import HhClient
from projectbot.jobs.model import SearchRequest, Vacancy
from projectbot.jobs.rank import rank_vacancies


@dataclass(frozen=True)
class SearchResult:
    items: tuple[Vacancy, ...]
    hidden: int
    source: str


async def search_jobs(request: SearchRequest, client: HhClient | None = None) -> SearchResult:
    checked = request.validated()
    owns_client = client is None
    hh = client or HhClient()
    try:
        raw, source = await hh.search(checked)
    finally:
        if owns_client:
            await hh.aclose()
    if checked.remote_only:
        raw = [item for item in raw if item.remote]
    if checked.with_salary:
        raw = [item for item in raw if item.salary_from is not None or item.salary_to is not None]
    ranked, hidden = rank_vacancies(raw, groups=checked.groups)
    return SearchResult(items=tuple(ranked), hidden=hidden, source=source)
