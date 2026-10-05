"""HTTP API with a real store (schema gzetryn_test) and GMGN answered from fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from gzetryn.api.app import create_app
from gzetryn.api.main import backend_of
from gzetryn.config import CurationTunables, Settings, Tunables, WatchTunables
from gzetryn.runtime import Runtime
from tests.conftest import TEST_SCHEMA, FakeTransport

pytestmark = pytest.mark.db

H = {"X-Consumer": "zetryn"}
WALLET = "DZAa55HwXgv5hStwaTEJGXZz1DhHejvpb7Yr762urXam"
MINT = "8HQgEcbhR5Xoh735zvndAocCLd7wdAYJfWHV55J8pump"


@pytest.fixture
async def client(sessions, migrated):
    t = Tunables(curation=CurationTunables(min_candidates=1), watch=WatchTunables(first_spread_sec=0))
    rt = Runtime(Settings(database_url=migrated, db_schema=TEST_SCHEMA), tunables=t, transport=FakeTransport())
    await rt.start(background=False)
    app = create_app(lambda: None)
    app.state.backend = backend_of(rt)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.rt = rt  # type: ignore[attr-defined]
        yield c
    await rt.stop()


async def test_consumer_required(client):
    r = await client.get("/health")
    assert r.status_code == 400 and r.json()["error"]["code"] == "missing_consumer"
    r = await client.get("/health", headers=H)
    assert r.status_code == 200 and r.json()["components"]["db"]["status"] == "ok"


async def test_manual_wallet_lifecycle(client):
    r = await client.post("/v1/wallets", headers=H, json={"address": WALLET, "label": "ozark", "tags": ["kol"]})
    assert r.status_code == 201 and r.json()["data"]["status"] == "manual"
    r = await client.post("/v1/wallets", headers=H, json={"address": WALLET})
    assert r.status_code == 200 and r.json()["data"]["label"] == "ozark"
    r = await client.post("/v1/wallets", headers=H, json={"address": "not-an-address"})
    assert r.status_code == 400
    r = await client.patch(f"/v1/wallets/{WALLET}", headers=H, json={"label": "oz"})
    assert r.json()["data"]["label"] == "oz"
    r = await client.get("/v1/wallets", headers=H, params={"status": "manual"})
    assert [w["address"] for w in r.json()["data"]] == [WALLET]
    r = await client.get(f"/v1/wallets/{WALLET}", headers=H)
    assert r.status_code == 200 and r.json()["data"]["recent_trades"] == []
    r = await client.delete(f"/v1/wallets/{WALLET}", headers=H)
    assert r.status_code == 200 and r.json()["data"]["manual"] is False
    r = await client.delete(f"/v1/wallets/{WALLET}", headers=H)
    assert r.status_code == 404


async def test_refresh_curate_leaderboard(client):
    r = await client.post("/v1/admin/refresh", headers=H, params={"curate": "true"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["refresh"]["lists"] == {"kol:7d": 6, "kol:30d": 6, "smart_degen:7d": 6, "smart_degen:30d": 6}
    assert body["curation"]["status"] == "applied"
    r = await client.get("/v1/leaderboard", headers=H, params={"period": "30d", "tag": "kol"})
    rows = r.json()["data"]
    assert len(rows) == 6 and rows[0]["position"] == 1
    assert rows[0]["realized_profit"] >= rows[-1]["realized_profit"]
    r = await client.get("/v1/leaderboard/copy", headers=H)
    assert r.status_code == 200 and "weights" in r.json()["meta"]
    r = await client.get("/v1/curation", headers=H)
    assert r.json()["data"]["latest"]["status"] == "applied"


async def test_watcher_poll_feeds_events(client):
    rt = client.rt
    await client.post("/v1/wallets", headers=H, json={"address": WALLET})
    await rt.watcher.reload()
    n = await rt.watcher.poll(WALLET)
    assert n == 6
    # fixture trades are older than the moment the wallet became active → baseline
    r = await client.get("/v1/feed", headers=H, params={"after": 0})
    assert r.json()["data"] == []
    r = await client.get("/v1/feed", headers=H, params={"after": 0, "baseline": "true"})
    data = r.json()["data"]
    assert len(data) == 6 and all(e["baseline"] for e in data)
    assert r.json()["next_cursor"] == str(data[-1]["seq"])
    assert await rt.watcher.poll(WALLET) == 0  # idempotent


async def test_triggered_poll_resolves_signature(client):
    rt = client.rt
    await client.post("/v1/wallets", headers=H, json={"address": WALLET})
    await rt.watcher.reload()
    assert rt.trigger is not None and rt.trigger.summary()["wallets"] == 1
    sig = "45G5viAvp2PdN7wELCBoJryThidWK6t1qSsRC5PpguzXBiqVoSF19zwo8CQbss4WJQRdDy63b3kiZ8iuUSJ33wjP"  # in fixture
    rt.watcher.on_trade(WALLET, sig, 1)
    rt.watcher.on_trade(WALLET, "not-indexed-yet", 1)
    await rt.watcher.poll(WALLET, "trigger")
    s = rt.watcher.summary()["trigger"]
    assert s["trigger_polls"] == 1 and s["hits"] == 1 and s["pending_wallets"] == 1
    r = await client.get("/v1/feed", headers=H, params={"after": 0, "baseline": "true"})
    assert {e["payload"]["source"] for e in r.json()["data"]} == {"trigger"}
    h = (await client.get("/health", headers=H)).json()
    assert "trigger" in h["components"]


async def test_token_intel_all_parts(client):
    r = await client.get(f"/v1/token/{MINT}", headers=H)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["info"]["symbol"] == "PUDU" and d["price"]["price_usd"] > 0
    assert d["launchpad"]["creator"] == "2griikJDBj2uJnjZ47s86tRn1XVTrLu2hMGS5UZto4sf"
    assert d["dev_history"]["creator"] == d["launchpad"]["creator"] and d["dev_history"]["migrated"] == 37
    assert d["security"]["mint_authority_renounced"] is True
    assert d["holders"]["holder_counts_by_tag"]["kol"] == 2
    assert {x["address"] for x in d["smart_traders"]} and all("tags" in x for x in d["smart_traders"])
    assert d["feed"] == {"wallets": [], "events": []}
    assert d["errors"] == {}
    assert r.json()["meta"]["gmgn_calls"] == 10
    r2 = await client.get(f"/v1/token/{MINT}", headers=H, params={"parts": "security,dev"})
    d2 = r2.json()["data"]
    assert set(d2) == {"mint", "security", "dev", "errors"} and r2.json()["meta"]["cached"] is True
    r3 = await client.get(f"/v1/token/{MINT}", headers=H, params={"parts": "nope"})
    assert r3.status_code == 400


async def test_token_holders_partial_while_vas_cools(client):
    rt = client.rt
    rt.budget.throttle("vas")  # vas cools 15 s
    rt.t.budget.max_wait_sec["P1"] = 0.5  # the API call must not wait it out in this test
    r = await client.get(f"/v1/token/{MINT}", headers=H, params={"parts": "holders,security", "max_age_sec": 0})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["holders"]["rates"]["holder_count"] == 1263  # token_stat is /api/: still served
    assert d["holders"]["holder_counts_by_tag"] is None and d["errors"]["holders"].startswith("partial:")
    assert d["security"] is not None


async def test_chain_fallback_event_then_gmgn_duplicate_skipped(client):
    from gzetryn.gmgn.parse import TradeRow
    from gzetryn.jobs.chain_fallback import Job
    from tests.conftest import load_fixture

    rt = client.rt
    case = load_fixture("chain-tx.json")[1]
    g, res = case["gmgn"], case["rpc"]["result"]
    await client.post("/v1/wallets", headers=H, json={"address": g["wallet"]})
    await rt.watcher.reload()
    # the fixture trade is older than this test's activation → pretend the wallet was active before it
    rt.watcher._ctx[g["wallet"]].watch_started_at = datetime(2026, 1, 1, tzinfo=UTC)

    async def fake_get_tx(sig):
        return res

    rt.chain._get_tx = fake_get_tx
    # routing: with the vas group cooling, a notification goes to the chain fallback
    rt.budget.throttle("vas")
    assert not rt.budget.is_open("vas")
    rt.watcher.on_trade(g["wallet"], g["tx_hash"], 1)
    assert rt.watcher.stats.chain_routed == 1 and rt.chain.summary()["queue"] == 1
    await rt.chain._process(Job(g["wallet"], g["tx_hash"], rt.clock.monotonic(), rt.clock.now().isoformat()))
    s = rt.chain.summary()
    assert s["decoded"] == 1 and s["events"] == 1
    r = await client.get("/v1/feed", headers=H, params={"after": 0})
    ev = r.json()["data"]
    assert len(ev) == 1 and ev[0]["payload"]["source"] == "chain" and ev[0]["side"] == g["side"]
    assert ev[0]["tx_hash"] == g["tx_hash"] and ev[0]["sol_amount"] > 0
    # GMGN's row for the same trade (rounded amount) arrives later: not a second event
    ctx = rt.watcher.context_of(g["wallet"])
    gm = TradeRow(
        wallet=g["wallet"], tx_hash=g["tx_hash"], trade_at=datetime.now(UTC),
        side=g["side"], mint=g["mint"], symbol="X", token_amount=round(g["token_amount"]), sol_amount=g["sol_amount"],
        quote_symbol="SOL", usd_amount=1.0, price_usd=1.0, price_sol=1.0, total_supply=1.0, mcap_usd=1.0,
        open_or_close=None, launchpad=None, launchpad_platform=None, payload={"source": "trigger"},
    )
    assert await rt.feed.insert(ctx, [gm], rt.clock.now()) == []
    assert len((await client.get("/v1/feed", headers=H, params={"after": 0})).json()["data"]) == 1


async def test_candidates_cursor_no_duplicates(client):
    rt = client.rt
    assert await rt.candidates.pump() == 6
    assert await rt.candidates.new_pairs() == 3
    n_tr = await rt.candidates.trending()
    # the fake answers every pool lookup with the PUDU window-info fixture: only PUDU (trending #1) gets a pool
    assert n_tr == 3 and rt.candidates.pools_resolved + rt.candidates.pools_missing == 3
    assert await rt.candidates.pump() == 0  # second sighting: no new seq
    r = await client.get("/v1/market/candidates", headers=H, params={"kind": "migrated"})
    rows = r.json()["data"]
    assert [x["kind"] for x in rows] == ["migrated", "migrated"] and rows[0]["pool_address"]
    assert rows[0]["seen_count"] == 2 and rows[0]["first"]["smart_degen_count"] == 17
    cur = r.json()["next_cursor"]
    r2 = await client.get("/v1/market/candidates", headers=H, params={"kind": "migrated", "after": cur})
    assert r2.json()["data"] == [] and r2.json()["next_cursor"] == cur
    allr = (await client.get("/v1/market/candidates", headers=H)).json()["data"]
    assert len(allr) == 12 and [x["seq"] for x in allr] == sorted(x["seq"] for x in allr)
    assert (await client.get("/v1/market/candidates", headers=H, params={"kind": "bogus"})).status_code == 400


async def test_market_and_stats(client):
    r = await client.get("/v1/market/pump", headers=H)
    assert set(r.json()["data"]) == {"new", "completing", "completed"}
    r = await client.get("/v1/market/trending", headers=H, params={"interval": "5m"})
    assert len(r.json()["data"]) == 3
    r = await client.get("/v1/stats", headers=H)
    s = r.json()["data"]
    assert s["since_start"]["gmgn_requests"] >= 1 and "budget" in s and s["feed_last_seq"] == 0
