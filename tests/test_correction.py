from datetime import date, datetime, timedelta, timezone

import pytest

from worktime.correction import DayEdit, PauseEdit, edit_from_day, events_from_edit, parse_time, resolve_candidates
from worktime.domain import DomainError, EventType, WorkDay, WorkEvent

BASE = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


@pytest.mark.parametrize("end,open_pause", [(False, False), (False, True), (True, False)])
def test_edit_roundtrip_working_paused_finished(end, open_pause):
    day = WorkDay()
    items = [(EventType.WORK_START, 0)]
    if open_pause or end:
        items.append((EventType.PAUSE_START, 60))
    if end:
        items += [(EventType.PAUSE_END, 90), (EventType.WORK_END, 480)]
    for kind, minutes in items:
        day.apply(WorkEvent(kind, BASE + timedelta(minutes=minutes)))
    edit = edit_from_day(day)
    assert edit.start == BASE
    assert events_from_edit(edit, allow_open_pause=open_pause) == day.events


def test_empty_day_edit_and_end_during_pause():
    assert edit_from_day(WorkDay()) == DayEdit(None)
    day = WorkDay()
    for kind, minutes in [(EventType.WORK_START, 0), (EventType.PAUSE_START, 60), (EventType.WORK_END, 120)]:
        day.apply(WorkEvent(kind, BASE + timedelta(minutes=minutes)))
    edit = edit_from_day(day)
    assert edit.pauses[0].end == edit.end
    normalized = WorkDay()
    for event in events_from_edit(edit):
        normalized.apply(event)
    assert normalized.calculate() == day.calculate()


@pytest.mark.parametrize("edit", [
    DayEdit(None),
    DayEdit(BASE, BASE - timedelta(minutes=1)),
    DayEdit(BASE, BASE + timedelta(hours=8), (PauseEdit(BASE - timedelta(hours=1), BASE),)),
    DayEdit(BASE, BASE + timedelta(hours=1), (PauseEdit(BASE, BASE + timedelta(hours=2)),)),
    DayEdit(BASE, None, (PauseEdit(BASE, BASE + timedelta(hours=2)), PauseEdit(BASE + timedelta(hours=1), BASE + timedelta(hours=3)))),
    DayEdit(BASE, None, (PauseEdit(BASE, None),)),
    DayEdit(BASE.replace(tzinfo=None)),
])
def test_invalid_edit_rejected(edit):
    with pytest.raises(DomainError):
        events_from_edit(edit)


def test_only_last_pause_of_paused_day_may_be_open():
    good = DayEdit(BASE, None, (PauseEdit(BASE + timedelta(hours=1), None),))
    assert events_from_edit(good, allow_open_pause=True)[-1].kind is EventType.PAUSE_START
    for bad in (
        DayEdit(BASE, BASE + timedelta(hours=8), good.pauses),
        DayEdit(BASE, None, good.pauses + good.pauses),
    ):
        with pytest.raises(DomainError):
            events_from_edit(bad, allow_open_pause=True)


@pytest.mark.parametrize("text", ["8:00", "24:00", "12:60", "", "08:00:01", "abc"])
def test_invalid_hhmm_rejected(text):
    with pytest.raises(DomainError):
        parse_time(BASE.date(), text, resolver=lambda wall: wall.replace(tzinfo=timezone.utc))


def test_parser_uses_explicit_date_and_aware_resolver():
    seen = []
    def resolver(wall):
        seen.append(wall)
        return wall.replace(tzinfo=timezone(timedelta(hours=2)))
    result = parse_time(date(2005, 7, 1), "08:15", resolver=resolver)
    assert seen == [datetime(2005, 7, 1, 8, 15)]
    assert result.isoformat() == "2005-07-01T08:15:00+02:00"
    with pytest.raises(DomainError):
        parse_time(BASE.date(), "08:00", resolver=lambda wall: wall)


def test_dst_fold_and_gap_are_rejected_and_unique_time_retains_offset():
    wall = datetime(2026, 10, 25, 2, 30)
    offsets = (timedelta(hours=1), timedelta(hours=2))
    with pytest.raises(DomainError, match="mehrdeutig"):
        resolve_candidates(wall, offsets, lambda utc: wall)
    with pytest.raises(DomainError, match="ungültig"):
        resolve_candidates(wall, offsets, lambda utc: wall + timedelta(hours=1))
    result = resolve_candidates(wall, offsets, lambda utc: utc.replace(tzinfo=None) + timedelta(hours=1))
    assert result.utcoffset() == timedelta(hours=1)
    assert resolve_candidates(wall, (offsets[0], offsets[0]), lambda utc: wall) == result


def test_cross_midnight_edit_uses_explicit_dates():
    start = BASE.replace(hour=23)
    end = (BASE + timedelta(days=1)).replace(hour=2)
    events = events_from_edit(DayEdit(start, end))
    day = WorkDay()
    for event in events:
        day.apply(event)
    assert day.calculate().net == timedelta(hours=3)



@pytest.mark.parametrize("failure", [None, "zone", "year", "convert"])
def test_windows_api_bindings_use_historical_year_and_roundtrip(monkeypatch, failure):
    import ctypes
    import sys
    from types import SimpleNamespace
    from worktime.correction import windows_local_time

    years = []
    class Function:
        def __init__(self, function):
            self.function = function
        def __call__(self, *args):
            return self.function(*args)

    def read_zone(pointer):
        return 0xFFFFFFFF if failure == "zone" else 0

    def read_year(year, zone, result):
        years.append(year)
        result._obj.Bias = -60
        result._obj.StandardBias = 0
        result._obj.DaylightBias = -60
        return failure != "year"

    def convert(zone, source, result):
        utc = source._obj
        local = datetime(utc.year, utc.month, utc.day, utc.hour, utc.minute, utc.second) + timedelta(hours=1)
        target = result._obj
        for key in ("year", "month", "day", "hour", "minute", "second"):
            setattr(target, key, getattr(local, key))
        return failure != "convert"

    api = SimpleNamespace(
        GetDynamicTimeZoneInformation=Function(read_zone),
        GetTimeZoneInformationForYear=Function(read_year),
        SystemTimeToTzSpecificLocalTimeEx=Function(convert),
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api, raising=False)
    wall = datetime(2005, 1, 15, 8, 30)
    if failure:
        with pytest.raises(DomainError):
            windows_local_time(wall)
    else:
        stamp = windows_local_time(wall)
        assert stamp.isoformat() == "2005-01-15T08:30:00+01:00"
        assert years == [2005]
        assert len(api.SystemTimeToTzSpecificLocalTimeEx.argtypes) == 3
