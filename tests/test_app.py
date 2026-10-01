from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone, tzinfo
from unittest.mock import Mock

import pytest

import worktime.app as app_module
from worktime.app import WorkTimeApp, create_default_app
from worktime.domain import DomainError, EventType, WorkState
from worktime.storage import SQLiteEventStore


BASE = datetime(2026, 9, 30, 8, tzinfo=timezone(timedelta(hours=2)))


class Clock:
    def __init__(self, now=BASE):
        self.now = now
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.now


@pytest.fixture
def context(tmp_path):
    clock = Clock()
    path = tmp_path / "worktime.db"
    app = WorkTimeApp(path, clock=clock)
    yield app, clock, path
    app.close()


def saved(path, work_date=BASE.date()):
    with SQLiteEventStore(path) as store:
        return store.load_day(work_date)


def test_empty_database_not_started(context):
    app, clock, path = context
    status = app.status()
    assert status.work_date == clock.now.date()
    assert status.day.state is WorkState.NOT_STARTED
    assert status.day.events == ()
    assert saved(path).events == ()


def test_complete_sequence_uses_injected_clock_and_survives_restart(context):
    app, clock, path = context
    expected = []
    for minutes, method, kind, state in [
        (0, app.start_work, EventType.WORK_START, WorkState.WORKING),
        (60, app.pause_work, EventType.PAUSE_START, WorkState.PAUSED),
        (90, app.resume_work, EventType.PAUSE_END, WorkState.WORKING),
        (480, app.finish_work, EventType.WORK_END, WorkState.FINISHED),
    ]:
        clock.now = BASE + timedelta(minutes=minutes)
        calls = clock.calls
        status = method()
        assert clock.calls == calls + 1
        assert status.work_date == BASE.date()
        assert status.day.state is state
        last = status.day.events[-1]
        assert last.kind is kind
        assert last.timestamp.isoformat() == clock.now.isoformat()
        assert last.timestamp.utcoffset() == timedelta(hours=2)
        expected.append(last)
        assert saved(path).events == tuple(expected)
    before = app.status().day.calculate()
    app.close()
    reopened = WorkTimeApp(path, clock=clock)
    try:
        assert reopened.status().day.events == tuple(expected)
        assert reopened.status().day.calculate() == before
        assert before.net == timedelta(hours=7, minutes=30)
    finally:
        reopened.close()


def test_finish_during_pause_uses_domain_calculation(context):
    app, clock, path = context
    app.start_work()
    clock.now += timedelta(hours=1)
    app.pause_work()
    clock.now += timedelta(minutes=30)
    status = app.finish_work()
    assert status.day.state is WorkState.FINISHED
    times = saved(path).calculate()
    assert times.gross == timedelta(minutes=90)
    assert times.pause == timedelta(minutes=30)
    assert times.net == timedelta(hours=1)


@pytest.mark.parametrize("state", list(WorkState))
def test_close_never_records_event_and_is_idempotent(context, state):
    app, clock, path = context
    if state is not WorkState.NOT_STARTED:
        app.start_work()
    if state is WorkState.PAUSED:
        app.pause_work()
    if state is WorkState.FINISHED:
        app.finish_work()
    before = path.read_bytes()
    events = saved(path).events
    calls = clock.calls
    app.close()
    app.close()
    assert clock.calls == calls
    assert path.read_bytes() == before
    assert saved(path).events == events
    with pytest.raises(RuntimeError, match="geschlossen"):
        app.start_work()


@pytest.mark.parametrize("prefix,method", [
    ((), "pause_work"), ((), "resume_work"), ((), "finish_work"),
    (("start_work",), "start_work"), (("start_work",), "resume_work"),
    (("start_work", "pause_work"), "start_work"),
    (("start_work", "pause_work"), "pause_work"),
    *(((("start_work", "finish_work"), name)) for name in
      ("start_work", "pause_work", "resume_work", "finish_work")),
])
def test_invalid_use_cases_do_not_change_database(context, prefix, method):
    app, clock, path = context
    for name in prefix:
        getattr(app, name)()
    before = path.read_bytes()
    with pytest.raises(DomainError):
        getattr(app, method)()
    assert path.read_bytes() == before


