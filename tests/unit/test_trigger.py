"""On-chain trigger: swap classifier on real logs (phase 7 capture), WS message handling, watcher scheduling."""

from __future__ import annotations

import json

import pytest

from gzetryn.clock import FakeClock
from gzetryn.config import TriggerTunables, WatchTunables
from gzetryn.core import watch as W
from gzetryn.jobs.watcher import Watcher
from gzetryn.trigger.solana_ws import WsTrigger, swap_like
from tests.conftest import load_fixture

WALLET = "DZAa55HwXgv5hStwaTEJGXZz1DhHejvpb7Yr762urXam"


def test_swap_like_on_captured_logs():
    samples = load_fixture("ws-logs.json")
    assert sum(s["gmgn_trade"] for s in samples) == 2
    for s in samples:
        assert swap_like(s["logs"]) is s["gmgn_trade"], (s["wallet"], s["signature"])


def test_swap_like_rules():
    assert swap_like(None) is False
    assert swap_like(["Program log: Instruction: Swap"]) is False  # no token movement
    assert swap_like(["Program log: Instruction: Transfer", "Program log: Instruction: Sell"]) is True
    assert swap_like(["Program log: Instruction: Transfer", "Program log: ray_log: AwAAAA"]) is True
    assert swap_like(["Program log: Instruction: TransferChecked"]) is False  # plain token transfer


def _trigger(calls):
    t = TriggerTunables()
    tr = WsTrigger(t, lambda w, s, slot: calls.append((w, s, slot)), FakeClock())
    tr.set_wallets({WALLET})
    tr._connected = True
    return tr


def test_ws_handle_ack_err_swap_and_skip():
    calls: list = []
    tr = _trigger(calls)
    rid = tr._req("sub", WALLET)
    tr._handle(json.dumps({"jsonrpc": "2.0", "result": 777, "id": rid}))
    assert tr.healthy and tr.summary()["subscriptions"] == 1
    trade = next(s for s in load_fixture("ws-logs.json") if s["gmgn_trade"])
    other = next(s for s in load_fixture("ws-logs.json") if not s["gmgn_trade"])

    def note(sig, err, logs):
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "logsNotification",
                "params": {
                    "subscription": 777,
                    "result": {"context": {"slot": 42}, "value": {"signature": sig, "err": err, "logs": logs}},
                },
            }
        )

    tr._handle(note("failed", {"InstructionError": [0, "x"]}, trade["logs"]))
    tr._handle(note("other", None, other["logs"]))
    tr._handle(note(trade["signature"], None, trade["logs"]))
    tr._handle(note("unknown-sub", None, trade["logs"]).replace("777", "778"))
    assert calls == [(WALLET, trade["signature"], 42)]
    s = tr.summary()
    assert s["notifications"] == 3 and s["failed_tx"] == 1 and s["ok_tx"] == 2
    assert s["swap_like"] == 1 and s["skipped_not_swap"] == 1
    assert tr.sigs.get("other", 1000.0)[1] is False
    tr.set_wallets(set())
    assert not tr.healthy


def _watcher():
    clock = FakeClock()
    w = Watcher(WatchTunables(), None, None, None, clock, TriggerTunables())  # type: ignore[arg-type]
    w._ctx[WALLET] = object()  # type: ignore[assignment]
    return w, clock


def test_trigger_debounce_min_gap_and_cap():
    w, clock = _watcher()
    t0 = clock.monotonic()
    w.on_trade(WALLET, "s1", 1)
    assert w._trig_due[WALLET] == pytest.approx(t0 + 1.0)
    w._last_trig_poll[WALLET] = t0 + 0.5  # a poll just ran → next one waits for min_gap (2 s)
    w._trig_due.clear()
    w.on_trade(WALLET, "s2", 1)
    assert w._trig_due[WALLET] == pytest.approx(t0 + 2.5)
    w._trig_times[WALLET].extend([t0] * 10)  # 10 triggered polls in the last minute
    w._trig_due.clear()
    w.on_trade(WALLET, "s3", 1)
    assert WALLET not in w._trig_due and w.stats.trigger_capped == 1
    w.on_trade("unknown-wallet", "s4", 1)
    assert w.stats.triggers == 3


def test_trigger_resolve_hit_retry_timeout():
    w, clock = _watcher()
    w.on_trade(WALLET, "a", 1)
    w.on_trade(WALLET, "b", 1)
    clock.advance(1.5)
    w._trig_due.clear()
    w._resolve(WALLET, {"a"}, clock.monotonic())  # a indexed, b not yet → retry in 2 s
    assert w.stats.trigger_hits == 1 and list(w._pending[WALLET]) == ["b"]
    assert w._trig_due[WALLET] == pytest.approx(clock.monotonic() + 2.0)
    for delay in (4.0, 8.0, 16.0):
        clock.advance(1)
        w._trig_due.clear()
        w._resolve(WALLET, set(), clock.monotonic())
        assert w._trig_due[WALLET] == pytest.approx(clock.monotonic() + delay)
    clock.advance(30)
    w._resolve(WALLET, set(), clock.monotonic())  # past max_wait → dropped
    assert WALLET not in w._pending and w.stats.trigger_timeouts == 1
    assert list(w.stats.hit_after_sec) == [pytest.approx(1.5)]


def test_intervals_and_retry_schedule():
    t = WatchTunables()
    assert W.interval(None, FakeClock().now(), t, fallback=True) == t.fallback_cold_interval_sec
    assert W.interval(None, FakeClock().now(), t) == t.cold_interval_sec
    assert [W.next_retry(n, [2, 4]) for n in (1, 2, 3)] == [2, 4, None]
