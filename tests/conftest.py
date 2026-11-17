"""Shared fixtures: a throwaway copy of the mailbox per test session."""
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")
DB_SRC = os.path.join(ROOT, "data", "mailbox.db")


@pytest.fixture()
def db_copy(tmp_path):
    dst = tmp_path / "mailbox.db"
    shutil.copy2(DB_SRC, dst)
    return str(dst)


@pytest.fixture()
def server(db_copy):
    from src.server import create_server

    return create_server(db_copy)
