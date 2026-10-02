from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import runpy
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from worktime.app import WorkTimeApp
from worktime.domain import DomainError, EventType, WorkState
from worktime.storage import SQLiteEventStore
from worktime.tray import (
    TrayAction, TrayController, create_image, create_menu, menu_state, run_tray,
)
import worktime.tray as tray_module
from worktime.windows_session import dispatch_session_event


MENU_CASES = [
    (WorkState.NOT_STARTED, "Noch nicht gestartet", (TrayAction.START, TrayAction.DAY, TrayAction.WEEK, TrayAction.MONTH, TrayAction.QUIT)),
    (WorkState.WORKING, "Arbeit läuft", (TrayAction.PAUSE, TrayAction.FINISH, TrayAction.DAY, TrayAction.WEEK, TrayAction.MONTH, TrayAction.QUIT)),
    (WorkState.PAUSED, "Pause", (TrayAction.RESUME, TrayAction.FINISH, TrayAction.DAY, TrayAction.WEEK, TrayAction.MONTH, TrayAction.QUIT)),
    (WorkState.FINISHED, "Feierabend", (TrayAction.DAY, TrayAction.WEEK, TrayAction.MONTH, TrayAction.QUIT)),
]


def fake_app(state=WorkState.WORKING):
    app = Mock(spec=WorkTimeApp)
    app.status.return_value = SimpleNamespace(day=SimpleNamespace(state=state))
    return app


class FakeSessionListener:
    def __init__(self, callback):
        self.callback = callback
        self.start = Mock()
        self.stop = Mock()

    def emit(self, code):
        dispatch_session_event(code, self.callback)


@pytest.fixture(autouse=True)
def session_listeners(monkeypatch):
    """Existing and new tray tests must never register on the real desktop."""
    created = []

    def factory(callback):
        listener = FakeSessionListener(callback)
        created.append(listener)
        return listener

    monkeypatch.setattr(tray_module, "WindowsSessionListener", factory)
    return created


@pytest.mark.parametrize("state,label,actions", MENU_CASES)
def test_pure_menu_state_exact_actions_without_desktop(state, label, actions):
    result = menu_state(state)
    assert result.label == label
    assert result.actions == actions
    assert TrayAction.QUIT in result.actions


@pytest.mark.parametrize("state,label,actions", MENU_CASES)
def test_real_pystray_descriptors_match_state_without_icon(state, label, actions):
    app = fake_app(state)
    controller = TrayController(app)
    items = tuple(create_menu(menu_state(state), controller))
    assert items[0].text == label
    assert not items[0].enabled
    assert [item.text for item in items[1:]] == [action.value for action in actions]
    assert all(item.visible and item.enabled for item in items[1:])
    items[0](Mock())
    assert app.mock_calls == []


@pytest.mark.parametrize("action,method", [
    (TrayAction.START, "start_work"),
    (TrayAction.PAUSE, "pause_work"),
    (TrayAction.RESUME, "resume_work"),
    (TrayAction.FINISH, "finish_work"),
])
def test_callback_routes_only_to_corresponding_use_case(action, method):
    app = fake_app()
    icon = Mock()
    controller = TrayController(app)
    # Construct a descriptor for the requested action without a native icon.
    view = SimpleNamespace(label="Status", actions=(action,))
    item = tuple(create_menu(view, controller))[1]
    item(icon)
    assert app.mock_calls == [getattr(call, method)(), call.status()]
    icon.stop.assert_not_called()
    icon.notify.assert_not_called()


@pytest.mark.parametrize("state,label,actions", MENU_CASES)
def test_quit_callback_only_closes_application(state, label, actions):
    app = fake_app(state)
    icon = Mock()
    controller = TrayController(app)
    quit_item = tuple(create_menu(menu_state(state), controller))[-1]
    quit_item(icon)
    assert app.mock_calls == [call.close()]
    icon.stop.assert_called_once_with()
    controller.close(icon)
    controller.perform(TrayAction.FINISH, icon)
    assert app.mock_calls == [call.close()]
    assert controller.view().actions == ()


@pytest.mark.parametrize("error", [DomainError("nicht erlaubt"), OSError("Speichern fehlgeschlagen")])
def test_action_error_is_reported_without_quitting(error, caplog):
    app = fake_app()
    app.pause_work.side_effect = error
    icon = Mock()
    controller = TrayController(app)
    controller.perform(TrayAction.PAUSE, icon)
    app.pause_work.assert_called_once_with()
    app.close.assert_not_called()
    icon.stop.assert_not_called()
    icon.notify.assert_called_once()
    assert str(error) in icon.notify.call_args.args[0]
    assert "fehlgeschlagen" in caplog.text
    assert tuple(icon.menu)[0].text == "Arbeit läuft"


