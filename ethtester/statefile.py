"""Latest status for other processes (the MCP server), replaced atomically each tick."""

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path


def write_state(path: Path, snapshot, verdict, wall_time: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "ts": datetime.fromtimestamp(wall_time).isoformat(timespec="seconds"),
        "epoch": wall_time,
        "verdict": verdict.code,
        "text": verdict.text,
        "snapshot": asdict(snapshot),
    }
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
