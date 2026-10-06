"""HeadHunter search. The public API is preferred; the site is the fallback."""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime
from urllib.parse import urlencode

import httpx

from projectbot.jobs.model import (
    EXPERIENCE_LABELS,
    SCHEDULE_LABELS,
    SearchRequest,
    RawVacancy,
)

logger = logging.getLogger(__name__)

API_ROOT = "https://api.hh.ru"
SITE_ROOT = "https://hh.ru"
USER_AGENT = "ProjectBot/0.1 (media-job-assistant)"
BROWSER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

QUERIES = {
    "video": '"видеоинженер" OR "видео-инженер" OR "video engineer"',
    "media": '"медиаинженер" OR "медиа-инженер" OR "media engineer"',
    "vcs": '"инженер ВКС" OR "специалист ВКС" OR "видеоконференцсвязь" OR "инженер видеоконференцсвязи"',
    "broadcast": (
        '"инженер трансляций" OR "инженер прямых трансляций" OR '
        '"инженер эфирной аппаратной" OR "broadcast engineer" OR "инженер ПТС"'
    ),
    "multimedia": '"инженер мультимедиа" OR "мультимедийный инженер" OR "AV-инженер"',
}

_LUX = re.compile(r'id="HH-Lux-InitialState">(.*?)</template>', re.DOTALL)
_HIGHLIGHT = re.compile(r"</?highlighttext>", re.IGNORECASE)


class HhError(Exception):
    """HeadHunter did not return a vacancy list."""