def test_notification_failure_is_logged_and_does_not_escape(caplog):
    app = fake_app()
    app.pause_work.side_effect = DomainError("nicht erlaubt")
    icon = Mock()
    icon.notify.side_effect = OSError("Benachrichtigung nicht verfuegbar")
    TrayController(app).perform(TrayAction.PAUSE, icon)
    assert "Fehlerbenachrichtigung" in caplog.text
    app.close.assert_not_called()


def test_status_failure_leaves_quit_available(caplog):
    app = fake_app()
    app.status.side_effect = OSError("Lesefehler")
    view = TrayController(app).view()
    assert view.label == "Status nicht verfügbar"
    assert view.actions == (TrayAction.DAY, TrayAction.WEEK, TrayAction.MONTH, TrayAction.QUIT)
    assert "Status konnte nicht gelesen" in caplog.text


def test_real_app_quit_leaves_running_day_unchanged(tmp_path):
    path = tmp_path / "worktime.db"
    now = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    app = WorkTimeApp(path, clock=lambda: now)
    app.start_work()
    before = path.read_bytes()
    TrayController(app).perform(TrayAction.QUIT, Mock())
    assert path.read_bytes() == before
    with SQLiteEventStore(path) as store:
        day = store.load_day(now.date())
        assert day.state is WorkState.WORKING
        assert [event.kind for event in day.events] == [EventType.WORK_START]


def test_real_app_finish_keeps_tray_alive_and_hides_start(tmp_path):
    path = tmp_path / "worktime.db"
    now = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    app = WorkTimeApp(path, clock=lambda: now)
    icon = Mock()
    controller = TrayController(app)
    try:
        controller.perform(TrayAction.START, icon)
        controller.perform(TrayAction.FINISH, icon)
        assert app.status().day.state is WorkState.FINISHED
        assert [item.text for item in tuple(icon.menu)] == ["Feierabend", "Tagesübersicht", "Wochenübersicht", "Monatsübersicht", "Anwendung beenden"]
        icon.stop.assert_not_called()
        before = path.read_bytes()
        controller.perform(TrayAction.START, icon)
        assert path.read_bytes() == before
        icon.notify.assert_called_once()
        assert controller.view().actions == (TrayAction.DAY, TrayAction.WEEK, TrayAction.MONTH, TrayAction.QUIT)
    finally:
        controller.close(icon)


def test_refresh_detects_next_local_day_without_auto_start(tmp_path):
    now = datetime(2026, 10, 1, 23, tzinfo=timezone.utc)
    app = WorkTimeApp(tmp_path / "worktime.db", clock=lambda: now)
    icon = Mock()
    controller = TrayController(app)
    try:
        app.start_work()
        app.finish_work()
        controller.refresh(icon)
        assert [item.text for item in tuple(icon.menu)] == ["Feierabend", "Tagesübersicht", "Wochenübersicht", "Monatsübersicht", "Anwendung beenden"]
        now += timedelta(hours=2)
        controller.refresh(icon)
        assert [item.text for item in tuple(icon.menu)] == ["Noch nicht gestartet", "Arbeit starten", "Tagesübersicht", "Wochenübersicht", "Monatsübersicht", "Anwendung beenden"]
        assert app.status().day.events == ()
    finally:
        controller.close(icon)


def test_setup_refresh_loop_stops_without_real_wait_or_desktop():
    app = fake_app()
    controller = TrayController(app)
    stopped = Mock()
    stopped.wait.side_effect = [False, True]
    stopped.is_set.return_value = False
    controller._stopped = stopped
    icon = Mock()
    controller.setup(icon)
    assert icon.visible is True
    assert stopped.wait.call_args_list == [call(1.0), call(1.0)]
    assert tuple(icon.menu)[0].text == "Arbeit läuft"
    assert app.mock_calls == [call.status()]


def test_icon_is_generated_with_pillow():
    image = create_image()
    try:
        assert image.size == (64, 64)
        assert image.mode == "RGBA"
        assert image.getbbox() is not None
        assert len(image.getcolors()) > 1
    finally:
        image.close()


