"""DEXTools snipers part: parser on a live answer, client behaviour (cache, 400, cooldown)."""

from __future__ import annotations

import pytest

from gzetryn.clock import FakeClock
from gzetryn.config import BudgetTunables, DexToolsTunables
from gzetryn.gateway.budget import Budget
from gzetryn.jobs.dextools import GROUP, DexTools, NoPair, parse_pair
from tests.conftest import load_fixture


def test_parse_live_answer():
    fx = load_fixture("dextools.json")
    out = parse_pair(fx["ok"]["body"], fx["pool"])
    tok = fx["ok"]["body"]["data"][0]["token"]
    snipers = tok["firstMakers"]["snipers"]
    assert out["source"] == "dextools" and out["available"] and out["measure_only"]
    assert out["count"] == len(snipers) and out["wallets"] == snipers
    assert out["creator"] == tok["deployment"]["owner"]
    assert out["creator_is_sniper"] == (tok["deployment"]["owner"] in snipers)
    assert out["count_excl_creator"] == len([w for w in snipers if w != tok["deployment"]["owner"]])
    assert out["migrated_from"]["pair"] == fx["mint"]  # pump.fun origin: DEXTools stores the mint as the old pair
    assert parse_pair({"data": []}, "x") is None


class Resp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class Sess:
    def __init__(self, answers):
        self.answers, self.calls = answers, 0

    async def get(self, url, params=None, headers=None):
        self.calls += 1
        return self.answers.pop(0)

    async def close(self):
        pass


async def test_client_cache_notfound_cooldown():
    clock = FakeClock()
    budget = Budget(BudgetTunables(), clock)
    fx = load_fixture("dextools.json")
    dt = DexTools(DexToolsTunables(), budget, clock)
    dt._session = Sess([Resp(200, fx["ok"]["body"]), Resp(400, fx["not_found"]["body"]), Resp(429, None)])
    a = await dt.snipers(fx["pool"])
    b = await dt.snipers(fx["pool"])  # cached
    assert a == b and dt._session.calls == 1 and dt.stats["cache_hits"] == 1
    with pytest.raises(NoPair):
        await dt.snipers("CurvePool111")
    with pytest.raises(RuntimeError):
        await dt.snipers("OtherPool111")
    assert budget.cooling() == {GROUP: 15.0} and budget.is_open("api") and dt.stats["throttled"] == 1
