"""Application use cases: clock, logical workday selection and persistence."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from threading import RLock

from worktime.domain import DomainError, EventType, WorkDay, WorkEvent, WorkState
from worktime.storage import SQLiteEventStore
from worktime.paths import database_path
from worktime.correction import windows_local_time
from worktime.summary import DaySummary, WeekSummary, summarize_day, summarize_week
from worktime.summary import MonthSummary, summarize_month, summarize_date_range, validate_date_range


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

    def __init__(self, database_path: str | Path, *, clock: Callable[[], datetime] = local_now,
                 time_resolver: Callable[[datetime], datetime] = windows_local_time):
        self._database_path = Path(database_path)
        self._clock = clock
        self._time_resolver = time_resolver
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
                day = store.append_event(selected.work_date, event,
                                         new_day=kind is EventType.WORK_START)
                return AppStatus(selected.work_date, day)

    def auto_start_if_needed(self) -> AppStatus:
        """Close earlier open days at 23:59 and start today at this trigger."""
        with self._operations:
            now = self._now()
            with SQLiteEventStore(self._database_path) as store:
                def day_end(work_date):
                    return self._time_resolver(datetime(work_date.year, work_date.month,
                                                        work_date.day, 23, 59))
                work_date, day = store.auto_start(now, day_end)
                return AppStatus(work_date, day)

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
                return summarize_day(work_date, store.load_day(work_date), as_of=now,
                                     flat_break_enabled=store.get_flat_break_enabled(work_date))

    def week_summary(self, anchor_date: date) -> WeekSummary:
        with self._operations:
            now = self._now()
            with SQLiteEventStore(self._database_path) as store:
                return summarize_week(anchor_date, store.load_day, as_of=now,
                                      load_flat_break_enabled=store.get_flat_break_enabled)

    def flat_break_enabled(self, work_date: date) -> bool:
        with self._operations:
            if self._closed:
                raise RuntimeError("WorkTime ist bereits geschlossen.")
            with SQLiteEventStore(self._database_path) as store:
                return store.get_flat_break_enabled(work_date)

    def month_summary(self, anchor_date: date) -> MonthSummary:
        with self._operations:
            now = self._now()
            with SQLiteEventStore(self._database_path) as store:
                return summarize_month(anchor_date, store.load_day, as_of=now,
                                       load_flat_break_enabled=store.get_flat_break_enabled)

    def date_range_summaries(self, start_date: date, end_date: date) -> tuple[DaySummary, ...]:
        validate_date_range(start_date, end_date)
        with self._operations:
            now = self._now()
            with SQLiteEventStore(self._database_path) as store:
                return summarize_date_range(start_date, end_date, store.load_day, as_of=now,
                                            load_flat_break_enabled=store.get_flat_break_enabled)

    def set_flat_break_enabled(self, work_date: date, enabled: bool) -> None:
        with self._operations:
            if self._closed:
                raise RuntimeError("WorkTime ist bereits geschlossen.")
            with SQLiteEventStore(self._database_path) as store:
                store.set_flat_break_enabled(work_date, enabled)

    def correct_day(self, work_date: date, events: Iterable[WorkEvent], *,
                    auto_lunch_minutes: int | None = None) -> AppStatus:
        with self._operations:
            if self._closed:
                raise RuntimeError("WorkTime ist bereits geschlossen.")
            with SQLiteEventStore(self._database_path) as store:
                return AppStatus(work_date, store.save_correction(
                    work_date, events, auto_lunch_minutes=auto_lunch_minutes))

    def raw_events(self, work_date: date) -> tuple[WorkEvent, ...]:
        with self._operations:
            if self._closed:
                raise RuntimeError("WorkTime ist bereits geschlossen.")
            with SQLiteEventStore(self._database_path) as store:
                return store.load_raw_events(work_date)


def create_default_app(
    base_directory: str | Path | None = None, *, clock: Callable[[], datetime] = local_now
) -> WorkTimeApp:
    """Use LOCALAPPDATA/WorkTime; an explicit base is available for tests."""
    return WorkTimeApp(database_path(base_directory=base_directory), clock=clock)
