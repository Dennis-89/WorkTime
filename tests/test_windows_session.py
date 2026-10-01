import ctypes
from ctypes import wintypes
from threading import Event, current_thread
from unittest.mock import Mock, call

import pytest

import worktime.windows_session as session
from worktime.windows_session import (
    WindowsSessionListener, dispatch_session_event,
    WTS_SESSION_LOGON, WTS_SESSION_LOCK, WTS_SESSION_UNLOCK,
)


def test_windows_constants():
    assert session.WM_WTSSESSION_CHANGE == 0x02B1
    assert WTS_SESSION_LOGON == 5
    assert WTS_SESSION_LOCK == 7
    assert WTS_SESSION_UNLOCK == 8
    assert session.NOTIFY_FOR_THIS_SESSION == 0


@pytest.mark.parametrize("code", range(17))
def test_pure_dispatch_calls_only_logon_and_unlock(code):
    callback = Mock()
    dispatch_session_event(code, callback)
    assert callback.call_count == (1 if code in (5, 8) else 0)
    if code in (5, 8):
        callback.assert_called_once_with()


class FakeWindow:
    def __init__(self):
        self.calls = []
        self.dispatch = None

    def open(self, dispatch, stopped):
        self.dispatch = dispatch
        self.calls.append(("open", current_thread()))

    def run(self, stopped):
        self.calls.append(("run", current_thread()))
        stopped.wait()

    def close(self):
        self.calls.append(("close", current_thread()))


def test_listener_idempotent_start_stop_and_restart_joins_owned_thread():
    created = []

    def factory():
        window = FakeWindow()
        created.append(window)
        return window

    callback = Mock()
    listener = WindowsSessionListener(callback, _backend_factory=factory)
    listener.stop()
    for _ in range(2):
        listener.start()
        thread = listener._thread
        assert listener.running
        assert not thread.daemon
        listener.start()
        assert listener._thread is thread
        created[-1].dispatch(WTS_SESSION_LOCK)
        callback.assert_not_called()
        created[-1].dispatch(WTS_SESSION_UNLOCK)
        callback.assert_called_once_with()
        listener.stop()
        listener.stop()
        assert not thread.is_alive()
        assert not listener.running
        created[-1].dispatch(WTS_SESSION_LOGON)
        callback.assert_called_once_with()
        callback.reset_mock()
        calls = created[-1].calls
        assert [name for name, owner in calls] == ["open", "run", "close"]
        assert all(owner is thread for name, owner in calls)
        assert thread is not current_thread()
    assert len(created) == 2


def test_stop_requested_inside_callback_never_joins_itself():
    window = FakeWindow()

    def run(stopped):
        window.dispatch(WTS_SESSION_UNLOCK)
        assert stopped.is_set()

    window.run = run
    listener = WindowsSessionListener(lambda: listener.stop(), _backend_factory=lambda: window)
    listener.start()
    thread = listener._thread
    thread.join(timeout=2)
    assert not thread.is_alive()
    listener.stop()
    assert [name for name, owner in window.calls] == ["open", "close"]


def test_start_failure_cleans_partial_backend_and_exposes_error():
    window = FakeWindow()
    window.open = Mock(side_effect=OSError("registration failed"))
    window.close = Mock()
    listener = WindowsSessionListener(Mock(), _backend_factory=lambda: window)
    with pytest.raises(RuntimeError, match="could not start") as caught:
        listener.start()
    assert isinstance(caught.value.__cause__, OSError)
    assert not listener._thread.is_alive()
    window.close.assert_called_once_with()
    with pytest.raises(RuntimeError, match="listener failed"):
        listener.stop()
    listener.stop()


def test_backend_construction_failure_leaves_no_thread():
    listener = WindowsSessionListener(Mock(), _backend_factory=Mock(side_effect=OSError("no API")))
    with pytest.raises(RuntimeError, match="could not start"):
        listener.start()
    assert not listener._thread.is_alive()
    with pytest.raises(RuntimeError):
        listener.stop()
    listener.stop()


