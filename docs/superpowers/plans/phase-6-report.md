# Phase 6 — deploy and verification report (2026-10-03)

## Deploy state

- `gzetryn.service` active on the VPS, `127.0.0.1:8793` (loopback only), user `gzetryn`, code `/opt/gzetryn`
  (rsync of the clean tree, revision in `/opt/gzetryn/REVISION`), venv `/var/lib/gzetryn/venv` (uv, Python
  3.14.7), Postgres 16 port 5433, role + database `gzetryn` (peer auth), migration `0001` applied.
- Test suite on the VPS before each restart (`GZETRYN_DEPLOY_TESTS=1`, `nice -n 19`, schema `gzetryn_test`):
  50 passed (unit + DB + API).
- Nothing else on the VPS was touched (only `gzetryn` unit restarted; one role + one database created).

## Checklist

| # | Check | Result |
|---|---|---|
| 1 | `curl -s -H 'X-Consumer: zetryn' 127.0.0.1:8793/health` | `ok`; db, gmgn, directory, curation, watcher all `ok` |
| 2 | First rank refresh | 05:04:00 UTC: 4 lists × 100 rows = 400 snapshots, 275 distinct wallets, ~0.3 s per list |
| 3 | Curated list | 275 candidates → **38 passed** (rejected: win rate 215, bot-paced 13, profit 9) → 38 curated (fewer than the 50 cap) |
| 4 | Feed | first live (non-baseline) event 27 s after its trade, within 1 min of the curated set going active; 19 live events in the first ~30 min (quiet hour) |
| 5 | `/v1/token/{mint}` on a pump.fun token created 2 s earlier | all 10 parts, `errors: {}`, 3.5 s cold, 0.06 s cached; on a migrated token `smart_traders` listed 2 KOLs + 6 smart wallets joined with our directory |
| 6 | Rate / throttling | ~500 GMGN requests on the day, **0 × 429/403**, 0 failed polls; latency ~0.2–0.5 s per endpoint |

## Curated top 10 (2026-10-03 05:04 UTC, 30d values from GMGN)

| # | Wallet | Name / X | Tag | Realized profit 30d | PnL 30d | Win rate 30d | Trades/day |
|---|---|---|---|---|---|---|---|
| 1 | `4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk` | jijo / @jijo_exe | kol | $95,749 | 0.369 | 0.571 | 93.0 |
| 2 | `D3zAG2Kb6dAWvfuJtxhMun9dT3VurJcFjRDJvVkApZ3d` | – | smart_degen | $68,897 | 0.061 | 0.519 | 94.5 |
| 3 | `4fZFcK8ms3bFMpo1ACzEUz8bH741fQW4zhAMGd5yZMHu` | Rilsio / @CryptoRilsio | kol | $68,149 | 0.383 | 0.516 | 64.9 |
| 4 | `hGPG7jEQLKz6muJJV37hhSRMrNLErRu3K1vk4zhDSbB` | newpairman / @newpairman | smart_degen | $67,800 | 4.554 | 1.000 | 0.4 |
| 5 | `HSeCG7T2KCTuVAARGXZuxBNZg6pas1EdJdjn7Xyy1ENB` | – | smart_degen | $49,275 | 0.211 | 0.562 | 81.1 |
| 6 | `DNfuF1L62WWyW3pNakVkyGGFzVVhj4Yr52jSmdTyeBHm` | gake / @Ga__ke | kol | $39,828 | 0.736 | 0.727 | 2.0 |
| 7 | `ENkZtLj1onzQRGnEeRkQfFzBeeY2dCeiQMXDzh8XbQbj` | Game / @game_for_one | kol | $33,165 | 0.135 | 0.612 | 14.9 |
| 8 | `6FZ1RasKHCPa9DnNYzg2v6ppDBhMigPM9X3L5fUY8kab` | – | smart_degen | $23,813 | 0.037 | 0.523 | 138.8 |
| 9 | `GUjYXh4aR9tDJ1yU1gsa91LtivS5t84CZF3mP5SBkKhz` | – | smart_degen | $22,378 | 0.181 | 0.512 | 29.2 |
| 10 | `8n9tnbCAbnqHhotzibKEtzMpv1eeXCy9LyXKboZgUYB1` | – | smart_degen | $18,703 | 0.258 | 0.542 | 13.6 |

## Tuning done during the deploy (measured, then changed)

1. **Budget** — first deploy 40/min, burst 8, P1 reserve 2: a cold `/v1/token` during the watcher's first round
   waited > 10 s and lost `smart_traders` (`unavailable: budget`, 11.3 s). Now cap 60/min, burst 15, P1 reserve 8,
   P0 wait 20 s → the same call took 3.5 s with every part.
2. **Feed sequence** — re-polled pages burned one `seq` per known row (`ON CONFLICT DO NOTHING`): seq jumped
   200 → 1762. Known rows are now filtered first; seq is dense again (1983 → 1996 consecutive).
3. **Watch intervals** — at 60/180/600 s: 10.8 req/min, live lag p50 122 s / p90 161 s. Now 45/90/300 s:
   19.4 req/min, 0 throttles; the next window's (only two) live trades had lag 8 s / 12 s.

## Measured

| Item | Value |
|---|---|
| Request rate (steady, 38 wallets) | 19.4 req/min (cap 60) |
| 429 / 403 from GMGN | 0 |
| Latency | wallet_activity ~275 ms avg; rank ~310–350 ms; token parts 120–490 ms |
| Failed polls | 0 of ~340 |
| Busiest curated wallets right now | quiet (newest trades 2.6 h, 5.3 h and 2.5 days old per GMGN), so few live events at this hour; nothing missed |

## Open items (each with a proposed fix)

- **Only 38 of the 50 slots are filled** under the owner's rule (the win-rate floor 0.50 rejects 215 of 275
  candidates). Options: (a) keep it (quality over count); (b) lower `curation.min_winrate_30d` to 0.45;
  (c) widen the candidate pool with more GMGN lists (e.g. tag `kol` 1d, or other tags). Owner decision; config only,
  no code change.
- **Low-activity wallets pass the rule** (e.g. #4 newpairman: win rate 1.00 on ~12 trades in 30 days). Proposed:
  add `curation.min_trades_30d` (e.g. 20) — an owner threshold; not added without approval.
- **"Trades/day" is a 30-day average**; several curated wallets are idle for hours or days. The watcher already
  adapts (cold wallets every 5 min). If the engine wants only currently-active traders, filter the feed client-side
  by `wallet` recency or use `/v1/wallets?status=curated` → `watch.last_trade_at`.
- **Manual wallets outside the ranks have no win rate** (GMGN hides it without login). No free fix found; the
  field stays null and is documented.
- **GitHub remote**: none yet (owner will add). Deploy is rsync-based meanwhile.
