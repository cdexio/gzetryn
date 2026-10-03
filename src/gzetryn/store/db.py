"""Async engine and session factory. `schema` sets the search_path (tests run in gzetryn_test)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(database_url: str, schema: str | None = None, **kwargs) -> AsyncEngine:
    if not database_url:
        raise RuntimeError("GZETRYN_DATABASE_URL is not set")
    if schema:
        kwargs.setdefault("connect_args", {})["server_settings"] = {"search_path": schema}
    return create_async_engine(database_url, pool_pre_ping=True, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
