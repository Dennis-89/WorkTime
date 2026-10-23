from datetime import date, datetime, timedelta, timezone
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
    assert dict(view.fields)["Netto"] == "00:00"
    assert dict(view.fields)["Pauschalpause"] == "00:00"
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
        def open_month(self):
            self.root.record("month")
            received.set()
        def show_error(self, message):
            raise AssertionError(message)

    service = UIService(app, root_factory=lambda: FakeRoot(calls), windows_factory=Windows)
    try:
        service.start()
        service.start()
        service.open_day()
        service.open_week()
        service.open_month()
        assert received.wait(3)
    finally:
        service.stop()
    service.stop()
    assert not service.running
    assert len({owner for _, owner in calls}) == 1
    assert calls[0][1] != get_ident()
    operations = [operation for operation, _ in calls]
    assert operations.count("create") == operations.count("destroy") == 1
    assert operations.index("day") < operations.index("week") < operations.index("month") < operations.index("quit") < operations.index("destroy")
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
        self.headings, self.rows = {}, []
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
    def xview(self, *args):
        pass
    def set(self, *args):
        pass
    def heading(self, key, **kwargs):
        self.headings[key] = kwargs
    def insert(self, *args, **kwargs):
        self.rows.append(kwargs['values'])
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


def fake_windows(app):
    from types import SimpleNamespace
    from worktime.ui import TkWindows
    windows = object.__new__(TkWindows)
    windows.root, windows.app = Widget(), app
    windows.tk = SimpleNamespace(Toplevel=Widget, Canvas=Widget, StringVar=Variable, BooleanVar=Variable)
    windows.ttk = SimpleNamespace(**{name: Widget for name in
                                    ('Frame', 'Label', 'Button', 'Checkbutton', 'Entry', 'Scrollbar', 'Treeview')})
    windows.filedialog = Mock()
    return windows


@pytest.mark.parametrize('new_day', [False, True])
def test_existing_correction_dialog_lunch_field_only_for_new_days(tmp_path, new_day):
    from worktime.domain import EventType, WorkEvent
    now = BASE
    app = WorkTimeApp(tmp_path / 'lunch-editor.db', clock=lambda: now)
    try:
        if new_day:
            app.start_work()
            now += timedelta(hours=8)
            app.finish_work()
        else:
            app.correct_day(BASE.date(), (WorkEvent(EventType.WORK_START, BASE),
                WorkEvent(EventType.WORK_END, BASE + timedelta(hours=8))))
        windows = fake_windows(app)
        refreshed = Mock()
        windows._editor(app.day_summary(BASE.date()), refreshed, windows.root)
        dialog = windows.root.children[0]
        lunch_rows = [widget for widget in widget_tree(dialog)
                      if widget.kwargs.get('text') == 'Automatische Mittagspause (Minuten)']
        assert bool(lunch_rows) is new_day
        if new_day:
            variable = next(widget.kwargs['textvariable']
                            for widget in lunch_rows[0].parent.children
                            if 'textvariable' in widget.kwargs)
            assert variable.get() == '30'
            variable.set('20')
            control(dialog, 'Speichern').kwargs['command']()
            refreshed.assert_called_once_with()
            assert app.day_summary(BASE.date()).pause == timedelta(minutes=20)
            assert dialog.destroyed
    finally:
        app.close()


@pytest.mark.parametrize('text', ['', '-1', '1.5', 'abc'])
def test_invalid_lunch_editor_value_never_calls_app(text):
    app = Mock()
    values = EditorInput(input_at('08:00'), input_at('16:00'), auto_lunch_minutes=text)
    with pytest.raises(DomainError):
        EditorController(app, BASE.date(), resolver=utc).save(values)
    assert app.mock_calls == []


@pytest.mark.parametrize('stamp,title,count,back,forward', [
    (datetime(2026,10,15,tzinfo=timezone.utc),'Oktober 2026',31,'September 2026','November 2026'),
    (datetime(2027,1,15,tzinfo=timezone.utc),'Januar 2027',31,'Dezember 2026','Februar 2027'),
    (datetime(2026,12,15,tzinfo=timezone.utc),'Dezember 2026',31,'November 2026','Januar 2027'),
    (datetime(2028,2,15,tzinfo=timezone.utc),'Februar 2028',29,'Januar 2028','März 2028')])
def test_month_window_calendar_navigation_title_and_columns(tmp_path,stamp,title,count,back,forward):
    from worktime.export import CSV_HEADER
    app=WorkTimeApp(tmp_path/'month-ui.db',clock=lambda:stamp)
    windows=fake_windows(app)
    try:
        windows.open_month()
        window=windows.root.children[0]
        assert window.caption==title
        table=next(w for w in widget_tree(window) if w.kwargs.get('columns')==CSV_HEADER)
        assert tuple(table.headings)==CSV_HEADER and len(table.rows)==count
        assert table.rows[0][0]==stamp.replace(day=1).strftime('%d.%m.%Y')
        assert table.rows[-1][0]==stamp.replace(day=count).strftime('%d.%m.%Y')
        assert control(window,'Gesamt Netto: 00:00')
        assert control(window,'CSV exportieren')
        control(window,'Vormonat').kwargs['command']()
        assert window.caption==back
        control(window,'Dieser Monat').kwargs['command']()
        assert window.caption==title
        control(window,'Folgemonat').kwargs['command']()
        assert window.caption==forward
        control(window,'Vormonat').kwargs['command']()
        assert window.caption==title
        assert not window.destroyed
    finally:
        app.close()


