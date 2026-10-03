# gzetryn

Self-hosted GMGN (`gmgn.ai`) web-data service for the ZetrynAI Solana memecoin engine, built like xscout and bscout.
Loopback only, `127.0.0.1:8793`. It keeps an automatic KOL / smart-money directory (GMGN ranks → daily curated top
50), polls the trades of every curated and manual wallet into an ordered feed, and answers token intel (security,
dev history, holder composition by tag, KOL/smart wallets that traded it). No login, no paid APIs.

- Design: `docs/superpowers/specs/2026-10-03-gzetryn-design.md`
- Verified GMGN facts: `docs/superpowers/plans/phase-0-report.md`
- API contract for the engine: `docs/contract.md` (schema `docs/openapi.json`)

## Layout

`src/gzetryn/`: `gmgn` (endpoints + parsers, the only code that knows GMGN), `transport` (curl_cffi, Chrome
impersonation), `gateway` (cache, coalescing, budget, classification), `core` (pure rules: curation, copy score,
watcher decisions), `jobs` (directory, watcher, token intel), `store` (Postgres), `api`, `cli`.

## Development

Never run tests or builds on the laptop. Tests run on the VPS (`GZETRYN_DEPLOY_TESTS=1 deploy/deploy.sh`, or by hand
as the `gzetryn` user with `nice -n 19`); DB tests use the schema `gzetryn_test` inside the `gzetryn` database.

## Deploy (production VPS)

Code in `/opt/gzetryn` (rsynced from a clean git tree, root-owned), virtualenv `/var/lib/gzetryn/venv`, user
`gzetryn`, unit `gzetryn.service`, Postgres 16 on port 5433 through peer authentication (role + database `gzetryn`).

- First time: `deploy/setup-vps.sh`, then `deploy/deploy.sh`.
- Updates: commit, then `deploy/deploy.sh` (add `GZETRYN_DEPLOY_TESTS=1` to run the suite before the restart).
- Tuning: `/opt/gzetryn/config/gzetryn.yaml` on the VPS (see `config/gzetryn.example.yaml`; kept by deploy).
- Logs: `journalctl -u gzetryn -f` (JSON lines).

## CLI (on the VPS: `gzetryn ...` runs as the service user)

- `gzetryn wallets list --status curated|manual|active|ranked|inactive|all`
- `gzetryn wallets add <address> --label <name> --tag <tag>` / `wallets label ...` / `wallets remove <address>`
- `gzetryn refresh [--curate]`, `gzetryn curate`, `gzetryn stats` (through the running service)
- `gzetryn fetch <endpoint> key=value ...` (one live GMGN call, for re-verification)
- `gzetryn openapi` (writes `docs/openapi.json`)

Every API request needs `X-Consumer: <name>` (the engine uses `zetryn`).
