import pytest

from projectbot.store import Store


@pytest.fixture
def store(tmp_path):
    database = Store(tmp_path / "bot.sqlite")
    database.migrate()
    yield database
    database.close()
