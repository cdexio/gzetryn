# Phase 5 — token intel, leaderboards, API, contract

- Spec: sections 7, 8, 11.

## Scope

- Token intel service: the eight parts of spec §7, each from cached
  gateway calls at P0, run concurrently (the budget paces them), joined
  with our directory (names of smart traders) and our feed (events for
  the mint); per-part errors; 503 only when every GMGN part failed.
- Leaderboards from the latest snapshots (with the ~24 h earlier rank and
  profit), wallet history series, and the "who to copy" score
  (`core/copyscore`, pure, components returned).
- Market passthrough: trending, new pairs, pump.fun lists (20 s cache).
- API (FastAPI): every endpoint of spec §11, `X-Consumer` everywhere,
  bscout envelope and error shapes, validation of Solana addresses
  (base58, 32–44 chars).
- Ops: `/health` components, `/v1/stats` (counters since start + 24 h
  from `request_log`, budget, throttles, job ages), admin refresh/curate.
- `docs/contract.md` and `docs/openapi.json` (`gzetryn openapi`).

## Done when

- API tests (validation, envelope, manual wallet lifecycle, feed cursor,
  leaderboard shape) pass on the VPS; `/v1/token/{mint}` for a fresh
  pump.fun token returns every part live.
