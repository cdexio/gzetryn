"""Alembic environment (async engine).

URL: `-x url=...` or GZETRYN_DATABASE_URL (env or .env). Schema: `-x schema=...` or GZETRYN_DB_SCHEMA; the DB tests
migrate the schema `gzetryn_test` this way, the service uses the default search_path (public).
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

from gzetryn.config import PROJECT_DIR
from gzetryn.store.db import make_engine
from gzetryn.store.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip() or None
    return None


def _database_url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or _env("GZETRYN_DATABASE_URL")
    if not url:
        raise RuntimeError("set GZETRYN_DATABASE_URL (or pass -x url=...)")
    return url


def _schema() -> str | None:
    return context.get_x_argument(as_dictionary=True).get("schema") or _env("GZETRYN_DB_SCHEMA")


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    schema = _schema()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table_schema=schema,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = make_engine(_database_url(), schema=_schema())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
