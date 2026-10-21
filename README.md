# ethtester

A Pi Zero with a USB Ethernet dongle and a Waveshare 2.13" e-ink HAT. Plug it into a wall socket or switch port, and the screen shows how far the connection gets and what is most likely at fault.

| Screen says | Likely cause |
|---|---|
| `NO LINK` | Cable, wall plug, or dead switch port |
| `CABLE? (100M)` | Damaged cable/termination (gigabit fell back to 100) |
| `FLAPPING` | Loose plug or worn connector |
| `NO DHCP` | Router/DHCP, or port on the wrong VLAN |
| `GATEWAY` | Router or VLAN configuration |
| `DNS` | DNS server |
| `NO INTERNET` | Router's uplink |

## Setup

1. Flash **Raspberry Pi OS Lite (64-bit, Bookworm or Trixie)** with Raspberry Pi Imager (use 32-bit on an original Pi Zero / Zero W). Enable SSH, set a user and, on Wi-Fi models, add your Wi-Fi so you can SSH in while the Ethernet port is under test.
2. Boot the Pi and copy this folder over:
   `scp -r ethernet-tester <user>@<pi-ip>:`
3. On the Pi, run `sudo ethernet-tester/deploy/install.sh`
4. In `/boot/firmware/ethtester.toml`, check that `display` matches the sticker on the back of the HAT (default `"V4"`). If your dongle is a 10/100 model, also set `expected_speed = 100`.
5. Reboot.

To update, or after changing `interface` in the config, copy the folder over again and re-run `install.sh`; it updates the network profile and restarts the service, so no reboot is needed.

Logs: `journalctl -u ethtester` for errors, and `/var/log/ethtester/ethtester.log` for the state history.

Limitations: it can't measure PoE (use an inline PoE tester). LLDP needs a managed switch.

## Development (Mac)

```
python3 -m venv .venv && .venv/bin/pip install pillow pytest
.venv/bin/pytest
```

Sample screens are written to `tests/output/`.

## Remote probes (MCP)

The tester runs an MCP server, so Claude Code can read its status and run probes through the Ethernet port from anywhere. It's reachable only over Tailscale and needs a token.

1. Re-run the installer (`sudo ethernet-tester/deploy/install.sh`). It installs Tailscale and creates the token.
2. On the Pi, run `sudo tailscale up` and open the link it prints. Then re-run the installer once more: it prints the `claude mcp add …` command to use.
3. On your Mac, install Tailscale, sign in to the same account, and run that `claude mcp add` command.

**Home Wi-Fi only (no Tailscale).** Set `mcp_bind = "wlan0"` and `mcp_clients = "lan"` in `/boot/firmware/ethtester.toml`, re-run the installer, and use the `claude mcp add …` command it prints. The token is still required. This only works while your Mac is on the same home network. Give the Pi's Wi-Fi address a DHCP reservation on your router so the URL doesn't change.

Tools: `get_status`, `get_history`, `get_link_details`, `ping`, `traceroute`, `dns_lookup`, `http_check`, `dhcp_renew`, `link_control`, `neighbour_scan`, `speed_test`.

Every call is logged to `/var/log/ethtester/probes.log`. To rotate the token, delete `/etc/ethtester/mcp-token` and re-run the installer.

## Command-line client (Mac)

`scripts/ethtester` talks to the MCP server over plain HTTP, so Claude Code can call it through Bash. It needs only Python 3.11+ (no dependencies).

```
ln -s "$PWD/scripts/ethtester" ~/.local/bin/ethtester
ethtester configure --url http://192.168.1.190:8765/mcp   # paste the token from the installer when asked
```

The settings are saved to `~/.config/ethtester/cli.toml` (mode 0600). `ETHTESTER_URL`, `ETHTESTER_TOKEN` and `ETHTESTER_CONFIG` override them, and so do `--url` and `--token`.

```
ethtester status
ethtester ping 1.1.1.1 -c 10
ethtester dns example.com -t AAAA --server 1.1.1.1
ethtester link-control force-10 --duration 30
ethtester speedtest 192.168.1.50 --reverse
ethtester tools
```

Output is JSON. Exit codes: 0 OK, 1 tool error, 2 connection or config error. `ethtester --help` lists every command.