def export_dialog(windows):
    windows.open_month()
    month=windows.root.children[0]
    control(month,'CSV exportieren').kwargs['command']()
    dialog=next(w for w in month.children if hasattr(w,'caption'))
    entries=[w.kwargs['textvariable'] for w in widget_tree(dialog) if 'textvariable' in w.kwargs]
    return month,dialog,entries


def test_month_values_and_export_defaults_follow_displayed_historical_month(tmp_path):
    from worktime.domain import WorkEvent, EventType
    from worktime.export import CSV_HEADER
    app=WorkTimeApp(tmp_path/'history-ui.db',clock=lambda:BASE+timedelta(days=60))
    app.correct_day(BASE.date(),(WorkEvent(EventType.WORK_START,BASE),WorkEvent(EventType.WORK_END,BASE+timedelta(hours=8))))
    windows=fake_windows(app)
    try:
        windows.open_month()
        month=windows.root.children[0]
        assert month.caption=='November 2026'
        control(month,'Vormonat').kwargs['command']()
        assert month.caption=='Oktober 2026'
        assert control(month,'Gesamt Netto: 07:45')
        table=next(w for w in widget_tree(month) if w.kwargs.get('columns')==CSV_HEADER)
        assert table.rows[0]==('01.10.2026','08:00','16:00','00:00','00:15','07:45','Feierabend','')
        control(month,'CSV exportieren').kwargs['command']()
        dialog=next(w for w in month.children if hasattr(w,'caption'))
        entries=[w.kwargs['textvariable'].get() for w in widget_tree(dialog) if 'textvariable' in w.kwargs]
        assert entries==['2026-10-01','2026-10-31']
    finally:
        app.close()


@pytest.mark.parametrize('start,end,label',[('bad','2026-10-31','Startdatum'),
                                          ('2026-02-30','2026-10-31','Startdatum'),
                                          ('2026-10-01','bad','Enddatum'),
                                          ('2026-10-01','2026-02-30','Enddatum'),
                                          ('2026-11-01','2026-10-31','Startdatum')])
def test_export_dialog_validation_precedes_file_dialog(tmp_path,start,end,label):
    app=WorkTimeApp(tmp_path/'validate-ui.db',clock=lambda:BASE)
    tracked=Mock(wraps=app)
    windows=fake_windows(tracked)
    windows.show_error=Mock()
    try:
        month,dialog,entries=export_dialog(windows)
        entries[0].set(start)
        entries[1].set(end)
        tracked.reset_mock()
        before=(tmp_path/'validate-ui.db').read_bytes()
        control(dialog,'Exportieren').kwargs['command']()
        assert label in windows.show_error.call_args.args[0]
        windows.filedialog.asksaveasfilename.assert_not_called()
        assert tracked.mock_calls==[] and not dialog.destroyed
        assert (tmp_path/'validate-ui.db').read_bytes()==before
    finally:
        app.close()


@pytest.mark.parametrize('cancel_save',[False,True])
def test_export_cancellation_creates_no_file_or_app_request(tmp_path,cancel_save):
    app=WorkTimeApp(tmp_path/'cancel-ui.db',clock=lambda:BASE)
    tracked=Mock(wraps=app)
    windows=fake_windows(tracked)
    windows.show_error=Mock()
    windows.filedialog.asksaveasfilename.return_value=''
    try:
        month,dialog,entries=export_dialog(windows)
        before=set(tmp_path.iterdir()),(tmp_path/'cancel-ui.db').read_bytes()
        tracked.reset_mock()
        control(dialog,'Exportieren' if cancel_save else 'Abbrechen').kwargs['command']()
        assert tracked.mock_calls==[]
        assert (set(tmp_path.iterdir()),(tmp_path/'cancel-ui.db').read_bytes())==before
        windows.show_error.assert_not_called()
        if cancel_save:
            windows.filedialog.asksaveasfilename.assert_called_once()
            assert not dialog.destroyed
        else:
            windows.filedialog.asksaveasfilename.assert_not_called()
            assert dialog.destroyed
    finally:
        app.close()


