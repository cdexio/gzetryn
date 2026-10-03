# Phase 6 — deploy and verify

- Spec: section 13.

## Scope

- `deploy/setup-vps.sh` (once): system user `gzetryn`, directories
  `/opt/gzetryn` and `/var/lib/gzetryn`, Postgres role + database
  `gzetryn` on port 5433 with peer auth (nothing else on the cluster),
  `.env` with the database URL and port.
- `deploy/deploy.sh`: refuse a dirty tree; rsync the tree to
  `/opt/gzetryn` (excluding `.git`, `.venv`, `.env*`, caches, logs);
  write the revision; `uv sync --frozen --no-dev` into
  `/var/lib/gzetryn/venv`; optional test run (`GZETRYN_DEPLOY_TESTS=1`,
  `nice -n 19`); `alembic upgrade head`; install the unit and CLI wrapper;
  restart only `gzetryn`; wait for `/health`.
- `deploy/gzetryn.service`: loopback, `Restart=on-failure`, hardening
  (`ProtectSystem=strict`, `ReadWritePaths=/var/lib/gzetryn`).

## Verification checklist

1. `curl -s -H 'X-Consumer: zetryn' 127.0.0.1:8793/health` → ok.
2. First rank refresh stored (4 lists × 100 rows).
3. Curated list built; report the top 10 with 30d profit / win rate.
4. Feed receives live events within ~10 minutes.
5. `/v1/token/{mint}` for a fresh pump.fun token returns all parts.
6. Request rate and any 429/403 from `/v1/stats` after the run.

## Done when

The checklist passes and its numbers are in the final report.
