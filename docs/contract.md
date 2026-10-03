# gzetryn API contract (v1)

For the ZetrynAI engine (and the dashboard). Base URL on the VPS: `http://127.0.0.1:8793` (loopback only).
**Every request, `/health` included, needs `X-Consumer: <name>`** (lowercase `[a-z0-9_-]`, ≤ 32; the engine uses
`zetryn`). It is used for accounting, not security. Machine-readable schema: `docs/openapi.json`.

## Envelope and errors

```
{"data": ..., "next_cursor": "..." | null, "meta": {"cached", "stale", "age_sec", "fetched_at", ...}}
{"error": {"code", "message", "retry_after_sec"?}}
```

| Status | When |
|---|---|
| 400 `missing_consumer` / `invalid_parameter` / `bad_request` | no header, bad address/param, GMGN refused the params |
| 404 `not_found` | wallet not in the directory / not a manual wallet / GMGN knows no such token |
| 502 `upstream` | GMGN answered something unusable (changed endpoint) |
| 503 `unavailable` + `Retry-After` | budget exhausted or GMGN throttling us; cached data is served with `meta.stale = true` when there is any |

Addresses (wallets and mints) are base58, 32–44 chars. Times are ISO 8601 UTC. Money: `*_usd` in USD,
`*_sol` in SOL. Rates are fractions (0.5 = 50 %).

## Directory

A wallet's `status` is `curated`, `manual`, `curated+manual`, `ranked` (in the latest GMGN rank, not curated) or
`inactive`. **Active** (= polled for trades) means curated or manual.

**Curated list** (owner decision 2026-10-03, values in config): candidates = every wallet in the latest GMGN rank
lists `kol` and `smart_degen` × `7d` and `30d` (100 each, refreshed hourly). Pass = tag kol|smart_degen,
`realized_profit_30d > 0`, `pnl_30d > 0`, `winrate_30d ≥ 0.50`, `(buy_30d + sell_30d)/30 ≤ 150`,
`buy_30d + sell_30d ≥ 20`. Ordered by
`realized_profit_30d` (GMGN's "PnL"), top 50, rebuilt daily. A wallet that drops out gets `curated = false` and
`uncurated_at`; nothing is ever deleted. Manual wallets are never removed by curation.

| Endpoint | Returns |
|---|---|
| `GET /v1/wallets?status=curated\|manual\|active\|ranked\|inactive\|all&tag=&q=&limit=` (default `active`) | wallet objects (below), curated rank first, then 30d profit |
| `GET /v1/wallets/{address}?trades=20` | one wallet + `recent_trades` (feed events, newest first) |
| `GET /v1/wallets/{address}/history?period=7d\|30d&days=30` | rank snapshots: `snapshot_at, period, tag, rank, realized_profit_7d/30d, pnl_7d/30d, winrate_7d/30d, buy_30d, sell_30d, txs_30d, sol_balance` |
| `GET /v1/wallets/{address}/stats` | live GMGN `walletNew` (7d/30d profit, PnL, buys/sells; `winrate_30d` null — GMGN hides it without login); cached 300 s |
| `POST /v1/wallets` `{"address", "label"?, "tags"?: [..], "note"?}` | add/update a manual wallet: 201 new, 200 existing. Its metrics are fetched in the background when it is not in a rank |
| `PATCH /v1/wallets/{address}` `{"label"?, "tags"?, "note"?}` | edit a manual wallet (404 if not manual) |
| `DELETE /v1/wallets/{address}` | clears the manual flag (404 if not manual); a curated wallet stays curated; data is kept |
| `GET /v1/curation` | `{rule, latest}` — the active thresholds and the latest run |
| `GET /v1/curation/runs?limit=10` | runs: `at, status (applied\|skipped), reason, candidates, passed, rejected{rule: n}, selected[{address, rank, name, twitter_username, tags, realized_profit_30d, pnl_30d, winrate_30d, trades_per_day}], added[], removed[]` |

Wallet object: `address, status, active, curated, curated_rank, curated_at, uncurated_at, manual, label,
user_tags, note, added_by, added_at, name, twitter_username, twitter_name, twitter_fans, avatar, gmgn_tags,
rank_lists (["kol:30d", ...]), metrics {realized_profit_7d, realized_profit_30d, pnl_7d, pnl_30d, winrate_7d,
winrate_30d, buy_30d, sell_30d, txs_30d, trades_per_day_30d, avg_holding_sec_30d, sol_balance, follow_count,
daily_profit_7d [{date, profit}], last_active_at, at, source (rank|wallet_new)}, first_seen_at, last_ranked_at,
watch {started_at, last_poll_at, last_poll_ok_at, last_poll_error, last_trade_at, polls_ok, polls_failed}`.

## Feed (wallet trades)

`GET /v1/feed?after=<seq>&limit=500&wait=<0..30>&wallet=&mint=&side=buy|sell&tag=&baseline=false`

- Events with `seq > after`, ascending. Keep `next_cursor` and pass it as `after` next time (`since` is accepted
  as an alias). `wait` long-polls up to 30 s when nothing is new.
- `next_cursor` also moves past events your filters excluded, and `seq` values commit in order, so following the
  cursor never skips an event. `meta.last_seq` is the newest seq overall. `seq` is increasing but not dense
  (gaps are normal); never infer "missed events" from a gap.
- `tag` matches the wallet's GMGN tags or its manual tags (as frozen in the event).

Event: `seq, trade_at (block time), seen_at (our poll), lag_sec (seen − trade), wallet, wallet_name,
twitter_username, wallet_tags, label, user_tags, wallet_status (frozen at event time), side, mint, symbol,
token_amount, sol_amount (null when the quote is not SOL), quote_symbol, usd_amount, price_usd, price_sol,
total_supply, mcap_usd (= price_usd × total_supply at the trade), liquidity_usd (always null: GMGN does not expose
it per trade), open_or_close (GMGN's is_open_or_close flag), launchpad, launchpad_platform, tx_hash, baseline,
payload {quote_amount, quote_address, buy_cost_usd, gas_usd, dex_usd, priority_fee, tip_fee}`.

Semantics:

- **`baseline = true`**: the trade happened before the wallet became active (history picked up by the first poll).
  It is not a signal; it is hidden unless `baseline=true` is asked.
- **Lag**: `lag_sec = seen_at − trade_at`, where `trade_at` is GMGN's block time in **whole seconds** (so `lag_sec`
  overstates the true delay by 0–1 s) and `seen_at` is when gzetryn fetched the trade; the event is in the feed a
  few milliseconds later. A long-poll `/v1/feed?wait=25` returns it in the same second.
