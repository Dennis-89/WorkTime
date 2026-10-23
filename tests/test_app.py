from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone, tzinfo
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


def test_default_factory_creates_data_directory_under_localappdata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert not (tmp_path / "data").exists()
    app = create_default_app(clock=Clock())
    try:
        app.start_work()
        path = tmp_path / "local" / "WorkTime" / "data" / "worktime.db"
        assert path.is_file()
        assert not (tmp_path / "data").exists()
        assert saved(path).state is WorkState.WORKING
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


def test_auto_start_empty_day_exactly_one_aware_event_and_one_clock_read(context):
    app, clock, path = context
    result = app.auto_start_if_needed()
    assert clock.calls == 1
    assert result.work_date == BASE.date()
    assert result.day.state is WorkState.WORKING
    assert len(result.day.events) == 1
    assert result.day.events[0].kind is EventType.WORK_START
    assert result.day.events[0].timestamp.isoformat() == BASE.isoformat()
    before = path.read_bytes()
    for expected_calls in range(2, 6):
        assert app.auto_start_if_needed().day.events == result.day.events
        assert clock.calls == expected_calls
    assert path.read_bytes() == before


@pytest.mark.parametrize("prefix,state", [
    (("start_work",), WorkState.WORKING),
    (("start_work", "pause_work"), WorkState.PAUSED),
    (("start_work", "finish_work"), WorkState.FINISHED),
])
def test_auto_start_preserves_started_same_day(context, prefix, state):
    app, clock, path = context
    for method in prefix:
        getattr(app, method)()
    events = saved(path).events
    before = path.read_bytes()
    calls = clock.calls
    clock.now += timedelta(hours=1)
    result = app.auto_start_if_needed()
    assert clock.calls == calls + 1
    assert result.day.state is state
    assert result.day.events == events
    assert path.read_bytes() == before


def test_auto_start_finished_previous_day_uses_local_not_utc_date(context):
    app, clock, path = context
    app.start_work()
    app.finish_work()
    old_events = saved(path).events
    clock.now = BASE.replace(hour=0, minute=15) + timedelta(days=1)
    assert clock.now.astimezone(timezone.utc).date() == BASE.date()
    calls = clock.calls
    result = app.auto_start_if_needed()
    assert clock.calls == calls + 1
    assert result.work_date == clock.now.date()
    assert result.day.state is WorkState.WORKING
    assert len(result.day.events) == 1
    assert result.day.events[0].timestamp.isoformat() == clock.now.isoformat()
    assert saved(path).events == old_events
    assert len(saved(path, clock.now.date()).events) == 1


@pytest.mark.parametrize("paused", [False, True])
def test_auto_start_closes_open_previous_day_after_restart(context, paused):
    app, clock, path = context
    app.start_work()
    if paused:
        app.pause_work()
    events = saved(path).events
    app.close()
    clock.now += timedelta(days=1)
    reopened = WorkTimeApp(path, clock=clock)
    try:
        result = reopened.auto_start_if_needed()
        assert result.work_date == clock.now.date()
        assert result.day.state is WorkState.WORKING
        assert len(result.day.events) == 1
        old = saved(path)
        assert old.events[:len(events)] == events
        assert old.state is WorkState.FINISHED
        assert old.events[-1].timestamp.strftime('%H:%M') == '23:59'
    finally:
        reopened.close()


def test_concurrent_auto_starts_on_one_app_write_once(context):
    app, clock, path = context
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: app.auto_start_if_needed(), range(12)))
    assert clock.calls == 12
    assert all(result.day.state is WorkState.WORKING for result in results)
    assert len(saved(path).events) == 1


def test_auto_start_does_not_reread_clock_across_midnight(tmp_path):
    times = [BASE.replace(hour=23, minute=59), BASE + timedelta(days=1)]
    clock = Mock(side_effect=times)
    app = WorkTimeApp(tmp_path / "clock.db", clock=clock)
    try:
        result = app.auto_start_if_needed()
        clock.assert_called_once_with()
        assert result.work_date == times[0].date()
        assert result.day.events[0].timestamp == times[0]
    finally:
        app.close()


