"""Workday states and elapsed durations, independent of storage and UI."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


class DomainError(ValueError):
    """An event or evaluation violates a workday's domain rules."""


class WorkState(Enum):
    NOT_STARTED = "NOT_STARTED"
    WORKING = "WORKING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"


class EventType(Enum):
    WORK_START = "WORK_START"
    PAUSE_START = "PAUSE_START"
    PAUSE_END = "PAUSE_END"
    WORK_END = "WORK_END"


def _utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DomainError("A timezone-aware timestamp is required.")
    return timestamp.astimezone(timezone.utc)


@dataclass(frozen=True)
class WorkEvent:
    kind: EventType
    timestamp: datetime

    def __post_init__(self) -> None:
        _utc(self.timestamp)


@dataclass(frozen=True)
class WorkTimes:
    gross: timedelta
    pause: timedelta
    net: timedelta


_TRANSITIONS = {
    (WorkState.NOT_STARTED, EventType.WORK_START): WorkState.WORKING,
    (WorkState.WORKING, EventType.PAUSE_START): WorkState.PAUSED,
    (WorkState.PAUSED, EventType.PAUSE_END): WorkState.WORKING,
    (WorkState.WORKING, EventType.WORK_END): WorkState.FINISHED,
    (WorkState.PAUSED, EventType.WORK_END): WorkState.FINISHED,
}


class WorkDay:
    """An ordered event history; rejected events leave it unchanged."""

    def __init__(self, *, auto_lunch_minutes: int | None = None,
                 manual_pause_seen: bool = False) -> None:
        self.auto_lunch_minutes = auto_lunch_minutes
        self.manual_pause_seen = manual_pause_seen
        self._state = WorkState.NOT_STARTED
        self._events: list[WorkEvent] = []

    @property
    def state(self) -> WorkState:
        return self._state

    @property
    def events(self) -> tuple[WorkEvent, ...]:
        """Read-only history retaining the original timestamp offsets."""
        return tuple(self._events)

    def apply(self, event: WorkEvent) -> None:
        next_state = _TRANSITIONS.get((self._state, event.kind))
        if next_state is None:
            raise DomainError("Event is not allowed in the current workday state.")
        timestamp = _utc(event.timestamp)
        if self._events and timestamp < _utc(self._events[-1].timestamp):
            raise DomainError("Event timestamp precedes the previous event.")
        self._events.append(event)
        self._state = next_state

    def calculate(self, as_of: datetime | None = None) -> WorkTimes:
        """Calculate elapsed time, requiring as_of for WORKING and PAUSED.

        An unstarted day has zero durations. A finished day always ends at
        WORK_END. Any supplied evaluation timestamp must be timezone-aware.
        Equal event timestamps are allowed and yield zero-length intervals.
        """
        evaluation = _utc(as_of) if as_of is not None else None
        if self._state is WorkState.NOT_STARTED:
            return WorkTimes(timedelta(), timedelta(), timedelta())

        if self._state is WorkState.FINISHED:
            end = _utc(self._events[-1].timestamp)
        else:
            if evaluation is None:
                raise DomainError("An explicit evaluation timestamp is required.")
            if evaluation < _utc(self._events[-1].timestamp):
                raise DomainError("Evaluation timestamp precedes the last event.")
            end = evaluation

        gross = end - _utc(self._events[0].timestamp)
        pause = timedelta()
        pause_start = None
        for event in self._events:
            if event.kind is EventType.PAUSE_START:
                pause_start = _utc(event.timestamp)
            elif pause_start is not None and event.kind in (
                EventType.PAUSE_END, EventType.WORK_END
            ):
                pause += _utc(event.timestamp) - pause_start
                pause_start = None
        if pause_start is not None:
            pause += end - pause_start
        return WorkTimes(gross, pause, gross - pause)