- How trades are found: an on-chain trigger (free Solana WebSocket, `logsSubscribe` per active wallet) sees the
  wallet's transaction ~1–2 s after the block and gzetryn polls GMGN for that wallet right away (retries until GMGN
  has indexed it, ≤ 30 s). Typical `lag_sec` is a few seconds; it grows only by GMGN's own indexing delay. If the
  trigger is down or missed a transaction, interval polling catches the trade (every 120/300/900 s by recency while
  the trigger is healthy, 45/90/300 s while it is down), with a larger `lag_sec`. `payload.source` says which path
  found the event (`trigger` | `interval`); `payload.notified` (`swap` | `filtered`) and `payload.notified_at` are
  set when the WebSocket saw the transaction. `/health` → `components.trigger` shows whether the fast path is up. A wallet's `watch.last_poll_ok_at` shows gaps; trades during a gap
  arrive late (larger `lag_sec`), never twice.
- Identity: `(wallet, tx_hash, mint, side, token_amount)`.

## Token intel

`GET /v1/token/{mint}?parts=info,launchpad,security,dev,dev_history,holders,smart_traders,feed&max_age_sec=`

Default: all parts (≤ 10 GMGN calls on a cold cache; cached 30–600 s per part). A part that fails is `null` with
its reason in `errors.{part}`; the call fails only when every GMGN part failed.

