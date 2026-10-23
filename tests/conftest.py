"""All test database connections must stay inside pytest's temporary tree."""
from pathlib import Path
import sqlite3

import pytest


@pytest.fixture(autouse=True)
def temporary_databases_only(monkeypatch, tmp_path_factory):
    connect = sqlite3.connect
    temporary_root = tmp_path_factory.getbasetemp().resolve()

    def guarded_connect(database, *args, **kwargs):
        assert Path(database).resolve().is_relative_to(temporary_root), (
            "Tests must never open an existing user database", database)
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