def test_message_loop_error_cleans_window_and_is_reported_on_stop():
    fail = Event()
    window = FakeWindow()

    def run(stopped):
        assert fail.wait(2)
        raise OSError("message pump failed")

    window.run = run
    listener = WindowsSessionListener(Mock(), _backend_factory=lambda: window)
    listener.start()
    fail.set()
    listener._thread.join(timeout=2)
    assert not listener._thread.is_alive()
    with pytest.raises(RuntimeError, match="listener failed"):
        listener.stop()
    assert window.calls[-1][0] == "close"


def test_cleanup_failure_still_joins_thread():
    window = FakeWindow()
    window.close = Mock(side_effect=OSError("cleanup failed"))
    listener = WindowsSessionListener(Mock(), _backend_factory=lambda: window)
    listener.start()
    thread = listener._thread
    with pytest.raises(RuntimeError, match="listener failed"):
        listener.stop()
    assert not thread.is_alive()
    listener.stop()


@pytest.fixture
def native(monkeypatch):
    # Only ctypes signatures and callbacks are real; no native window is made.
    user, kernel, wts = Mock(), Mock(), Mock()
    kernel.GetModuleHandleW.return_value = 0x100000001
    user.RegisterClassW.return_value = 1
    user.CreateWindowExW.return_value = 0x100000002
    user.DefWindowProcW.return_value = 42
    user.DestroyWindow.return_value = 1
    user.UnregisterClassW.return_value = 1
    user.MsgWaitForMultipleObjects.return_value = 258
    user.PeekMessageW.return_value = 0
    wts.WTSRegisterSessionNotification.return_value = 1
    wts.WTSUnRegisterSessionNotification.return_value = 1
    libraries = {"user32": user, "kernel32": kernel, "wtsapi32": wts}
    loader = Mock(side_effect=lambda name, **kwargs: libraries[name])
    monkeypatch.setattr(session.ctypes, "WinDLL", loader)
    return session._Win32Window(), user, kernel, wts, loader


def test_native_registration_own_session_pointer_sizes_and_cleanup_order(native):
    window, user, kernel, wts, loader = native
    owner = Mock()
    owner.attach_mock(wts.WTSUnRegisterSessionNotification, "unregister")
    owner.attach_mock(user.DestroyWindow, "destroy")
    owner.attach_mock(user.UnregisterClassW, "class_cleanup")
    window.open(Mock(), Event())
    wts.WTSRegisterSessionNotification.assert_called_once_with(0x100000002, 0)
    assert user.CreateWindowExW.call_args.args[3] == 0  # no WS_VISIBLE
    assert user.CreateWindowExW.restype is wintypes.HWND
    assert user.DefWindowProcW.restype is ctypes.c_ssize_t
    assert window._proc_type._restype_ is ctypes.c_ssize_t
    assert ctypes.sizeof(wintypes.WPARAM) == ctypes.sizeof(ctypes.c_void_p)
    window.close()
    window.close()
    assert owner.mock_calls == [
        call.unregister(0x100000002), call.destroy(0x100000002),
        call.class_cleanup(window._name, 0x100000001),
    ]
    assert loader.call_args_list == [
        call("user32", use_last_error=True), call("kernel32", use_last_error=True),
        call("wtsapi32", use_last_error=True),
    ]


@pytest.mark.parametrize("code", [WTS_SESSION_LOGON, WTS_SESSION_LOCK, WTS_SESSION_UNLOCK, 6])
def test_native_message_bridge_uses_pure_dispatch(native, code):
    window, user, kernel, wts, loader = native
    callback = Mock()
    stopped = Event()
    window.open(lambda c: dispatch_session_event(c, callback), stopped)
    try:
        assert window._wndproc(0x100000002, session.WM_WTSSESSION_CHANGE, code, 1) == 0
        assert callback.call_count == (1 if code in (5, 8) else 0)
        stopped.set()
        window._wndproc(0x100000002, session.WM_WTSSESSION_CHANGE, code, 1)
        assert callback.call_count == (1 if code in (5, 8) else 0)
    finally:
        window.close()


