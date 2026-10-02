from datetime import date, datetime, timedelta, timezone

import pytest

from worktime.domain import DomainError, EventType, WorkDay, WorkEvent, WorkState
from worktime.storage import SQLiteEventStore
from worktime.summary import summarize_day, summarize_week
from worktime.summary import summarize_month, summarize_date_range

DAY = date(2026, 10, 1)
BASE = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


def day_with(*items):
    day = WorkDay()
    for kind, minutes in items:
        day.apply(WorkEvent(kind, BASE + timedelta(minutes=minutes)))
    return day


@pytest.mark.parametrize("state,net,pause", [
    (WorkState.NOT_STARTED, 0, 0),
    (WorkState.WORKING, 465, 0),
    (WorkState.PAUSED, 45, 420),
    (WorkState.FINISHED, 405, 60),
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
    assert summary.gross == (timedelta(hours=8) if items else timedelta())
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
    assert summary.pause == timedelta(hours=1)
    assert summary.net == timedelta(minutes=45)


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
    assert summary.total_net == timedelta(hours=16, minutes=30)
    assert summary.incomplete_dates == (DAY - timedelta(days=2),)
    assert summary.days[2].net == timedelta(hours=7, minutes=45)
    assert summary.days[3].net == timedelta(hours=8, minutes=45)


@pytest.mark.parametrize("pause,enabled,expected", [(0, True, 465), (30, True, 435),
                                                   (30, False, 450)])
def test_additional_flat_break_is_separate_from_actual_pause(pause, enabled, expected):
    items = [(EventType.WORK_START, 0)]
    if pause:
        items += [(EventType.PAUSE_START, 120), (EventType.PAUSE_END, 120 + pause)]
    items.append((EventType.WORK_END, 480))
    day = day_with(*items)
    before = day.events
    result = summarize_day(DAY, day, as_of=BASE + timedelta(days=20), flat_break_enabled=enabled)
    assert result.gross == timedelta(hours=8)
    assert result.pause == timedelta(minutes=pause)
    assert result.flat_break == timedelta(minutes=15 if enabled else 0)
    assert result.net == timedelta(minutes=expected)
    assert day.events == before


@pytest.mark.parametrize("minutes", [0, 1, 10, 14, 15, 16, 480])
@pytest.mark.parametrize("paused", [False, True])
def test_running_net_never_negative(minutes, paused):
    items = [(EventType.WORK_START, 0)]
    if paused:
        items.append((EventType.PAUSE_START, 0))
    summary = summarize_day(DAY, day_with(*items), as_of=BASE + timedelta(minutes=minutes))
    assert summary.flat_break == timedelta(minutes=15)
    assert summary.pause == timedelta(minutes=minutes if paused else 0)
    assert summary.net == timedelta(minutes=max(0, (0 if paused else minutes) - 15))


@pytest.mark.parametrize("enabled", [True, False])
def test_unstarted_day_has_zero_durations_even_if_enabled(enabled):
    summary = summarize_day(DAY, WorkDay(), as_of=BASE, flat_break_enabled=enabled)
    assert summary.gross == summary.pause == summary.flat_break == summary.net == timedelta()


def test_week_uses_each_daily_option_and_historical_default(tmp_path):
    with SQLiteEventStore(tmp_path / "daily-options.db") as store:
        for delta in (0, 1):
            work_date = DAY - timedelta(days=delta)
            start = BASE - timedelta(days=delta)
            store.append_event(work_date, WorkEvent(EventType.WORK_START, start))
            store.append_event(work_date, WorkEvent(EventType.WORK_END, start + timedelta(hours=8)))
        store.set_flat_break_enabled(DAY, False)
        week = summarize_week(DAY, store.load_day, as_of=BASE + timedelta(days=10),
                              load_flat_break_enabled=store.get_flat_break_enabled)
    assert week.total_net == timedelta(hours=15, minutes=45)
    assert week.days[2].flat_break == timedelta(minutes=15)
    assert week.days[3].flat_break == timedelta()


def test_finished_cross_midnight_and_offset_change_keep_elapsed_time():
    day = WorkDay()
    start = datetime(2026, 10, 24, 23, tzinfo=timezone(timedelta(hours=2)))
    end = datetime(2026, 10, 25, 4, tzinfo=timezone(timedelta(hours=1)))
    day.apply(WorkEvent(EventType.WORK_START, start))
    day.apply(WorkEvent(EventType.WORK_END, end))
    result = summarize_day(start.date(), day, as_of=end)
    assert result.gross == timedelta(hours=6)
    assert result.net == timedelta(hours=5, minutes=45)
    assert result.start == start and result.end == end


@pytest.mark.parametrize("enabled", [True, False])
def test_incomplete_history_keeps_unknown_times_and_week_exclusion(enabled):
    day = day_with((EventType.WORK_START, 0), (EventType.PAUSE_START, 10))
    result = summarize_day(DAY, day, as_of=BASE + timedelta(days=1), flat_break_enabled=enabled)
    assert result.incomplete and result.net is result.pause is result.gross is None
    assert result.flat_break == timedelta(minutes=15 if enabled else 0)


def test_as_of_must_be_aware_and_not_before_running_events():
    day = day_with((EventType.WORK_START, 0))
    for stamp in (BASE.replace(tzinfo=None), BASE - timedelta(minutes=1)):
        with pytest.raises(DomainError):
            summarize_day(DAY, day, as_of=stamp)


@pytest.mark.parametrize("anchor,count", [(date(2026, 1, 15), 31), (date(2026, 4, 15), 30),
                                         (date(2026, 2, 15), 28), (date(2028, 2, 15), 29),
                                         (date(9999, 12, 1), 31)])
def test_month_calendar_bounds_and_all_unstarted_days(anchor, count):
    month = summarize_month(anchor, lambda _: WorkDay(), as_of=BASE)
    assert month.first_day == anchor.replace(day=1)
    assert month.last_day == anchor.replace(day=count)
    assert len(month.days) == count
    assert [day.work_date for day in month.days] == [month.first_day + timedelta(days=i) for i in range(count)]
    assert all(day.net == day.flat_break == day.pause == timedelta() for day in month.days)
    assert month.total_net == timedelta() and month.incomplete_dates == ()


def test_month_reuses_daily_semantics_options_corrections_and_incomplete_days(tmp_path):
    with SQLiteEventStore(tmp_path / 'month.db') as store:
        for delta in range(4):
            work_date = DAY + timedelta(days=delta)
            stamp = BASE + timedelta(days=delta)
            store.append_event(work_date, WorkEvent(EventType.WORK_START, stamp))
            if delta != 3:
                store.append_event(work_date, WorkEvent(EventType.PAUSE_START, stamp + timedelta(hours=1)))
                store.append_event(work_date, WorkEvent(EventType.PAUSE_END, stamp + timedelta(hours=1, minutes=30)))
                store.append_event(work_date, WorkEvent(EventType.WORK_END, stamp + timedelta(hours=8)))
        store.set_flat_break_enabled(DAY + timedelta(days=1), False)
        stamp = BASE + timedelta(days=2)
        store.save_correction(DAY + timedelta(days=2), (WorkEvent(EventType.WORK_START, stamp),
                                                       WorkEvent(EventType.WORK_END, stamp + timedelta(hours=9))))
        as_of = BASE + timedelta(days=10)
        month = summarize_month(DAY, store.load_day, as_of=as_of,
                                load_flat_break_enabled=store.get_flat_break_enabled)
        assert month.days == tuple(summarize_day(day.work_date, store.load_day(day.work_date), as_of=as_of,
                                  flat_break_enabled=store.get_flat_break_enabled(day.work_date)) for day in month.days)
    assert month.days[0].net == timedelta(hours=7, minutes=15)
    assert month.days[0].pause == timedelta(minutes=30)
    assert month.days[1].net == timedelta(hours=7, minutes=30)
    assert month.days[1].flat_break == timedelta()
    assert month.days[2].net == timedelta(hours=8, minutes=45)
    assert month.days[3].net is None and month.days[3].incomplete
    assert month.total_net == timedelta(hours=23, minutes=30)
    assert month.incomplete_dates == (DAY + timedelta(days=3),)


def test_month_uses_one_supplied_as_of_and_current_running_day(monkeypatch):
    import worktime.summary as module
    from unittest.mock import Mock
    original = module.summarize_day
    tracked = Mock(wraps=original)
    monkeypatch.setattr(module, 'summarize_day', tracked)
    day = day_with((EventType.WORK_START, 0))
    as_of = BASE + timedelta(hours=2)
    month = summarize_month(DAY, lambda work_date: day if work_date == DAY else WorkDay(), as_of=as_of)
    assert month.days[0].net == timedelta(hours=1, minutes=45)
    assert not month.days[0].incomplete
    assert len(tracked.call_args_list) == 31
    assert all(call.kwargs['as_of'] is as_of for call in tracked.call_args_list)


def test_range_includes_date_max_without_overflow():
    summaries = summarize_date_range(date.max, date.max, lambda _: WorkDay(), as_of=BASE)
    assert len(summaries) == 1 and summaries[0].work_date == date.max
