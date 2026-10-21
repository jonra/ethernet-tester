"""Command-line client for the ethtester MCP server (stdlib only; runs on any Python 3.11+)."""

import argparse
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TIMEOUT = 120.0
DEFAULT_CONFIG = Path("~/.config/ethtester/cli.toml")
EXIT_OK, EXIT_TOOL_ERROR, EXIT_CONNECTION = 0, 1, 2

EPILOG = """\
Runs network probes on the ethtester (a Raspberry Pi) over its MCP server. Every probe goes out
through the tester's Ethernet port only, never Wi-Fi, so results describe the cable/port it is
plugged into. Start with 'status'. On a 10/100 dongle ('link' shows its modes) a 100M link is normal.

Output is JSON on stdout (pretty-printed). Errors go to stderr.
Exit codes: 0 OK, 1 tool error (the probe failed, e.g. no link), 2 connection/config error
(unreachable, bad token, missing url).

'renew-dhcp' and 'link-control' briefly disturb the tested port: the tester's own screen and
'history' show NO DHCP / CABLE? / extra flaps for a short time. Treat those as caused by the test.
'link-control' forced speeds revert to autonegotiation automatically. renew-dhcp, link-control and
neighbours can't run at the same time. speedtest needs 'iperf3 -s' running on SERVER.

Setup: 'ethtester configure --url http://HOST:8765/mcp' (the token is read from stdin if --token is
omitted; leave it empty if the tester runs with mcp_auth = "none", then no Authorization header is sent). Settings resolve as: --url/--token flags, then ETHTESTER_URL/ETHTESTER_TOKEN env,
then ~/.config/ethtester/cli.toml (override the path with ETHTESTER_CONFIG).

Examples:
  ethtester status
  ethtester ping 1.1.1.1 -c 10
  ethtester dns example.com -t AAAA --server 1.1.1.1
  ethtester link-control force-10 --duration 30
  ethtester speedtest 192.168.1.50 --seconds 10 --reverse
"""


class CliError(Exception):
    def __init__(self, message: str, code: int = EXIT_CONNECTION):
        super().__init__(message)
        self.code = code


# --- transport -------------------------------------------------------------------------------

