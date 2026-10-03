# Phase 7 — on-chain trigger (real-time feed)

- Spec: section 6.1. Owner decision 2026-10-03: feed real-time or a few seconds at most, without raising the
  GMGN rate (≈ 450 req/min for 5 s polling of 38 wallets was rejected as a Cloudflare-block risk).
- Result: `phase-7-report.md`.

## Verification first (done before code)

- `tools/ws_probe.py` on the VPS against `wss://api.mainnet-beta.solana.com`: subscription limits on one
  connection, disconnects, notification delay vs `getBlockTime`, `processed` vs `confirmed`, bandwidth, share of
  failed transactions, and coverage: every GMGN trade of the probed wallets in the window must have been notified.
- Logs of successful notifications captured and matched to GMGN trades (`tests/fixtures/ws-logs.json`) to define
  the swap-like filter from evidence.

## Scope

- `trigger/solana_ws.py`: one connection, `logsSubscribe` per active wallet, sync on set changes, reconnect with
  backoff, swap-like classifier, signature cache for coverage stats, counters.
- Watcher: trigger queue with debounce, min gap, per-wallet cap, retries until the signature is indexed, one poll
  for every pending signature; fallback intervals while the trigger is healthy, normal ones when it is not;
  `payload.source/notified/notified_at`; coverage counters.
- `/health` component `trigger`; `/v1/stats` trigger counters and live lag by source; contract lag semantics.

## Decisions

- GMGN stays the only event source; no WebSocket-only events (no duplicates, same enrichment).
- No transaction decoding in v1 (needs `getTransaction` on the rate-limited public HTTP RPC for a ~1–2 s gain).
- No metered provider (engine's Alchemy at ~83 % of its free quota; Helius WS bills per MB) without owner approval.

## Done when

- End-to-end lag (block time → event) p50/p90 measured on live trades, GMGN req/min after the change, reconnects,
  coverage — written in the report.
