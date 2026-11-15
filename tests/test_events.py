"""Which OctoPrint events turn into which notifications."""

import pytest
from octoprint.events import Events

from octoprint_printpocket import PrintPocketPlugin, _display_name, _fmt_duration


class FakeSettings:
    def __init__(self, **values):
        self.values = {"progress_step": 25, "printer_name": "Ender 3", **values}

    def get(self, path):
        return self.values.get(path[0])

    def get_int(self, path):
        v = self.values.get(path[0])
        return None if v is None else int(v)

    def global_get(self, path):
        return None


class RecordingSender:
    def __init__(self):
        self.sent = []

    def send(self, event, payload, only_endpoint=None, background=True):
        self.sent.append((event, payload))
        return 1


@pytest.fixture
def plugin():
    p = PrintPocketPlugin()
    p._settings = FakeSettings()
    p._sender = RecordingSender()
    p._keys = object()
    return p


def events(p):
    return [(e, pl["title"], pl["body"]) for e, pl in p._sender.sent]


def test_print_lifecycle(plugin):
    plugin.on_event(Events.PRINT_STARTED, {"name": "benchy.gcode", "path": "benchy.gcode"})
    plugin.on_event(Events.PRINT_PAUSED, {"name": "benchy.gcode"})
    plugin.on_event(Events.PRINT_DONE, {"name": "benchy.gcode", "time": 3725.2})
    assert events(plugin) == [
        ("started", "Ender 3: printing", "Started benchy"),
        ("paused", "Ender 3: paused", "benchy is paused"),
        ("done", "Ender 3: done", "benchy finished in 1h 02m"),
    ]


def test_cancel_sends_one_notification_not_two(plugin):
    # OctoPrint fires PrintFailed(reason=cancelled) and PrintCancelled
    plugin.on_event(Events.PRINT_FAILED, {"name": "a.gcode", "reason": "cancelled"})
    plugin.on_event(Events.PRINT_CANCELLED, {"name": "a.gcode"})
    assert [e for e, *_ in events(plugin)] == ["cancelled"]


def test_real_failure_and_errors(plugin):
    plugin.on_event(Events.PRINT_FAILED, {"name": "a.gcode", "reason": "error"})
    plugin.on_event(Events.ERROR, {"error": "Thermal Runaway"})
    plugin.on_event(Events.FILAMENT_CHANGE, {})
    assert [(e, b) for e, _, b in events(plugin)] == [
        ("failed", "a failed"),
        ("error", "Thermal Runaway"),
        ("filament", "The printer is waiting for a filament change"),
    ]


def test_progress_steps(plugin):
    plugin.on_event(Events.PRINT_STARTED, {"name": "x.gcode"})
    plugin._sender.sent.clear()
    for pct in range(0, 101):
        plugin.on_print_progress("local", "dir/x.gcode", pct)
    assert [b for e, _, b in events(plugin)] == [
        "dir/x is 25% done",
        "dir/x is 50% done",
        "dir/x is 75% done",
    ]


def test_progress_off(plugin):
    plugin._settings.values["progress_step"] = 0
    plugin.on_event(Events.PRINT_STARTED, {"name": "x.gcode"})
    plugin._sender.sent.clear()
    for pct in range(0, 101):
        plugin.on_print_progress("local", "x.gcode", pct)
    assert plugin._sender.sent == []


def test_irrelevant_events_are_quiet(plugin):
    plugin.on_event(Events.CONNECTED, {})
    plugin.on_event(Events.UPLOAD, {"name": "x.gcode"})
    assert plugin._sender.sent == []


def test_formatting():
    assert _fmt_duration(59) == "59s"
    assert _fmt_duration(61) == "1m"
    assert _fmt_duration(3600 * 5 + 60 * 7) == "5h 07m"
    assert _display_name({"name": "Benchy.GCODE"}) == "Benchy"
    assert _display_name({"path": "a/b.bgcode"}) == "a/b"
    assert _display_name(None) == "print"
