import json
from pathlib import Path

from ethtester.statefile import write_state
from ethtester.verdict import evaluate
from tests.factories import ok_snapshot


def test_write_state(tmp_path: Path):
    path = tmp_path / "run" / "state.json"
    s = ok_snapshot()
    write_state(path, s, evaluate(s, 1000, 20), 1_700_000_000.5)
    data = json.loads(path.read_text())
    assert data["verdict"] == "OK"
    assert data["text"] == "OK"
    assert data["epoch"] == 1_700_000_000.5
    assert data["snapshot"]["dhcp"]["ip"] == "192.168.1.57"
    assert "ts" in data
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


def test_write_state_replaces(tmp_path: Path):
    path = tmp_path / "state.json"
    s = ok_snapshot()
    write_state(path, s, evaluate(s, 1000, 20), 1.0)
    write_state(path, s, evaluate(s, 1000, 20), 2.0)
    assert json.loads(path.read_text())["epoch"] == 2.0
