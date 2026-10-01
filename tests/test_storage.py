from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import sqlite3
from threading import Barrier

import pytest

from worktime.domain import DomainError, EventType, WorkDay, WorkEvent, WorkState
from worktime.storage import SQLiteEventStore


WORK_DATE = date(2026, 10, 1)
BASE = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


def event(kind, minutes=0):
    return WorkEvent(kind, BASE + timedelta(minutes=minutes))


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "worktime.sqlite3"


@pytest.fixture
def store(db_path):
    with SQLiteEventStore(db_path) as database:
        yield database


def rows(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute(
            "SELECT work_date, sequence, event_type, timestamp "
            "FROM work_events ORDER BY work_date, sequence"
        ).fetchall()


def test_new_database_initializes_schema_and_empty_day(db_path):
    assert not db_path.exists()
    with SQLiteEventStore(db_path) as store:
        assert db_path.is_file()
        day = store.load_day(WORK_DATE)
        assert isinstance(day, WorkDay)
        assert day.state is WorkState.NOT_STARTED
        assert day.events == ()
        assert store.list_work_dates() == ()
    assert rows(db_path) == []


def test_start_is_committed_and_returned_as_workday(store, db_path):
    start = event(EventType.WORK_START)
    result = store.append_event(WORK_DATE, start)
    assert isinstance(result, WorkDay)
    assert result.state is WorkState.WORKING
    assert result.events == (start,)
    assert store.load_day(WORK_DATE).events == (start,)
    assert rows(db_path) == [(WORK_DATE.isoformat(), 1, "WORK_START", BASE.isoformat())]


def test_start_end_survive_explicit_close_and_reopen(db_path):
    start, end = event(EventType.WORK_START), event(EventType.WORK_END, 480)
    store = SQLiteEventStore(str(db_path))
    try:
        store.append_event(WORK_DATE, start)
        original = store.append_event(WORK_DATE, end)
    finally:
        store.close()
    with SQLiteEventStore(db_path) as reopened:
        restored = reopened.load_day(WORK_DATE)
        assert restored.state is WorkState.FINISHED
        assert restored.events == (start, end)
        assert restored.calculate() == original.calculate()
        assert restored.calculate().net == timedelta(hours=8)


@pytest.mark.parametrize("terminal", ["working", "paused", "finished", "finished_paused"])
def test_multiple_pauses_and_durations_roundtrip(db_path, terminal):
    events = [
        event(EventType.WORK_START),
        event(EventType.PAUSE_START, 60),
        event(EventType.PAUSE_END, 75),
        event(EventType.PAUSE_START, 180),
        event(EventType.PAUSE_END, 210),
    ]
    if terminal in ("paused", "finished_paused"):
        events.append(event(EventType.PAUSE_START, 300))
    if terminal.startswith("finished"):
        events.append(event(EventType.WORK_END, 480))
    original = WorkDay()
    with SQLiteEventStore(db_path) as store:
        for item in events:
            original.apply(item)
            returned = store.append_event(WORK_DATE, item)
            assert returned.events == original.events
    with SQLiteEventStore(db_path) as reopened:
        restored = reopened.load_day(WORK_DATE)
        assert restored.events == tuple(events)
        assert restored.state is original.state
        as_of = None if terminal.startswith("finished") else BASE + timedelta(hours=8)
        times = restored.calculate(as_of)
        assert times == original.calculate(as_of)
        assert times.gross == timedelta(hours=8)
        assert times.pause == timedelta(minutes=225 if "paused" in terminal else 45)
        assert times.net == times.gross - times.pause
    assert [row[1] for row in rows(db_path)] == list(range(1, len(events) + 1))


def test_multiple_dates_are_sorted_separate_and_survive_reopen(db_path):
    days = (date(2026, 12, 31), date(2025, 1, 1), WORK_DATE)
    expected = {}
    with SQLiteEventStore(db_path) as store:
        for index, work_date in enumerate(days):
            # Logical dates deliberately differ from the timestamp's date.
            start = event(EventType.WORK_START, index * 10)
            end = event(EventType.WORK_END, 120 + index * 10)
            store.append_event(work_date, start)
            store.append_event(work_date, end)
            expected[work_date] = (start, end)
        assert store.list_work_dates() == tuple(sorted(days))
    with SQLiteEventStore(db_path) as reopened:
        assert reopened.list_work_dates() == tuple(sorted(days))
        for work_date, events in expected.items():
            assert reopened.load_day(work_date).events == events
        assert reopened.load_day(date(2026, 1, 2)).events == ()
        assert reopened.list_work_dates() == tuple(sorted(days))


def test_logical_day_can_end_after_midnight(db_path):
    start = WorkEvent(EventType.WORK_START, BASE.replace(hour=23))
    end = WorkEvent(EventType.WORK_END, BASE.replace(hour=2) + timedelta(days=1))
    with SQLiteEventStore(db_path) as store:
        store.append_event(WORK_DATE, start)
        store.append_event(WORK_DATE, end)
    with SQLiteEventStore(db_path) as reopened:
        assert reopened.list_work_dates() == (WORK_DATE,)
        assert reopened.load_day(WORK_DATE).events == (start, end)
        assert reopened.load_day(WORK_DATE).calculate().net == timedelta(hours=3)
        assert reopened.load_day(WORK_DATE + timedelta(days=1)).state is WorkState.NOT_STARTED
    assert {row[0] for row in rows(db_path)} == {WORK_DATE.isoformat()}


@pytest.mark.parametrize("offset", [timedelta(), timedelta(hours=5, minutes=45), -timedelta(hours=3, minutes=30)])
def test_offsets_and_microseconds_are_preserved(db_path, offset):
    stamp = BASE.replace(tzinfo=timezone(offset), microsecond=123456)
    start = WorkEvent(EventType.WORK_START, stamp)
    with SQLiteEventStore(db_path) as store:
        store.append_event(WORK_DATE, start)
    with SQLiteEventStore(db_path) as reopened:
        restored = reopened.load_day(WORK_DATE).events[0].timestamp
        assert restored.tzinfo is not None
        assert restored.utcoffset() == offset
        assert restored.isoformat() == stamp.isoformat()
    assert rows(db_path)[0][3] == stamp.isoformat()


def test_mixed_offsets_use_sequence_instead_of_local_clock_order(db_path):
    events = (
        WorkEvent(EventType.WORK_START, datetime.fromisoformat("2026-10-25T02:50:00+02:00")),
        WorkEvent(EventType.PAUSE_START, datetime.fromisoformat("2026-10-25T02:10:00+01:00")),
        WorkEvent(EventType.PAUSE_END, datetime.fromisoformat("2026-10-25T01:30:00+00:00")),
        WorkEvent(EventType.WORK_END, datetime.fromisoformat("2026-10-25T03:00:00+01:00")),
    )
    with SQLiteEventStore(db_path) as store:
        for item in events:
            store.append_event(WORK_DATE, item)
    with SQLiteEventStore(db_path) as reopened:
        day = reopened.load_day(WORK_DATE)
        assert day.events == events
        assert [e.timestamp.isoformat() for e in day.events] == [e.timestamp.isoformat() for e in events]
        assert day.calculate().gross == timedelta(minutes=70)
        assert day.calculate().pause == timedelta(minutes=20)
        assert day.calculate().net == timedelta(minutes=50)


def test_identical_timestamps_keep_sequence(db_path):
    events = tuple(event(kind) for kind in (
        EventType.WORK_START, EventType.PAUSE_START, EventType.PAUSE_END,
        EventType.PAUSE_START, EventType.WORK_END,
    ))
    with SQLiteEventStore(db_path) as store:
        for item in events:
            store.append_event(WORK_DATE, item)
    with SQLiteEventStore(db_path) as reopened:
        assert reopened.load_day(WORK_DATE).events == events
        assert reopened.load_day(WORK_DATE).calculate().gross == timedelta()


INVALID_TRANSITIONS = [
    ((), EventType.PAUSE_START),
    ((), EventType.PAUSE_END),
    ((), EventType.WORK_END),
    ((EventType.WORK_START,), EventType.WORK_START),
    ((EventType.WORK_START,), EventType.PAUSE_END),
    ((EventType.WORK_START, EventType.PAUSE_START), EventType.WORK_START),
    ((EventType.WORK_START, EventType.PAUSE_START), EventType.PAUSE_START),
] + [((EventType.WORK_START, EventType.WORK_END), kind) for kind in EventType]


@pytest.mark.parametrize("prefix,rejected_kind", INVALID_TRANSITIONS)
def test_invalid_transition_leaves_database_unchanged(store, db_path, prefix, rejected_kind):
    for index, kind in enumerate(prefix):
        store.append_event(WORK_DATE, event(kind, index * 60))
    before = rows(db_path)
    before_bytes = db_path.read_bytes()
    with pytest.raises(DomainError):
        store.append_event(WORK_DATE, event(rejected_kind, 180))
    assert rows(db_path) == before
    assert len(store.load_day(WORK_DATE).events) == len(prefix)
    assert db_path.read_bytes() == before_bytes


@pytest.mark.parametrize("prefix,next_kind", [
    ((EventType.WORK_START,), EventType.PAUSE_START),
    ((EventType.WORK_START,), EventType.WORK_END),
    ((EventType.WORK_START, EventType.PAUSE_START), EventType.PAUSE_END),
    ((EventType.WORK_START, EventType.PAUSE_START), EventType.WORK_END),
])
def test_backwards_time_is_not_saved_and_next_valid_append_works(store, db_path, prefix, next_kind):
    for index, kind in enumerate(prefix):
        store.append_event(WORK_DATE, event(kind, index * 60))
    before = rows(db_path)
    before_bytes = db_path.read_bytes()
    last = store.load_day(WORK_DATE).events[-1].timestamp
    with pytest.raises(DomainError, match="precedes"):
        store.append_event(WORK_DATE, WorkEvent(next_kind, last - timedelta(microseconds=1)))
    assert rows(db_path) == before
    assert db_path.read_bytes() == before_bytes
    accepted = WorkEvent(next_kind, last)
    day = store.append_event(WORK_DATE, accepted)
    assert len(day.events) == len(prefix) + 1
    assert [row[1] for row in rows(db_path)] == list(range(1, len(prefix) + 2))


def test_finished_cannot_restart_after_reopen(db_path):
    with SQLiteEventStore(db_path) as store:
        store.append_event(WORK_DATE, event(EventType.WORK_START))
        store.append_event(WORK_DATE, event(EventType.WORK_END, 480))
    before = rows(db_path)
    with SQLiteEventStore(db_path) as reopened:
        with pytest.raises(DomainError):
            reopened.append_event(WORK_DATE, event(EventType.WORK_START, 600))
        assert reopened.load_day(WORK_DATE).state is WorkState.FINISHED
    assert rows(db_path) == before


def test_write_failure_after_insert_rolls_back_and_does_not_consume_sequence(store, db_path):
    store.append_event(WORK_DATE, event(EventType.WORK_START))
    other_date = date(2026, 10, 2)
    store.append_event(other_date, event(EventType.WORK_START))
    with closing(sqlite3.connect(db_path)) as connection:
        # RAISE(FAIL) keeps earlier changes of the statement unless our
        # enclosing transaction rolls them back. AFTER INSERT exercises this.
        connection.execute("""
            CREATE TRIGGER fail_pause AFTER INSERT ON work_events
            WHEN NEW.event_type = 'PAUSE_START'
            BEGIN SELECT RAISE(FAIL, 'forced write failure'); END
        """)
        connection.commit()
    before = rows(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="forced write failure"):
        store.append_event(WORK_DATE, event(EventType.PAUSE_START, 60))
    assert rows(db_path) == before
    assert store.load_day(WORK_DATE).state is WorkState.WORKING
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("DROP TRIGGER fail_pause")
        connection.commit()
    restored = store.append_event(WORK_DATE, event(EventType.PAUSE_START, 60))
    assert restored.state is WorkState.PAUSED
    assert [row[1] for row in rows(db_path) if row[0] == WORK_DATE.isoformat()] == [1, 2]
    with SQLiteEventStore(db_path) as reopened:
        assert reopened.load_day(WORK_DATE).events == restored.events
        assert len(reopened.load_day(other_date).events) == 1


def test_two_stores_reload_committed_history(db_path):
    with SQLiteEventStore(db_path) as first, SQLiteEventStore(db_path) as second:
        first.append_event(WORK_DATE, event(EventType.WORK_START))
        second.append_event(WORK_DATE, event(EventType.PAUSE_START, 60))
        with pytest.raises(DomainError):
            first.append_event(WORK_DATE, event(EventType.PAUSE_START, 70))
        first.append_event(WORK_DATE, event(EventType.PAUSE_END, 80))
        assert first.load_day(WORK_DATE).events == second.load_day(WORK_DATE).events
    assert len(rows(db_path)) == 3


def test_concurrent_appends_validate_under_write_lock(db_path):
    with SQLiteEventStore(db_path) as store:
        store.append_event(WORK_DATE, event(EventType.WORK_START))
    ready = Barrier(2, timeout=10)

    def append_pause():
        with SQLiteEventStore(db_path) as store:
            ready.wait()
            try:
                store.append_event(WORK_DATE, event(EventType.PAUSE_START, 60))
            except DomainError:
                return "rejected"
            return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: append_pause(), range(2)))
    assert sorted(outcomes) == ["rejected", "saved"]
    assert [row[1] for row in rows(db_path)] == [1, 2]
    with SQLiteEventStore(db_path) as reopened:
        assert reopened.load_day(WORK_DATE).state is WorkState.PAUSED


