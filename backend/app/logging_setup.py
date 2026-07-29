"""Логирование в консоль и в файл с ротацией.

Журнал сделок — отдельная история (Этап 6, таблица в БД). Здесь только
технические логи: запросы к площадкам, ошибки адаптеров, старт/стоп.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from app import paths

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # uvicorn мог настроить логи раньше — не дублируем хендлеры

    root.setLevel(level.upper())
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        paths.log_dir() / "flipper.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # httpx на INFO печатает каждый запрос — при сканировании рынка это шум.
    logging.getLogger("httpx").setLevel(logging.WARNING)
