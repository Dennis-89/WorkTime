"""WP-009: explicit legacy schema, shared summaries and atomic day rollover."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import csv
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from worktime.app import WorkTimeApp
from worktime.domain import DomainError, EventType, WorkDay, WorkEvent, WorkState
from worktime.export import day_row, write_csv
from worktime.storage import SQLiteEventStore
from worktime.summary import summarize_day
from worktime.ui import EditorController, EditorInput, TimeInput, day_view
from worktime.windows_session import dispatch_session_event


BASE = datetime(2026, 10, 2, 7, tzinfo=timezone.utc)  # Friday
DAY = BASE.date()
OLD_TABLES = ('work_events', 'workday_corrections', 'workday_correction_events', 'workday_settings')
# Exact pre-WP-009 schema; deliberately independent of storage._SCHEMA.
OLD_SCHEMA = """
CREATE TABLE work_events (
    work_date TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    PRIMARY KEY (work_date, sequence)
);
CREATE TABLE workday_corrections (
    work_date TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    raw_through_sequence INTEGER NOT NULL CHECK (raw_through_sequence >= 0),
    PRIMARY KEY (work_date, revision)
);
CREATE TABLE workday_correction_events (
    work_date TEXT NOT NULL,
    revision INTEGER NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    PRIMARY KEY (work_date, revision, sequence),
    FOREIGN KEY (work_date, revision) REFERENCES workday_corrections (work_date, revision)
);
CREATE TABLE workday_settings (
    work_date TEXT PRIMARY KEY,
    flat_break_enabled INTEGER NOT NULL CHECK (flat_break_enabled IN (0, 1))
);
"""


def event(kind, minutes=0):
    return WorkEvent(kind, BASE + timedelta(minutes=minutes))


def rows(path, tables=OLD_TABLES):
    with closing(sqlite3.connect(path)) as connection:
        return {table: connection.execute(f'SELECT * FROM {table} ORDER BY 1, 2').fetchall()
                for table in tables}


def metadata(path):
    return rows(path, ('workday_lunch',))['workday_lunch']


def make_app(path, clock):
    return WorkTimeApp(path, clock=clock, time_resolver=lambda wall: wall.replace(tzinfo=timezone.utc))


@pytest.mark.parametrize('corrected,flat_enabled', [(False, True), (True, True), (True, False)])
def test_old_schema_open_read_summaries_csv_preserve_every_legacy_row(tmp_path, corrected, flat_enabled):
    path = tmp_path / 'old.db'
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(OLD_SCHEMA)
        connection.executemany('INSERT INTO work_events VALUES (?, ?, ?, ?)', [
            (DAY.isoformat(), 1, 'WORK_START', BASE.isoformat()),
            (DAY.isoformat(), 2, 'WORK_END', (BASE + timedelta(hours=9)).isoformat()),
        ])
        connection.execute('INSERT INTO workday_settings VALUES (?, ?)', (DAY.isoformat(), int(flat_enabled)))
        if corrected:
            connection.execute('INSERT INTO workday_corrections VALUES (?, 1, 2)', (DAY.isoformat(),))
            connection.executemany('INSERT INTO workday_correction_events VALUES (?, 1, ?, ?, ?)', [
                (DAY.isoformat(), 1, 'WORK_START', (BASE - timedelta(hours=1)).isoformat()),
                (DAY.isoformat(), 2, 'WORK_END', (BASE + timedelta(hours=9)).isoformat()),
            ])
        connection.commit()
    before = rows(path)
    app = make_app(path, lambda: BASE + timedelta(hours=10))
    try:
        assert rows(path) == before
        assert metadata(path) == []
        file_before_reads = path.read_bytes()
        expected_gross = 10 if corrected else 9
        expected = timedelta(hours=expected_gross, minutes=-15 if flat_enabled else 0)
        daily = app.day_summary(DAY)
        assert daily.auto_lunch_minutes is None and daily.lunch == timedelta()
        assert daily.pause == timedelta() and daily.net == expected
        weekly = app.week_summary(DAY)
        monthly = app.month_summary(DAY)
        assert weekly.days[4] == daily and weekly.total_net == expected
        assert monthly.days[1] == daily and monthly.total_net == expected
        output = tmp_path / 'old.csv'
        write_csv(output, app.date_range_summaries(DAY, DAY))
        with output.open(encoding='utf-8-sig', newline='') as stream:
            exported = list(csv.reader(stream, delimiter=';'))[1]
        assert exported == list(day_row(daily))
        assert exported[3] == '00:00'
        assert exported[5] == ('09:45' if corrected and flat_enabled else
                               '10:00' if corrected else '08:45')
        assert metadata(path) == [] and rows(path) == before
        assert path.read_bytes() == file_before_reads
        # Repeated same-day trigger cannot mark a previously finished legacy day.
        app.auto_start_if_needed()
        assert metadata(path) == [] and rows(path) == before
    finally:
        app.close()
    next_app = make_app(path, lambda: BASE + timedelta(days=3))
    try:
        next_app.auto_start_if_needed()
        assert metadata(path) == [((DAY + timedelta(days=3)).isoformat(), 30, 0)]
        after = rows(path)
        assert after['work_events'][:-1] == before['work_events']
        assert all(after[t] == before[t] for t in OLD_TABLES[1:])
    finally:
        next_app.close()


@pytest.mark.parametrize('pauses,expected', [((), 30), ((20,), 20), ((40,), 40), ((30, 10), 40)])
def test_new_days_default_or_real_pauses_and_all_outputs_agree(tmp_path, pauses, expected):
    path = tmp_path / 'new.db'
    now = BASE
    app = make_app(path, lambda: now)
    try:
        app.start_work()
        assert metadata(path) == [(DAY.isoformat(), 30, 0)]
        for index, duration in enumerate(pauses):
            now = BASE + timedelta(hours=2 + index)
            app.pause_work()
            # First PAUSE_START disables the whole default immediately.
            running = app.day_summary(DAY)
            assert running.lunch == timedelta()
            now += timedelta(minutes=duration)
            assert app.day_summary(DAY).pause == timedelta(minutes=sum(pauses[:index]) + duration)
            app.resume_work()
        now = BASE + timedelta(hours=9)
        app.finish_work()
        summary = app.day_summary(DAY)
        assert summary.gross == timedelta(hours=9)
        assert summary.pause == timedelta(minutes=expected)
        assert summary.flat_break == timedelta(minutes=15)
        assert summary.net == timedelta(minutes=540 - expected - 15)
        assert app.week_summary(DAY).days[4] == summary
        assert app.month_summary(DAY).days[1] == summary
        assert app.date_range_summaries(DAY, DAY) == (summary,)
        assert dict(day_view(summary).fields)['Pause'] == f'00:{expected:02}'
        output = tmp_path / 'new.csv'
        write_csv(output, (summary,))
        with output.open(encoding='utf-8-sig', newline='') as stream:
            assert list(csv.reader(stream, delimiter=';'))[1] == list(day_row(summary))
    finally:
        app.close()


@pytest.mark.parametrize('minutes', [0, 10, 45])
def test_existing_editor_persists_default_correction_without_changing_raw_events(tmp_path, minutes):
    path = tmp_path / 'edit.db'
    now = BASE
    app = make_app(path, lambda: now)
    app.auto_start_if_needed()
    now += timedelta(hours=9)
    app.finish_work()
    raw = app.raw_events(DAY)
    values = EditorInput(TimeInput(DAY.isoformat(), '07:00'), TimeInput(DAY.isoformat(), '16:00'),
                         auto_lunch_minutes=str(minutes))
    EditorController(app, DAY, resolver=lambda wall: wall.replace(tzinfo=timezone.utc)).save(values)
    app.close()
    reopened = make_app(path, lambda: now)
    try:
        summary = reopened.day_summary(DAY)
        assert summary.auto_lunch_minutes == minutes
        assert summary.pause == timedelta(minutes=minutes)
        assert summary.net == timedelta(minutes=525 - minutes)
        assert reopened.raw_events(DAY) == raw
        assert reopened.week_summary(DAY).total_net == summary.net
        assert reopened.month_summary(DAY).total_net == summary.net
        assert day_row(reopened.date_range_summaries(DAY, DAY)[0]) == day_row(summary)
    finally:
        reopened.close()


def test_manual_pause_stays_replacement_after_restart_and_correction_removes_intervals(tmp_path):
    path = tmp_path / 'seen.db'
    app = make_app(path, lambda: BASE)
    app.start_work()
    app.pause_work()
    app.close()
    app = make_app(path, lambda: BASE + timedelta(hours=9))
    try:
        app.correct_day(DAY, (event(EventType.WORK_START), event(EventType.WORK_END, 540)),
                        auto_lunch_minutes=45)
        assert metadata(path) == [(DAY.isoformat(), 45, 1)]
        assert app.day_summary(DAY).pause == timedelta()
        assert app.day_summary(DAY).net == timedelta(hours=8, minutes=45)
    finally:
        app.close()


@pytest.mark.parametrize('bad', [-1, True, 1.5, '20'])
def test_bad_lunch_correction_rolls_back_all_tables(tmp_path, bad):
    path = tmp_path / 'bad.db'
    app = make_app(path, lambda: BASE)
    try:
        app.start_work()
        before = rows(path, OLD_TABLES + ('workday_lunch',))
        with pytest.raises(DomainError):
            app.correct_day(DAY, (event(EventType.WORK_START),), auto_lunch_minutes=bad)
        assert rows(path, OLD_TABLES + ('workday_lunch',)) == before
    finally:
        app.close()


def test_correction_cannot_opt_legacy_day_into_lunch(tmp_path):
    path = tmp_path / 'legacy-edit.db'
    app = make_app(path, lambda: BASE)
    try:
        snapshot = (event(EventType.WORK_START), event(EventType.WORK_END, 540))
        app.correct_day(DAY, snapshot)
        before = rows(path)
        with pytest.raises(DomainError, match='Legacy'):
            app.correct_day(DAY, snapshot, auto_lunch_minutes=30)
        assert rows(path) == before and metadata(path) == []
    finally:
        app.close()


@pytest.mark.parametrize('paused', [False, True])
@pytest.mark.parametrize('gap', [1, 3, 10])
@pytest.mark.parametrize('legacy', [False, True])
def test_rollover_exact_2359_and_trigger_no_duplicates(tmp_path, paused, gap, legacy):
    path = tmp_path / 'rollover.db'
    with SQLiteEventStore(path) as store:
        store.append_event(DAY, event(EventType.WORK_START), new_day=not legacy)
        if paused:
            store.append_event(DAY, event(EventType.PAUSE_START, 300))
    trigger = (BASE + timedelta(days=gap)).replace(hour=6, minute=42, second=17)
    app = make_app(path, lambda: trigger)
    try:
        before = app.raw_events(DAY)
        # Lock and ordinary status reads must not roll over.
        dispatch_session_event(7, app.auto_start_if_needed)
        assert app.status().work_date == DAY
        assert app.raw_events(DAY) == before
        dispatch_session_event(8, app.auto_start_if_needed)
        assert app.status().work_date == trigger.date()
        assert app.raw_events(trigger.date()) == (WorkEvent(EventType.WORK_START, trigger),)
        with SQLiteEventStore(path) as store:
            old = store.load_day(DAY)
        end = BASE.replace(hour=23, minute=59)
        assert old.state is WorkState.FINISHED
        assert old.events[:len(before)] == before
        tail = (WorkEvent(EventType.PAUSE_END, end),) if paused else ()
        assert old.events == before + tail + (WorkEvent(EventType.WORK_END, end),)
        if paused:
            assert old.calculate().pause == end - (BASE + timedelta(hours=5))
        stable = rows(path, OLD_TABLES + ('workday_lunch',))
        dispatch_session_event(5, app.auto_start_if_needed)
        app.auto_start_if_needed()
        assert rows(path, OLD_TABLES + ('workday_lunch',)) == stable
        if legacy:
            assert old.auto_lunch_minutes is None
    finally:
        app.close()


def test_day_rollover_is_atomic_when_today_insert_fails(tmp_path):
    path = tmp_path / 'rollback.db'
    with SQLiteEventStore(path) as store:
        store.append_event(DAY, event(EventType.WORK_START), new_day=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("""CREATE TRIGGER fail_today BEFORE INSERT ON workday_lunch
            WHEN NEW.work_date != '2026-10-02'
            BEGIN SELECT RAISE(FAIL, 'start failure'); END""")
        connection.commit()
    before = rows(path, OLD_TABLES + ('workday_lunch',))
    app = make_app(path, lambda: BASE + timedelta(days=3))
    try:
        with pytest.raises(sqlite3.IntegrityError, match='start failure'):
            app.auto_start_if_needed()
        assert rows(path, OLD_TABLES + ('workday_lunch',)) == before
    finally:
        app.close()


def test_concurrent_connections_rollover_only_once(tmp_path):
    path = tmp_path / 'concurrent.db'
    first = make_app(path, lambda: BASE)
    first.start_work()
    first.close()
    trigger = BASE + timedelta(days=3)
    apps = [make_app(path, lambda: trigger) for _ in range(4)]
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda app: app.auto_start_if_needed(), apps))
        assert all(result.day.events == (WorkEvent(EventType.WORK_START, trigger),) for result in results)
        assert len(apps[0].raw_events(DAY)) == 2
        assert len(metadata(path)) == 2
    finally:
        for app in apps:
            app.close()


def test_short_new_day_net_is_clamped_but_legacy_math_unchanged():
    for lunch, expected in [(None, 5), (30, 0)]:
        day = WorkDay(auto_lunch_minutes=lunch)
        day.apply(event(EventType.WORK_START))
        summary = summarize_day(DAY, day, as_of=BASE + timedelta(minutes=20))
        assert summary.net == timedelta(minutes=expected)


def test_nonmonotonic_2359_close_fails_without_any_partial_writes(tmp_path):
    path = tmp_path / 'late.db'
    late = BASE.replace(hour=23, minute=59, second=30)
    app = make_app(path, lambda: late)
    app.start_work()
    app.close()
    before = rows(path, OLD_TABLES + ('workday_lunch',))
    app = make_app(path, lambda: BASE + timedelta(days=1))
    try:
        with pytest.raises(DomainError, match='precedes'):
            app.auto_start_if_needed()
        assert rows(path, OLD_TABLES + ('workday_lunch',)) == before
    finally:
        app.close()


def test_lunch_update_and_snapshot_failure_roll_back_together(tmp_path):
    path = tmp_path / 'correction-rollback.db'
    app = make_app(path, lambda: BASE)
    try:
        app.start_work()
        app.correct_day(DAY, (event(EventType.WORK_START),), auto_lunch_minutes=20)
        before = rows(path, OLD_TABLES + ('workday_lunch',))
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("""CREATE TRIGGER fail_revision BEFORE INSERT ON workday_corrections
                BEGIN SELECT RAISE(FAIL, 'revision failure'); END""")
            connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match='revision failure'):
            app.correct_day(DAY, (event(EventType.WORK_START),), auto_lunch_minutes=45)
        assert rows(path, OLD_TABLES + ('workday_lunch',)) == before
    finally:
        app.close()


def test_manual_pause_replaces_previously_corrected_default(tmp_path):
    path = tmp_path / 'corrected-manual.db'
    now = BASE
    app = make_app(path, lambda: now)
    try:
        app.start_work()
        app.correct_day(DAY, (event(EventType.WORK_START),), auto_lunch_minutes=45)
        now += timedelta(hours=2)
        app.pause_work()
        now += timedelta(minutes=10)
        summary = app.day_summary(DAY)
        assert summary.lunch == timedelta()
        assert summary.pause == timedelta(minutes=10)
        assert summary.net == timedelta(minutes=105)
    finally:
        app.close()
