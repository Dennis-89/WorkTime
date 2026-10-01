"""Application use cases: clock, logical workday selection and persistence."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from worktime.domain import DomainError, EventType, WorkDay, WorkEvent, WorkState
from worktime.storage import SQLiteEventStore


def local_now() -> datetime:
    """Current local time with an explicit UTC offset."""
    return datetime.now(timezone.utc).astimezone()


@dataclass(frozen=True)
class AppStatus:
    work_date: date
    day: WorkDay


class WorkTimeApp:
    """Use a short-lived store connection per call, including tray callbacks.

    No SQLite connection is shared between threads. Closing the application
    disables further calls; each store connection has already been closed.
    """

    def __init__(self, database_path: str | Path, *, clock: Callable[[], datetime] = local_now):
        self._database_path = Path(database_path)
        self._clock = clock
        self._closed = False
        with SQLiteEventStore(self._database_path):
            pass

    def _now(self) -> datetime:
        if self._closed:
            raise RuntimeError("WorkTime ist bereits geschlossen.")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise DomainError("Die Zeitquelle muss eine Zeitzone liefern.")
        return now

    @staticmethod
    def _select_day(store: SQLiteEventStore, now: datetime) -> AppStatus:
        dates = store.list_work_dates()
        if dates:
            latest = store.load_day(dates[-1])
            if latest.state in (WorkState.WORKING, WorkState.PAUSED):
                return AppStatus(dates[-1], latest)
        today = now.date()
        return AppStatus(today, store.load_day(today))

    def status(self) -> AppStatus:
        now = self._now()
        with SQLiteEventStore(self._database_path) as store:
            return self._select_day(store, now)

    def _append(self, kind: EventType) -> AppStatus:
        now = self._now()
        event = WorkEvent(kind, now)
        with SQLiteEventStore(self._database_path) as store:
            selected = self._select_day(store, now)
            day = store.append_event(selected.work_date, event)
            return AppStatus(selected.work_date, day)

    def start_work(self) -> AppStatus:
        return self._append(EventType.WORK_START)

    def pause_work(self) -> AppStatus:
        return self._append(EventType.PAUSE_START)

    def resume_work(self) -> AppStatus:
        return self._append(EventType.PAUSE_END)

    def finish_work(self) -> AppStatus:
        return self._append(EventType.WORK_END)

    def close(self) -> None:
        """Release application access without recording any domain event."""
        self._closed = True


def create_default_app(
    base_directory: str | Path = Path("."), *, clock: Callable[[], datetime] = local_now
) -> WorkTimeApp:
    """Use data/worktime.db below the working directory (or supplied base)."""
    database_path = Path(base_directory) / "data" / "worktime.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return WorkTimeApp(database_path, clock=clock)
