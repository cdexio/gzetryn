# Phase 8 — throttling policy and engine sources

- Spec: sections 7, 7.1, 10, 11. Owner approval 2026-10-05 (gzetryn becomes an engine candidate source and token
  enricher). Result: `phase-8-report.md`.

## Problem (measured 2026-10-05)

- Every endpoint answered `503 budget` (retry ≈ 1,580 s): one 429 paused all GMGN traffic, doubling to 30 min.
  49 h: 30 throttles (0.61/h, 24 `wallet_activity`, 6 `token_stat`), ≈ 7,800 s of total blackout (4.4 %), the
  last one 06:54 → 07:48 UTC.
- The 429s are Cloudflare challenges (`cf-mitigated: challenge`, "Just a moment…"), scoped to a path group: during
  the block `/vas/` (wallet_activity, token_holder_stat, token_traders) answered 429 while `/api/` (token_stat) and
  `/defi/` (wallet rank, walletNew) answered 200. The `/vas/` block cleared ~7 min after the last throttled call.
- Average load was only 5–11 req/min and throttles did not follow hourly volume → short bursts set them off. Burst
  sources: trigger retry ladders (5 polls in 31 s; 1,453 of 3,849 triggers never resolved and used all of them),
  concurrent interval polls, and token calls firing 4 `/vas/` requests at once.
- No other process on the VPS talks to gmgn.ai.

## Scope

- Budget per path group (`vas`, `api`, `defi`, `mrwapi`): token bucket and minimum gap per group, cooldown per
  group (15 s doubling to 5 min, reset after 15 min clean), one probe after a cooldown, global pause only when two
  or more groups cool together, global cap kept.
- Strict priority inside a group: P0 trigger polls, P1 API calls, P2 interval polls, P3 background. Trigger polls
  get their own concurrency slots in the watcher.
- Shorter trigger retry ladder (+1, +3, +7 s; 10 s) from the measured lag distribution.
- Measurement: request starts per group in 10/60/300 s windows, peaks, and the counts before each throttle (stats
  and throttle samples) — the data to set the per-group rates from facts later.
- Candidates: background job + `candidates` table + `/v1/market/candidates` cursor endpoint; pool address resolved
  for trending rows (≤ 5 mints per `mutil_window_token_info` call, verified).
- Enricher: token parts at P1 with longer cache TTLs; expected load documented.
- Contract and OpenAPI updated with the verified row fields.

## Decisions

- No global pause on a single group's throttle: verified that other groups keep answering.
- Per-group rates are `[TUNABLE]` starting points (vas 20/min with 1 s gap to smooth bursts); the window data
  decides later changes.
- Candidates are stored, not proxied, so the engine's polling never multiplies GMGN calls and never sees a mint
  twice per kind.

## Done when

- Deployed; before/after throttles per hour, cooldown/pause seconds and feed lag p50/p90 measured over a window of
  several hours; candidate and enricher fields documented from live rows.