def test_load_revalidates_events_with_domain(store, db_path):
    store.append_event(WORK_DATE, event(EventType.WORK_START))
    # Simulate external corruption: reconstruction must not bypass WorkDay.apply.
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            "INSERT INTO work_events VALUES (?, ?, ?, ?)",
            (WORK_DATE.isoformat(), 2, "WORK_START", BASE.isoformat()),
        )
        connection.commit()
    before = rows(db_path)
    with pytest.raises(DomainError):
        store.load_day(WORK_DATE)
    with pytest.raises(DomainError):
        store.append_event(WORK_DATE, event(EventType.WORK_END, 480))
    assert rows(db_path) == before


def test_returned_workdays_do_not_mutate_stored_history(store):
    appended = store.append_event(WORK_DATE, event(EventType.WORK_START))
    loaded = store.load_day(WORK_DATE)
    assert loaded is not appended
    appended.apply(event(EventType.PAUSE_START, 60))
    loaded.apply(event(EventType.WORK_END, 480))
    assert store.load_day(WORK_DATE).events == (event(EventType.WORK_START),)


@pytest.mark.parametrize("invalid_date", [BASE, "2026-10-01", None])
def test_logical_date_must_be_explicit_date(store, db_path, invalid_date):
    with pytest.raises(TypeError, match="explicit date"):
        store.append_event(invalid_date, event(EventType.WORK_START))
    with pytest.raises(TypeError, match="explicit date"):
        store.load_day(invalid_date)
    assert rows(db_path) == []


