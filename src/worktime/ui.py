"""Tk views on one dedicated owner thread; tray callbacks only enqueue requests."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import gc
from queue import Empty, Queue
from threading import Event, RLock, Thread, current_thread

from worktime.correction import (
    DayEdit, PauseEdit, edit_from_summary, events_from_edit, parse_time, windows_local_time,
)
from worktime.domain import DomainError, WorkState
from worktime.export import (
    CSV_HEADER, STATE_LABELS as LABELS, INCOMPLETE_HINT, day_row,
    duration_text as format_duration, parse_date_range, suggested_filename, write_csv,
)


MONTH_NAMES = ('Januar', 'Februar', 'März', 'April', 'Mai', 'Juni', 'Juli',
               'August', 'September', 'Oktober', 'November', 'Dezember')


def duration_text(value: timedelta | None) -> str:
    return format_duration(value, missing='-')


def stamp_text(value: datetime | None) -> str:
    return value.isoformat(sep=" ", timespec="minutes") if value else "-"


@dataclass(frozen=True)
class DayView:
    fields: tuple[tuple[str, str], ...]
    pauses: tuple[str, ...]


def day_view(summary) -> DayView:
    return DayView((
        ("Datum", summary.work_date.isoformat()),
        ("Arbeitsbeginn", stamp_text(summary.start)),
        ("Arbeitsende", stamp_text(summary.end)),
        ("Brutto", duration_text(summary.gross)),
        ("Pause", duration_text(summary.pause)),
        ("Davon automatische Mittagspause", duration_text(summary.lunch)),
        ("Pauschalpause", duration_text(summary.flat_break)),
        ("Netto", duration_text(summary.net)),
        ("Zustand", LABELS[summary.state]),
        ("Hinweis", INCOMPLETE_HINT if summary.incomplete else ""),
    ), tuple(f"{stamp_text(p.start)} – {stamp_text(p.end)}" for p in summary.pauses))


@dataclass(frozen=True)
class WeekView:
    title: str
    rows: tuple[tuple[str, str, str], ...]
    total: str


@dataclass(frozen=True)
class MonthView:
    title: str
    rows: tuple[tuple[str, ...], ...]
    total: str


def month_view(summary) -> MonthView:
    return MonthView(f"{MONTH_NAMES[summary.first_day.month - 1]} {summary.first_day.year}",
                     tuple(day_row(day, missing='-') for day in summary.days),
                     duration_text(summary.total_net))


def shift_month(anchor: date, delta: int) -> date:
    year, month = divmod(anchor.year * 12 + anchor.month - 1 + delta, 12)
    return date(year, month + 1, 1)


def week_view(summary) -> WeekView:
    return WeekView(f"{summary.monday.isoformat()} bis {summary.sunday.isoformat()}",
                    tuple((day.work_date.isoformat(), duration_text(day.net),
                           "Unvollständig" if day.incomplete else "") for day in summary.days),
                    duration_text(summary.total_net))


@dataclass(frozen=True)
class TimeInput:
    day: str
    time: str


@dataclass(frozen=True)
class EditorInput:
    start: TimeInput
    end: TimeInput
    pauses: tuple[tuple[TimeInput, TimeInput], ...] = ()
    auto_lunch_minutes: str | None = None


def editor_events(values: EditorInput, *, allow_open_pause=False, resolver=windows_local_time):
    def parse(value, optional=False):
        if not value.time.strip() and optional:
            return None
        try:
            day = date.fromisoformat(value.day)
        except ValueError as error:
            raise DomainError("Datum bitte als JJJJ-MM-TT eingeben.") from error
        return parse_time(day, value.time.strip(), resolver=resolver)

    edit = DayEdit(parse(values.start), parse(values.end, True),
                   tuple(PauseEdit(parse(start), parse(end, True)) for start, end in values.pauses))
    return events_from_edit(edit, allow_open_pause=allow_open_pause)


class EditorController:
    def __init__(self, app, work_date, *, allow_open_pause=False, resolver=windows_local_time):
        self.app, self.work_date = app, work_date
        self.allow_open_pause, self.resolver = allow_open_pause, resolver

    def save(self, values: EditorInput):
        events = editor_events(values, allow_open_pause=self.allow_open_pause, resolver=self.resolver)
        if values.auto_lunch_minutes is not None:
            text = values.auto_lunch_minutes.strip()
            if not text or not text.isascii() or not text.isdecimal():
                raise DomainError("Mittagspause bitte als nichtnegative ganze Minuten eingeben.")
            return self.app.correct_day(self.work_date, events, auto_lunch_minutes=int(text))
        return self.app.correct_day(self.work_date, events)


class TkWindows:
    """Created, called and destroyed exclusively by UIService's Tk thread."""
    def __init__(self, root, app):
        import tkinter as tk
        from tkinter import ttk, filedialog
        self.root, self.app, self.tk, self.ttk = root, app, tk, ttk
        self.filedialog = filedialog

    def show_error(self, message):
        window = self.tk.Toplevel(self.root)
        window.title("WorkTime – Fehler")
        self.ttk.Label(window, text=message, wraplength=600).pack(padx=16, pady=16)
        self.ttk.Button(window, text="Schließen", command=window.destroy).pack(pady=8)

    def open_day(self):
        self._day_window(self.app.today())

    def _day_window(self, selected):
        window = self.tk.Toplevel(self.root)
        window.title("WorkTime – Tagesübersicht")
        content = self.ttk.Frame(window, padding=12)
        content.pack(fill="both", expand=True)
        current = [selected]

        def render():
            try:
                summary = self.app.day_summary(current[0])
                view = day_view(summary)
                enabled = self.app.flat_break_enabled(current[0])
            except Exception as error:
                self.show_error(str(error))
                return
            for child in content.winfo_children():
                child.destroy()
            navigation = self.ttk.Frame(content)
            navigation.pack(fill="x")
            for text, step in (("Vortag", -1), ("Heute", 0), ("Folgetag", 1)):
                def move(delta=step):
                    current[0] = (current[0] + timedelta(days=delta)) if delta else self.app.today()
                    render()
                self.ttk.Button(navigation, text=text, command=move).pack(side="left")
            option = self.tk.BooleanVar(master=window, value=enabled)

            def toggle():
                try:
                    self.app.set_flat_break_enabled(current[0], option.get())
                except Exception as error:
                    self.show_error(str(error))
                render()

            self.ttk.Checkbutton(content, text="15 min Pauschalpause abziehen",
                                 variable=option, command=toggle).pack(anchor="w", pady=4)
            for label, value in view.fields:
                self.ttk.Label(content, text=f"{label}: {value}").pack(anchor="w", pady=2)
            for index, pause in enumerate(view.pauses, 1):
                self.ttk.Label(content, text=f"Pause {index}: {pause}").pack(anchor="w")
            self.ttk.Button(content, text="Bearbeiten",
                            command=lambda: self._editor(summary, render, window)).pack(pady=8)
            self.ttk.Button(content, text="Aktualisieren", command=render).pack()
        render()

    def open_week(self):
        window = self.tk.Toplevel(self.root)
        window.title("WorkTime – Wochenübersicht")
        content = self.ttk.Frame(window, padding=12)
        content.pack(fill="both", expand=True)
        anchor = [self.app.today()]

        def render():
            try:
                view = week_view(self.app.week_summary(anchor[0]))
            except Exception as error:
                self.show_error(str(error))
                return
            for child in content.winfo_children():
                child.destroy()
            navigation = self.ttk.Frame(content)
            navigation.pack()
            for text, step in (("Vorwoche", -7), ("Diese Woche", 0), ("Folgewoche", 7)):
                def move(delta=step):
                    anchor[0] = anchor[0] + timedelta(days=delta) if delta else self.app.today()
                    render()
                self.ttk.Button(navigation, text=text, command=move).pack(side="left")
            self.ttk.Label(content, text=view.title).pack(pady=8)
            table = self.ttk.Treeview(content, columns=("date", "net", "note"), show="headings", height=7)
            for key, title in (("date", "Datum"), ("net", "Netto"), ("note", "Hinweis")):
                table.heading(key, text=title)
            for row in view.rows:
                table.insert("", "end", values=row)
            table.pack(fill="both", expand=True)
            self.ttk.Label(content, text=f"Gesamt Netto: {view.total}").pack(pady=8)
            self.ttk.Button(content, text="Aktualisieren", command=render).pack()
        render()

    def open_month(self):
        window = self.tk.Toplevel(self.root)
        window.geometry("1150x650")
        content = self.ttk.Frame(window, padding=12)
        content.pack(fill="both", expand=True)
        anchor = [self.app.today()]

        def render():
            try:
                summary = self.app.month_summary(anchor[0])
                view = month_view(summary)
            except Exception as error:
                self.show_error(str(error))
                return
            window.title(view.title)
            for child in content.winfo_children():
                child.destroy()
            navigation = self.ttk.Frame(content)
            navigation.pack()
            for text, step in (("Vormonat", -1), ("Dieser Monat", 0), ("Folgemonat", 1)):
                def move(delta=step):
                    try:
                        anchor[0] = shift_month(anchor[0], delta) if delta else self.app.today()
                    except Exception as error:
                        self.show_error(str(error))
                        return
                    render()
                self.ttk.Button(navigation, text=text, command=move).pack(side="left")
            area = self.ttk.Frame(content)
            area.pack(fill="both", expand=True, pady=8)
            table = self.ttk.Treeview(area, columns=CSV_HEADER, show="headings", height=16)
            vertical = self.ttk.Scrollbar(area, orient="vertical", command=table.yview)
            vertical.pack(side="right", fill="y")
            horizontal = self.ttk.Scrollbar(content, orient="horizontal", command=table.xview)
            horizontal.pack(fill="x")
            table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
            for key in CSV_HEADER:
                table.heading(key, text=key)
            for row in view.rows:
                table.insert("", "end", values=row)
            table.pack(side="left", fill="both", expand=True)
            self.ttk.Label(content, text=f"Gesamt Netto: {view.total}").pack(pady=8)
            self.ttk.Button(content, text="Aktualisieren", command=render).pack()
            self.ttk.Button(content, text="CSV exportieren",
                            command=lambda: self._export_dialog(summary, window)).pack(pady=8)
        render()

    def _export_dialog(self, summary, parent):
        window = self.tk.Toplevel(parent)
        window.title("WorkTime – CSV exportieren")
        frame = self.ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        self.ttk.Label(frame, text="Datumsformat: JJJJ-MM-TT (Zeitraum einschließlich Start und Ende)").pack()
        variables = []
        for label, value in (("Startdatum", summary.first_day), ("Enddatum", summary.last_day)):
            row = self.ttk.Frame(frame)
            row.pack(fill="x", pady=4)
            self.ttk.Label(row, text=label, width=12).pack(side="left")
            variable = self.tk.StringVar(master=window, value=value.isoformat())
            self.ttk.Entry(row, textvariable=variable, width=12).pack(side="left")
            variables.append(variable)

        def save():
            try:
                start, end = parse_date_range(*(value.get() for value in variables))
                path = self.filedialog.asksaveasfilename(
                    parent=window, title="WorkTime – CSV speichern", defaultextension=".csv",
                    filetypes=(("CSV-Dateien", "*.csv"), ("Alle Dateien", "*.*")),
                    initialfile=suggested_filename(start, end))
                if not path:
                    return
                days = self.app.date_range_summaries(start, end)
                write_csv(path, days)
                window.destroy()
            except Exception as error:
                self.show_error(str(error))

        self.ttk.Button(frame, text="Exportieren", command=save).pack(side="left", pady=8)
        self.ttk.Button(frame, text="Abbrechen", command=window.destroy).pack(side="left", pady=8)

    def _editor(self, summary, refreshed, parent):
        window = self.tk.Toplevel(parent)
        window.title("WorkTime – Tag korrigieren")
        window.geometry("780x500")
        canvas = self.tk.Canvas(window, highlightthickness=0)
        scrollbar = self.ttk.Scrollbar(window, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        frame = self.ttk.Frame(canvas, padding=12)
        canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        self.ttk.Label(frame, text="Datum JJJJ-MM-TT / Zeit HH:MM; Ende darf leer sein.").pack()
        self.ttk.Label(frame, text="Für Zeiten nach Mitternacht das Datum ausdrücklich ändern.").pack()
        edit = edit_from_summary(summary.start, summary.end, summary.pauses)

        def entry(parent, stamp):
            pair = self.ttk.Frame(parent)
            pair.pack(side="left", padx=4)
            day = self.tk.StringVar(master=window, value=(stamp.date() if stamp else summary.work_date).isoformat())
            clock = self.tk.StringVar(master=window, value=stamp.strftime("%H:%M") if stamp else "")
            self.ttk.Entry(pair, textvariable=day, width=12).pack(side="left")
            self.ttk.Entry(pair, textvariable=clock, width=7).pack(side="left")
            return day, clock

        def field(label, stamp):
            row = self.ttk.Frame(frame)
            row.pack(anchor="w", pady=3)
            self.ttk.Label(row, text=label, width=14).pack(side="left")
            return entry(row, stamp)

        start, end = field("Arbeitsbeginn", edit.start), field("Arbeitsende", edit.end)
        lunch_value = None
        if summary.auto_lunch_minutes is not None:
            row = self.ttk.Frame(frame)
            row.pack(anchor="w", pady=3)
            self.ttk.Label(row, text="Automatische Mittagspause (Minuten)").pack(side="left")
            lunch_value = self.tk.StringVar(master=window, value=str(summary.auto_lunch_minutes))
            self.ttk.Entry(row, textvariable=lunch_value, width=7).pack(side="left")
            self.ttk.Label(frame, text=("Standard: 30 Minuten (12:00–12:30). "
                "Nach einer manuellen Pause zählen nur die realen Pausen. "
                "Die 15-Minuten-Pauschalpause bleibt zusätzlich.")).pack()
        rows = []
        pause_area = self.ttk.Frame(frame)
        pause_area.pack()

        def add_pause(pause=None):
            row = self.ttk.Frame(pause_area)
            row.pack(pady=3)
            self.ttk.Label(row, text="Pause").pack(side="left")
            pair = (entry(row, pause.start if pause else None), entry(row, pause.end if pause else None))
            rows.append(pair)

            def remove():
                rows.remove(pair)
                row.destroy()
            self.ttk.Button(row, text="Pause entfernen", command=remove).pack(side="left")
        for pause in edit.pauses:
            add_pause(pause)
        self.ttk.Button(frame, text="Pause hinzufügen", command=add_pause).pack()
        error_text = self.tk.StringVar(master=window)
        self.ttk.Label(frame, textvariable=error_text, foreground="red", wraplength=650).pack()
        controller = EditorController(self.app, summary.work_date,
                                      allow_open_pause=summary.state is WorkState.PAUSED)

        def value(pair):
            return TimeInput(pair[0].get(), pair[1].get())

        def save():
            try:
                controller.save(EditorInput(value(start), value(end),
                                            tuple((value(a), value(b)) for a, b in rows),
                                            lunch_value.get() if lunch_value is not None else None))
            except Exception as error:
                error_text.set(str(error))
                return
            window.destroy()
            refreshed()

        self.ttk.Button(frame, text="Speichern", command=save).pack(side="left")
        self.ttk.Button(frame, text="Abbrechen", command=window.destroy).pack(side="right")


class UIService:
    """Lazy hidden Tk root; no Tk references cross the owner-thread boundary."""
    def __init__(self, app, *, root_factory=None, windows_factory=TkWindows):
        self._app = app
        self._root_factory, self._windows_factory = root_factory, windows_factory
        self._queue = Queue()
        self._lock = RLock()
        self._ready, self._stop = Event(), Event()
        self._thread = None
        self._failure = None
        self._closed = False

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Oberfläche ist geschlossen.")
            if self._thread is None:
                self._thread = Thread(target=self._run, name="WorkTime-Tk")
                self._thread.start()
        self._ready.wait()
        if self._failure is not None:
            self.stop()
            raise RuntimeError(self._failure)

    def _open(self, command):
        self.start()
        with self._lock:
            if self._closed:
                return
            self._queue.put(command)

    def open_day(self):
        self._open("day")

    def open_week(self):
        self._open("week")

    def open_month(self):
        self._open("month")

    def stop(self):
        with self._lock:
            self._closed = True
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join()

    def _run(self):
        root = windows = None
        try:
            if self._root_factory is None:
                from tkinter import Tk
                root = Tk()
            else:
                root = self._root_factory()
            root.withdraw()
            windows = self._windows_factory(root, self._app)

            def poll():
                if self._stop.is_set():
                    root.quit()
                    return
                for _ in range(32):
                    try:
                        command = self._queue.get_nowait()
                    except Empty:
                        break
                    try:
                        if command == "day":
                            windows.open_day()
                        elif command == "week":
                            windows.open_week()
                        elif command == "month":
                            windows.open_month()
                    except Exception as error:
                        windows.show_error(str(error))
                root.after(30, poll)

            root.after(0, poll)
            self._ready.set()
            root.mainloop()
            if not self._stop.is_set():
                self._failure = "Die Oberfläche wurde unerwartet beendet."
        except Exception as error:
            # Store only text: a traceback could retain Tk objects on another thread.
            self._failure = str(error)
        finally:
            self._ready.set()
            try:
                if root is not None:
                    root.destroy()
            finally:
                windows = root = None
                gc.collect()
