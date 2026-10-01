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


@dataclass(frozen=True)
class WeekSummary:
    monday: date
    sunday: date
    days: tuple[DaySummary, ...]
    total_net: timedelta
    incomplete_dates: tuple[date, ...]


def summarize_day(work_date: date, day: WorkDay, *, as_of: datetime) -> DaySummary:
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
    if day.state is WorkState.FINISHED:
        times = day.calculate()
    elif running and not incomplete:
        times = day.calculate(as_of)
    return DaySummary(
        work_date, day.state,
        day.events[0].timestamp if day.events else None,
        day.events[-1].timestamp if day.state is WorkState.FINISHED else None,
        tuple(pauses),
        times.gross if times else None,
        times.pause if times else None,
        times.net if times else None,
        incomplete,
    )


def summarize_week(anchor_date: date, load_day: Callable[[date], WorkDay],
                   *, as_of: datetime) -> WeekSummary:
    monday = anchor_date - timedelta(days=anchor_date.weekday())
    days = tuple(summarize_day(monday + timedelta(days=index),
                              load_day(monday + timedelta(days=index)), as_of=as_of)
                 for index in range(7))
    return WeekSummary(monday, monday + timedelta(days=6), days,
                       sum((day.net for day in days if day.net is not None), timedelta()),
                       tuple(day.work_date for day in days if day.incomplete))
