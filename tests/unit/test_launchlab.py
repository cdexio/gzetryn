"""Raydium LaunchLab list → candidates (fixture = live answers 2026-10-05)."""

from __future__ import annotations

import pytest

from gzetryn.clock import FakeClock
from gzetryn.config import BudgetTunables, LaunchLabTunables
from gzetryn.gateway.budget import Budget
from gzetryn.jobs.launchlab import GROUP, LaunchLab, parse_rows
from tests.conftest import load_fixture


def test_parse_new():
    rows = parse_rows(load_fixture("launchlab.json")["new"], "new")
    assert len(rows) == 4 and all(r.kind == "new" and r.source == "launchlab" for r in rows)
    r = rows[0]
    raw = load_fixture("launchlab.json")["new"]["data"]["rows"][0]
    assert r.mint == raw["mint"] and r.pool_address == raw["poolId"] and r.exchange == "ray_launchpad"
    assert r.launchpad == "launchlab" and r.launchpad_platform == raw["platformInfo"]["name"]
    assert r.quote_address == raw["mintB"]["address"] and r.creator == raw["creator"]
    assert r.created_at.timestamp() == pytest.approx(raw["createAt"] / 1000)
    assert r.metrics["mcap_usd"] == pytest.approx(raw["marketCap"])
    assert r.metrics["price_usd"] == pytest.approx(raw["marketCap"] / raw["supply"])
    assert r.metrics["progress"] == pytest.approx(raw["finishingRate"] / 100)
    assert r.metrics["volume_total_usd"] == pytest.approx(raw["volumeU"])
    assert r.metrics["holders"] is None


def test_parse_completing_threshold():
    body = load_fixture("launchlab.json")["lastTrade"]
    rows = parse_rows(body, "completing", 25.0)
    rates = [round(r.metrics["progress"] * 100, 2) for r in rows]
    assert rates and all(25 <= x < 100 for x in rates)
    assert len(rows) == sum(1 for x in body["data"]["rows"] if 25 <= x["finishingRate"] < 100)
    body["data"]["rows"][0]["finishingRate"] = 100  # migrated: never `completing`
    assert len(parse_rows(body, "completing", 25.0)) == len(rows) - 1


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def get(self, url, params=None, headers=None):
        self.calls.append(params["sort"])
        return self.answers.pop(0)

    async def close(self):
        pass


class FakeStore:
    def __init__(self):
        self.rows = []

    async def upsert(self, rows, at):
        self.rows.extend(rows)
        return len(rows)


async def test_cycle_and_cooldown():
    clock = FakeClock()
    budget = Budget(BudgetTunables(), clock)
    store = FakeStore()
    ll = LaunchLab(LaunchLabTunables(), budget, store, clock)
    fx = load_fixture("launchlab.json")
    ll._session = FakeSession([FakeResp(200, fx["new"]), FakeResp(429, None), FakeResp(200, fx["lastTrade"])])
    assert await ll.cycle("new") == 4
    assert await ll.cycle("completing") == 0 and ll.stats["completing"]["failed"] == 1
    assert budget.cooling() == {GROUP: 15.0} and budget.is_open("api")  # only the launchlab line cools
    n = await ll.cycle("completing")  # waits out the cooldown (P3, 120 s limit), probe succeeds
    assert n >= 1 and budget.is_open(GROUP)
    assert {r.kind for r in store.rows} == {"new", "completing"}
