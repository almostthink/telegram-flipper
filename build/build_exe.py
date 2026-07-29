"""Локальная сборка GiftFlipper.exe одной командой.

    python build/build_exe.py

Собирает фронтенд, затем упаковывает всё в build/dist/GiftFlipper.exe.
Требуется Windows: PyInstaller не умеет кросс-компиляцию. На Linux/macOS
скрипт отработает, но получится бинарь под текущую ОС, а не .exe.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
BUILD = ROOT / "build"


def run(cmd: list[str], cwd: Path) -> None:
    print(f"\n$ {' '.join(cmd)}  (в {cwd})")
    result = subprocess.run(cmd, cwd=cwd, shell=(sys.platform == "win32"))
    if result.returncode != 0:
        raise SystemExit(f"Команда завершилась с кодом {result.returncode}")


def build_frontend() -> None:
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm не найден в PATH — установите Node.js 20+")

    # ci быстрее и воспроизводимее, но требует package-lock.json.
    install = ["npm", "ci"] if (FRONTEND / "package-lock.json").exists() else ["npm", "install"]
    run(install, FRONTEND)
    run(["npm", "run", "build"], FRONTEND)


def build_exe() -> None:
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(BUILD / "dist"),
            "--workpath",
            str(BUILD / "work"),
            str(BUILD / "flipper.spec"),
        ],
        ROOT,
    )


def main() -> int:
    if sys.platform != "win32":
        print("! Не Windows — получится бинарь под текущую ОС, а не .exe.")
        print("! Настоящий .exe собирает GitHub Actions на windows-latest.\n")

    build_frontend()
    build_exe()

    produced = list((BUILD / "dist").glob("GiftFlipper*"))
    print("\nГотово:")
    for path in produced:
        print(f"  {path}  ({path.stat().st_size / 1024 / 1024:.1f} МБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
