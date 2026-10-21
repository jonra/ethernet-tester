#!/usr/bin/env bash
# Installs ethtester on Raspberry Pi OS Lite (Bookworm or Trixie). Usage: sudo deploy/install.sh [interface]
# The interface defaults to the one in /boot/firmware/ethtester.toml, then eth0.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

CONFIG=/boot/firmware/ethtester.toml
IFACE="${1:-}"
if [ -z "$IFACE" ] && [ -f "$CONFIG" ]; then
  IFACE="$(sed -n 's/^interface *= *"\(.*\)".*/\1/p' "$CONFIG")"
  IFACE="${IFACE%%$'\n'*}"
fi
IFACE="${IFACE:-eth0}"
MCP_PORT=""
if [ -f "$CONFIG" ]; then
  MCP_PORT="$(sed -n 's/^mcp_port *= *\([0-9]*\).*/\1/p' "$CONFIG")"
  MCP_PORT="${MCP_PORT%%$'\n'*}"
fi
MCP_PORT="${MCP_PORT:-8765}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo." >&2
  exit 1
fi

apt-get update
apt-get install -y python3-pil python3-spidev python3-gpiozero python3-lgpio lldpd curl fonts-dejavu-core \
  traceroute ethtool iperf3 arp-scan python3-venv
if ! command -v tailscale >/dev/null; then
  CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
  curl -fsSL "https://pkgs.tailscale.com/stable/debian/${CODENAME}.noarmor.gpg" \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/debian/${CODENAME}.tailscale-keyring.list" \
    -o /etc/apt/sources.list.d/tailscale.list
  apt-get update
  apt-get install -y tailscale
fi
systemctl enable --now tailscaled

raspi-config nonint do_spi 0
systemctl enable --now lldpd

NM_SETTINGS=(connection.interface-name "$IFACE" ipv4.method auto ipv4.dhcp-timeout 2147483647
  ipv6.method ignore connection.autoconnect-retries 0 connection.autoconnect-priority 10
  ipv4.route-metric 2000 ipv4.dns-priority 200)
if nmcli -t -f NAME connection show | grep -x ethtester >/dev/null; then
  nmcli connection modify ethtester "${NM_SETTINGS[@]}"
else
  nmcli connection add type ethernet con-name ethtester "${NM_SETTINGS[@]}"
fi

mkdir -p /opt/ethtester /var/log/ethtester
rm -rf /opt/ethtester/ethtester
cp -r "$SRC/ethtester" /opt/ethtester/
find /opt/ethtester -name __pycache__ -prune -exec rm -rf {} +
if [ ! -x /opt/ethtester/venv/bin/python ]; then
  python3 -m venv --system-site-packages /opt/ethtester/venv
fi
/opt/ethtester/venv/bin/pip install --quiet "mcp==1.30.0" "uvicorn>=0.30"

install -d -m 700 /etc/ethtester
if [ ! -s /etc/ethtester/mcp-token ]; then
  (umask 077 && python3 -c 'import secrets; print(secrets.token_hex(32))' > /etc/ethtester/mcp-token)
fi

if [ ! -f "$CONFIG" ]; then
  cp "$SRC/deploy/ethtester.toml" "$CONFIG"
fi

cp "$SRC/deploy/ethtester.service" /etc/systemd/system/ethtester.service
cp "$SRC/deploy/ethtester-mcp.service" /etc/systemd/system/ethtester-mcp.service
systemctl daemon-reload
systemctl enable ethtester ethtester-mcp
systemctl try-restart ethtester
systemctl restart ethtester-mcp || echo "warning: ethtester-mcp did not start yet (is Tailscale up?)"

echo "Installed. On a first install, reboot to enable SPI; on a re-run the service has been restarted."
TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
if [ -z "$TS_IP" ]; then
  echo "Next: run 'sudo tailscale up' on the Pi, open the link it prints, then re-run this installer."
else
  echo "Add the tester to Claude Code on your Mac with:"
  echo "  claude mcp add --transport http ethtester http://$TS_IP:$MCP_PORT/mcp --header \"Authorization: Bearer $(cat /etc/ethtester/mcp-token)\""
fi
