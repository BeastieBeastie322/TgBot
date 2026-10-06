"""Small text helpers shared by the menu and the bot transport."""

from urllib.parse import urlsplit

TELEGRAM_TEXT_LIMIT = 4000


def chunk_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split a Telegram message without cutting past the platform limit."""
    if limit < 1:
        raise ValueError("limit must be positive")
    cleaned = text.strip()
    if not cleaned:
        return []
    parts: list[str] = []
    rest = cleaned
    while len(rest) > limit:
        window = rest[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return parts


def fit_label(text: str, limit: int = 40) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def repo_label(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    return path or url


def normalize_repo_url(raw: str) -> str | None:
    """Return https://host/owner/name, or None when the text is not a repo URL."""
    text = raw.strip()
    if text.startswith("git@") and ":" in text[4:]:
        host, path = text[4:].split(":", 1)
        text = f"https://{host}/{path}"
    if text.endswith(".git"):
        text = text[:-4]
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"https://{host}/{parts[0]}/{parts[1]}"
