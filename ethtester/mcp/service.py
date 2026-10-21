"""The operations behind the MCP tools: validate, check the link, run the probe, log the call."""

import json
import logging
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..activity import write_activity
from ..checks.command import run_command
from ..checks.dhcp import check_dhcp
from ..checks.link import check_link
from ..config import Config
from ..results import (DhcpResult, DnsResult, GatewayResult, InternetResult, LinkResult, LldpResult,
                       Snapshot)
from . import dnsq, probes, summary, validate

log = logging.getLogger(__name__)

STALE_S = 30
LINK_SETTLE_S = 3.0
MAX_SCAN_PREFIX = 22
UNREADABLE_STATUS = "status file is unreadable; check the ethtester service"
FORCE_SPEEDS = {"force_10": 10, "force_100": 100}
# Tools that only read state; every other tool shows on the tester's probe screen.
READ_ONLY = frozenset({"get_status", "get_history", "get_link_details"})
# After a link-disturbing probe, how long the display waits before taking its verdict baseline.
SETTLE_S = 10
MAX_FORCE_S = 300


NESTED = {"link": LinkResult, "lldp": LldpResult, "dhcp": DhcpResult, "gateway": GatewayResult,
          "dns": DnsResult, "internet": InternetResult}


class ProbeError(Exception):
    pass


def snapshot_from_dict(data: dict) -> Snapshot:
    """Rebuilds a Snapshot from its asdict()/JSON form; raises on anything that doesn't fit."""
    fields = dict(data)
    for key, cls in NESTED.items():
        value = fields.get(key)
        if value is not None:
            if cls is DhcpResult and "dns_servers" in value:
                value = {**value, "dns_servers": tuple(value["dns_servers"])}  # JSON gave a list
            fields[key] = cls(**value)
    return Snapshot(**fields)


def _screen_rows(data: dict) -> list | None:
    """The tester's screen rows rebuilt from the state file, or None if they can't be."""
    try:
        from ..render import build_rows
        from ..verdict import Verdict
        rows = build_rows(snapshot_from_dict(data["snapshot"]), Verdict(code=data["verdict"], text=data["text"]))
        return [{"label": r.label, "status": r.status, "text": r.text} for r in rows]
    except Exception:
        log.debug("could not rebuild screen rows", exc_info=True)
        return None


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
                 dhcp=check_dhcp, dns=dnsq.lookup, sleep=time.sleep, wall=time.time,
                 activity_path: Path | None = None):
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
        self.activity_path = None if activity_path is None else Path(activity_path)
        self._activity_lock = threading.Lock()
        self._activity_calls = 0
        self._activity_id: str | None = None

    def _logged(self, tool: str, args: dict, fn):
        started = time.monotonic()
        activity = self._activity_start(tool, args)
        try:
            result = fn()
        except Exception as exc:
            self._log(tool, args, started, False, str(exc))
            self._activity_finish(activity, args, None, exc)
            raise
        self._log(tool, args, started, True, None)
        self._activity_finish(activity, args, result, None)
        return result

    def _activity_start(self, tool: str, args: dict) -> dict | None:
        """Shows the probe as running on the tester's screen; never raises."""
        if self.activity_path is None or tool in READ_ONLY:
            return None
        try:
            with self._activity_lock:
                self._activity_calls += 1
                now = self.wall()
                activity = {"id": f"{now:.3f}-{self._activity_calls}", "tool": tool,
                            "target": summary.target_of(tool, args), "state": "running",
                            "started": now, "finished": None, "settle_until": None, "summary": [],
                            "calls": self._activity_calls}
                self._activity_id = activity["id"]
                write_activity(self.activity_path, activity)
            return activity
        except Exception:
            log.exception("activity write failed")
            return None

    def _activity_finish(self, activity: dict | None, args: dict, result, error) -> None:
        """Replaces the running activity with its result, unless a newer probe has started since."""
        if activity is None:
            return
        try:
            tool = activity["tool"]
            finished = self.wall()
            if error is None:
                state = summary.outcome(tool, result)
                try:
                    lines = summary.summarise(tool, args, result)
                except Exception:
                    log.exception("summary failed")
                    lines = []
            else:
                state, lines = "error", summary.error_summary(str(error))
            data = {**activity, "state": state, "finished": finished, "summary": lines,
                    "settle_until": finished + self._settle_s(tool, args, result, error)}
            with self._activity_lock:
                if self._activity_id == activity["id"]:
                    write_activity(self.activity_path, data)
        except Exception:
            log.exception("activity write failed")

    @staticmethod
    def _settle_s(tool: str, args: dict, result, error=None) -> float:
        """Seconds after finishing during which the probe's own link disturbance is expected."""
        if error is not None or result is None:
            return 0   # the call raised: nothing was changed, so nothing to settle
        if tool == "dhcp_renew":
            return SETTLE_S
        if tool != "link_control":
            return 0
        action = args.get("action")
        if action in FORCE_SPEEDS:
            seconds = result.get("reverts_in_s") if isinstance(result, dict) else args.get("duration_s")
            if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds != seconds:
                seconds = 0
            return min(max(seconds, 0), MAX_FORCE_S) + SETTLE_S
        return SETTLE_S if action == "renegotiate" else 0

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
            except (OSError, ValueError):
                raise ProbeError(UNREADABLE_STATUS) from None
            epoch = data.get("epoch", 0) if isinstance(data, dict) else None
            if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
                raise ProbeError(UNREADABLE_STATUS)
            age = self.wall() - epoch
            return {**data, "rows": _screen_rows(data), "age_s": round(age, 1), "stale": age > STALE_S}
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
                    if not isinstance(entry, dict):
                        continue
                    try:
                        when = datetime.fromisoformat(entry["ts"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if start is not None and when < start:
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
            self._require_link()
            if server is not None:
                resolver = validate.ipv4(server)
            else:
                lease = self.dhcp(self.iface)
                candidates = lease.dns_servers or ((lease.gateway,) if lease.gateway else ())
                if not candidates:
                    raise ProbeError("no DNS server from DHCP; pass server explicitly")
                resolver = candidates[0]
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
                if lease.prefix is None:
                    raise ProbeError("subnet size is unknown; refusing to scan")
                if lease.prefix < MAX_SCAN_PREFIX:
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
