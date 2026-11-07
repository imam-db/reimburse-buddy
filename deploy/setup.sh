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
SVC_USER=reimburse
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

echo "==> User and directories"
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE_DIR/home" --create-home --shell /usr/sbin/nologin "$SVC_USER"
fi
install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 "$STATE_DIR" "$STATE_DIR/home" "$STATE_DIR/home/.cache"
for inst in app demo; do
  install -d -o "$SVC_USER" -g "$SVC_USER" -m 750 \
    "$STATE_DIR/$inst" "$STATE_DIR/$inst/plans" "$STATE_DIR/$inst/uploads" "$STATE_DIR/$inst/agent-runs"
done
install -d -o root -g root -m 755 "$ENV_DIR"
chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR"

echo "==> Python environments (as $SVC_USER)"
as_svc() { runuser -u "$SVC_USER" -- env HOME="$STATE_DIR/home" UV_CACHE_DIR="$STATE_DIR/home/.cache/uv" "$@"; }
as_svc "$UV_BIN" --directory "$INSTALL_DIR" sync --frozen --group rapidocr
as_svc "$UV_BIN" --directory "$INSTALL_DIR/agent" sync --frozen --extra postgres

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
