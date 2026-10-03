# Phase 1 — foundation

- Spec: sections 3, 9, 12, 13.

## Scope

- uv project (`pyproject.toml`, Python 3.14, package `src/gzetryn`,
  console script `gzetryn`), ruff and pytest settings mirroring bscout.
- `config`: `Settings` from env / `.env` (`GZETRYN_` prefix: database URL,
  host, port 8793, config file, log level) and `Tunables` from YAML with
  validated defaults for every `[TUNABLE]` number (budget, cache TTLs,
  rank, curation, watch tiers, token parts, retention, API limits).
- `clock` (real and fake), `log` (JSON lines to stdout → journald).
- `store/models` for `wallets`, `rank_snapshots`, `curation_runs`,
  `trades`, `request_log`, `samples`; Alembic environment that accepts
  `-x url=` and `-x schema=` (the latter for the DB test schema), and the
  initial migration.
- `store/db`: async engine factory with an optional `search_path` so the
  same code runs in `public` (service) and `gzetryn_test` (tests).

## Decisions

- One database `gzetryn`; tests use a schema, guarded so they can never
  run in `public`.
- No secrets exist (no login), so no encryption module.

## Done when

- `alembic upgrade head` creates the tables on the VPS database, and in
  the test schema from the test fixtures.
