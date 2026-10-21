"""What the MCP server is probing right now, shared with the display loop through one small file.

The MCP service writes it (atomically, like statefile); the display loop reads it every tick and
treats anything missing or malformed as "no activity"."""

import json
import os
import tempfile
from pathlib import Path

STATES = ("running", "ok", "error")


def write_activity(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".activity-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid(data) -> bool:
    if not isinstance(data, dict):
        return False
    if not all(isinstance(data.get(k), str) for k in ("id", "tool", "target")):
        return False
    if data.get("state") not in STATES or not _number(data.get("started")):
        return False
    finished, settle = data.get("finished"), data.get("settle_until")
    if data["state"] != "running" and not _number(finished):
        return False
    if finished is not None and not _number(finished):
        return False
    if settle is not None and not _number(settle):
        return False
    summary = data.get("summary")
    if not isinstance(summary, list) or not all(isinstance(line, str) for line in summary):
        return False
    calls = data.get("calls")
    return isinstance(calls, int) and not isinstance(calls, bool)


def read_activity(path: Path) -> dict | None:
    """The current activity, or None if the file is missing, unreadable or malformed. Never raises."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return None
    return data if _valid(data) else None
