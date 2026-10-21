from ethtester.screens import ScreenSelector


def running(id="a", started=1000.0):
    return {"id": id, "tool": "ping", "target": "1.1.1.1", "state": "running", "started": started,
            "finished": None, "settle_until": None, "summary": [], "calls": 1}


def done(id="a", finished=1010.0, settle_until=None, state="ok"):
    return {**running(id), "state": state, "finished": finished,
            "settle_until": finished if settle_until is None else settle_until, "summary": ["x"]}


def test_no_activity_is_ethernet():
    sel = ScreenSelector()
    assert sel.choose(None, "OK", 1000.0) == "ethernet"
    assert sel.fast(None) is False


def test_running_is_probe_and_fast():
    sel = ScreenSelector()
    assert sel.choose(running(), "OK", 1001.0) == "probe"
    assert sel.fast(running()) is True
    # a probe disturbing the link doesn't dismiss its own RUNNING screen
    assert sel.choose(running(), "NO_DHCP", 1002.0) == "probe"


def test_finished_within_hold_with_unchanged_verdict_is_probe():
    sel = ScreenSelector()
    for now in (1010.0, 1100.0, 1309.0):
        assert sel.choose(done(), "OK", now) == "probe"
    assert sel.fast(done()) is False


def test_hold_expiry_is_ethernet():
    sel = ScreenSelector(hold_s=300)
    assert sel.choose(done(), "OK", 1010.0) == "probe"
    assert sel.choose(done(), "OK", 1310.0) == "ethernet"
    assert sel.choose(done(), "OK", 1311.0) == "ethernet"


def test_verdict_change_after_settle_dismisses_for_that_id():
    sel = ScreenSelector()
    assert sel.choose(done(), "OK", 1010.0) == "probe"      # baseline OK
    assert sel.choose(done(), "NO_LINK", 1015.0) == "ethernet"
    assert sel.choose(done(), "OK", 1020.0) == "ethernet"   # stays dismissed


def test_verdict_change_before_settle_is_still_probe():
    sel = ScreenSelector()
    activity = done(finished=1010.0, settle_until=1020.0)   # e.g. dhcp_renew: finished + 10 s
    assert sel.choose(activity, "OK", 1011.0) == "probe"
    assert sel.choose(activity, "NO_DHCP", 1015.0) == "probe"
    assert sel.choose(activity, "OK", 1021.0) == "probe"     # baseline taken now: OK
    assert sel.choose(activity, "OK", 1030.0) == "probe"
    assert sel.choose(activity, "SLOW_LINK", 1035.0) == "ethernet"


def test_baseline_is_first_tick_after_settle_even_if_already_disturbed():
    sel = ScreenSelector()
    activity = done(finished=1010.0, settle_until=1020.0)
    assert sel.choose(activity, "SLOW_LINK", 1021.0) == "probe"   # baseline SLOW_LINK
    assert sel.choose(activity, "SLOW_LINK", 1030.0) == "probe"
    assert sel.choose(activity, "OK", 1031.0) == "ethernet"


def test_new_id_is_probe_again():
    sel = ScreenSelector()
    assert sel.choose(done("a"), "OK", 1010.0) == "probe"
    assert sel.choose(done("a"), "NO_LINK", 1011.0) == "ethernet"
    assert sel.choose(running("b", started=1012.0), "NO_LINK", 1012.0) == "probe"
    assert sel.choose(done("b", finished=1013.0), "NO_LINK", 1013.0) == "probe"  # new baseline
    assert sel.choose(done("b", finished=1013.0), "NO_LINK", 1100.0) == "probe"


def test_error_result_follows_the_same_rules():
    sel = ScreenSelector()
    assert sel.choose(done(state="error"), "OK", 1010.0) == "probe"
    assert sel.choose(done(state="error"), "OK", 1400.0) == "ethernet"


def test_stale_running_activity_gives_up_after_hold():
    # the MCP server died mid-probe: don't show RUNNING (and poll fast) forever
    sel = ScreenSelector(hold_s=300)
    assert sel.choose(running(started=1000.0), "OK", 1170.0) == "probe"
    assert sel.choose(running(started=1000.0), "OK", 1300.0) == "ethernet"
    assert sel.fast(running(started=1000.0)) is False


def test_clock_step_back_expires_activity():
    assert ScreenSelector().choose(done(finished=1010.0), "OK", 1000.0) == "ethernet"
    assert ScreenSelector().choose(running(started=1010.0), "OK", 1000.0) == "ethernet"


def test_running_activity_stale_after_180s_even_with_longer_hold():
    sel = ScreenSelector(hold_s=300)
    assert sel.choose(running(started=1000.0), "OK", 1179.0) == "probe"
    assert sel.choose(running(started=1000.0), "OK", 1180.0) == "ethernet"
