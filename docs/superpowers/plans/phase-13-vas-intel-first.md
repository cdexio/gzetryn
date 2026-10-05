# Phase 13 — `/vas/` for token intel first, wallet activity from the chain (owner D-2026-10-05-14)

## Problem (measured 5 Oct)

- `/vas/` was blocked 40–47 min/h even after the gentler ladder (phase 12). In the open minutes the
  wallet_activity polls took the group: 13:29–15:07 UTC, wallet_activity asked 1,979 requests/h (93 sent,
  1,886 denied), pump lists 92/h (0 sent), engine intel 10/h (0 sent).
- Engine intel (`gmgn_token_intel`, 11:37–16:28 CEST): 17 calls, **0** with `smart_traders` or holder tags;
  12 answered with `cooldown:vas` errors, 5 timed out at the engine's 4 s limit — those were the minutes `/vas/`
  was open: gzetryn got the vas parts from GMGN, but behind the trigger polls (P0 above the API's P1) and the 1 s
  `vas` gap the four vas calls needed > 4 s.
- The chain decoder already matched GMGN side and token amount exactly (10/10), and the engine reads only wallet,
  mint, side, SOL/USD amounts, price, symbol, time and signature from the feed: no consumer needs GMGN's
  `open_or_close` / launchpad on feed events.
- The pump lists added nearly nothing: since the pump.fun chain source started (12:29 UTC) it saw 90 of 92
  `migrated` first; the engine ignores `completing` and bonding-curve `new` rows and reads `first` only.
- The chain decoder itself queued in bursts: one RPC paced at 2 s → bursts of 14 swaps/min waited up to ~60 s
  (chain lag p50 36.4 s, p90 107 s over 13:29–15:07).

## Decision (with the data above)

1. **Priority in every group**: P0 engine token intel, P1 other API + chain-event enrichment, P2 pump lists,
   P3 wallet_activity sweep and other background. In `vas`: burst 5, P2/P3 leave 4 tokens (one survivor's four
   vas calls), P0 0.25 s apart (global gap 0.05 s for P0), P0 wait limit 3 s → a part whose group is cooling or
   being probed answers `unavailable` at once. All `[TUNABLE]`.
2. **Wallet activity chain-only per notified swap** (`trigger.chain_first`): every swap-like notification goes
   to the decoder, also while `/vas/` is open; no GMGN trigger poll. No GMGN enrichment pass (no consumer).
   GMGN wallet_activity remains a P3 **sweep** (900/1800/3600 s by recency) for what the chain cannot see
   (6.5 % of live trades not notified / not swap-like, plus decoder misses); a notified swap the decoder could
   not fetch pulls that wallet's sweep to +60 s; `not_a_swap` does not. Signatures the decoder dropped are
   remembered; a sweep that finds them counts them per reason (`watcher.chain_missed_found_by_gmgn`) and marks
   `payload.chain_missed`, to decide later with data whether `not_a_swap` deserves a GMGN poll.
3. **Pump lists skipped while the pump.fun chain source is healthy** (a pump.fun transaction within 120 s).
4. **Decoder throughput**: a second free RPC, PublicNode (verified 80/80 at 0.5 s, 60/60 at 1 s, p50 0.27 s;
   later 300/300 at 0.5 s over 160 s), paced at 1 s, then 0.5 s after a 58 swaps/min burst queued up to 73 s,
   next to mainnet-beta at 2 s; chain enrichment waits ≤ 2 s for the `/api/` budget; RPC calls serial, enrichment + insert in tasks, not-indexed
   transactions retried 2 s later without blocking. SOL/USD from GMGN's wSOL price (`/api/`), else the feed
   median (the feed now has few GMGN trades).
5. `/v1/stats` → `budget.groups.<g>.classes`: requests per class (`intel`, else the endpoint name) since start
   — sent, ok, throttled, denied, cache, sent in the last hour.

## Components touched

`config` (group reserve and P0 gap, P0 wait, RPC list, chain_first, sweep intervals, pump skip), `gateway/budget`
(per-group reserve, P0 gaps), `gateway/gateway` (class counters), `gmgn/endpoints` (request class), `jobs/token`
(P0), `jobs/watcher` (chain-first routing, sweep, miss polls, P3), `jobs/chain_fallback` (two RPCs, pipeline,
miss reasons, wSOL price), `jobs/candidates` (P2, skip), `jobs/pump_chain` (health), `runtime`, `api/status`.
Contract, spec §6.1/§6.2/§7/§7.1/§10 and the example config updated.

## Risks

- A 4-request burst in 0.75 s on `vas` per survivor: rare (~3/h) and far below the 13–15 req/60 s seen before
  re-challenges; watch the throttle windows in the samples.
- PublicNode is a third-party free endpoint: if it 429s or fails, it pauses 30 s and mainnet-beta carries the
  load (as before); trades not fetched go to the sweep.
- Fewer GMGN feed rows: SOL/USD comes from GMGN's wSOL price instead of the feed median.
- Legacy behaviour stays one switch away (`trigger.chain_first: false`, `candidates.pump_skip_when_chain_healthy:
  false`).

## Measure (≥ 60 min before vs after)

Share of engine intel calls with `smart_traders` and holder tags (engine `gmgn_token_intel`); `vas` requests per
hour by class; `vas` throttles per hour and blocked minutes per hour (throttle samples with their cooldown); feed
lag p50/p90 by source.
