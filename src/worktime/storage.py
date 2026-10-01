"""Local SQLite persistence of ordered domain events."""

from datetime import date, datetime
from os import PathLike
import sqlite3
from typing import Self

from worktime.domain import EventType, WorkDay, WorkEvent


_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_events (
    work_date TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    PRIMARY KEY (work_date, sequence)
)
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
            self._connection.execute(_SCHEMA)
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
                (key, len(day.events), event.kind.value, event.timestamp.isoformat()),
            )
        return day

    def load_day(self, work_date: date) -> WorkDay:
        """Rebuild a fresh domain object in stored sequence order."""
        rows = self._connection.execute(
            "SELECT event_type, timestamp FROM work_events "
            "WHERE work_date = ? ORDER BY sequence",
            (_date_key(work_date),),
        ).fetchall()
        day = WorkDay()
        for kind, timestamp in rows:
            day.apply(WorkEvent(EventType(kind), datetime.fromisoformat(timestamp)))
        return day

    def list_work_dates(self) -> tuple[date, ...]:
        """Return unique logical work dates in ascending order."""
        rows = self._connection.execute(
            "SELECT DISTINCT work_date FROM work_events ORDER BY work_date"
        ).fetchall()
        return tuple(date.fromisoformat(row[0]) for row in rows)
