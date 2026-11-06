"""Penanda klik perekam demo: logika tanpa browser (demo/clickmark.py, LiveRunner)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo import clickmark, record  # noqa: E402


class FakePage:
    def __init__(self, log):
        self.log = log

    def wait_for_timeout(self, ms):
        self.log.append(("wait", ms))


class FakeHandle:
    def __init__(self, log, result=True):
        self.log, self.result = log, result

    def evaluate(self, js):
        self.log.append(("mark", js is clickmark.MARK_JS))
        return self.result


class FakeLocator:
    def __init__(self, log, mark_result=True):
        self.log, self.mark_result = log, mark_result
        self.page = FakePage(log)

    @property
    def first(self):
        return self

    def scroll_into_view_if_needed(self, **kw):
        self.log.append(("scroll",))

    def element_handle(self, **kw):
        return FakeHandle(self.log, self.mark_result)

    def click(self, **kw):
        self.log.append(("click",))

    def check(self, **kw):
        self.log.append(("check",))

    def select_option(self, **kw):
        self.log.append(("select",))


class FakeRunnerPage:
    def __init__(self, log, mark_result=True):
        self.loc = FakeLocator(log, mark_result)

    def locator(self, sel):
        return self.loc


def runner(log, **kw):
    return record.LiveRunner(FakeRunnerPage(log, kw.pop("mark_result", True)), "http://127.0.0.1:8090", **kw)


def test_enabled_only_for_recorded_context():
    assert clickmark.enabled(quiet=False, click_marks=True)
    assert not clickmark.enabled(quiet=True, click_marks=True)
    assert not clickmark.enabled(quiet=False, click_marks=False)


def test_marker_is_visible_before_the_click_for_about_a_second():
    assert 600 <= clickmark.BEFORE_MS <= 800
    log = []
    runner(log).click("#x")
    assert [e[0] for e in log] == ["scroll", "mark", "wait", "click", "wait"]
    assert log[2] == ("wait", clickmark.BEFORE_MS) and log[4] == ("wait", clickmark.AFTER_MS)


def test_check_and_select_are_marked_too():
    for call, name in ((lambda r: r.check("#c"), "check"), (lambda r: r.select("#s", value="v"), "select")):
        log = []
        call(runner(log))
        assert ("mark", True) in log and log.index(("mark", True)) < log.index((name,))


def test_quiet_and_no_click_marks_never_touch_the_page():
    for kw in ({"quiet": True}, {"click_marks": False}):
        log = []
        runner(log, **kw).click("#x")
        assert log == [("click",)]


def test_marker_failure_does_not_break_the_click():
    log = []
    runner(log, mark_result=False).click("#x")
    assert ("click",) in log and ("wait", clickmark.BEFORE_MS) not in log


def test_cli_flag():
    assert record.parse_args(["--scene", "ch1"]).no_click_marks is False
    assert record.parse_args(["--scene", "ch1", "--no-click-marks"]).no_click_marks is True


def test_marker_js_only_runs_in_recorded_context_and_follows_the_element():
    js = clickmark.MARK_JS
    assert "window.__setCaption" in js and "requestAnimationFrame" in js and "scrollIntoView" in js
