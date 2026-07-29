"""Разрешение путей — работает и из исходников, и из собранного PyInstaller-бинаря.

PyInstaller в onefile-режиме распаковывает бандл во временную папку и кладёт её
путь в ``sys._MEIPASS``. Статика фронта лежит внутри бандла (read-only),
а данные пользователя — всегда снаружи, в профиле пользователя, чтобы
не потеряться при перезапуске и обновлении exe.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "TelegramGiftFlipper"


def is_frozen() -> bool:
    """True, если запущены из собранного PyInstaller-бинаря."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def bundle_dir() -> Path:
    """Корень read-only ресурсов (статика фронта, шаблоны)."""
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    # backend/app/paths.py -> backend/
    return Path(__file__).resolve().parent.parent


def static_dir() -> Path:
    """Собранный фронтенд."""
    if is_frozen():
        return bundle_dir() / "static"
    return bundle_dir().parent / "frontend" / "dist"


def data_dir() -> Path:
    """Записываемая папка пользователя: БД, логи, конфиг, сессии.

    Переопределяется через FLIPPER_DATA_DIR — удобно для тестов и портативного
    режима (запуск с флешки рядом с exe).
    """
    override = os.environ.get("FLIPPER_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        path = Path(base) / APP_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        path = Path(base) / APP_NAME

    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "flipper.db"


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return data_dir() / "config.json"
