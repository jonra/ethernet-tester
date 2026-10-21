import pytest

from ethtester.mcp.linkctl import Busy, LinkControl

AUTONEG_ON = "Settings for eth0:\n\tSpeed: 100Mb/s\n\tAuto-negotiation: on\n"
AUTONEG_OFF = "Settings for eth0:\n\tSpeed: 10Mb/s\n\tAuto-negotiation: off\n"


class FakeRun:
    def __init__(self, ethtool=AUTONEG_ON, devices="eth0:connected\n"):
        self.ethtool = ethtool
        self.devices = devices
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd == ["ethtool", "eth0"]:
            return self.ethtool
        if cmd[:4] == ["nmcli", "-t", "-f", "DEVICE,STATE"]:
            return self.devices
        return ""


class FakeTimer:
    created = []

    def __init__(self, delay, fn):
        self.delay, self.fn = delay, fn
        FakeTimer.created.append(self)

    def start(self):
        pass


def make(run=None):
    FakeTimer.created = []
    run = run or FakeRun()
    return run, LinkControl("eth0", run=run, timer=FakeTimer, clock=lambda: 100.0)


def test_renegotiate():
    run, ctl = make()
    assert ctl.renegotiate() == {"action": "renegotiate"}
    assert run.calls == [["ethtool", "-r", "eth0"]]


def test_force_holds_lock_until_revert():
    run, ctl = make()
    assert ctl.force(10, 60) == {"action": "force_10", "reverts_in_s": 60}
    assert run.calls[0] == ["ethtool", "-s", "eth0", "speed", "10", "duplex", "full", "autoneg", "off"]
    assert ctl.revert_in() == 60.0
    with pytest.raises(Busy, match="reverts in 60"):
        ctl.renegotiate()
    with pytest.raises(Busy):
        with ctl.exclusive():
            pass
    run.ethtool = AUTONEG_OFF
    timer = FakeTimer.created[0]
    assert timer.delay == 60 and timer.daemon is True
    timer.fn()
    assert run.calls[-1] == ["ethtool", "-s", "eth0", "autoneg", "on"]
    assert ctl.revert_in() is None
    with ctl.exclusive():
        pass


def test_force_failure_releases_lock():
    class Failing(FakeRun):
        def __call__(self, cmd, **kwargs):
            if cmd[:2] == ["ethtool", "-s"]:
                raise RuntimeError("ethtool failed")
            return super().__call__(cmd, **kwargs)

    _, ctl = make(Failing())
    with pytest.raises(RuntimeError):
        ctl.force(100, 30)
    with ctl.exclusive():
        pass


def test_restore_only_when_autoneg_off():
    run, ctl = make()
    ctl.restore_autoneg()
    assert ["ethtool", "-s", "eth0", "autoneg", "on"] not in run.calls
    run.ethtool = AUTONEG_OFF
    ctl.restore_autoneg()
    assert run.calls[-1] == ["ethtool", "-s", "eth0", "autoneg", "on"]


def test_recover_reconnects_disconnected_device():
    run, ctl = make(FakeRun(ethtool=AUTONEG_ON, devices="wlan0:connected\neth0:disconnected\n"))
    ctl.recover()
    assert run.calls[-1] == ["nmcli", "--wait", "0", "device", "connect", "eth0"]
    run2, ctl2 = make(FakeRun())
    ctl2.recover()
    assert ["nmcli", "--wait", "0", "device", "connect", "eth0"] not in run2.calls