def test_finished_next_local_day_can_start(context):
    app, clock, path = context
    app.start_work()
    app.finish_work()
    with pytest.raises(DomainError):
        app.start_work()
    clock.now = BASE.replace(hour=0, minute=15) + timedelta(days=1)
    assert clock.now.astimezone(timezone.utc).date() == BASE.date()
    assert app.status().day.state is WorkState.NOT_STARTED
    result = app.start_work()
    assert result.work_date == clock.now.date()
    assert result.day.state is WorkState.WORKING
    assert saved(path).state is WorkState.FINISHED
    with SQLiteEventStore(path) as store:
        assert store.list_work_dates() == (BASE.date(), clock.now.date())


@pytest.mark.parametrize("paused", [False, True])
def test_open_day_crosses_midnight_and_restart(context, paused):
    app, clock, path = context
    clock.now = BASE.replace(hour=23)
    app.start_work()
    if paused:
        clock.now += timedelta(minutes=30)
        app.pause_work()
    app.close()
    clock.now = BASE.replace(hour=0, minute=30) + timedelta(days=1)
    reopened = WorkTimeApp(path, clock=clock)
    try:
        state = reopened.status()
        assert state.work_date == BASE.date()
        assert state.day.state is (WorkState.PAUSED if paused else WorkState.WORKING)
        with pytest.raises(DomainError):
            reopened.start_work()
        if paused:
            result = reopened.resume_work()
        else:
            result = reopened.pause_work()
        assert result.work_date == BASE.date()
        clock.now += timedelta(minutes=30)
        final = reopened.finish_work()
        assert final.work_date == BASE.date()
        assert final.day.calculate().gross == timedelta(hours=2)
        assert final.day.calculate().pause == timedelta(minutes=60 if paused else 30)
        assert saved(path, clock.now.date()).events == ()
        assert reopened.start_work().work_date == clock.now.date()
    finally:
        reopened.close()


def test_backwards_clock_is_rejected_without_changes(context):
    app, clock, path = context
    app.start_work()
    before = path.read_bytes()
    clock.now -= timedelta(seconds=1)
    with pytest.raises(DomainError, match="precedes"):
        app.pause_work()
    assert path.read_bytes() == before


class NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


@pytest.mark.parametrize("zone", [None, NoOffset()])
def test_naive_clock_is_rejected_before_storage(context, zone):
    app, clock, path = context
    clock.now = BASE.replace(tzinfo=zone)
    before = path.read_bytes()
    with pytest.raises(DomainError, match="Zeitzone"):
        app.start_work()
    with pytest.raises(DomainError, match="Zeitzone"):
        app.status()
    assert path.read_bytes() == before


def test_default_factory_creates_data_directory_under_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "data").exists()
    app = create_default_app(clock=Clock())
    try:
        app.start_work()
        assert (tmp_path / "data" / "worktime.db").is_file()
        assert saved(tmp_path / "data" / "worktime.db").state is WorkState.WORKING
    finally:
        app.close()


def test_default_factory_accepts_explicit_base_directory(tmp_path):
    root = tmp_path / "nested" / "application"
    app = create_default_app(root, clock=Clock())
    app.close()
    assert (root / "data" / "worktime.db").is_file()


def test_local_clock_uses_aware_utc_then_local_conversion(monkeypatch):
    stamp = Mock()
    stamp.astimezone.return_value = BASE
    datetime_mock = Mock()
    datetime_mock.now.return_value = stamp
    monkeypatch.setattr(app_module, "datetime", datetime_mock)
    assert app_module.local_now() is BASE
    datetime_mock.now.assert_called_once_with(timezone.utc)
    stamp.astimezone.assert_called_once_with()


def test_callbacks_can_use_application_from_a_different_thread(context):
    app, clock, path = context
    app.start_work()
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(app.status).result().day.state is WorkState.WORKING
        assert pool.submit(app.pause_work).result().day.state is WorkState.PAUSED
    assert saved(path).events[-1].kind is EventType.PAUSE_START
