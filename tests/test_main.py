from pathlib import Path

import pytest
from PIL import Image

from ethtester.config import Config, load_config
from ethtester.display import Display, PngBackend
from ethtester.main import CONFIG_ERROR_S, FAST_INTERVAL, run, start
from ethtester.render import render_message
from ethtester.results import LinkResult, Snapshot
from ethtester.statelog import StateLogger
from tests.factories import ok_snapshot


class ScriptedCollector:
    def __init__(self, *items):
        self.items = list(items)

    def collect(self):
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_run_loop(tmp_path: Path):
    cfg = Config(display="png", png_path=str(tmp_path / "s.png"), interval=5)
    collector = ScriptedCollector(Snapshot(link=LinkResult(up=False)),
                                  RuntimeError("boom"),
                                  ok_snapshot())
    delays = []
    run(cfg, collector, Display(PngBackend(Path(cfg.png_path))),
        StateLogger(tmp_path / "state.log"), sleep=delays.append,
        wall=lambda: 1_700_000_000, ticks=3)

    assert delays == [FAST_INTERVAL, 5, 5]
    assert Image.open(tmp_path / "s.png").size == (250, 122)
    assert len((tmp_path / "state.log").read_text().splitlines()) == 2


class FailingStateLog:
    def record(self, snapshot, verdict, now):
        raise OSError("disk full")


def test_run_survives_statelog_failure(tmp_path: Path):
    cfg = Config(display="png", png_path=str(tmp_path / "s.png"), interval=5)
    collector = ScriptedCollector(Snapshot(link=LinkResult(up=False)))
    delays = []
    run(cfg, collector, Display(PngBackend(Path(cfg.png_path))),
        FailingStateLog(), sleep=delays.append,
        wall=lambda: 1_700_000_000, ticks=1)

    assert delays == [FAST_INTERVAL]
    assert Image.open(tmp_path / "s.png").size == (250, 122)


def test_run_pets_watchdog_every_tick_even_when_tick_fails(tmp_path: Path):
    cfg = Config(display="png", png_path=str(tmp_path / "s.png"), interval=5)
    collector = ScriptedCollector(Snapshot(link=LinkResult(up=False)),
                                  RuntimeError("boom"),
                                  ok_snapshot())
    sent = []
    run(cfg, collector, Display(PngBackend(Path(cfg.png_path))),
        StateLogger(tmp_path / "state.log"), sleep=lambda s: None,
        wall=lambda: 1_700_000_000, ticks=3, notify=sent.append)

    assert sent == ["WATCHDOG=1"] * 3


class Shown:
    def __init__(self):
        self.images = []

    def show(self, image, full):
        self.images.append(image.tobytes())


