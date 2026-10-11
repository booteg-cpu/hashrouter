#!/bin/bash
set -euo pipefail
umask 027
if [[ $EUID -ne 0 ]]; then echo 'Run with sudo on the Debian VPS.' >&2; exit 1; fi
if [[ ! -r /etc/os-release ]]; then echo 'Debian required.' >&2; exit 1; fi
. /etc/os-release
if [[ ${ID:-} != debian ]]; then echo 'This installer targets Debian.' >&2; exit 1; fi
systemctl stop hash-router 2>/dev/null || true
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
apt-get update
apt-get install -y python3 python3-venv ca-certificates openssl
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python >=3.11 required"'
if ! id hash-router >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/hash-router --shell /usr/sbin/nologin hash-router
fi
install -d -o root -g root -m 0755 /opt/hash-router
install -d -o root -g hash-router -m 0750 /etc/hash-router
install -d -o hash-router -g hash-router -m 0700 /var/lib/hash-router
# Copy only the service package, never bot credentials/state or its GUI.
install -d -o root -g root -m 0755 /opt/hash-router/hash_router
for module in "$PROJECT_DIR"/hash_router/*.py; do
  install -o root -g root -m 0644 "$module" /opt/hash-router/hash_router/
done
python3 -m venv /opt/hash-router/.venv
/opt/hash-router/.venv/bin/python -m pip install -r "$PROJECT_DIR/deploy/hash_router/requirements.txt"
# umask 027 creates root-only directories for the service unless its group is set.
# Keep the environment root-owned; the service gets read/traverse/execute only.
# -hR changes symlinks themselves, never the system Python they point to.
chgrp -hR hash-router /opt/hash-router/.venv
chmod -R g+rX,o-rwx /opt/hash-router/.venv
python3 "$PROJECT_DIR/deploy/hash_router/install_files.py" \
  "$PROJECT_DIR/deploy/hash_router/config.example.json" /etc/hash-router/config.json
chown root:hash-router /etc/hash-router/config.json
chmod 0640 /etc/hash-router/config.json
install -o root -g root -m 0644 "$PROJECT_DIR/deploy/hash_router/hash-router.service" /etc/systemd/system/hash-router.service
systemctl daemon-reload
read -r -p 'Public VPS IP or DNS name: ' PUBLIC_ROUTER_HOST
read -r -p 'HTTPS API port [8443]: ' ROUTER_API_PORT
ROUTER_API_PORT="${ROUTER_API_PORT:-8443}"
python3 "$PROJECT_DIR/deploy/hash_router/setup_connection.py" --host "$PUBLIC_ROUTER_HOST" --port "$ROUTER_API_PORT"
python3 - <<'PY'
import grp,json,os
from pathlib import Path
config=json.loads(Path('/etc/hash-router/config.json').read_text())
for path in [Path('/etc/hash-router/config.json'),Path(config['tls_key']),Path(config['tls_certificate'])]:
    if path.parent==Path('/etc/hash-router'):
        os.chown(path,0,grp.getgrnam('hash-router').gr_gid);path.chmod(0o640)
PY
ROUTER_API_PORT=$(python3 -c "import json; print(json.load(open('/etc/hash-router/config.json'))['control_port'])")
ROUTER_STRATUM_BASE=$(python3 -c "import json; print(json.load(open('/etc/hash-router/config.json'))['stratum_port'])")
ROUTER_STRATUM_END=$((ROUTER_STRATUM_BASE + 100))
if (( ROUTER_STRATUM_END > 65535 )); then ROUTER_STRATUM_END=65535; fi
(cd /opt/hash-router && runuser -u hash-router -- /opt/hash-router/.venv/bin/python -m hash_router --config /etc/hash-router/config.json --check-config)
systemctl enable hash-router
systemctl restart hash-router
systemctl is-active --quiet hash-router
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q '^Status: active'; then
  ufw allow "${ROUTER_API_PORT}/tcp"
  ufw allow "${ROUTER_STRATUM_BASE}:${ROUTER_STRATUM_END}/tcp"
fi
echo "Router started. Allow TCP ${ROUTER_API_PORT} and ${ROUTER_STRATUM_BASE}..${ROUTER_STRATUM_END} in your VPS provider firewall."
echo 'Use the VPS address, API port and access key printed above; verify the TLS fingerprint through SSH.'
echo 'SBP needs certificate-pin support for this file-free connection. See README for legacy SBP.'
