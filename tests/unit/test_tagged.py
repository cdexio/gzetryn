"""Phase 14: watch-only tagged wallets — sticky connection shards, chain-only routing, tag_buyers counts."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from gzetryn.clock import FakeClock
from gzetryn.config import TaggedTunables, TriggerTunables, WatchTunables
from gzetryn.jobs.token import tag_buyers
from gzetryn.jobs.watcher import Watcher


class FakeTrig:
    def __init__(self):
        self.wallets: set[str] = set()

    def set_wallets(self, wallets: set[str]) -> None:
        self.wallets = set(wallets)


class FakeChain:
    def __init__(self, backlog: int = 0):
        self.backlog = backlog
        self.jobs: list[tuple[str, str]] = []

    def enqueue(self, wallet: str, signature: str, notified_at: str) -> None:
        self.jobs.append((wallet, signature))


def wallet(addr: str, tags=("kol",)):
    return SimpleNamespace(
        address=addr, name=None, twitter_username=None, gmgn_tags=list(tags), label=None, user_tags=[],
        curated=False, manual=False, rank_lists=["kol:30d"], watch_started_at=None, last_trade_at=None,
    )


def watcher(per_connection: int = 2, connections: int = 2):
    w = Watcher(
        WatchTunables(), None, None, None, FakeClock(), TriggerTunables(),  # type: ignore[arg-type]
        TaggedTunables(per_connection=per_connection, max_wallets=per_connection * connections),
    )
    w.tagged_triggers = [FakeTrig() for _ in range(connections)]
    return w


def test_shards_are_sticky_and_capped():
    w = watcher()
    w._set_tagged([wallet("a"), wallet("b"), wallet("c")])
    first = dict(w._shard)
    assert sorted(len(t.wallets) for t in w.tagged_triggers) == [1, 2]
    # b leaves, d, e and f arrive: a and c keep their connection; f does not fit (2 + 2 = room for 4)
    w._set_tagged([wallet("a"), wallet("c"), wallet("d"), wallet("e"), wallet("f")])
    assert w._shard["a"] == first["a"] and w._shard["c"] == first["c"]
    assert len(w._shard) == 4 and all(len(t.wallets) == 2 for t in w.tagged_triggers)
    assert set(w._tagged) == set(w._shard)


def test_tagged_swaps_go_to_the_chain_only_and_shed_when_half_full():
    w = watcher()
    w.chain = FakeChain()
    w._set_tagged([wallet("a")])
    w.on_tagged_trade("a", "sig1", 1)
    w.on_tagged_trade("unknown", "sig2", 1)
    assert w.chain.jobs == [("a", "sig1")] and w.stats.tagged_routed == 1
    assert w._due == {} and w._trig_due == {}  # never a GMGN poll
    w.chain.backlog = TriggerTunables().chain_queue_max // 2
    w.on_tagged_trade("a", "sig3", 1)
    assert w.stats.tagged_shed == 1 and len(w.chain.jobs) == 1
    ctx = w.context_of("a")
    assert ctx is not None and ctx.status == "tagged"


def test_tag_universe_counts_active_and_tagged():
    w = watcher()
    w._ctx["x"] = wallet("x", tags=("kol", "smart_degen"))  # type: ignore[assignment]
    w._set_tagged([wallet("a", tags=("smart_degen",)), wallet("b", tags=("renowned",))])
    assert w.tag_universe() == {"kol": 1, "smart_degen": 2, "renowned": 1}


def test_tag_buyers_counts_and_holding():
    at = datetime(2026, 10, 5, 20, tzinfo=UTC)
    rows = [
        {"wallet": "a", "status": "curated", "tags": ["kol"], "first_buy_at": at, "buys": 1, "buy_sol": 1.0,
         "sell_sol": 0.0, "buy_tokens": 10.0, "sell_tokens": 0.0},
        {"wallet": "b", "status": "tagged", "tags": ["kol", "smart_degen"], "first_buy_at": at, "buys": 2,
         "buy_sol": 2.0, "sell_sol": 2.5, "buy_tokens": 20.0, "sell_tokens": 20.0},
        {"wallet": "c", "status": "tagged", "tags": ["smart_degen"], "first_buy_at": None, "buys": 0,
         "buy_sol": 0.0, "sell_sol": 1.0, "buy_tokens": 0.0, "sell_tokens": 5.0},  # only sold: not a buyer
    ]
    out = tag_buyers(rows, ["kol", "smart_degen", "renowned"], 30, {"kol": 5})
    assert out["buyers"] == 2 and out["source"] == "own" and out["measure_only"]
    assert out["by_tag"]["kol"] == {"buyers": 2, "holding": 1}
    assert out["by_tag"]["smart_degen"] == {"buyers": 1, "holding": 0}
    assert out["by_tag"]["renowned"] == {"buyers": 0, "holding": 0}
    assert [w["wallet"] for w in out["wallets"]] == ["a", "b"] and out["wallets"][0]["first_buy_at"] == at.isoformat()
