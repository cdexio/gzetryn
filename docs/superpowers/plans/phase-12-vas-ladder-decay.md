# Phase 12 — cooldown ladder decay and slow reopen (approved by the coordinator 2026-10-05)

## Problem (measured)

With "reset the ladder on the first success", `/vas/` was re-challenged 6 times between 12:12 and 13:08 UTC,
30 s – 20 min after each reopen, and every episode restarted at 15 s (5–6 probes into a fresh block each time).

## Change

- A success reopens the group but keeps the ladder level. The level steps down one per 15 clean minutes
  `[TUNABLE cooldown_decay_sec 900]`, counted from the last throttle; a re-challenge continues from the decayed
  level (doubling as before, cap 1 h).
- For 10 minutes after a reopen `[TUNABLE reopen_slow_sec 600]` the group runs at half its rate and with double its
  minimum gap `[TUNABLE reopen_rate_factor 0.5]`, so the reopen does not flush a backlog burst.
- Both apply per group (all GMGN groups and the launchlab/dextools lines). `/v1/stats` shows `cooldown_level` (after
  decay) and `slow_after_reopen_sec` per group.

## Measure

Re-challenge count (block episodes that start after a reopen) and blocked minutes per hour for `vas`, from the
throttle samples (each records its cooldown), 60+ minutes before vs after the deploy.
