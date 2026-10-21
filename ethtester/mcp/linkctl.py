"""Link changes for diagnosis: renegotiate or force a speed, always reverting to autoneg."""

import logging
import threading
import time
from contextlib import contextmanager

from ..checks.command import run_command
from .probes import parse_ethtool

log = logging.getLogger(__name__)


class Busy(Exception):
    pass


class LinkControl:
    def __init__(self, interface: str, run=run_command, timer=threading.Timer, clock=time.monotonic):
        self.interface = interface
        self.run = run
        self.timer = timer
        self.clock = clock
        self.lock = threading.Lock()
        self._revert_at: float | None = None

    def _busy_message(self) -> str:
        left = self.revert_in()
        if left is not None:
            return f"link is forced; reverts in {left:.0f} s"
        return "another link or DHCP action is running"

    @contextmanager
    def exclusive(self):
        if not self.lock.acquire(blocking=False):
            raise Busy(self._busy_message())
        try:
            yield
        finally:
            self.lock.release()

    def revert_in(self) -> float | None:
        return None if self._revert_at is None else max(0.0, self._revert_at - self.clock())

    def renegotiate(self) -> dict:
        with self.exclusive():
            self.run(["ethtool", "-r", self.interface])
        return {"action": "renegotiate"}

    def force(self, speed: int, duration_s: int) -> dict:
        """Forces a speed and keeps the lock until the timer has restored autoneg."""
        if not self.lock.acquire(blocking=False):
            raise Busy(self._busy_message())
        try:
            self.run(["ethtool", "-s", self.interface, "speed", str(speed), "duplex", "full", "autoneg", "off"])
        except Exception:
            self.lock.release()
            raise
        self._revert_at = self.clock() + duration_s
        timer = self.timer(duration_s, self._revert)
        timer.daemon = True
        timer.start()
        return {"action": f"force_{speed}", "reverts_in_s": duration_s}

    def _revert(self) -> None:
        try:
            self.restore_autoneg()
        except Exception:
            log.exception("autoneg revert failed")
        finally:
            self._revert_at = None
            self.lock.release()

    def restore_autoneg(self) -> None:
        """Turns autoneg back on only when it is off, so a healthy link doesn't bounce."""
        settings = parse_ethtool(self.run(["ethtool", self.interface], check=False))
        if settings["speed"] is not None and not settings["autoneg"]:
            self.run(["ethtool", "-s", self.interface, "autoneg", "on"])

    def recover(self) -> None:
        """Undoes anything a crashed previous run may have left: forced speed, disconnected device."""
        self.restore_autoneg()
        states = self.run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"], check=False)
        for line in states.splitlines():
            device, _, state = line.partition(":")
            if device == self.interface and state == "disconnected":
                self.run(["nmcli", "--wait", "0", "device", "connect", self.interface], check=False)
