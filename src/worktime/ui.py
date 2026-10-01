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


LABELS = {
    WorkState.NOT_STARTED: "Noch nicht gestartet",
    WorkState.WORKING: "Arbeit läuft",
    WorkState.PAUSED: "Pause",
    WorkState.FINISHED: "Feierabend",
}


def duration_text(value: timedelta | None) -> str:
    if value is None:
        return "-"
    minutes = int(value.total_seconds() // 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


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
        ("Netto", duration_text(summary.net)),
        ("Zustand", LABELS[summary.state]),
        ("Hinweis", "Unvollständig – Korrektur erforderlich" if summary.incomplete else ""),
    ), tuple(f"{stamp_text(p.start)} – {stamp_text(p.end)}" for p in summary.pauses))


@dataclass(frozen=True)
class WeekView:
    title: str
    rows: tuple[tuple[str, str, str], ...]
    total: str


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
        return self.app.correct_day(self.work_date, events)


class TkWindows:
    """Created, called and destroyed exclusively by UIService's Tk thread."""
    def __init__(self, root, app):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.app, self.tk, self.ttk = root, app, tk, ttk

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
                                            tuple((value(a), value(b)) for a, b in rows)))
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
