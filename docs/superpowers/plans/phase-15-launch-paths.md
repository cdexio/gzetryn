# Phase 15 — curve path recorder (engine research G1)

**Decision:** owner D-2026-10-06-01, "G1 perbaiki sekarang".

**Why:** the engine is moving from Migration to launch-stage (sniper / curve) strategies. None of them can be
backtested without the price path of every launch on its bonding curve. This research is in the engine repo
under `docs/research/2026-10-05-sniper-strategies-research.md`.

## Facts this rests on

- The pump program stream (phase 9) already decodes every pump.fun TradeEvent, CreateEvent, CompleteEvent and
  migration on the trigger's public WS connection: about 65 notifications/s, 0 decode errors.
- About 46k pump.fun launches/day in the engine's `launches_seen` table (7 d).
- TradeEvent carries the trader (`user`), block time, the virtual and real reserves, and the creator.
  The decoder now keeps `user` as well.

## Units

1. **LaunchPaths (jobs).** Fed synchronously by PumpChain for every create / trade / complete / migration.
   - **A CreateEvent opens a path:** creator, name, symbol, mayhem flag, the create slot.
   - **Each trade inside `window_sec`** (default 1800 [TUNABLE]) updates the path:
     - trades, buys and sells;
     - distinct buyers and sellers, capped [TUNABLE];
     - SOL in and out;
     - dev (= creator) buy/sell SOL and the time of the dev's first sell;
     - same-slot bundle buyers (create slot + `bundle_slots` [TUNABLE]) and their SOL;
     - the price at each checkpoint (last trade price at or before 10/30/60/120/300/600/900/1800 s [TUNABLE]);
     - first, last and peak price, the peak time, the low after the peak;
     - maximum progress;
     - the first 10 distinct buyers (address, second, SOL) [TUNABLE].
   - **A CompleteEvent or migration** records graduation (time, pool), also after the window, as an update of
     the stored row.
   - **Finalization:** at the end of the window the path becomes one row. Above `max_in_flight` the oldest path
     is finalized early, counted in the stats.
2. **launch_paths table** (alembic 0004): one row per mint; indexes on created_at and creator; retention 30 days
   [TUNABLE].
3. **Runtime:** attached to PumpChain when `launch_paths.enabled`; written every `flush_sec`; shown in
   `/v1/stats` under `launch_paths`.

## Limits (accepted)

- Launches created while gzetryn is down, or whose CreateEvent was missed, are not recorded.
- Paths in flight at a restart are lost (flushed only once they are finalized).
- Prices are SOL per token from the virtual reserves, so non-SOL quote curves have null prices.

## Verification

- Unit tests on the VPS: checkpoints, dev and bundle accounting, peak and low, the window, late graduation, the
  in-flight cap.
- **After deploy:**
  - `launch_paths.created` grows at about 30–50/min;
  - the first rows appear 30 min later;
  - rows/day and size/day are measured;
  - the graduation share is checked against the engine's 1.34 % proxy.
