import csv
from datetime import date, datetime, timedelta, timezone
import io

import pytest

from worktime.app import WorkTimeApp
from worktime.domain import DomainError, EventType, WorkEvent
from worktime.export import CSV_HEADER, parse_date_range, suggested_filename, write_csv

BASE = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)


@pytest.mark.parametrize('start,end', [(date(2026, 10, 1), date(2026, 10, 1)),
                                      (date(2026, 10, 1), date(2026, 10, 31)),
                                      (date(2026, 10, 15), date(2026, 11, 10)),
                                      (date(2026, 12, 20), date(2027, 1, 10)),
                                      (date(2028, 2, 1), date(2028, 2, 29))])
def test_csv_exact_header_encoding_and_every_inclusive_calendar_day(tmp_path, start, end):
    path = tmp_path / 'csv.db'
    app = WorkTimeApp(path, clock=lambda: BASE)
    try:
        before = path.read_bytes()
        output = tmp_path / 'chosen.csv'
        write_csv(output, app.date_range_summaries(start, end))
        data = output.read_bytes()
        assert data.startswith(b'\xef\xbb\xbf') and data.count(b'\xef\xbb\xbf') == 1
        text = data.decode('utf-8-sig')
        assert text.splitlines()[0] == 'Datum;Arbeitsbeginn;Arbeitsende;Pause;Pauschalpause;Netto;Zustand;Hinweis'
        rows = list(csv.reader(io.StringIO(text), delimiter=';'))
        assert rows[0] == list(CSV_HEADER)
        count = (end-start).days + 1
        assert len(rows) == count + 1
        for i, row in enumerate(rows[1:]):
            day = start + timedelta(days=i)
            assert row == [day.strftime('%d.%m.%Y'), '', '', '00:00', '00:00', '00:00', 'Noch nicht gestartet', '']
        assert path.read_bytes() == before
    finally:
        app.close()


def test_csv_real_pause_options_corrections_and_incomplete_history(tmp_path):
    db = tmp_path / 'values.db'
    now = BASE + timedelta(days=3, hours=2)
    app = WorkTimeApp(db, clock=lambda: now)
    try:
        for delta in range(4):
            stamp = BASE + timedelta(days=delta)
            events = [WorkEvent(EventType.WORK_START, stamp)]
            if delta < 2:
                events += [WorkEvent(EventType.PAUSE_START, stamp+timedelta(hours=1)),
                           WorkEvent(EventType.PAUSE_END, stamp+timedelta(hours=1, minutes=30)),
                           WorkEvent(EventType.WORK_END, stamp+timedelta(hours=8))]
            app.correct_day(stamp.date(), events)
        app.set_flat_break_enabled((BASE+timedelta(days=1)).date(), False)
        before = db.read_bytes()
        output = tmp_path / 'values.csv'
        write_csv(output, app.date_range_summaries(BASE.date(), now.date()))
        with output.open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.reader(handle, delimiter=';'))
        assert rows[1] == ['01.10.2026','08:00','16:00','00:30','00:15','07:15','Feierabend','']
        assert rows[2] == ['02.10.2026','08:00','16:00','00:30','00:00','07:30','Feierabend','']
        assert rows[3] == ['03.10.2026','08:00','','','00:15','','Arbeit läuft','Unvollständig – Korrektur erforderlich']
        assert rows[4] == ['04.10.2026','08:00','','00:00','00:15','01:45','Arbeit läuft','']
        assert db.read_bytes() == before
    finally:
        app.close()


def test_csv_finished_midnight_and_offset_change_uses_summary_times(tmp_path):
    start = datetime(2026, 10, 24, 23, tzinfo=timezone(timedelta(hours=2)))
    end = datetime(2026, 10, 25, 4, tzinfo=timezone(timedelta(hours=1)))
    app = WorkTimeApp(tmp_path / 'offset.db', clock=lambda: end)
    try:
        app.correct_day(start.date(), (WorkEvent(EventType.WORK_START, start), WorkEvent(EventType.WORK_END, end)))
        output = tmp_path / 'offset.csv'
        write_csv(output, app.date_range_summaries(start.date(), start.date()))
        assert '24.10.2026;23:00;04:00;00:00;00:15;05:45;Feierabend;' in output.read_text(encoding='utf-8-sig')
    finally:
        app.close()


@pytest.mark.parametrize('start,end,label', [('bad','2026-10-31','Startdatum'),
                                           ('2026-02-30','2026-10-31','Startdatum'),
                                           ('20261001','2026-10-31','Startdatum'),
                                           ('2026-W40-4','2026-10-31','Startdatum'),
                                           ('2026-10-01','bad','Enddatum'),
                                           ('2026-10-01','2026-11-31','Enddatum'),
                                           ('2026-11-01','2026-10-31','Startdatum')])
def test_export_dates_reject_invalid_input_without_correction(start, end, label):
    with pytest.raises(DomainError, match=label):
        parse_date_range(start,end)


def test_export_date_parsing_and_suggested_name():
    start, end = parse_date_range('2026-10-01','2026-10-31')
    assert (start,end)==(date(2026,10,1),date(2026,10,31))
    assert suggested_filename(start,end)=='worktime_2026-10-01_bis_2026-10-31.csv'