class HhClient:
    def __init__(
        self,
        *,
        api_transport: httpx.AsyncBaseTransport | None = None,
        site_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._api = httpx.AsyncClient(
            base_url=API_ROOT,
            headers={"HH-User-Agent": USER_AGENT, "User-Agent": USER_AGENT},
            transport=api_transport,
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
        )
        self._site = httpx.AsyncClient(
            base_url=SITE_ROOT,
            headers={"User-Agent": BROWSER_AGENT, "Accept-Language": "ru"},
            transport=site_transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._api.aclose()
        await self._site.aclose()

    async def search(self, request: SearchRequest) -> tuple[list[RawVacancy], str]:
        area_id = await self._area_id(request.city) if request.city else None
        text = query_text(request.groups)
        try:
            vacancies = await self._search_api(request, text, area_id)
            source = "api"
        except HhError as error:
            logger.info("hh api search failed: %s", error)
            vacancies = await self._search_site(request, text, area_id)
            source = "site"
        if request.city and area_id is None:
            needle = request.city.casefold()
            vacancies = [item for item in vacancies if needle in item.area.casefold()]
        return vacancies, source

    async def _area_id(self, city: str) -> str | None:
        try:
            response = await self._api.get("/suggests/areas", params={"text": city})
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        items = payload.get("items") if isinstance(payload, dict) else None
        if not items:
            return None
        area_id = str(items[0].get("id") or "")
        return area_id or None

    async def _search_api(
        self,
        request: SearchRequest,
        text: str,
        area_id: str | None,
    ) -> list[RawVacancy]:
        found: dict[str, RawVacancy] = {}
        for page in (0, 1):
            params: list[tuple[str, str]] = [
                ("text", text),
                ("search_field", "name"),
                ("period", str(request.period_days)),
                ("per_page", "50"),
                ("page", str(page)),
                ("order_by", "publication_time"),
                ("no_magic", "true"),
            ]
            if area_id:
                params.append(("area", area_id))
            if request.remote_only:
                params.append(("schedule", "remote"))
            if request.with_salary:
                params.append(("only_with_salary", "true"))
            if request.experience:
                params.append(("experience", request.experience))
            try:
                response = await self._api.get("/vacancies", params=params)
            except httpx.HTTPError as error:
                raise HhError("Нет соединения с api.hh.ru.") from error
            if response.status_code == 403:
                raise HhError("api.hh.ru закрыл поиск с этого адреса.")
            if response.status_code >= 400:
                raise HhError(f"api.hh.ru ответил {response.status_code}.")
            try:
                payload = response.json()
            except ValueError as error:
                raise HhError("api.hh.ru вернул не JSON.") from error
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise HhError("В ответе api.hh.ru нет списка вакансий.")
            for item in items:
                vacancy = vacancy_from_api(item)
                if vacancy is not None:
                    found[vacancy.id] = vacancy
            pages = int(payload.get("pages") or 1)
            if page + 1 >= pages or not items:
                break
        return list(found.values())

    async def _search_site(
        self,
        request: SearchRequest,
        text: str,
        area_id: str | None,
    ) -> list[RawVacancy]:
        found: dict[str, RawVacancy] = {}
        last_page = ""
        for page in (0, 1):
            params = {
                "text": text,
                "search_field": "name",
                "items_on_page": "50",
                "search_period": str(request.period_days),
                "order_by": "publication_time",
                "page": str(page),
            }
            if area_id:
                params["area"] = area_id
            if request.remote_only:
                params["schedule"] = "remote"
            if request.with_salary:
                params["only_with_salary"] = "true"
            if request.experience:
                params["experience"] = request.experience
            url = "/search/vacancy?" + urlencode(params)
            try:
                response = await self._site.get(url)
            except httpx.HTTPError as error:
                raise HhError("Нет соединения с hh.ru.") from error
            if response.status_code >= 400:
                raise HhError(f"hh.ru ответил {response.status_code}.")
            last_page = response.text
            vacancies, total = vacancies_from_page(last_page)
            for vacancy in vacancies:
                found[vacancy.id] = vacancy
            if len(found) >= total or not vacancies:
                break
        if not found and page_has_no_state(last_page):
            raise HhError("hh.ru открыл страницу без списка вакансий.")
        return list(found.values())


def query_text(groups: tuple[str, ...]) -> str:
    parts = [QUERIES[group] for group in groups if group in QUERIES]
    return " OR ".join(f"({part})" for part in parts)


def vacancy_from_api(item: object) -> RawVacancy | None:
    if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
        return None
    salary = item.get("salary") if isinstance(item.get("salary"), dict) else {}
    snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
    schedule = item.get("schedule") if isinstance(item.get("schedule"), dict) else {}
    experience = item.get("experience") if isinstance(item.get("experience"), dict) else {}
    employer = item.get("employer") if isinstance(item.get("employer"), dict) else {}
    area = item.get("area") if isinstance(item.get("area"), dict) else {}
    schedule_id = str(schedule.get("id") or "")
    return RawVacancy(
        id=str(item["id"]),
        title=str(item["name"]),
        company=str(employer.get("name") or "Компания не указана"),
        area=str(area.get("name") or ""),
        url=str(item.get("alternate_url") or f"https://hh.ru/vacancy/{item['id']}"),
        published_at=_parse_time(item.get("published_at")),
        schedule=str(schedule.get("name") or SCHEDULE_LABELS.get(schedule_id, "")),
        experience=str(experience.get("name") or ""),
        remote=schedule_id == "remote",
        salary_from=_optional_int(salary.get("from")),
        salary_to=_optional_int(salary.get("to")),
        currency=_optional_str(salary.get("currency")),
        gross=salary.get("gross") if isinstance(salary.get("gross"), bool) else None,
        snippet=_clean_snippet(snippet.get("requirement"), snippet.get("responsibility")),
    )


def vacancies_from_page(page: str) -> tuple[list[RawVacancy], int]:
    match = _LUX.search(page)
    if match is None:
        return [], 0
    try:
        data = json.loads(html.unescape(match.group(1)))
    except ValueError:
        return [], 0
    result = data.get("vacancySearchResult") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return [], 0
    raw_items = result.get("vacancies")
    total = int(result.get("totalResults") or 0)
    if not isinstance(raw_items, list):
        return [], total
    vacancies = [vacancy for item in raw_items if (vacancy := vacancy_from_site(item)) is not None]
    return vacancies, total or len(vacancies)


def vacancy_from_site(item: object) -> RawVacancy | None:
    if not isinstance(item, dict) or not item.get("vacancyId") or not item.get("name"):
        return None
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    area = item.get("area") if isinstance(item.get("area"), dict) else {}
    links = item.get("links") if isinstance(item.get("links"), dict) else {}
    compensation = item.get("compensation") if isinstance(item.get("compensation"), dict) else {}
    if isinstance(compensation.get("salaryRange"), dict):
        compensation = compensation["salaryRange"]
    salary_from = _optional_int(compensation.get("from"))
    salary_to = _optional_int(compensation.get("to"))
    if "noCompensation" in compensation:
        salary_from = None
        salary_to = None
    schedule_id = str(item.get("@workSchedule") or "")
    experience_id = str(item.get("workExperience") or "")
    remote = schedule_id == "remote" or _has_remote_format(item.get("workFormats"))
    vacancy_id = str(item["vacancyId"])
    return RawVacancy(
        id=vacancy_id,
        title=str(item["name"]),
        company=str(company.get("visibleName") or company.get("name") or "Компания не указана"),
        area=str(area.get("name") or ""),
        url=str(links.get("desktop") or f"https://hh.ru/vacancy/{vacancy_id}"),
        published_at=_parse_time(_site_time(item.get("publicationTime"))),
        schedule=SCHEDULE_LABELS.get(schedule_id, ""),
        experience=EXPERIENCE_LABELS.get(experience_id, ""),
        remote=remote,
        salary_from=salary_from,
        salary_to=salary_to,
        currency=_optional_str(compensation.get("currencyCode")),
        gross=compensation.get("gross") if isinstance(compensation.get("gross"), bool) else None,
        snippet=_clean_snippet(item.get("snippet")),
    )


def page_has_no_state(page: str) -> bool:
    return _LUX.search(page) is None


def _site_time(value: object) -> object:
    if isinstance(value, dict):
        return value.get("$")
    return value


def _has_remote_format(value: object) -> bool:
    return "REMOTE" in json.dumps(value, ensure_ascii=False)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    # hh site uses offsets like +03:00; datetime accepts that.
    # Some stamps include milliseconds and a colonless offset.
    if re.search(r"[+-]\d{4}$", text):
        text = text[:-2] + ":" + text[-2:]
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_snippet(*parts: object) -> str:
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, str):
            continue
        text = _HIGHLIGHT.sub("", part)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            chunks.append(text)
    return " ".join(chunks)
