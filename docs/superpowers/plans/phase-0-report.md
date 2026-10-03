# Phase 0 — report (2026-10-03)

All probes ran on the VPS (`46.250.236.190`, Singapore Cloudflare edge
`SIN`) with `tools/probe.py`: curl_cffi `impersonate="chrome"`, header
`Referer: https://gmgn.ai/`, no cookies, no login. About 230 requests in
total, 0.5–1.2 s apart. **No 429, no Cloudflare challenge on any JSON
endpoint.** Latency p50 0.25 s, p95 0.4–0.8 s.

Sample subjects (picked from live answers): KOL wallets
`DZAa55HwXgv5hStwaTEJGXZz1DhHejvpb7Yr762urXam` (ozark),
`9iaawVBEsFG35PSwd4PahwT8fYNQe9XYuRdWm872dUqY` (meechie),
`DNfuF1L62WWyW3pNakVkyGGFzVVhj4Yr52jSmdTyeBHm` (gake); pump.fun mint
`8HQgEcbhR5Xoh735zvndAocCLd7wdAYJfWHV55J8pump` (PUDU, 21 min old), its
creator `2griikJDBj2uJnjZ47s86tRn1XVTrLu2hMGS5UZto4sf`.

## Discovery

The public HTML of `gmgn.ai` (wallet, token and trade pages) loads without
a challenge from the VPS. The Next.js bundles (59 files) contain ~370 API
path strings; the parameter names and POST bodies below come from the code
around each path, then were verified live. The web app adds common query
parameters (`device_id`, `client_id`, `from_app=gmgn`, `app_ver`,
`tz_name`, `os`, …) to every call; **none of them is required** — every
endpoint below answered without them.

## Envelope and errors

JSON answers are `{code, msg | message, reason?, data}`; `code = 0` is
success.

| Answer | Meaning | gzetryn class |
|---|---|---|
| 200, `code 0` | ok | ok |
| 200, `code 40000300` "invalid argument" | bad path parameter (e.g. old `/defi/quotation/v1/tokens/sol/{mint}`) | bad_param |
| 200, `code 50001300` "internal server error" | GMGN failed on our parameters | server_error |
| 401, `code 40101611` "empty token" | endpoint needs a GMGN login (`/pf/api/...`) | needs_login — never used |
| 403, Cloudflare HTML (answered in 0.01 s) | edge rule block (`/api/v1/wallet_holdings/...`) | blocked |
| 404, `404 page not found` (text) | unknown path | not_found |
| 429 | not seen | throttled (pause) |

## Endpoints verified (no login)

All `GET` unless marked POST; base `https://gmgn.ai`; chain `sol`.

### Wallet rank (KOL / smart money directory)

`/defi/quotation/v1/rank/sol/wallets/{1d|7d|30d}?tag={kol|smart_degen|…}&orderby=…&direction=desc`
→ `data.rank[]`, **100 rows max** (`limit=200` still returns 100).

- Row fields: `wallet_address` (= `address`), `name`, `nickname`,
  `twitter_username`, `twitter_name`, `twitter_description`, `avatar`,
  `tags[]`, `balance` / `sol_balance` (SOL, string), `follow_count`,
  `remark_count`, `last_active` (unix s); per window `1d|7d|30d`:
  `pnl_*` (ROI ratio, string), `realized_profit_*` (USD, string),
  `winrate_*` (0–1), `buy_*`, `sell_*`, `txs_*`, `volume_*`,
  `net_inflow_*`, `avg_cost_*`, `avg_holding_period_*` (s); 7d buckets
  `pnl_gt_5x_num_7d`, `pnl_2x_5x_num_7d`, `pnl_lt_2x_num_7d`,
  `pnl_minus_dot5_0x_num_7d`, `pnl_lt_minus_dot5_num_7d`;
  `daily_profit_7d[]` = `{timestamp, profit}` (USD per day, 7 items).
- **`orderby=pnl_30d` sorts by `realized_profit_30d` (USD), not by the
  ROI ratio** (observed: smart_degen 30d rows 181 567, 136 245, 130 018 …
  USD while `pnl_30d` is 0.19, 0.32, 0.20). `orderby=realized_profit_30d`
  gives the same order. GMGN's "PnL" leaderboard is therefore a USD
  profit leaderboard.
