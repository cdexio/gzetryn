"""Curation rule, copy score, watcher decisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gzetryn.config import CopyScoreTunables, CurationTunables, WatchTunables
from gzetryn.core import watch as W
from gzetryn.core.copyscore import percentiles, positive_day_share, score_rows
from gzetryn.core.curation import Candidate, check, curate, diff


def cand(addr, profit=1000.0, pnl=0.2, wr=0.6, buys=300, sells=300, tags=("kol",)):
    return Candidate(addr, set(tags), profit, pnl, wr, buys, sells)


def test_curation_rules_in_order():
    t = CurationTunables()
    assert check(cand("a"), t) is None
    assert check(cand("a", tags=("whale",)), t) == "tag"
    assert check(cand("a", profit=0.0), t) == "profit_30d"
    assert check(cand("a", profit=None), t) == "profit_30d"
    assert check(cand("a", pnl=-0.1), t) == "pnl_30d"
    assert check(cand("a", wr=0.49), t) == "winrate_30d"
    assert check(cand("a", wr=0.5), t) is None  # >= 0.50 passes
    assert check(cand("a", buys=2300, sells=2300), t) == "bot_paced"  # 153/day
    assert check(cand("a", buys=2250, sells=2250), t) is None  # exactly 150/day passes
    assert check(cand("a", tags=("smart_degen", "axiom")), t) is None


def test_curate_orders_by_profit_and_caps():
    t = CurationTunables(top_n=2)
    res = curate(
        [cand("low", profit=10), cand("top", profit=500), cand("mid", profit=100), cand("bad", wr=0.1)], t
    )
    assert res.addresses == ["top", "mid"]
    assert [x["rank"] for x in res.selected] == [1, 2]
    assert res.candidates == 4 and res.passed == 3 and res.rejected == {"winrate_30d": 1}


def test_diff():
    added, removed = diff({"a", "b"}, ["b", "c"])
    assert added == ["c"] and removed == ["a"]


def test_copy_score_components():
    assert positive_day_share([{"profit": 1}, {"profit": -1}, {"profit": 2}, {"profit": None}]) == pytest.approx(2 / 3)
    assert positive_day_share(None) is None
    assert percentiles([10, 30, 20]) == [0.0, 1.0, 0.5]
    rows = [
        {"address": "a", "realized_profit_30d": 100, "winrate_30d": 0.9, "daily_profit_7d": [{"profit": 1}], "presence_days": 7},
        {"address": "b", "realized_profit_30d": 900, "winrate_30d": 0.5, "daily_profit_7d": [{"profit": -1}], "presence_days": 1},
    ]
    out = score_rows(rows, CopyScoreTunables())
    a = next(x for x in out if x["address"] == "a")
    assert a["score"] == pytest.approx(0.35 * 0 + 0.30 * 0.9 + 0.20 * 1 + 0.15 * 1)
    assert out[0]["address"] == "a"  # consistency beats raw profit here
    assert set(a["components"]) == {"profit_percentile", "winrate_30d", "positive_day_share_7d", "presence"}


NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def test_tiers():
    t = WatchTunables()
    assert W.tier(None, NOW, t) == "cold"
    assert W.tier(NOW - timedelta(minutes=10), NOW, t) == "hot"
    assert W.tier(NOW - timedelta(hours=5), NOW, t) == "warm"
    assert W.tier(NOW - timedelta(days=3), NOW, t) == "cold"
    assert W.interval(NOW - timedelta(minutes=10), NOW, t) == t.hot_interval_sec


def test_paging_rule():
    old = NOW - timedelta(hours=1)
    newer = NOW - timedelta(minutes=5)
    # full page, all new, older than nothing we know → next page
    assert W.want_next_page(20, 20, 20, newer, old, True) is True
    # some rows already known → stop
    assert W.want_next_page(20, 20, 19, newer, old, True) is False
    # page reaches back past our newest stored trade → stop
    assert W.want_next_page(20, 20, 20, old - timedelta(minutes=1), old, True) is False
    # short page, no cursor, first-ever poll → stop
    assert W.want_next_page(20, 7, 7, newer, old, True) is False
    assert W.want_next_page(20, 20, 20, newer, old, False) is False
    assert W.want_next_page(20, 20, 20, newer, None, True) is False


def test_baseline():
    assert W.is_baseline(NOW - timedelta(seconds=1), NOW) is True
    assert W.is_baseline(NOW, NOW) is False
    assert W.is_baseline(NOW, None) is True
