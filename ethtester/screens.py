"""Chooses between the Ethernet screen and the probe screen each tick.

The probe screen stays up while a probe runs and after it finishes, until the Ethernet verdict
changes from its baseline or hold_s seconds have passed. The baseline is the verdict at the first
tick at or after the activity's settle_until, so a probe's own disturbance doesn't dismiss it."""

PROBE, ETHERNET = "probe", "ethernet"


class ScreenSelector:
    def __init__(self, hold_s: float = 300):
        self.hold_s = hold_s
        self._id: str | None = None
        self._baseline: str | None = None
        self._dismissed = False

    def _track(self, activity_id: str) -> None:
        if activity_id != self._id:
            self._id, self._baseline, self._dismissed = activity_id, None, False

    def choose(self, activity: dict | None, verdict_code: str, now: float) -> str:
        if activity is None:
            return ETHERNET
        self._track(activity["id"])
        if self._dismissed:
            return ETHERNET
        if activity["state"] == "running":
            # A running activity older than hold_s means the MCP server died mid-probe.
            if now - activity["started"] >= self.hold_s:
                self._dismissed = True
                return ETHERNET
            return PROBE
        finished = activity["finished"]
        if now - finished >= self.hold_s:
            self._dismissed = True
            return ETHERNET
        settle_until = activity.get("settle_until")
        if now < (finished if settle_until is None else settle_until):
            return PROBE
        if self._baseline is None:
            self._baseline = verdict_code
        elif verdict_code != self._baseline:
            self._dismissed = True
            return ETHERNET
        return PROBE

    def fast(self, activity: dict | None) -> bool:
        if activity is None or activity["state"] != "running":
            return False
        return not (activity["id"] == self._id and self._dismissed)
