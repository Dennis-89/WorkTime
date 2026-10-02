"""Tray presentation and callbacks; all workday use cases live in app.py."""

from dataclasses import dataclass
from enum import Enum
import logging
from threading import Event, RLock

from worktime.app import WorkTimeApp, create_default_app
from worktime.domain import WorkState
from worktime.windows_session import WindowsSessionListener
from worktime.ui import UIService
from worktime.single_instance import SingleInstance


logger = logging.getLogger(__name__)


class TrayAction(Enum):
    START = "Arbeit starten"
    PAUSE = "Pause"
    RESUME = "Weiter"
    FINISH = "Feierabend"
    DAY = "Tagesübersicht"
    WEEK = "Wochenübersicht"
    MONTH = "Monatsübersicht"
    QUIT = "Anwendung beenden"


@dataclass(frozen=True)
class MenuState:
    label: str
    actions: tuple[TrayAction, ...]


def menu_state(state: WorkState) -> MenuState:
    """Pure presentation mapping; no desktop, clock or database access."""
    view = {
        WorkState.NOT_STARTED: MenuState("Noch nicht gestartet", (TrayAction.START, TrayAction.QUIT)),
        WorkState.WORKING: MenuState("Arbeit läuft", (TrayAction.PAUSE, TrayAction.FINISH, TrayAction.QUIT)),
        WorkState.PAUSED: MenuState("Pause", (TrayAction.RESUME, TrayAction.FINISH, TrayAction.QUIT)),
        WorkState.FINISHED: MenuState("Feierabend", (TrayAction.QUIT,)),
    }[state]
    return MenuState(view.label, view.actions[:-1] + (TrayAction.DAY, TrayAction.WEEK, TrayAction.MONTH, TrayAction.QUIT))


def create_image():
    """Generate a small neutral clock symbol without an external image."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    drawing = ImageDraw.Draw(image)
    drawing.ellipse((4, 4, 60, 60), fill="#374151", outline="white", width=3)
    drawing.line((32, 15, 32, 32, 45, 32), fill="white", width=4)
    return image


def create_menu(view: MenuState, controller):
    """Build native-menu descriptors without starting an icon or event loop."""
    from pystray import Menu, MenuItem

    def callback(action):
        def invoke(icon, item):
            controller.perform(action, icon)
        return invoke

    return Menu(
        MenuItem(view.label, None, enabled=False),
        *(MenuItem(action.value, callback(action)) for action in view.actions),
    )


class TrayController:
    def __init__(self, app: WorkTimeApp):
        self._app = app
        self._stopped = Event()
        self._lock = RLock()
        self._view = None
        self._closed = False
        self._listener = None
        self._ui = UIService(app)

    def initialize(self) -> None:
        """Run once before constructing the initial native tray menu."""
        with self._lock:
            self._app.auto_start_if_needed()

    def start_listener(self, icon) -> None:
        self._listener = WindowsSessionListener(lambda: self.session_auto_start(icon))
        self._listener.start()

    def session_auto_start(self, icon) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._app.auto_start_if_needed()
            except Exception as error:
                logger.exception("Automatischer WorkTime-Start fehlgeschlagen.")
                try:
                    icon.notify(f"Automatischer Start nicht möglich: {error}", "WorkTime")
                except Exception:
                    logger.exception("WorkTime-Fehlerbenachrichtigung fehlgeschlagen.")
            self.refresh(icon)

    def view(self) -> MenuState:
        if self._closed:
            return MenuState("WorkTime beendet", ())
        try:
            return menu_state(self._app.status().day.state)
        except Exception:
            logger.exception("WorkTime-Status konnte nicht gelesen werden.")
            return MenuState("Status nicht verfügbar", (TrayAction.DAY, TrayAction.WEEK, TrayAction.MONTH, TrayAction.QUIT))

    def refresh(self, icon) -> None:
        with self._lock:
            view = self.view()
            if not self._stopped.is_set() and view != self._view:
                icon.menu = create_menu(view, self)
                self._view = view

    def setup(self, icon) -> None:
        """pystray runs setup on a worker thread; refresh after date changes."""
        icon.visible = True
        while not self._stopped.wait(1.0):
            self.refresh(icon)

    def perform(self, action: TrayAction, icon) -> None:
        if self._closed:
            return
        if action is TrayAction.QUIT:
            self.close(icon)
            return
        with self._lock:
            if self._closed:
                return
            try:
                if action in (TrayAction.DAY, TrayAction.WEEK, TrayAction.MONTH):
                    {TrayAction.DAY: self._ui.open_day, TrayAction.WEEK: self._ui.open_week,
                     TrayAction.MONTH: self._ui.open_month}[action]()
                    return
                {
                    TrayAction.START: self._app.start_work,
                    TrayAction.PAUSE: self._app.pause_work,
                    TrayAction.RESUME: self._app.resume_work,
                    TrayAction.FINISH: self._app.finish_work,
                }[action]()
            except Exception as error:
                # UI boundary: preserve the process and make rejected actions visible.
                logger.exception("WorkTime-Aktion fehlgeschlagen: %s", action.value)
                try:
                    icon.notify(f"Aktion nicht möglich: {error}", "WorkTime")
                except Exception:
                    logger.exception("WorkTime-Fehlerbenachrichtigung fehlgeschlagen.")
            self.refresh(icon)

    def close(self, icon=None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stopped.set()
        # Never join a listener/setup thread while holding its callback lock.
        try:
            self._ui.stop()
        finally:
            try:
                if self._listener is not None:
                    self._listener.stop()
            finally:
                try:
                    if icon is not None:
                        icon.stop()
                finally:
                    self._app.close()


def run_tray(app: WorkTimeApp) -> None:
    controller = TrayController(app)
    icon = None
    try:
        from pystray import Icon

        controller.initialize()
        icon = Icon("WorkTime", create_image(), "WorkTime", create_menu(controller.view(), controller))
        controller.start_listener(icon)
        icon.run(setup=controller.setup)
    finally:
        controller.close(icon)


def main() -> None:
    # Acquire before creating even the data directory or the first connection.
    with SingleInstance() as acquired:
        if not acquired:
            return
        run_tray(create_default_app())
