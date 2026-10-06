"""Vacancy records shared by the HeadHunter client and the ranker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

GROUPS = ("video", "media", "vcs", "broadcast", "multimedia")
GROUP_LABELS = {
    "video": "Видеоинженер",
    "media": "Медиаинженер",
    "vcs": "ВКС",
    "broadcast": "Трансляции",
    "multimedia": "Мультимедиа",
}
EXPERIENCES = {
    "": "Любой опыт",
    "noExperience": "Без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "Более 6 лет",
}
EXPERIENCE_LABELS = {
    "noExperience": "Без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "Более 6 лет",
}
SCHEDULE_LABELS = {
    "fullDay": "Полный день",
    "remote": "Удалённо",
    "flexible": "Гибкий график",
    "shift": "Сменный график",
    "flyInFlyOut": "Вахта",
}
MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


@dataclass(frozen=True)
class SearchRequest:
    groups: tuple[str, ...] = GROUPS
    city: str = ""
    remote_only: bool = False
    with_salary: bool = False
    period_days: int = 14
    experience: str = ""

    def validated(self) -> SearchRequest:
        chosen = tuple(group for group in self.groups if group in GROUP_LABELS)
        if not chosen:
            raise ValueError("Выберите хотя бы одно направление.")
        if self.experience not in EXPERIENCES:
            raise ValueError("Неизвестный фильтр опыта.")
        period = self.period_days
        if period < 1 or period > 30:
            raise ValueError("Период может быть от 1 до 30 дней.")
        city = " ".join(self.city.split())
        if len(city) > 80:
            raise ValueError("Слишком длинное название города.")
        return SearchRequest(
            groups=chosen,
            city=city,
            remote_only=self.remote_only,
            with_salary=self.with_salary,
            period_days=period,
            experience=self.experience,
        )


@dataclass(frozen=True)
class RawVacancy:
    id: str
    title: str
    company: str
    area: str
    url: str
    published_at: datetime | None
    schedule: str
    experience: str
    remote: bool
    salary_from: int | None
    salary_to: int | None
    currency: str | None
    gross: bool | None
    snippet: str


@dataclass(frozen=True)
class Vacancy:
    id: str
    title: str
    company: str
    area: str
    url: str
    published: str
    schedule: str
    experience: str
    salary: str
    snippet: str
    tags: tuple[str, ...]
    score: int
    published_at: datetime | None = field(compare=False, default=None)


def format_salary(vacancy: RawVacancy) -> str:
    if vacancy.salary_from is None and vacancy.salary_to is None:
        return "Зарплата не указана"
    currency = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€"}.get(
        vacancy.currency or "",
        vacancy.currency or "",
    )
    start = _money(vacancy.salary_from)
    end = _money(vacancy.salary_to)
    if start and end:
        body = f"{start}–{end}"
    elif start:
        body = f"от {start}"
    else:
        body = f"до {end}"
    tax = ""
    if vacancy.gross is True:
        tax = " до налогов"
    elif vacancy.gross is False:
        tax = " на руки"
    return f"{body} {currency}{tax}".strip()


def format_published(moment: datetime | None, *, now: datetime | None = None) -> str:
    if moment is None:
        return ""
    current = now or datetime.now(moment.tzinfo)
    delta = current - moment
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "только что"
    if hours < 24:
        return f"{hours} ч. назад"
    if delta.days == 1:
        return "вчера"
    return f"{moment.day} {MONTHS[moment.month - 1]}"


def _money(value: int | None) -> str:
    if value is None:
        return ""
    return f"{value:,}".replace(",", " ")
