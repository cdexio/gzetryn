"""Parsers on the phase 0 fixtures (real GMGN answers, trimmed)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gzetryn.gmgn import parse
from gzetryn.gmgn.answer import (
    BAD_PARAM,
    NEEDS_LOGIN,
    NETWORK,
    NOT_FOUND,
    OK,
    SERVER_ERROR,
    THROTTLED,
    UNKNOWN,
    RawAnswer,
    classify,
)
from tests.conftest import load_fixture


def test_scalars():
    assert parse.f("1.5") == 1.5 and parse.f("") is None and parse.f(None) is None and parse.f("nan") is None
    assert parse.i("24172") == 24172
    assert parse.ts(1791001579) == datetime.fromtimestamp(1791001579, UTC)
    assert parse.ts(1790380800000) == datetime.fromtimestamp(1790380800, UTC)
    assert parse.ts(0) is None
    assert parse.b(1) is True and parse.b("false") is False and parse.b(None) is None


def test_rank_wallets():
    pg = parse.rank_wallets(load_fixture("rank-wallets-kol-7d.json"))
    assert len(pg.items) == 6 and pg.skipped == 0
    w = pg.items[0]
    assert w.address == "4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9"
    assert w.rank == 1 and w.name == "decu" and w.twitter_username == "notdecu"
    assert w.realized_profit_30d == pytest.approx(783016.79, rel=1e-6)
    assert w.pnl_30d == pytest.approx(0.30529, rel=1e-4)
    assert w.winrate_30d == pytest.approx(0.35064, rel=1e-4)
    assert w.buy_30d == 24172 and w.sell_30d == 14209
    assert w.trades_per_day_30d == pytest.approx((24172 + 14209) / 30)
    assert "kol" in w.tags
    assert w.daily_profit_7d[0]["profit"] == pytest.approx(17448.2388, rel=1e-6)
    assert len(w.daily_profit_7d) == 7
    assert w.last_active_at == datetime.fromtimestamp(1791001579, UTC)


def test_rank_wallets_smart_degen_tags():
    pg = parse.rank_wallets(load_fixture("rank-wallets-sd-30d.json"))
    assert all("smart_degen" in w.tags for w in pg.items)


def test_rank_wallets_tolerates_garbage():
    pg = parse.rank_wallets({"code": 0, "data": {"rank": [{"foo": 1}, None, {"wallet_address": "abc"}]}})
    assert len(pg.items) == 1 and pg.skipped == 2
    assert parse.rank_wallets({"code": 0, "data": None}).items == []


def test_wallet_activity():
    pg = parse.wallet_activity(load_fixture("wallet-activity.json"), "DZAa55HwXgv5hStwaTEJGXZz1DhHejvpb7Yr762urXam")
    assert len(pg.items) == 6 and pg.next == "NDUyNzc3NjgyMDA3OTYwMjA6MDo6"
    t = pg.items[0]
    assert t.side == "sell" and t.mint == "EHfj12MDoETURYUFxm9CNr1E8zhpCZqZy9u4njbQpump" and t.symbol == "nvk"
    assert t.sol_amount == pytest.approx(1.242653771) and t.quote_symbol == "SOL"
    assert t.usd_amount == pytest.approx(148.04977, rel=1e-6)
    assert t.price_sol == pytest.approx(4.972234838292e-08)
    assert t.mcap_usd == pytest.approx(0.000005923920586341088 * 999996103, rel=1e-9)
    assert t.open_or_close is True
    assert t.trade_at == datetime.fromtimestamp(1790998340, UTC)
    assert t.payload["buy_cost_usd"] == pytest.approx(120.0673655359)
    usdc = pg.items[-1]
    assert usdc.sol_amount is None and usdc.price_sol is None and usdc.quote_symbol != "SOL"


def test_wallet_new_has_real_30d_metrics():
    w = parse.wallet_new(load_fixture("wallet-new-30d.json"), "9iaawVBEsFG35PSwd4PahwT8fYNQe9XYuRdWm872dUqY")
    assert w is not None and w.name == "meechie"
    assert w.realized_profit_30d and w.realized_profit_30d > 0
    assert w.buy_30d == 1914
    assert w.winrate_30d is None  # GMGN sends null without login
    assert w.twitter_fans == 99322


def test_wallet_common_stat():
    c = parse.wallet_common_stat(load_fixture("wallet-common-stat.json"))
    assert c["twitter_username"] == "ohzarke" and c["twitter_fans"] == 21886
    assert c["fund_from_address"] == "AxiomRXZAq1Jgjj9pHmNqVP7Lhu67wLXZJZbaK87TTSk"


def test_token_window_info():
    p = parse.token_window_info(load_fixture("token-window-info.json"))
    info, price = p["info"], p["price"]
    assert info["symbol"] == "PUDU" and info["decimals"] == 6 and info["holder_count"] == 1281
    assert info["pool"]["exchange"] == "pump_amm" and info["migrated_at"] is not None
    assert price["price_usd"] == pytest.approx(0.000093014371)
    assert price["change"]["1m"] == pytest.approx(0.000093014371 / 0.000066395796 - 1)
    assert price["swaps"]["5m"] == 2277
    assert price["mcap_usd"] == pytest.approx(0.000093014371 * 975144998.26166)


def test_token_multi_info_has_creator():
    m = parse.token_multi_info(load_fixture("token-multi-info.json"))
    assert m["creator"] == "2griikJDBj2uJnjZ47s86tRn1XVTrLu2hMGS5UZto4sf"
    assert m["launchpad_platform"] == "Pump.fun" and m["bonding_progress"] == 1 and m["migrated"] is True


def test_token_security_dev_stats():
    sec = parse.token_security(load_fixture("token-security.json"))
    assert sec["mint_authority_renounced"] is True and sec["freeze_authority_renounced"] is True
    assert sec["top_10_holder_rate"] == pytest.approx(0.1604)
    dev = parse.token_dev_info(load_fixture("token-dev-info.json"))
    assert dev["creator"] is None  # blanked by GMGN for this CTO token → creator comes from multi_token_info
    assert dev["creator_token_status"] == "creator_close" and dev["cto_flag"] is True
    st = parse.token_stat(load_fixture("token-stat.json"))
    assert st["holder_count"] == 1263 and st["bundler_trader_rate"] == pytest.approx(0.3573)
    counts = parse.tag_counts(load_fixture("token-holder-stat.json"))
    assert counts["smart_degen"] == 7 and counts["kol"] == 2 and counts["sniper"] == 21


def test_dev_created_tokens():
    d = parse.dev_created_tokens(load_fixture("dev-created-tokens.json"), recent=2)
    assert d["never_migrated"] == 603 and d["migrated"] == 37 and d["created_total"] == 640
    assert d["ath"]["symbol"] == "BISON"
    assert len(d["recent"]) == 2
    assert d["recent"][0]["created_at"] >= d["recent"][1]["created_at"]


def test_token_traders():
    pg = parse.token_traders(load_fixture("token-traders-renowned.json"), "kol")
    assert len(pg.items) == 2
    row = pg.items[0]
    assert row["tag"] == "kol" and row["address"] == "J23qr98GjGJJqKq9CBEnyRhHbmkaVxtTJNNxKu597wsA"
    assert row["buy_usd"] == pytest.approx(698.8010656) and row["buys"] == 1
    assert "kol" in row["gmgn_tags"]


def test_market_lists():
    assert len(parse.market_rows(load_fixture("rank-swaps-1h.json"), "rank")) == 3
    assert len(parse.market_rows(load_fixture("new-pairs.json"), "pairs")) == 3
    lists = parse.pump_lists(load_fixture("trenches.json"))
    assert {k: len(v) for k, v in lists.items()} == {"new": 2, "completing": 2, "completed": 2}


@pytest.mark.parametrize(
    ("raw", "outcome"),
    [
        (RawAnswer(200, {"code": 0, "data": {}}), OK),
        (RawAnswer(200, {"code": 40000300, "msg": "invalid argument"}), BAD_PARAM),
        (RawAnswer(200, {"code": 50001300, "msg": "internal server error"}), SERVER_ERROR),
        (RawAnswer(401, {"code": 40101611, "msg": "empty token"}), NEEDS_LOGIN),
        (RawAnswer(403, None, text_head="<!DOCTYPE html>"), THROTTLED),
        (RawAnswer(429, None), THROTTLED),
        (RawAnswer(404, None, text_head="404 page not found"), NOT_FOUND),
        (RawAnswer(502, None), SERVER_ERROR),
        (RawAnswer(0, None, error="Timeout"), NETWORK),
        (RawAnswer(200, None, text_head="<html>"), UNKNOWN),
    ],
)
def test_classify(raw, outcome):
    assert classify(raw).outcome == outcome
