"""Loopback HTTP API for the engine (spec §11; contract in docs/contract.md)."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gzetryn import __version__
from gzetryn.config import Tunables
from gzetryn.core.copyscore import score_rows
from gzetryn.core.curation import check
from gzetryn.gateway.gateway import BadRequest, Gateway, NotFound, Result, Unavailable, UpstreamError
from gzetryn.gmgn import endpoints as E
from gzetryn.gmgn import parse
from gzetryn.jobs.directory import Directory
from gzetryn.jobs.token import DEFAULT_PARTS, PARTS, TokenIntel
from gzetryn.store.feed import FeedStore
from gzetryn.store.wallets import WalletStore

CONSUMER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ADDRESS = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"
TAG_RE = r"^[A-Za-z0-9_:.-]{1,32}$"


@dataclass
class Backend:
    wallets: WalletStore
    feed: FeedStore
    gateway: Gateway
    directory: Directory
    token: TokenIntel
    status: Any  # RuntimeStatus: async health(), async stats()
    t: Tunables
    clock: Any
    candidates: Any = None  # CandidateStore


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after_sec: float | None = None):
        self.status, self.code, self.message, self.retry_after_sec = status, code, message, retry_after_sec


def _error(status: int, code: str, message: str, retry_after_sec: float | None = None) -> JSONResponse:
    body: dict = {"error": {"code": code, "message": message}}
    headers = {}
    if retry_after_sec is not None:
        secs = int(max(1, round(retry_after_sec)))
        body["error"]["retry_after_sec"] = secs
        headers["Retry-After"] = str(secs)
    return JSONResponse(body, status_code=status, headers=headers)


def consumer(x_consumer: Annotated[str | None, Header()] = None) -> str:
    value = (x_consumer or "").strip().lower()
    if not CONSUMER_RE.fullmatch(value):
        raise ApiError(400, "missing_consumer", "header X-Consumer is required (e.g. zetryn)")
    return value


def backend(request: Request) -> Backend:
    return request.app.state.backend


Svc = Annotated[Backend, Depends(backend)]
Consumer = Annotated[str, Depends(consumer)]
Address = Annotated[str, Path(pattern=ADDRESS, description="Solana address (base58, 32-44 chars)")]
Limit = Annotated[int, Query(ge=1, description="capped by the server's max_limit")]
MaxAge = Annotated[float | None, Query(ge=0, description="accept cached GMGN data up to this age (seconds)")]


def _limit(svc: Backend, limit: int) -> int:
    return max(1, min(limit, svc.t.api.max_limit))


def _meta(r: Result | None = None, **extra) -> dict:
    meta: dict[str, Any] = {"cached": False, "stale": False, "age_sec": 0.0, "fetched_at": None}
    if r is not None:
        meta.update(cached=r.cached, stale=r.stale, age_sec=round(r.age_sec, 1), fetched_at=r.fetched_at.isoformat())
    meta.update(extra)
    return meta


def _envelope(data: Any, meta: dict | None = None, next_cursor: str | None = None) -> dict:
    return {"data": data, "next_cursor": next_cursor, "meta": meta or _meta()}


class ManualWallet(BaseModel):
    address: str = Field(pattern=ADDRESS)
    label: str | None = Field(None, max_length=128)
    tags: list[Annotated[str, Field(pattern=TAG_RE)]] | None = Field(None, max_length=20)
    note: str | None = Field(None, max_length=2000)


class ManualWalletPatch(BaseModel):
    label: str | None = Field(None, max_length=128)
    tags: list[Annotated[str, Field(pattern=TAG_RE)]] | None = Field(None, max_length=20)
    note: str | None = Field(None, max_length=2000)


def create_app(open_backend: Callable[[], AbstractAsyncContextManager[Backend]]) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_backend() as b:
            app.state.backend = b
            yield

    app = FastAPI(
        title="gzetryn",
        version=__version__,
        description="GMGN web-data service for ZetrynAI: KOL/smart-money directory, wallet trade feed, token intel. "
        "Loopback only. Contract: docs/contract.md.",
        lifespan=lifespan,
    )

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError) -> JSONResponse:
        return _error(e.status, e.code, e.message, e.retry_after_sec)

    @app.exception_handler(Unavailable)
    async def _unavailable(_: Request, e: Unavailable) -> JSONResponse:
        return _error(503, "unavailable", e.reason, e.retry_after_sec)

    @app.exception_handler(BadRequest)
    async def _bad(_: Request, e: BadRequest) -> JSONResponse:
        return _error(400, "bad_request", e.message)

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound) -> JSONResponse:
        return _error(404, "not_found", e.message)

    @app.exception_handler(UpstreamError)
    async def _upstream(_: Request, e: UpstreamError) -> JSONResponse:
        return _error(502, "upstream", e.message)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: RequestValidationError) -> JSONResponse:
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", []))
        return _error(400, "invalid_parameter", f"{where}: {first.get('msg', 'invalid')}")

    # ---------- operations ----------

    @app.get("/health", tags=["ops"])
    async def health(svc: Svc, _: Consumer):
        return await svc.status.health()

    @app.get("/v1/stats", tags=["ops"])
    async def stats(svc: Svc, _: Consumer):
        return _envelope(await svc.status.stats())

    @app.post("/v1/admin/refresh", tags=["ops"])
    async def admin_refresh(svc: Svc, _: Consumer, curate: bool = False):
        return _envelope(await svc.directory.cycle(force_curation=curate))

    @app.post("/v1/admin/curate", tags=["ops"])
    async def admin_curate(svc: Svc, _: Consumer):
        return _envelope(await svc.directory.curate_now())

    # ---------- directory ----------

    @app.get("/v1/wallets", tags=["directory"])
    async def wallets(
        svc: Svc,
        _: Consumer,
        status: Literal["curated", "manual", "active", "ranked", "inactive", "all"] = "active",
        tag: Annotated[str | None, Query(pattern=TAG_RE)] = None,
        q: Annotated[str | None, Query(min_length=2, max_length=64)] = None,
        limit: Limit = 500,
    ):
        return _envelope(await svc.wallets.list_wallets(status, tag, q, _limit(svc, limit)))

    @app.get("/v1/wallets/{address}", tags=["directory"])
    async def wallet(svc: Svc, _: Consumer, address: Address, trades: Annotated[int, Query(ge=0, le=200)] = 20):
        w = await svc.wallets.get(address)
        if w is None:
            raise ApiError(404, "not_found", f"wallet {address} is not in the directory")
        w["recent_trades"] = await svc.feed.for_wallet(address, trades) if trades else []
        return _envelope(w)

    @app.get("/v1/wallets/{address}/history", tags=["directory"])
    async def wallet_history(
        svc: Svc,
        _: Consumer,
        address: Address,
        period: Literal["7d", "30d"] | None = None,
        days: Annotated[int, Query(ge=1, le=365)] = 30,
        limit: Limit = 1000,
    ):
        since = svc.clock.now() - timedelta(days=days)
        return _envelope(await svc.wallets.history(address, period, since, _limit(svc, limit)))

    @app.get("/v1/wallets/{address}/stats", tags=["directory"])
    async def wallet_stats(svc: Svc, c: Consumer, address: Address, max_age_sec: MaxAge = None):
        r = await svc.gateway.call(
            E.WALLET_NEW, path={"address": address}, priority="P1", consumer=c, max_age_sec=max_age_sec
        )
        row = parse.wallet_new(r.body, address)
        if row is None:
            raise ApiError(404, "not_found", f"GMGN has no stats for {address}")
        return _envelope(row.as_dict(), _meta(r, source="walletNew?period=30d", winrate_note="null without login"))

    @app.post("/v1/wallets", tags=["directory"])
    async def add_wallet(svc: Svc, c: Consumer, body: ManualWallet):
        w, created = await svc.wallets.add_manual(body.address, body.label, body.tags, body.note, c, svc.clock.now())
        if created and not w["metrics"]["at"]:
            svc.directory.kick_manual(body.address)
        return JSONResponse(_envelope(w, _meta(created=created)), status_code=201 if created else 200)

    @app.patch("/v1/wallets/{address}", tags=["directory"])
    async def patch_wallet(svc: Svc, _: Consumer, address: Address, body: ManualWalletPatch):
        w = await svc.wallets.update_manual(address, body.label, body.tags, body.note)
        if w is None:
            raise ApiError(404, "not_found", f"{address} is not a manual wallet")
        return _envelope(w)

    @app.delete("/v1/wallets/{address}", tags=["directory"])
    async def delete_wallet(svc: Svc, _: Consumer, address: Address):
        w = await svc.wallets.remove_manual(address)
        if w is None:
            raise ApiError(404, "not_found", f"{address} is not a manual wallet")
        return _envelope(w)

    @app.get("/v1/curation", tags=["directory"])
    async def curation(svc: Svc, _: Consumer):
        runs = await svc.wallets.curation_runs(1)
        return _envelope({"rule": svc.t.curation.model_dump(), "latest": runs[0] if runs else None})

    @app.get("/v1/curation/runs", tags=["directory"])
    async def curation_runs(svc: Svc, _: Consumer, limit: Annotated[int, Query(ge=1, le=100)] = 10):
        return _envelope(await svc.wallets.curation_runs(limit))

    # ---------- leaderboards ----------

    @app.get("/v1/leaderboard", tags=["leaderboard"])
    async def leaderboard(
        svc: Svc,
        _: Consumer,
        period: Literal["7d", "30d"] = "30d",
        tag: Literal["kol", "smart_degen", "all"] = "all",
        sort: Literal["profit", "pnl", "winrate"] = "profit",
        limit: Limit = 100,
    ):
        tags = list(svc.t.rank.tags) if tag == "all" else [tag]
        rows, meta = await svc.wallets.leaderboard(period, tags, sort, _limit(svc, limit))
        return _envelope(rows, _meta(**meta))

    @app.get("/v1/leaderboard/copy", tags=["leaderboard"])
    async def leaderboard_copy(svc: Svc, _: Consumer, limit: Limit = 50):
        rows = await svc.wallets.copy_rows(
            list(svc.t.rank.tags), list(svc.t.rank.periods), svc.t.copy_score.presence_days, svc.clock.now()
        )
        passing = []
        for r in rows:
            cand = r.pop("candidate")
            if check(cand, svc.t.curation) is None:
                passing.append(r)
        scored = score_rows(passing, svc.t.copy_score)
        return _envelope(
            scored[: _limit(svc, limit)],
            _meta(candidates=len(rows), passed=len(passing), weights=svc.t.copy_score.model_dump()),
        )

    # ---------- feed ----------

    @app.get("/v1/feed", tags=["feed"])
    async def feed(
        svc: Svc,
        _: Consumer,
        after: Annotated[int | None, Query(ge=0, description="last seq already consumed (0 = from the start)")] = None,
        since: Annotated[int | None, Query(ge=0, description="alias of `after`")] = None,
        limit: Limit = 500,
        wait: Annotated[float, Query(ge=0, le=30, description="long-poll seconds when nothing is new")] = 0,
        wallet: Annotated[str | None, Query(pattern=ADDRESS)] = None,
        mint: Annotated[str | None, Query(pattern=ADDRESS)] = None,
        side: Literal["buy", "sell"] | None = None,
        tag: Annotated[str | None, Query(pattern=TAG_RE)] = None,
        baseline: bool = False,
        include_tagged: Annotated[
            bool, Query(description="also the watch-only GMGN-tagged wallets (phase 14); off for copy-trading consumers")
        ] = False,
    ):
        cursor0 = after if after is not None else (since or 0)
        deadline = time.monotonic() + min(wait, svc.t.api.feed_max_wait_sec)
        while True:
            rows, cursor = await svc.feed.read(
                cursor0,
                wallet=wallet,
                mint=mint,
                side=side,
                tag=tag,
                include_baseline=baseline,
                include_tagged=include_tagged,
                limit=_limit(svc, limit),
            )
            if rows or time.monotonic() >= deadline:
                break
            await asyncio.sleep(1.0)
        return _envelope(rows, _meta(last_seq=await svc.feed.last_seq()), next_cursor=str(cursor))

    # ---------- token intel ----------

    @app.get("/v1/token/{mint}", tags=["token"])
    async def token(
        svc: Svc,
        c: Consumer,
        mint: Address,
        parts: Annotated[
            str | None,
            Query(
                description="comma list of: " + ",".join(PARTS)
                + " (default: all but snipers and tag_buyers, which are opt-in)"
            ),
        ] = None,
        max_age_sec: MaxAge = None,
        window_min: Annotated[
            int | None, Query(ge=1, le=1440, description="tag_buyers look-back in minutes (default tagged.buyers_window_min)")
        ] = None,
    ):
        wanted = {p.strip() for p in parts.split(",") if p.strip()} if parts else set(DEFAULT_PARTS)
        bad = wanted - set(PARTS)
        if bad:
            raise ApiError(400, "invalid_parameter", f"unknown parts: {','.join(sorted(bad))}")
        out = await svc.token.get(mint, wanted, c, max_age_sec, window_min=window_min)
        meta = out.pop("_meta")
        return _envelope(out, _meta(**meta))

    # ---------- market lists ----------

    @app.get("/v1/market/candidates", tags=["market"])
    async def market_candidates(
        svc: Svc,
        _: Consumer,
        kind: Annotated[
            str | None, Query(description="comma list of new,completing,migrated,trending (default: all)")
        ] = None,
        after: Annotated[int, Query(ge=0, description="last seq already consumed (0 = from the start)")] = 0,
        limit: Limit = 500,
        wait: Annotated[float, Query(ge=0, le=30, description="long-poll seconds when nothing is new")] = 0,
    ):
        kinds = [k.strip() for k in kind.split(",") if k.strip()] if kind else None
        allowed = {"new", "completing", "migrated", "trending"}
        if kinds and set(kinds) - allowed:
            raise ApiError(400, "invalid_parameter", f"kind must be among {','.join(sorted(allowed))}")
        deadline = time.monotonic() + min(wait, svc.t.api.feed_max_wait_sec)
        while True:
            rows, cursor = await svc.candidates.read(after, kinds, _limit(svc, limit))
            if rows or time.monotonic() >= deadline:
                break
            await asyncio.sleep(1.0)
        return _envelope(rows, _meta(last_seq=await svc.candidates.last_seq()), next_cursor=str(cursor))

    @app.get("/v1/market/trending", tags=["market"])
    async def trending(
        svc: Svc,
        c: Consumer,
        interval: Literal["1m", "5m", "1h", "6h", "24h"] = "1h",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        max_age_sec: MaxAge = None,
    ):
        r = await svc.gateway.call(
            E.RANK_SWAPS,
            path={"interval": interval},
            params={"limit": limit},
            priority="P1",
            consumer=c,
            max_age_sec=max_age_sec,
        )
        return _envelope(parse.market_rows(r.body, "rank"), _meta(r, source="rank/swaps"))

    @app.get("/v1/market/new-pairs", tags=["market"])
    async def new_pairs(
        svc: Svc,
        c: Consumer,
        interval: Literal["1m", "5m", "1h", "6h", "24h"] = "1h",
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        max_age_sec: MaxAge = None,
    ):
        r = await svc.gateway.call(
            E.NEW_PAIRS,
            path={"interval": interval},
            params={"limit": limit},
            priority="P1",
            consumer=c,
            max_age_sec=max_age_sec,
        )
        return _envelope(parse.market_rows(r.body, "pairs"), _meta(r, source="pairs/new_pairs"))

    @app.get("/v1/market/pump", tags=["market"])
    async def pump(
        svc: Svc, c: Consumer, limit: Annotated[int, Query(ge=1, le=100)] = 30, max_age_sec: MaxAge = None
    ):
        r = await svc.gateway.call(
            E.PUMP_LISTS, body=E.pump_lists_body(limit), priority="P1", consumer=c, max_age_sec=max_age_sec
        )
        return _envelope(parse.pump_lists(r.body), _meta(r, source="vas/rank (Pump.fun)"))

    return app
