"""GUI-independent summaries of effective workdays."""
from collections.abc import Callable
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


@dataclass(frozen=True)
class WeekSummary:
    monday: date
    sunday: date
    days: tuple[DaySummary, ...]
    total_net: timedelta
    incomplete_dates: tuple[date, ...]


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
    return DaySummary(
        work_date, day.state,
        day.events[0].timestamp if day.events else None,
        day.events[-1].timestamp if day.state is WorkState.FINISHED else None,
        tuple(pauses),
        times.gross if times else None,
        times.pause if times else None,
        max(times.gross - times.pause - flat_break, timedelta()) if times else None,
        incomplete,
        flat_break,
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
