# Phase 10 — Raydium LaunchLab lists as candidate sources

- Owner decision D-2026-10-05-13 (1): candidate kinds `new` and `completing` for non-pump.fun launchpads
  (LetsBonk, StonkFun, …) from the Raydium LaunchLab list API, same `/v1/market/candidates` contract, launchpad /
  platform from `platformInfo.name`. Result: `phase-10-report.md` (in this file's "Live" section after deploy).

## Verified facts (2026-10-05, VPS, no key)

- Endpoint `GET https://launch-mint-v1.raydium.io/get/list?sort=&size=&mintType=default&includeNsfw=false` →
  `{id, success, data: {rows, nextPageId}}`. **Sorts accepted: `new`, `lastTrade`, `marketCap`**; others
  (`finishingRate`, `volumeU`, `createAt`, `hot`, `graduating`, …) → 500 "sort type error". `mintType` accepts only
  `default` (or none); `bonk`/`all` → 500. `size=100` works. `GET /get/by/mints?ids=a,b,…` returns rows by mint.
- Row: `mint, poolId` (owner `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` = Raydium LaunchLab, 3/3), `creator,
  createAt` (ms; equals the pool's on-chain creation block time, 6/6 within 0–6 s), `name, symbol, twitter,
  platformInfo{name, web, …}, mintB{address, symbol, decimals}` (quote: WSOL, NVDAx, SPCXx, WBTC, ZEC, USD1, …),
  `supply` (1 B), `marketCap` (USD: within 0.5–3.4 % of GMGN price × supply, 5/5), `volumeA/B/U` (cumulative;
  U = USD), `finishingRate` (percent), `migrateType` (cpmm/amm), `totalFundRaisingB`.
- **finishingRate = 100 after migration** (6/6 tokens GMGN shows on ray_v4/meteora/orca). It is Raydium's own scale
  and is consistently lower than GMGN's token-based progress (2.46 vs 7.3 %, 9.49 vs 18.5 %, 0.61 vs 2.2 %), so it is
  not comparable to pump.fun's progress.
- `sort=marketCap` is dominated by absurd caps on tiny test tokens (up to 9.9 T USD, finishingRate 0) → not used.
- Platform mix today: `sort=new` 98 % StonkFun, 1 % letsbonk.fun, 1 % Raydium (100 rows over ~79 min); GMGN's own
  rows agree (186 StonkFun, 1 letsbonk of 223 LaunchLab-family rows). LetsBonk activity is near zero right now.
- **Rate**: 20 calls at 0.3 s and 120 calls at 5 s → all 200, latency p50 0.22 s. No limit seen.
- **Freshness**: launches appear in `sort=new` 13–119 s after their on-chain creation (p50 79 s, 6 launches in
  10 min ≈ 36/h): the API indexes with a lag that polling cannot remove.
- Freshness against PumpPortal `bonk`: not measured — a second PumpPortal connection from this IP (the engine already
  holds one) is not allowed by PumpPortal's one-connection rule; the chain (pool creation block time) was used as the
  reference instead.
- Chain alternative measured: LaunchLab program `logsSubscribe` = 23.4 notifications/s (86 % failed), 27 KiB/s,
  0.8 MB per 30 s — cheap, but needs a verified decode of LaunchLab's own TradeEvent/PoolCreateEvent layout (its
  event names collide with pump.fun's). Not built now.

## Design

- New job `launchlab` (P3), own rate line `launchlab` in the budget with the GMGN group policy (bucket, gap,
  cooldown 15 s → 1 h, probe): 20/min, burst 3, gap 1 s `[TUNABLE]`.
- Every 15 s `[TUNABLE]`: `sort=new&size=50` → kind `new`. Every 30 s `[TUNABLE]`: `sort=lastTrade&size=100` → rows
  with 25 ≤ finishingRate < 100 `[TUNABLE]` → kind `completing` (25: a funds-based rate; it is lower than the token
  progress, see facts; tune with data). Load ≈ 6 requests/min.
- Row: `source launchlab`, mint, `pool_address` = poolId (the LaunchLab pool), `exchange ray_launchpad` (GMGN's name),
  `launchpad launchlab`, `launchpad_platform` = `platformInfo.name`, quote = `mintB.address`, creator, created_at =
  createAt, symbol/name; metrics: progress = finishingRate / 100 (documented as Raydium's scale), mcap_usd =
  marketCap, price_usd = marketCap / supply, `volume_total_usd` = volumeU (new metric key, null for other sources);
  holders, 1 h volume, buys/sells, tags, liquidity null.
- One row per (kind, mint), merged with GMGN rows as in phase 9.

## Risks

- Undocumented API; 500 on unknown parameters. Cooldown + probe protect it; stats show failures.
- The 1–2 min indexing lag; fix = chain reader on LaunchLab program logs (measured cheap) once its layout is verified.

## Live (deployed `6ce004b` 12:49 UTC, checked 13:00)

- Rows: `new` 25 (StonkFun 22, letsbonk.fun 1, Brim 1, stonk 1) + `completing` 6 (StonkFun, finishingRate
  29.8–41.4 %), every row with its LaunchLab pool; more LaunchLab launches already had a `new` row from GMGN new
  pairs and were merged (one row per kind + mint).
- Calls: ≈ 6/min on the `launchlab` line, 0 failures, 0 throttles; peak 5 requests in 60 s.
- Freshness: bounded by LaunchLab's own indexing (13–119 s, p50 79 s, measured in the probe) + ≤ 15 s polling.
