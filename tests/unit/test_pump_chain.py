"""pump.fun chain reader: decoders on captured live logs (pump IDL), PDA, row logic."""

from __future__ import annotations

import pytest

from gzetryn.clock import FakeClock
from gzetryn.config import PumpChainTunables
from gzetryn.jobs.pump_chain import PumpChain
from gzetryn.trigger import solana as S
from tests.conftest import load_fixture


def events(name: str, kind: str) -> list[bytes]:
    return [d for k, d in S.events_from_logs(load_fixture("pump-logs.json")[name]["logs"]) if k == kind]


def test_base58_roundtrip_and_pda():
    m = "9Ycs3vt3LFCDFGLrM8XiKQupbmGT7Bsf2tVh5o8rpump"
    assert S.b58encode(S.b58decode(m)) == m
    # GMGN pump list row (trenches fixture): new_creation mint → its bonding curve (`pool_address`)
    assert S.bonding_curve_address(m) == "3FihULRghwfxhrAQRBUT5g4s9EmB3XZikMhtv6dryDDA"
    assert S.on_curve(S.b58decode(S.PUMP_PROGRAM)) is True  # program ids are ordinary keys


def test_trade_decoding_standard_curve():
    d = events("trade_completing", "trade")[0]
    t = S.decode_trade(d)
    assert t.standard_curve and t.sol_quote and t.mayhem_mode is False
    assert t.virtual_token_reserves - t.real_token_reserves == S.VIRTUAL_MINUS_REAL_TOKENS
    assert S.progress(t.real_token_reserves) >= 0.55
    assert t.price_sol and t.price_sol > 0 and t.creator and len(t.creator) >= 32
    assert t.ix_name in ("buy", "sell", "buy_exact_sol_in", "buy_exact_quote_in", "sell_exact_quote_out")


def test_trade_decoding_mayhem_excluded():
    t = S.decode_trade(events("trade_mayhem", "trade")[0])
    assert t.mayhem_mode is True and t.standard_curve is False


def test_create_complete_migration():
    c = S.decode_create(events("create", "create")[0])
    assert c.name and c.symbol and c.mint and c.timestamp > 1_700_000_000
    assert c.token_total_supply in (None, S.STANDARD_TOTAL_SUPPLY) or c.mayhem_mode
    mint, curve, ts = S.decode_complete(events("complete", "complete")[0])
    assert S.bonding_curve_address(mint) == curve and ts > 1_700_000_000
    m = S.decode_migration(events("migration", "migration")[0])
    assert m.pool and m.mint and S.bonding_curve_address(m.mint) == m.bonding_curve


class FakeStore:
    def __init__(self):
        self.rows = []

    async def upsert(self, rows, at):
        self.rows.extend(rows)
        return len(rows)


async def sol_usd():
    return 150.0


async def test_reader_rows():
    clock = FakeClock()
    store = FakeStore()
    pc = PumpChain(PumpChainTunables(), store, sol_usd, clock)
    fx = load_fixture("pump-logs.json")
    pc.on_logs(fx["trade_mayhem"]["signature"], 1, fx["trade_mayhem"]["logs"])
    pc.on_logs(fx["create"]["signature"], 1, fx["create"]["logs"])
    pc.on_logs(fx["trade_completing"]["signature"], 1, fx["trade_completing"]["logs"])
    assert pc.stats["excluded_mayhem"] >= 1 and pc.stats["completing_new"] >= 1
    pc.on_logs(fx["trade_completing"]["signature"], 1, fx["trade_completing"]["logs"])  # within update_sec: merged
    assert await pc.flush() >= 1
    comp = [r for r in store.rows if r.kind == "completing"]
    assert len(comp) >= 1
    r = comp[0]
    t = S.decode_trade(events("trade_completing", "trade")[0])
    assert r.source == "chain" and r.mint == t.mint and r.pool_address == S.bonding_curve_address(t.mint)
    assert r.exchange == "pump" and r.launchpad_platform == "Pump.fun"
    assert r.metrics["progress"] == pytest.approx(S.progress(t.real_token_reserves), abs=1e-6)
    assert r.metrics["price_usd"] == pytest.approx(t.price_sol * 150.0)
    assert r.metrics["mcap_usd"] == pytest.approx(t.price_sol * 150.0 * 1e9)
    assert r.metrics["liquidity_usd"] == pytest.approx(t.real_sol_reserves / 1e9 * 150.0)
    assert not any(k.startswith("_") for k in r.metrics)
    # migration → migrated row with the event's pool; the mint is no longer tracked as completing
    pc.on_logs(fx["migration"]["signature"], 1, fx["migration"]["logs"])
    await pc.flush()
    mig = [r for r in store.rows if r.kind == "migrated"]
    m = S.decode_migration(events("migration", "migration")[0])
    assert mig and mig[0].pool_address == m.pool and mig[0].exchange == "pump_amm" and mig[0].complete_at
    assert pc.summary()["fresh_new_sec_p50"] is not None


async def test_update_throttle():
    clock = FakeClock()
    store = FakeStore()
    pc = PumpChain(PumpChainTunables(update_sec=30), store, sol_usd, clock)
    fx = load_fixture("pump-logs.json")["trade_completing"]
    pc.on_logs(fx["signature"], 1, fx["logs"])
    await pc.flush()
    pc.on_logs(fx["signature"], 1, fx["logs"])
    assert await pc.flush() == 0  # 0 s later: no update
    clock.advance(31)
    pc.on_logs(fx["signature"], 1, fx["logs"])
    assert await pc.flush() == 1 and pc.stats["completing_updates"] >= 1