| Part | Fields |
|---|---|
| `info` (+ `price`) | `mint, symbol, name, decimals, logo, total_supply, circulating_supply, holder_count, liquidity_usd, created_at, open_at, migrated_at, pool{address, exchange, quote_symbol, quote_reserve, initial_liquidity_usd, created_at}`; `price{price_usd, mcap_usd, change{1m,5m,1h,6h,24h}, volume_usd{..}, buy_volume_usd{..}, sell_volume_usd{..}, buys{..}, sells{..}, swaps{..}, hot_level}` |
| `launchpad` | `creator, launchpad, launchpad_platform, launchpad_status, bonding_progress (0–1), migrated, migrated_at, migration_mcap, migration_mcap_quote, exchange, ath_price_usd, holder_count, liquidity_usd` |
| `security` | `mint_authority_renounced, freeze_authority_renounced, top_10_holder_rate, burn_ratio, burn_status, dev_token_burn_ratio, buy_tax, sell_tax, is_show_alert, lp_locked, lp_lock_percent` |
| `dev` | `creator` (GMGN blanks it for CTO tokens; use `launchpad.creator`), `creator_token_balance, creator_token_status (e.g. creator_hold, creator_close), creator_open_count, fund_from, fund_from_at, cto_flag, dexscreener_ad, dexscreener_update_link, dexscreener_boost_fee, twitter_rename_count, twitter_del_post_token_count, twitter_create_token_count` |
| `dev_history` | `creator, created_total, never_migrated, migrated, migrated_ratio, last_create_at, ath{mint, symbol, mcap_usd}, listed, recent[{mint, symbol, created_at, migrated, mcap_usd, ath_mcap_usd, holders, launchpad_platform}]` |
| `holders` | `rates{holder_count, top_10_holder_rate, creator_hold_rate, dev_team_hold_rate, sniper_hold_rate_top70, fresh_wallet_rate, bot_degen_rate, bundler_trader_rate, insider_trader_rate, entrapment_trader_rate, bluechip_owner_rate, private_vault_hold_rate, creator_created_count}`, `holder_counts_by_tag` and `trader_counts_by_tag` `{smart_degen, kol, sniper, bundler, insider, dev, fresh_wallet, dex_bot, bluechip_owner, following}` |
| `smart_traders` | KOL and smart wallets that traded it (GMGN): `[{address, tags ["kol"\|"smart_degen"], gmgn_tags, buy_usd, sell_usd, buys, sells, holding_rate, holding_usd, avg_cost_usd, avg_sold_usd, profit_usd, realized_profit_usd, unrealized_profit_usd, first_at, exited_at, last_active_at, name, twitter_username, label, directory_status}]`, oldest entry first |
| `feed` | from our own feed: `{wallets: [{wallet, name, twitter_username, label, buys, sells, buy_usd, sell_usd, buy_sol, sell_sol, first_buy_at, last_trade_at}], events: [...]}` |

`meta`: `cached` (every call from cache), `stale`, `age_sec` (oldest part), `fetched_at`, `gmgn_calls`.

## Leaderboards

| Endpoint | Returns |
|---|---|
| `GET /v1/leaderboard?period=30d\|7d&tag=kol\|smart_degen\|all&sort=profit\|pnl\|winrate&limit=100` | latest snapshot rows: `position, address, name, twitter_username, lists, gmgn_rank, realized_profit, pnl, winrate (for the period), realized_profit_30d, winrate_30d, trades_per_day_30d, sol_balance, curated, curated_rank, manual, prev_rank, profit_change` (vs the snapshot ~24 h earlier); `meta.snapshot_at`, `meta.prev_snapshot_at` |
| `GET /v1/leaderboard/copy?limit=50` | "who to copy" research view: wallets passing the curation filters, `score` = 0.35 × 30d profit percentile + 0.30 × 30d win rate + 0.20 × share of profitable days (7d) + 0.15 × presence in ranks over 7 days, with `components`; weights in `meta.weights`. Research only, not a trading signal |

## Market lists (GMGN passthrough, cached 20 s, GMGN field names kept)

| Endpoint | Rows |
|---|---|
| `GET /v1/market/trending?interval=1m\|5m\|1h\|6h\|24h&limit=50` | GMGN swaps rank (price, volume, liquidity, market_cap, holder_count, smart_degen_count, renowned_count, creator, launchpad, …) |
| `GET /v1/market/new-pairs?interval=…&limit=50` | new pairs (`base_address`, `launchpad`, `open_timestamp`, `base_token_info{…}`) |
| `GET /v1/market/pump?limit=30` | pump.fun `{new, completing, completed}` |

## Operations

| Endpoint | Returns |
|---|---|
| `GET /health` | `status ok\|degraded\|broken`, `revision`, components `db`, `gmgn` (pause, throttles, last ok), `directory` (last refresh), `curation` (curated/manual counts, last run), `watcher` (active wallets, tiers, mode, polls, events, trigger counters), `trigger` (connected, healthy, subscriptions, reconnects, notifications) |
| `GET /v1/stats` | GMGN requests since start (per consumer, per endpoint and outcome, average latency, req/min), last 24 h from the database (incl. throttled count), budget, cache, job summaries (`watcher.trigger`: polls, hits, timeouts, caps, notification→GMGN-index p50/p90, coverage), wallet counts, feed 24 h (events, live events, lag p50/p90 overall and `live_by_source`) |
| `POST /v1/admin/refresh?curate=false` | run a rank refresh now (optionally the curation too) |
| `POST /v1/admin/curate` | run the curation now from the latest snapshots |
