# Phase 9 — pump.fun `completing` (and `migrated`) from the chain

- Owner decision D-2026-10-05-11: build the pump.fun completing list from the chain inside gzetryn, replacing GMGN
  `/vas/` pump lists while `/vas/` is blocked. Same `/v1/market/candidates` contract; mark the source; de-duplicate
  against GMGN rows. `migrated` from the chain only if cheap. GMGN tags have no chain equivalent (out of scope).
- Spec: section 7.1 (updated). Result: `phase-9-report.md`.

## Verified facts (2026-10-05, from the VPS, before any code)

Source of layouts: the public pump.fun IDL, `pump-fun/pump-public-docs/idl/pump.json`, program
`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` (TradeEvent, CreateEvent, CompleteEvent, CompletePumpAmmMigrationEvent,
BondingCurve, Global). Events are Anchor `Program data:` log lines, discriminator = sha256("event:<Name>")[:8].

- **Volume** (program-wide `logsSubscribe {mentions: [program]}`, confirmed, public WS): 65.6 notifications/s,
  36 TradeEvents/s, 172–178 KiB/s = 5.3–5.5 MB per 30 s, message p50 2.8 KB / p95 6.4 KB; 173 distinct mints per
  minute; 1.1 CreateEvent/s. The documented public limit is 100 MB per 30 s per IP and 40 connections
  (solana.com/docs/references/clusters, Mainnet). The trigger connection already carries ≈ 2.2 MB per 30 s → ≈ 7.5
  MB per 30 s together (7.5 % of the limit), on the **same** connection (no second connection, no RPC requests).
- **Decoding**: 8,738 TradeEvents decoded, 0 errors. Invariant `virtual_token_reserves − real_token_reserves =
  279.9 M tokens` held on every curve (IDL Global: initial virtual 1,073 M, initial real 793.1 M).
- **Progress** = 1 − real_token_reserves / 793.1 M (6 decimals) for standard curves. Ground truth: the decoded
  event equals the on-chain BondingCurve account in 21/21 curves not traded since; GMGN's `launchpad_progress`
  equals it to 1e-4 whenever GMGN is fresh (10/10 in the account check, 15 in the event check). The other GMGN
  values were stale or wrong (e.g. 0.0233 vs 0.846, 0.0008 vs 0.577).
- **Excluded**: `mayhem_mode` curves (platform `pump_mayhem`, larger initial real reserves, formula does not apply;
  GMGN's completing list asks for `Pump.fun` only) and any curve whose reserves break the standard invariant.
  Non-SOL quotes (e.g. `pumpCm…`) keep a valid progress but have no SOL price.
- **Bonding-curve address**: PDA `["bonding-curve", mint]` computed locally, 50/50 equal to the frontend's
  `bonding_curve`.
- **Migrated**: `CompleteEvent` and `CompletePumpAmmMigrationEvent` are in the logs; the migration event follows the
  complete event by 0–1.2 s, arrives 1.6–1.7 s after its block timestamp and carries the PumpSwap `pool` — 2/2 equal
  to GMGN's pool. Rate ≈ 1 complete per 1.5–2.5 min. → cheap, included.
- **Threshold**: GMGN's completing list (113 rows captured) spans progress 0.5477–1.0 (p05 0.58, p50 0.67); it is a
  top-N-by-progress list. Chain threshold **0.55 [TUNABLE]** (≈ 49 standard curves above it at a time, ≈ 3 new
  crossings per minute ≈ 180 rows/h).
- **pump.fun frontend API** (`frontend-api-v3.pump.fun/coins`, coordinator's input), measured: 19/19 calls 200,
  latency p50 0.69 s. `sort=market_cap` returns stalled near-graduation coins (none of 900 rows traded in 3 min) — not a
  live completing signal. `sort=last_trade_timestamp` is fresh (newest trade 2.3–9.3 s old) but covers 50 rows per
  call against 36 trades/s, so following every curve needs ≈ 40 calls/min to an undocumented API. Not chosen as the
  source; noted as a possible symbol/metadata fallback.

## Design

- **Subscription**: the existing `WsTrigger` connection gains one program-level subscription (pump program). Its
  notifications go to a new consumer; wallet trigger counters stay separate. Reconnects resubscribe it like the
  wallet subscriptions.
- **Reader (`jobs/pump_chain`)**, per successful transaction:
  - CreateEvent → remember (name, symbol, creation time, total supply) per mint in a bounded LRU
    (200k mints [TUNABLE]) so later rows have a symbol without any request.
  - TradeEvent on a standard curve with progress ≥ threshold → a `completing` candidate: first sighting written at
    once; later trades update its `last` metrics at most every 30 s per mint [TUNABLE].
  - CompletePumpAmmMigrationEvent (or CompleteEvent without it) → a `migrated` candidate with `complete_at`,
    `exchange pump_amm` and the pool from the event.
  - Writes are queued and flushed every 1 s [TUNABLE] in one transaction.
- **Row fields**: `source = chain`; pool = bonding-curve PDA (completing) / PumpSwap pool (migrated); exchange
  `pump` / `pump_amm`; launchpad `pump`, platform `Pump.fun`; creator (TradeEvent); quote; created_at from the cached
  CreateEvent (null when the token was created before gzetryn started); metrics: progress, price_usd =
  virtual SOL / virtual tokens × SOL/USD (median of recent GMGN trades in the feed, as the chain fallback), mcap_usd =
  price × total supply, liquidity_usd = real SOL in the curve × SOL/USD. Holders, volume, buy/sell counts and GMGN tag
  counts are null (no chain equivalent without extra reads).
- **De-duplication with GMGN**: one row per (kind, mint) as before. Whichever source sees a mint first owns `source`
  and `seq`; the other updates it. `last` is merged (a source's null fields no longer erase the other's values), so
  GMGN's holders/tags survive chain updates and vice versa.
- **Stats**: notifications, bytes, trades decoded, decode errors, rows written per kind, freshness (row write time −
  trade block time) p50/p90, in `/v1/stats` and `/health`.

## Risks

- Public WS without SLA: if the connection drops, the trigger reconnects; trades during the gap are missed and
  curves reappear at their next trade.
- Program upgrades can change event layouts: decode errors are counted; the fields used come before the variable
  part except `mayhem_mode`/`quote_mint`, which are read only when present.
- CPU: ≈ 66 JSON messages/s on the service's event loop (small); measured after deploy.

## Done when

Deployed; probe numbers, freshness (event → row), extra WS/RPC load and the first live rows are in the report.
