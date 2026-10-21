import json
from dataclasses import asdict
from pathlib import Path

import pytest

from ethtester.config import Config
from ethtester.mcp.linkctl import Busy, LinkControl
from ethtester.mcp.probelog import ProbeLog
from ethtester.mcp.service import ProbeError, ProbeService, snapshot_from_dict
from ethtester.render import build_rows
from ethtester.results import DhcpResult, LinkResult, Snapshot
from ethtester.statefile import write_state
from ethtester.verdict import evaluate
from tests.factories import ok_snapshot

PING = "5 packets transmitted, 5 received, 0% packet loss\nrtt min/avg/max/mdev = 1.0/1.5/2.0/0.1 ms\n"
LEASE = DhcpResult(ip="192.168.1.222", prefix=24, gateway="192.168.1.1", dns_servers=("192.168.1.1",))


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[0] == "ping":
            return PING
        if cmd == ["ethtool", "eth0"]:
            return "Settings for eth0:\n\tSpeed: 100Mb/s\n\tAuto-negotiation: on\n"
        return ""


class NoTimer:
    def __init__(self, delay, fn):
        pass

    def start(self):
        pass


def make(tmp_path: Path, up=True, lease=LEASE, activity_path=None, wall=lambda: 1_000.0):
    cfg = Config(state_path=str(tmp_path / "state.json"), log_path=str(tmp_path / "ethtester.log"))
    run = FakeRun()
    linkctl = LinkControl("eth0", run=run, timer=NoTimer, clock=lambda: 0.0)
    probelog = ProbeLog(tmp_path / "probes.log")
    dns_calls = []

    def dns(server, name, qtype, interface):
        dns_calls.append((server, name, qtype, interface))
        return {"rcode": "NOERROR", "answers": [], "server": server, "ms": 1.0}

    service = ProbeService(cfg, linkctl, probelog=probelog, run=run,
                           link=lambda i: LinkResult(up=up, speed_mbps=100 if up else None, carrier_changes=2),
                           dhcp=lambda i: lease, dns=dns, sleep=lambda s: None, wall=wall,
                           activity_path=activity_path)
    return service, run, dns_calls, tmp_path / "probes.log"


def test_get_status_fresh_and_stale(tmp_path):
    service, *_ = make(tmp_path)
    s = ok_snapshot()
    write_state(tmp_path / "state.json", s, evaluate(s, 1000, 20), 990.0)
    status = service.get_status()
    assert status["verdict"] == "OK" and status["age_s"] == 10.0 and status["stale"] is False
    write_state(tmp_path / "state.json", s, evaluate(s, 1000, 20), 900.0)
    assert service.get_status()["stale"] is True


def test_snapshot_from_dict_round_trips_through_json():
    s = ok_snapshot()
    assert snapshot_from_dict(json.loads(json.dumps(asdict(s)))) == s
    bare = Snapshot(link=LinkResult(up=False))
    assert snapshot_from_dict(json.loads(json.dumps(asdict(bare)))) == bare


def test_get_status_includes_screen_rows(tmp_path):
    service, *_ = make(tmp_path)
    s = ok_snapshot()
    v = evaluate(s, 1000, 20)
    write_state(tmp_path / "state.json", s, v, 990.0)
    rows = service.get_status()["rows"]
    assert rows == [asdict(r) for r in build_rows(s, v)]
    assert [r["label"] for r in rows] == ["LINK", "PORT", "IP", "GW", "DNS", "NET"]
    assert set(rows[0]) == {"label", "status", "text"}


@pytest.mark.parametrize("snapshot", [None, {}, {"link": "up"}, {"link": {"up": True, "bogus": 1}}])
def test_get_status_rows_none_when_snapshot_is_odd(tmp_path, snapshot):
    service, *_ = make(tmp_path)
    data = {"epoch": 990.0, "verdict": "OK", "text": "OK", "snapshot": snapshot}
    (tmp_path / "state.json").write_text(json.dumps(data))
    status = service.get_status()
    assert status["rows"] is None and status["verdict"] == "OK"


