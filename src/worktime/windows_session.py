"""Technical WTS session notifications; no workday or persistence logic."""

from collections.abc import Callable
import ctypes
from ctypes import wintypes
import logging
import sys
from threading import Event, Lock, Thread, current_thread
from uuid import uuid4


logger = logging.getLogger(__name__)
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOGON = 0x5
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_QUIT = 0x0012


def dispatch_session_event(code: int, callback: Callable[[], None]) -> None:
    """Only logon/unlock request an application check; lock is a no-op."""
    if code in (WTS_SESSION_LOGON, WTS_SESSION_UNLOCK):
        callback()


class _Win32Window:
    """All creation, dispatch and destruction occur on the listener thread."""

    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Windows session notifications require Windows.")
        self._user = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self._proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM,
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT), ("lpfnWndProc", self._proc_type),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
            ]

        self._class_type = WNDCLASSW
        signatures = [
            (self._kernel.GetModuleHandleW, [wintypes.LPCWSTR], wintypes.HMODULE),
            (self._user.RegisterClassW, [ctypes.POINTER(WNDCLASSW)], wintypes.ATOM),
            (self._user.CreateWindowExW, [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
            ], wintypes.HWND),
            (self._user.DefWindowProcW, [wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            (self._user.PeekMessageW, [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                     wintypes.UINT, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            (self._user.TranslateMessage, [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
            (self._user.DispatchMessageW, [ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t),
            (self._user.MsgWaitForMultipleObjects, [
                wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                wintypes.DWORD, wintypes.DWORD,
            ], wintypes.DWORD),
            (self._user.DestroyWindow, [wintypes.HWND], wintypes.BOOL),
            (self._user.UnregisterClassW, [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
            (self._wts.WTSRegisterSessionNotification, [wintypes.HWND, wintypes.DWORD], wintypes.BOOL),
            (self._wts.WTSUnRegisterSessionNotification, [wintypes.HWND], wintypes.BOOL),
        ]
        for function, arguments, result in signatures:
            function.argtypes = arguments
            function.restype = result
        self._name = "WorkTime.Session." + uuid4().hex
        self._instance = None
        self._atom = None
        self._window = None
        self._registered = False
        self._wndproc = None

    @staticmethod
    def _check(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def open(self, dispatch: Callable[[int], None], stopped: Event) -> None:
        def window_proc(hwnd, message, wparam, lparam):
            if message == WM_WTSSESSION_CHANGE:
                if not stopped.is_set():
                    try:
                        dispatch(wparam)
                    except Exception:
                        # Exceptions must never cross a ctypes window callback.
                        logger.exception("Windows session callback failed.")
                return 0
            if message in (WM_CLOSE, WM_DESTROY):
                stopped.set()
                return 0
            return self._user.DefWindowProcW(hwnd, message, wparam, lparam)

        # Keep the callback alive for the entire native window lifetime.
        self._wndproc = self._proc_type(window_proc)
        self._instance = self._check(self._kernel.GetModuleHandleW(None))
        window_class = self._class_type()
        window_class.lpfnWndProc = self._wndproc
        window_class.hInstance = self._instance
        window_class.lpszClassName = self._name
        self._atom = self._check(self._user.RegisterClassW(ctypes.byref(window_class)))
        # Hidden top-level window: no WS_VISIBLE and never call ShowWindow.
        self._window = self._check(self._user.CreateWindowExW(
            0, self._name, "WorkTime session listener", 0,
            0, 0, 0, 0, None, None, self._instance, None,
        ))
        self._check(self._wts.WTSRegisterSessionNotification(
            self._window, NOTIFY_FOR_THIS_SESSION,
        ))
        self._registered = True

    def run(self, stopped: Event) -> None:
        message = wintypes.MSG()
        while not stopped.is_set():
            # Bounded native wait: stop needs no cross-thread window operation
            # or successfully posted message to wake the message loop.
            result = self._user.MsgWaitForMultipleObjects(0, None, False, 100, 0x04FF)
            if result == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            while not stopped.is_set() and self._user.PeekMessageW(
                ctypes.byref(message), None, 0, 0, 1,
            ):
                if message.message == WM_QUIT:
                    stopped.set()
                    break
                self._user.TranslateMessage(ctypes.byref(message))
                self._user.DispatchMessageW(ctypes.byref(message))

    def close(self) -> None:
        errors = []
        for needed, function, arguments in [
            (self._registered, self._wts.WTSUnRegisterSessionNotification, (self._window,)),
            (self._window, self._user.DestroyWindow, (self._window,)),
            (self._atom, self._user.UnregisterClassW, (self._name, self._instance)),
        ]:
            if needed:
                try:
                    self._check(function(*arguments))
                except Exception as error:
                    errors.append(error)
        self._registered = False
        self._window = None
        self._atom = None
        if errors:
            raise errors[0]


class WindowsSessionListener:
    """Own one non-daemon listener thread; repeated start/stop is safe.

    start waits for native registration. stop signals cancellation and joins
    without requiring a window message. Callbacks must return promptly; stop
    from the callback itself only signals cancellation (never self-joins).
    """

    def __init__(self, callback: Callable[[], None], *, _backend_factory=_Win32Window):
        self._callback = callback
        self._backend_factory = _backend_factory
        self._lifecycle = Lock()
        self._stopped = Event()
        self._ready = Event()
        self._thread = None
        self._error = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive()
                    and self._ready.is_set() and not self._stopped.is_set()
                    and self._error is None)

    def _dispatch(self, code: int) -> None:
        if not self._stopped.is_set():
            dispatch_session_event(code, self._callback)

    def _run(self) -> None:
        backend = None
        try:
            backend = self._backend_factory()
            backend.open(self._dispatch, self._stopped)
            self._ready.set()
            backend.run(self._stopped)
        except BaseException as error:
            self._error = error
            logger.exception("Windows session listener failed.")
        finally:
            if backend is not None:
                try:
                    backend.close()
                except BaseException as error:
                    self._error = error
                    logger.exception("Windows session cleanup failed.")
            self._ready.set()

    def _join(self) -> None:
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("Windows session listener did not stop.")

    def start(self) -> None:
        with self._lifecycle:
            if self._thread and self._thread.is_alive():
                return
            self._stopped.clear()
            self._ready.clear()
            self._error = None
            self._thread = Thread(target=self._run, name="WorkTime-WTS", daemon=False)
            self._thread.start()
            if not self._ready.wait(timeout=10):
                self._stopped.set()
                self._join()
                raise RuntimeError("Windows session initialization timed out.")
            if self._error is not None:
                self._join()
                raise RuntimeError("Windows session listener could not start.") from self._error

    def stop(self) -> None:
        self._stopped.set()
        # Do not take a lifecycle lock held by another thread joining us.
        if self._thread is current_thread():
            return
        with self._lifecycle:
            self._stopped.set()
            if self._thread is not None:
                self._join()
                self._thread = None
                if self._error is not None:
                    raise RuntimeError("Windows session listener failed.") from self._error
