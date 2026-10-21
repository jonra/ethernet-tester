"""The operations behind the MCP tools: validate, check the link, run the probe, log the call."""

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..checks.command import run_command
from ..checks.dhcp import check_dhcp
from ..checks.link import check_link
from ..config import Config
from . import dnsq, probes, validate

log = logging.getLogger(__name__)

STALE_S = 30
LINK_SETTLE_S = 3.0
MAX_SCAN_PREFIX = 22
FORCE_SPEEDS = {"force_10": 10, "force_100": 100}


class ProbeError(Exception):
    pass


def _summarise(entry: dict) -> dict:
    snap = entry.get("snapshot") or {}
    link = snap.get("link") or {}
    dhcp = snap.get("dhcp") or {}
    lldp = snap.get("lldp") or {}
    return {"ts": entry.get("ts"), "verdict": entry.get("verdict"), "text": entry.get("text"),
            "link_up": link.get("up"), "speed_mbps": link.get("speed_mbps"), "ip": dhcp.get("ip"),
            "switch": lldp.get("switch_name"), "port": lldp.get("port")}


class ProbeService:
    def __init__(self, cfg: Config, linkctl, probelog=None, run=run_command, link=check_link,
                 dhcp=check_dhcp, dns=dnsq.lookup, sleep=time.sleep, wall=time.time):
        self.cfg = cfg
        self.iface = cfg.interface
        self.linkctl = linkctl
        self.probelog = probelog
        self.run = run
        self.link = link
        self.dhcp = dhcp
        self.dns = dns
        self.sleep = sleep
        self.wall = wall

    def _logged(self, tool: str, args: dict, fn):
        started = time.monotonic()
        try:
            result = fn()
        except Exception as exc:
            self._log(tool, args, started, False, str(exc))
            raise
        self._log(tool, args, started, True, None)
        return result

    def _log(self, tool, args, started, ok, error):
        if self.probelog is None:
            return
        try:
            self.probelog.record(tool, args, round(time.monotonic() - started, 2), ok, error, self.wall())
        except Exception:
            log.exception("probe log write failed")

    def _require_link(self):
        link = self.link(self.iface)
        if not link.up:
            raise ProbeError(f"{self.iface} has no link; plug in a cable first")
        return link

    def get_status(self) -> dict:
        def body():
            try:
                data = json.loads(Path(self.cfg.state_path).read_text())
            except FileNotFoundError:
                raise ProbeError("no status yet; is the ethtester service running?") from None
            age = self.wall() - data.get("epoch", 0)
            return {**data, "age_s": round(age, 1), "stale": age > STALE_S}
        return self._logged("get_status", {}, body)

    def get_history(self, limit: int = 50, since: str | None = None) -> dict:
        args = {"limit": limit, "since": since}

        def body():
            count = validate.bounded("limit", limit, 1, 200)
            start = validate.timestamp(since) if since else None
            path = Path(self.cfg.log_path)
            rows = []
            for name in (path.with_name(path.name + ".1"), path):
                if not name.exists():
                    continue
                for line in name.read_text().splitlines():
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if start is not None and datetime.fromisoformat(entry["ts"]) < start:
                        continue
                    rows.append(_summarise(entry))
            return {"entries": rows[-count:]}
        return self._logged("get_history", args, body)

    def get_link_details(self) -> dict:
        def body():
            settings = probes.parse_ethtool(self.run(["ethtool", self.iface]))
            counters = probes.parse_ethtool_stats(self.run(["ethtool", "-S", self.iface], check=False))
            link = self.link(self.iface)
            return {"interface": self.iface, **settings, "carrier_changes": link.carrier_changes,
                    "counters": counters, "forced_reverts_in_s": self.linkctl.revert_in()}
        return self._logged("get_link_details", {}, body)

    def ping(self, host: str, count: int = 5, size: int = 56) -> dict:
        args = {"host": host, "count": count, "size": size}

        def body():
            target = validate.host(host)
            n = validate.bounded("count", count, 1, 20)
            s = validate.bounded("size", size, 16, 1472)
            self._require_link()
            return probes.ping(self.iface, target, n, s, run=self.run)
        return self._logged("ping", args, body)

    def traceroute(self, host: str, max_hops: int = 20) -> dict:
        args = {"host": host, "max_hops": max_hops}

        def body():
            target = validate.host(host)
            hops = validate.bounded("max_hops", max_hops, 1, 30)
            self._require_link()
            return probes.traceroute(self.iface, target, hops, run=self.run)
        return self._logged("traceroute", args, body)

    def dns_lookup(self, name: str, record_type: str = "A", server: str | None = None) -> dict:
        args = {"name": name, "record_type": record_type, "server": server}

        def body():
            target = validate.host(name)
            qtype = validate.record_type(record_type)
            if server is not None:
                resolver = validate.ipv4(server)
            else:
                lease = self.dhcp(self.iface)
                candidates = lease.dns_servers or ((lease.gateway,) if lease.gateway else ())
                if not candidates:
                    raise ProbeError("no DNS server from DHCP; pass server explicitly")
                resolver = candidates[0]
            self._require_link()
            try:
                return self.dns(resolver, target, qtype, self.iface)
            except OSError as exc:
                raise ProbeError(f"no answer from {resolver}: {exc}") from None
        return self._logged("dns_lookup", args, body)

    def http_check(self, url: str) -> dict:
        def body():
            target = validate.url(url)
            self._require_link()
            return probes.http_check(self.iface, target, run=self.run)
        return self._logged("http_check", {"url": url}, body)

    def dhcp_renew(self) -> dict:
        def body():
            with self.linkctl.exclusive():
                self._require_link()
                return probes.dhcp_renew(self.iface, run=self.run, sleep=self.sleep)
        return self._logged("dhcp_renew", {}, body)

    def link_control(self, action: str, duration_s: int = 60) -> dict:
        args = {"action": action, "duration_s": duration_s}

        def body():
            if action == "renegotiate":
                result = self.linkctl.renegotiate()
            elif action in FORCE_SPEEDS:
                seconds = validate.bounded("duration_s", duration_s, 10, 300)
                result = self.linkctl.force(FORCE_SPEEDS[action], seconds)
            else:
                raise ValueError(f"action must be renegotiate, force_10 or force_100, got {action!r}")
            self.sleep(LINK_SETTLE_S)
            return {**result, "link": asdict(self.link(self.iface))}
        return self._logged("link_control", args, body)

    def neighbour_scan(self) -> dict:
        def body():
            with self.linkctl.exclusive():
                self._require_link()
                lease = self.dhcp(self.iface)
                if not lease.ip:
                    raise ProbeError(f"{self.iface} has no IPv4 address to scan from")
                if lease.prefix is not None and lease.prefix < MAX_SCAN_PREFIX:
                    raise ProbeError(f"subnet /{lease.prefix} is too large to scan; limit is /{MAX_SCAN_PREFIX}")
                return probes.neighbour_scan(self.iface, run=self.run)
        return self._logged("neighbour_scan", {}, body)

    def speed_test(self, server: str, seconds: int = 10, reverse: bool = False) -> dict:
        args = {"server": server, "seconds": seconds, "reverse": reverse}

        def body():
            target = validate.host(server)
            duration = validate.bounded("seconds", seconds, 3, 30)
            backwards = validate.boolean("reverse", reverse)
            self._require_link()
            return probes.speed_test(self.iface, target, duration, backwards, run=self.run)
        return self._logged("speed_test", args, body)
