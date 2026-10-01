from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone, tzinfo

import pytest

from worktime.domain import (
    DomainError, EventType, WorkDay, WorkEvent, WorkState, WorkTimes,
)


BASE = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


def event(kind, minutes=0):
    return WorkEvent(kind, BASE + timedelta(minutes=minutes))


def day_in(state):
    day = WorkDay()
    if state is not WorkState.NOT_STARTED:
        day.apply(event(EventType.WORK_START))
    if state is WorkState.PAUSED:
        day.apply(event(EventType.PAUSE_START, 60))
    if state is WorkState.FINISHED:
        day.apply(event(EventType.WORK_END, 120))
    return day


ALLOWED = [
    (WorkState.NOT_STARTED, EventType.WORK_START, WorkState.WORKING),
    (WorkState.WORKING, EventType.PAUSE_START, WorkState.PAUSED),
    (WorkState.PAUSED, EventType.PAUSE_END, WorkState.WORKING),
    (WorkState.WORKING, EventType.WORK_END, WorkState.FINISHED),
    (WorkState.PAUSED, EventType.WORK_END, WorkState.FINISHED),
]
INVALID = [
    (state, kind)
    for state in WorkState
    for kind in EventType
    if not any(state is s and kind is k for s, k, _ in ALLOWED)
]


def test_exact_states_and_event_types():
    assert {s.value for s in WorkState} == {
        "NOT_STARTED", "WORKING", "PAUSED", "FINISHED",
    }
    assert {e.value for e in EventType} == {
        "WORK_START", "PAUSE_START", "PAUSE_END", "WORK_END",
    }


def test_new_day():
    day = WorkDay()
    assert day.state is WorkState.NOT_STARTED
    assert day.events == ()
    assert day.calculate() == WorkTimes(timedelta(), timedelta(), timedelta())


@pytest.mark.parametrize("state,kind,expected", ALLOWED)
def test_allowed_transitions(state, kind, expected):
    day = day_in(state)
    addition = event(kind, 180)
    before = day.events
    day.apply(addition)
    assert day.state is expected
    assert day.events == before + (addition,)


@pytest.mark.parametrize("state,kind", INVALID)
def test_invalid_transitions_preserve_day(state, kind):
    day = day_in(state)
    before = day.events
    with pytest.raises(DomainError):
        day.apply(event(kind, 180))
    assert day.state is state
    assert day.events == before


@pytest.mark.parametrize("state,kind,expected", ALLOWED[1:])
def test_backwards_events_preserve_day(state, kind, expected):
    day = day_in(state)
    before = day.events
    backwards = before[-1].timestamp - timedelta(microseconds=1)
    with pytest.raises(DomainError, match="precedes"):
        day.apply(WorkEvent(kind, backwards))
    assert day.state is state
    assert day.events == before
    day.apply(WorkEvent(kind, before[-1].timestamp))
    assert day.state is expected


class NoOffset(tzinfo):
    def utcoffset(self, dt):
        return None


@pytest.mark.parametrize("kind", list(EventType))
@pytest.mark.parametrize("zone", [None, NoOffset()])
def test_naive_events_rejected(kind, zone):
    with pytest.raises(DomainError, match="timezone-aware"):
        WorkEvent(kind, datetime(2026, 10, 1, 8, tzinfo=zone))


@pytest.mark.parametrize("state", list(WorkState))
@pytest.mark.parametrize("zone", [None, NoOffset()])
def test_naive_evaluation_rejected(state, zone):
    with pytest.raises(DomainError, match="timezone-aware"):
        day_in(state).calculate(datetime(2026, 10, 1, 18, tzinfo=zone))


@pytest.mark.parametrize("state", [WorkState.WORKING, WorkState.PAUSED])
def test_running_day_requires_evaluation(state):
    day = day_in(state)
    with pytest.raises(DomainError, match="explicit"):
        day.calculate()
    with pytest.raises(DomainError, match="explicit"):
        day.calculate(None)


@pytest.mark.parametrize("state", [WorkState.WORKING, WorkState.PAUSED])
def test_evaluation_before_last_event_rejected(state):
    day = day_in(state)
    with pytest.raises(DomainError, match="precedes"):
        day.calculate(day.events[-1].timestamp - timedelta(microseconds=1))


@pytest.mark.parametrize("state,gross,pause,net", [
    (WorkState.WORKING, 180, 0, 180),
    (WorkState.PAUSED, 180, 120, 60),
])
def test_running_durations_are_deterministic(state, gross, pause, net):
    day = day_in(state)
    before = day.events
    expected = WorkTimes(*(timedelta(minutes=m) for m in (gross, pause, net)))
    assert day.calculate(BASE + timedelta(minutes=180)) == expected
    day.calculate(BASE + timedelta(minutes=240))
    assert day.calculate(BASE + timedelta(minutes=180)) == expected
    assert day.events == before
    assert day.state is state


@pytest.mark.parametrize("state,gross,pause,net", [
    (WorkState.WORKING, 0, 0, 0),
    (WorkState.PAUSED, 60, 0, 60),
])
def test_evaluation_equal_to_last_event(state, gross, pause, net):
    day = day_in(state)
    assert day.calculate(day.events[-1].timestamp) == WorkTimes(
        *(timedelta(minutes=m) for m in (gross, pause, net))
    )


