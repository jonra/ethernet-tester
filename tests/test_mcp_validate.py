from datetime import datetime, timezone

import pytest

from ethtester.mcp import validate


@pytest.mark.parametrize("value", ["example.com", "ap-1.lan", "192.168.1.1", "2001:db8::1", "localhost", "a.b.c."])
def test_host_accepts(value):
    assert validate.host(value) == value


@pytest.mark.parametrize("value", ["", "-f", "a;b", "a b", "$(reboot)", "a..b", "x" * 254, "-c 5 1.1.1.1", "example.com\n", "1.1.1.1\n", "fe80::1%eth0"])
def test_host_rejects(value):
    with pytest.raises(ValueError):
        validate.host(value)


def test_ipv4():
    assert validate.ipv4("10.0.0.1") == "10.0.0.1"
    for bad in ("::1", "dns.google", "10.0.0.256"):
        with pytest.raises(ValueError):
            validate.ipv4(bad)


def test_bounded():
    assert validate.bounded("count", 5, 1, 20) == 5
    for bad in (0, 21, True, 2.5, "5"):
        with pytest.raises(ValueError, match="count"):
            validate.bounded("count", bad, 1, 20)


def test_url():
    assert validate.url("https://example.com/x?y=1") == "https://example.com/x?y=1"
    for bad in ("ftp://example.com", "file:///etc/passwd", "https://", "http://-x/", "https://a b.com", "example.com"):
        with pytest.raises(ValueError):
            validate.url(bad)


def test_record_type():
    assert validate.record_type("aaaa") == "AAAA"
    with pytest.raises(ValueError):
        validate.record_type("ANY")


def test_timestamp():
    assert validate.timestamp("2026-10-21T09:40:00") == datetime(2026, 10, 21, 9, 40)
    aware = validate.timestamp("2026-10-21T07:40:00+00:00")
    assert aware.tzinfo is None
    assert aware == datetime(2026, 10, 21, 7, 40, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    with pytest.raises(ValueError):
        validate.timestamp("yesterday")


def test_boolean():
    assert validate.boolean("reverse", True) is True
    with pytest.raises(ValueError):
        validate.boolean("reverse", "yes")
