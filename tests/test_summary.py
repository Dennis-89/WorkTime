from datetime import date, datetime, timedelta, timezone

import pytest

from worktime.domain import DomainError, EventType, WorkDay, WorkEvent, WorkState
from worktime.storage import SQLiteEventStore
from worktime.summary import summarize_day, summarize_week

DAY = date(2026, 10, 1)
BASE = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


def day_with(*items):
    day = WorkDay()
    for kind, minutes in items:
        day.apply(WorkEvent(kind, BASE + timedelta(minutes=minutes)))
    return day


@pytest.mark.parametrize("state,net,pause", [
    (WorkState.NOT_STARTED, None, None),
    (WorkState.WORKING, 480, 0),
    (WorkState.PAUSED, 60, 420),
    (WorkState.FINISHED, 420, 60),
])
def test_day_summary_states(state, net, pause):
    items = []
    if state is not WorkState.NOT_STARTED:
        items.append((EventType.WORK_START, 0))
    if state in (WorkState.PAUSED, WorkState.FINISHED):
        items.append((EventType.PAUSE_START, 60))
    if state is WorkState.FINISHED:
        items.extend([(EventType.PAUSE_END, 120), (EventType.WORK_END, 480)])
    summary = summarize_day(DAY, day_with(*items), as_of=BASE + timedelta(hours=8))
    assert summary.state is state
    assert summary.net == (timedelta(minutes=net) if net is not None else None)
    assert summary.pause == (timedelta(minutes=pause) if pause is not None else None)
    assert summary.gross == (timedelta(hours=8) if items else None)
    assert summary.start == (BASE if items else None)
    assert summary.end == (BASE + timedelta(hours=8) if state is WorkState.FINISHED else None)
    assert not summary.incomplete
    if state is WorkState.PAUSED:
        assert summary.pauses[0].end is None


@pytest.mark.parametrize("paused", [False, True])
def test_historical_open_day_never_counts_until_now(paused):
    items = [(EventType.WORK_START, 0)]
    if paused:
        items.append((EventType.PAUSE_START, 60))
    summary = summarize_day(DAY, day_with(*items), as_of=BASE + timedelta(days=10))
    assert summary.incomplete
    assert summary.net is summary.gross is summary.pause is None


def test_end_during_pause_closes_summary_pause():
    day = day_with((EventType.WORK_START, 0), (EventType.PAUSE_START, 60), (EventType.WORK_END, 120))
    summary = summarize_day(DAY, day, as_of=BASE + timedelta(days=10))
    assert summary.pauses[0].end == summary.end
    assert summary.net == summary.pause == timedelta(hours=1)


@pytest.mark.parametrize("anchor", [date(2026, 9, 28) + timedelta(days=i) for i in range(7)])
def test_week_is_monday_through_sunday_with_seven_days(anchor):
    summary = summarize_week(anchor, lambda _: WorkDay(), as_of=BASE)
    assert summary.monday == date(2026, 9, 28)
    assert summary.sunday == date(2026, 10, 4)
    assert len(summary.days) == 7
    assert tuple(d.work_date for d in summary.days) == tuple(summary.monday + timedelta(days=i) for i in range(7))
    assert summary.total_net == timedelta()
    assert summary.incomplete_dates == ()


def test_week_uses_corrected_and_correction_only_values_excludes_incomplete(tmp_path):
    with SQLiteEventStore(tmp_path / "summary.db") as store:
        start = WorkEvent(EventType.WORK_START, BASE)
        end = WorkEvent(EventType.WORK_END, BASE + timedelta(hours=8))
        store.append_event(DAY, start)
        store.append_event(DAY, end)
        store.save_correction(DAY, (WorkEvent(EventType.WORK_START, BASE - timedelta(hours=1)), end))
        store.save_correction(DAY - timedelta(days=1), (start, end))
        store.append_event(DAY - timedelta(days=2), start)
        summary = summarize_week(DAY, store.load_day, as_of=BASE + timedelta(hours=9))
    assert summary.total_net == timedelta(hours=17)
    assert summary.incomplete_dates == (DAY - timedelta(days=2),)
    assert summary.days[2].net == timedelta(hours=8)
    assert summary.days[3].net == timedelta(hours=9)


def test_as_of_must_be_aware_and_not_before_running_events():
    day = day_with((EventType.WORK_START, 0))
    for stamp in (BASE.replace(tzinfo=None), BASE - timedelta(minutes=1)):
        with pytest.raises(DomainError):
            summarize_day(DAY, day, as_of=stamp)
