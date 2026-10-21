"""Link changes for diagnosis: renegotiate or force a speed, always reverting to full advertising.

Forcing keeps autonegotiation on and only narrows what is advertised, so the link partner sees the
reduced speed at full duplex instead of falling back to half duplex (a duplex mismatch)."""

import logging
import threading
import time
from contextlib import contextmanager

from ..checks.command import run_command
from .probes import parse_ethtool

log = logging.getLogger(__name__)

REVERT_ATTEMPTS = 3
REVERT_RETRY_S = 2.0
MODE_BITS = {"10baseT/Half": 0x001, "10baseT/Full": 0x002, "100baseT/Half": 0x004, "100baseT/Full": 0x008,
             "1000baseT/Half": 0x010, "1000baseT/Full": 0x020}
FORCE_MASKS = {10: MODE_BITS["10baseT/Full"], 100: MODE_BITS["100baseT/Full"]}
FALLBACK_MASK = 0x00f


def full_advertise_mask(settings: dict) -> int:
    """ethtool advertise mask for every supported mode we know; 10/100 half+full if none are known."""
    mask = 0
    for mode in settings.get("supported_modes") or []:
        mask |= MODE_BITS.get(mode, 0)
    return mask or FALLBACK_MASK


class Busy(Exception):
    pass


class LinkControl:
    def __init__(self, interface: str, run=run_command, timer=threading.Timer, clock=time.monotonic,
                 sleep=time.sleep):
        self.interface = interface
        self.run = run
        self.timer = timer
        self.clock = clock
        self.sleep = sleep
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
        """Advertises only one speed at full duplex (autoneg stays on) and keeps the lock until the
        timer has restored full advertising."""
        mask = FORCE_MASKS.get(speed)
        if mask is None:
            raise ValueError(f"speed must be 10 or 100, got {speed!r}")
        if not self.lock.acquire(blocking=False):
            raise Busy(self._busy_message())
        try:
            self.run(["ethtool", "-s", self.interface, "advertise", f"0x{mask:03x}"])
        except Exception:
            self._restore_quietly()
            self.lock.release()
            raise
        self._revert_at = self.clock() + duration_s
        try:
            timer = self.timer(duration_s, self._revert)
            timer.daemon = True
            timer.start()
        except Exception:
            self._restore_quietly()
            self._revert_at = None
            self.lock.release()
            raise
        return {"action": f"force_{speed}", "reverts_in_s": duration_s}

    def _restore_quietly(self) -> None:
        try:
            self.restore_autoneg()
        except Exception:
            log.exception("autoneg restore failed")

    def _revert(self) -> None:
        try:
            for attempt in range(REVERT_ATTEMPTS):
                try:
                    self.restore_autoneg()
                    return
                except Exception:
                    log.exception("autoneg revert failed (attempt %d/%d)", attempt + 1, REVERT_ATTEMPTS)
                    if attempt < REVERT_ATTEMPTS - 1:
                        self.sleep(REVERT_RETRY_S)
        finally:
            self._revert_at = None
            self.lock.release()

    def restore_autoneg(self) -> None:
        """Restores autoneg with every supported mode advertised, but only when autoneg is off or the
        advertised modes are restricted, so a healthy link doesn't bounce."""
        settings = parse_ethtool(self.run(["ethtool", self.interface], check=False))
        if settings["speed"] is None:
            return
        restricted = set(settings["advertised_modes"]) != set(settings["supported_modes"])
        if not settings["autoneg"] or restricted:
            mask = full_advertise_mask(settings)
            self.run(["ethtool", "-s", self.interface, "autoneg", "on", "advertise", f"0x{mask:03x}"])

    def recover(self) -> None:
        """Undoes anything a crashed previous run may have left: forced speed, disconnected device."""
        try:
            self.restore_autoneg()
        except Exception:
            log.exception("recover: autoneg restore failed")
        try:
            states = self.run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"], check=False)
            for line in states.splitlines():
                device, _, state = line.partition(":")
                if device == self.interface and state == "disconnected":
                    self.run(["nmcli", "--wait", "0", "device", "connect", self.interface], check=False)
        except Exception:
            log.exception("recover: reconnect failed")