def rpc(url: str, token: str, method: str, params: dict, timeout: float = DEFAULT_TIMEOUT,
        opener=urllib.request.urlopen) -> dict:
    """POST one JSON-RPC request and return the decoded response object."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise CliError("unauthorized: check the token (ethtester configure)") from None
        raise CliError(f"cannot reach {url}: HTTP {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise CliError(f"cannot reach {url}: {reason}") from None
    return _decode(raw, url)


def _decode(raw: str, url: str) -> dict:
    text = raw.strip()
    if text.startswith(("event:", "data:")):  # event-stream framing
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = data[-1] if data else ""
    try:
        message = json.loads(text)
    except ValueError:
        raise CliError(f"cannot reach {url}: not a JSON-RPC response") from None
    if not isinstance(message, dict):
        raise CliError(f"cannot reach {url}: not a JSON-RPC response")
    if "error" in message:
        err = message["error"]
        detail = err.get("message", err) if isinstance(err, dict) else err
        raise CliError(f"server error: {detail}")
    return message.get("result") or {}


def call_tool(url, token, name, arguments, timeout=DEFAULT_TIMEOUT, opener=urllib.request.urlopen) -> dict:
    return rpc(url, token, "tools/call", {"name": name, "arguments": arguments}, timeout, opener)


def result_text(result: dict) -> str:
    content = result.get("content") or []
    return content[0].get("text", "") if content else ""


def format_output(text: str) -> str:
    try:
        return json.dumps(json.loads(text), indent=2)
    except ValueError:
        return text


# --- config ----------------------------------------------------------------------------------

def config_path(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("ETHTESTER_CONFIG") or DEFAULT_CONFIG).expanduser()


def load_file_config(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CliError(f"cannot read config {path}: {exc}") from None


def resolve_settings(url=None, token=None, env=None) -> tuple[str, str | None]:
    env = os.environ if env is None else env
    file_cfg = load_file_config(config_path(env))
    url = url or env.get("ETHTESTER_URL") or file_cfg.get("url")
    token = token or env.get("ETHTESTER_TOKEN") or file_cfg.get("token")
    if not url:
        raise CliError("missing url: run 'ethtester configure --url URL' (or set ETHTESTER_URL)")
    return str(url), str(token) if token else None


def write_config(path: Path, url: str, token: str | None) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = f"url = {json.dumps(url)}\n"
    if token:
        content += f"token = {json.dumps(token)}\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    os.chmod(path, 0o600)  # also tightens a pre-existing file


# --- commands --------------------------------------------------------------------------------

def _opt(**values):
    return {k: v for k, v in values.items() if v is not None}


TOOL_ARGS = {
    "status": lambda a: ("get_status", {}),
    "history": lambda a: ("get_history", _opt(limit=a.limit, since=a.since)),
    "link": lambda a: ("get_link_details", {}),
    "ping": lambda a: ("ping", {"host": a.host, **_opt(count=a.count, size=a.size)}),
    "traceroute": lambda a: ("traceroute", {"host": a.host, **_opt(max_hops=a.max_hops)}),
    "dns": lambda a: ("dns_lookup", {"name": a.name, **_opt(record_type=a.type, server=a.server)}),
    "http": lambda a: ("http_check", {"url": a.target}),
    "renew-dhcp": lambda a: ("dhcp_renew", {}),
    "link-control": lambda a: ("link_control", {"action": a.action.replace("-", "_"),
                                                **_opt(duration_s=a.duration)}),
    "neighbours": lambda a: ("neighbour_scan", {}),
    "speedtest": lambda a: ("speed_test", {"server": a.server,
                                           **_opt(seconds=a.seconds), **({"reverse": True} if a.reverse else {})}),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ethtester", description="Command-line client for the ethtester network tester (MCP server on a Raspberry Pi).",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="MCP endpoint, e.g. http://192.168.1.190:8765/mcp (env ETHTESTER_URL)")
    p.add_argument("--token", help="bearer token, if the tester needs one (env ETHTESTER_TOKEN)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT:.0f})")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name, help_text):
        return sub.add_parser(name, help=help_text, description=help_text)

    add("status", "current verdict, every check row, and data age (stale if > 30 s)")
    s = add("history", "recent state changes (verdict, link, IP, switch port)")
    s.add_argument("--limit", type=int, help="number of entries, 1-200 (server default 50)")
    s.add_argument("--since", help="only entries after this ISO time, e.g. 2026-01-31T12:00:00")
    add("link", "speed, duplex, autonegotiation, advertised/partner modes, NIC error counters")
    s = add("ping", "ping a host through the Ethernet port")
    s.add_argument("host")
    s.add_argument("-c", "--count", type=int, help="packets, 1-20 (default 5)")
    s.add_argument("-s", "--size", type=int, help="payload bytes, 16-1472 (default 56)")
    s = add("traceroute", "trace the route to a host through the Ethernet port")
    s.add_argument("host")
    s.add_argument("--max-hops", type=int, help="1-30 (default 20)")
    s = add("dns", "resolve a name via the DHCP DNS server or a given IPv4 server")
    s.add_argument("name")
    s.add_argument("-t", "--type", help="record type: A, AAAA, CNAME, MX, NS, TXT (default A)")
    s.add_argument("--server", help="IPv4 DNS server to ask instead of the DHCP one")
    s = add("http", "fetch an http(s) URL: status, DNS/connect/TLS/first-byte timings, cert expiry")
    s.add_argument("target", metavar="URL")
    add("renew-dhcp", "drop and re-request the DHCP lease (briefly disturbs the port)")
    s = add("link-control", "renegotiate the link or force 10/100 Mbit for a while (briefly disturbs the port)")
    s.add_argument("action", choices=["renegotiate", "force-10", "force-100"])
    s.add_argument("--duration", type=int, help="seconds, 10-300 (default 60); speed reverts automatically")
    add("neighbours", "ARP-scan the local subnet (/22 or smaller): IP, MAC, vendor")
    s = add("speedtest", "iperf3 throughput to a server running 'iperf3 -s'")
    s.add_argument("server")
    s.add_argument("--seconds", type=int, help="3-30 (default 10)")
    s.add_argument("--reverse", action="store_true", help="download instead of upload")
    add("tools", "list the server's tools with a one-line description")
    s = add("configure", "save the server URL and token to the config file")
    s.add_argument("--url", dest="cfg_url", required=True, help="MCP endpoint URL")
    s.add_argument("--token", dest="cfg_token", help="bearer token (read from stdin if omitted; empty means none)")
    return p


def _configure(args, stdin) -> int:
    token = args.cfg_token
    if token is None:
        if stdin.isatty():
            sys.stderr.write("token (leave empty for none): ")
            sys.stderr.flush()
        token = stdin.readline().strip()
    path = config_path()
    write_config(path, args.cfg_url, token)
    print(f"saved config to {path}")
    return EXIT_OK


def _tools(url, token, timeout, opener) -> int:
    result = rpc(url, token, "tools/list", {}, timeout, opener)
    for tool in result.get("tools", []):
        first = (tool.get("description") or "").strip().split("\n")[0]
        print(f"{tool['name']}: {first}")
    return EXIT_OK


def main(argv=None, opener=urllib.request.urlopen, stdin=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "configure":
            return _configure(args, stdin or sys.stdin)
        url, token = resolve_settings(args.url, args.token)
        if args.command == "tools":
            return _tools(url, token, args.timeout, opener)
        name, arguments = TOOL_ARGS[args.command](args)
        result = call_tool(url, token, name, arguments, args.timeout, opener)
    except CliError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    text = result_text(result)
    if result.get("isError"):
        print(text, file=sys.stderr)
        return EXIT_TOOL_ERROR
    print(format_output(text))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