- Every row of `tag=kol` carries `kol` in `tags`; every row of
  `tag=smart_degen` carries `smart_degen`.
- Also served under `/api/v1/rank/sol/wallets/...` (same answer).

### Wallet activity (the feed source)

`/vas/api/v1/wallet_activity/sol?wallet={addr}&limit={≤50}&type=buy&type=sell[&cursor={next}]`
→ `data.activities[]`, `data.next` (opaque cursor; `cursor=` pages back).

- Fields: `wallet`, `tx_hash`, `timestamp` (unix s), `event_type`
  (`buy`/`sell`), `token{address, symbol, logo, total_supply}`,
  `token_amount`, `quote_amount` (SOL when `quote_address` is wSOL),
  `cost_usd`, `buy_cost_usd` (sells: cost basis), `price_usd`, `price`
  (in SOL), `is_open_or_close` (1 = first buy / full exit, 0 otherwise),
  `quote_token{token_address, symbol, decimals}`, `quote_address`,
  `gas_native`, `gas_usd`, `dex_native`, `dex_usd`, `priority_fee`,
  `tip_fee`, `launchpad`, `launchpad_platform`.
- No market cap or liquidity in the row: **mcap at trade time =
  `price_usd × token.total_supply`**; liquidity is not available per
  trade.
- Freshness: for an active wallet the newest buy was **30 s** old at
  fetch time. `last_active` in rank/stat can be newer than the newest
  buy/sell (it counts other activity), so it is not a trade signal.
- `/defi/quotation/v1/wallet_activity/sol` answers `code 0` with an empty
  list (dead); `/api/v1/wallet_activity/...` is 404.

### Wallet stats and profile

- `/api/v1/wallet_stat/sol/{addr}/{7d|30d|all}` → same field names as
  below plus `tags[]`, `tag_rank{}`, twitter, `follow_count`,
  `last_active_timestamp`, `risk{…}`, `creator_created_count`. **Without a
  login every period metric is 0** (`buy_30d`, `realized_profit_30d`,
  `pnl_30d`, `winrate` … = 0 for ozark and meechie, whose rank rows show
  44 529 USD / 2 571 buys); only all-time `buy`/`sell` and identity are
  real. Not used for metrics.
- `/defi/quotation/v1/smartmoney/sol/walletNew/{addr}?period=30d` →
  **real** 7d/30d metrics: `realized_profit_7d/30d`, `pnl_7d/30d`,
  `buy_7d/30d`, `sell_7d/30d`, `buy`/`sell` (for the period asked),
  `sol_balance`, `last_active_timestamp`, twitter (`twitter_username`,
  `twitter_name`, `twitter_fans_num`), `tags`. `winrate` is **null**.
  Values are close to, not equal to, the rank row (meechie 30d: 54 360 vs
  44 529 USD; computed at another time). Used for **manual wallets** that
  are not in a rank (their win rate stays unknown).
- `/api/v1/wallet_common_stat/sol/{addr}` → `name`, `tags`, twitter
  (`twitter_username`, `twitter_fans_num`, `is_blue_verified`),
  `created_token_count`, `fund_from`, `fund_from_address`,
  `fund_amount`, `fund_from_ts`.
- `/defi/quotation/v1/smartmoney/sol/walletstat/{addr}` → **timed out
  (20 s, no bytes)**; not used.
- Holdings: `/api/v1/wallet_holdings/sol/{addr}` → **403 Cloudflare**;
  `/pf/api/v1/wallet/sol/{addr}/holdings` and `.../profit_stat/7d` →
  **401 needs login**. Holdings are therefore out of scope (open
  positions can be derived from our own feed instead).

### Token intel