def test_get_status_missing(tmp_path):
    service, *_ = make(tmp_path)
    with pytest.raises(ProbeError, match="no status"):
        service.get_status()


def test_get_history(tmp_path):
    service, *_ = make(tmp_path)
    lines = [{"ts": f"2026-10-21T09:4{i}:00", "verdict": "OK", "text": "OK",
              "snapshot": json.loads(json.dumps({"link": {"up": True, "speed_mbps": 100},
                                                 "dhcp": {"ip": "10.0.0.2"}, "lldp": None}))}
             for i in range(5)]
    (tmp_path / "ethtester.log").write_text("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
    entries = service.get_history(limit=2)["entries"]
    assert [e["ts"] for e in entries] == ["2026-10-21T09:43:00", "2026-10-21T09:44:00"]
    assert entries[0] == {"ts": "2026-10-21T09:43:00", "verdict": "OK", "text": "OK", "link_up": True,
                          "speed_mbps": 100, "ip": "10.0.0.2", "switch": None, "port": None}
    assert len(service.get_history(since="2026-10-21T09:42:00")["entries"]) == 3
    with pytest.raises(ValueError):
        service.get_history(limit=500)


def test_ping_validates_checks_link_and_logs(tmp_path):
    service, run, _, log_path = make(tmp_path)
    result = service.ping("192.168.1.1", count=5)
    assert result["received"] == 5
    assert run.calls[-1][:3] == ["ping", "-I", "eth0"]
    with pytest.raises(ValueError):
        service.ping("-f 192.168.1.1")
    entries = [json.loads(l) for l in log_path.read_text().splitlines()]
    assert [(e["tool"], e["ok"]) for e in entries] == [("ping", True), ("ping", False)]
    assert entries[0]["args"] == {"host": "192.168.1.1", "count": 5, "size": 56}


def test_probe_refuses_without_link(tmp_path):
    service, *_ = make(tmp_path, up=False)
    with pytest.raises(ProbeError, match="no link"):
        service.ping("1.1.1.1")


def test_dns_lookup_defaults_to_dhcp_server(tmp_path):
    service, _, dns_calls, _ = make(tmp_path)
    service.dns_lookup("example.com", record_type="mx")
    assert dns_calls == [("192.168.1.1", "example.com", "MX", "eth0")]
    service.dns_lookup("example.com", server="1.1.1.1")
    assert dns_calls[-1][0] == "1.1.1.1"
    with pytest.raises(ValueError):
        service.dns_lookup("example.com", server="dns.google")


def test_neighbour_scan_refuses_large_subnet(tmp_path):
    service, *_ = make(tmp_path, lease=DhcpResult(ip="10.1.2.3", prefix=16))
    with pytest.raises(ProbeError, match="/16"):
        service.neighbour_scan()


def test_link_control_force_then_busy(tmp_path):
    service, run, _, _ = make(tmp_path)
    result = service.link_control("force_10", duration_s=30)
    assert result["action"] == "force_10" and result["link"]["up"] is True
    with pytest.raises(Busy):
        service.dhcp_renew()
    with pytest.raises(ValueError):
        service.link_control("force_1000")
    with pytest.raises(ValueError):
        service.link_control("force_10", duration_s=5)


def test_speed_test_validates(tmp_path):
    service, run, _, _ = make(tmp_path)
    service.speed_test("mac.lan", seconds=5)
    assert run.calls[-1][:2] == ["iperf3", "-c"]
    with pytest.raises(ValueError):
        service.speed_test("mac.lan", reverse="yes")


@pytest.mark.parametrize("content", ["{", "[1, 2]", '{"epoch": "abc"}', '{"epoch": true}'])
def test_get_status_corrupt_state(tmp_path, content):
    service, *_ = make(tmp_path)
    (tmp_path / "state.json").write_text(content)
    with pytest.raises(ProbeError, match="unreadable"):
        service.get_status()


def test_neighbour_scan_refuses_unknown_prefix(tmp_path):
    service, *_ = make(tmp_path, lease=DhcpResult(ip="10.1.2.3", prefix=None))
    with pytest.raises(ProbeError, match="unknown"):
        service.neighbour_scan()


def test_neighbour_scan_accepts_slash_22(tmp_path):
    service, run, *_ = make(tmp_path, lease=DhcpResult(ip="10.1.2.3", prefix=22))
    service.neighbour_scan()
    assert any(c[0] == "arp-scan" for c in run.calls)


def test_dns_lookup_requires_link_before_dhcp(tmp_path):
    service, _, dns_calls, _ = make(tmp_path, up=False, lease=DhcpResult(ip="192.168.1.5", prefix=24))
    with pytest.raises(ProbeError, match="no link"):
        service.dns_lookup("example.com")
    assert dns_calls == []


def test_get_history_skips_odd_lines(tmp_path):
    service, *_ = make(tmp_path)
    good = {"ts": "2026-10-21T09:40:00", "verdict": "OK", "text": "OK", "snapshot": {}}
    lines = ["[1,2]", '{"verdict":"OK"}', '{"ts":"garbage"}', '{"ts":5}', json.dumps(good)]
    (tmp_path / "ethtester.log").write_text("\n".join(lines) + "\n")
    assert [e["ts"] for e in service.get_history()["entries"]] == ["2026-10-21T09:40:00"]
    assert len(service.get_history(since="2026-10-21T09:00:00")["entries"]) == 1


from ethtester.activity import read_activity  # noqa: E402
from ethtester.mcp import service as service_mod  # noqa: E402


class Ticker:
    def __init__(self, start=1_000.0, step=1.0):
        self.now, self.step = start, step

    def __call__(self):
        self.now += self.step
        return self.now


def test_activity_running_then_finished(tmp_path):
    path = tmp_path / "run" / "activity.json"
    service, run, *_ = make(tmp_path, activity_path=path, wall=Ticker())
    seen = []
    inner = service.run

    def spy(cmd, **kwargs):
        if cmd[0] == "ping":
            seen.append(read_activity(path))
        return inner(cmd, **kwargs)

    service.run = spy
    service.ping("1.1.1.1", count=5)
    running = seen[0]
    assert running["tool"] == "ping" and running["target"] == "1.1.1.1"
    assert running["state"] == "running" and running["finished"] is None
    assert running["summary"] == [] and running["calls"] == 1
    done = read_activity(path)
    assert done["id"] == running["id"] and done["state"] == "ok"
    assert done["started"] == running["started"] and done["finished"] > done["started"]
    assert done["settle_until"] == done["finished"]
    assert done["summary"] == ["sent 5  received 5  loss 0%", "rtt 1.0 / 1.5 / 2.0 ms"]


def test_activity_not_written_for_read_only_tools(tmp_path):
    path = tmp_path / "activity.json"
    service, *_ = make(tmp_path, activity_path=path)
    s = ok_snapshot()
    write_state(tmp_path / "state.json", s, evaluate(s, 1000, 20), 990.0)
    service.get_status()
    service.get_history()
    service.get_link_details()
    assert not path.exists()
    service.ping("1.1.1.1")
    assert read_activity(path)["calls"] == 1


def test_activity_error_and_new_ids_and_call_count(tmp_path):
    path = tmp_path / "activity.json"
    service, *_ = make(tmp_path, up=False, activity_path=path, wall=Ticker())
    with pytest.raises(ProbeError):
        service.ping("1.1.1.1")
    first = read_activity(path)
    assert first["state"] == "error" and first["summary"] == ["eth0 has no link; plug in a cable", "first"]
    with pytest.raises(ValueError):
        service.traceroute("1.1.1.1", max_hops=99)
    second = read_activity(path)
    assert second["id"] != first["id"] and second["calls"] == 2 and second["tool"] == "traceroute"
    assert second["state"] == "error"


def test_activity_failed_result_is_error(tmp_path, monkeypatch):
    path = tmp_path / "activity.json"
    service, *_ = make(tmp_path, activity_path=path)
    monkeypatch.setattr(service_mod.probes, "speed_test",
                        lambda *a, **k: {"server": "mac.lan", "reverse": False, "ok": False, "error": "refused"})
    service.speed_test("mac.lan")
    done = read_activity(path)
    assert done["state"] == "error" and done["summary"] == ["refused"]


def test_activity_settle_until(tmp_path, monkeypatch):
    path = tmp_path / "activity.json"
    service, *_ = make(tmp_path, activity_path=path)   # wall is always 1000
    monkeypatch.setattr(service_mod.probes, "dhcp_renew",
                        lambda iface, run, sleep: {"ok": True, "seconds_to_ip": 2.0, "ip": "10.0.0.2",
                                                   "prefix": 24, "gateway": "10.0.0.1", "dhcp_server": "10.0.0.1"})
    service.dhcp_renew()
    assert read_activity(path)["settle_until"] == 1_010.0
    service.link_control("renegotiate")
    assert read_activity(path)["settle_until"] == 1_010.0
    service.link_control("force_100", duration_s=30)
    done = read_activity(path)
    assert done["settle_until"] == 1_040.0 and done["summary"][:2] == ["force_100", "reverts in 30s"]
    service.ping("1.1.1.1")
    assert read_activity(path)["settle_until"] == 1_000.0


def test_activity_older_probe_does_not_overwrite_newer(tmp_path):
    path = tmp_path / "activity.json"
    service, *_ = make(tmp_path, activity_path=path, wall=Ticker())
    inner = service.run

    def run(cmd, **kwargs):
        if cmd[0] == "ping":
            service.traceroute("8.8.8.8")   # a second client starts a probe meanwhile
        return inner(cmd, **kwargs)

    service.run = run
    service.ping("1.1.1.1")
    shown = read_activity(path)
    assert shown["tool"] == "traceroute" and shown["state"] == "ok" and shown["calls"] == 2


def test_activity_write_failure_never_breaks_tools(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    service, *_ = make(tmp_path, activity_path=blocker / "activity.json")
    assert service.ping("1.1.1.1")["received"] == 5
    with pytest.raises(ValueError, match="count"):
        service.ping("1.1.1.1", count=99)
    assert "activity write failed" in caplog.text


def test_activity_summary_failure_still_finishes(tmp_path, monkeypatch):
    path = tmp_path / "activity.json"
    service, *_ = make(tmp_path, activity_path=path)

    def broken(*a):
        raise RuntimeError("bug")

    monkeypatch.setattr(service_mod.summary, "summarise", broken)
    assert service.ping("1.1.1.1")["received"] == 5
    done = read_activity(path)
    assert done["state"] == "ok" and done["summary"] == []


def test_no_activity_path_writes_nothing(tmp_path):
    service, *_ = make(tmp_path)
    service.ping("1.1.1.1")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["probes.log"]


def test_settle_is_zero_when_the_call_raised(tmp_path):
    settle = ProbeService._settle_s
    assert settle("link_control", {"action": "force_100", "duration_s": 30}, None) == 0
    assert settle("link_control", {"action": "renegotiate"}, None) == 0
    assert settle("dhcp_renew", {}, None) == 0


def test_failed_link_control_gets_no_settle(tmp_path, monkeypatch):
    path = tmp_path / "activity.json"
    service, *_ = make(tmp_path, activity_path=path)
    monkeypatch.setattr(service.linkctl, "force", lambda *a: (_ for _ in ()).throw(Busy("busy")))
    with pytest.raises(Exception):
        service.link_control("force_100", duration_s=30)
    done = read_activity(path)
    assert done["state"] == "error" and done["settle_until"] == done["finished"]
