# Phase 11 — measure-only `snipers` token part from DEXTools

- Owner decision D-2026-10-05-13 follow-up: replace GMGN's sniper tags (behind the blocked `/vas/` group) with
  DEXTools' first makers, as a measure-only part of `/v1/token/{mint}`. The engine is not changed now.

## Verified facts (2026-10-05, VPS, no key)

- `GET https://www.dextools.io/shared/data/pair?address=<pool>&chain=solana&audit=true&locks=true` (Chrome
  impersonation, `Referer: https://www.dextools.io/`) → `data[0]` with `token.firstMakers{snipers[], others[]}`
  (wallet strings), `token.deployment{owner, provider, createdAt}`, `token.promoted`, `migratedFrom{exchange, pair,
  date}`, `token.metrics{holders, mcap, fdv}`, `periodStats`, `metrics.initialLiquidity/balanceLpTokenBurned`.
- **Only AMM pools are known**: 26/26 bonding-curve pools (pump.fun `completing`/`new`, chain and GMGN rows) →
  400 "Pair not found"; 44/44 PumpSwap / Raydium / Meteora pools → 200. For pump.fun tokens `migratedFrom.exchange` =
  `dexbewf6p` (pump.fun) and `migratedFrom.pair` = the mint; `deployment.provider` = the pump program and
  `deployment.owner` = the token creator, so first makers count from the token's creation.
- `creator_is_sniper` true for 35/40 (the creator's buy in the create transaction) → weak signal; `promoted` true for
  32/40 → reported raw, meaning unverified.
- **Agreement with GMGN `sniper_count`** (40 tokens with stored GMGN values, pump.fun completed + trending):
  equal 3/40 (creator excluded), within ±1 4/40; DEXTools usually higher by tens (45 vs 0, 55 vs 11). The two
  measure different things (DEXTools: every first-block buyer; GMGN's counts: snipers among current holders) →
  **not a substitute for GMGN's number**; kept measure-only.
- Rate: 71 calls at a 2 s gap → 71 × 200, latency p50 0.24 s (coordinator: 15 rapid calls → 200).

## Design

- New part `snipers` (opt-in: not in the default part list) for `/v1/token/{mint}`.
- Pool resolution: the token's AMM pool from GMGN token info (`info.pool`, `/api/` group, cached 30 s); when that
  pool's exchange is a bonding curve (`pump`, `ray_launchpad`) → `available: false, reason: no AMM pool yet` without
  calling DEXTools.
- DEXTools call on its own budget rate line `dextools`: 20/min, burst 3, ≥ 2 s apart `[TUNABLE]` (2 s measured
  clean), GMGN group cooldown/probe policy; answer cached 1 h `[TUNABLE]` (first makers do not change).
- Output: `{source: "dextools", available, pool, count, count_excl_creator, wallets, creator, creator_is_sniper,
  promoted, migrated_from, holders, measure_only: true}`.

## Risks

Undocumented web endpoint; Cloudflare may challenge it → cooldown, part error, other parts unaffected.

## Live (deployed `ad180e7` 12:59 UTC)

- Migrated pump.fun tokens (chain rows): `available`, 27 and 34 snipers (creator included, creator_is_sniper
  true), 1.1–1.9 s cold, through the `dextools` line (2 calls, 0 errors).
- Bonding-curve tokens: `available: false` in 0.3 s without a DEXTools call (4/4).
- Engine wiring left to the coordinator (`parts=…,snipers`).
