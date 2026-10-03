# Phase 2 — GMGN client (endpoints, parsers, transport, gateway)

- Spec: sections 2, 4, 10; facts: `phase-0-report.md`.

## Scope

- `gmgn/endpoints`: one registry entry per verified endpoint (name,
  method, path template with `{chain}`/`{address}`/`{mint}`/`{period}`,
  fixed params, cache class). The only place that knows GMGN paths.
- `gmgn/answer`: raw answer record and classification into outcomes
  (ok, bad_param, needs_login, not_found, throttled, server_error,
  network, unknown) from the phase 0 codes.
- `gmgn/parse`: tolerant parsers that turn GMGN rows into plain records:
  rank row, trade, wallet stat, common stat, and the token parts
  (window info, launchpad, security, dev, dev history, holder rates and
  counts, smart traders), plus market list passthrough. Strings become
  floats/ints, unix seconds become UTC datetimes, missing fields become
  null; a malformed row is skipped and counted, never fatal.
- `transport/http`: one curl_cffi session, GET with repeated query keys
  (`type=buy&type=sell`), POST JSON, 15 s timeout, latency measured.
- `gateway`: cache (TTL by endpoint, stale on failure, max-age override),
  coalescing of identical in-flight calls, budget (token bucket, burst,
  min gap, priority reserves, bounded waits, throttle pause with
  doubling), classification → action, counters per consumer/endpoint/
  outcome with latency, samples hook.
- CLI `gzetryn fetch <endpoint> key=value ...` for live re-verification.

## Done when

- Unit tests: every parser on its fixture; budget (reserves, min gap,
  pause doubling/reset) and gateway (cache hit, coalescing, stale on
  throttle, bad param → 400 class) on a fake clock and transport.
- `gzetryn fetch rank_wallets ...` and `wallet_activity` work on the VPS.
