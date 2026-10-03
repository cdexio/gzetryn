"""Budget and gateway on a fake clock and a fake transport."""

from __future__ import annotations

import asyncio

import pytest

from gzetryn.clock import FakeClock
from gzetryn.config import BudgetTunables, Tunables
from gzetryn.gateway.budget import Budget
from gzetryn.gateway.gateway import BadRequest, Gateway, NotFound, Unavailable, UpstreamError
from gzetryn.gmgn import endpoints as E
from gzetryn.gmgn.answer import RawAnswer
from gzetryn.transport.http import query_pairs
from tests.conftest import FakeTransport

OK_BODY = {"code": 0, "data": {"rank": []}}


def make(answers=None, **budget):
    clock = FakeClock()
    t = Tunables(budget=BudgetTunables(**budget))
    tr = FakeTransport(answers)
    return Gateway(t, tr, Budget(t.budget, clock), clock=clock), tr, clock


def test_query_pairs_repeat_list_values():
    pairs = query_pairs({"type": ["buy", "sell"]}, {"wallet": "W", "limit": 20, "cursor": None, "x": True})
    assert pairs == [("type", "buy"), ("type", "sell"), ("wallet", "W"), ("limit", "20"), ("x", "true")]


async def test_budget_reserves_and_min_gap():
    clock = FakeClock()
    b = Budget(BudgetTunables(per_minute=60, burst=4, min_gap_sec=0.5, reserve_p1=1, reserve_p2=2), clock)
    assert await b.acquire("P2", 0.0)  # tokens 4 → 3
    assert not await b.acquire("P2", 0.0)  # min gap not over
    clock.advance(0.5)
    assert await b.acquire("P2", 0.0)  # 3.5 → 2.5
    clock.advance(0.5)
    assert await b.acquire("P2", 0.0)  # 3.0 ≥ 1 + reserve 2 → 2.0
    clock.advance(0.5)
    assert not await b.acquire("P2", 0.0)  # 2.5 < 3: P2 must leave 2 tokens
    assert await b.acquire("P1", 0.0)  # P1 needs 2 → 1.5
    clock.advance(0.5)
    assert await b.acquire("P0", 0.0)  # P0 may take the last tokens → 1.0
    assert b.granted == {"P0": 1, "P1": 1, "P2": 3}


async def test_budget_p0_takes_last_token_and_waits():
    clock = FakeClock()
    b = Budget(BudgetTunables(per_minute=60, burst=2, min_gap_sec=0, reserve_p1=1, reserve_p2=1), clock)
    assert await b.acquire("P0", 0)
    assert await b.acquire("P0", 0)
    assert not await b.acquire("P1", 0)
    t0 = clock.monotonic()
    assert await b.acquire("P0", 5.0)  # waits ~1 s for one token at 1/s
    assert 0.9 <= clock.monotonic() - t0 <= 1.6


async def test_throttle_pause_doubles_and_resets():
    clock = FakeClock()
    b = Budget(BudgetTunables(throttle_pause_sec=10, throttle_pause_max_sec=40, throttle_reset_sec=100), clock)
    assert b.throttle() == 10
    assert b.throttle() == 20
    assert b.throttle() == 40
    assert b.throttle() == 40
    clock.advance(101)
    assert b.throttle() == 10
    assert b.paused_for() == pytest.approx(10)
    assert not await b.acquire("P0", 5.0)


async def test_cache_hit_and_max_age():
    gw, tr, clock = make()
    r1 = await gw.call(E.RANK_WALLETS, path={"period": "7d"}, params={"tag": "kol"})
    r2 = await gw.call(E.RANK_WALLETS, path={"period": "7d"}, params={"tag": "kol"})
    assert not r1.cached and r2.cached and len(tr.calls) == 1
    clock.advance(10)
    r3 = await gw.call(E.RANK_WALLETS, path={"period": "7d"}, params={"tag": "kol"}, max_age_sec=5)
    assert not r3.cached and len(tr.calls) == 2
    await gw.call(E.RANK_WALLETS, path={"period": "30d"}, params={"tag": "kol"})
    assert len(tr.calls) == 3  # other params, other key


async def test_wallet_activity_never_cached():
    gw, tr, _ = make()
    await gw.call(E.WALLET_ACTIVITY, params={"wallet": "W"})
    await gw.call(E.WALLET_ACTIVITY, params={"wallet": "W"})
    assert len(tr.calls) == 2


async def test_coalescing_identical_inflight():
    gw, tr, _ = make()
    rs = await asyncio.gather(*(gw.call(E.TOKEN_STAT, path={"mint": "M"}) for _ in range(5)))
    assert len(tr.calls) == 1 and all(r.body == rs[0].body for r in rs)


async def test_throttle_serves_stale_then_unavailable():
    gw, tr, clock = make({"rank_wallets": [RawAnswer(200, OK_BODY), RawAnswer(429, None)]})
    await gw.call(E.RANK_WALLETS, path={"period": "7d"})
    clock.advance(1000)
    r = await gw.call(E.RANK_WALLETS, path={"period": "7d"})
    assert r.stale and r.cached
    assert gw.budget.throttles == 1 and gw.budget.paused_for() > 0
    with pytest.raises(Unavailable) as e:
        await gw.call(E.TOKEN_STAT, path={"mint": "M"}, priority="P0")
    assert e.value.reason == "budget"


async def test_bad_param_not_found_needs_login():
    gw, _, _ = make(
        {
            "token_stat": [RawAnswer(200, {"code": 40000300, "msg": "invalid argument"})],
            "token_security": [RawAnswer(404, None, text_head="404 page not found")],
            "token_dev_info": [RawAnswer(401, {"code": 40101611, "msg": "empty token"})],
        }
    )
    with pytest.raises(BadRequest):
        await gw.call(E.TOKEN_STAT, path={"mint": "M"})
    with pytest.raises(NotFound):
        await gw.call(E.TOKEN_SECURITY, path={"mint": "M"})
    with pytest.raises(UpstreamError):
        await gw.call(E.TOKEN_DEV_INFO, path={"mint": "M"})
    assert gw.budget.throttles == 0


async def test_server_error_retried_once():
    gw, tr, _ = make({"token_stat": [RawAnswer(200, {"code": 50001300}), RawAnswer(200, {"code": 0, "data": {}})]})
    r = await gw.call(E.TOKEN_STAT, path={"mint": "M"})
    assert r.body["code"] == 0 and len(tr.calls) == 2
    gw2, tr2, _ = make({"token_stat": [RawAnswer(0, None, error="Timeout")]})
    with pytest.raises(Unavailable):
        await gw2.call(E.TOKEN_STAT, path={"mint": "M"})
    assert len(tr2.calls) == 2
    assert gw2.counts[("gzetryn", "token_stat", "network")] == 2
