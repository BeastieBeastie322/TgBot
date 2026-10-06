import html
import json
from datetime import datetime, timezone
from urllib.parse import unquote

import httpx
import pytest

from projectbot.jobs.hh import HhClient, query_text, vacancies_from_page, vacancy_from_api
from projectbot.jobs.model import RawVacancy, SearchRequest
from projectbot.jobs.rank import rank_vacancies
from projectbot.jobs.service import search_jobs


def _raw(**overrides) -> RawVacancy:
    data = dict(
        id="1",
        title="Видеоинженер",
        company="Студия",
        area="Москва",
        url="https://hh.ru/vacancy/1",
        published_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
        schedule="Полный день",
        experience="1–3 года",
        remote=False,
        salary_from=None,
        salary_to=None,
        currency=None,
        gross=None,
        snippet="",
    )
    data.update(overrides)
    return RawVacancy(**data)


def test_rank_keeps_broadcast_roles_and_drops_neighbors():
    now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    kept, hidden = rank_vacancies(
        [
            _raw(id="1", title="Видеоинженер эфирной аппаратной"),
            _raw(id="2", title="Специалист ВКС"),
            _raw(id="3", title="Инженер видеонаблюдения"),
            _raw(id="4", title="Менеджер по продажам трансляций", snippet="трансляции для клиентов"),
            _raw(id="5", title="Инженер мультимедиа"),
            _raw(id="6", title="Senior AI / Generative Video Engineer"),
        ],
        groups=("video", "media", "vcs", "broadcast", "multimedia"),
        now=now,
    )
    assert [item.id for item in kept] == ["1", "2", "5"]
    assert hidden == 3
    assert kept[0].tags == ("Видеоинженер", "Трансляции")
    assert kept[0].published == "12 ч. назад"


def test_query_uses_only_selected_directions():
    text = query_text(("vcs",))
    assert "ВКС" in text
    assert "видеоинженер" not in text


def test_api_vacancy_parses_salary_and_snippet():
    vacancy = vacancy_from_api(
        {
            "id": "10",
            "name": "Медиаинженер",
            "alternate_url": "https://hh.ru/vacancy/10",
            "published_at": "2026-10-05T10:00:00+0300",
            "area": {"name": "Казань"},
            "employer": {"name": "Театр"},
            "schedule": {"id": "remote", "name": "Удаленная работа"},
            "experience": {"id": "between3And6", "name": "От 3 до 6 лет"},
            "salary": {"from": 150000, "to": 200000, "currency": "RUR", "gross": True},
            "snippet": {
                "requirement": "Опыт <highlighttext>ВКС</highlighttext>",
                "responsibility": "Сопровождение эфира",
            },
        }
    )
    assert vacancy is not None
    assert vacancy.remote is True
    assert vacancy.salary_from == 150000
    assert "ВКС" in vacancy.snippet
    assert "<" not in vacancy.snippet
    assert vacancy.published_at is not None


def test_site_page_parses_lux_state():
    payload = {
        "vacancySearchResult": {
            "totalResults": 1,
            "vacancies": [
                {
                    "vacancyId": 77,
                    "name": "Инженер ПТС",
                    "company": {"visibleName": "Матч"},
                    "area": {"name": "Москва"},
                    "links": {"desktop": "https://hh.ru/vacancy/77"},
                    "publicationTime": {"$": "2026-10-06T10:00:00+03:00"},
                    "@workSchedule": "fullDay",
                    "workExperience": "between1And3",
                    "workFormats": [{"workFormatsElement": ["ON_SITE"]}],
                    "compensation": {"from": 180000, "to": None, "currencyCode": "RUR", "gross": False},
                }
            ],
        }
    }
    page = f'<template id="HH-Lux-InitialState">{html.escape(json.dumps(payload))}</template>'
    vacancies, total = vacancies_from_page(page)
    assert total == 1
    assert vacancies[0].title == "Инженер ПТС"
    assert vacancies[0].salary_from == 180000
    assert vacancies[0].gross is False


class _ScriptClient:
    def __init__(self, vacancies: list[RawVacancy]):
        self.vacancies = vacancies

    async def search(self, request: SearchRequest):
        assert request.city == "Казань"
        return list(self.vacancies), "api"


@pytest.mark.asyncio
async def test_service_applies_remote_and_salary_filters():
    result = await search_jobs(
        SearchRequest(city="Казань", remote_only=True, with_salary=True),
        client=_ScriptClient(
            [
                _raw(id="1", title="Видеоинженер", remote=True, salary_from=100000, currency="RUR"),
                _raw(id="2", title="Видеоинженер удалённо", remote=False, salary_from=100000),
                _raw(id="3", title="Видеоинженер без вилки", remote=True),
            ]
        ),
    )
    assert [item.id for item in result.items] == ["1"]
    assert result.items[0].salary.startswith("от 100 000")


@pytest.mark.asyncio
async def test_client_falls_back_to_the_site_when_the_api_is_closed():
    payload = {
        "vacancySearchResult": {
            "totalResults": 1,
            "vacancies": [
                {
                    "vacancyId": 5,
                    "name": "Видеоинженер",
                    "company": {"visibleName": "Эфир"},
                    "area": {"name": "Москва"},
                    "links": {"desktop": "https://hh.ru/vacancy/5"},
                    "compensation": {"noCompensation": {}},
                    "publicationTime": {"$": "2026-10-06T09:00:00+03:00"},
                    "@workSchedule": "fullDay",
                    "workExperience": "noExperience",
                }
            ],
        }
    }
    page = f'<html><template id="HH-Lux-InitialState">{html.escape(json.dumps(payload, ensure_ascii=False))}</template></html>'

    def api_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/suggests/areas":
            return httpx.Response(200, json={"items": [{"id": "1", "text": "Москва"}]})
        return httpx.Response(403, json={"errors": [{"type": "forbidden"}]})

    def site_handler(request: httpx.Request) -> httpx.Response:
        query = unquote(str(request.url.query))
        assert "видеоинженер" in query
        assert "area=1" in query
        return httpx.Response(200, text=page)

    client = HhClient(
        api_transport=httpx.MockTransport(api_handler),
        site_transport=httpx.MockTransport(site_handler),
    )
    vacancies, source = await client.search(SearchRequest(city="Москва", groups=("video",)))
    await client.aclose()
    assert source == "site"
    assert vacancies[0].company == "Эфир"
    assert vacancies[0].title == "Видеоинженер"
