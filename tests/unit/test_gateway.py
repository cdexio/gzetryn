"""Budget and gateway on a fake clock and a fake transport."""

from __future__ import annotations

import asyncio

import pytest

from gzetryn.clock import FakeClock
from gzetryn.config import BudgetTunables, GroupTunables, Tunables
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


def group_budget(clock, **kw):
    g = {"vas": GroupTunables(per_minute=60, burst=4, min_gap_sec=0.5), "api": GroupTunables(per_minute=60, burst=4)}
    return Budget(BudgetTunables(per_minute=600, burst=100, min_gap_sec=0, groups=g, **kw), clock)


def test_endpoint_groups():
    assert E.WALLET_ACTIVITY.group == "vas" and E.TOKEN_HOLDER_STAT.group == "vas" and E.PUMP_LISTS.group == "vas"
    assert E.TOKEN_STAT.group == "api" and E.NEW_PAIRS.group == "api" and E.TOKEN_WINDOW_INFO.group == "api"
    assert E.RANK_WALLETS.group == "defi" and E.RANK_SWAPS.group == "defi" and E.TOKEN_MULTI_INFO.group == "mrwapi"


async def test_group_reserves_and_min_gap():
    clock = FakeClock()
    b = group_budget(clock)  # reserves P2 1, P3 2
    assert await b.acquire("P3", 0.0, group="vas")  # 4 → 3
    assert not await b.acquire("P3", 0.0, group="vas")  # min gap 0.5 s not over
    clock.advance(0.5)
    assert await b.acquire("P3", 0.0, group="vas")  # 3.5 → 2.5
    clock.advance(0.5)
    assert await b.acquire("P3", 0.0, group="vas")  # 3.0 ≥ 1 + 2 → 2.0
    clock.advance(0.5)
    assert not await b.acquire("P3", 0.0, group="vas")  # 2.5 < 3: P3 leaves 2 tokens
    assert await b.acquire("P2", 0.0, group="vas")  # needs 2 → 1.5
    clock.advance(0.5)
    assert await b.acquire("P0", 0.0, group="vas")  # P0 may take the last tokens
    assert await b.acquire("P1", 0.0, group="api")  # other group, own bucket and gap
    assert b.granted == {"P0": 1, "P1": 1, "P2": 1, "P3": 3}


async def test_throttle_cools_one_group_only_then_probe():
    clock = FakeClock()
    b = group_budget(clock, cooldown_start_sec=15, cooldown_max_sec=60, cooldown_decay_sec=100)
    assert b.throttle("vas") == 15
    assert b.cooling() == {"vas": 15.0} and b.paused_for() == 0
    assert await b.acquire("P1", 0.0, group="api")  # api unaffected
    assert not await b.acquire("P0", 5.0, group="vas")  # 15 s cooldown > 5 s wait
    clock.advance(15)
    assert await b.acquire("P0", 0.0, group="vas")  # the probe
    assert not await b.acquire("P0", 0.0, group="vas")  # others wait for the probe's answer
    b.ok("vas")
    clock.advance(1)
    assert await b.acquire("P0", 0.0, group="vas")  # reopened
    # no reset on success: a re-challenge soon after the reopen continues the ladder
    assert b.report()["vas"]["cooldown_level"] == 1
    assert [b.throttle("vas") for _ in range(4)] == [30, 60, 60, 60]
    assert b.report()["vas"]["cooldown_level"] == 5
    b.ok("vas")  # (pretend the probe after the last cooldown succeeded)
    # step down one level per clean cooldown_decay_sec, counted from the last throttle
    clock.advance(60 + 250)  # cooldown over + 2.5 decay periods after it
    assert b.report()["vas"]["cooldown_level"] == 2  # 5 - floor(310 / 100)
    assert b.throttle("vas") == 30  # two levels down from 60 → 15, then doubled
    clock.advance(10_000)
    b.ok("vas")
    assert b.report()["vas"]["cooldown_level"] == 0 and b.throttle("vas") == 15
    assert b.report()["vas"]["throttles"] == 7


async def test_half_rate_after_reopen():
    clock = FakeClock()
    b = group_budget(clock, reopen_slow_sec=600, reopen_rate_factor=0.5)  # vas: 60/min, gap 0.5 s
    b.throttle("vas")
    clock.advance(15)
    assert await b.acquire("P0", 0.0, group="vas")  # probe
    b.ok("vas")
    assert b.report()["vas"]["slow_after_reopen_sec"] == pytest.approx(600)
    clock.advance(0.6)
    assert not await b.acquire("P0", 0.0, group="vas")  # gap stretched to 1.0 s
    clock.advance(0.5)
    assert await b.acquire("P0", 0.0, group="vas")
    clock.advance(600)
    assert b.report()["vas"]["slow_after_reopen_sec"] == 0
    assert await b.acquire("P0", 0.0, group="vas")
    clock.advance(0.5)
    assert await b.acquire("P0", 0.0, group="vas")  # normal gap again