def test_unrelated_window_messages_use_default_proc_and_close_sets_stop(native):
    window, user, kernel, wts, loader = native
    stopped = Event()
    dispatch = Mock()
    window.open(dispatch, stopped)
    try:
        assert window._wndproc(12, 0x0401, 0, 0) == 42
        user.DefWindowProcW.assert_called_once_with(12, 0x0401, 0, 0)
        window._wndproc(12, session.WM_CLOSE, 0, 0)
        assert stopped.is_set()
        dispatch.assert_not_called()
    finally:
        window.close()


def test_callback_exception_does_not_cross_ctypes_boundary(native, caplog):
    window, user, kernel, wts, loader = native
    window.open(Mock(side_effect=RuntimeError("callback error")), Event())
    try:
        assert window._wndproc(12, session.WM_WTSSESSION_CHANGE, 8, 1) == 0
        assert "callback failed" in caplog.text
    finally:
        window.close()


@pytest.mark.parametrize("failure", ["module", "class", "window", "registration"])
def test_partial_native_initialization_is_cleaned(native, failure):
    window, user, kernel, wts, loader = native
    functions = {"module": kernel.GetModuleHandleW, "class": user.RegisterClassW,
                 "window": user.CreateWindowExW, "registration": wts.WTSRegisterSessionNotification}
    functions[failure].return_value = 0
    try:
        with pytest.raises(OSError):
            window.open(Mock(), Event())
    finally:
        window.close()
    wts.WTSUnRegisterSessionNotification.assert_not_called()
    assert user.DestroyWindow.call_count == (1 if failure == "registration" else 0)
    assert user.UnregisterClassW.call_count == (1 if failure in ("window", "registration") else 0)


def test_native_cleanup_attempts_all_resources_even_on_deregister_failure(native):
    window, user, kernel, wts, loader = native
    window.open(Mock(), Event())
    wts.WTSUnRegisterSessionNotification.return_value = 0
    with pytest.raises(OSError):
        window.close()
    user.DestroyWindow.assert_called_once()
    user.UnregisterClassW.assert_called_once()


def test_native_message_loop_handles_quit(native):
    window, user, kernel, wts, loader = native
    stopped = Event()
    callback = Mock()
    window.open(lambda c: dispatch_session_event(c, callback), stopped)
    messages = [session.WM_WTSSESSION_CHANGE, session.WM_QUIT]

    def peek(pointer, *args):
        pointer._obj.message = messages.pop(0)
        pointer._obj.wParam = WTS_SESSION_UNLOCK
        pointer._obj.lParam = 1
        return 1

    def dispatch(pointer):
        m = pointer._obj
        return window._wndproc(12, m.message, m.wParam, m.lParam)

    user.PeekMessageW.side_effect = peek
    user.DispatchMessageW.side_effect = dispatch
    try:
        window.run(stopped)
        callback.assert_called_once_with()
        assert stopped.is_set()
        assert not messages
    finally:
        window.close()


def test_native_stop_does_not_require_posted_message(native):
    window, user, kernel, wts, loader = native
    stopped = Event()
    window.open(Mock(), stopped)

    def wait(*args):
        stopped.set()
        return 258

    user.MsgWaitForMultipleObjects.side_effect = wait
    try:
        window.run(stopped)
        user.MsgWaitForMultipleObjects.assert_called_once_with(0, None, False, 100, 0x04FF)
        user.PeekMessageW.assert_not_called()
    finally:
        window.close()


def test_native_wait_failure_propagates(native):
    window, user, kernel, wts, loader = native
    window.open(Mock(), Event())
    user.MsgWaitForMultipleObjects.return_value = 0xFFFFFFFF
    try:
        with pytest.raises(OSError):
            window.run(Event())
    finally:
        window.close()