@pytest.mark.parametrize("fails", [False, True])
def test_run_path_closes_resources_without_native_tray(monkeypatch, fails):
    import pystray

    app = fake_app()
    icon = Mock()
    if fails:
        icon.run.side_effect = RuntimeError("Tray-Start fehlgeschlagen")
    factory = Mock(return_value=icon)
    monkeypatch.setattr(pystray, "Icon", factory)
    if fails:
        with pytest.raises(RuntimeError, match="Tray-Start"):
            run_tray(app)
    else:
        run_tray(app)
    assert factory.call_args.args[0] == "WorkTime"
    assert callable(icon.run.call_args.kwargs["setup"])
    app.close.assert_called_once_with()
    icon.stop.assert_called_once_with()
    assert [c for c in app.mock_calls if c != call.status()] == [call.auto_start_if_needed(), call.close()]


def test_icon_construction_failure_closes_app(monkeypatch):
    import pystray

    app = fake_app()
    monkeypatch.setattr(pystray, "Icon", Mock(side_effect=OSError("kein Tray")))
    with pytest.raises(OSError, match="kein Tray"):
        run_tray(app)
    app.close.assert_called_once_with()


def test_entrypoint_only_delegates_to_main(monkeypatch):
    main = Mock()
    monkeypatch.setattr(tray_module, "main", main)
    runpy.run_module("worktime", run_name="__main__")
    main.assert_called_once_with()


def test_main_uses_default_factory_and_tray_runner(monkeypatch):
    app = fake_app()
    factory = Mock(return_value=app)
    runner = Mock()
    guard = Mock()
    guard.__enter__ = Mock(return_value=True)
    guard.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(tray_module, "SingleInstance", Mock(return_value=guard))
    monkeypatch.setattr(tray_module, "create_default_app", factory)
    monkeypatch.setattr(tray_module, "run_tray", runner)
    tray_module.main()
    factory.assert_called_once_with()
    runner.assert_called_once_with(app)
    guard.__exit__.assert_called_once()


def test_startup_auto_start_precedes_initial_menu_and_listener(tmp_path, monkeypatch, session_listeners):
    import pystray

    now = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    path = tmp_path / "startup.db"
    app = WorkTimeApp(path, clock=lambda: now)
    icon = Mock()

    def create_icon(name, image, title, menu):
        assert [item.text for item in tuple(menu)] == ["Arbeit läuft", "Pause", "Feierabend", "Tagesübersicht", "Wochenübersicht", "Monatsübersicht", "Anwendung beenden"]
        assert app.status().day.state is WorkState.WORKING
        assert not session_listeners
        return icon

    monkeypatch.setattr(pystray, "Icon", create_icon)
    run_tray(app)
    session_listeners[0].start.assert_called_once_with()
    session_listeners[0].stop.assert_called_once_with()
    with SQLiteEventStore(path) as store:
        assert [event.kind for event in store.load_day(now.date()).events] == [EventType.WORK_START]


@pytest.mark.parametrize("code", [5, 8])
def test_logon_unlock_delegate_once_and_refresh(code, session_listeners):
    app = fake_app()
    icon = Mock()
    controller = TrayController(app)
    controller.start_listener(icon)
    try:
        session_listeners[0].emit(code)
        assert app.mock_calls == [call.auto_start_if_needed(), call.status()]
        assert [item.text for item in tuple(icon.menu)] == ["Arbeit läuft", "Pause", "Feierabend", "Tagesübersicht", "Wochenübersicht", "Monatsübersicht", "Anwendung beenden"]
    finally:
        controller.close(icon)


@pytest.mark.parametrize("code", [1, 2, 3, 4, 6, 7, 9, 15])
def test_lock_and_other_sessions_do_not_call_app_or_refresh(code, session_listeners):
    app = fake_app()
    icon = Mock()
    controller = TrayController(app)
    controller.start_listener(icon)
    try:
        session_listeners[0].emit(code)
        assert app.mock_calls == []
        assert icon.mock_calls == []
        assert "menu" not in icon.__dict__
    finally:
        controller.close(icon)


def test_lock_unlock_keep_continuous_work_without_pause(tmp_path, session_listeners):
    now = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    path = tmp_path / "continuous.db"
    app = WorkTimeApp(path, clock=lambda: now)
    controller = TrayController(app)
    icon = Mock()
    controller.initialize()
    controller.start_listener(icon)
    try:
        now = now.replace(hour=9)
        before = path.read_bytes()
        session_listeners[0].emit(7)
        assert path.read_bytes() == before
        now = now.replace(hour=10, minute=30)
        session_listeners[0].emit(8)
        day = app.status().day
        assert [event.kind for event in day.events] == [EventType.WORK_START]
        assert day.calculate(now).net == timedelta(hours=2, minutes=30)
        assert day.calculate(now).pause == timedelta()
        assert path.read_bytes() == before
    finally:
        controller.close(icon)


