# gzetryn — design spec

- Date: 2026-10-03
- Status: approved by the owner (2026-10-03: "build it fully now"; curated
  list rule "recommended" chosen the same day)
- Location: `zetryn/gzetryn/`
- Facts: `../plans/phase-0-report.md` (every GMGN endpoint used here was
  verified live from the VPS on 2026-10-03)
- Plans: one file per phase under `docs/superpowers/plans/`

## 1. Purpose

gzetryn is a self-hosted GMGN (`gmgn.ai`) web-data service for the
ZetrynAI Solana memecoin engine, built the same way as xscout (X) and
bscout (Binance): a loopback HTTP service that is the **only** process on
the VPS talking to GMGN, with one shared request budget, a cache, a
database and a feed.

It closes these data gaps for the engine:

1. **KOL and smart-money directory, automatic.** GMGN's KOL and smart
   money ranks are refreshed on a schedule and kept as history; a curated
   list of wallets worth following is rebuilt daily. KOL wallets are never
   typed in by hand again (manual additions remain possible).
2. **Watch and feed.** The trades (buys/sells) of every active wallet are
   polled and appended to an ordered feed the engine reads with a cursor.
3. **Token intel.** One call returns security, dev/creator and their
   history, holder composition by tag, and which smart/KOL wallets traded
   the token (GMGN's view plus our own feed).
4. **Leaderboards for copy-trading research.** Best realized PnL this
   month (30d) and this week (7d), KOL board, history, and a "who to copy"
   view that joins profit, win rate and consistency.
5. **Management.** CLI and API to add, label and remove manual wallets,
   list curated ones, and see stats and health.

Consumers: the ZetrynAI engine (`X-Consumer: zetryn`) first; the
dashboard's KOL Tracking page can read the same API.

### Non-goals

- Trading or any write action on GMGN; GMGN login; anything under
  `/pf/api/...` (needs login) or wallet holdings (Cloudflare 403).
- Paid APIs or proxies. One VPS IP, a polite rate.
- Judging whether following these wallets is profitable: gzetryn records
  point-in-time data, the engine measures it.
- Access from other machines (loopback only).

## 2. Verified facts (summary)

From `phase-0-report.md`:

- All JSON endpoints answer without login or cookies from the VPS with a
  Chrome TLS fingerprint and `Referer: https://gmgn.ai/`; envelope
  `{code, msg|message, data}`, `code 0` = ok.
- Wallet rank: `/defi/quotation/v1/rank/sol/wallets/{7d|30d}?tag=&orderby=pnl_{p}&direction=desc`,
  100 rows max; GMGN's "PnL" order is **realized USD profit**.
- Wallet trades: `/vas/api/v1/wallet_activity/sol?wallet=&limit=&type=buy&type=sell&cursor=`,
  near real-time (newest buy 30 s old for an active wallet).
- Token intel: `mutil_window_token_info` (POST), `multi_token_info`
  (POST, creator + launchpad), `token_security_sol`, `token_dev_info`,
  `dev_created_tokens`, `token_stat`, `token_holder_stat`,
  `token_trader_stat`, `token_traders?tag=renowned|smart_degen`.
- Wallet metrics without a rank row: `walletNew?period=30d` (real
  profit/PnL/buys/sells, win rate null); `wallet_stat/{period}` returns
  zeros without login.
- Market: swaps rank (trending), `new_pairs`, POST `/vas/api/v1/rank/sol`
  (pump.fun new / completing / completed).
- Rate: 150 requests at 1–2 req/s, all 200, p50 0.25 s; no rate-limit
  headers. Above 2 req/s is unmeasured.

## 3. Decisions

| Topic | Decision |
|---|---|
| Method | Copy bscout's architecture: transport → gateway (cache, coalescing, budget, classification) → jobs/store → API. No accounts, sessions or encryption (no login) |
| Stack | Python 3.14, uv, curl_cffi (`impersonate="chrome"`), FastAPI + uvicorn, SQLAlchemy 2 async + asyncpg + Alembic, click, pydantic-settings + YAML, JSON logs to journald |
| Storage | PostgreSQL 16 on the VPS, port 5433, peer auth, one role + one database `gzetryn`. DB tests run in the schema `gzetryn_test` of that database (no second database) |
| API | `127.0.0.1:8793`, `X-Consumer` header on every request (including `/health`), bscout-style envelope |
| Rate | Cap 60 req/min `[TUNABLE]` (expected use 20–30/min), burst 15, ≥ 0.3 s between request starts; P0 (API) > P1 (feed polling) > P2 (rank, stats) |
| Curated list | Owner's rule (section 5) with every threshold in config |
| Deploy | rsync of the clean git tree to `/opt/gzetryn` (no GitHub remote yet), venv `/var/lib/gzetryn/venv`, user `gzetryn`, `gzetryn.service` |

## 4. Architecture

```
 engine (zetryn) ─┐                                       ┌─► gmgn.ai JSON endpoints
 dashboard       ─┼─HTTP─► api ──► gateway ──► transport ──┘   (curl_cffi, chrome)
 gzetryn CLI     ─┘ :8793   │  ▲     │  │
                            │  │     │  └─ budget (token bucket, min gap, throttle pause)
                            ▼  │     └──── cache (TTL per endpoint, stale on failure)
          jobs: directory (rank → snapshots → curation), watcher (wallet trades → feed),
                token intel, retention, counters
                            │
                            ▼
                     store (Postgres `gzetryn`)          gmgn (endpoints + tolerant parsers)
```

| Component | Responsibility |
|---|---|
| **gmgn** | The only code that knows GMGN's wire format: endpoint registry (method, path template, fixed params, cache class) and tolerant parsers into plain gzetryn records |
| **transport** | One curl_cffi `AsyncSession` (Chrome impersonation, `Referer`, `Accept`), GET/POST, returns status, parsed JSON, size, latency or a network error |
| **gateway** | The only path to GMGN: cache lookup, coalescing of identical in-flight calls, budget by priority, classification (section 10), throttle pause, stale serving, per-endpoint counters and latency, samples of unknown answers |
| **directory job** | Rank refresh (4 lists), snapshots, wallet upserts, daily curation, metrics refresh for manual wallets |
| **watcher job** | Adaptive round-robin over active wallets; trades → feed with seq; baseline handling; paging on gaps |
| **token intel** | Assembles `/v1/token/{mint}` from up to 10 cached GMGN calls plus our feed; partial failures are reported per part |
| **store** | Repositories, Alembic migrations, ordered feed writes |
| **api** | REST endpoints; reads the store or goes through the gateway; never calls GMGN itself |
| **cli** | `serve`, `migrate`, `wallets add/remove/label/list`, `curate`, `refresh`, `fetch`, `stats`, `openapi` |

## 5. Directory and curation

### Rank refresh

- Every `rank.interval_sec` = 3600 `[TUNABLE]`, four lists: tags
  `kol`, `smart_degen` × periods `7d`, `30d`, `orderby=pnl_{period}`
  (= realized profit), 100 rows each, P2. The tag and period lists are
  configuration.
- Each fetch stores a **rank snapshot** (one row per wallet per list) and
  upserts the wallet: identity (name, twitter handle/name, avatar, GMGN
  tags), metrics (7d and 30d realized profit, PnL ratio, win rate, buys,
  sells, trades, average holding period, SOL balance, follow count,
  `daily_profit_7d`, last active) with `metrics_at` and
  `metrics_source = rank`.
- Wallets are never deleted. A wallet missing from every list keeps its
  last metrics and `last_ranked_at`.

### Curated list (owner decision 2026-10-03, "recommended")

Candidates: every wallet in the latest snapshot of any configured list.
A candidate passes when **all** hold (`[TUNABLE]` values in config):

1. tag `kol` or `smart_degen` (from the list or the row's tags);
2. `realized_profit_30d` > `curation.min_profit_30d_usd` = 0 and
   `pnl_30d` > `curation.min_pnl_30d` = 0;
3. `winrate_30d` ≥ `curation.min_winrate_30d` = 0.50;
4. not bot-paced: `(buy_30d + sell_30d) / 30` ≤
   `curation.max_trades_per_day` = 150;
5. enough evidence: `buy_30d + sell_30d` ≥ `curation.min_trades_30d` = 20
   (owner decision 2026-10-03, after the first run curated a wallet with a
   1.00 win rate on ~12 trades; `min_winrate_30d` stays 0.50).

Passing wallets are ordered by `realized_profit_30d` (GMGN's 30d PnL)
descending; the first `curation.top_n` = 50 are **curated**.

- Re-curated every `curation.interval_sec` = 86400 `[TUNABLE]`, after a
  rank refresh, and at start-up when the last run is older than that.
- A wallet that drops out is **deactivated** (`curated = false`,
  `uncurated_at` set), never deleted; its trades and history stay.
- Manual wallets are never removed by curation (manual and curated are
  independent flags; a wallet can be both).
- Every run is stored (`curation_runs`: parameters, candidate/pass
  counts, selected addresses with rank and metrics, added, removed).
- A run that finds fewer than `curation.min_candidates` = 50 candidates
  (rank fetch failed) changes nothing and is logged as skipped.

### Manual wallets

- Added by CLI or `POST /v1/wallets` with an optional label, tags (free
  strings, e.g. `insider`, `friend`) and note; `added_by` = consumer.
- On add and every `directory.manual_metrics_sec` = 21600 `[TUNABLE]`, a
  manual wallet that is not in the latest rank snapshot gets its metrics
  from `smartmoney/sol/walletNew/{addr}?period=30d` and identity from
  `wallet_common_stat` (P2), `metrics_source = wallet_new`. Its 30d win
  rate stays null (GMGN does not expose it without login; anonymous
  `wallet_stat/{period}` returns zeros — phase 0).
- Remove (`DELETE` / `wallets remove`) clears the manual flag only.

### Wallet status (derived)

`curated`, `manual`, `ranked` (in the latest snapshot, not curated),
`inactive` (none of these). **Active** = curated or manual: the watcher
polls exactly the active set.

## 6. Watch and feed

### Polling

- The active set is reloaded from the store every 60 s (picks up
  curation and CLI changes).
- Each wallet has a due time. Interval by its latest known trade
  `[TUNABLE]`: **hot** (trade in the last 30 min) 45 s, **warm** (last
  24 h) 90 s, **cold** 300 s. Due wallets are polled oldest-due first, at
  most 2 at a time, priority P1, with jitter so polls do not bunch. (The
  first soak ran 60/180/600 s: 10.8 req/min, live lag p50 122 s, p90
  161 s; tightened the same day.)
- Expected load for 38–60 active wallets: ~20–30 req/min, inside the
  60 req/min cap with room for API calls.
- A poll = `wallet_activity` with `limit = 20`, `type=buy&type=sell`. When
  every row of the page is new **and** the oldest row is newer than the
  wallet's last stored trade, the next page is fetched (`cursor`), up to
  `watch.max_pages` = 3, so a burst of trades between polls is not lost.

### 6.1 On-chain trigger (owner decision 2026-10-03: "real-time, at most a few seconds")

Faster GMGN interval polling is rejected (38 wallets every ~5 s ≈ 450
req/min, ~4× the rate proven clean; a Cloudflare block of the VPS IP would
stop everything). Instead a free Solana WebSocket says *when* a wallet
transacted, and GMGN is polled only then.

- Endpoint: `wss://api.mainnet-beta.solana.com` (free, no key), verified
  from the VPS (phase 7 report): 43 subscriptions on one connection, 0
  disconnects in 10 min, notifications 1.1–1.7 s after the whole-second
  block time, every GMGN trade in the window notified. `confirmed`
  commitment (+0.07 s vs `processed`, no rollback risk). Not used: the
  engine's Alchemy (≈ 83 % of its free quota), Helius WS (metered).
- One connection, one `logsSubscribe {mentions: [wallet]}` per **active**
  wallet, kept in sync with the active set (subscribe/unsubscribe on
  reload); reconnect with backoff 1 → 60 s and resubscribe everything.
- A notification with `err` ≠ null is dropped (99 % of the traffic: bot
  wallets spamming failed transactions). A successful one is **swap-like**
  when its logs show a token `Transfer`/`TransferChecked` plus an
  instruction containing buy/sell/swap/route/exact-in/out (or a Raydium
  `ray_log`). Others (account setup, SOL transfers, fee claims, bot
  programs without token transfers) are skipped but remembered, so a later
  trade with that signature is counted as a classifier miss.
- Swap-like for wallet W → GMGN `wallet_activity` poll of W after
  `trigger.debounce_sec` = 1 s; if the signature is not in GMGN's page yet,
  retry after 2 and 4 s, giving up after `max_wait_sec` = 10 s (was 2, 4,
  8, 16 s / 30 s until 2026-10-05: 95.4 % of trigger events had lag ≤ 5 s,
  while the late retries were mostly wasted on 1,453 timed-out triggers and
  doubled bursts on the challenged `/vas/` group). One
  poll serves every pending signature of the wallet. Per wallet ≥
  `min_gap_sec` = 2 s between triggered polls and ≤ `max_polls_per_min` =
  10; above the cap the trade is left to fallback polling.
- GMGN remains the only source of events (symbol, USD, price). No event is
  created from the WebSocket, so there is never a second event for a
  trade. Decoding swaps from the transaction itself was not built: it needs
  a `getTransaction` per notification on the rate-limited public HTTP RPC
  and would duplicate GMGN's enrichment for a gain of ~1–2 s.
- Interval polling stays as the fallback: while the trigger is healthy
  (connected, every active wallet subscribed) hot/warm/cold run at
  120/300/900 s; when it is not, at the normal 45/90/300 s (due times are
  pulled in at once when the trigger goes down).
- Each event's payload records `source` (`trigger` | `interval`) and, when
  the WebSocket saw the signature, `notified` (`swap` | `filtered`) and
  `notified_at`. `/v1/stats` and `/health` expose connection state,
  reconnects, notification counts, trigger polls, hits, time from
  notification to GMGN index, timeouts, caps, coverage (notified / filtered
  / not notified) and live lag p50/p90 per source.

### 6.2 Chain fallback (2026-10-05)

Measured: once Cloudflare challenges GMGN's `/vas/` group (wallet_activity),
it stays challenged for tens of minutes regardless of our rate (still
blocked after 17 min without a single request; 06:54 → past 07:50 UTC with
two brief openings; throttled at 1 request in 10 s / 2 in 60 s). During
such a block the GMGN-based feed cannot deliver.

- While `/vas/` is not open (cooling or awaiting its probe), or when a
  triggered GMGN poll fails, a swap-like notification is decoded from the
  transaction itself: `getTransaction` (jsonParsed,
  `maxSupportedTransactionVersion: 1` — version 1 transactions are live) on
  the free public RPC `https://api.mainnet-beta.solana.com`, paced ≥ 2 s
  apart (it returned 429 after ~16 calls in 8 s; 1 per 2.5 s ran clean;
  latency p50 0.06 s), 30 s back-off on 429, 3 tries while the RPC has not
  indexed the transaction, dropped after 120 s.
- Decoding (pure, `trigger/chain.py`): the wallet's token balance deltas
  by mint (owner = wallet) and its SOL + wSOL delta with the network fee
  removed. Exactly one non-SOL mint with the opposite SOL leg = a trade
  (buy: tokens up, SOL down); anything else (transfers, token↔token
  routes) is left to GMGN. Verified on 10 GMGN events: side and token
  amount equal to rounding, SOL amount 0.4–2.2 % off (the chain value is
  what the wallet really paid/received, fees and tips included).
- Enrichment without `/vas/`: symbol and total supply from
  `mutil_window_token_info` (`/api/`, cached); SOL/USD = median of
  `usd_amount / sol_amount` over GMGN trades in the feed (30 min, else 24 h);
  `price_sol = sol / tokens`, `price_usd`, `usd_amount`, `mcap_usd` derived.
  `open_or_close` and launchpad are null.
- Event `payload.source = chain` (+ `sol_usd`, `sol_includes_fees`,
  `decode_delay_sec`). Identity for de-duplication is `(wallet, tx_hash,
  mint, side)`: when `/vas/` reopens, GMGN's row for the same trade is
  recognised and not stored a second time.
- Cost: one RPC call per swap-like notification while `/vas/` is down
  (≈ 1.3/min on average over 49 h); no GMGN `/vas/` call.

### Events

- Identity: `(wallet, tx_hash, mint, side)` for de-duplication before
  insert (since 2026-10-05, so a chain-decoded event and GMGN's rounded row
  stay one event); the table's unique key also includes `token_amount`.
  Inserts are idempotent, so overlapping pages never duplicate.
- Each event gets a monotonic `seq` (bigserial). Inserting transactions
  hold one advisory lock, so `seq` values **commit in order**; a reader
  following the cursor never skips an event.
- Fields: `seq`, `trade_at` (GMGN block time), `seen_at` (our poll),
  `lag_sec` = seen − trade, `wallet`, wallet name/twitter/GMGN tags/user
  label and tags and status **frozen at event time**, `side`
  (`buy|sell`), `mint`, `symbol`, `token_amount`, `sol_amount`
  (`quote_amount` when the quote is wSOL, else null), `quote_symbol`,
  `usd_amount` (`cost_usd`), `price_usd`, `price_sol` (`price` when quote
  is wSOL), `mcap_usd` = `price_usd × total_supply` (liquidity at trade
  time is not exposed by GMGN: null), `open_or_close` (GMGN's
  `is_open_or_close` flag), `launchpad`, `launchpad_platform`, `tx_hash`,
  `baseline`, `payload` (fees, `buy_cost_usd`).
- **Baseline**: a trade older than the moment the wallet became active
  (`watch_started_at`, reset when an inactive wallet becomes active again)
  is stored with `baseline = true`. It is history, not a signal; the feed
  hides baseline events unless asked.
- A gap in polling (errors, throttling) is visible in the wallet's
  `last_poll_ok_at`; paging and idempotent inserts recover the trades on
  the next ok poll, with a larger `lag_sec`.

### Feed API

`GET /v1/feed?after=<seq>&limit=&wait=<sec>&wallet=&mint=&side=&tag=&baseline=false`:
events with `seq > after`, ascending. `wait` (≤ 30) long-polls the
database. `next_cursor` moves past events filtered out (highest seq that
existed when the page was read, or the last row when the page is full).
`since` is accepted as an alias of `after` (bscout/xscout style).

## 7. Token intel

`GET /v1/token/{mint}?parts=&max_age_sec=` assembles, from cached GMGN
calls (TTL per call `[TUNABLE]`):

| Part | GMGN calls | TTL |
|---|---|---|
| `info` (symbol, supply, pool, price + changes, volume/swaps by window, mcap, liquidity, creation/open/migration times) | POST `mutil_window_token_info` | 30 s |
| `launchpad` (creator address, platform, status, bonding progress, migration mcap, ATH price) | POST `mrwapi/v1/multi_token_info` (its `creator_address` is filled even when `token_dev_info` blanks it) | 60 s |
| `security` (mint/freeze authority renounced, top 10 rate, burn, taxes, lock, alert) | `token_security_sol` | 600 s |
| `dev` (creator, balance, status hold/close/sell, fund source, twitter renames, dexscreener flags) | `token_dev_info` | 300 s |
| `dev_history` (tokens created, migrated vs never migrated, open ratio, ATH token, recent tokens) | `dev_created_tokens/{creator}` | 1800 s |
| `holders` (rates: top 10, creator, dev team, snipers, fresh, bots, bundlers, insiders, bluechip; counts by tag among holders and among traders) | `token_stat`, `token_holder_stat`, `token_trader_stat` | 120 s |
| `smart_traders` (KOL and smart wallets that traded it: buy/sell USD and counts, holding %, profit, first/last time; joined with our directory names) | `token_traders?tag=renowned`, `?tag=smart_degen` | 120 s |
| `feed` (our own events for the mint, newest first, plus a per-wallet summary) | none | live |

TTLs raised 2026-10-05 for the engine's enricher (≈ 200–300 Migration
survivors/day, repeated calls within minutes served from the cache).
Expected extra load with `parts=security,launchpad,dev,dev_history,holders,smart_traders`:
9 GMGN calls per cold token (4 on `/vas/`) → ≈ 2,250–2,700 calls/day ≈
**1.6–1.9 req/min** on average (≈ 0.7–0.8/min on `/vas/`), paced per group.

Default `parts` = all (≤ 10 GMGN calls on a cold cache, P1 — below the
trigger's P0). A failing
part is returned as `errors.{part}` while the rest is served; the call
fails (503) only when every GMGN part fails. `max_age_sec` lowers the
accepted cache age.

### 7.1 Market candidates (owner-approved 2026-10-05: engine candidate source)

A background job (P3) polls three GMGN lists into the `candidates` table;
the engine reads it with a cursor, so its polling costs no GMGN calls and
never returns a candidate twice per kind.

| Kind | GMGN source (verified 2026-10-05) | Every `[TUNABLE]` |
|---|---|---|
| `new` | pump.fun `new_creation` (POST `/vas/api/v1/rank/sol`, 50) and every row of `/api/v1/pairs/sol/new_pairs/1m` (50) | 30 s |
| `completing` | pump.fun `pump` list (bonding progress ≈ 0.9–1.0) | 30 s |
| `migrated` | pump.fun `completed` list only (pool = AMM pool, `exchange` pump_amm, `complete_timestamp`). New-pair rows cannot be used: a new pump_amm pool is also how direct PumpSwap launches look, and their `creation_timestamp` equals the pool open time even for real migrations (verified 2026-10-05) | 30 s |
| `trending` | `/defi/quotation/v1/rank/sol/swaps/1h` (50) | 60 s |

- One row per (kind, mint); `seq` is assigned at the first sighting only.
  Later sightings update `last` (metrics), `last_seen_at`, `seen_count`,
  and fill a missing pool or completion time.
- Pool address: pump rows carry `pool_address` (bonding curve while
  new/completing, AMM pool after migration); new-pair rows carry it as
  `address`; **trending rows have none** — the job resolves it for new
  trending mints with POST `mutil_window_token_info` (≤ 5 mints per call;
  20 is refused with `40000300`), matched by address, before storing.
- Normalized metrics, frozen at the first sighting in `first` and updated
  in `last`: price_usd, liquidity_usd, mcap_usd (GMGN market cap = price ×
  total supply = FDV), holders, volume_1h_usd, buys_1h, sells_1h, swaps_1h,
  smart_degen_count, renowned_count (KOL), sniper_count,
  top_10_holder_rate, progress (bonding curve). Fields a source does not
  carry are null (new pairs: no buy/sell counts or tag counts).
- Load: pump lists 2/min (`/vas/`), new pairs 2/min (`/api/`), trending
  1/min (`/defi/`), pool resolution ≤ 4 calls/min (`/api/`). Retention 7
  days after the last sighting.
- `GET /v1/market/candidates?kind=&after=&limit=&wait=` (section 11).

## 8. Leaderboards and "who to copy"

- `GET /v1/leaderboard?period=30d|7d&tag=kol|smart_degen|all&sort=profit|pnl|winrate&limit=`:
  the latest snapshot of the matching list(s), one row per wallet, with
  realized profit, PnL ratio, win rate, trades/day, holding period,
  curated/manual flags, and the rank and profit of the snapshot about
  24 h earlier (`prev_rank`, `profit_change`).
- `GET /v1/wallets/{address}/history?period=&days=`: the wallet's
  snapshot series (rank, profit, PnL, win rate per refresh).
- `GET /v1/leaderboard/copy?limit=`: research view over the latest
  snapshot of all lists, after the curation filters 2–4 (section 5).
  Score `[TUNABLE]` = 0.35 × percentile of 30d realized profit + 0.30 ×
  30d win rate + 0.20 × share of profitable days in `daily_profit_7d` +
  0.15 × presence (share of the last 7 days with a snapshot of the
  wallet). Every component is returned next to the score; it ranks
  research candidates, it is not a trading signal.

## 9. Data model

| Table | Content |
|---|---|
| `wallets` | `address` (PK), identity (name, twitter username/name/fans, avatar, GMGN tags), flags `curated` / `manual`, curated rank and times, manual label/tags/note/`added_by`/`added_at`, latest metrics + `metrics_at`/`metrics_source`, `first_seen_at`, `last_ranked_at`, watch state (`watch_started_at`, `last_poll_at`, `last_poll_ok_at`, `last_poll_error`, `last_trade_at`, `polls_ok`, `polls_failed`) |
| `rank_snapshots` | one row per (refresh, list, wallet): `snapshot_at`, `period`, `tag`, `rank`, 7d/30d realized profit, PnL, win rate, buys/sells/trades, SOL balance |
| `curation_runs` | `at`, parameters, candidates, passed, selected (address, rank, metrics), added, removed, skipped reason |
| `trades` | the feed (section 6) |
| `request_log` | hourly buckets: consumer, endpoint, outcome, count, latency sum |
| `samples` | raw answers on unknown errors and throttles (with the group's window counts) (7 days) |
| `candidates` | market candidates (section 7.1): kind, mint, seq, pool, exchange, launchpad, creator, created/open/complete times, `first`/`last` metrics (7 days after last sighting) |

Retention `[TUNABLE]`: `rank_snapshots` 180 days, `request_log` 30 days,
`samples` 7 days; trades, wallets and curation runs are kept. Volume:
~400 snapshot rows/h, trades a few thousand per day.

## 10. Budget, pacing and errors

Revised 2026-10-05 from measured throttling (phase 8 report). Facts:
GMGN's 429s are Cloudflare challenges (`cf-mitigated: challenge`, "Just a
moment…" HTML) scoped to a **path group**: `/vas/` was challenged for
~7 min while `/api/` and `/defi/` answered 200 from the same IP. They came
~0.6/h at an average of only 5–11 req/min and did not track hourly volume,
so they are set off by short bursts, not by the sustained rate. The old
policy (one 429 → *everything* paused 120 s doubling to 30 min) turned 30
throttles into ~7,800 s of total blackout in 49 h.

- **Groups** = first path segment: `vas` (wallet_activity, token
  holder/trader stats, token_traders, pump lists), `api` (token stat,
  security, dev info, dev tokens, window info, new pairs), `defi` (wallet
  rank, swaps rank, walletNew), `mrwapi` (multi_token_info). Each has its
  own token bucket and minimum gap `[TUNABLE]`: vas 20/min, burst 4,
  ≥ 1.0 s apart (smooths trigger storms); api 30/min, burst 8, 0.3 s; defi
  and mrwapi 20/min, burst 6, 0.3 s. A global bucket keeps the overall cap
  (60/min, burst 15, ≥ 0.25 s).
- **Per-group cooldown** on a 429/403: 15 s, doubling to 5 min, reset
  after 15 min clean `[TUNABLE]`. After the cooldown exactly one request
  probes the group; it reopens when GMGN answers. Other groups keep
  working. A **global** pause (60 s) happens only when 2+ groups are
  cooling at the same time.
- **Priorities, strict inside a group**: P0 trigger polls (the real-time
  feed), P1 API calls (engine: token enricher, market, stats), P2 interval
  polls, P3 background (ranks, wallet metrics, candidate lists). A lower
  priority never takes a group token while a higher one waits there;
  reserves: P2 leaves 1 token, P3 leaves 2. Wait limits P0 20 s, P1 30 s,
  P2 60 s, P3 120 s; a call that cannot be served returns cached data with
  `stale: true` or `503 {reason: cooldown:<group> | budget | throttled}`
  + `Retry-After`. Trigger polls also have their own concurrency slots.
- **Measuring what GMGN tolerates**: per group, request starts in the last
  10/60/300 s now, their peaks since start, and the counts just before
  each throttle (also written to the throttle sample) — in `/v1/stats`
  `budget.groups`.
- **Classification** (phase 0 codes):

| Answer | Action |
|---|---|
| 200 + `code 0` | ok, cached |
| 429, or 403 Cloudflare HTML (`cf-mitigated: challenge`) | throttled: that group cools (15 s → 5 min), probe afterwards; global 60 s pause only if 2+ groups cool; sample stored with the window counts; health degraded |
| 200 + `code 40000300` / other 4xx JSON | bad parameter → 400 to the caller, no pause |
| 401 `40101611` | needs login → logged as a bug (no endpoint in use needs it) |
| 404 | not found → 404 / part error |
| 5xx, 200 + `code 50001300` | server error, one retry after 2 s |
| network error / timeout (15 s) | one retry after 2 s |
| non-JSON 200 or unknown code | unknown: sample stored, error returned |

- Counters per (consumer, endpoint, outcome) with latency, flushed hourly
  buckets to `request_log` every 60 s; `/v1/stats` shows them since start
  and for the last 24 h, 429/403 counts, the current pause, tokens, and
  the age of the last rank refresh, curation and ok poll.

## 11. HTTP API (v1)

Bind `127.0.0.1:8793`. Every request carries `X-Consumer: <name>`
(lowercase `[a-z0-9_-]{1,32}`; missing → 400). Envelope:
`{"data", "next_cursor", "meta"}`; errors `{"error": {code, message,
retry_after_sec?}}` with 400 / 404 / 409 / 503.

| Endpoint | Purpose |
|---|---|
| `GET /health` | status `ok/degraded/broken`, components (db, gmgn budget/throttle, directory, curation, watcher) |
| `GET /v1/wallets?status=curated\|manual\|active\|ranked\|inactive\|all&tag=&q=&limit=` | directory |
| `GET /v1/wallets/{address}` | one wallet + metrics + watch state + recent trades |
| `GET /v1/wallets/{address}/history?period=&days=` | rank snapshot series |
| `GET /v1/wallets/{address}/stats` | live GMGN `walletNew?period=30d` (7d and 30d profit, PnL, buys, sells; cached 300 s) |
| `POST /v1/wallets` `{address, label?, tags?, note?}` | add/update a manual wallet (201 new, 200 existing) |
| `PATCH /v1/wallets/{address}` `{label?, tags?, note?}` | edit a manual wallet |
| `DELETE /v1/wallets/{address}` | clear the manual flag (404 if not manual); never deletes data |
| `GET /v1/feed?...` | section 6 |
| `GET /v1/token/{mint}?parts=&max_age_sec=` | section 7 |
| `GET /v1/leaderboard?...`, `GET /v1/leaderboard/copy` | section 8 |
| `GET /v1/curation`, `GET /v1/curation/runs?limit=` | current rule, latest run, history |
| `GET /v1/market/candidates?kind=new,completing,migrated,trending&after=<seq>&limit=&wait=` | normalized candidates (section 7.1), cursor, no GMGN call |
| `GET /v1/market/trending?interval=1m\|5m\|1h\|6h\|24h&limit=` | swaps rank (cached 20 s) |
| `GET /v1/market/new-pairs?interval=&limit=` | new pairs (cached 20 s) |
| `GET /v1/market/pump?limit=` | pump.fun new / completing / completed (cached 20 s) |
| `GET /v1/stats` | requests, errors, 429s, latency per endpoint; budget; job ages |
| `POST /v1/admin/refresh`, `POST /v1/admin/curate` | run the rank refresh / curation now (CLI uses these) |

Contract details: `docs/contract.md`; schema: `docs/openapi.json`.

## 12. Configuration

Secrets/endpoints from env (`GZETRYN_DATABASE_URL`, `GZETRYN_PORT`,
`GZETRYN_CONFIG_FILE`, `GZETRYN_LOG_LEVEL`); every number above from
`config/gzetryn.yaml` (optional; defaults in code, example in
`config/gzetryn.example.yaml`). Values marked `[TUNABLE]` are not
measured; the soak report records what was observed.

## 13. Operations

- systemd `gzetryn.service` (user `gzetryn`, `127.0.0.1:8793`,
  `Restart=on-failure`, hardening as bscout), code in `/opt/gzetryn`
  (root-owned, rsynced), venv `/var/lib/gzetryn/venv` (uv, Python 3.14),
  journald JSON logs, `/usr/local/bin/gzetryn` CLI wrapper.
- `deploy/setup-vps.sh` once (user, role/db via peer auth, `.env`),
  `deploy/deploy.sh` for updates (clean tree → rsync → `uv sync --frozen`
  → tests (opt-in) → alembic → restart → health).

## 14. Testing

- Unit (no network, no DB): parsers on the phase 0 fixtures, curation
  rule, copy score, budget and gateway with a fake clock and transport,
  watcher paging/baseline decisions, API validation.
- DB tests: schema `gzetryn_test` inside `gzetryn` (guarded: refuse any
  other schema); feed ordering and cursor, wallet flags, curation
  persistence, API with the test client.
- Run on the VPS only: `nice -n 19 uv run pytest`.

## 15. Phases

| Phase | Scope | Done when |
|---|---|---|
| 0 — Live verification | endpoints, shapes, rate | report written (done) |
| 1 — Foundation | scaffold, config, logging, models, migration, DB access | migration applies on the VPS |
| 2 — GMGN client | endpoints, parsers, transport, budget, cache, gateway | unit tests pass; `gzetryn fetch` works live |
| 3 — Directory | rank refresh, snapshots, curation, manual wallets | curated top 50 stored from live ranks |
| 4 — Watcher + feed | adaptive polling, trades, baseline, paging | live trades in the feed within 10 min |
| 5 — Token intel + API | token endpoint, leaderboards, market lists, stats, contract | all endpoints answer; contract + OpenAPI written |
| 6 — Deploy + verify | setup, deploy, service, soak check | service active, checks in section 13 of the report pass |
| 7 — On-chain trigger | verify free WS, trigger module, watcher integration, stats, contract | end-to-end lag measured; GMGN rate and reconnects reported |
| 8 — Throttling + engine sources | per-group budget/cooldown, strict priority, window measurement; candidates table + endpoint; enricher TTLs | before/after throttles, pause time and lag measured; candidate and enricher fields documented from live rows |

## 16. Risks

- **Terms and blocking**: GMGN may tighten Cloudflare rules or start
  signing requests. Mitigation: low rate, one IP budget, immediate pause
  on 429/403, health degraded; fallback is a `browser` transport (real
  Chrome via CDP) behind the same interface, not built in v1.
- **Endpoint drift**: GMGN changes paths often (two older paths are
  already dead). Parsers are tolerant; unknown answers are sampled;
  `gzetryn fetch` re-verifies an endpoint in one command.
- **Rank composition**: KOL tags are GMGN's labels; curation inherits
  their mistakes. Thresholds are config; every run is stored.
- **Feed completeness**: a wallet with > 60 trades between two polls
  loses the excess (3 pages × 20). The trigger polls within seconds of a
  trade; the bot-pace filter keeps such wallets out of the curated set.
- **Public WebSocket**: free, unauthenticated, no SLA; it may drop or rate
  limit. The watcher falls back to normal interval polling automatically;
  `/health` shows `trigger` degraded. A free alternative can be set with
  `trigger.ws_url` without code changes.
- **Predictive value**: unmeasured; the engine measures it from the feed
  before any of it trades.