@pytest.mark.parametrize("zone", [None, NoOffset()])
def test_auto_start_rejects_naive_clock_without_write(context, zone):
    app, clock, path = context
    before = path.read_bytes()
    clock.now = BASE.replace(tzinfo=zone)
    with pytest.raises(DomainError):
        app.auto_start_if_needed()
    assert clock.calls == 1
    assert path.read_bytes() == before


# WP-005: application access remains synchronized and uses one explicit clock.
def test_correction_and_raw_audit_app_api(context):
    from worktime.domain import WorkEvent
    app, clock, path = context
    original = app.start_work().day.events
    corrected = (WorkEvent(EventType.WORK_START, BASE - timedelta(hours=1)),)
    calls = clock.calls
    result = app.correct_day(BASE.date(), iter(corrected))
    assert result.work_date == BASE.date()
    assert result.day.events == corrected
    assert app.raw_events(BASE.date()) == original
    assert clock.calls == calls
    clock.now += timedelta(hours=8)
    assert app.finish_work().day.calculate().net == timedelta(hours=9)


def test_day_and_week_summaries_read_clock_once(context):
    app, clock, path = context
    app.start_work()
    clock.now += timedelta(hours=2)
    calls = clock.calls
    summary = app.day_summary(BASE.date())
    assert clock.calls == calls + 1
    assert summary.net == timedelta(hours=1, minutes=15)
    weekly = app.week_summary(BASE.date())
    assert clock.calls == calls + 2
    assert weekly.total_net == timedelta(hours=1, minutes=15)
    clock.now += timedelta(days=1)
    assert app.day_summary(BASE.date()).incomplete
    assert app.week_summary(BASE.date()).total_net == timedelta()


@pytest.mark.parametrize("paused,next_method", [(False, "finish_work"), (True, "resume_work"), (True, "finish_work")])
def test_correction_only_followup_via_app(context, paused, next_method):
    from worktime.domain import WorkEvent
    app, clock, path = context
    snapshot = [WorkEvent(EventType.WORK_START, BASE)]
    if paused:
        snapshot.append(WorkEvent(EventType.PAUSE_START, BASE + timedelta(hours=1)))
    app.correct_day(BASE.date(), snapshot)
    clock.now += timedelta(hours=8)
    result = getattr(app, next_method)()
    assert result.day.events[:-1] == tuple(snapshot)
    assert app.raw_events(BASE.date()) == (result.day.events[-1],)


def test_new_app_use_cases_reject_closed_app(context):
    app, clock, path = context
    app.close()
    for method, args in ((app.today, ()), (app.day_summary, (BASE.date(),)),
                         (app.week_summary, (BASE.date(),)), (app.raw_events, (BASE.date(),)),
                         (app.correct_day, (BASE.date(), ())),
                         (app.flat_break_enabled, (BASE.date(),)),
                         (app.set_flat_break_enabled, (BASE.date(), False))):
        with pytest.raises(RuntimeError, match="geschlossen"):
            method(*args)


def test_daily_options_survive_app_reopen_and_apply_per_day(context):
    app, clock, path = context
    for delta in (0, 1):
        clock.now = BASE + timedelta(days=delta)
        app.start_work()
        clock.now += timedelta(hours=8)
        app.finish_work()
    first, second = BASE.date(), clock.now.date()
    assert app.flat_break_enabled(first) is True
    assert app.day_summary(first).net == timedelta(hours=7, minutes=15)
    app.set_flat_break_enabled(first, False)
    assert app.day_summary(first).net == timedelta(hours=7, minutes=30)
    assert app.day_summary(second).net == timedelta(hours=7, minutes=15)
    assert app.week_summary(first).total_net == timedelta(hours=14, minutes=45)
    app.close()
    with_app = WorkTimeApp(path, clock=clock)
    try:
        assert with_app.flat_break_enabled(first) is False
        assert with_app.flat_break_enabled(second) is True
        assert with_app.week_summary(first).total_net == timedelta(hours=14, minutes=45)
        with_app.set_flat_break_enabled(first, True)
        assert with_app.day_summary(first).flat_break == timedelta(minutes=15)
    finally:
        with_app.close()


