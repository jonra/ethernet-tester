from pathlib import Path

from PIL import ImageDraw, ImageOps

from ethtester.render import (
    FAIL, HEIGHT, OK, ROW_FONT, SKIP, WAIT, WIDTH, Row, _fit, build_rows, render, render_message,
)
from ethtester.results import DhcpResult, GatewayResult, LinkResult, Snapshot
from ethtester.verdict import Verdict, evaluate
from tests.factories import ok_snapshot

OUT = Path(__file__).parent / "output"


def rows_for(s: Snapshot) -> list[Row]:
    return build_rows(s, evaluate(s, 1000, 20))


def test_rows_all_ok():
    assert rows_for(ok_snapshot()) == [
        Row("LINK", OK, "1000M full  flaps:0"),
        Row("PORT", OK, "SW-Office / gi0/14"),
        Row("IP", OK, "192.168.1.57/24  2.1s"),
        Row("GW", OK, "192.168.1.1  3ms 0%"),
        Row("DNS", OK, "192.168.1.1"),
        Row("NET", OK, "84.1.2.3"),
    ]


def test_rows_link_down():
    rows = rows_for(Snapshot(link=LinkResult(up=False)))
    assert rows[0] == Row("LINK", FAIL, "down")
    assert all(r.status == SKIP for r in rows[1:])


def test_rows_waiting_and_no_dhcp():
    waiting = ok_snapshot(dhcp=DhcpResult(), seconds_since_link=3.0, lldp=None,
                          gateway=None, dns=None, internet=None)
    rows = rows_for(waiting)
    assert rows[1] == Row("PORT", WAIT, "…")
    assert rows[2] == Row("IP", WAIT, "waiting…")
    assert [r.status for r in rows[3:]] == [SKIP, SKIP, SKIP]
    failed = ok_snapshot(dhcp=DhcpResult(), seconds_since_link=30.0, lldp=None,
                         lldp_given_up=True, gateway=None, dns=None, internet=None)
    rows = rows_for(failed)
    assert rows[1] == Row("PORT", SKIP, "no LLDP")
    assert rows[2] == Row("IP", FAIL, "no address")


def test_rows_slow_link_and_gateway_fail():
    rows = rows_for(ok_snapshot(link=LinkResult(up=True, speed_mbps=100, duplex="full")))
    assert rows[0] == Row("LINK", FAIL, "100M full  flaps:0")
    rows = rows_for(ok_snapshot(gateway=GatewayResult(False), dns=None, internet=None))
    assert rows[3] == Row("GW", FAIL, "192.168.1.1  100% loss")


def test_fit_truncates_to_width():
    draw = ImageDraw.Draw(render_message("x", "y"))
    text = _fit(draw, "a-very-long-switch-name-that-goes-on / GigabitEthernet1/0/48", ROW_FONT, 200)
    assert text.endswith("…")
    assert draw.textlength(text, font=ROW_FONT) <= 200


def is_drawn(image) -> bool:
    return ImageOps.invert(image.convert("L")).getbbox() is not None


def test_render_samples():
    OUT.mkdir(exist_ok=True)
    samples = {
        "ok": ok_snapshot(),
        "no_link": Snapshot(link=LinkResult(up=False)),
        "cable_100m": ok_snapshot(link=LinkResult(up=True, speed_mbps=100, duplex="full")),
        "no_dhcp": ok_snapshot(dhcp=DhcpResult(), seconds_since_link=30.0, lldp=None,
                               lldp_given_up=True, gateway=None, dns=None, internet=None),
    }
    for name, snap in samples.items():
        image = render(rows_for(snap), evaluate(snap, 1000, 20), "10:42")
        assert image.size == (WIDTH, HEIGHT)
        assert image.mode == "1"
        assert is_drawn(image)
        image.save(OUT / f"{name}.png")


def test_render_message():
    image = render_message("ethtester", "waiting for link  v0.1.0")
    assert image.size == (WIDTH, HEIGHT) and is_drawn(image)


from ethtester.render import render_probe  # noqa: E402

PROBE_BASE = {"id": "1", "tool": "ping", "target": "1.1.1.1", "state": "running", "started": 1000.0,
              "finished": None, "settle_until": None, "summary": [], "calls": 3}
