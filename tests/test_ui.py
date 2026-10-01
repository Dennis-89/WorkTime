from datetime import datetime, timedelta, timezone
from threading import Event, get_ident
from unittest.mock import Mock

import pytest

from worktime.app import WorkTimeApp
from worktime.domain import DomainError, WorkDay, WorkState
from worktime.summary import summarize_day, summarize_week
from worktime.ui import (
    EditorController, EditorInput, TimeInput, UIService, day_view, duration_text, week_view,
)

BASE = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


def utc(wall):
    return wall.replace(tzinfo=timezone.utc)


def input_at(text):
    return TimeInput("2026-10-01", text)


def test_desktop_free_viewmodels():
    empty = summarize_day(BASE.date(), WorkDay(), as_of=BASE)
    view = day_view(empty)
    assert dict(view.fields)["Netto"] == "-"
    assert dict(view.fields)["Zustand"] == "Noch nicht gestartet"
    week = week_view(summarize_week(BASE.date(), lambda _: WorkDay(), as_of=BASE))
    assert len(week.rows) == 7
    assert week.total == "00:00"
    assert duration_text(timedelta(hours=27, minutes=5)) == "27:05"


def test_valid_editor_delegates_exactly_once():
    app = Mock()
    values = EditorInput(input_at("08:00"), input_at("16:00"),
                         ((input_at("12:00"), input_at("12:30")),))
    EditorController(app, BASE.date(), resolver=utc).save(values)
    assert len(app.mock_calls) == 1
    app.correct_day.assert_called_once()
    day, events = app.correct_day.call_args.args
    assert day == BASE.date()
    assert [e.kind.value for e in events] == ["WORK_START", "PAUSE_START", "PAUSE_END", "WORK_END"]
    assert all(e.timestamp.tzinfo is not None for e in events)


@pytest.mark.parametrize("values", [
    EditorInput(input_at(""), input_at("16:00")),
    EditorInput(input_at("bad"), input_at("16:00")),
    EditorInput(input_at("08:00"), input_at("07:00")),
    EditorInput(TimeInput("bad", "08:00"), input_at("16:00")),
    EditorInput(input_at("08:00"), input_at("16:00"), ((input_at("12:00"), input_at("")),)),
    EditorInput(input_at("08:00"), input_at("16:00"),
                ((input_at("12:00"), input_at("13:00")), (input_at("12:30"), input_at("14:00")))),
])
def test_invalid_editor_never_calls_app(values):
    app = Mock()
    with pytest.raises(DomainError):
        EditorController(app, BASE.date(), resolver=utc).save(values)
    assert app.mock_calls == []


def test_paused_editor_allows_one_open_last_pause():
    app = Mock()
    EditorController(app, BASE.date(), allow_open_pause=True, resolver=utc).save(
        EditorInput(input_at("08:00"), input_at(""), ((input_at("12:00"), input_at("")),)))
    assert app.correct_day.call_args.args[1][-1].kind.value == "PAUSE_START"


class FakeRoot:
    def __init__(self, calls):
        self.calls, self.owner, self.callbacks = calls, get_ident(), []
        self.quit_requested = False
        self.record("create")

    def record(self, operation):
        assert get_ident() == self.owner
        self.calls.append((operation, get_ident()))

    def withdraw(self):
        self.record("withdraw")

    def after(self, delay, callback):
        self.record("after")
        self.callbacks.append(callback)

    def mainloop(self):
        self.record("mainloop")
        while not self.quit_requested:
            if self.callbacks:
                self.callbacks.pop(0)()
            Event().wait(0.001)

    def quit(self):
        self.record("quit")
        self.quit_requested = True

    def destroy(self):
        self.record("destroy")


def test_ui_lifecycle_dispatch_and_close_stay_on_owner_thread_without_desktop(tmp_path):
    calls, received = [], Event()
    app = WorkTimeApp(tmp_path / "ui.db", clock=lambda: BASE)
    app.start_work()
    before = app.raw_events(BASE.date())

    class Windows:
        def __init__(self, root, app):
            self.root = root
        def open_day(self):
            self.root.record("day")
        def open_week(self):
            self.root.record("week")
            received.set()
        def show_error(self, message):
            raise AssertionError(message)

    service = UIService(app, root_factory=lambda: FakeRoot(calls), windows_factory=Windows)
    try:
        service.start()
        service.start()
        service.open_day()
        service.open_week()
        assert received.wait(3)
    finally:
        service.stop()
    service.stop()
    assert not service.running
    assert len({owner for _, owner in calls}) == 1
    assert calls[0][1] != get_ident()
    operations = [operation for operation, _ in calls]
    assert operations.count("create") == operations.count("destroy") == 1
    assert operations.index("day") < operations.index("week") < operations.index("quit") < operations.index("destroy")
    assert app.raw_events(BASE.date()) == before
    assert app.status().day.state is WorkState.WORKING
    app.close()


