"""Serialize existing daily summaries; no GUI, database or time calculation."""
from collections.abc import Iterable
import csv
from datetime import date, timedelta
from pathlib import Path
import re

from worktime.domain import DomainError, WorkState
from worktime.summary import DaySummary, validate_date_range


CSV_HEADER = ('Datum', 'Arbeitsbeginn', 'Arbeitsende', 'Pause', 'Pauschalpause', 'Netto',
              'Zustand', 'Hinweis')
STATE_LABELS = {
    WorkState.NOT_STARTED: "Noch nicht gestartet",
    WorkState.WORKING: "Arbeit läuft",
    WorkState.PAUSED: "Pause",
    WorkState.FINISHED: "Feierabend",
}
INCOMPLETE_HINT = "Unvollständig – Korrektur erforderlich"


def duration_text(value: timedelta | None, *, missing: str = '') -> str:
    if value is None:
        return missing
    minutes = int(value.total_seconds() // 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def day_row(day: DaySummary, *, missing: str = '') -> tuple[str, ...]:
    return (f"{day.work_date.day:02d}.{day.work_date.month:02d}.{day.work_date.year:04d}",
            day.start.strftime('%H:%M') if day.start else missing,
            day.end.strftime('%H:%M') if day.end else missing,
            duration_text(day.pause, missing=missing), duration_text(day.flat_break),
            duration_text(day.net, missing=missing), STATE_LABELS[day.state],
            INCOMPLETE_HINT if day.incomplete else '')


def parse_date_range(start_text: str, end_text: str) -> tuple[date, date]:
    values = []
    for label, text in (('Startdatum', start_text), ('Enddatum', end_text)):
        try:
            if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', text) is None:
                raise ValueError
            values.append(date.fromisoformat(text))
        except ValueError as error:
            raise DomainError(f"{label} bitte als gültiges Kalenderdatum JJJJ-MM-TT eingeben.") from error
    validate_date_range(*values)
    return tuple(values)


def suggested_filename(start_date: date, end_date: date) -> str:
    validate_date_range(start_date, end_date)
    return f"worktime_{start_date.isoformat()}_bis_{end_date.isoformat()}.csv"


def write_csv(path: str | Path, days: Iterable[DaySummary]) -> None:
    with open(path, 'w', encoding='utf-8-sig', newline='') as output:
        writer = csv.writer(output, delimiter=';')
        writer.writerow(CSV_HEADER)
        writer.writerows(day_row(day) for day in days)