| Need | Endpoint | Key fields |
|---|---|---|
| Info, price, pool, dev (one call) | POST `/api/v1/mutil_window_token_info` body `{"chain":"sol","addresses":[mint]}` → `data[0]` | `symbol`, `name`, `decimals`, `total_supply`, `circulating_supply`, `holder_count`, `liquidity`, `creation_timestamp`, `open_timestamp`, `migrated_timestamp`, `biggest_pool_address`, `pool{exchange, quote_reserve, initial_liquidity, creator, …}`, `price{price, price_1m/5m/1h/6h/24h, buys_*, sells_*, volume_*, swaps_*, hot_level}`, `dev{creator_address, creator_token_balance, creator_token_status, creator_open_count, fund_from, cto_flag, twitter_rename_count, …}`, `launchpad` fields |
| Security | `/api/v1/token_security_sol/sol/{mint}` | `renounced_mint`, `renounced_freeze_account`, `top_10_holder_rate`, `burn_ratio`, `burn_status`, `dev_token_burn_ratio`, `buy_tax`, `sell_tax`, `is_show_alert`, `lock_summary{is_locked, lock_percent}` |
| Creator address, launchpad, bonding | POST `/mrwapi/v1/multi_token_info` body `{"chain":"sol","addresses":[mint]}` → `data[0]` | **`creator_address`** (present even when `token_dev_info` blanks it, e.g. CTO tokens), `launchpad`, `launchpad_platform`, `launchpad_status`, `launchpad_progress`, `migration_market_cap`, `migrated_timestamp`, `holder_count`, `liquidity`, `total_supply`, `ath_price` |
| Dev / creator | `/api/v1/token_dev_info/sol/{mint}` (`creator_address` is `""` for PUDU, a CTO token) | `creator_address`, `creator_token_balance`, `creator_token_status` (e.g. `creator_close`, `creator_hold`), `creator_open_count`, `fund_from`, `fund_from_ts`, dexscreener flags, twitter rename/delete counts |
| Creator history | `/api/v1/dev_created_tokens/sol/{creator}` | `inner_count` (never migrated), `open_count` (migrated), `open_ratio`, `last_create_timestamp`, `creator_ath_info{ath_token, ath_mc, token_symbol}`, `tokens[]` (≈100: `token_address`, `symbol`, `create_timestamp`, `is_open`, `market_cap`, `token_ath_mc`, `holders`, `launchpad_platform`) |
| Holder composition (rates) | `/api/v1/token_stat/sol/{mint}` | `holder_count`, `top_10_holder_rate`, `creator_hold_rate`, `dev_team_hold_rate`, `top70_sniper_hold_rate`, `fresh_wallet_rate`, `bot_degen_rate`, `top_bundler_trader_percentage`, `top_rat_trader_percentage` (insider), `top_entrapment_trader_percentage`, `bluechip_owner_percentage`, `creator_created_count` |
| Holder composition (counts) | `/vas/api/v1/token_holder_stat/sol/{mint}` | `smart_degen_count`, `renowned_count` (KOL), `sniper_count`, `bundler_count`, `insider_count`, `dev_count`, `fresh_wallet_count`, `dex_bot_count`, `bluechip_owner_count`, `following_count` |
| Trader composition (counts) | `/vas/api/v1/token_trader_stat/sol/{mint}` | same keys, over everyone who traded |
| Bundlers | `/api/v1/token_bundler_stat/sol/{mint}` | `bundler_count`, `bundler_hold_ratio`, `bundler_swap_ratio`, … |
| Launchpad / bonding | `/api/v1/token_launchpad_info/sol/{mint}` | `launchpad`, `launchpad_platform`, `launchpad_status`, `launchpad_progress`, `migrated_pool_exchange` |
| Smart / KOL wallets that traded it | `/vas/api/v1/token_traders/sol/{mint}?limit=50&orderby=profit&direction=desc&tag={renowned|smart_degen}` | per wallet: `address`, `tags`, `buy_volume_cur`, `sell_volume_cur`, `buy_tx_count_cur`, `sell_tx_count_cur`, `amount_percentage`, `usd_value`, `profit`, `realized_profit`, `unrealized_profit`, `avg_cost`, `avg_sold`, `start_holding_at`, `end_holding_at`, `last_active_timestamp`. **The `tag` filter works here** (renowned → 2 KOLs, smart_degen → 7 smart wallets) |
| Holders by tag | `/vas/api/v1/token_holders/sol/{mint}?limit=50&orderby=amount_percentage&direction=desc&tag=…` | same row shape, current holders |
| Top / first buyers | `/api/v1/tokens/top_buyers/sol/{mint}` | 70 first buyers with `status` (hold/sold/…) and `maker_token_tags` (`sniper`, …) |
| Rug history of holders | `/api/v1/tokens/rug_history/sol/{mint}` | `rug_ratio`, `holder_rugged_num`, `history[]` |
| Trades | `/vas/api/v1/token_trades/sol/{mint}?limit=50` | `maker`, `event`, `amount_usd`, `price_usd`, `maker_tags`, `maker_token_tags`, `maker_twitter_username`. **`tag=` is ignored** (unfiltered list), so it is not used for "who traded" |

