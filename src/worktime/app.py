"""Application use cases: clock, logical workday selection and persistence."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from threading import RLock

from worktime.domain import DomainError, EventType, WorkDay, WorkEvent, WorkState
from worktime.storage import SQLiteEventStore
from worktime.summary import DaySummary, WeekSummary, summarize_day, summarize_week


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
        self._operations = RLock()
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
        with self._operations:
            now = self._now()
            with SQLiteEventStore(self._database_path) as store:
                return self._select_day(store, now)

    def _append(self, kind: EventType) -> AppStatus:
        with self._operations:
            now = self._now()
            event = WorkEvent(kind, now)
            with SQLiteEventStore(self._database_path) as store:
                selected = self._select_day(store, now)
                day = store.append_event(selected.work_date, event)
                return AppStatus(selected.work_date, day)

    def auto_start_if_needed(self) -> AppStatus:
        """Start only an unstarted selected day, using one clock reading.

        Serialize with manual use cases on this application instance. Open
        previous days and today's finished day are returned without a write.
        """
        with self._operations:
            now = self._now()
            with SQLiteEventStore(self._database_path) as store:
                selected = self._select_day(store, now)
                if selected.day.state is not WorkState.NOT_STARTED:
                    return selected
                event = WorkEvent(EventType.WORK_START, now)
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
        with self._operations:
            self._closed = True

    def today(self) -> date:
        with self._operations:
            return self._now().date()

    def day_summary(self, work_date: date) -> DaySummary:
        with self._operations:
            now = self._now()
            with SQLiteEventStore(self._database_path) as store:
                return summarize_day(work_date, store.load_day(work_date), as_of=now)

    def week_summary(self, anchor_date: date) -> WeekSummary:
        with self._operations:
            now = self._now()
            with SQLiteEventStore(self._database_path) as store:
                return summarize_week(anchor_date, store.load_day, as_of=now)

    def correct_day(self, work_date: date, events: Iterable[WorkEvent]) -> AppStatus:
        with self._operations:
            if self._closed:
                raise RuntimeError("WorkTime ist bereits geschlossen.")
            with SQLiteEventStore(self._database_path) as store:
                return AppStatus(work_date, store.save_correction(work_date, events))

    def raw_events(self, work_date: date) -> tuple[WorkEvent, ...]:
        with self._operations:
            if self._closed:
                raise RuntimeError("WorkTime ist bereits geschlossen.")
            with SQLiteEventStore(self._database_path) as store:
                return store.load_raw_events(work_date)


def create_default_app(
    base_directory: str | Path = Path("."), *, clock: Callable[[], datetime] = local_now
) -> WorkTimeApp:
    """Use data/worktime.db below the working directory (or supplied base)."""
    database_path = Path(base_directory) / "data" / "worktime.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return WorkTimeApp(database_path, clock=clock)
