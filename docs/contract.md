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
| 503 `unavailable` + `Retry-After` | `message` = `cooldown:<group>` (GMGN's Cloudflare challenged that path group, e.g. `cooldown:vas`; it cools 15 s, doubling on every failed probe up to 1 h, back to normal on the first success; other groups keep working), `budget` (no slot within the wait limit) or `throttled` (2+ groups cooling: global 60 s pause). Cached data is served with `meta.stale = true` instead when there is any |

GMGN path groups (each with its own rate limit and cooldown): `vas` = wallet trades (feed), token holder/trader
stats, smart traders, pump.fun lists; `api` = token info/stat/security/dev/dev history, new pairs; `defi` = wallet
ranks, trending, wallet stats; `mrwapi` = launchpad info. Priority inside a group: trigger polls (feed) > API calls
(you) > interval polls > background jobs.

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
  wallet's transaction ~1–2 s after the block and gzetryn polls GMGN for that wallet right away (retries at +3 and
  +7 s until GMGN has indexed it, ≤ 10 s). Measured 2026-10-03 → 05 over 1,518 trigger events: `lag_sec` p50
  2.64 s, p90 3.41 s. While GMGN challenges the `vas` group (`/health` → `components.gmgn.cooling_groups`), new
  trades wait for the cooldown to end. If the
  trigger is down or missed a transaction, interval polling catches the trade (every 120/300/900 s by recency while
  the trigger is healthy, 45/90/300 s while it is down), with a larger `lag_sec`. `payload.source` says which path
  found the event (`trigger` | `interval` | `chain`); `payload.notified` (`swap` | `filtered`) and `payload.notified_at` are
  set when the WebSocket saw the transaction. `/health` → `components.trigger` shows whether the fast path is up. A wallet's `watch.last_poll_ok_at` shows gaps; trades during a gap
  arrive late (larger `lag_sec`), never twice.