def test_finished_without_pause_does_not_keep_growing():
    day = day_in(WorkState.FINISHED)
    expected = WorkTimes(timedelta(hours=2), timedelta(), timedelta(hours=2))
    assert day.calculate() == expected
    assert day.calculate(BASE + timedelta(days=1)) == expected


@pytest.mark.parametrize("finish", [False, True])
def test_multiple_closed_pauses(finish):
    day = WorkDay()
    for kind, minutes in [
        (EventType.WORK_START, 0),
        (EventType.PAUSE_START, 120),
        (EventType.PAUSE_END, 135),
        (EventType.PAUSE_START, 240),
        (EventType.PAUSE_END, 270),
    ]:
        day.apply(event(kind, minutes))
    if finish:
        day.apply(event(EventType.WORK_END, 480))
    assert day.calculate(None if finish else BASE + timedelta(hours=8)) == WorkTimes(
        timedelta(hours=8), timedelta(minutes=45), timedelta(minutes=435)
    )


@pytest.mark.parametrize("finish", [False, True])
def test_open_pause_includes_time_until_evaluation_or_work_end(finish):
    day = WorkDay()
    for kind, minutes in [
        (EventType.WORK_START, 0),
        (EventType.PAUSE_START, 60),
        (EventType.PAUSE_END, 75),
        (EventType.PAUSE_START, 120),
    ]:
        day.apply(event(kind, minutes))
    if finish:
        day.apply(event(EventType.WORK_END, 180))
    assert day.state is (WorkState.FINISHED if finish else WorkState.PAUSED)
    assert day.calculate(None if finish else BASE + timedelta(hours=3)) == WorkTimes(
        timedelta(hours=3), timedelta(minutes=75), timedelta(minutes=105)
    )


def test_equal_timestamps_allow_zero_length_intervals():
    day = WorkDay()
    for kind in [
        EventType.WORK_START, EventType.PAUSE_START, EventType.PAUSE_END,
        EventType.PAUSE_START, EventType.WORK_END,
    ]:
        day.apply(event(kind))
    assert day.calculate() == WorkTimes(timedelta(), timedelta(), timedelta())


def test_offsets_use_instant_order_and_preserve_source_timestamp():
    day = WorkDay()
    start = datetime(2026, 10, 1, 10, tzinfo=timezone(timedelta(hours=2)))
    end = datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    day.apply(WorkEvent(EventType.WORK_START, start))
    day.apply(WorkEvent(EventType.WORK_END, end))
    assert day.events[0].timestamp is start
    assert day.calculate() == WorkTimes(timedelta(hours=1), timedelta(), timedelta(hours=1))


def test_later_wall_clock_with_earlier_instant_is_rejected():
    day = day_in(WorkState.WORKING)
    earlier = datetime(2026, 10, 1, 9, tzinfo=timezone(timedelta(hours=2)))
    with pytest.raises(DomainError, match="precedes"):
        day.apply(WorkEvent(EventType.WORK_END, earlier))
    with pytest.raises(DomainError, match="precedes"):
        day.calculate(earlier)


class FoldOffset(tzinfo):
    """Repeated local hour with two offsets, without a platform tz database."""

    def utcoffset(self, dt):
        return timedelta(hours=2 - dt.fold)

    def dst(self, dt):
        return timedelta()


def test_repeated_local_hour_counts_elapsed_time_and_rejects_reverse():
    zone = FoldOffset()
    first = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    second = first.replace(fold=1)
    day = WorkDay()
    day.apply(WorkEvent(EventType.WORK_START, first))
    day.apply(WorkEvent(EventType.PAUSE_START, first))
    assert day.calculate(second) == WorkTimes(timedelta(hours=1), timedelta(hours=1), timedelta())
    day.apply(WorkEvent(EventType.PAUSE_END, second))
    with pytest.raises(DomainError, match="precedes"):
        day.apply(WorkEvent(EventType.WORK_END, first))
    day.apply(WorkEvent(EventType.WORK_END, second))
    assert day.calculate() == WorkTimes(timedelta(hours=1), timedelta(hours=1), timedelta())


def test_subsecond_precision():
    day = WorkDay()
    day.apply(event(EventType.WORK_START))
    day.apply(WorkEvent(EventType.PAUSE_START, BASE + timedelta(microseconds=1)))
    day.apply(WorkEvent(EventType.WORK_END, BASE + timedelta(microseconds=3)))
    assert day.calculate() == WorkTimes(
        timedelta(microseconds=3), timedelta(microseconds=2), timedelta(microseconds=1)
    )


def test_history_and_events_cannot_be_mutated_through_public_api():
    day = day_in(WorkState.WORKING)
    snapshot = day.events
    with pytest.raises(FrozenInstanceError):
        snapshot[0].timestamp = BASE - timedelta(hours=1)
    with pytest.raises(AttributeError):
        day.state = WorkState.FINISHED
    with pytest.raises(AttributeError):
        day.events = ()
    day.apply(event(EventType.PAUSE_START, 60))
    assert len(snapshot) == 1
    assert len(day.events) == 2