def test_start_shows_config_error_and_falls_back(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text("interval = \n")
    shown, waits, made = Shown(), [], []

    def factory(cfg):
        made.append(cfg)
        return shown

    cfg, display = start(path, sleep=waits.append, backend_factory=factory)

    assert cfg == Config()
    assert made == [Config()]
    assert waits == [CONFIG_ERROR_S] and CONFIG_ERROR_S == 15
    with pytest.raises(ValueError) as exc:
        load_config(path)
    reason = str(exc.value).splitlines()[0]
    assert reason.startswith("invalid TOML")
    assert shown.images[0] == render_message("CONFIG ERROR", reason).tobytes()


def test_start_without_error_does_not_wait(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text(f'display = "png"\npng_path = "{tmp_path / "s.png"}"\n')
    waits = []
    cfg, display = start(path, sleep=waits.append)
    assert waits == []
    assert isinstance(display.backend, PngBackend)
    assert Image.open(tmp_path / "s.png").size == (250, 122)


def test_start_falls_back_to_png_when_display_fails(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text(f'display = "V4"\npng_path = "{tmp_path / "s.png"}"\n')

    def broken(cfg):
        raise RuntimeError("no SPI")

    cfg, display = start(path, sleep=lambda s: None, backend_factory=broken)
    assert isinstance(display.backend, PngBackend)
    assert display.backend.path == tmp_path / "s.png"
    assert (tmp_path / "s.png").exists()


def test_start_survives_failing_startup_screen(tmp_path: Path):
    path = tmp_path / "c.toml"
    path.write_text('display = "png"\n')

    class Broken:
        def show(self, image, full):
            raise OSError("panel gone")

    cfg, display = start(path, sleep=lambda s: None, backend_factory=lambda cfg: Broken())
    assert display.backend.__class__ is Broken


from ethtester.config import Config as _Config
from ethtester.display import Display as _Display, PngBackend as _PngBackend
from ethtester.main import run as _run
from ethtester.statelog import StateLogger as _StateLogger


def test_run_writes_state_and_survives_state_failure(tmp_path):
    cfg = _Config(display="png", png_path=str(tmp_path / "s.png"))
    written = []
    _run(cfg, ScriptedCollector(ok_snapshot()), _Display(_PngBackend(Path(cfg.png_path))),
         _StateLogger(tmp_path / "a.log"), sleep=lambda d: None, wall=lambda: 1.0, ticks=1,
         notify=lambda m: None, write_state=lambda s, v, now: written.append((v.code, now)))
    assert written == [("OK", 1.0)]

    def failing(s, v, now):
        raise OSError("read-only")

    png = tmp_path / "t.png"
    _run(cfg, ScriptedCollector(ok_snapshot()), _Display(_PngBackend(png)),
         _StateLogger(tmp_path / "b.log"), sleep=lambda d: None, wall=lambda: 1.0, ticks=1,
         notify=lambda m: None, write_state=failing)
    assert png.exists()


import time as _time  # noqa: E402

from ethtester import main as main_mod  # noqa: E402
from ethtester.render import build_rows, render, render_probe  # noqa: E402
from ethtester.screens import ScreenSelector  # noqa: E402
from ethtester.verdict import evaluate  # noqa: E402

ACT = {"id": "1", "tool": "ping", "target": "1.1.1.1", "state": "running", "started": 1000.0,
       "finished": None, "settle_until": None, "summary": [], "calls": 1}
ACT_DONE = {**ACT, "state": "ok", "finished": 1001.0, "settle_until": 1001.0, "summary": ["sent 1"]}


class RecordingDisplay:
    def __init__(self):
        self.shown = []

    def update(self, image, code):
        self.shown.append((image.tobytes(), code))
        return True


class Clock:
    def __init__(self, *times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0)


def run_screens(snapshots, activities, times, read=None, selector=None):
    cfg = Config(display="png", interval=5)
    display, delays = RecordingDisplay(), []
    acts = list(activities)
    run(cfg, ScriptedCollector(*snapshots), display, FailingStateLog(), sleep=delays.append,
        wall=Clock(*times), ticks=len(snapshots), notify=lambda m: None,
        read_activity=read or (lambda: acts.pop(0)), selector=selector or ScreenSelector())
    return display.shown, delays


def clock_at(now):
    return _time.strftime("%H:%M", _time.localtime(now))


def ethernet_image(snapshot, now):
    v = evaluate(snapshot, 1000, 20)
    return render(build_rows(snapshot, v), v, clock_at(now)).tobytes()


def test_run_shows_probe_screen_fast_while_running_then_holds_then_returns():
    ok, down = ok_snapshot(), Snapshot(link=LinkResult(up=False))
    shown, delays = run_screens([ok, ok, ok, down],
                                [ACT, ACT_DONE, ACT_DONE, ACT_DONE],
                                [1000.5, 1002.0, 1010.0, 1020.0])
    assert shown[0] == (render_probe(ACT, clock_at(1000.5)).tobytes(), "PROBE:running")
    assert shown[1] == (render_probe(ACT_DONE, clock_at(1002.0)).tobytes(), "PROBE:ok")
    assert shown[2][1] == "PROBE:ok"
    assert shown[3] == (ethernet_image(down, 1020.0), "NO_LINK")   # verdict changed: Ethernet again
    assert delays == [FAST_INTERVAL, 5, 5, FAST_INTERVAL]


def test_run_without_activity_shows_ethernet():
    ok = ok_snapshot()
    shown, delays = run_screens([ok], [None], [1000.0])
    assert shown == [(ethernet_image(ok, 1000.0), "OK")]
    assert delays == [5]


def test_run_survives_activity_read_selector_and_render_failures():
    ok = ok_snapshot()

    def broken_read():
        raise OSError("gone")

    shown, delays = run_screens([ok], [], [1000.0], read=broken_read)
    assert shown == [(ethernet_image(ok, 1000.0), "OK")] and delays == [5]

    class BrokenSelector:
        def choose(self, *a):
            raise RuntimeError("bug")

        def fast(self, a):
            raise RuntimeError("bug")

    shown, delays = run_screens([ok], [ACT], [1000.0], selector=BrokenSelector())
    assert shown == [(ethernet_image(ok, 1000.0), "OK")] and delays == [5]

    odd = {k: v for k, v in ACT_DONE.items() if k != "tool"}   # render_probe raises KeyError
    shown, delays = run_screens([ok], [odd], [1001.5])
    assert shown == [(ethernet_image(ok, 1001.5), "OK")] and delays == [5]


def test_main_wires_activity_reader_and_selector(tmp_path, monkeypatch):
    cfg = Config(display="png", png_path=str(tmp_path / "s.png"), log_path=str(tmp_path / "e.log"),
                 state_path=str(tmp_path / "state.json"), activity_path=str(tmp_path / "a.json"))
    captured = {}
    monkeypatch.setattr(main_mod, "start", lambda path: (cfg, "display"))
    monkeypatch.setattr(main_mod, "run", lambda *a, **kw: captured.update(kw))
    main_mod.main(["--config", str(tmp_path / "c.toml")])
    assert isinstance(captured["selector"], ScreenSelector)
    assert captured["read_activity"]() is None
    from ethtester.activity import write_activity
    write_activity(tmp_path / "a.json", ACT)
    assert captured["read_activity"]() == ACT
