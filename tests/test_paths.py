from datetime import datetime, timezone
from pathlib import Path

import pytest

from worktime.app import create_default_app
from worktime.paths import database_path


def test_localappdata_path_is_stable_and_creates_parents(tmp_path, monkeypatch):
    local = tmp_path / "user profile"
    first = tmp_path / "cwd1"
    second = tmp_path / "cwd2"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    path = database_path(environ={"LOCALAPPDATA": str(local)})
    assert path == local / "WorkTime" / "data" / "worktime.db"
    assert path.parent.is_dir()
    assert not path.exists()
    monkeypatch.chdir(second)
    assert database_path(environ={"LOCALAPPDATA": str(local)}) == path
    assert not (first / "data").exists()
    assert not (second / "data").exists()


@pytest.mark.parametrize("value", [None, "", " ", "relative", "C:relative"])
def test_invalid_environment_never_falls_back_to_cwd(tmp_path, monkeypatch, value):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="LOCALAPPDATA"):
        database_path(environ={} if value is None else {"LOCALAPPDATA": value})
    assert list(tmp_path.iterdir()) == []


def test_explicit_test_root_needs_no_environment(tmp_path):
    assert database_path(environ={}, base_directory=tmp_path) == tmp_path / "data" / "worktime.db"


def test_productive_factory_does_not_migrate_legacy_database(tmp_path, monkeypatch):
    legacy = tmp_path / "repo" / "data" / "worktime.db"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy bytes must not be read or copied")
    monkeypatch.chdir(legacy.parent.parent)
    local = tmp_path / "profile"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    now = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    app = create_default_app(clock=lambda: now)
    try:
        assert app.status().day.events == ()
        app.start_work()
    finally:
        app.close()
    assert legacy.read_bytes() == b"legacy bytes must not be read or copied"
    assert (local / "WorkTime/data/worktime.db").is_file()
