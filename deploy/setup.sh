#!/usr/bin/env bash
# Prepare a fresh Ubuntu 22.04/24.04 VPS for Reimburse Buddy (ADR 0013, docs/deploy.md).
#
# Run as root from the checkout at /opt/reimburse-buddy:
#   git clone <repo-url> /opt/reimburse-buddy && sudo /opt/reimburse-buddy/deploy/setup.sh
#
# Safe to re-run. It never writes env values: it only checks that /etc/reimburse-buddy/{app,demo}.env exist
# and, if not, stops before starting any service and lists the missing ones.
set -euo pipefail

INSTALL_DIR=/opt/reimburse-buddy
STATE_DIR=/var/lib/reimburse-buddy
ENV_DIR=/etc/reimburse-buddy
UV_PYTHON_DIR=/opt/uv-python
BUILD_CACHE=/var/cache/reimburse-buddy-build
UV_BIN=/usr/local/bin/uv

if [[ $EUID -ne 0 ]]; then
  echo "setup.sh must run as root (try: sudo $0)" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
if [[ "$(readlink -f "$REPO")" != "$INSTALL_DIR" ]]; then
  echo "The checkout must live at $INSTALL_DIR (the systemd units use that path); this one is at $REPO." >&2
  exit 1
fi

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# libgl1 and libglib2.0-0 are needed by OpenCV, which RapidOCR pulls in.
apt-get install -y -qq curl ca-certificates git ufw debian-keyring debian-archive-keyring apt-transport-https \
  gpg libgl1 libglib2.0-0

echo "==> uv"
if [[ ! -x "$UV_BIN" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

echo "==> Caddy"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes \
    -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    -o /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi

echo "==> Users and directories"
# One system user per instance, each with its own state root: a compromised demo cannot read the app's data.
# A legacy shared "reimburse" user from an older install is left alone (see docs/deploy.md, migration).
for inst in app demo; do
  u="reimburse-$inst"
  if ! id "$u" >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_DIR/$inst/home" --no-create-home --shell /usr/sbin/nologin "$u"
  fi
done
install -d -o root -g root -m 755 "$STATE_DIR"
for inst in app demo; do
  u="reimburse-$inst"
  install -d -o "$u" -g "$u" -m 750 \
    "$STATE_DIR/$inst" "$STATE_DIR/$inst/home" "$STATE_DIR/$inst/home/.cache" \
    "$STATE_DIR/$inst/plans" "$STATE_DIR/$inst/uploads" "$STATE_DIR/$inst/agent-runs"
done
install -d -o root -g root -m 755 "$ENV_DIR"
# Code and virtualenvs stay root-owned: the service users can read and execute them, never change them.
chown -R root:root "$INSTALL_DIR"
install -d -o root -g root -m 755 "$UV_PYTHON_DIR"
install -d -o root -g root -m 700 "$BUILD_CACHE"

echo "==> Python environments (as root, read-only for the services)"
umask 022
# A virtualenv from an older install points at a Python under the old user's home, which the new service users
# cannot read: rebuild any venv whose interpreter is not under $UV_PYTHON_DIR.
for venv in "$INSTALL_DIR/.venv" "$INSTALL_DIR/agent/.venv"; do
  if [[ -e "$venv" ]]; then
    py="$(readlink -f "$venv/bin/python" 2>/dev/null || true)"
    if [[ "$py" != "$UV_PYTHON_DIR"/* ]]; then
      echo "    rebuilding $venv (interpreter ${py:-missing} is outside $UV_PYTHON_DIR)"
      rm -rf "$venv"
    fi
  fi
done
as_root() { env UV_PYTHON_INSTALL_DIR="$UV_PYTHON_DIR" UV_CACHE_DIR="$BUILD_CACHE" "$@"; }
as_root "$UV_BIN" --directory "$INSTALL_DIR" sync --frozen --group rapidocr
as_root "$UV_BIN" --directory "$INSTALL_DIR/agent" sync --frozen --extra postgres
chmod -R go+rX,go-w "$INSTALL_DIR/.venv" "$INSTALL_DIR/agent/.venv" "$UV_PYTHON_DIR"

echo "==> systemd units and Caddyfile"
install -m 644 "$HERE/reimburse-app.service" /etc/systemd/system/reimburse-app.service
install -m 644 "$HERE/reimburse-demo.service" /etc/systemd/system/reimburse-demo.service
install -m 644 "$HERE/Caddyfile" /etc/caddy/Caddyfile
systemctl daemon-reload

echo "==> Firewall (22, 80, 443 only)"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

missing=()
for inst in app demo; do
  [[ -f "$ENV_DIR/$inst.env" ]] || missing+=("$ENV_DIR/$inst.env")
done
if (( ${#missing[@]} )); then
  echo >&2
  echo "Stopped before starting any service. Missing env files:" >&2
  printf '  %s\n' "${missing[@]}" >&2
  echo "Create each from deploy/env.{app,demo}.example, then: chown root:root FILE && chmod 600 FILE" >&2
  echo "Run the database steps in docs/deploy.md, then run this script again." >&2
  exit 2
fi
chown root:root "$ENV_DIR/app.env" "$ENV_DIR/demo.env"
chmod 600 "$ENV_DIR/app.env" "$ENV_DIR/demo.env"

echo "==> Start"
systemctl enable --now reimburse-app reimburse-demo
systemctl reload caddy || systemctl restart caddy
echo "Done. Check: curl -fsS http://127.0.0.1:8000/healthz && curl -fsS http://127.0.0.1:8001/healthz"