def test_context_manager_closes_connection(db_path):
    with SQLiteEventStore(db_path) as store:
        store.append_event(WORK_DATE, event(EventType.WORK_START))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.load_day(WORK_DATE)
    store.close()


def test_context_exception_closes_without_undoing_prior_appends(db_path):
    with pytest.raises(RuntimeError, match="caller failure"):
        with SQLiteEventStore(db_path) as store:
            store.append_event(WORK_DATE, event(EventType.WORK_START))
            raise RuntimeError("caller failure")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.list_work_dates()
    with SQLiteEventStore(db_path) as reopened:
        assert reopened.load_day(WORK_DATE).events == (event(EventType.WORK_START),)


# WP-005 correction regression tests.
def correction_rows(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        return (
            connection.execute("SELECT * FROM workday_corrections ORDER BY work_date, revision").fetchall(),
            connection.execute("SELECT * FROM workday_correction_events ORDER BY work_date, revision, sequence").fetchall(),
        )


def test_old_schema_opens_without_raw_migration_loss(db_path):
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE work_events (work_date TEXT, sequence INTEGER, "
                           "event_type TEXT, timestamp TEXT, PRIMARY KEY(work_date, sequence))")
        connection.execute("INSERT INTO work_events VALUES (?, 1, 'WORK_START', ?)",
                           (WORK_DATE.isoformat(), BASE.isoformat()))
        connection.commit()
    before = rows(db_path)
    with SQLiteEventStore(db_path) as store:
        assert store.load_raw_events(WORK_DATE) == (event(EventType.WORK_START),)
        assert store.load_day(WORK_DATE).events == store.load_raw_events(WORK_DATE)
        assert store.list_correction_revisions(WORK_DATE) == ()
    assert rows(db_path) == before


def test_revisions_preserve_raw_and_earlier_snapshots(store, db_path):
    original = (event(EventType.WORK_START), event(EventType.WORK_END, 480))
    for item in original:
        store.append_event(WORK_DATE, item)
    before = rows(db_path)
    first = (event(EventType.WORK_START, -30), original[1])
    second = (event(EventType.WORK_START, -60), event(EventType.WORK_END, 510))
    assert store.save_correction(WORK_DATE, iter(first)).events == first
    old = correction_rows(db_path)
    assert store.save_correction(WORK_DATE, second).events == second
    assert store.list_correction_revisions(WORK_DATE) == (1, 2)
    assert store.load_correction_revision(WORK_DATE, 1).events == first
    assert store.load_correction_revision(WORK_DATE, 2).events == second
    assert store.load_raw_events(WORK_DATE) == original
    assert rows(db_path) == before
    metadata, snapshots = correction_rows(db_path)
    assert metadata[:1] == old[0]
    assert [r for r in snapshots if r[1] == 1] == old[1]
    with SQLiteEventStore(db_path) as reopened:
        assert reopened.load_day(WORK_DATE).events == second
        assert reopened.load_raw_events(WORK_DATE) == original


@pytest.mark.parametrize("add_pause", [False, True])
def test_raw_sequence_ignores_corrected_event_count(store, db_path, add_pause):
    start = event(EventType.WORK_START)
    pause = (event(EventType.PAUSE_START, 60), event(EventType.PAUSE_END, 90))
    raw = (start,) if add_pause else (start,) + pause
    corrected = (event(EventType.WORK_START, -30),) + (pause if add_pause else ())
    for item in raw:
        store.append_event(WORK_DATE, item)
    before = rows(db_path)
    store.save_correction(WORK_DATE, corrected)
    assert rows(db_path) == before
    end = event(EventType.WORK_END, 480)
    assert store.append_event(WORK_DATE, end).events == corrected + (end,)
    assert [r[1] for r in rows(db_path)] == list(range(1, len(raw) + 2))
    assert store.load_raw_events(WORK_DATE) == raw + (end,)
    assert correction_rows(db_path)[0][0][2] == len(raw)


@pytest.mark.parametrize("paused,next_kind", [
    (False, EventType.WORK_END), (True, EventType.PAUSE_END), (True, EventType.WORK_END),
])
def test_correction_only_day_then_first_raw_event(store, db_path, paused, next_kind):
    snapshot = (event(EventType.WORK_START),)
    if paused:
        snapshot += (event(EventType.PAUSE_START, 60),)
    store.save_correction(WORK_DATE, snapshot)
    assert store.load_raw_events(WORK_DATE) == ()
    assert store.list_work_dates() == (WORK_DATE,)
    assert correction_rows(db_path)[0] == [(WORK_DATE.isoformat(), 1, 0)]
    later = event(next_kind, 480)
    effective = store.append_event(WORK_DATE, later)
    assert effective.events == snapshot + (later,)
    assert store.load_raw_events(WORK_DATE) == (later,)
    assert rows(db_path) == [(WORK_DATE.isoformat(), 1, next_kind.value, later.timestamp.isoformat())]
    assert store.load_day(WORK_DATE).state is (
        WorkState.FINISHED if next_kind is EventType.WORK_END else WorkState.WORKING)
    assert effective.calculate(BASE + timedelta(hours=8)).net == timedelta(hours=1 if paused else 8)
    before = rows(db_path)
    store.save_correction(WORK_DATE, snapshot)
    assert rows(db_path) == before
    assert store.load_raw_events(WORK_DATE) == (later,)


def test_max_raw_sequence_and_revision_cutoff(store, db_path):
    store.append_event(WORK_DATE, event(EventType.WORK_START))
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("INSERT INTO work_events VALUES (?, 5, 'PAUSE_START', ?)",
                           (WORK_DATE.isoformat(), (BASE + timedelta(hours=1)).isoformat()))
        connection.commit()
    snapshot = (event(EventType.WORK_START, -30),)
    store.save_correction(WORK_DATE, snapshot)
    store.append_event(WORK_DATE, event(EventType.WORK_END, 480))
    assert [r[1] for r in rows(db_path)] == [1, 5, 6]
    assert store.load_day(WORK_DATE).events == snapshot + (event(EventType.WORK_END, 480),)
    store.save_correction(WORK_DATE, snapshot)
    assert store.load_day(WORK_DATE).events == snapshot
    assert [r[2] for r in correction_rows(db_path)[0]] == [5, 6]


@pytest.mark.parametrize("bad", [
    (), (event(EventType.WORK_END),),
    (event(EventType.WORK_START), event(EventType.WORK_START, 60)),
    (event(EventType.WORK_START), event(EventType.WORK_END, -1)),
])
def test_invalid_correction_is_atomic(store, db_path, bad):
    store.append_event(WORK_DATE, event(EventType.WORK_START))
    before = rows(db_path), correction_rows(db_path)
    with pytest.raises(DomainError):
        store.save_correction(WORK_DATE, bad)
    assert (rows(db_path), correction_rows(db_path)) == before
    store.save_correction(WORK_DATE, (event(EventType.WORK_START),))
    assert store.list_correction_revisions(WORK_DATE) == (1,)


def test_partial_snapshot_failure_rolls_back_revision_number(store, db_path):
    store.append_event(WORK_DATE, event(EventType.WORK_START))
    snapshot = (event(EventType.WORK_START, -60), event(EventType.WORK_END, 480))
    store.save_correction(WORK_DATE, snapshot)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("""
            CREATE TRIGGER fail_snapshot AFTER INSERT ON workday_correction_events
            WHEN NEW.sequence = 2 BEGIN SELECT RAISE(FAIL, 'snapshot failure'); END
        """)
        connection.commit()
    before = rows(db_path), correction_rows(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="snapshot failure"):
        store.save_correction(WORK_DATE, snapshot)
    assert (rows(db_path), correction_rows(db_path)) == before
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("DROP TRIGGER fail_snapshot")
        connection.commit()
    store.save_correction(WORK_DATE, snapshot)
    assert store.list_correction_revisions(WORK_DATE) == (1, 2)


def test_failed_append_validates_effective_snapshot_only(store, db_path):
    store.save_correction(WORK_DATE, (event(EventType.WORK_START),))
    before = rows(db_path), correction_rows(db_path)
    with pytest.raises(DomainError):
        store.append_event(WORK_DATE, event(EventType.PAUSE_END, 60))
    assert (rows(db_path), correction_rows(db_path)) == before
    store.append_event(WORK_DATE, event(EventType.WORK_END, 480))
    assert rows(db_path)[0][1] == 1


def test_dates_union_and_missing_revision(store):
    for day in (date(2026, 10, 3), WORK_DATE):
        store.save_correction(day, (event(EventType.WORK_START),))
    store.append_event(WORK_DATE, event(EventType.WORK_END, 480))
    store.append_event(date(2026, 10, 2), event(EventType.WORK_START))
    assert store.list_work_dates() == (WORK_DATE, date(2026, 10, 2), date(2026, 10, 3))
    with pytest.raises(LookupError):
        store.load_correction_revision(WORK_DATE, 99)


def test_concurrent_append_and_correction_are_serialized(db_path):
    with SQLiteEventStore(db_path) as store:
        store.append_event(WORK_DATE, event(EventType.WORK_START))
    barrier = Barrier(2, timeout=10)
    snapshot = (event(EventType.WORK_START, -30),)

    def operation(correct):
        with SQLiteEventStore(db_path) as store:
            barrier.wait()
            if correct:
                store.save_correction(WORK_DATE, snapshot)
            else:
                store.append_event(WORK_DATE, event(EventType.WORK_END, 480))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(operation, (True, False)))
    with SQLiteEventStore(db_path) as store:
        cutoff = correction_rows(db_path)[0][0][2]
        expected = snapshot + ((event(EventType.WORK_END, 480),) if cutoff == 1 else ())
        assert cutoff in (1, 2)
        assert store.load_day(WORK_DATE).events == expected
        assert store.load_raw_events(WORK_DATE) == (
            event(EventType.WORK_START), event(EventType.WORK_END, 480))
        assert store.list_correction_revisions(WORK_DATE) == (1,)


def test_pause_times_and_running_paused_day_can_be_corrected(store, db_path):
    raw = (event(EventType.WORK_START), event(EventType.PAUSE_START, 60))
    for item in raw:
        store.append_event(WORK_DATE, item)
    before = rows(db_path)
    corrected = (event(EventType.WORK_START, -30), event(EventType.PAUSE_START, 90))
    assert store.save_correction(WORK_DATE, corrected).state is WorkState.PAUSED
    finished = store.append_event(WORK_DATE, event(EventType.WORK_END, 120))
    assert finished.calculate().pause == timedelta(minutes=30)
    assert finished.calculate().net == timedelta(hours=2)
    assert rows(db_path)[:2] == before
    shifted = (corrected[0], event(EventType.PAUSE_START, 30),
               event(EventType.PAUSE_END, 45), event(EventType.WORK_END, 150))
    store.save_correction(WORK_DATE, shifted)
    assert store.load_day(WORK_DATE).calculate().pause == timedelta(minutes=15)
    assert store.load_day(WORK_DATE).calculate().net == timedelta(minutes=165)
    assert store.load_raw_events(WORK_DATE) == raw + (event(EventType.WORK_END, 120),)
