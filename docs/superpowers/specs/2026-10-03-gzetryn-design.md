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
- Token intel: `mutil_window_token_info` (POST), `token_security_sol`,
  `token_dev_info`, `dev_created_tokens`, `token_stat`,
  `token_holder_stat`, `token_trader_stat`, `token_launchpad_info`,
  `token_traders?tag=renowned|smart_degen`.
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
| Rate | Sustained ≤ 40 req/min `[TUNABLE]`, burst 8, ≥ 0.3 s between request starts; P0 (API) > P1 (feed polling) > P2 (rank, stats) |
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
   `curation.max_trades_per_day` = 150.

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
  from `wallet_stat/sol/{addr}/30d` and identity from
  `wallet_common_stat` (P2), `metrics_source = wallet_stat`.
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
  `[TUNABLE]`: **hot** (trade in the last 30 min) 60 s, **warm** (last
  24 h) 180 s, **cold** 600 s. Due wallets are polled oldest-due first, at
  most 2 at a time, priority P1, with jitter so polls do not bunch.
- Expected load for 50–60 active wallets: ~15–25 req/min, inside the
  40 req/min budget with room for API calls.
- A poll = `wallet_activity` with `limit = 20`, `type=buy&type=sell`. When
  every row of the page is new **and** the oldest row is newer than the
  wallet's last stored trade, the next page is fetched (`cursor`), up to
  `watch.max_pages` = 3, so a burst of trades between polls is not lost.

### Events

- Identity: `(wallet, tx_hash, mint, side, token_amount)`; inserts are
  idempotent, so overlapping pages never duplicate.
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
| `launchpad` (platform, status, bonding progress, migrated exchange) | `token_launchpad_info` | 30 s |
| `security` (mint/freeze authority renounced, top 10 rate, burn, taxes, lock, alert) | `token_security_sol` | 120 s |
| `dev` (creator, balance, status hold/close/sell, fund source, twitter renames, dexscreener flags) | `token_dev_info` | 60 s |
| `dev_history` (tokens created, migrated vs never migrated, open ratio, ATH token, recent tokens) | `dev_created_tokens/{creator}` | 600 s |
| `holders` (rates: top 10, creator, dev team, snipers, fresh, bots, bundlers, insiders, bluechip; counts by tag among holders and among traders) | `token_stat`, `token_holder_stat`, `token_trader_stat` | 45 s |
| `smart_traders` (KOL and smart wallets that traded it: buy/sell USD and counts, holding %, profit, first/last time; joined with our directory names) | `token_traders?tag=renowned`, `?tag=smart_degen` | 45 s |
| `feed` (our own events for the mint, newest first, plus a per-wallet summary) | none | live |

Default `parts` = all (≤ 10 GMGN calls on a cold cache, P0). A failing
part is returned as `errors.{part}` while the rest is served; the call
fails (503) only when every GMGN part fails. `max_age_sec` lowers the
accepted cache age.

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
| `samples` | raw answers on unknown errors (7 days) |

Retention `[TUNABLE]`: `rank_snapshots` 180 days, `request_log` 30 days,
`samples` 7 days; trades, wallets and curation runs are kept. Volume:
~400 snapshot rows/h, trades a few thousand per day.

## 10. Budget, pacing and errors

- **Budget**: token bucket, `budget.per_minute` = 40, capacity
  `budget.burst` = 8, and `budget.min_gap_sec` = 0.3 between request
  starts (all `[TUNABLE]`). P0 may take the last token, P1 leaves
  `reserve_p1` = 2, P2 leaves `reserve_p2` = 4. Waits are bounded per
  priority (P0 10 s, P1 60 s, P2 120 s); a P0 call that cannot be served
  returns cached data with `stale: true` or `503` + `Retry-After`.
- **Classification** (phase 0 codes):

| Answer | Action |
|---|---|
| 200 + `code 0` | ok, cached |
| 429, or 403 Cloudflare HTML | throttled: whole IP paused 120 s, doubling to 30 min, reset after 30 min clean; counted; health degraded |
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
| `GET /v1/wallets/{address}/stats?period=7d\|30d` | live GMGN wallet stat (cached 300 s) |
| `POST /v1/wallets` `{address, label?, tags?, note?}` | add/update a manual wallet (201 new, 200 existing) |
| `PATCH /v1/wallets/{address}` `{label?, tags?, note?}` | edit a manual wallet |
| `DELETE /v1/wallets/{address}` | clear the manual flag (404 if not manual); never deletes data |
| `GET /v1/feed?...` | section 6 |
| `GET /v1/token/{mint}?parts=&max_age_sec=` | section 7 |
| `GET /v1/leaderboard?...`, `GET /v1/leaderboard/copy` | section 8 |
| `GET /v1/curation`, `GET /v1/curation/runs?limit=` | current rule, latest run, history |
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
  loses the excess (3 pages × 20). Hot wallets are polled every 60 s; the
  bot-pace filter keeps such wallets out of the curated set.
- **Predictive value**: unmeasured; the engine measures it from the feed
  before any of it trades.
