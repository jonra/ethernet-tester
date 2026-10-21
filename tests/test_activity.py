import json
import os
from pathlib import Path

import pytest

from ethtester.activity import read_activity, write_activity

RUNNING = {"id": "1000.000-1", "tool": "ping", "target": "1.1.1.1", "state": "running",
           "started": 1000.0, "finished": None, "settle_until": None, "summary": [], "calls": 1}
DONE = {**RUNNING, "state": "ok", "finished": 1002.0, "settle_until": 1002.0,
        "summary": ["sent 5  received 5  loss 0%"]}


def test_write_then_read_round_trips(tmp_path: Path):
    path = tmp_path / "run" / "activity.json"
    write_activity(path, RUNNING)
    assert read_activity(path) == RUNNING
    write_activity(path, DONE)
    assert read_activity(path) == DONE
    assert [p.name for p in path.parent.iterdir()] == ["activity.json"]


def test_write_is_atomic_and_cleans_up_on_failure(tmp_path: Path, monkeypatch):
    path = tmp_path / "activity.json"
    write_activity(path, RUNNING)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_activity(path, DONE)
    assert read_activity(path) == RUNNING
    assert [p.name for p in tmp_path.iterdir()] == ["activity.json"]


def test_read_missing_is_none(tmp_path: Path):
    assert read_activity(tmp_path / "nope.json") is None


@pytest.mark.parametrize("content", [
    "", "{", "[1, 2]", "null", '"text"',
    json.dumps({**RUNNING, "id": 5}),
    json.dumps({**RUNNING, "state": "weird"}),
    json.dumps({**RUNNING, "started": "soon"}),
    json.dumps({**RUNNING, "started": True}),
    json.dumps({**DONE, "finished": None}),
    json.dumps({**DONE, "settle_until": "x"}),
    json.dumps({**DONE, "summary": "not a list"}),
    json.dumps({**DONE, "summary": [1, 2]}),
    json.dumps({**DONE, "calls": "3"}),
    json.dumps({k: v for k, v in DONE.items() if k != "tool"}),
])
def test_read_tolerates_corrupt_files(tmp_path: Path, content):
    path = tmp_path / "activity.json"
    path.write_text(content)
    assert read_activity(path) is None


def test_read_tolerates_binary_and_directories(tmp_path: Path):
    path = tmp_path / "activity.json"
    path.write_bytes(b"\xff\xfe\x00garbage")
    assert read_activity(path) is None
    assert read_activity(tmp_path) is None


@pytest.mark.parametrize("field", ["started", "finished", "settle_until"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timestamps_are_rejected(tmp_path: Path, field, bad):
    path = tmp_path / "activity.json"
    path.write_text(json.dumps({**DONE, field: bad}))   # json emits NaN / Infinity
    assert read_activity(path) is None
