"""Shared fixtures. DB tests run only in the schema gzetryn_test (never public), inside the gzetryn database."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from gzetryn.gmgn.answer import RawAnswer
from gzetryn.store.db import make_engine, make_sessionmaker

PROJECT_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_SCHEMA = "gzetryn_test"
TABLES_SQL = text(
    "select tablename from pg_tables where schemaname = :s and tablename <> 'alembic_version'"
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# endpoint name → fixture answer used by FakeTransport
FIXTURE_FOR = {
    "rank_wallets": "rank-wallets-kol-7d.json",
    "wallet_activity": "wallet-activity.json",
    "wallet_new": "wallet-new-30d.json",
    "wallet_common_stat": "wallet-common-stat.json",
    "token_window_info": "token-window-info.json",
    "token_multi_info": "token-multi-info.json",
    "token_security": "token-security.json",
    "token_dev_info": "token-dev-info.json",
    "dev_created_tokens": "dev-created-tokens.json",
    "token_stat": "token-stat.json",
    "token_holder_stat": "token-holder-stat.json",
    "token_trader_stat": "token-holder-stat.json",
    "token_traders": "token-traders-renowned.json",
    "rank_swaps": "rank-swaps-1h.json",
    "new_pairs": "new-pairs.json",
    "pump_lists": "trenches.json",
}


class FakeTransport:
    """Answers from fixtures (or scripted answers), records every call."""

    def __init__(self, answers: dict[str, list[RawAnswer]] | None = None):
        self.calls: list[tuple[str, dict, dict, dict | None]] = []
        self.answers = answers or {}

    async def request(self, endpoint, path_params, params, body) -> RawAnswer:
        self.calls.append((endpoint.name, dict(path_params), dict(params), body))
        queue = self.answers.get(endpoint.name)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return RawAnswer(200, load_fixture(FIXTURE_FOR[endpoint.name]), size=1, latency_ms=5.0)

    async def close(self) -> None:
        pass


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


def _test_db_url() -> str | None:
    url = os.environ.get("GZETRYN_TEST_DATABASE_URL")
    if not url:
        env_file = PROJECT_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("GZETRYN_TEST_DATABASE_URL="):
                    url = line.split("=", 1)[1].strip()
    return url or None


def alembic(url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"url={url}", "-x", f"schema={TEST_SCHEMA}", *args],
        cwd=PROJECT_DIR,
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def db_url() -> str:
    url = _test_db_url()
    if not url:
        pytest.skip("GZETRYN_TEST_DATABASE_URL not set")
    return url


@pytest.fixture(scope="session")
async def migrated(db_url: str) -> str:
    eng = make_engine(db_url)
    async with eng.begin() as conn:
        await conn.execute(text(f"create schema if not exists {TEST_SCHEMA}"))
    await eng.dispose()
    alembic(db_url, "downgrade", "base")
    alembic(db_url, "upgrade", "head")
    return db_url


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(migrated, schema=TEST_SCHEMA)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sessions(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.begin() as conn:
        assert (await conn.execute(text("select current_schema()"))).scalar_one() == TEST_SCHEMA
        tables = (await conn.execute(TABLES_SQL, {"s": TEST_SCHEMA})).scalars().all()
        if tables:
            await conn.execute(text(f"truncate {', '.join(tables)} restart identity cascade"))
    yield make_sessionmaker(engine)