PROBE_SAMPLES = {
    "probe_running": PROBE_BASE,
    "probe_ok": {**PROBE_BASE, "state": "ok", "finished": 1002.0, "settle_until": 1002.0,
                 "summary": ["sent 5  received 5  loss 0%", "rtt 1.0 / 1.5 / 2.0 ms"]},
    "probe_error": {**PROBE_BASE, "tool": "dns_lookup", "target": "example.com", "state": "error",
                    "finished": 1002.0, "settle_until": 1002.0,
                    "summary": ["no answer from 192.168.1.1: timed", "out"]},
    "probe_long": {**PROBE_BASE, "tool": "http_check",
                   "target": "https://a-very-long-host-name.example.com/some/path?q=1",
                   "state": "ok", "finished": 1002.0, "settle_until": 1002.0, "calls": 1234,
                   "summary": ["HTTP 200  total 1234ms and a lot more text that will not fit",
                               "cert expires Jan 12 23:59:59 2027 GMT plus more",
                               "third line WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW",
                               "fourth line", "fifth line is dropped"]},
}


def text_band_is_blank(image, top, bottom) -> bool:
    band = image.crop((0, top, WIDTH, bottom))
    return ImageOps.invert(band.convert("L")).getbbox() is None


def test_render_probe_samples():
    OUT.mkdir(exist_ok=True)
    for name, activity in PROBE_SAMPLES.items():
        image = render_probe(activity, "10:42")
        assert image.size == (WIDTH, HEIGHT)
        assert image.mode == "1"
        assert is_drawn(image)
        image.save(OUT / f"{name}.png")


def test_render_probe_states_differ():
    images = {name: render_probe(a, "10:42").tobytes() for name, a in PROBE_SAMPLES.items()}
    assert len(set(images.values())) == len(images)


def test_render_probe_running_has_empty_body():
    from ethtester.render import PROBE_BODY_Y, PROBE_FOOT_DIVIDER_Y
    image = render_probe(PROBE_BASE, "10:42")
    assert text_band_is_blank(image, PROBE_BODY_Y, PROBE_FOOT_DIVIDER_Y)


def test_render_probe_shows_at_most_four_summary_lines():
    four = {**PROBE_SAMPLES["probe_long"], "summary": PROBE_SAMPLES["probe_long"]["summary"][:4]}
    assert render_probe(four, "10:42").tobytes() == render_probe(PROBE_SAMPLES["probe_long"], "10:42").tobytes()


def test_render_probe_text_fits_and_is_truncated():
    from ethtester.render import HEAD_FONT, HEAD_SMALL_FONT, probe_lines
    draw = ImageDraw.Draw(render_message("x", "y"))
    lines = probe_lines(draw, PROBE_SAMPLES["probe_long"], "10:42")
    header, body, verdict, right = lines["header"], lines["body"], lines["verdict"], lines["right"]
    assert header.startswith("PROBE  http_check https://a-") and header.endswith("…")
    assert lines["header_font"] is HEAD_SMALL_FONT
    assert draw.textlength(header, font=HEAD_SMALL_FONT) <= WIDTH - 4
    short = probe_lines(draw, PROBE_BASE, "10:42")
    assert short["header"] == "PROBE  ping 1.1.1.1" and short["header_font"] is HEAD_FONT
    medium = probe_lines(draw, {**PROBE_BASE, "tool": "dns_lookup", "target": "example.com"}, "10:42")
    assert medium["header"] == "PROBE  dns_lookup example.com"
    assert len(body) == 4 and body[0].endswith("…") and body[3] == "fourth line"
    assert all(draw.textlength(line, font=ROW_FONT) <= WIDTH - 4 for line in body)
    assert verdict == "OK" and right == "1234 calls 10:42"
    assert probe_lines(draw, PROBE_BASE, "10:42")["verdict"] == "RUNNING…"
    assert probe_lines(draw, PROBE_SAMPLES["probe_error"], "10:42")["verdict"] == "FAILED"
    assert probe_lines(draw, {**PROBE_BASE, "calls": 1}, "09:00")["right"] == "1 call 09:00"
    assert probe_lines(draw, {**PROBE_BASE, "tool": "dhcp_renew", "target": ""}, "09:00")["header"] == "PROBE  dhcp_renew"


def test_probe_lines_defensively_strip_control_characters():
    from ethtester.render import probe_lines
    draw = ImageDraw.Draw(render_message("x", "y"))
    act = {**PROBE_BASE, "target": "a\r\nb", "summary": ["x\ny\x07z"]}
    lines = probe_lines(draw, act, "10:42")
    assert lines["header"] == "PROBE  ping a b" and lines["body"] == ["x y z"]
