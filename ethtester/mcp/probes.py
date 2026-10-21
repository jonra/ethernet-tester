"""Probe commands and their output parsers. Commands are argv lists, never shell strings."""

import json
import re
import time
from dataclasses import asdict

from ..checks.command import run_command
from ..checks.dhcp import parse_nmcli

PING_COUNTS_RE = re.compile(r"(\d+) packets transmitted, (\d+) received")
PING_LOSS_RE = re.compile(r"([\d.]+)% packet loss")
PING_RTT_RE = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)/")
HOP_RE = re.compile(r"^\s*(\d+)\s+(\S+)(?:\s+([\d.]+) ms)?")
EXPIRE_RE = re.compile(r"expire date:\s*(.+)$", re.IGNORECASE)
JSON_MARK = "ETHTESTER_JSON "
MODE_KEYS = {"Supported link modes": "supported_modes",
             "Advertised link modes": "advertised_modes",
             "Link partner advertised link modes": "partner_modes"}


def parse_ping(raw: str) -> dict:
    counts = PING_COUNTS_RE.search(raw)
    loss = PING_LOSS_RE.search(raw)
    rtt = PING_RTT_RE.search(raw)
    return {
        "sent": int(counts.group(1)) if counts else 0,
        "received": int(counts.group(2)) if counts else 0,
        "loss_pct": float(loss.group(1)) if loss else 100.0,
        "rtt_ms": ({"min": float(rtt.group(1)), "avg": float(rtt.group(2)), "max": float(rtt.group(3))}
                   if rtt else None),
    }


def ping(interface: str, host: str, count: int, size: int, run=run_command) -> dict:
    cmd = ["ping", "-I", interface, "-n", "-c", str(count), "-s", str(size), "-W", "2", "-i", "0.2", host]
    return {"host": host, **parse_ping(run(cmd, timeout=count * 2.5 + 5, check=False))}


def parse_traceroute(raw: str) -> list:
    hops = []
    for line in raw.splitlines()[1:]:
        m = HOP_RE.match(line)
        if m:
            hops.append({"hop": int(m.group(1)),
                         "ip": None if m.group(2) == "*" else m.group(2),
                         "rtt_ms": float(m.group(3)) if m.group(3) else None})
    return hops


def traceroute(interface: str, host: str, max_hops: int, run=run_command) -> dict:
    cmd = ["traceroute", "-i", interface, "-n", "-q", "1", "-w", "2", "-m", str(max_hops), host]
    return {"host": host, "hops": parse_traceroute(run(cmd, timeout=max_hops * 2 + 10, check=False))}


def parse_ethtool(raw: str) -> dict:
    fields: dict[str, str] = {}
    key = None
    for line in raw.splitlines()[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip()
        elif key is not None:
            fields[key] = f"{fields[key]} {line.strip()}".strip()
    result = {
        "speed": fields.get("Speed"),
        "duplex": fields.get("Duplex"),
        "autoneg": fields.get("Auto-negotiation") == "on",
        "link_detected": fields.get("Link detected") == "yes",
    }
    for source, target in MODE_KEYS.items():
        value = fields.get(source, "")
        result[target] = [] if value in ("", "Not reported") else value.split()
    return result


def parse_ethtool_stats(raw: str) -> dict:
    counters = {}
    for line in raw.splitlines():
        name, sep, value = line.strip().partition(":")
        value = value.strip()
        if sep and value.lstrip("-").isdigit():
            counters[name.strip()] = int(value)
    return counters


def parse_curl(raw: str) -> dict:
    data: dict = {}
    expires = None
    for line in raw.splitlines():
        if line.startswith(JSON_MARK):
            try:
                parsed = json.loads(line[len(JSON_MARK):])
            except ValueError:
                parsed = None
            data = parsed if isinstance(parsed, dict) else {}
        else:
            m = EXPIRE_RE.search(line)
            if m:
                expires = m.group(1).strip()

    def ms(key):
        value = data.get(key)
        return round(value * 1000, 1) if isinstance(value, (int, float)) else None

    return {
        "ok": data.get("exitcode", 1) == 0 and not data.get("errormsg"),
        "status": data.get("http_code") or None,
        "error": data.get("errormsg") or (None if data else "no result from curl"),
        "remote_ip": data.get("remote_ip") or None,
        "ms": {"dns": ms("time_namelookup"), "connect": ms("time_connect"), "tls": ms("time_appconnect"),
               "first_byte": ms("time_starttransfer"), "total": ms("time_total")},
        "cert_expires": expires,
    }


def http_check(interface: str, url: str, run=run_command) -> dict:
    cmd = ["curl", "--interface", interface, "-sS", "-g", "-v", "--stderr", "-", "-o", "/dev/null",
           "-m", "15", "-w", "\n" + JSON_MARK + "%{json}\n", url]
    return {"url": url, **parse_curl(run(cmd, timeout=20, check=False))}


def dhcp_renew(interface: str, run=run_command, clock=time.monotonic, sleep=time.sleep,
               timeout: float = 30.0) -> dict:
    """Drops the lease and reconnects; the main tester briefly shows the port without an address.

    Once the disconnect has been issued the connect always runs, so the port is never left down."""
    show = ["nmcli", "-t", "-f", "IP4,DHCP4", "device", "show", interface]
    connect = ["nmcli", "--wait", "0", "device", "connect", interface]
    run(["nmcli", "--wait", "10", "device", "disconnect", interface], timeout=15, check=False)
    dropped = False
    try:
        for _ in range(10):
            if not parse_nmcli(run(show, check=False)).ip:
                dropped = True
                break
            sleep(0.5)
    finally:
        started = clock()
        run(connect, check=False)
    if not dropped:
        return {"ok": False, "seconds_to_ip": None, "error": "could not drop the old lease"}
    while clock() - started < timeout:
        lease = parse_nmcli(run(show, check=False))
        if lease.ip:
            return {"ok": True, "seconds_to_ip": round(clock() - started, 1), **asdict(lease)}
        sleep(0.5)
    detail = (run(connect, check=False) or "").strip()
    error = f"no DHCP lease within {timeout:.0f} s"
    return {"ok": False, "seconds_to_ip": None, "error": f"{error}: {detail}" if detail else error}


def parse_arp_scan(raw: str) -> list:
    found = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].count(".") == 3:
            found.append({"ip": parts[0], "mac": parts[1], "vendor": parts[2] if len(parts) > 2 else None})
    return found


def neighbour_scan(interface: str, run=run_command) -> dict:
    cmd = ["arp-scan", "--interface", interface, "--localnet", "--plain", "--retry", "2"]
    return {"neighbours": parse_arp_scan(run(cmd, timeout=90, check=False))}


def parse_iperf(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "iperf3 gave no JSON output"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "unexpected iperf3 output"}
    if data.get("error"):
        return {"ok": False, "error": data["error"]}
    end = data.get("end") or {}
    bps = (end.get("sum_received") or {}).get("bits_per_second")
    return {"ok": bps is not None, "mbps": round(bps / 1e6, 1) if bps is not None else None,
            "retransmits": (end.get("sum_sent") or {}).get("retransmits")}


def speed_test(interface: str, server: str, seconds: int, reverse: bool, run=run_command) -> dict:
    cmd = ["iperf3", "-c", server, "--bind-dev", interface, "-t", str(seconds), "-J", "--connect-timeout", "5000"] + (["-R"] if reverse else [])
    return {"server": server, "reverse": reverse, **parse_iperf(run(cmd, timeout=seconds + 20, check=False))}
