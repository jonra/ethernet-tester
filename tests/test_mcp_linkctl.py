import pytest

from ethtester.mcp.linkctl import Busy, LinkControl, full_advertise_mask
from ethtester.mcp.probes import parse_ethtool

AUTONEG_ON = "Settings for eth0:\n\tSpeed: 100Mb/s\n\tAuto-negotiation: on\n"
AUTONEG_OFF = "Settings for eth0:\n\tSpeed: 10Mb/s\n\tAuto-negotiation: off\n"
RESTORE = ["ethtool", "-s", "eth0", "autoneg", "on", "advertise", "0x00f"]
MODES_100 = "10baseT/Half 10baseT/Full\n\t                        100baseT/Half 100baseT/Full"


def ethtool_out(advertised=MODES_100, autoneg="on", supported=MODES_100):
    return ("Settings for eth0:\n"
            f"\tSupported link modes:   {supported}\n"
            f"\tAdvertised link modes:  {advertised}\n"
            "\tSpeed: 100Mb/s\n"
            f"\tAuto-negotiation: {autoneg}\n")


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
    assert run.calls[0] == ["ethtool", "-s", "eth0", "advertise", "0x002"]
    assert ctl.revert_in() == 60.0
    with pytest.raises(Busy, match="reverts in 60"):
        ctl.renegotiate()
    with pytest.raises(Busy):
        with ctl.exclusive():
            pass
    run.ethtool = ethtool_out(advertised="10baseT/Full")
    timer = FakeTimer.created[0]
    assert timer.delay == 60 and timer.daemon is True
    timer.fn()
    assert run.calls[-1] == RESTORE
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


def test_force_100_advertises_100_full_only():
    run, ctl = make()
    ctl.force(100, 30)
    assert run.calls[0] == ["ethtool", "-s", "eth0", "advertise", "0x008"]


def test_restore_runs_when_autoneg_off():
    run, ctl = make()
    ctl.restore_autoneg()
    assert not any(c[:2] == ["ethtool", "-s"] for c in run.calls)
    run.ethtool = AUTONEG_OFF
    ctl.restore_autoneg()
    assert run.calls[-1] == RESTORE
    run.ethtool = ethtool_out(autoneg="off")
    ctl.restore_autoneg()
    assert run.calls[-1] == RESTORE


def test_restore_runs_when_advertising_is_restricted():
    run, ctl = make(FakeRun(ethtool=ethtool_out(advertised="100baseT/Full")))
    ctl.restore_autoneg()
    assert run.calls[-1] == RESTORE


def test_restore_noop_when_advertising_everything_supported():
    reordered = "100baseT/Full 100baseT/Half 10baseT/Full 10baseT/Half"
    run, ctl = make(FakeRun(ethtool=ethtool_out(advertised=reordered)))
    ctl.restore_autoneg()
    assert run.calls == [["ethtool", "eth0"]]


def test_full_advertise_mask_from_supported_modes():
    assert full_advertise_mask(parse_ethtool(ethtool_out())) == 0x00f
    gig = "10baseT/Full 100baseT/Full 1000baseT/Half 1000baseT/Full 2500baseT/Full"
    assert full_advertise_mask(parse_ethtool(ethtool_out(supported=gig))) == 0x03a
    assert full_advertise_mask(parse_ethtool(ethtool_out(supported="2500baseT/Full"))) == 0x00f
    assert full_advertise_mask(parse_ethtool(AUTONEG_ON)) == 0x00f


def test_restore_uses_mask_of_supported_modes():
    run, ctl = make(FakeRun(ethtool=ethtool_out(advertised="10baseT/Full", supported="10baseT/Full 100baseT/Full")))
    ctl.restore_autoneg()
    assert run.calls[-1] == ["ethtool", "-s", "eth0", "autoneg", "on", "advertise", "0x00a"]