def test_next_day_refresh_is_manual_fallback_until_unlock(tmp_path, session_listeners):
    now = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    app = WorkTimeApp(tmp_path / "next.db", clock=lambda: now)
    controller = TrayController(app)
    icon = Mock()
    controller.initialize()
    app.finish_work()
    controller.start_listener(icon)
    try:
        session_listeners[0].emit(8)
        assert controller.view().actions == (TrayAction.DAY, TrayAction.WEEK, TrayAction.MONTH, TrayAction.QUIT)
        now += timedelta(days=1)
        controller.refresh(icon)
        assert app.status().day.events == ()
        assert controller.view().actions == (TrayAction.START, TrayAction.DAY, TrayAction.WEEK, TrayAction.MONTH, TrayAction.QUIT)
        session_listeners[0].emit(8)
        assert app.status().day.state is WorkState.WORKING
        assert len(app.status().day.events) == 1
        assert [item.text for item in tuple(icon.menu)][1:] == ["Pause", "Feierabend", "Tagesübersicht", "Wochenübersicht", "Monatsübersicht", "Anwendung beenden"]
    finally:
        controller.close(icon)


def test_close_stops_listener_before_tray_and_app_without_lock_deadlock(session_listeners):
    app = fake_app()
    icon = Mock()
    controller = TrayController(app)
    controller.start_listener(icon)
    order = []

    def stop_listener():
        order.append("listener")
        # A callback already queued at shutdown must be able to take the lock,
        # see _closed and return while close waits for its thread.
        thread = Thread(target=session_listeners[0].callback, daemon=True)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive(), "callback was blocked by close's lock"

    session_listeners[0].stop.side_effect = stop_listener
    icon.stop.side_effect = lambda: order.append("tray")
    app.close.side_effect = lambda: order.append("app")
    controller.close(icon)
    controller.close(icon)
    assert order == ["listener", "tray", "app"]
    assert app.mock_calls == [call.close()]


def test_manual_action_and_session_callback_are_serialized(session_listeners):
    app = fake_app()
    icon = Mock()
    controller = TrayController(app)
    controller.start_listener(icon)
    entered, release, callback_entered = Event(), Event(), Event()

    def pause():
        entered.set()
        assert release.wait(2)

    def unlock():
        callback_entered.set()
        session_listeners[0].emit(8)

    app.pause_work.side_effect = pause
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            manual = pool.submit(controller.perform, TrayAction.PAUSE, icon)
            assert entered.wait(2)
            automatic = pool.submit(unlock)
            try:
                assert callback_entered.wait(2)
                app.auto_start_if_needed.assert_not_called()
            finally:
                release.set()
            manual.result(timeout=2)
            automatic.result(timeout=2)
        assert app.mock_calls == [call.pause_work(), call.status(), call.auto_start_if_needed(), call.status()]
    finally:
        controller.close(icon)


def test_startup_auto_failure_closes_app_before_icon_creation(monkeypatch, session_listeners):
    import pystray

    app = fake_app()
    app.auto_start_if_needed.side_effect = DomainError("invalid clock")
    factory = Mock()
    monkeypatch.setattr(pystray, "Icon", factory)
    with pytest.raises(DomainError, match="invalid clock"):
        run_tray(app)
    factory.assert_not_called()
    assert not session_listeners
    app.close.assert_called_once_with()


def test_listener_start_failure_cleans_tray_and_keeps_valid_start(tmp_path, monkeypatch):
    import pystray

    path = tmp_path / "failure.db"
    now = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    app = WorkTimeApp(path, clock=lambda: now)
    listener = FakeSessionListener(Mock())
    listener.start.side_effect = OSError("WTS unavailable")
    monkeypatch.setattr(tray_module, "WindowsSessionListener", lambda cb: listener)
    icon = Mock()
    monkeypatch.setattr(pystray, "Icon", Mock(return_value=icon))
    with pytest.raises(OSError, match="WTS unavailable"):
        run_tray(app)
    listener.stop.assert_called_once_with()
    icon.stop.assert_called_once_with()
    icon.run.assert_not_called()
    with pytest.raises(RuntimeError, match="geschlossen"):
        app.status()
    with SQLiteEventStore(path) as store:
        assert [event.kind for event in store.load_day(now.date()).events] == [EventType.WORK_START]


