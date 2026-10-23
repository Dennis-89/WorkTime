"""GUI-independent summaries of effective workdays."""
from collections.abc import Callable
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from worktime.domain import DomainError, EventType, WorkDay, WorkState


@dataclass(frozen=True)
class PauseSummary:
    start: datetime
    end: datetime | None


@dataclass(frozen=True)
class DaySummary:
    work_date: date
    state: WorkState
    start: datetime | None
    end: datetime | None
    pauses: tuple[PauseSummary, ...]
    gross: timedelta | None
    pause: timedelta | None
    net: timedelta | None
    incomplete: bool
    flat_break: timedelta
    auto_lunch_minutes: int | None = None
    lunch: timedelta = timedelta()


@dataclass(frozen=True)
class WeekSummary:
    monday: date
    sunday: date
    days: tuple[DaySummary, ...]
    total_net: timedelta
    incomplete_dates: tuple[date, ...]


@dataclass(frozen=True)
class MonthSummary:
    first_day: date
    last_day: date
    days: tuple[DaySummary, ...]
    total_net: timedelta
    incomplete_dates: tuple[date, ...]


def validate_date_range(start_date: date, end_date: date) -> None:
    for value in (start_date, end_date):
        if not isinstance(value, date) or isinstance(value, datetime):
            raise TypeError("Der Zeitraum benötigt Kalenderdaten, keine Zeitstempel.")
    if start_date > end_date:
        raise DomainError("Startdatum darf nicht nach dem Enddatum liegen.")


def month_bounds(anchor_date: date) -> tuple[date, date]:
    validate_date_range(anchor_date, anchor_date)
    return (anchor_date.replace(day=1),
            anchor_date.replace(day=monthrange(anchor_date.year, anchor_date.month)[1]))


def summarize_date_range(start_date: date, end_date: date, load_day: Callable[[date], WorkDay],
                         *, as_of: datetime,
                         load_flat_break_enabled: Callable[[date], bool] | None = None) -> tuple[DaySummary, ...]:
    """Inclusive calendar range; one supplied evaluation instant for every day."""
    validate_date_range(start_date, end_date)
    days = []
    for index in range((end_date - start_date).days + 1):
        work_date = start_date + timedelta(days=index)
        days.append(summarize_day(work_date, load_day(work_date), as_of=as_of,
                                 flat_break_enabled=(load_flat_break_enabled(work_date)
                                                     if load_flat_break_enabled else True)))
    return tuple(days)


def summarize_month(anchor_date: date, load_day: Callable[[date], WorkDay], *, as_of: datetime,
                    load_flat_break_enabled: Callable[[date], bool] | None = None) -> MonthSummary:
    first, last = month_bounds(anchor_date)
    days = summarize_date_range(first, last, load_day, as_of=as_of,
                                load_flat_break_enabled=load_flat_break_enabled)
    return MonthSummary(first, last, days,
                        sum((day.net for day in days if day.net is not None), timedelta()),
                        tuple(day.work_date for day in days if day.incomplete))


def summarize_day(work_date: date, day: WorkDay, *, as_of: datetime,
                  flat_break_enabled: bool = True) -> DaySummary:
    if type(flat_break_enabled) is not bool:
        raise TypeError("flat_break_enabled must be a bool.")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise DomainError("Die Auswertung benötigt eine Zeitzone.")
    pauses = []
    opened = None
    for event in day.events:
        if event.kind is EventType.PAUSE_START:
            opened = event.timestamp
        elif opened is not None and event.kind in (EventType.PAUSE_END, EventType.WORK_END):
            pauses.append(PauseSummary(opened, event.timestamp))
            opened = None
    if opened is not None:
        pauses.append(PauseSummary(opened, None))
    running = day.state in (WorkState.WORKING, WorkState.PAUSED)
    incomplete = running and work_date != as_of.date()
    times = None
    if day.state in (WorkState.NOT_STARTED, WorkState.FINISHED):
        times = day.calculate()
    elif running and not incomplete:
        times = day.calculate(as_of)
    flat_break = (timedelta(minutes=15)
                  if day.state is not WorkState.NOT_STARTED and flat_break_enabled else timedelta())
    lunch = (timedelta(minutes=day.auto_lunch_minutes)
             if day.auto_lunch_minutes is not None and not day.manual_pause_seen
             and not any(event.kind is EventType.PAUSE_START for event in day.events)
             and day.state is not WorkState.NOT_STARTED else timedelta())
    return DaySummary(
        work_date, day.state,
        day.events[0].timestamp if day.events else None,
        day.events[-1].timestamp if day.state is WorkState.FINISHED else None,
        tuple(pauses),
        times.gross if times else None,
        times.pause + lunch if times else None,
        max(times.gross - times.pause - lunch - flat_break, timedelta()) if times else None,
        incomplete,
        flat_break,
        day.auto_lunch_minutes,
        lunch,
    )


def summarize_week(anchor_date: date, load_day: Callable[[date], WorkDay],
                   *, as_of: datetime,
                   load_flat_break_enabled: Callable[[date], bool] | None = None) -> WeekSummary:
    monday = anchor_date - timedelta(days=anchor_date.weekday())
    days = tuple(summarize_day(monday + timedelta(days=index),
                              load_day(monday + timedelta(days=index)), as_of=as_of,
                              flat_break_enabled=(load_flat_break_enabled(monday + timedelta(days=index))
                                                  if load_flat_break_enabled else True))
                 for index in range(7))
    return WeekSummary(monday, monday + timedelta(days=6), days,
                       sum((day.net for day in days if day.net is not None), timedelta()),
                       tuple(day.work_date for day in days if day.incomplete))
