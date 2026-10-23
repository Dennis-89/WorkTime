"""Local SQLite persistence of ordered domain events."""

from collections.abc import Callable, Iterable
from datetime import date, datetime
from os import PathLike
import sqlite3
from typing import Self

from worktime.domain import DomainError, EventType, WorkDay, WorkEvent, WorkState


_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_events (
    work_date TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    PRIMARY KEY (work_date, sequence)
);
CREATE TABLE IF NOT EXISTS workday_corrections (
    work_date TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    raw_through_sequence INTEGER NOT NULL CHECK (raw_through_sequence >= 0),
    PRIMARY KEY (work_date, revision)
);
CREATE TABLE IF NOT EXISTS workday_correction_events (
    work_date TEXT NOT NULL,
    revision INTEGER NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    PRIMARY KEY (work_date, revision, sequence),
    FOREIGN KEY (work_date, revision)
        REFERENCES workday_corrections (work_date, revision)
);
CREATE TABLE IF NOT EXISTS workday_settings (
    work_date TEXT PRIMARY KEY,
    flat_break_enabled INTEGER NOT NULL CHECK (flat_break_enabled IN (0, 1))
);
CREATE TABLE IF NOT EXISTS workday_lunch (
    work_date TEXT PRIMARY KEY,
    auto_minutes INTEGER NOT NULL CHECK (auto_minutes >= 0),
    manual_pause_seen INTEGER NOT NULL DEFAULT 0 CHECK (manual_pause_seen IN (0, 1))
);
"""


def _date_key(work_date: date) -> str:
    if not isinstance(work_date, date) or isinstance(work_date, datetime):
        raise TypeError("work_date must be an explicit date, not a datetime.")
    return work_date.isoformat()


class SQLiteEventStore:
    """A store owning one SQLite connection; close it or use a with block.

    The database's parent directory must already exist. Each append commits
    independently. Separate store instances use SQLite's write locking so
    validation always sees the preceding committed events.
    """

    def __init__(self, path: str | PathLike[str]) -> None:
        self._connection = sqlite3.connect(path, isolation_level=None)
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(_SCHEMA)
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        """Close the underlying connection; already committed events persist."""
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def get_flat_break_enabled(self, work_date: date) -> bool:
        """Missing daily settings default to enabled, including historical days."""
        row = self._connection.execute(
            "SELECT flat_break_enabled FROM workday_settings WHERE work_date = ?",
            (_date_key(work_date),),
        ).fetchone()
        return row is None or row[0] == 1

    def set_flat_break_enabled(self, work_date: date, enabled: bool) -> None:
        """Atomically change only the daily option, never events or revisions."""
        key = _date_key(work_date)
        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool.")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "INSERT INTO workday_settings (work_date, flat_break_enabled) VALUES (?, ?) "
                "ON CONFLICT(work_date) DO UPDATE SET flat_break_enabled = excluded.flat_break_enabled",
                (key, int(enabled)),
            )

    def append_event(self, work_date: date, event: WorkEvent, *,
                     new_day: bool = False) -> WorkDay:
        """Validate with WorkDay.apply, then atomically persist the event.

        Domain and SQLite errors propagate. Any failure rolls back the entire
        append, including an insert that failed after changing a row.
        """
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            return self._append_event(work_date, event, new_day=new_day)

    def _append_event(self, work_date: date, event: WorkEvent, *,
                      new_day: bool = False) -> WorkDay:
        """Append inside an already held write transaction."""
        key = _date_key(work_date)
        day = self.load_day(work_date)
        day.apply(event)
        self._connection.execute(
            "INSERT INTO work_events VALUES (?, ?, ?, ?)",
            (key, self._raw_sequence(key) + 1, event.kind.value, event.timestamp.isoformat()),
        )
        if new_day:
            if event.kind is not EventType.WORK_START:
                raise DomainError("Mittagspausenmetadaten benötigen einen neuen Arbeitsbeginn.")
            self._connection.execute("INSERT INTO workday_lunch VALUES (?, 30, 0)", (key,))
        if event.kind is EventType.PAUSE_START:
            self._connection.execute(
                "UPDATE workday_lunch SET manual_pause_seen = 1 WHERE work_date = ?", (key,))
        return self.load_day(work_date)

    def auto_start(self, now: datetime,
                   day_end: Callable[[date], datetime]) -> tuple[date, WorkDay]:
        """Close earlier open days and start today atomically, across connections."""
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            for work_date in self.list_work_dates():
                if work_date >= now.date():
                    continue
                day = self.load_day(work_date)
                if day.state not in (WorkState.WORKING, WorkState.PAUSED):
                    continue
                end = day_end(work_date)
                if day.state is WorkState.PAUSED:
                    self._append_event(work_date, WorkEvent(EventType.PAUSE_END, end))
                self._append_event(work_date, WorkEvent(EventType.WORK_END, end))
            day = self.load_day(now.date())
            if day.state is WorkState.NOT_STARTED:
                day = self._append_event(now.date(), WorkEvent(EventType.WORK_START, now),
                                         new_day=True)
            return now.date(), day

    def load_day(self, work_date: date) -> WorkDay:
        """Read the latest snapshot and raw tail in one consistent SQL read."""
        if self._connection.in_transaction:
            return self._load_day(work_date)
        with self._connection:
            self._connection.execute("BEGIN")
            return self._load_day(work_date)

    def _load_day(self, work_date: date) -> WorkDay:
        """Keep events and lunch metadata in the same read snapshot."""
        rows = self._connection.execute(
            """
            WITH latest AS (
                SELECT revision, raw_through_sequence FROM workday_corrections
                WHERE work_date = ? ORDER BY revision DESC LIMIT 1
            )
            SELECT event_type, timestamp FROM (
                SELECT 0 AS source, sequence, event_type, timestamp
                FROM workday_correction_events
                WHERE work_date = ? AND revision = (SELECT revision FROM latest)
                UNION ALL
                SELECT 1 AS source, sequence, event_type, timestamp
                FROM work_events WHERE work_date = ?
                AND sequence > COALESCE((SELECT raw_through_sequence FROM latest), 0)
            ) ORDER BY source, sequence
            """,
            (_date_key(work_date),) * 3,
        ).fetchall()
        day = self._reconstruct(rows)
        metadata = self._connection.execute(
            "SELECT auto_minutes, manual_pause_seen FROM workday_lunch WHERE work_date = ?",
            (_date_key(work_date),),
        ).fetchone()
        if metadata is not None:
            day.auto_lunch_minutes, seen = metadata
            day.manual_pause_seen = bool(seen)
        return day

    @staticmethod
    def _reconstruct(rows) -> WorkDay:
        day = WorkDay()
        for kind, timestamp in rows:
            day.apply(WorkEvent(EventType(kind), datetime.fromisoformat(timestamp)))
        return day

    def load_raw_events(self, work_date: date) -> tuple[WorkEvent, ...]:
        """Audit only: raw events need not form a standalone valid WorkDay."""
        rows = self._connection.execute(
            "SELECT event_type, timestamp FROM work_events "
            "WHERE work_date = ? ORDER BY sequence",
            (_date_key(work_date),),
        ).fetchall()
        return tuple(WorkEvent(EventType(kind), datetime.fromisoformat(stamp))
                     for kind, stamp in rows)

    def _raw_sequence(self, key: str) -> int:
        return self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM work_events WHERE work_date = ?",
            (key,),
        ).fetchone()[0]

    def save_correction(self, work_date: date, events: Iterable[WorkEvent], *,
                        auto_lunch_minutes: int | None = None) -> WorkDay:
        """Append a validated snapshot and its raw cutoff in one transaction."""
        key = _date_key(work_date)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            day = WorkDay()
            for event in events:
                day.apply(event)
            if not day.events:
                raise DomainError("Eine leere Korrektur ist nicht erlaubt.")
            if auto_lunch_minutes is not None:
                if type(auto_lunch_minutes) is not int or auto_lunch_minutes < 0:
                    raise DomainError("Mittagspause benötigt eine nichtnegative ganze Minutenzahl.")
                updated = self._connection.execute(
                    "UPDATE workday_lunch SET auto_minutes = ? WHERE work_date = ?",
                    (auto_lunch_minutes, key),
                )
                if updated.rowcount != 1:
                    raise DomainError("Legacy-Tage erhalten keine WP-009-Mittagspause.")
            revision = self._connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM workday_corrections "
                "WHERE work_date = ?", (key,),
            ).fetchone()[0]
            self._connection.execute(
                "INSERT INTO workday_corrections VALUES (?, ?, ?)",
                (key, revision, self._raw_sequence(key)),
            )
            self._connection.executemany(
                "INSERT INTO workday_correction_events VALUES (?, ?, ?, ?, ?)",
                ((key, revision, sequence, event.kind.value, event.timestamp.isoformat())
                 for sequence, event in enumerate(day.events, 1)),
            )
        return self.load_day(work_date)

    def list_correction_revisions(self, work_date: date) -> tuple[int, ...]:
        return tuple(row[0] for row in self._connection.execute(
            "SELECT revision FROM workday_corrections WHERE work_date = ? ORDER BY revision",
            (_date_key(work_date),),
        ))

    def load_correction_revision(self, work_date: date, revision: int) -> WorkDay:
        rows = self._connection.execute(
            "SELECT event_type, timestamp FROM workday_correction_events "
            "WHERE work_date = ? AND revision = ? ORDER BY sequence",
            (_date_key(work_date), revision),
        ).fetchall()
        if not rows:
            raise LookupError("Korrekturrevision nicht vorhanden.")
        return self._reconstruct(rows)

    def list_work_dates(self) -> tuple[date, ...]:
        """Return unique logical work dates in ascending order."""
        rows = self._connection.execute(
            "SELECT work_date FROM work_events UNION "
            "SELECT work_date FROM workday_corrections ORDER BY work_date"
        ).fetchall()
        return tuple(date.fromisoformat(row[0]) for row in rows)
