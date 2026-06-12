import asyncio

import pytest

from reqsmith import settings as settings_module
from reqsmith.persistence import db as db_module
from reqsmith.persistence.models import Base


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Fresh file-backed SQLite per test; production uses Postgres via the same code paths."""
    url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    monkeypatch.setenv("DATABASE_URL", url)
    settings_module.get_settings.cache_clear()
    db_module.reset_db_state()

    async def _create():
        engine = db_module.get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_create())
    yield
    settings_module.get_settings.cache_clear()
    db_module.reset_db_state()


@pytest.fixture
def sample_webhook_payload():
    return {
        "webhookEvent": "jira:issue_created",
        "issue": {
            "key": "BANK-101",
            "fields": {
                "issuetype": {"name": "Epic"},
                "summary": "Customer onboarding revamp",
                "description": "Digitise KYC intake for retail onboarding.",
            },
        },
    }
