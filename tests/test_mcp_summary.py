import pytest

from ethtester.mcp.summary import error_summary, outcome, summarise, target_of

PING_OK = {"host": "1.1.1.1", "sent": 5, "received": 5, "loss_pct": 0.0,
           "rtt_ms": {"min": 1.0, "avg": 1.5, "max": 2.04}}


def test_ping():
    assert summarise("ping", {}, PING_OK) == ["sent 5  received 5  loss 0%", "rtt 1.0 / 1.5 / 2.0 ms"]
    lost = {**PING_OK, "received": 0, "loss_pct": 100.0, "rtt_ms": None}
    assert summarise("ping", {}, lost) == ["sent 5  received 0  loss 100%"]
    assert outcome("ping", PING_OK) == "ok" and outcome("ping", lost) == "error"


def test_traceroute():
    hops = [{"hop": 1, "ip": "192.168.1.1", "rtt_ms": 0.8}, {"hop": 2, "ip": None, "rtt_ms": None},
            {"hop": 3, "ip": "10.0.0.1", "rtt_ms": 5.25}, {"hop": 4, "ip": "1.1.1.1", "rtt_ms": 9.0}]
    assert summarise("traceroute", {}, {"host": "1.1.1.1", "hops": hops}) == [
        "4 hops", "#2 * *", "#3 10.0.0.1 5.2 ms", "#4 1.1.1.1 9.0 ms"]
    assert summarise("traceroute", {}, {"host": "x", "hops": [hops[0]]}) == ["1 hop", "#1 192.168.1.1 0.8 ms"]


def test_dns_lookup():
    answers = [{"name": "example.com", "type": "A", "ttl": 60, "data": f"10.0.0.{i}"} for i in range(5)]
    result = {"rcode": "NOERROR", "answers": answers, "server": "192.168.1.1", "ms": 12.4}
    assert summarise("dns_lookup", {}, result) == [
        "NOERROR via 192.168.1.1  12ms", "10.0.0.0", "10.0.0.1", "10.0.0.2"]
    nx = {"rcode": "NXDOMAIN", "answers": [], "server": "1.1.1.1", "ms": 3.0}
    assert summarise("dns_lookup", {}, nx) == ["NXDOMAIN via 1.1.1.1  3ms"]


def test_http_check():
    ok = {"url": "https://x", "ok": True, "status": 200, "error": None, "remote_ip": "1.2.3.4",
          "ms": {"dns": 1.0, "connect": 2.0, "tls": 3.0, "first_byte": 4.0, "total": 123.4},
          "cert_expires": "Jan 12 23:59:59 2027 GMT"}
    assert summarise("http_check", {}, ok) == ["HTTP 200  total 123ms", "cert expires Jan 12 23:59:59 2027 GMT"]
    plain = {**ok, "cert_expires": None}
    assert summarise("http_check", {}, plain) == ["HTTP 200  total 123ms"]
    bad = {**ok, "ok": False, "status": None, "error": "Could not resolve host: x",
           "ms": {k: None for k in ok["ms"]}, "cert_expires": None}
    assert summarise("http_check", {}, bad) == ["Could not resolve host: x"]
    assert outcome("http_check", ok) == "ok" and outcome("http_check", bad) == "error"


def test_dhcp_renew():
    ok = {"ok": True, "seconds_to_ip": 2.3, "ip": "192.168.1.57", "prefix": 24, "gateway": "192.168.1.1",
          "dhcp_server": "192.168.1.254", "dns_servers": ["192.168.1.1"]}
    assert summarise("dhcp_renew", {}, ok) == ["192.168.1.57/24 in 2.3s", "gw 192.168.1.1  dhcp 192.168.1.254"]
    bad = {"ok": False, "seconds_to_ip": None, "error": "no DHCP lease within 30 s"}
    assert summarise("dhcp_renew", {}, bad) == ["no DHCP lease within 30 s"]
    assert outcome("dhcp_renew", bad) == "error"


def test_link_control():
    forced = {"action": "force_100", "reverts_in_s": 60,
              "link": {"up": True, "speed_mbps": 100, "duplex": "full", "carrier_changes": 4}}
    assert summarise("link_control", {}, forced) == ["force_100", "reverts in 60s", "link up 100M full"]
    reneg = {"action": "renegotiate", "link": {"up": False, "speed_mbps": None, "duplex": None}}
    assert summarise("link_control", {}, reneg) == ["renegotiate", "link down"]


def test_neighbour_scan():
    found = [{"ip": f"192.168.1.{i}", "mac": "aa:bb", "vendor": "Apple" if i else None} for i in range(5)]
    assert summarise("neighbour_scan", {}, {"neighbours": found}) == [
        "5 devices", "192.168.1.0 aa:bb", "192.168.1.1 Apple", "192.168.1.2 Apple"]
    assert summarise("neighbour_scan", {}, {"neighbours": [found[1]]}) == ["1 device", "192.168.1.1 Apple"]


def test_speed_test():
    up = {"server": "mac.lan", "reverse": False, "ok": True, "mbps": 94.3, "retransmits": 2}
    assert summarise("speed_test", {}, up) == ["94.3 Mbit/s (upload)", "retransmits 2"]
    down = {**up, "reverse": True, "retransmits": None}
    assert summarise("speed_test", {}, down) == ["94.3 Mbit/s (download)"]
    bad = {"server": "mac.lan", "reverse": False, "ok": False, "error": "unable to connect to server"}
    assert summarise("speed_test", {}, bad) == ["unable to connect to server"]
    assert outcome("speed_test", bad) == "error"


@pytest.mark.parametrize("tool", ["ping", "traceroute", "dns_lookup", "http_check", "dhcp_renew",
                                  "link_control", "neighbour_scan", "speed_test", "unknown"])
def test_summarise_never_raises_and_caps_at_four(tool):
    for odd in (None, [], "text", {"weird": object()}, {"hops": "x", "neighbours": 5, "answers": None}):
        lines = summarise(tool, {}, odd)
        assert isinstance(lines, list) and len(lines) <= 4


def test_error_summary_is_first_line_in_at_most_two_lines():
    assert error_summary("eth0 has no link; plug in a cable first") == ["eth0 has no link; plug in a cable", "first"]
    assert error_summary("short\nsecond line ignored") == ["short"]
    long = error_summary("x" * 200)
    assert len(long) == 2 and long[1].endswith("…")
    assert error_summary("") == ["error"]


def test_target_of():
    assert target_of("ping", {"host": "1.1.1.1", "count": 5}) == "1.1.1.1"
    assert target_of("traceroute", {"host": "8.8.8.8"}) == "8.8.8.8"
    assert target_of("dns_lookup", {"name": "example.com", "record_type": "A"}) == "example.com"
    assert target_of("dns_lookup", {"name": "example.com", "record_type": "mx"}) == "example.com MX"
    assert target_of("http_check", {"url": "https://x.nl/"}) == "https://x.nl/"
    assert target_of("speed_test", {"server": "mac.lan"}) == "mac.lan"
    assert target_of("link_control", {"action": "force_10"}) == "force_10"
    assert target_of("dhcp_renew", {}) == ""
    assert target_of("neighbour_scan", {}) == ""
    assert target_of("ping", {"host": 5}) == "5"
