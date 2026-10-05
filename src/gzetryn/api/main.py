"""`gzetryn serve`: API + directory + watcher + retention in one process on 127.0.0.1:8793."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gzetryn.api.app import Backend, create_app
from gzetryn.api.status import RuntimeStatus
from gzetryn.config import Settings, get_settings
from gzetryn.runtime import Runtime


def backend_of(rt: Runtime) -> Backend:
    return Backend(
        wallets=rt.wallets,
        feed=rt.feed,
        gateway=rt.gateway,
        directory=rt.directory,
        token=rt.token,
        status=RuntimeStatus(rt),
        t=rt.t,
        clock=rt.clock,
        candidates=rt.candidate_store,
    )


def build_app(settings: Settings | None = None, background: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def open_backend() -> AsyncIterator[Backend]:
        rt = Runtime(settings)
        await rt.start(background=background)
        try:
            yield backend_of(rt)
        finally:
            await rt.stop()

    return create_app(open_backend)
