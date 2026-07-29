"""Точка входа приложения — то, что превращается в flipper.exe.

Поднимает локальный uvicorn и открывает интерфейс в браузере по умолчанию.
Слушает только 127.0.0.1: снаружи к приложению подключиться нельзя, потому
что оно оперирует торговым доступом к аккаунту.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import multiprocessing
import socket
import sys
import threading
import time
import webbrowser

import uvicorn
from app import paths
from app.config import APP_VERSION, settings

log = logging.getLogger(__name__)

PORT_SCAN_ATTEMPTS = 20


def find_free_port(host: str, preferred: int) -> int:
    """Ищем свободный порт начиная с preferred.

    Второй запущенный exe не должен падать с 'address already in use' —
    он просто займёт соседний порт.
    """
    for offset in range(PORT_SCAN_ATTEMPTS):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
            return candidate

    # Все занято — просим ОС выдать любой свободный.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _open_browser_when_ready(url: str, server: uvicorn.Server) -> None:
    """Ждём фактического старта сервера, иначе браузер откроет пустую страницу."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            with contextlib.suppress(Exception):
                webbrowser.open(url)
            return
        time.sleep(0.15)
    log.warning("Сервер не поднялся за 20с — откройте %s вручную", url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flipper", description="Telegram Gift Flipper")
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    parser.add_argument("--reload", action="store_true", help="автоперезагрузка (разработка)")
    parser.add_argument("--version", action="version", version=f"flipper {APP_VERSION}")
    args = parser.parse_args(argv)

    port = find_free_port(args.host, args.port)
    url = f"http://{args.host}:{port}"

    print(f"\n  Telegram Gift Flipper {APP_VERSION}")
    print(f"  Интерфейс:    {url}")
    print(f"  Данные:       {paths.data_dir()}")
    print(f"  Режим:        {'PAPER (симуляция)' if settings.paper_mode else 'LIVE'}")
    print("  Остановка:    Ctrl+C\n")

    if args.reload:
        # В режиме перезагрузки uvicorn сам управляет процессом — браузер не трогаем.
        uvicorn.run(
            "app.main:app",
            host=args.host,
            port=port,
            reload=True,
            log_level=settings.log_level.lower(),
        )
        return 0

    config = uvicorn.Config(
        "app.main:app",
        host=args.host,
        port=port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(config)

    if settings.open_browser and not args.no_browser:
        threading.Thread(
            target=_open_browser_when_ready, args=(url, server), daemon=True
        ).start()

    with contextlib.suppress(KeyboardInterrupt):
        server.run()
    print("\n  Остановлено.")
    return 0


if __name__ == "__main__":
    # Обязательно для PyInstaller под Windows: иначе дочерние процессы
    # перезапускают exe заново и получается форк-бомба.
    multiprocessing.freeze_support()
    sys.exit(main())
