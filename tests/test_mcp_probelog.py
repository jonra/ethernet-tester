import json

from ethtester.mcp.probelog import ProbeLog


def test_long_string_args_are_truncated(tmp_path):
    log = ProbeLog(tmp_path / "probes.log")
    log.record("http_check", {"url": "x" * 300, "short": "y" * 256, "count": 5, "none": None}, 0.1, True,
               wall_time=0.0)
    entry = json.loads((tmp_path / "probes.log").read_text().splitlines()[-1])
    assert entry["args"] == {"url": "x" * 256 + "…", "short": "y" * 256, "count": 5, "none": None}
    assert entry["tool"] == "http_check" and entry["ok"] is True
