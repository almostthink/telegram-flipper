"""Общие фикстуры.

Тесты не должны трогать реальную папку данных пользователя, поэтому
FLIPPER_DATA_DIR переопределяется до импорта приложения.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("FLIPPER_DATA_DIR", tempfile.mkdtemp(prefix="flipper-test-"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
