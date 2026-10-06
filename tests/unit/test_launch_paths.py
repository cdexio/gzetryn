"""Phase 15: curve path recorder — checkpoints, dev and bundle buys, peak / low, window, late graduation."""

from __future__ import annotations

from gzetryn.clock import FakeClock
from gzetryn.config import LaunchPathsTunables
from gzetryn.jobs.launch_paths import LaunchPaths
from gzetryn.trigger import solana as S

T0 = 1_791_200_000
MINT = "MintAAAA"
DEV = "DevWallet"


class FakeStore:
    def __init__(self):
        self.rows: list[dict] = []
        self.completed: list[tuple] = []

    async def insert(self, rows):
        self.rows.extend(rows)
        return len(rows)

    async def mark_completed(self, mint, at, pool):
        self.completed.append((mint, at, pool))
        return 1


def created(mint=MINT, ts=T0):
    return S.Created(mint, "Name", "SYM", DEV, ts, 1_000_000_000_000_000, False)


def trade(sec, buy=True, user="u1", sol=1.0, vsol=30.0, vtok=1_000_000_000.0, rtok=793_100_000.0, mint=MINT):
    """vsol in SOL, vtok / rtok in whole tokens (price = vsol / vtok SOL per token)."""
    return S.Trade(
        mint=mint, sol_amount=int(sol * 1e9), token_amount=1, is_buy=buy, timestamp=T0 + sec,
        virtual_sol_reserves=int(vsol * 1e9), virtual_token_reserves=int(vtok * 1e6),
        real_sol_reserves=0, real_token_reserves=int(rtok * 1e6), ix_name="buy", mayhem_mode=False,
        quote_mint=None, creator=DEV, user=user,
    )


def recorder(**kw):
    t = LaunchPathsTunables(window_sec=600, checkpoints_sec=[10, 60, 300, 600], **kw)
    store = FakeStore()
    return LaunchPaths(t, store, FakeClock()), store


async def test_path_aggregates_and_finalizes_after_the_window():
    lp, store = recorder()
    lp.on_create(created(), slot=100)
    lp.on_trade(trade(0, user=DEV, sol=0.5), slot=100)  # dev buy in the create tx
    lp.on_trade(trade(0, user="b1", sol=2.0, vsol=32.0), slot=100)  # bundle (same slot)
    lp.on_trade(trade(1, user="b2", sol=1.0, vsol=33.0), slot=101)  # bundle (next slot, bundle_slots=1)
    lp.on_trade(trade(30, user="b3", sol=1.0, vsol=40.0), slot=180)  # peak 4e-8
    lp.on_trade(trade(90, buy=False, user=DEV, sol=0.75, vsol=36.0), slot=300)  # dev sells at 90 s
    lp.on_trade(trade(120, buy=False, user="b1", sol=1.5, vsol=34.0), slot=400)  # low after peak
    lp.on_trade(trade(700, user="late"), slot=900)  # past the window: ignored
    assert lp.sweep(now=T0 + 599) == 0
    assert lp.sweep(now=T0 + 600) == 1
    assert await lp.flush() == 1
    r = store.rows[0]
    assert r["trades"] == 6 and r["buys"] == 4 and r["sells"] == 2
    assert r["buyers"] == 3 and r["sellers"] == 1  # dev counted apart
    assert r["dev_buy_sol"] == 0.5 and r["dev_sell_sol"] == 0.75 and r["dev_first_sell_sec"] == 90
    assert r["bundle_buyers"] == 2 and r["bundle_sol"] == 3.0
    assert r["peak_price_sol"] == 40.0 / 1e9 and r["peak_sec"] == 30
    assert r["low_after_peak_sol"] == 34.0 / 1e9 and r["last_price_sol"] == 34.0 / 1e9
    # 10 s: last price before 10 s = 33e-9; 60 s: 40e-9; 300 / 600 s: the last price (34e-9)
    assert r["checkpoints"] == {"10": 33.0 / 1e9, "60": 40.0 / 1e9, "300": 34.0 / 1e9, "600": 34.0 / 1e9}
    assert [b["w"] for b in r["first_buyers"]] == ["b1", "b2", "b3"]
    assert r["completed_at"] is None and r["creator"] == DEV and r["symbol"] == "SYM"


async def test_graduation_inside_and_after_the_window():
    lp, store = recorder()
    lp.on_create(created(), slot=1)
    lp.on_trade(trade(5, rtok=10_000_000.0), slot=2)
    lp.on_complete(MINT, T0 + 50)
    lp.on_complete(MINT, T0 + 51, pool="PoolX")
    lp.sweep(now=T0 + 600)
    await lp.flush()
    r = store.rows[0]
    assert r["completed_at"].timestamp() == T0 + 50 and r["migrated_pool"] == "PoolX" and r["max_progress"] == 1.0
    # a second launch graduates after its window: the stored row is updated
    lp.on_create(created("MintB", T0 + 10), slot=3)
    lp.sweep(now=T0 + 610)
    await lp.flush()
    lp.on_complete("MintB", T0 + 900, pool="PoolB")
    await lp.flush()
    assert store.completed[0][0] == "MintB" and store.completed[0][2] == "PoolB"
    lp.on_complete("never-seen", T0 + 900)  # unknown mint: ignored
    await lp.flush()
    assert len(store.completed) == 1


async def test_in_flight_cap_finalizes_the_oldest_early():
    lp, store = recorder(max_in_flight=2)
    for i in range(3):
        lp.on_create(created(f"M{i}", T0 + i), slot=i)
    assert lp.stats["early_finalized"] == 1 and lp.summary()["in_flight"] == 2
    await lp.flush()  # the flush also sweeps by wall clock, which is past these test windows
    assert store.rows[0]["mint"] == "M0"