def test_recover_reconnects_disconnected_device():
    run, ctl = make(FakeRun(ethtool=AUTONEG_ON, devices="wlan0:connected\neth0:disconnected\n"))
    ctl.recover()
    assert run.calls[-1] == ["nmcli", "--wait", "0", "device", "connect", "eth0"]
    run2, ctl2 = make(FakeRun())
    ctl2.recover()
    assert ["nmcli", "--wait", "0", "device", "connect", "eth0"] not in run2.calls


class Scripted(FakeRun):
    """FakeRun whose ethtool settings write and nmcli calls can be made to fail."""

    def __init__(self, fail_force=False, fail_restores=0, fail_nmcli=False, **kw):
        super().__init__(**kw)
        self.fail_force, self.fail_restores, self.fail_nmcli = fail_force, fail_restores, fail_nmcli

    def __call__(self, cmd, **kwargs):
        if cmd[:2] == ["ethtool", "-s"]:
            if "autoneg" not in cmd:
                self.calls.append(cmd)
                if self.fail_force:
                    raise RuntimeError("ethtool failed")
                return ""
            if self.fail_restores > 0:
                self.fail_restores -= 1
                self.calls.append(cmd)
                raise RuntimeError("restore failed")
        if cmd[0] == "nmcli" and self.fail_nmcli:
            self.calls.append(cmd)
            raise RuntimeError("nmcli failed")
        return super().__call__(cmd, **kwargs)


def test_force_ethtool_failure_attempts_restore():
    run = Scripted(fail_force=True, ethtool=AUTONEG_OFF)
    _, ctl = make(run)
    with pytest.raises(RuntimeError, match="ethtool failed"):
        ctl.force(100, 30)
    assert RESTORE in run.calls
    with ctl.exclusive():
        pass


def test_timer_start_failure_releases_lock_and_restores():
    class BadTimer(FakeTimer):
        def start(self):
            raise RuntimeError("can't start new thread")

    run = FakeRun(ethtool=AUTONEG_OFF)
    ctl = LinkControl("eth0", run=run, timer=BadTimer, clock=lambda: 100.0)
    with pytest.raises(RuntimeError, match="thread"):
        ctl.force(10, 60)
    assert run.calls[-1] == RESTORE
    assert ctl.revert_in() is None
    with ctl.exclusive():
        pass


def test_revert_retries_until_autoneg_restored():
    run = Scripted(fail_restores=2, ethtool=AUTONEG_OFF)
    sleeps = []
    FakeTimer.created = []
    ctl = LinkControl("eth0", run=run, timer=FakeTimer, clock=lambda: 100.0, sleep=sleeps.append)
    ctl.force(10, 60)
    FakeTimer.created[0].fn()
    restores = [c for c in run.calls if c == RESTORE]
    assert len(restores) == 3
    assert len(sleeps) == 2
    assert ctl.revert_in() is None
    with ctl.exclusive():
        pass


def test_revert_gives_up_after_three_attempts_and_still_unlocks():
    run = Scripted(fail_restores=99, ethtool=AUTONEG_OFF)
    FakeTimer.created = []
    ctl = LinkControl("eth0", run=run, timer=FakeTimer, clock=lambda: 100.0, sleep=lambda s: None)
    ctl.force(10, 60)
    FakeTimer.created[0].fn()
    assert len([c for c in run.calls if c == RESTORE]) == 3
    assert ctl.revert_in() is None
    with ctl.exclusive():
        pass


def test_recover_reconnects_even_when_restore_raises():
    run = Scripted(fail_restores=1, ethtool=AUTONEG_OFF, devices="eth0:disconnected\n")
    _, ctl = make(run)
    ctl.recover()
    assert run.calls[-1] == ["nmcli", "--wait", "0", "device", "connect", "eth0"]


def test_recover_never_raises():
    run = Scripted(fail_restores=1, fail_nmcli=True, ethtool=AUTONEG_OFF)
    _, ctl = make(run)
    ctl.recover()