def test_options_and_corrections_do_not_change_each_other(context):
    from worktime.domain import WorkEvent
    app, clock, path = context
    app.start_work()
    clock.now += timedelta(hours=8)
    app.finish_work()
    original = app.raw_events(BASE.date())
    app.set_flat_break_enabled(BASE.date(), False)
    app.correct_day(BASE.date(), (WorkEvent(EventType.WORK_START, BASE - timedelta(hours=1)),
                                  WorkEvent(EventType.WORK_END, clock.now)))
    assert app.flat_break_enabled(BASE.date()) is False
    with SQLiteEventStore(path) as store:
        corrected = store.load_correction_revision(BASE.date(), 1).events
    app.set_flat_break_enabled(BASE.date(), True)
    assert app.raw_events(BASE.date()) == original
    with SQLiteEventStore(path) as store:
        assert store.list_correction_revisions(BASE.date()) == (1,)
        assert store.load_correction_revision(BASE.date(), 1).events == corrected
    assert app.day_summary(BASE.date()).net == timedelta(hours=8, minutes=15)


@pytest.mark.parametrize("historical", [False, True])
def test_month_summary_reads_clock_once_and_does_not_mutate_data(context, historical):
    import hashlib
    app, clock, path = context
    app.start_work()
    clock.now += timedelta(hours=2)
    if historical:
        app.finish_work()
        clock.now += timedelta(days=65)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    calls = clock.calls
    month = app.month_summary(BASE.date())
    assert clock.calls == calls + 1
    assert month.first_day == date(2026, 9, 1) and month.last_day == date(2026, 9, 30)
    assert month.total_net == timedelta(hours=1, minutes=15)
    assert month.incomplete_dates == ()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert month.days[-1] == app.day_summary(BASE.date())


@pytest.mark.parametrize("start,end", [(date(2026, 10, 1), date(2026, 10, 1)),
                                       (date(2026, 10, 15), date(2026, 11, 10)),
                                       (date(2026, 12, 20), date(2027, 1, 10)),
                                       (date(2028, 2, 1), date(2028, 2, 29))])
def test_inclusive_range_clock_once_and_no_database_mutation(context, start, end):
    app, clock, path = context
    before = path.read_bytes()
    calls = clock.calls
    days = app.date_range_summaries(start, end)
    assert clock.calls == calls + 1
    assert tuple(day.work_date for day in days) == tuple(start + timedelta(days=i) for i in range((end-start).days + 1))
    assert path.read_bytes() == before


def test_range_uses_day_options_and_one_evaluation_instant(context):
    app, clock, path = context
    app.start_work()
    clock.now += timedelta(hours=2)
    days = app.date_range_summaries(BASE.date(), BASE.date())
    assert days[0].net == timedelta(hours=1, minutes=15) and not days[0].incomplete
    app.set_flat_break_enabled(BASE.date(), False)
    assert app.date_range_summaries(BASE.date(), BASE.date())[0].net == timedelta(hours=1, minutes=30)
    clock.now += timedelta(days=1)
    assert app.date_range_summaries(BASE.date(), BASE.date())[0].net is None


def test_invalid_range_rejected_without_mutation_or_clock_read(context):
    app, clock, path = context
    before, calls = path.read_bytes(), clock.calls
    with pytest.raises(DomainError, match='Startdatum'):
        app.date_range_summaries(BASE.date()+timedelta(days=1), BASE.date())
    assert path.read_bytes() == before and clock.calls == calls
    for invalid in (None, BASE, '2026-10-01'):
        with pytest.raises(TypeError):
            app.date_range_summaries(invalid, BASE.date())


def test_month_and_range_reject_closed_application(context):
    app, clock, path = context
    app.close()
    for method, args in ((app.month_summary, (BASE.date(),)),
                         (app.date_range_summaries, (BASE.date(), BASE.date()))):
        with pytest.raises(RuntimeError, match='geschlossen'):
            method(*args)
