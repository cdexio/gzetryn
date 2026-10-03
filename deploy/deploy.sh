#!/usr/bin/env bash
# Deploy gzetryn to the VPS from this machine (no git remote yet): clean tree → rsync → uv sync → optional tests
# → alembic → restart gzetryn only → health. First time: deploy/setup-vps.sh (once).
#
#   deploy/deploy.sh                      deploy HEAD
#   GZETRYN_DEPLOY_TESTS=1 deploy/deploy.sh   also run the test suite on the VPS (nice -n 19) before restarting
set -euo pipefail

HOST="${GZETRYN_DEPLOY_HOST:-root@46.250.236.190}"
KEY="${GZETRYN_DEPLOY_KEY:-$HOME/.ssh/vps-contabo}"
SSH=(ssh -i "$KEY" -o BatchMode=yes "$HOST")
TESTS="${GZETRYN_DEPLOY_TESTS:-0}"

cd "$(dirname "$0")/.."
if [[ -n "$(git status --porcelain)" ]]; then
  echo "working tree not clean; commit first" >&2
  exit 1
fi
REV="$(git rev-parse --short HEAD)"

# The tree is clean, so what is on disk is HEAD. VPS-local files (.env, config/gzetryn.yaml, REVISION) are
# excluded and therefore never deleted by --delete.
rsync -az --delete --no-owner --no-group -e "ssh -i $KEY -o BatchMode=yes" \
  --exclude '.git/' --exclude '.venv/' --exclude '.env' --exclude '.env.*' --exclude '__pycache__/' \
  --exclude '.pytest_cache/' --exclude '.ruff_cache/' --exclude '.mypy_cache/' --exclude 'logs/' \
  --exclude 'samples/' --exclude 'tools/out/' --exclude 'config/gzetryn.yaml' --exclude 'REVISION' \
  ./ "$HOST:/opt/gzetryn/"

"${SSH[@]}" REV="$REV" TESTS="$TESTS" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/gzetryn
chown -R root:root /opt/gzetryn && chown root:gzetryn /opt/gzetryn/.env
printf '%s\n' "$REV" > /opt/gzetryn/REVISION
export PYTHONDONTWRITEBYTECODE=1
sudo -u gzetryn -H env UV_PROJECT_ENVIRONMENT=/var/lib/gzetryn/venv \
  /usr/local/bin/uv sync --frozen --no-dev --python 3.14 --quiet
if [[ "$TESTS" == "1" ]]; then
  sudo -u gzetryn -H env UV_PROJECT_ENVIRONMENT=/var/lib/gzetryn/venv-test \
    /usr/local/bin/uv sync --frozen --python 3.14 --quiet
  sudo -u gzetryn -H env PYTHONDONTWRITEBYTECODE=1 nice -n 19 \
    /var/lib/gzetryn/venv-test/bin/python -m pytest -q -p no:cacheprovider tests
fi
sudo -u gzetryn -H /var/lib/gzetryn/venv/bin/alembic upgrade head
install -m 644 deploy/gzetryn.service /etc/systemd/system/gzetryn.service
install -m 755 deploy/gzetryn /usr/local/bin/gzetryn
systemctl daemon-reload
systemctl enable gzetryn.service >/dev/null 2>&1 || true
systemctl restart gzetryn.service
for i in $(seq 1 30); do
  curl -sf -H 'X-Consumer: deploy' http://127.0.0.1:8793/health >/dev/null && break
  sleep 1
done
curl -s -H 'X-Consumer: deploy' http://127.0.0.1:8793/health | head -c 800; echo
echo "deployed $REV"
REMOTE