def test_session_auto_failure_is_reported_without_closing_or_extra_action(session_listeners):
    app = fake_app()
    app.auto_start_if_needed.side_effect = DomainError("invalid clock")
    controller = TrayController(app)
    icon = Mock()
    controller.start_listener(icon)
    try:
        session_listeners[0].emit(8)
        icon.notify.assert_called_once()
        icon.stop.assert_not_called()
        assert app.mock_calls == [call.auto_start_if_needed(), call.status()]
    finally:
        controller.close(icon)


def test_listener_stop_failure_still_closes_tray_and_app(session_listeners):
    app = fake_app()
    controller = TrayController(app)
    icon = Mock()
    controller.start_listener(icon)
    session_listeners[0].stop.side_effect = RuntimeError("listener failed")
    with pytest.raises(RuntimeError, match="listener failed"):
        controller.close(icon)
    icon.stop.assert_called_once_with()
    assert app.mock_calls == [call.close()]


@pytest.fixture(autouse=True)
def ui_services(monkeypatch):
    services = []
    def factory(app):
        service = Mock()
        services.append(service)
        return service
    monkeypatch.setattr(tray_module, "UIService", factory)
    return services


@pytest.mark.parametrize("action,method", [(TrayAction.DAY, "open_day"), (TrayAction.WEEK, "open_week"),
                                          (TrayAction.MONTH, "open_month")])
def test_overview_actions_only_open_ui(action, method, ui_services):
    app = fake_app()
    controller = TrayController(app)
    icon = Mock()
    controller.perform(action, icon)
    assert ui_services[0].mock_calls == [getattr(call, method)()]
    assert app.mock_calls == []
    icon.stop.assert_not_called()


def test_quit_stops_ui_then_session_then_tray_then_app(ui_services, session_listeners):
    app, icon = fake_app(), Mock()
    controller = TrayController(app)
    controller.start_listener(icon)
    order = []
    ui_services[0].stop.side_effect = lambda: order.append("ui")
    session_listeners[0].stop.side_effect = lambda: order.append("session")
    icon.stop.side_effect = lambda: order.append("tray")
    app.close.side_effect = lambda: order.append("app")
    controller.close(icon)
    assert order == ["ui", "session", "tray", "app"]
    assert app.mock_calls == [call.close()]


def test_ui_stop_failure_still_closes_other_resources(ui_services, session_listeners):
    app, icon = fake_app(), Mock()
    controller = TrayController(app)
    controller.start_listener(icon)
    ui_services[0].stop.side_effect = RuntimeError("UI shutdown failed")
    with pytest.raises(RuntimeError, match="UI shutdown"):
        controller.close(icon)
    session_listeners[0].stop.assert_called_once_with()
    icon.stop.assert_called_once_with()
    app.close.assert_called_once_with()


@pytest.mark.parametrize("failure", [None, "factory", "runner"])
def test_single_instance_wraps_startup_and_shutdown(monkeypatch, failure):
    from contextlib import contextmanager
    order = []
    @contextmanager
    def guard():
        order.append("mutex acquired")
        try:
            yield True
        finally:
            order.append("mutex released")
    def factory():
        order.append("app")
        if failure == "factory":
            raise OSError("factory")
        return fake_app()
    def runner(app):
        order.append("tray")
        if failure == "runner":
            raise OSError("runner")
    monkeypatch.setattr(tray_module, "SingleInstance", guard)
    monkeypatch.setattr(tray_module, "create_default_app", factory)
    monkeypatch.setattr(tray_module, "run_tray", runner)
    if failure:
        with pytest.raises(OSError, match=failure):
            tray_module.main()
    else:
        tray_module.main()
    assert order == ["mutex acquired", "app"] + ([] if failure == "factory" else ["tray"]) + ["mutex released"]


def test_second_instance_does_not_initialize_app_or_modify_database(tmp_path, monkeypatch):
    from contextlib import contextmanager
    path = tmp_path / "existing.db"
    now = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    app = WorkTimeApp(path, clock=lambda: now)
    app.start_work()
    app.close()
    before = path.read_bytes()
    @contextmanager
    def busy():
        yield False
    factory, runner = Mock(), Mock()
    monkeypatch.setattr(tray_module, "SingleInstance", busy)
    monkeypatch.setattr(tray_module, "create_default_app", factory)
    monkeypatch.setattr(tray_module, "run_tray", runner)
    tray_module.main()
    factory.assert_not_called()
    runner.assert_not_called()
    assert path.read_bytes() == before


def test_mutex_error_never_initializes_app(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(tray_module, "SingleInstance", Mock(side_effect=OSError("mutex")))
    monkeypatch.setattr(tray_module, "create_default_app", factory)
    with pytest.raises(OSError, match="mutex"):
        tray_module.main()
    factory.assert_not_called()
