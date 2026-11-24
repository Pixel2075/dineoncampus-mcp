#!/usr/bin/env bash
# Install dineoncampus-mcp as a public HTTPS MCP server on Ubuntu 24.04+ / Debian 12+.
#
# The server runs in snapshot mode: DineOnCampus blocks most cloud IPs, so a home machine
# fetches the menus with `dineoncampus-mcp sync` and pushes them here. Caddy serves HTTPS
# with a Let's Encrypt certificate, so ports 80 and 443 must be reachable from the internet.
#
# Re-running the script upgrades the app and keeps the existing ingest token.
set -euo pipefail

REPO="https://github.com/Pixel2075/dineoncampus-mcp"
REF="main"
DOMAIN=""
LOCATION_ID=""
TZ_NAME="America/New_York"
PORT=8000
APP_DIR=/opt/dineoncampus-mcp
DATA_DIR=/var/lib/dineoncampus-mcp
ENV_FILE=/etc/dineoncampus-mcp.env
UNIT_FILE=/etc/systemd/system/dineoncampus-mcp.service
CADDYFILE=/etc/caddy/Caddyfile
SITE_FILE=/etc/caddy/dineoncampus-mcp.caddy
SERVICE_USER=dineoncampus

usage() {
  cat <<'EOF'
Usage: sudo bash install-server.sh --location-id ID [--tz ZONE] [--domain HOST] [--ref GIT_REF]

  --location-id  DineOnCampus location id the server accepts snapshots for
                 (find it with: dineoncampus-mcp find "<school name>")
  --tz           dining hall timezone, IANA name (default America/New_York)
  --domain       public hostname pointing at this machine
                 (default <public-ip>.sslip.io, which needs no DNS setup)
  --ref          git branch, tag or commit to install (default main)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --location-id) LOCATION_ID="${2:-}"; shift 2 ;;
    --tz) TZ_NAME="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --ref) REF="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root, e.g. with sudo"
[[ "$LOCATION_ID" =~ ^[0-9a-f]{24}$ ]] || die "--location-id must be a 24-character DineOnCampus id"
[ -f "/usr/share/zoneinfo/$TZ_NAME" ] || die "unknown timezone $TZ_NAME"
[[ "$REF" =~ ^[A-Za-z0-9._/-]+$ ]] || die "invalid --ref"

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv git curl openssl caddy >/dev/null

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || die "Python 3.11+ is required (found $(python3 --version)); use Ubuntu 24.04 or newer"

if [ -z "$DOMAIN" ]; then
  IP=$(curl -fsS -m 10 https://checkip.amazonaws.com | tr -d '[:space:]')
  [[ "$IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "could not detect the public IPv4 address; pass --domain"
  DOMAIN="${IP//./-}.sslip.io"
fi
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || die "invalid --domain"

echo "==> Installing dineoncampus-mcp ($REF) into $APP_DIR"
id -u "$SERVICE_USER" >/dev/null 2>&1 \
  || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$DATA_DIR"
[ -x "$APP_DIR/bin/python" ] || python3 -m venv "$APP_DIR"
"$APP_DIR/bin/pip" install -q --upgrade pip
"$APP_DIR/bin/pip" install -q --upgrade --force-reinstall "dineoncampus-mcp @ git+${REPO}@${REF}"

echo "==> Writing $ENV_FILE"
TOKEN=""
if [ -f "$ENV_FILE" ]; then
  TOKEN=$(sed -n 's/^DINING_INGEST_TOKEN=//p' "$ENV_FILE")
fi
[ -n "$TOKEN" ] || TOKEN=$(openssl rand -hex 32)
umask 027
cat > "$ENV_FILE" <<EOF
DINING_SOURCE=snapshots
DINING_TRANSPORT=http
DINING_HOST=127.0.0.1
DINING_PORT=$PORT
DINING_ALLOWED_HOSTS=$DOMAIN
DINING_TZ=$TZ_NAME
DINING_DATA_DIR=$DATA_DIR
DINING_LOCATION_ID=$LOCATION_ID
DINING_INGEST_TOKEN=$TOKEN
EOF
chown "root:$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"
umask 022

echo "==> Writing systemd unit"
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=DineOnCampus MCP server
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/bin/dineoncampus-mcp serve
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

echo "==> Configuring Caddy for https://$DOMAIN"
cat > "$SITE_FILE" <<EOF
# Managed by dineoncampus-mcp deploy/install-server.sh
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
EOF
if [ ! -f "$CADDYFILE" ] || grep -q "/usr/share/caddy" "$CADDYFILE"; then
  # Stock placeholder config from the caddy package: replace it, keeping a copy.
  [ -f "$CADDYFILE" ] && cp -n "$CADDYFILE" "$CADDYFILE.orig"
  echo "import $SITE_FILE" > "$CADDYFILE"
elif ! grep -qF "import $SITE_FILE" "$CADDYFILE"; then
  # Someone else's config: leave it alone and add ours alongside.
  printf '\nimport %s\n' "$SITE_FILE" >> "$CADDYFILE"
fi
caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1 \
  || die "Caddy rejected $CADDYFILE; run: caddy validate --config $CADDYFILE"

echo "==> Starting services"
systemctl daemon-reload
systemctl enable dineoncampus-mcp >/dev/null 2>&1
systemctl restart dineoncampus-mcp
systemctl enable caddy >/dev/null 2>&1
systemctl reload-or-restart caddy

for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  journalctl -u dineoncampus-mcp -n 40 --no-pager >&2 || true
  die "the server did not come up on port $PORT"
fi

PUBLIC_OK=no
for _ in $(seq 1 12); do
  if curl -fsS -m 10 "https://$DOMAIN/health" >/dev/null 2>&1; then PUBLIC_OK=yes; break; fi
  sleep 5
done

cat <<EOF

dineoncampus-mcp is running.

  MCP URL (add this to Claude):  https://$DOMAIN/mcp
  Ingest token:                  $TOKEN
  Token file:                    $ENV_FILE
  Logs:                          journalctl -u dineoncampus-mcp -f

EOF
if [ "$PUBLIC_OK" = yes ]; then
  echo "https://$DOMAIN/health answers from the internet."
else
  cat <<EOF
WARNING: https://$DOMAIN/health did not answer yet. Check that inbound TCP 80 and 443 are
open (on AWS: the instance's security group) and look at: journalctl -u caddy -n 50
EOF
fi
cat <<EOF

The server has no menus until a home machine pushes them. On that machine:

  uv tool install git+$REPO
  dineoncampus-mcp sync --server https://$DOMAIN --location-id $LOCATION_ID --site-id <site id> --tz $TZ_NAME
      (with DINING_INGEST_TOKEN=$TOKEN in the environment)

or on Windows run deploy/windows-sync-task.ps1 to schedule it. See the README.
EOF
