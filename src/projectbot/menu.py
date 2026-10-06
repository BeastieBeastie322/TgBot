"""Pure menu text and inline buttons."""

from dataclasses import dataclass

from projectbot.textutil import fit_label

PAGE_SIZE = 6

PROJECTS_BUTTON = "Проекты"
NEW_BUTTON = "Новый проект"
STATUS_BUTTON = "Статус"
REPLY_KEYBOARD = (PROJECTS_BUTTON, NEW_BUTTON, STATUS_BUTTON)


@dataclass(frozen=True)
class Button:
    text: str
    data: str


@dataclass(frozen=True)
class Outgoing:
    text: str
    buttons: tuple[tuple[Button, ...], ...] = ()
    show_reply_keyboard: bool = False


def page_window(total: int, page: int, size: int = PAGE_SIZE) -> tuple[int, int, int]:
    """Return start index, clamped page, and page count."""
    pages = max(1, (total + size - 1) // size) if total else 1
    current = min(max(page, 0), pages - 1)
    return current * size, current, pages


def nav_row(prefix: str, page: int, pages: int) -> tuple[Button, ...]:
    row: list[Button] = []
    if page > 0:
        row.append(Button("←", f"{prefix}:page:{page - 1}"))
    if pages > 1:
        row.append(Button(f"{page + 1}/{pages}", f"{prefix}:page:{page}"))
    if page < pages - 1:
        row.append(Button("→", f"{prefix}:page:{page + 1}"))
    return tuple(row)


def project_buttons(
    items: list[tuple[int, str, bool]],
    page: int,
    pages: int,
) -> tuple[tuple[Button, ...], ...]:
    rows: list[tuple[Button, ...]] = []
    for project_id, name, active in items:
        label = fit_label(("• " if active else "") + name)
        rows.append((Button(label, f"p:open:{project_id}"),))
    navigation = nav_row("p", page, pages)
    if navigation:
        rows.append(navigation)
    rows.append(
        (
            Button("Новый проект", "m:new"),
            Button("Репозитории", "m:repos"),
        )
    )
    return tuple(rows)


def repo_buttons(
    items: list[tuple[int, str, bool]],
    page: int,
    pages: int,
    *,
    attach: bool,
) -> tuple[tuple[Button, ...], ...]:
    rows: list[tuple[Button, ...]] = []
    prefix = "n:attach" if attach else "r:open"
    for index, label, opened in items:
        mark = "✓ " if opened else ""
        rows.append((Button(fit_label(mark + label), f"{prefix}:{index}"),))
    navigation = nav_row("r", page, pages)
    if navigation:
        rows.append(navigation)
    if attach:
        rows.append((Button("Обновить", "r:refresh"), Button("Отмена", "w:cancel")))
    else:
        rows.append((Button("Обновить", "r:refresh"), Button("К проектам", "m:projects")))
    return tuple(rows)
