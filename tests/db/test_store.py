"""Directory, curation and feed against Postgres (schema gzetryn_test)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gzetryn.config import CurationTunables
from gzetryn.core.curation import curate
from gzetryn.gmgn import parse
from gzetryn.gmgn.parse import TradeRow, WalletRow
from gzetryn.store.feed import FeedStore, WalletContext
from gzetryn.store.wallets import WalletStore
from tests.conftest import load_fixture

pytestmark = pytest.mark.db

T0 = datetime(2026, 10, 3, 6, tzinfo=UTC)
A = "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
B = "DNfuF1L62WWyW3pNakVkyGGFzVVhj4Yr52jSmdTyeBHm"
C = "9iaawVBEsFG35PSwd4PahwT8fYNQe9XYuRdWm872dUqY"


def row(addr, rank, profit, wr=0.6, buys=100, sells=100, tags=("kol",)):
    return WalletRow(
        address=addr,
        rank=rank,
        name=f"n-{addr[:4]}",
        tags=list(tags),
        realized_profit_7d=profit / 4,
        realized_profit_30d=profit,
        pnl_7d=0.1,
        pnl_30d=0.2,
        winrate_7d=wr,
        winrate_30d=wr,
        buy_30d=buys,
        sell_30d=sells,
        daily_profit_7d=[{"date": "2026-10-01", "profit": 5.0}],
    )


async def test_rank_snapshot_and_wallet_upsert(sessions):
    ws = WalletStore(sessions)
    pg = parse.rank_wallets(load_fixture("rank-wallets-kol-7d.json"))
    stats = await ws.store_rank(T0, {("kol", "7d"): pg.items}, complete=True)
    assert stats == {"snapshots": 6, "wallets": 6, "left_ranks": 0}
    w = await ws.get(pg.items[0].address)
    assert w["status"] == "ranked" and w["rank_lists"] == ["kol:7d"]
    assert w["metrics"]["source"] == "rank" and w["metrics"]["realized_profit_30d"] > 0
    # a later complete refresh without that wallet clears its rank membership, keeps the wallet
    await ws.store_rank(T0 + timedelta(hours=1), {("kol", "7d"): pg.items[1:]}, complete=True)
    w = await ws.get(pg.items[0].address)
    assert w["status"] == "inactive" and w["metrics"]["realized_profit_30d"] > 0
    hist = await ws.history(pg.items[1].address, None, T0 - timedelta(days=1), 100)
    assert len(hist) == 2


async def test_curation_apply_deactivate_manual_untouched(sessions):
    ws = WalletStore(sessions)
    t = CurationTunables(top_n=2, min_candidates=1)
    await ws.store_rank(T0, {("kol", "30d"): [row(A, 1, 900), row(B, 2, 500), row(C, 3, 100, wr=0.2)]}, True)
    await ws.add_manual(C, "friend", ["insider"], None, "test", T0)
    cands, oldest = await ws.candidates(["kol", "smart_degen"], ["7d", "30d"])
    assert {c.address for c in cands} == {A, B, C} and oldest == T0
    res = curate(cands, t)
    assert res.addresses == [A, B]
    out = await ws.apply_curation(res, T0, t.model_dump())
    assert out == {"selected": 2, "added": 2, "removed": 0}
    a = await ws.get(A)
    assert a["curated"] and a["curated_rank"] == 1 and a["watch"]["started_at"] == T0.isoformat()
    # next day B drops out (win rate falls); C stays manual even though it never passes
    t1 = T0 + timedelta(days=1)
    await ws.store_rank(t1, {("kol", "30d"): [row(A, 1, 950), row(B, 2, 600, wr=0.1), row(C, 3, 100, wr=0.2)]}, True)
    cands, _ = await ws.candidates(["kol"], ["30d"])
    res = curate(cands, t)
    out = await ws.apply_curation(res, t1, t.model_dump())
    assert out == {"selected": 1, "added": 0, "removed": 1}
    b = await ws.get(B)
    assert not b["curated"] and b["uncurated_at"] == t1.isoformat() and b["status"] == "ranked"
    c = await ws.get(C)
    assert c["manual"] and c["label"] == "friend" and c["user_tags"] == ["insider"]
    active = {w.address for w in await ws.active()}
    assert active == {A, C}
    runs = await ws.curation_runs(5)
    assert [r["status"] for r in runs] == ["applied", "applied"] and runs[0]["removed"] == [B]


async def test_manual_lifecycle(sessions):
    ws = WalletStore(sessions)
    w, created = await ws.add_manual(A, "kol x", ["watch"], "note", "zetryn", T0)
    assert created and w["status"] == "manual" and w["added_by"] == "zetryn"
    w, created = await ws.add_manual(A, None, None, None, "other", T0 + timedelta(hours=1))
    assert not created and w["label"] == "kol x" and w["added_by"] == "zetryn"
    assert w["watch"]["started_at"] == T0.isoformat()
    w = await ws.update_manual(A, "renamed", ["a", "b"], None)
    assert w["label"] == "renamed" and w["user_tags"] == ["a", "b"]
    w = await ws.remove_manual(A)
    assert w["status"] == "inactive"
    assert await ws.remove_manual(A) is None
    assert await ws.update_manual(B, "x", None, None) is None
    assert await ws.manual_needing_metrics(T0) == []


def trade(tx, at, side="buy", mint="EHfj12MDoETURYUFxm9CNr1E8zhpCZqZy9u4njbQpump", amount=10.0):
    return TradeRow(
        wallet=A,
        tx_hash=tx,
        trade_at=at,
        side=side,
        mint=mint,
        symbol="nvk",
        token_amount=amount,
        sol_amount=1.0,
        quote_symbol="SOL",
        usd_amount=120.0,
        price_usd=0.0001,
        price_sol=1e-6,
        total_supply=1e9,
        mcap_usd=1e5,
        open_or_close=True,
        launchpad="pump",
        launchpad_platform="Pump.fun",
        payload={},
    )


def ctx(started):
    return WalletContext(A, "decu", "notdecu", ["kol"], "lbl", ["insider"], "curated", started)


async def test_feed_idempotent_ordered_baseline_and_cursor(sessions):
    feed = FeedStore(sessions)
    started = T0
    old = trade("tx-old", T0 - timedelta(minutes=5))
    t1 = trade("tx1", T0 + timedelta(seconds=10))
    t2 = trade("tx2", T0 + timedelta(seconds=20), side="sell")
    new = await feed.insert(ctx(started), [t2, old, t1], seen_at=T0 + timedelta(seconds=30))
    assert {x.tx_hash for x in new} == {"tx-old", "tx1", "tx2"}
    again = await feed.insert(ctx(started), [t1, t2], seen_at=T0 + timedelta(seconds=90))
    assert again == []
    assert await feed.last_seq() == 3  # known rows are filtered before the insert: no sequence values burned
    rows, cur = await feed.read(0)
    assert [r["tx_hash"] for r in rows] == ["tx1", "tx2"]  # baseline hidden by default, seq follows trade time
    assert rows[0]["lag_sec"] == pytest.approx(20.0) and rows[0]["label"] == "lbl"
    assert rows[0]["mcap_usd"] == 1e5 and rows[0]["liquidity_usd"] is None
    all_rows, _ = await feed.read(0, include_baseline=True)
    assert [r["tx_hash"] for r in all_rows] == ["tx-old", "tx1", "tx2"] and all_rows[0]["baseline"]
    assert cur == all_rows[-1]["seq"]
    rows, cur2 = await feed.read(cur)
    assert rows == [] and cur2 == cur
    sells, cur3 = await feed.read(0, side="sell")
    assert [r["tx_hash"] for r in sells] == ["tx2"] and cur3 == cur  # cursor moves past filtered rows
    assert (await feed.read(0, tag="insider"))[0] and (await feed.read(0, tag="nope"))[0] == []
    one, c1 = await feed.read(0, limit=1, include_baseline=True)
    assert len(one) == 1 and c1 == one[0]["seq"]
    assert len(await feed.for_mint(t1.mint, 10)) == 3
    st = await feed.stats(T0 - timedelta(days=1))
    assert st["events"] == 3 and st["live_events"] == 2
