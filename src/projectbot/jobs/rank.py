"""Keep vacancies that match media, video, VCS, or broadcast engineering."""

from __future__ import annotations

import re

from projectbot.jobs.model import GROUP_LABELS, RawVacancy, Vacancy, format_published, format_salary

# Title matches are the signal. Description-only hits stay below the cutoff
# unless two different directions mention the role.
_GROUPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("video", re.compile(r"видео[\s-]?инженер|video engineer", re.IGNORECASE)),
    ("media", re.compile(r"медиа[\s-]?инженер|media engineer", re.IGNORECASE)),
    ("vcs", re.compile(r"видеоконференц|\bвкс\b", re.IGNORECASE)),
    (
        "broadcast",
        re.compile(
            r"трансляц|broadcast|стриминг|streaming|эфирн|\bптс\b|передвижн\w* телевизион",
            re.IGNORECASE,
        ),
    ),
    ("multimedia", re.compile(r"мультимедиа|\bav[\s-]?инженер|\bинженер\s+av\b", re.IGNORECASE)),
)
_NEGATIVE = re.compile(
    r"видеонаблюден|охранн|\bскуд\b|домофон|менеджер по продаж|курьер|продавец|бухгалтер|"
    r"generative video|генеративн",
    re.IGNORECASE,
)
_TITLE_SCORE = 10
_SNIPPET_SCORE = 4
_NEGATIVE_PENALTY = 12
_CUTOFF = 8


def rank_vacancies(
    vacancies: list[RawVacancy],
    *,
    groups: tuple[str, ...],
    now=None,
) -> tuple[list[Vacancy], int]:
    """Return ranked matches and how many fetched vacancies were set aside."""
    selected = set(groups)
    ranked: list[Vacancy] = []
    hidden = 0
    for vacancy in vacancies:
        scored = _score(vacancy, selected)
        if scored is None:
            hidden += 1
            continue
        score, tags = scored
        ranked.append(
            Vacancy(
                id=vacancy.id,
                title=vacancy.title,
                company=vacancy.company,
                area=vacancy.area,
                url=vacancy.url,
                published=format_published(vacancy.published_at, now=now),
                schedule=vacancy.schedule,
                experience=vacancy.experience,
                salary=format_salary(vacancy),
                snippet=vacancy.snippet,
                tags=tags,
                score=score,
                published_at=vacancy.published_at,
            )
        )
    ranked.sort(key=lambda item: (item.score, item.published_at or _epoch()), reverse=True)
    return ranked, hidden


def _score(vacancy: RawVacancy, selected: set[str]) -> tuple[int, tuple[str, ...]] | None:
    title = vacancy.title or ""
    snippet = vacancy.snippet or ""
    if _NEGATIVE.search(title):
        return None
    tags: list[str] = []
    score = 0
    for group, pattern in _GROUPS:
        in_title = pattern.search(title) is not None
        in_snippet = pattern.search(snippet) is not None
        if not in_title and not in_snippet:
            continue
        if group not in selected:
            continue
        tags.append(GROUP_LABELS[group])
        score += _TITLE_SCORE if in_title else _SNIPPET_SCORE
    if score < _CUTOFF or not tags:
        return None
    return score, tuple(tags)


def _epoch():
    from datetime import datetime, timezone

    return datetime.min.replace(tzinfo=timezone.utc)
