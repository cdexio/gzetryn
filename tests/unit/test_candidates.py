"""Candidate normalization on the phase 0 / phase 8 fixtures (real GMGN rows)."""

from __future__ import annotations

from gzetryn.gmgn import parse
from tests.conftest import load_fixture


async def test_pump_lists_skipped_while_chain_source_healthy():
    from gzetryn.clock import FakeClock
    from gzetryn.config import CandidatesTunables, Tunables
    from gzetryn.gateway.budget import Budget
    from gzetryn.gateway.gateway import Gateway
    from gzetryn.jobs.candidates import Candidates
    from tests.conftest import FakeTransport

    class Store:
        rows = []

        async def upsert(self, rows, at):
            self.rows.extend(rows)
            return len(rows)

    clock, t, tr = FakeClock(), Tunables(), FakeTransport()
    c = Candidates(CandidatesTunables(), Gateway(t, tr, Budget(t.budget, clock), clock=clock), Store(), clock)
    c.pump_chain_healthy = lambda: True
    assert await c.pump() is None and tr.calls == [] and c.stats["pump"]["skipped_chain_healthy"] == 1
    c.pump_chain_healthy = lambda: False  # chain source idle → GMGN lists again
    assert await c.pump() > 0 and [x[0] for x in tr.calls] == ["pump_lists"]


def test_pump_lists_kinds_and_pools():
    rows = parse.candidates_pump(load_fixture("trenches.json"))
    assert [r.kind for r in rows] == ["new", "new", "completing", "completing", "migrated", "migrated"]
    mig = rows[4]
    assert mig.mint == "FCBFJoC3J1XfnaxbcroETYFnn1rERw8rRH3Fzsawpump"
    assert mig.pool_address == "F9ZT71YCCaRrEsqJUA6Qek91iiR8VYHWrEFxsarkKZXk" and mig.exchange == "pump_amm"
    assert mig.complete_at is not None and mig.open_at == mig.complete_at
    assert mig.creator == "DJQwtiowTJmAvAu4vhCaFgPiS3nnKD5r5agZgBQEdHK2"
    m = mig.metrics
    assert m["smart_degen_count"] == 17 and m["renowned_count"] == 2 and m["holders"] == 356
    assert round(m["mcap_usd"]) == 66000 and round(m["liquidity_usd"]) == 19495
    assert set(m) == set(parse.CANDIDATE_METRICS)
    new = rows[0]
    assert new.complete_at is None and new.open_at is None and new.pool_address  # bonding curve


def test_new_pairs_pool_is_row_address():
    rows = parse.candidates_new_pairs(load_fixture("new-pairs.json"))
    assert rows and all(r.source == "new_pairs" and r.created_at is None for r in rows)
    body = load_fixture("new-pairs.json")
    p0 = body["data"]["pairs"][0]
    for exchange, launchpad, platform, kind in (
        ("pump_amm", "pump", "Pump.fun", "migrated"),  # pump.fun graduation
        ("pump_amm", None, "pool_pump_amm", "new"),  # direct PumpSwap launch
        ("pump_amm", "pump", "pump_mayhem", "new"),
        ("pump", "pump", "Pump.fun", "new"),  # still on the bonding curve
    ):
        p0.update(exchange=exchange, launchpad=launchpad, launchpad_platform=platform)
        assert parse.candidates_new_pairs(body)[0].kind == kind
    raw = load_fixture("new-pairs.json")["data"]["pairs"][0]
    assert rows[0].pool_address == raw["address"] and rows[0].mint == raw["base_address"]
    assert rows[0].metrics["buys_1h"] is None  # not in these rows


def test_trending_has_no_pool_until_resolved():
    rows = parse.candidates_trending(load_fixture("rank-swaps-1h.json"))
    assert rows and all(r.kind == "trending" and r.pool_address is None for r in rows)
    assert rows[0].metrics["swaps_1h"] is not None and rows[0].metrics["smart_degen_count"] is not None
    pools = parse.pools_from_window_info(load_fixture("token-window-info.json"))
    assert pools["8HQgEcbhR5Xoh735zvndAocCLd7wdAYJfWHV55J8pump"]["pool_address"] == (
        "95WhUbKGxbWEjj9UgZtcEAMXoBmTSmHRAFiAxPLFf1FJ"
    )