Not used: `/defi/quotation/v1/tokens/sol/{mint}` (40000300 invalid
argument — retired), `/defi/quotation/v1/tokens/top_holders/sol/{mint}`
(404). POST `/api/v1/token_info_brief` and POST
`/mrwapi/v1/multi_token_info` also work (smaller info sets).

### Market lists

| List | Endpoint |
|---|---|
| Trending | `/defi/quotation/v1/rank/sol/swaps/{1m|5m|1h|6h|24h}?orderby=swaps&direction=desc&limit=…` (`filters[]=renounced&filters[]=frozen` accepted) — rows with price, volume, liquidity, market_cap, holder_count, top_10_holder_rate, creator, launchpad, `smart_degen_count`, `renowned_count`, `sniper_count`, `rug_ratio` |
| New pairs | `/api/v1/pairs/sol/new_pairs/{1m|5m|1h|6h|24h}?limit=…&orderby=open_timestamp&direction=desc` → `pairs[]` with `base_address`, `launchpad`, `launchpad_platform`, `open_timestamp`, `base_token_info{price, market_cap, liquidity, holder_count?, progress, creator, renounced_*}` |
| pump.fun new / completing / completed | POST `/vas/api/v1/rank/sol` body `{"new_creation":{"limit":N,"launchpad_platform_v2":true,"launchpad_platform":["Pump.fun"]},"near_completion":{…same…},"completed":{…same…},"version":"v2"}` → `data.new_creation[]`, `data.pump[]` (completing, `progress` 0.92–0.98), `data.completed[]` (with `complete_timestamp`, `complete_cost_time`). Without `launchpad_platform` all three lists are empty |

`/defi/quotation/v1/rank/sol/pump_ranks/1h` returns only
`new_creations` (completing/completed always empty);
`/defi/quotation/v1/rank/sol/pump/1h` returns an empty rank. Not used.

## Rate and latency

| Test | Result |
|---|---|
| 60 × `wallet_activity`, 1 req/s | 60 × 200 JSON; p50 0.25 s, p95 0.79 s |
| 90 × `wallet_activity`, 2 req/s | 90 × 200 JSON; p50 0.25 s, p95 0.37 s |
| Whole phase 0 (~230 req) | no 429, no challenge; one timeout (dead `walletstat` endpoint) |

No rate-limit headers are sent (`server: cloudflare`,
`cf-cache-status: DYNAMIC`). The limit is unknown above 2 req/s; gzetryn
runs at **≤ 40 req/min sustained `[TUNABLE]`**, a third of the tested
rate, and backs off on the first 429/403.

## Consequences for the design

1. Directory: 4 rank lists per refresh (`kol` and `smart_degen` × `7d`
   and `30d`, ordered by PnL = realized USD profit), 100 rows each.
   "Top 50 by 30d PnL" means top by `realized_profit_30d`.
2. Feed: `wallet_activity` per watched wallet, `type=buy&type=sell`,
   dedup by `(wallet, tx_hash, mint, side)`, mcap from
   `price_usd × total_supply`, `cursor` paging when a poll's whole page is
   new.
3. Token intel: `mutil_window_token_info` + security + dev info + dev
   created tokens + token_stat + holder_stat + launchpad info + two
   `token_traders` calls (renowned, smart_degen) + our own feed.
4. Holdings and anything under `/pf/` are out of scope (login).
5. Manual wallets outside the ranks get their metrics from
   `walletNew?period=30d` (win rate unknown) and identity from
   `wallet_common_stat`; `wallet_stat/{period}` is not used for metrics
   (zeros without login).
6. Token creator comes from `multi_token_info.creator_address`, which
   also gives launchpad and bonding progress (replaces
   `token_launchpad_info` in the token call).

## Still open

- The real throttle threshold (above 2 req/s) — not probed on purpose;
  the service measures and reports 429/403 counts in `/v1/stats`.
- Trade lag (trade time → first seen) is measured in production per event
  (`lag_sec`).
