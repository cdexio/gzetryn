# Phase 8 — report (2026-10-05): throttling, chain fallback, engine sources

All times UTC. Plan: `phase-8-throttling-and-engine-sources.md`.

## Root cause of the 429s

- GMGN's 429s are **Cloudflare challenges** (`cf-mitigated: challenge`, "Just a moment…" HTML, `__cf_bm` cookie),
  not GMGN application errors. The same block answers 403 to other TLS profiles (chrome131, firefox) and 429 to
  chrome/safari: changing the fingerprint does not help.
- They are scoped to a **path group**: during the 07:23 block `/vas/` (wallet_activity, token_holder_stat,
  token_traders) answered 429 while `/api/` (token_stat) and `/defi/` (wallet rank, walletNew) answered 200.
- No other process on the VPS talks to gmgn.ai (no connections to its IPs, no references in other deployed code).
- Not a sustained-rate limit: 30 throttles in 49 h at 5–11 req/min on average, uncorrelated with hourly volume
  (hours with 685 requests had none, hours with 272 had two). Triggers seen: short bursts after a group reopens
  (13–15 requests in 60 s before the 09:11:27 and 09:19:42 re-challenges) and the old trigger retry ladder
  (5 `/vas/` polls in 31 s; 1,453 of 3,849 triggers never resolved and used all of them).
- **`/vas/` blocks last long**: still blocked after 17 min without a single request (07:01 → 07:18); 06:54 →
  past 10:11 with only brief openings. Probes at 1 request per 5 min kept hitting 429 (09:19:42, 09:24:42,
  09:29:42, windows {10: 1, 60: 1, 300: 1}).
- `/api/` was challenged for the first time at 10:11:44 / 10:12:44 (18–20 requests in 300 s) and reopened within
  about a minute.
- The old policy turned every 429 into a **global** pause of 120 s doubling to 30 min: ≈ 7,800 s of total blackout
  in 49 h (4.4 %), 06:54 → 07:48 this morning, every endpoint `503 budget`.

## Changes (commits, all deployed)

| Commit | Change |
|---|---|
| `ff713f7` | Per-path-group budget (vas/api/defi/mrwapi): own bucket, min gap, cooldown + probe; global pause only if 2+ groups cool; strict priority P0 trigger > P1 API > P2 interval > P3 background; trigger polls own slots; retry ladder +1/+3/+7 s; window counters; candidates table + `/v1/market/candidates`; enricher TTLs |
| `69f2f6d`, `d672ea6` | Candidate kinds verified: pump.fun `completed` list = migrated; new-pair `pump_amm` + `pump` + `Pump.fun` = migrated (11/12 confirmed, ~279 s earlier); other new pump_amm pools = new; migration 0003 dropped 5 misclassified rows |
| `982915c` | **Chain fallback**: while `/vas/` is not open, a swap-like notification is decoded from the transaction (public RPC, `maxSupportedTransactionVersion: 1`) and stored as `source=chain`; de-dup identity (wallet, tx_hash, mint, side) |
| `0034c68` | `holders` part returns its `/api/` rates while the `/vas/` tag counts are unavailable |
| `172e231` | Cooldown escalates on consecutive 429s (15 s → … → 1920 → 3600 s), reset by the first success; level/step logged and in `/health` |
| `06d4a01` | Cooldown ladders restored after a restart from the last throttle sample |
| `584973a` | vas 12/min burst 3; chain fallback skips already queued/decoded signatures |

## Measured

### Before (old policy, 2026-10-03 06:22 → 10-05 07:20, 49 h)

| Metric | Value |
|---|---|
| Throttles | 30 (0.61/h): 24 wallet_activity, 6 token_stat |
| Pause | ≈ 7,800 s total (4.4 %), **all** endpoints |
| Feed lag (24 h) | trigger p50 2.64 s / p90 3.41 s (1,514 events); interval p50 192 s / p90 876 s; overall p90 53 s |
| Denied at the end | P0 138, P1 757 (every endpoint 503 `budget`, retry ≈ 1,580 s) |

### After

| Metric | Value |
|---|---|
| Throttles 07:42 → 10:11 | 47, **all in `vas`** until 10:11 (07: 14, 08: 18, 09: 14, 10: 1); then 2 in `api` (10:11–10:12) |
| Global pauses | 0 |
| Escalation (172e231, 09:37:53 → 10:11) | 8 `vas` probes at 15/30/60/120/240/480/960/1920 s (levels 1–8) instead of one every 300 s; next probe ~10:41; after a restart the ladder continued at level 8 (1,682 s remaining) |
| Other groups while `vas` cooled | api/defi/mrwapi granted 118/32/3, denied 0; candidates: new_pairs 53 ok / 1,050 new, trending 29 ok; pump lists (vas) failed 54 |
| Feed lag, trades after the 07:56 deploy (2 h) | **p50 1.87 s / p90 3.83 s** (80 events); chain 71 events p50 1.71 / p90 3.35 |
| Feed lag, 09:37:53 → 10:11 (30+ min) | **p50 2.01 s / p90 5.33 s** (57 events, all `chain`: `/vas/` was closed the whole time) |
| Chain fallback (09:37 → 10:11) | 61 triggers, 208 RPC calls, 0 RPC 429, 189 decoded, 15 not a swap, 57 events, 132 duplicates (re-routes; fixed in 584973a) |
| WebSocket | connected since 10-03 06:22, 0 reconnects, 63 subscriptions |

### Events that lost enrichment while `vas` cooled

99 `source=chain` events since 07:42. All 99 lack `open_or_close`, `launchpad` and `launchpad_platform`; 0 lack
`symbol`, `usd_amount` or `mcap_usd`. Their `sol_amount` includes DEX fees/tips (0.4–2.2 % above GMGN's figure),
and `usd_amount`/`price_usd`/`mcap_usd` use a SOL/USD median from recent GMGN trades (`payload.sol_usd`).

## Engine sources (verified live 2026-10-05)

- Candidates: 3,886 new, 99 completing, 111 migrated (81 from the completed list, 30 from new pairs), 152
  trending; every row has a pool address (trending pools resolved, 0 missing).
- Enricher: a cold `parts=security,launchpad,dev,dev_history,holders,smart_traders` takes ~2–2.5 s, a repeat
  0.1 s (`meta.cached`). While `vas` cools, `smart_traders` is null and `holders` returns its rates only
  (`errors.holders = partial: …`). Expected load at 200–300 tokens/day: ≈ 1.6–1.9 GMGN req/min (≈ 0.7–0.8 on vas).

## Still open (with the proposed fix)

1. **`/vas/` has been challenged for hours** (since 06:54 with brief openings). The feed stays real-time through the
   chain fallback, but the `vas`-only data is missing meanwhile: pump.fun lists (completing; the main `migrated`
   source), token holder tag counts, smart traders, and GMGN's own row for each trade. Fix options: (a) wait —
   the 1 h ladder costs ~1 probe/h; (b) take the `completing`/`migrated` lists from the chain too (pump.fun
   program events via the same free WebSocket) — needs a design and verification; (c) a second egress IP — not
   free, owner decision.
2. **`/api/` can be challenged too** (first time 10:11). The chain fallback's symbol lookup and the candidate pool
   resolution use it. If `api` and `vas` cool at the same time, a 60 s global pause follows. Watch
   `budget.groups.api.last_throttle_windows` over the next day; if it repeats, lower `api` to 20/min.
3. **Chain events stay without `open_or_close`/launchpad**: GMGN's later row is skipped as a duplicate.
   Fix if the engine needs these: let GMGN's row update the chain event in place (no new seq). Engine decision.
