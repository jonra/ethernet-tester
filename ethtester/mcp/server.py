"""MCP server: FastMCP over streamable HTTP, token-gated, listening on the Tailscale address (or a chosen Wi-Fi interface) only."""

import argparse
import functools
import ipaddress
import logging
import re
from pathlib import Path

import anyio
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from ..checks.command import CheckError, run_command
from ..config import DEFAULT_CONFIG_PATH, Config, load_config
from . import validate
from .auth import BearerAuth, read_token
from .linkctl import LinkControl
from .probelog import ProbeLog
from .service import ProbeService

log = logging.getLogger("ethtester.mcp")

DEFAULT_TOKEN_PATH = Path("/etc/ethtester/mcp-token")
INSTRUCTIONS = """Ethernet tester on a Raspberry Pi. Every probe runs through its Ethernet dongle only,
never Wi-Fi, so results describe the cable/port it is plugged into. Start with get_status. The dongle is
100 Mbit only, so 100M links are normal. dhcp_renew, link_control and neighbour_scan disturb the link
briefly and can't run at the same time; forced speeds revert to autonegotiation automatically.
dhcp_renew and link_control briefly show up on the tester's own screen and in get_history (e.g. NO DHCP,
CABLE? (10M), extra flaps); treat those entries as caused by the test."""


def build_app(service, token: str, clients: str = "tailscale"):
    mcp = FastMCP("ethtester", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
                  transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    async def call(fn, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))

    @mcp.tool()
    async def get_status() -> dict:
        """Current verdict, every check row, and how old the data is (stale if > 30 s)."""
        return await call(service.get_status)

    @mcp.tool()
    async def get_history(limit: int = 50, since: str | None = None) -> dict:
        """Recent state changes (verdict, link, IP, switch port); limit 1-200, since = ISO time."""
        return await call(service.get_history, limit=limit, since=since)

    @mcp.tool()
    async def get_link_details() -> dict:
        """Speed, duplex, autonegotiation, advertised and link-partner modes, and NIC error counters."""
        return await call(service.get_link_details)

    @mcp.tool()
    async def ping(host: str, count: int = 5, size: int = 56) -> dict:
        """Ping a host through the Ethernet port; count 1-20, size 16-1472 bytes."""
        return await call(service.ping, host=host, count=count, size=size)

    @mcp.tool()
    async def traceroute(host: str, max_hops: int = 20) -> dict:
        """Trace the route to a host through the Ethernet port; max_hops 1-30."""
        return await call(service.traceroute, host=host, max_hops=max_hops)

    @mcp.tool()
    async def dns_lookup(name: str, record_type: str = "A", server: str | None = None) -> dict:
        """Resolve a name (A, AAAA, CNAME, MX, NS, TXT) via the DHCP DNS server or an IPv4 server."""
        return await call(service.dns_lookup, name=name, record_type=record_type, server=server)

    @mcp.tool()
    async def http_check(url: str) -> dict:
        """Fetch an http(s) URL: status, DNS/connect/TLS/first-byte timings, certificate expiry."""
        return await call(service.http_check, url=url)

    @mcp.tool()
    async def dhcp_renew() -> dict:
        """Drop and re-request the DHCP lease; reports seconds to IP and the new lease."""
        return await call(service.dhcp_renew)

    @mcp.tool()
    async def link_control(action: str, duration_s: int = 60) -> dict:
        """renegotiate, force_10 or force_100 for 10-300 s. Forced modes keep autonegotiation on and
        advertise only that speed at full duplex; full advertising is restored automatically."""
        return await call(service.link_control, action=action, duration_s=duration_s)

    @mcp.tool()
    async def neighbour_scan() -> dict:
        """ARP-scan the local subnet (/22 or smaller): IP, MAC and vendor of each device found."""
        return await call(service.neighbour_scan)

    @mcp.tool()
    async def speed_test(server: str, seconds: int = 10, reverse: bool = False) -> dict:
        """iperf3 throughput to a server running 'iperf3 -s'; seconds 3-30; reverse = download."""
        return await call(service.speed_test, server=server, seconds=seconds, reverse=reverse)

    return BearerAuth(mcp.streamable_http_app(), token, clients)


IFACE_RE = re.compile(r"[a-zA-Z0-9_.-]{1,15}")
INET_RE = re.compile(r"\binet (\d+\.\d+\.\d+\.\d+)/")


def interface_address(name: str, run=run_command) -> str | None:
    match = INET_RE.search(run(["ip", "-4", "-o", "addr", "show", "dev", name], check=False))
    return validate.ipv4(match.group(1)) if match else None


def _bind_to_interface(cfg: Config, name: str, run) -> str:
    if not IFACE_RE.fullmatch(name):
        raise ValueError(f"mcp_bind must be 'tailscale', an IPv4 address or an interface name, got {name!r}")
    if name == cfg.interface:
        raise ValueError("won't serve MCP on the interface under test")
    addr = interface_address(name, run)
    if addr is None:
        raise SystemExit(f"{name} has no IPv4 address yet; connect it to Wi-Fi (will retry)")
    return addr


def resolve_bind(cfg: Config, run=run_command) -> str:
    if cfg.mcp_bind != "tailscale":
        try:
            addr = validate.ipv4(cfg.mcp_bind)
        except ValueError:
            addr = _bind_to_interface(cfg, cfg.mcp_bind, run)
        if ipaddress.IPv4Address(addr).is_unspecified:
            raise ValueError("mcp_bind must not be 0.0.0.0; use a specific address or 'tailscale'")
        try:
            test_addr = interface_address(cfg.interface, run)
        except CheckError:
            test_addr = None
        if test_addr == addr:
            raise ValueError("won't serve MCP on the interface under test")
        return addr
    try:
        addresses = run(["tailscale", "ip", "-4"], check=False).split()
    except CheckError as exc:
        raise SystemExit(f"Tailscale isn't available ({exc}); install and start Tailscale on the Pi") from None
    if not addresses:
        raise SystemExit("Tailscale has no IPv4 address yet; run 'sudo tailscale up' on the Pi")
    addr = validate.ipv4(addresses[0])
    if ipaddress.IPv4Address(addr).is_unspecified:
        raise SystemExit("Tailscale reported 0.0.0.0; refusing to listen on all interfaces")
    return addr


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ethtester.mcp")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    token = read_token(args.token_file)
    linkctl = LinkControl(cfg.interface)
    linkctl.recover()  # restore a forced link first, even if Tailscale isn't up yet
    bind = resolve_bind(cfg)
    probelog = ProbeLog(Path(cfg.log_path).with_name("probes.log"))
    app = build_app(ProbeService(cfg, linkctl, probelog=probelog), token, cfg.mcp_clients)
    log.info("MCP server listening on http://%s:%d/mcp", bind, cfg.mcp_port)
    try:
        uvicorn.run(app, host=bind, port=cfg.mcp_port, log_level="info")
    finally:
        linkctl._restore_quietly()
