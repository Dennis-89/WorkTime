"""Local SQLite persistence of ordered domain events."""

from collections.abc import Iterable
from datetime import date, datetime
from os import PathLike
import sqlite3
from typing import Self

from worktime.domain import DomainError, EventType, WorkDay, WorkEvent


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

    def append_event(self, work_date: date, event: WorkEvent) -> WorkDay:
        """Validate with WorkDay.apply, then atomically persist the event.

        Domain and SQLite errors propagate. Any failure rolls back the entire
        append, including an insert that failed after changing a row.
        """
        key = _date_key(work_date)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            day = self.load_day(work_date)
            day.apply(event)
            self._connection.execute(
                "INSERT INTO work_events "
                "(work_date, sequence, event_type, timestamp) VALUES (?, ?, ?, ?)",
                (key, self._raw_sequence(key) + 1, event.kind.value, event.timestamp.isoformat()),
            )
        return day

    def load_day(self, work_date: date) -> WorkDay:
        """Read the latest snapshot and raw tail in one consistent SQL read."""
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
        return self._reconstruct(rows)

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

    def save_correction(self, work_date: date, events: Iterable[WorkEvent]) -> WorkDay:
        """Append a validated snapshot and its raw cutoff in one transaction."""
        key = _date_key(work_date)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            day = WorkDay()
            for event in events:
                day.apply(event)
            if not day.events:
                raise DomainError("Eine leere Korrektur ist nicht erlaubt.")
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
        return day

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
