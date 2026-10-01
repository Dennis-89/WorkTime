from datetime import datetime, timedelta, timezone
import runpy
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


MENU_CASES = [
    (WorkState.NOT_STARTED, "Noch nicht gestartet", (TrayAction.START, TrayAction.QUIT)),
    (WorkState.WORKING, "Arbeit läuft", (TrayAction.PAUSE, TrayAction.FINISH, TrayAction.QUIT)),
    (WorkState.PAUSED, "Pause", (TrayAction.RESUME, TrayAction.FINISH, TrayAction.QUIT)),
    (WorkState.FINISHED, "Feierabend", (TrayAction.QUIT,)),
]


def fake_app(state=WorkState.WORKING):
    app = Mock(spec=WorkTimeApp)
    app.status.return_value = SimpleNamespace(day=SimpleNamespace(state=state))
    return app


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
    assert view.actions == (TrayAction.QUIT,)
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
        assert [item.text for item in tuple(icon.menu)] == ["Feierabend", "Anwendung beenden"]
        icon.stop.assert_not_called()
        before = path.read_bytes()
        controller.perform(TrayAction.START, icon)
        assert path.read_bytes() == before
        icon.notify.assert_called_once()
        assert controller.view().actions == (TrayAction.QUIT,)
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
        assert [item.text for item in tuple(icon.menu)] == ["Feierabend", "Anwendung beenden"]
        now += timedelta(hours=2)
        controller.refresh(icon)
        assert [item.text for item in tuple(icon.menu)] == ["Noch nicht gestartet", "Arbeit starten", "Anwendung beenden"]
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
    assert [c for c in app.mock_calls if c != call.status()] == [call.close()]


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
    monkeypatch.setattr(tray_module, "create_default_app", factory)
    monkeypatch.setattr(tray_module, "run_tray", runner)
    tray_module.main()
    factory.assert_called_once_with()
    runner.assert_called_once_with(app)