async def test_cooldown_ladder_defaults_reach_one_hour():
    clock = FakeClock()
    b = Budget(BudgetTunables(), clock)
    steps = [b.throttle("vas") for _ in range(10)]
    assert steps == [15, 30, 60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert b.paused_for() == 0  # one group cooling never pauses the others
    assert await b.acquire("P1", 0.0, group="api")


async def test_global_pause_only_when_two_groups_cool():
    clock = FakeClock()
    b = group_budget(clock, global_pause_groups=2, global_pause_sec=60)
    b.throttle("vas")
    assert b.paused_for() == 0 and b.global_pauses == 0
    b.throttle("api")
    assert b.paused_for() == pytest.approx(60) and b.global_pauses == 1
    assert not await b.acquire("P0", 5.0, group="defi")


async def test_strict_priority_in_group():
    clock = FakeClock()
    b = group_budget(clock)
    g = b._group("vas")
    g.waiting["P0"] += 1  # a trigger poll is waiting in vas
    assert not await b.acquire("P1", 0.0, group="vas")
    assert await b.acquire("P1", 0.0, group="api")  # other group is not blocked
    g.waiting["P0"] -= 1
    assert await b.acquire("P1", 0.0, group="vas")


async def test_vas_defaults_keep_one_survivors_intel_free():
    """D-2026-10-05-14: in vas, P2/P3 leave 4 tokens and P0 starts 0.25 s apart, so the 4 vas calls of one survivor
    start within 0.75 s even right after wallet polls drained what they may take."""
    clock = FakeClock()
    b = Budget(BudgetTunables(), clock)
    assert await b.acquire("P3", 0.0, group="vas")  # full bucket 5 → 4
    clock.advance(1.0)  # 4.2 tokens
    assert not await b.acquire("P3", 0.0, group="vas")  # P3 needs 1 + 4 reserve
    assert not await b.acquire("P2", 0.0, group="vas")  # pump lists too
    t0 = clock.monotonic()
    starts = []
    for _ in range(4):
        assert await b.acquire("P0", 1.0, group="vas")
        starts.append(clock.monotonic() - t0)
    assert starts[-1] <= 0.8
    # a cooling group answers P0 at once (3 s wait limit < cooldown): the caller gets "unavailable" fast
    b.throttle("vas")
    t1 = clock.monotonic()
    assert not await b.acquire("P0", group="vas")
    assert clock.monotonic() - t1 < 0.01


async def test_p0_global_gap_and_class_counts():
    gw, tr, clock = make()
    t0 = clock.monotonic()
    # one survivor's calls go to four groups: only the global gap separates them (0.05 s for P0, 0.25 s otherwise)
    await gw.call(E.TOKEN_SECURITY, path={"mint": "M"}, priority="P0")
    await gw.call(E.TOKEN_MULTI_INFO, body={"chain": "sol", "addresses": ["M"]}, priority="P0")
    await gw.call(E.TOKEN_HOLDER_STAT, path={"mint": "M"}, priority="P0")
    await gw.call(E.RANK_SWAPS, path={"interval": "1h"}, priority="P0")
    assert clock.monotonic() - t0 < 0.3
    await gw.call(E.WALLET_ACTIVITY, params={"wallet": "W"}, priority="P3")
    await gw.call(E.TOKEN_SECURITY, path={"mint": "M"}, priority="P0")  # cached
    rep = gw.class_report()
    assert rep["api"]["intel"] == {"sent": 1, "ok": 1, "throttled": 0, "denied": 0, "cache": 1, "sent_last_hour": 1}
    assert rep["vas"]["intel"]["sent"] == 1
    assert rep["vas"]["wallet_activity"]["sent"] == 1 and rep["vas"]["wallet_activity"]["sent_last_hour"] == 1


async def test_windows_recorded_at_throttle():
    clock = FakeClock()
    b = group_budget(clock)
    for _ in range(3):
        assert await b.acquire("P0", 5.0, group="vas")
    b.throttle("vas")
    r = b.report()["vas"]
    assert r["last_throttle_windows"] == {10: 3, 60: 3, 300: 3} and r["peak"][10] == 3


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


async def test_throttle_serves_stale_cools_group_only():
    gw, tr, clock = make({"rank_wallets": [RawAnswer(200, OK_BODY), RawAnswer(429, None)]})
    await gw.call(E.RANK_WALLETS, path={"period": "7d"})
    clock.advance(1000)
    r = await gw.call(E.RANK_WALLETS, path={"period": "7d"})
    t_throttle = clock.monotonic()
    assert r.stale and r.cached
    assert gw.budget.throttles == 1 and gw.budget.cooling() == {"defi": 15.0} and gw.budget.paused_for() == 0
    s = await gw.call(E.TOKEN_STAT, path={"mint": "M"}, priority="P1")  # api group still served
    assert not s.cached and clock.monotonic() - t_throttle < 1
    ok = await gw.call(E.RANK_SWAPS, path={"interval": "1h"}, priority="P3")  # waits out 15 s, then probes
    assert not ok.stale and clock.monotonic() - t_throttle >= 15 and gw.budget.report()["defi"]["probing"] is False
    gw.budget.throttle("defi")  # re-challenge right after the reopen: the ladder continues (30 s)
    gw.budget.throttle("defi")  # 60 s, longer than P0's 3 s wait limit
    with pytest.raises(Unavailable) as e:
        await gw.call(E.RANK_SWAPS, path={"interval": "5m"}, priority="P0")
    assert e.value.reason == "cooldown:defi" and e.value.retry_after_sec > 0


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