def test_ui_start_failure_and_stop_before_start_are_safe():
    def fail():
        raise RuntimeError("Tk not available")
    service = UIService(Mock(), root_factory=fail)
    with pytest.raises(RuntimeError, match="Tk not available"):
        service.start()
    assert not service.running
    service.stop()
    closed = UIService(Mock(), root_factory=fail)
    closed.stop()
    closed.stop()
    with pytest.raises(RuntimeError, match="geschlossen"):
        closed.start()


def test_ui_command_failure_is_reported_on_owner_thread_and_worker_survives():
    calls, error_reported = [], Event()
    class Windows:
        def __init__(self, root, app):
            self.root = root
        def open_day(self):
            raise DomainError("invalid day")
        def show_error(self, message):
            assert message == "invalid day"
            self.root.record("error")
            error_reported.set()
    service = UIService(Mock(), root_factory=lambda: FakeRoot(calls), windows_factory=Windows)
    try:
        service.open_day()
        assert error_reported.wait(3)
        assert service.running
    finally:
        service.stop()
    assert not service.running


class Widget:
    def __init__(self, parent=None, **kwargs):
        self.parent, self.kwargs = parent, kwargs
        self.children, self.destroyed = [], False
        if parent is not None:
            parent.children.append(self)
    def pack(self, **kwargs):
        pass
    def title(self, text):
        self.caption = text
    def geometry(self, text):
        pass
    def configure(self, **kwargs):
        self.kwargs.update(kwargs)
    def bind(self, *args):
        pass
    def create_window(self, *args, **kwargs):
        pass
    def yview(self, *args):
        pass
    def set(self, *args):
        pass
    def heading(self, *args, **kwargs):
        pass
    def insert(self, *args, **kwargs):
        pass
    def winfo_children(self):
        return list(self.children)
    def destroy(self):
        for child in self.children[:]:
            child.destroy()
        self.destroyed = True
        if self.parent:
            self.parent.children.remove(self)


class Variable:
    def __init__(self, *, master, value=""):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


def widget_tree(widget):
    yield widget
    for child in widget.children:
        yield from widget_tree(child)


def test_window_builders_navigation_editor_and_close_without_desktop(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import worktime.ui as module
    from worktime.ui import TkWindows

    app = WorkTimeApp(tmp_path / "windows.db", clock=lambda: BASE + timedelta(hours=8))
    app.start_work()
    before = app.raw_events(BASE.date())
    root = Widget()
    windows = object.__new__(TkWindows)
    windows.root, windows.app = root, app
    windows.tk = SimpleNamespace(Toplevel=Widget, Canvas=Widget, StringVar=Variable)
    windows.ttk = SimpleNamespace(**{name: Widget for name in
                                    ("Frame", "Label", "Button", "Entry", "Scrollbar", "Treeview")})
    original_controller = module.EditorController
    monkeypatch.setattr(module, "EditorController",
                        lambda *args, **kwargs: original_controller(*args, **kwargs, resolver=utc))
    try:
        windows.open_day()
        day_window = root.children[0]
        buttons = {w.kwargs.get("text"): w for w in widget_tree(day_window) if "command" in w.kwargs}
        assert {"Vortag", "Heute", "Folgetag", "Bearbeiten"} <= buttons.keys()
        buttons["Bearbeiten"].kwargs["command"]()
        editor = next(w for w in day_window.children if hasattr(w, "caption"))
        controls = {w.kwargs.get("text"): w for w in widget_tree(editor) if "command" in w.kwargs}
        controls["Pause hinzufügen"].kwargs["command"]()
        remove = next(w for w in widget_tree(editor) if w.kwargs.get("text") == "Pause entfernen")
        remove.kwargs["command"]()
        controls["Abbrechen"].kwargs["command"]()
        assert editor.destroyed
        windows.open_week()
        week_window = root.children[-1]
        labels = {w.kwargs.get("text") for w in widget_tree(week_window)}
        assert {"Vorwoche", "Diese Woche", "Folgewoche"} <= labels
        # Default Tk close is destroy; neither window has a work-event close handler.
        day_window.destroy()
        week_window.destroy()
        assert root.children == []
        assert app.raw_events(BASE.date()) == before
        assert app.status().day.state is WorkState.WORKING
    finally:
        app.close()
