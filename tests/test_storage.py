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
