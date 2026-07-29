# -*- mode: python ; coding: utf-8 -*-
"""Спецификация PyInstaller: собирает backend и статику фронта в один exe.

Собирать нужно на Windows — PyInstaller не кросс-компилирует. В репозитории
это делает GitHub Actions на windows-latest, локально — build/build_exe.py.
"""

from pathlib import Path

# SPECPATH задаёт PyInstaller: это папка со спекой (build/).
ROOT = Path(SPECPATH).parent  # noqa: F821
BACKEND = ROOT / "backend"
FRONTEND_DIST = ROOT / "frontend" / "dist"

if not (FRONTEND_DIST / "index.html").exists():
    raise SystemExit(
        f"Фронтенд не собран: {FRONTEND_DIST}\n"
        "Выполните: cd frontend && npm ci && npm run build"
    )

a = Analysis(  # noqa: F821
    [str(BACKEND / "launcher.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    # Статика попадает внутрь бандла, app/paths.py достаёт её из sys._MEIPASS.
    datas=[(str(FRONTEND_DIST), "static")],
    hiddenimports=[
        # uvicorn грузит воркеры и протоколы динамически — PyInstaller их не видит.
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        # SQLAlchemy подтягивает драйвер БД по строке подключения, статического
        # импорта нет — без этих строк собранный exe падает на старте с
        # ModuleNotFoundError: aiosqlite.
        "aiosqlite",
        "sqlalchemy.dialects.sqlite",
        "sqlalchemy.dialects.sqlite.aiosqlite",
        # APScheduler так же резолвит планировщики и триггеры по имени.
        "apscheduler.schedulers.asyncio",
        "apscheduler.triggers.interval",
        "apscheduler.executors.asyncio",
        "app.main",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="GiftFlipper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # Консоль оставляем: в ней видно адрес интерфейса, режим и ошибки запуска.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
