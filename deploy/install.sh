#!/usr/bin/env bash
# Installs ethtester on Raspberry Pi OS Lite (Bookworm or Trixie).
# Usage: sudo deploy/install.sh [--set key=value]... [interface]
# The interface defaults to the one in /boot/firmware/ethtester.toml, then eth0.
# --set (repeatable) writes a key into the config; allowed keys: interface, display, expected_speed,
# mcp_port, mcp_bind, mcp_clients, mcp_auth.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

CONFIG=/boot/firmware/ethtester.toml
SRC="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo." >&2
  exit 1
fi

set_config() {  # set_config key=value: replaces the key's line in $CONFIG, or appends it
  local pair="$1" key value line tmp
  case "$pair" in
    *=*) key="${pair%%=*}"; value="${pair#*=}" ;;
    *) echo "--set expects key=value, got '$pair'" >&2; exit 1 ;;
  esac
  case "$key" in
    interface|display|expected_speed|mcp_port|mcp_bind|mcp_clients|mcp_auth) ;;
    *) echo "--set: unsupported key '$key'" >&2; exit 1 ;;
  esac
  case "$value" in
    *'"'*|*'\'*|*$'\n'*) echo "--set: value for $key must not contain a quote, backslash or newline" >&2; exit 1 ;;
  esac
  case "$key" in
    expected_speed|mcp_port)
      case "$value" in
        ''|*[!0-9]*) echo "--set: $key must be all digits" >&2; exit 1 ;;
      esac
      line="$key = $value" ;;
    *) line="$key = \"$value\"" ;;
  esac
  tmp="$(mktemp)"
  KEY="$key" LINE="$line" awk 'BEGIN { k = ENVIRON["KEY"]; l = ENVIRON["LINE"]; done = 0 }
    $0 ~ "^" k " *=" { if (!done) print l; done = 1; next }
    { print }
    END { if (!done) print l }' "$CONFIG" > "$tmp"
  cat "$tmp" > "$CONFIG"
  rm -f "$tmp"
}

SETS=()
IFACE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --set) [ $# -ge 2 ] || { echo "--set needs key=value" >&2; exit 1; }; SETS+=("$2"); shift 2 ;;
    --set=*) SETS+=("${1#--set=}"); shift ;;
    -*) echo "unknown option: $1" >&2; exit 1 ;;
    *) IFACE="$1"; shift ;;
  esac
done

if [ ! -f "$CONFIG" ]; then
  cp "$SRC/deploy/ethtester.toml" "$CONFIG"
fi
for pair in "${SETS[@]+"${SETS[@]}"}"; do
  set_config "$pair"
done

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
MCP_BIND=""
if [ -f "$CONFIG" ]; then
  MCP_BIND="$(sed -n 's/^mcp_bind *= *"\(.*\)".*/\1/p' "$CONFIG")"
  MCP_BIND="${MCP_BIND%%$'\n'*}"
fi
MCP_BIND="${MCP_BIND:-tailscale}"
MCP_AUTH=""
if [ -f "$CONFIG" ]; then
  MCP_AUTH="$(sed -n 's/^mcp_auth *= *"\(.*\)".*/\1/p' "$CONFIG")"
  MCP_AUTH="${MCP_AUTH%%$'\n'*}"
fi
MCP_AUTH="${MCP_AUTH:-token}"

apt-get update
apt-get install -y python3-pil python3-spidev python3-gpiozero python3-lgpio lldpd curl fonts-dejavu-core \
  traceroute ethtool iperf3 arp-scan python3-venv nftables
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

# Keep the MCP port closed on the interface under test (the service loads this too, before it starts).
cat > /etc/ethtester/mcp-firewall.nft <<NFT
table inet ethtester_mcp
delete table inet ethtester_mcp
table inet ethtester_mcp {
  chain input {
    type filter hook input priority -10; policy accept;
    iifname "$IFACE" tcp dport $MCP_PORT drop
  }
}
NFT
nft -f /etc/ethtester/mcp-firewall.nft || echo "warning: could not apply MCP firewall rule"

cp "$SRC/deploy/ethtester.service" /etc/systemd/system/ethtester.service
cp "$SRC/deploy/ethtester-mcp.service" /etc/systemd/system/ethtester-mcp.service
systemctl daemon-reload
systemctl enable ethtester ethtester-mcp
systemctl try-restart ethtester
systemctl restart ethtester-mcp || echo "warning: ethtester-mcp did not start yet (is Tailscale up?)"

echo "Installed. On a first install, reboot to enable SPI; on a re-run the service has been restarted."
TOKEN="$(cat /etc/ethtester/mcp-token)"
print_hint() {  # print_hint host: the commands for the Mac, matching mcp_auth
  local url="http://$1:$MCP_PORT/mcp"
  echo "Add the tester to Claude Code on your Mac with:"
  if [ "$MCP_AUTH" = "none" ]; then
    echo "  claude mcp add --transport http ethtester $url"
    echo "For the ethtester CLI:"
    echo "  ethtester configure --url $url"
  else
    echo "  claude mcp add --transport http ethtester $url --header \"Authorization: Bearer $TOKEN\""
    echo "For the ethtester CLI:"
    echo "  ethtester configure --url $url   (paste the token when asked)"
  fi
}
if [ "$MCP_BIND" = "tailscale" ]; then
  TS_IP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  if [ -z "$TS_IP" ]; then
    echo "Next: run 'sudo tailscale up' on the Pi, open the link it prints, then re-run this installer."
  else
    print_hint "$TS_IP"
  fi
else
  if [[ "$MCP_BIND" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    MCP_ADDR="$MCP_BIND"
  else
    MCP_ADDR="$(ip -4 -o addr show dev "$MCP_BIND" 2>/dev/null | sed -n 's/.* inet \([0-9.]*\)\/.*/\1/p' | head -n1 || true)"
  fi
  if [ -z "$MCP_ADDR" ]; then
    echo "Next: $MCP_BIND has no IPv4 address yet; connect the Pi to Wi-Fi and re-run this installer."
  else
    print_hint "$MCP_ADDR"
  fi
fi
