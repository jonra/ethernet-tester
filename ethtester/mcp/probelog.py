"""One JSON line per MCP tool call, so the user can see what Claude ran."""

import json
import logging
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class ProbeLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger = logging.getLogger(f"ethtester.probes.{path}")
        self.logger.handlers[:] = [handler]
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

    def record(self, tool: str, args: dict, duration_s: float, ok: bool, error: str | None = None,
               wall_time: float | None = None) -> None:
        when = datetime.fromtimestamp(wall_time if wall_time is not None else time.time())
        self.logger.info(json.dumps({"ts": when.isoformat(timespec="seconds"), "tool": tool, "args": args,
                                     "duration_s": duration_s, "ok": ok, "error": error}))
