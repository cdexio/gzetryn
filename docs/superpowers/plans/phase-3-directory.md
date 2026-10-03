# Phase 3 — directory (ranks, snapshots, curation, manual wallets)

- Spec: section 5.

## Scope

- Directory job: every `rank.interval_sec`, fetch the configured lists
  (`kol`, `smart_degen` × `7d`, `30d`) at P2, store a snapshot per list,
  upsert wallets with identity and metrics from the row.
- `core/curation` (pure): candidates from the latest snapshots → filters
  (tag, profit > 0, PnL > 0, win rate ≥ 0.5, trades/day ≤ 150) → order by
  30d realized profit → top 50; returns selected, added, removed and
  reasons per rejected wallet count.
- Curation run: after a refresh when due (daily) or at start-up when the
  last run is older than the interval; skipped when candidates are fewer
  than `curation.min_candidates`; applies flags (`curated`,
  `curated_rank`, `curated_at`, `uncurated_at`, `watch_started_at` for
  newly active wallets) in one transaction and stores the run.
- Manual wallets: add/update/remove in the store; metrics refresh job
  for manual wallets outside the latest snapshot (`wallet_stat` 30d +
  `wallet_common_stat`, P2) on add and every 6 h.
- CLI `wallets add/remove/label/list`, `curate`, `refresh` (the latter
  two through the running service's admin endpoints).

## Decisions

- Curated and manual are independent flags; curation never touches the
  manual flag; nothing is deleted.
- "PnL" = GMGN realized profit in USD (what GMGN's PnL order uses); the
  ROI ratio `pnl_30d` must also be positive.

## Done when

- Curation unit tests (thresholds, ordering, bot filter, manual
  untouched, deactivation) pass; on the VPS a live refresh stores 400
  snapshot rows and a curated list of up to 50 wallets.
