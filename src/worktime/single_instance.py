"""User-specific Windows named mutex, acquired before any application writes."""
import ctypes as c
from ctypes import wintypes as w
import re
import sys
from threading import RLock


ERROR_ALREADY_EXISTS = 183
ERROR_INSUFFICIENT_BUFFER = 122


class WindowsMutexAPI:
    def __init__(self):
        if sys.platform != "win32":
            raise OSError("WorkTime Single Instance benoetigt Windows.")
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.security = c.WinDLL("advapi32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [c.c_void_p, w.BOOL, w.LPCWSTR]
        self.kernel.CreateMutexW.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL
        self.kernel.GetCurrentProcess.argtypes = []
        self.kernel.GetCurrentProcess.restype = w.HANDLE
        self.kernel.LocalFree.argtypes = [c.c_void_p]
        self.kernel.LocalFree.restype = c.c_void_p
        self.security.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
        self.security.OpenProcessToken.restype = w.BOOL
        self.security.GetTokenInformation.argtypes = [
            w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
        self.security.GetTokenInformation.restype = w.BOOL
        self.security.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
        self.security.ConvertSidToStringSidW.restype = w.BOOL

    def user_sid(self) -> str:
        token = w.HANDLE()
        if not self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008, c.byref(token)):
            raise c.WinError(c.get_last_error())
        try:
            size = w.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, c.byref(size))
            if c.get_last_error() != ERROR_INSUFFICIENT_BUFFER or not size.value:
                raise c.WinError(c.get_last_error())
            buffer = c.create_string_buffer(size.value)
            if not self.security.GetTokenInformation(token, 1, buffer, size, c.byref(size)):
                raise c.WinError(c.get_last_error())
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member is PSID.
            sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
            text = w.LPWSTR()
            if not self.security.ConvertSidToStringSidW(sid, c.byref(text)):
                raise c.WinError(c.get_last_error())
            try:
                return text.value
            finally:
                self.kernel.LocalFree(c.cast(text, c.c_void_p))
        finally:
            self.close(token)

    def create(self, name: str):
        c.set_last_error(0)
        handle = self.kernel.CreateMutexW(None, False, name)
        error = c.get_last_error()
        if not handle:
            raise c.WinError(error)
        return handle, error == ERROR_ALREADY_EXISTS

    def close(self, handle):
        if not self.kernel.CloseHandle(handle):
            raise c.WinError(c.get_last_error())


class SingleInstance:
    """Keep the named kernel object alive; no thread-owned mutex wait is needed.

    Global namespace covers this user's sessions, while the SID separates users.
    This is not a machine-wide lock against other users. Default token ACLs apply.
    The handle is non-inheritable. Windows closes it on process termination.
    """
    def __init__(self, *, api=None, user_sid: str | None = None):
        self._api = WindowsMutexAPI() if api is None else api
        sid = self._api.user_sid() if user_sid is None else user_sid
        if not isinstance(sid, str) or not re.fullmatch(r"S-\d+(?:-\d+)+", sid):
            raise ValueError("Ungueltige Windows-Benutzer-SID.")
        self.name = "Global\\WorkTime.MVP." + sid
        self._handle = None
        self._lock = RLock()

    def acquire(self) -> bool:
        with self._lock:
            if self._handle is not None:
                return True
            handle, exists = self._api.create(self.name)
            if exists:
                self._api.close(handle)
                return False
            self._handle = handle
            return True

    def close(self):
        with self._lock:
            if self._handle is not None:
                self._api.close(self._handle)
                self._handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