- **`source = chain`** (since 2026-10-05): while GMGN challenges the `vas` group, the trade is decoded from the
  Solana transaction instead, so the feed stays real-time. Differences from a GMGN event: `sol_amount` is what the
  wallet actually paid/received (DEX fees and tips included, 0.4–2.2 % off GMGN's figure); `usd_amount`,
  `price_usd`, `mcap_usd` use a SOL/USD estimate from recent GMGN trades (`payload.sol_usd`); `open_or_close`,
  `launchpad`, `launchpad_platform` are null; `symbol`/`total_supply` come from GMGN token info and can be null.
  Only single-token ↔ SOL swaps are decoded; other shapes wait for GMGN.
- Identity: `(wallet, tx_hash, mint, side)` — a trade is one event whichever path found it first (when `vas`
  reopens, GMGN's row for a chain-decoded trade is not added again).

## Token intel

`GET /v1/token/{mint}?parts=info,launchpad,security,dev,dev_history,holders,smart_traders,feed&max_age_sec=`

Default: all parts (≤ 10 GMGN calls on a cold cache). A part that fails is `null` with its reason in
`errors.{part}`; the call fails only when every GMGN part failed.

**Enricher use (engine)**: `parts=security,launchpad,dev,dev_history,holders,smart_traders` = 9 GMGN calls on a
cold token, 0 when cached. Cache per part: launchpad 60 s, security 600 s, dev 300 s, dev_history 1800 s, holders
120 s, smart_traders 120 s (info 30 s), so repeated calls within those windows are free (`meta.cached = true`).
Runs at API priority, below the real-time feed. For ~200–300 tokens/day expect ≈ 1.6–1.9 extra GMGN req/min.
A cold call takes ~2–5 s (calls are paced per group); a `cooldown:<group>` error on a part means that part's group
is cooling — retry after `Retry-After`. `holders` tag counts and `smart_traders` come from the `vas` group: while
it is challenged, `smart_traders` is null and `holders` returns its `rates` (from `api`) with
`holder_counts_by_tag`/`trader_counts_by_tag` null and `errors.holders = "partial: …"`. For pump.fun candidates the
`smart_degen_count` / `renowned_count` / `sniper_count` in `/v1/market/candidates` rows are an alternative.
(`token_liquidity_stats` was checked as a substitute and rejected: it counts liquidity providers, not holders.)

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

## Market candidates (engine candidate source — use this)

`GET /v1/market/candidates?kind=new,completing,migrated,trending&after=<seq>&limit=500&wait=<0..30>`

Served from gzetryn's database (no GMGN call per request). A background job polls GMGN every 30 s (pump.fun lists,
new pairs) and 60 s (trending 1h). Each (kind, mint) appears **once**, with a `seq` assigned at its first sighting;
keep `next_cursor` and pass it as `after`. `wait` long-polls. `kind` filters (comma list; default all).

| Kind | Source |
|---|---|
| `new` | new pump.fun tokens on the bonding curve (`source` pump_lists), and new pools from GMGN's new pairs (`source` new_pairs: any dex; direct PumpSwap launches `launchpad_platform` pool_pump_amm, meteora, pump_mayhem included) |
| `completing` | pump.fun tokens near the end of the bonding curve (`progress` ≈ 0.9–1.0) |
| `migrated` | pump.fun graduations (`exchange` `pump_amm`, `pool_address` = the AMM pool). Main source: GMGN's pump.fun `completed` list (`source` pump_lists, `complete_at` set). Supplement: new pairs with `pump_amm` + launchpad `pump` + `Pump.fun` (`source` new_pairs, `complete_at` null) — 11 of 12 such rows were confirmed by the completed list and arrived ~279 s earlier, but GMGN's new pairs carry only ~16 % of graduations. The same mint can therefore appear once per kind only, from whichever source saw it first |
| `trending` | GMGN 1 h swaps rank |

Row: `seq, kind, mint, source (pump_lists|new_pairs|rank_swaps), first_seen_at, last_seen_at, seen_count, symbol,
name, pool_address, exchange (dex: pump = bonding curve, pump_amm, raydium…, meteora_dlmm…), launchpad (e.g.
pump), launchpad_platform (e.g. Pump.fun, pump_mayhem), quote_address, creator, created_at (token creation),
open_at (pool open), complete_at (bonding curve completed; migrated only), first{…}, last{…}`.

`first` (frozen at the first sighting) and `last` (latest sighting) have the same keys: `price_usd, liquidity_usd,
mcap_usd (GMGN market cap = price × total supply = FDV), holders, volume_1h_usd, buys_1h, sells_1h, swaps_1h,
smart_degen_count, renowned_count (KOL), sniper_count, top_10_holder_rate, progress (bonding curve 0–1)`. A field
its source does not carry is null: pump.fun rows have no `price_usd`; new-pair rows have no buy/sell/swap counts,
tag counts or holders (often) and no `created_at` (GMGN's value there is the pool open time, not the token's
creation); trending rows have no `progress`.

`pool_address`: pump.fun rows carry it (bonding-curve account while new/completing, AMM pool when migrated); new
pairs carry it; trending rows do not, so gzetryn resolves it from GMGN token info before storing — it can still
be null when GMGN has no pool (then use `/v1/token/{mint}?parts=info` → `info.pool.address`). Candidates are kept 7
days after their last sighting.

## Market lists (raw GMGN passthrough, cached 20 s, GMGN field names kept)

For browsing; the engine should use `/v1/market/candidates`. Verified fields 2026-10-05:

| Endpoint | Rows |
|---|---|
| `GET /v1/market/trending?interval=1m\|5m\|1h\|6h\|24h&limit=50` | GMGN swaps rank: `address` (mint), `symbol, name, price, price_change_percent*, volume, buys, sells, swaps, liquidity, market_cap, holder_count, top_10_holder_rate, open_timestamp, creation_timestamp, exchange, pool_type_str, launchpad, launchpad_platform, launchpad_status, migrated_pool_exchange, creator, smart_degen_count, renowned_count, sniper_count, rug_ratio, …` — **no pool address** |
| `GET /v1/market/new-pairs?interval=…&limit=50` | `address` (**pool**), `base_address` (mint), `exchange, launchpad, launchpad_platform, open_timestamp, quote_address, quote_reserve_usd, initial_liquidity, base_token_info{price, liquidity, market_cap, holder_count, creator, creation_timestamp, progress, buy_volume_*, sell_volume_*, …}` |
| `GET /v1/market/pump?limit=30` | pump.fun `{new, completing, completed}`; rows: `address` (mint), `pool_address, exchange, launchpad, launchpad_platform, created_timestamp, open_timestamp, complete_timestamp, liquidity, usd_market_cap, holder_count, volume_1h, buys_1h, sells_1h, swaps_1h, creator, smart_degen_count, renowned_count, sniper_count, progress, …` |

## Operations

| Endpoint | Returns |
|---|---|
| `GET /health` | `status ok\|degraded\|broken`, `revision`, components `db`, `gmgn` (`global_paused_sec`, `cooling_groups {group: sec}`, `cooldown_levels {group: {level, step_sec, probing}}`, throttles, global pauses, last ok), `directory` (last refresh), `curation` (curated/manual counts, last run), `watcher` (active wallets, tiers, mode, polls, events, trigger counters), `trigger` (connected, healthy, subscriptions, reconnects, notifications) |
| `GET /v1/stats` | GMGN requests since start (per consumer, per endpoint and outcome incl. `denied`, average latency, req/min), last 24 h from the database (incl. throttled count), `budget` (global + `groups{vas, api, defi, mrwapi}`: limits, tokens, cooling, throttles, request counts in the last 10/60/300 s, peaks, counts before the last throttle, granted/denied per priority), cache, `candidates` job, job summaries (`watcher.trigger`: polls, hits, timeouts, caps, notification→GMGN-index p50/p90, coverage), wallet counts, feed 24 h (events, live events, lag p50/p90 overall and `live_by_source`) |
| `POST /v1/admin/refresh?curate=false` | run a rank refresh now (optionally the curation too) |
| `POST /v1/admin/curate` | run the curation now from the latest snapshots |
