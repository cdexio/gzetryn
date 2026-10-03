# Phase 4 — watcher and feed

- Spec: section 6.

## Scope

- Watcher job: active set (curated or manual) reloaded every 60 s; per
  wallet due time with hot/warm/cold intervals from the last trade,
  jitter, at most 2 polls in flight, P1.
- Poll: `wallet_activity` page of 20 buys/sells; parse to trades; insert
  idempotently; follow `cursor` while the whole page is new and older
  than nothing we know, up to 3 pages; mark `baseline` for trades before
  `watch_started_at`; update the wallet's watch state (ok/error, last
  trade, counters).
- Feed writes under one advisory lock so `seq` commits in order; wallet
  label/tags/status are copied into each event at insert.
- `core/watch` (pure): interval tier, "fetch next page?" and baseline
  decisions, so they are unit-tested without a DB.

## Decisions

- Event identity `(wallet, tx_hash, mint, side, token_amount)`.
- `mcap_usd = price_usd × total_supply`; liquidity per trade is null
  (GMGN does not expose it).
- Baseline events are stored but hidden from the feed by default.

## Done when

- Unit tests for tiers, paging and baseline; DB tests for idempotent
  insert, seq order and the feed cursor with filters; on the VPS live
  trades appear in the feed within 10 minutes of start.
