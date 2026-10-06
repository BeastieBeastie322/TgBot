from projectbot.config import user_is_allowed
from projectbot.textutil import chunk_text, normalize_repo_url


def test_chunk_text_respects_the_limit():
    parts = chunk_text("слово " * 2000, limit=100)
    assert parts
    assert all(len(part) <= 100 for part in parts)
    assert "слово" in "".join(parts)


def test_chunk_text_keeps_a_long_token_inside_the_limit():
    parts = chunk_text("a" * 250, limit=100)
    assert [len(part) for part in parts] == [100, 100, 50]
    assert "".join(parts) == "a" * 250


def test_normalize_repo_url_accepts_common_forms():
    assert normalize_repo_url("https://github.com/acme/ledger") == "https://github.com/acme/ledger"
    assert normalize_repo_url("https://github.com/acme/ledger.git") == "https://github.com/acme/ledger"
    assert normalize_repo_url("https://github.com/acme/ledger/tree/main") == "https://github.com/acme/ledger"
    assert normalize_repo_url("git@github.com:acme/ledger.git") == "https://github.com/acme/ledger"
    assert normalize_repo_url("https://gitlab.example.com:8443/group/app") == (
        "https://gitlab.example.com:8443/group/app"
    )


def test_normalize_repo_url_rejects_loose_text():
    assert normalize_repo_url("ledger") is None
    assert normalize_repo_url("https://github.com/acme") is None
    assert normalize_repo_url("not a url") is None


def test_allow_list_opens_when_empty():
    assert user_is_allowed(5, frozenset())
    assert user_is_allowed(5, frozenset({5}))
    assert not user_is_allowed(6, frozenset({5}))
