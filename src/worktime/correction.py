"""Editable day data and strict local Windows time conversion, without GUI."""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re

from worktime.domain import DomainError, EventType, WorkDay, WorkEvent
from worktime.summary import PauseSummary


@dataclass(frozen=True)
class PauseEdit:
    start: datetime
    end: datetime | None


@dataclass(frozen=True)
class DayEdit:
    start: datetime | None
    end: datetime | None = None
    pauses: tuple[PauseEdit, ...] = ()


def edit_from_day(day: WorkDay) -> DayEdit:
    start = end = opened = None
    pauses = []
    for event in day.events:
        if event.kind is EventType.WORK_START:
            start = event.timestamp
        elif event.kind is EventType.PAUSE_START:
            opened = event.timestamp
        elif event.kind in (EventType.PAUSE_END, EventType.WORK_END):
            if opened is not None:
                pauses.append(PauseEdit(opened, event.timestamp))
                opened = None
            if event.kind is EventType.WORK_END:
                end = event.timestamp
    if opened is not None:
        pauses.append(PauseEdit(opened, None))
    return DayEdit(start, end, tuple(pauses))


def edit_from_summary(start: datetime | None, end: datetime | None,
                      pauses: tuple[PauseSummary, ...]) -> DayEdit:
    return DayEdit(start, end, tuple(PauseEdit(p.start, p.end) for p in pauses))


def events_from_edit(edit: DayEdit, *, allow_open_pause: bool = False) -> tuple[WorkEvent, ...]:
    if edit.start is None:
        raise DomainError("Arbeitsbeginn fehlt.")
    events = [WorkEvent(EventType.WORK_START, edit.start)]
    for index, pause in enumerate(edit.pauses):
        events.append(WorkEvent(EventType.PAUSE_START, pause.start))
        if pause.end is None:
            if not allow_open_pause or edit.end is not None or index != len(edit.pauses) - 1:
                raise DomainError("Nur ein pausierter Tag erlaubt eine offene letzte Pause.")
        else:
            events.append(WorkEvent(EventType.PAUSE_END, pause.end))
    if edit.end is not None:
        events.append(WorkEvent(EventType.WORK_END, edit.end))
    day = WorkDay()
    for event in events:
        day.apply(event)
    return day.events


def resolve_candidates(wall: datetime, offsets: tuple[timedelta, ...],
                       to_local: Callable[[datetime], datetime]) -> datetime:
    """Reject gaps and folds by round-tripping each possible UTC offset."""
    matches = {}
    for offset in offsets:
        candidate = wall.replace(tzinfo=timezone(offset))
        utc = candidate.astimezone(timezone.utc)
        if to_local(utc).replace(tzinfo=None) == wall:
            matches[utc] = candidate
    if len(matches) != 1:
        raise DomainError("Lokale Uhrzeit ist ungültig oder mehrdeutig (Zeitumstellung).")
    return next(iter(matches.values()))


def windows_local_time(wall: datetime) -> datetime:
    """Use Windows' historical year rules; never guess a gap/fold offset."""
    import ctypes as c
    from ctypes import wintypes as w
    import sys

    if sys.platform != "win32":
        raise DomainError("Lokale Zeitzonenauflösung benötigt Windows.")

    class SystemTime(c.Structure):
        _fields_ = [(name, w.WORD) for name in
                    ("year", "month", "weekday", "day", "hour", "minute", "second", "millis")]

    fields = [("Bias", w.LONG), ("StandardName", w.WCHAR * 32),
              ("StandardDate", SystemTime), ("StandardBias", w.LONG),
              ("DaylightName", w.WCHAR * 32), ("DaylightDate", SystemTime),
              ("DaylightBias", w.LONG)]

    class TimeZone(c.Structure):
        _fields_ = fields

    class DynamicZone(c.Structure):
        _fields_ = fields + [("TimeZoneKeyName", w.WCHAR * 128),
                            ("DynamicDaylightTimeDisabled", w.BOOLEAN)]

    api = c.WinDLL("kernel32", use_last_error=True)
    api.GetDynamicTimeZoneInformation.argtypes = [c.POINTER(DynamicZone)]
    api.GetDynamicTimeZoneInformation.restype = w.DWORD
    api.GetTimeZoneInformationForYear.argtypes = [w.WORD, c.POINTER(DynamicZone), c.POINTER(TimeZone)]
    api.GetTimeZoneInformationForYear.restype = w.BOOL
    api.SystemTimeToTzSpecificLocalTimeEx.argtypes = [
        c.POINTER(DynamicZone), c.POINTER(SystemTime), c.POINTER(SystemTime)]
    api.SystemTimeToTzSpecificLocalTimeEx.restype = w.BOOL
    dynamic, yearly = DynamicZone(), TimeZone()
    if api.GetDynamicTimeZoneInformation(c.byref(dynamic)) == 0xFFFFFFFF:
        raise DomainError("Windows-Zeitzone konnte nicht gelesen werden.")
    if not api.GetTimeZoneInformationForYear(wall.year, c.byref(dynamic), c.byref(yearly)):
        raise DomainError("Historische Windows-Zeitzone konnte nicht gelesen werden.")

    def to_local(utc):
        source = SystemTime(utc.year, utc.month, 0, utc.day, utc.hour, utc.minute, utc.second, 0)
        target = SystemTime()
        if not api.SystemTimeToTzSpecificLocalTimeEx(c.byref(dynamic), c.byref(source), c.byref(target)):
            raise DomainError("Windows-Zeitumrechnung fehlgeschlagen.")
        return datetime(target.year, target.month, target.day, target.hour, target.minute, target.second)

    offsets = (timedelta(minutes=-yearly.Bias - yearly.StandardBias),
               timedelta(minutes=-yearly.Bias - yearly.DaylightBias))
    return resolve_candidates(wall, offsets, to_local)


def parse_time(day: date, text: str, *,
               resolver: Callable[[datetime], datetime] = windows_local_time) -> datetime:
    if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", text):
        raise DomainError("Zeit bitte als HH:MM eingeben.")
    try:
        hour, minute = map(int, text.split(":"))
        wall = datetime(day.year, day.month, day.day, hour, minute)
    except ValueError as error:
        raise DomainError("Ungültige Uhrzeit.") from error
    result = resolver(wall)
    if result.tzinfo is None or result.utcoffset() is None:
        raise DomainError("Zeitumrechnung lieferte keine Zeitzone.")
    return result
