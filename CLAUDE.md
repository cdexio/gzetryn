# CLAUDE.md — project rules for gzetryn

These rules apply to every session in this directory and override any
default or auto-mode guidance that says otherwise.

## 1. File editing: editor tools only

- Create, write and edit files **only** with the `Write` and `Edit` tools.
- Never create or modify file contents through Bash (no heredocs, `echo >`,
  `sed -i`, `tee`, `awk`/`perl -i`, `python - <<EOF`).
- Read files with `Read`. Bash is for searching, listing, running, git and
  inspecting. Deleting a file needs the owner's confirmation first.

## 2. Language

- Code, comments, identifiers, commits, config, logs and docs are in
  **English**. The conversation with the owner is in Indonesian.
- Every commit message ends with
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## 3. Facts before code

- Every GMGN endpoint, parameter, field, header and rate the code relies on
  is verified live from the VPS first and written down in
  `docs/superpowers/plans/phase-0-report.md` (or a later report).
- Unmeasured numbers are configuration and marked `[TUNABLE]` in the spec.
- 100% free: no paid APIs, no paid proxies, no GMGN login.

## 4. Plans

- One spec: `docs/superpowers/specs/2026-10-03-gzetryn-design.md` (update it
  when the design changes). One plan per phase under
  `docs/superpowers/plans/`.
- Plans are functional (components, data flow, interfaces, decisions,
  risks) with no code blocks.

## 5. Problems come with a fix

Never report a problem without investigating it and proposing a concrete
solution.

## 6. Machines

- **Never run tests, builds or heavy commands on the laptop.** Tests run on
  the VPS: `nice -n 19` and the `gzetryn_test` database.
- The VPS (`root@46.250.236.190`, key `~/.ssh/vps-contabo`) is shared.
  gzetryn owns only `/opt/gzetryn`, `/var/lib/gzetryn`, systemd
  `gzetryn.service`, port `127.0.0.1:8793`, and the Postgres 16 (port 5433)
  roles/databases `gzetryn` and `gzetryn_test`. Never touch cdexio-*,
  bscout, xscout, zetryn-* services, other databases, or global Postgres
  config. Never use wildcard `systemctl`/`pkill`.

## 7. Contract with consumers

`docs/contract.md` is the API contract the engine (`X-Consumer: zetryn`)
reads. Any change to endpoint shapes or semantics updates it, and
`docs/openapi.json` (`gzetryn openapi`), in the same commit.
