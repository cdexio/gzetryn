#!/usr/bin/env bash
# One-time VPS setup for gzetryn: service user, directories, Postgres role + database (peer auth, port 5433),
# and /opt/gzetryn/.env. Creates ONE role and ONE database named gzetryn on the shared cluster and nothing else
# (DB tests use the schema gzetryn_test inside it). Touches no other service, database or global config.
set -euo pipefail

HOST="${GZETRYN_DEPLOY_HOST:-root@46.250.236.190}"
KEY="${GZETRYN_DEPLOY_KEY:-$HOME/.ssh/vps-contabo}"
SSH=(ssh -i "$KEY" -o BatchMode=yes "$HOST")

"${SSH[@]}" bash -s <<'REMOTE'
set -euo pipefail
id gzetryn >/dev/null 2>&1 || useradd --system --home-dir /var/lib/gzetryn --create-home --shell /usr/sbin/nologin gzetryn
install -d -o gzetryn -g gzetryn -m 750 /var/lib/gzetryn
install -d -o root -g root -m 755 /opt/gzetryn
sudo -u postgres psql -p 5433 -tAc "select 1 from pg_roles where rolname='gzetryn'" | grep -q 1 \
  || sudo -u postgres createuser -p 5433 gzetryn
sudo -u postgres psql -p 5433 -tAc "select 1 from pg_database where datname='gzetryn'" | grep -q 1 \
  || sudo -u postgres createdb -p 5433 -O gzetryn gzetryn
if [[ ! -f /opt/gzetryn/.env ]]; then
  printf '%s\n' \
    'GZETRYN_DATABASE_URL=postgresql+asyncpg://gzetryn@/gzetryn?host=/var/run/postgresql&port=5433' \
    'GZETRYN_TEST_DATABASE_URL=postgresql+asyncpg://gzetryn@/gzetryn?host=/var/run/postgresql&port=5433' \
    'GZETRYN_PORT=8793' > /opt/gzetryn/.env
fi
chown root:gzetryn /opt/gzetryn/.env && chmod 640 /opt/gzetryn/.env
echo "setup ok: user gzetryn, /var/lib/gzetryn, role+db gzetryn (5433), /opt/gzetryn/.env"
REMOTE
