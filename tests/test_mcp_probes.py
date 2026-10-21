import json

from ethtester.mcp import probes

PING = """PING 192.168.1.1 (192.168.1.1) from 192.168.1.222 eth0: 56(84) bytes of data.

--- 192.168.1.1 ping statistics ---
5 packets transmitted, 4 received, 20% packet loss, time 812ms
rtt min/avg/max/mdev = 0.981/1.356/2.004/0.310 ms
"""

TRACEROUTE = """traceroute to 1.1.1.1 (1.1.1.1), 20 hops max, 60 byte packets
 1  192.168.1.1  0.981 ms
 2  *
 3  10.0.0.1  5.123 ms
"""

ETHTOOL = """Settings for eth0:
\tSupported ports: [ TP MII ]
\tSupported link modes:   10baseT/Half 10baseT/Full
\t                        100baseT/Half 100baseT/Full
\tAdvertised link modes:  10baseT/Half 10baseT/Full
\t                        100baseT/Half 100baseT/Full
\tLink partner advertised link modes:  10baseT/Half 10baseT/Full
\t                                     100baseT/Full 1000baseT/Full
\tSpeed: 100Mb/s
\tDuplex: Full
\tAuto-negotiation: on
\tLink detected: yes
"""

STATS = """NIC statistics:
     tx_packets: 1234
     rx_errors: 3
     rx_missed: 0
"""

CURL = """*   Trying 1.1.1.1:443...
*  expire date: Jan 15 12:00:00 2027 GMT
< HTTP/2 200

ETHTESTER_JSON {"http_code":200,"exitcode":0,"errormsg":null,"remote_ip":"1.1.1.1","time_namelookup":0.001,"time_connect":0.012,"time_appconnect":0.045,"time_starttransfer":0.080,"time_total":0.081}
"""

ARP = "192.168.1.1\t60:38:e0:aa:bb:cc\tBelkin International Inc.\n192.168.1.20\t00:e0:4c:36:00:f5\t(Unknown)\n"

IPERF = json.dumps({"end": {"sum_received": {"bits_per_second": 93_400_000.0},
                            "sum_sent": {"retransmits": 4}}})


class Recorder:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.outputs.pop(0) if self.outputs else ""


def test_ping():
    run = Recorder(PING)
    result = probes.ping("eth0", "192.168.1.1", 5, 56, run=run)
    assert result == {"host": "192.168.1.1", "sent": 5, "received": 4, "loss_pct": 20.0,
                      "rtt_ms": {"min": 0.981, "avg": 1.356, "max": 2.004}}
    cmd, kwargs = run.calls[0]
    assert cmd == ["ping", "-I", "eth0", "-n", "-c", "5", "-s", "56", "-W", "2", "-i", "0.2", "192.168.1.1"]
    assert kwargs["check"] is False


def test_ping_total_loss():
    assert probes.parse_ping("3 packets transmitted, 0 received, 100% packet loss")["rtt_ms"] is None


def test_traceroute():
    run = Recorder(TRACEROUTE)
    result = probes.traceroute("eth0", "1.1.1.1", 20, run=run)
    assert result["hops"] == [{"hop": 1, "ip": "192.168.1.1", "rtt_ms": 0.981},
                              {"hop": 2, "ip": None, "rtt_ms": None},
                              {"hop": 3, "ip": "10.0.0.1", "rtt_ms": 5.123}]
    assert run.calls[0][0] == ["traceroute", "-i", "eth0", "-n", "-q", "1", "-w", "2", "-m", "20", "1.1.1.1"]


def test_ethtool():
    parsed = probes.parse_ethtool(ETHTOOL)
    assert parsed["speed"] == "100Mb/s"
    assert parsed["duplex"] == "Full"
    assert parsed["autoneg"] is True and parsed["link_detected"] is True
    assert parsed["advertised_modes"] == ["10baseT/Half", "10baseT/Full", "100baseT/Half", "100baseT/Full"]
    assert "1000baseT/Full" in parsed["partner_modes"]
    assert probes.parse_ethtool("Settings for eth0:\n\tLink partner advertised link modes:  Not reported\n")["partner_modes"] == []


def test_ethtool_stats():
    assert probes.parse_ethtool_stats(STATS) == {"tx_packets": 1234, "rx_errors": 3, "rx_missed": 0}


def test_http_check():
    run = Recorder(CURL)
    result = probes.http_check("eth0", "https://1.1.1.1/", run=run)
    assert result["ok"] is True and result["status"] == 200
    assert result["ms"] == {"dns": 1.0, "connect": 12.0, "tls": 45.0, "first_byte": 80.0, "total": 81.0}
    assert result["cert_expires"] == "Jan 15 12:00:00 2027 GMT"
    cmd = run.calls[0][0]
    assert cmd[:3] == ["curl", "--interface", "eth0"] and cmd[-1] == "https://1.1.1.1/"


def test_http_check_failure():
    raw = 'curl: (6) Could not resolve host\nETHTESTER_JSON {"http_code":0,"exitcode":6,"errormsg":"Could not resolve host: x"}\n'
    result = probes.parse_curl(raw)
    assert result["ok"] is False and result["status"] is None and "resolve" in result["error"]
    assert probes.parse_curl("")["ok"] is False


def test_dhcp_renew():
    lease = "IP4.ADDRESS[1]:192.168.1.222/24\nIP4.GATEWAY:192.168.1.1\n"
    run = Recorder("", "", "", lease)
    times = iter([0.0, 0.0, 1.0, 2.5, 2.5])
    result = probes.dhcp_renew("eth0", run=run, clock=lambda: next(times), sleep=lambda s: None)
    assert result["ok"] is True and result["ip"] == "192.168.1.222" and result["seconds_to_ip"] == 2.5
    assert run.calls[0][0] == ["nmcli", "--wait", "10", "device", "disconnect", "eth0"]
    assert run.calls[1][0] == ["nmcli", "--wait", "0", "device", "connect", "eth0"]


def test_dhcp_renew_timeout():
    times = iter([0.0, 0.0, 31.0])
    result = probes.dhcp_renew("eth0", run=Recorder(), clock=lambda: next(times), sleep=lambda s: None)
    assert result["ok"] is False and "30" in result["error"]


def test_neighbour_scan():
    run = Recorder(ARP)
    result = probes.neighbour_scan("eth0", run=run)
    assert result["neighbours"][0] == {"ip": "192.168.1.1", "mac": "60:38:e0:aa:bb:cc",
                                       "vendor": "Belkin International Inc."}
    assert run.calls[0][0][:3] == ["arp-scan", "--interface", "eth0"]


def test_speed_test():
    run = Recorder(IPERF)
    result = probes.speed_test("eth0", "mac.lan", 10, True, run=run)
    assert result == {"server": "mac.lan", "reverse": True, "ok": True, "mbps": 93.4, "retransmits": 4}
    assert run.calls[0][0] == ["iperf3", "-c", "mac.lan", "--bind-dev", "eth0", "-t", "10", "-J", "-R"]
    assert probes.parse_iperf('{"error": "unable to connect"}') == {"ok": False, "error": "unable to connect"}
    assert probes.parse_iperf("garbage")["ok"] is False
