from concurrent.futures import ThreadPoolExecutor
import ctypes as c
from ctypes import wintypes as w
from threading import Lock
from unittest.mock import Mock

import pytest

from worktime.single_instance import SingleInstance, WindowsMutexAPI


SID = "S-1-5-21-100-200-300-1001"


class FakeAPI:
    def __init__(self):
        self.handles = {}
        self.counter = 0
        self.lock = Lock()
    def user_sid(self):
        return SID
    def create(self, name):
        with self.lock:
            exists = name in self.handles.values()
            self.counter += 1
            self.handles[self.counter] = name
            return self.counter, exists
    def close(self, handle):
        with self.lock:
            del self.handles[handle]


def test_first_second_release_and_restart():
    api = FakeAPI()
    first, second = SingleInstance(api=api), SingleInstance(api=api)
    assert first.acquire()
    assert first.acquire()
    assert not second.acquire()
    assert len(api.handles) == 1
    first.close()
    first.close()
    assert second.acquire()
    second.close()
    assert api.handles == {}


def test_different_users_do_not_block_each_other():
    api = FakeAPI()
    with SingleInstance(api=api) as first, SingleInstance(api=api, user_sid=SID + "2") as second:
        assert first and second
        assert len(api.handles) == 2
    assert api.handles == {}


def test_concurrent_starts_have_exactly_one_winner():
    api = FakeAPI()
    instances = [SingleInstance(api=api) for _ in range(16)]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda instance: instance.acquire(), instances))
        assert results.count(True) == 1
        assert len(api.handles) == 1
    finally:
        for instance in instances:
            instance.close()
    assert not api.handles


def test_creation_error_is_not_treated_as_success():
    api = Mock()
    api.create.side_effect = OSError("access denied")
    instance = SingleInstance(api=api, user_sid=SID)
    with pytest.raises(OSError, match="access denied"):
        instance.acquire()
    instance.close()
    api.close.assert_not_called()


def test_exception_and_close_error_paths():
    api = FakeAPI()
    with pytest.raises(RuntimeError):
        with SingleInstance(api=api) as acquired:
            assert acquired
            raise RuntimeError("startup failed")
    assert not api.handles
    backend = Mock()
    backend.create.return_value = (123, False)
    backend.close.side_effect = [OSError("close"), None]
    instance = SingleInstance(api=backend, user_sid=SID)
    assert instance.acquire()
    with pytest.raises(OSError):
        instance.close()
    instance.close()
    assert backend.close.call_count == 2


@pytest.mark.parametrize("sid", ["", "name", "S-1\\bad", None])
def test_invalid_sid_rejected(sid):
    api = Mock()
    api.user_sid.return_value = sid
    with pytest.raises(ValueError):
        SingleInstance(api=api)
    api.create.assert_not_called()


def native_backend(monkeypatch):
    kernel, security = Mock(), Mock()
    monkeypatch.setattr(c, "WinDLL", lambda name, **kwargs: kernel if name == "kernel32" else security)
    return WindowsMutexAPI(), kernel, security


@pytest.mark.parametrize("handle,error,exists", [(123, 0, False), (456, 183, True), (None, 5, None)])
def test_native_mutex_create_checks_last_error_and_noninheritance(monkeypatch, handle, error, exists):
    api, kernel, security = native_backend(monkeypatch)
    def create(attributes, owned, name):
        assert attributes is None and owned is False
        c.set_last_error(error)
        return handle
    kernel.CreateMutexW.side_effect = create
    if handle is None:
        with pytest.raises(OSError):
            api.create("Global\\WorkTime.test")
    else:
        assert api.create("Global\\WorkTime.test") == (handle, exists)
    assert kernel.CreateMutexW.restype is w.HANDLE


@pytest.mark.parametrize("failure", [None, "open", "size", "read", "convert"])
def test_native_sid_query_closes_token_and_frees_sid(monkeypatch, failure):
    api, kernel, security = native_backend(monkeypatch)
    sid_buffer = c.create_unicode_buffer(SID)
    kernel.CloseHandle.return_value = True
    def open_token(process, rights, token):
        assert rights == 8
        token._obj.value = 77
        c.set_last_error(5)
        return failure != "open"
    def information(token, kind, buffer, size, needed):
        assert kind == 1
        needed._obj.value = 32
        if buffer is None:
            c.set_last_error(5 if failure == "size" else 122)
            return False
        c.cast(buffer, c.POINTER(c.c_void_p))[0] = 1234
        return failure != "read"
    def convert(sid, text):
        c.cast(text, c.POINTER(c.c_void_p))[0] = c.addressof(sid_buffer)
        return failure != "convert"
    security.OpenProcessToken.side_effect = open_token
    security.GetTokenInformation.side_effect = information
    security.ConvertSidToStringSidW.side_effect = convert
    if failure:
        with pytest.raises(OSError):
            api.user_sid()
    else:
        assert api.user_sid() == SID
        kernel.LocalFree.assert_called_once()
    assert kernel.CloseHandle.call_count == (0 if failure == "open" else 1)