def test_export_dialog_writes_selected_file_with_selected_inclusive_range(tmp_path):
    import csv
    from worktime.export import CSV_HEADER
    from unittest.mock import call
    app=WorkTimeApp(tmp_path/'save-ui.db',clock=lambda:BASE)
    tracked=Mock(wraps=app)
    windows=fake_windows(tracked)
    output=tmp_path/'user-chosen-name.csv'
    windows.filedialog.asksaveasfilename.return_value=str(output)
    try:
        month,dialog,entries=export_dialog(windows)
        assert [v.get() for v in entries]==['2026-10-01','2026-10-31']
        entries[0].set('2026-10-31')
        entries[1].set('2026-11-01')
        tracked.reset_mock()
        before=(tmp_path/'save-ui.db').read_bytes()
        control(dialog,'Exportieren').kwargs['command']()
        assert tracked.mock_calls==[call.date_range_summaries(date(2026,10,31),date(2026,11,1))]
        options=windows.filedialog.asksaveasfilename.call_args.kwargs
        assert options['initialfile']=='worktime_2026-10-31_bis_2026-11-01.csv'
        assert options['parent'] is dialog and options['defaultextension']=='.csv'
        with output.open(encoding='utf-8-sig',newline='') as handle:
            rows=list(csv.reader(handle,delimiter=';'))
        assert rows[0]==list(CSV_HEADER) and len(rows)==3
        assert rows[1][0]=='31.10.2026' and rows[2][0]=='01.11.2026'
        assert dialog.destroyed and not month.destroyed
        assert (tmp_path/'save-ui.db').read_bytes()==before
    finally:
        app.close()


def control(window, text):
    return next(w for w in widget_tree(window) if w.kwargs.get('text') == text)


def test_checkbox_navigation_toggle_refresh_and_week_update(tmp_path):
    from worktime.domain import EventType, WorkEvent
    from unittest.mock import call
    app = WorkTimeApp(tmp_path / 'checkbox.db', clock=lambda: BASE + timedelta(hours=8))
    today = BASE.date()
    app.correct_day(today, (WorkEvent(EventType.WORK_START, BASE),
                            WorkEvent(EventType.WORK_END, BASE + timedelta(hours=8))))
    app.set_flat_break_enabled(today - timedelta(days=1), False)
    tracked = Mock(wraps=app)
    windows = fake_windows(tracked)
    try:
        windows.open_day()
        day = windows.root.children[0]
        checkbox = control(day, '15 min Pauschalpause abziehen')
        assert checkbox.kwargs['variable'].get() is True
        assert control(day, 'Pauschalpause: 00:15')
        assert control(day, 'Pause: 00:00')
        assert control(day, 'Netto: 07:45')
        windows.open_week()
        week = windows.root.children[-1]
        assert control(week, 'Gesamt Netto: 07:45')
        tracked.reset_mock()
        checkbox.kwargs['variable'].set(False)
        checkbox.kwargs['command']()
        assert tracked.mock_calls == [call.set_flat_break_enabled(today, False),
                                      call.day_summary(today), call.flat_break_enabled(today)]
        assert not day.destroyed
        assert control(day, 'Pauschalpause: 00:00')
        assert control(day, 'Netto: 08:00')
        control(week, 'Aktualisieren').kwargs['command']()
        assert control(week, 'Gesamt Netto: 08:00')
        control(day, 'Vortag').kwargs['command']()
        assert control(day, '15 min Pauschalpause abziehen').kwargs['variable'].get() is False
        control(day, 'Heute').kwargs['command']()
        assert control(day, '15 min Pauschalpause abziehen').kwargs['variable'].get() is False
        control(day, 'Folgetag').kwargs['command']()
        assert control(day, '15 min Pauschalpause abziehen').kwargs['variable'].get() is True
        assert control(day, 'Pauschalpause: 00:00')  # Enabled but not yet started.
        control(day, 'Heute').kwargs['command']()
        day.destroy()
        windows.open_day()
        reopened = windows.root.children[-1]
        assert control(reopened, '15 min Pauschalpause abziehen').kwargs['variable'].get() is False
        assert {'Vortag', 'Heute', 'Folgetag', 'Bearbeiten', 'Aktualisieren'} <= {
            w.kwargs.get('text') for w in widget_tree(reopened)}
    finally:
        app.close()


def test_failed_checkbox_save_reports_error_and_reloads_saved_value(tmp_path):
    app = WorkTimeApp(tmp_path / 'checkbox-error.db', clock=lambda: BASE)
    tracked = Mock(wraps=app)
    tracked.set_flat_break_enabled.side_effect = RuntimeError('save failed')
    windows = fake_windows(tracked)
    windows.show_error = Mock()
    try:
        windows.open_day()
        window = windows.root.children[0]
        checkbox = control(window, '15 min Pauschalpause abziehen')
        checkbox.kwargs['variable'].set(False)
        checkbox.kwargs['command']()
        windows.show_error.assert_called_once_with('save failed')
        assert control(window, '15 min Pauschalpause abziehen').kwargs['variable'].get() is True
        assert app.flat_break_enabled(BASE.date()) is True
    finally:
        app.close()


def test_ui_imports_do_not_bypass_application_layer():
    import ast
    import inspect
    import worktime.ui as module
    tree = ast.parse(inspect.getsource(module))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert 'sqlite3' not in imported
    assert 'worktime.storage' not in imported


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
    windows.tk = SimpleNamespace(Toplevel=Widget, Canvas=Widget, StringVar=Variable, BooleanVar=Variable)
    windows.ttk = SimpleNamespace(**{name: Widget for name in
                                    ("Frame", "Label", "Button", "Checkbutton", "Entry", "Scrollbar", "Treeview")})
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
