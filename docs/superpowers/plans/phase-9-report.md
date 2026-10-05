# Phase 9 — report (2026-10-05): pump.fun completing / migrated from the chain

Plan and verification: `phase-9-chain-completing.md`. Deployed `886fd27` at 12:28 UTC.

## Probe numbers (before code)

| Item | Value |
|---|---|
| Program stream (public WS, confirmed) | 65.6 notifications/s, 36 TradeEvents/s, 172–178 KiB/s (5.3–5.5 MB/30 s), msg p50 2.8 KB |
| Decoding | 8,738 TradeEvents, 0 errors; `virtual − real tokens = 279.9 M` on every curve |
| Progress vs bonding-curve account | 21/21 equal (curves not traded since) |
| Progress vs GMGN `launchpad_progress` | equal to 1e-4 whenever GMGN was fresh (10 in the account check, 15 in the event check); other GMGN values stale/wrong (0.0233 vs 0.846, 0.0008 vs 0.577) |
| `pump_mayhem` | different initial reserves → excluded (GMGN's completing list is `Pump.fun` only) |
| PDA `["bonding-curve", mint]` | 50/50 equal to pump.fun's `bonding_curve` |
| Migration event pool | 2/2 equal to GMGN; event 0–1.2 s after CompleteEvent, 1.6–1.7 s after its block |
| GMGN completing list range | progress 0.5477–1.0 (113 rows) → threshold 0.55 [TUNABLE] |
| pump.fun frontend API | 19/19 calls 200, p50 0.69 s; `sort=market_cap` = stalled coins (0 of 900 rows traded in 3 min); `sort=last_trade_timestamp` fresh (2.3–9.3 s) but full coverage ≈ 40 calls/min → not chosen |

## Design

Program-level `logsSubscribe` on the trigger's existing WS connection; TradeEvent → `completing` (progress ≥ 0.55,
standard non-mayhem curves, `last` refreshed ≤ every 30 s), CompletePumpAmmMigrationEvent → `migrated` with pool,
CreateEvent → symbol/name; `source chain`; candidates merge per (kind, mint) without erasing the other source's
values. Chosen over the frontend API because it is real-time, complete (every trade), verified exact, adds no
request load and no new external dependency.

## Live (first 5 minutes after deploy)

| Metric | Value |
|---|---|
| Transactions / trades decoded | 21,934 / 16,587, decode errors 0, handler errors 0 |
| Excluded | mayhem 3,254 trades, non-standard 4 |
| completing first sightings / updates | 70 / 464 (40 new `source chain` rows; 30 mints already had a GMGN row and kept it) |
| migrated | 4 (pool from the event; e.g. ZACHXBT created 12:29:06, completed 12:29:10) |
| Freshness block → candidates row | **first sighting p50 1.76 s / p90 2.53 s**; updates p50 1.76 / p90 2.44 (block time is whole seconds: up to 1 s of this is rounding) |
| WS load | program stream 105 MB in 5.1 min ≈ 344 KB/s ≈ 10.3 MB/30 s (61 % failed bot transactions at this hour, vs 14 % in the probe); all subscriptions ≈ 11 % of the documented 100 MB/30 s per IP; 1 connection, 0 reconnects, 0 RPC requests |
| Process | 14.6 % CPU (one core), RSS 133 MB |

## Open

- Failed transactions are ~60 % of the stream's bytes at busy hours and cannot be filtered server-side with
  `logsSubscribe`. If the public endpoint ever throttles, the fix is to move the program subscription to a second
  connection (40 allowed per IP) so the wallet trigger is isolated; not needed at 11 % of the limit.
- Chain rows have no holders/volume/tag counts (no chain equivalent without extra reads); GMGN rows fill them via
  the merge when `vas` is open.
- symbol/name/created_at are null for tokens created before the service (re)started (CreateEvent not seen).
