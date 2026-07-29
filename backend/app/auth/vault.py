"""Хранилище секретов.

Основной путь — системное хранилище ОС (на Windows это DPAPI через keyring):
api_id, api_hash и tma-токены не должны лежать в открытом файле рядом с exe.

Если keyring недоступен (бывает на голых Linux-контейнерах без D-Bus),
откатываемся на файл с правами 0600 и честно сообщаем об этом в UI —
молча понижать уровень защиты нельзя.
"""

from __future__ import annotations

import json
import logging
import os
import stat

from app import paths

log = logging.getLogger(__name__)

SERVICE = "TelegramGiftFlipper"


class Vault:
    """Ключ-значение для секретов с прозрачным фолбэком на файл."""

    def __init__(self) -> None:
        self._backend: str = "keyring"
        self._keyring = None
        try:
            import keyring

            # Проверяем, что бэкенд рабочий, а не заглушка fail.Keyring.
            backend = keyring.get_keyring()
            if "fail" in type(backend).__module__:
                raise RuntimeError("рабочий бэкенд keyring не найден")
            self._keyring = keyring
        except Exception as exc:  # noqa: BLE001 — любая проблема ведёт к фолбэку
            log.warning("keyring недоступен (%s), секреты будут в файле 0600", exc)
            self._backend = "file"

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def is_secure(self) -> bool:
        return self._backend == "keyring"

    # --- Операции --------------------------------------------------------

    def get(self, key: str) -> str | None:
        if self._keyring is not None:
            try:
                return self._keyring.get_password(SERVICE, key)
            except Exception:  # noqa: BLE001
                log.exception("Не удалось прочитать секрет из keyring")
                return None
        return self._file_read().get(key)

    def set(self, key: str, value: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.set_password(SERVICE, key, value)
                return
            except Exception:  # noqa: BLE001
                log.exception("Не удалось записать секрет в keyring, пишу в файл")
                self._backend = "file"
                self._keyring = None
        data = self._file_read()
        data[key] = value
        self._file_write(data)

    def delete(self, key: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(SERVICE, key)
                return
            except Exception:  # noqa: BLE001
                pass
        data = self._file_read()
        if data.pop(key, None) is not None:
            self._file_write(data)

    def has(self, key: str) -> bool:
        return bool(self.get(key))

    # --- Файловый фолбэк -------------------------------------------------

    def _file_path(self):
        return paths.data_dir() / "secrets.json"

    def _file_read(self) -> dict[str, str]:
        path = self._file_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _file_write(self, data: dict[str, str]) -> None:
        path = self._file_path()
        path.write_text(json.dumps(data), encoding="utf-8")
        if os.name != "nt":
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)


vault = Vault()
