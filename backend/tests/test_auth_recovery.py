"""Поведение при протухшем токене.

На реальном запуске приложение сутки крутило один и тот же отказ: каждые
пятнадцать секунд «токен недействителен (401) — пробую обновить токен»,
и так до бесконечности. Данных не собиралось никаких, а пользователю
никто не сказал, что от него что-то требуется.

Причин было две, и обе здесь зафиксированы.
"""

from __future__ import annotations

import pytest
from app.adapters.base import AuthExpired
from app.auth.tma import TmaAuth, TokenState
from app.config import MarketplaceConfig, Settings
from app.domain import Market
from app.ingest.scanner import Scanner, ScanStats


@pytest.fixture
def settings() -> Settings:
    box = Settings(paper_mode=True)
    box.marketplaces = {"mrkt": MarketplaceConfig(enabled=True, trade_enabled=True)}
    return box


@pytest.fixture
def isolated_auth(monkeypatch) -> TmaAuth:
    """Свой экземпляр авторизации: общий трогать нельзя."""
    from app.auth import tma as tma_mod
    from app.ingest import scanner as scanner_mod

    box = TmaAuth.__new__(TmaAuth)
    box._tokens = {}
    box._pending = None

    monkeypatch.setattr(scanner_mod, "auth", box)
    monkeypatch.setattr(tma_mod, "auth", box)
    return box


def put_token(box: TmaAuth, value: str, *, age_sec: float = 0.0) -> None:
    import time

    box._tokens[Market.MRKT] = TokenState(
        value=value, obtained_at=time.time() - age_sec, source="manual"
    )


# --- Обновление токена ----------------------------------------------------


async def test_rejected_token_is_not_returned_as_fresh(isolated_auth):
    """Вернуть отвергнутый токен — соврать вызывающему.

    Он спрашивает именно потому, что этот токен только что получил 401.
    """
    put_token(isolated_auth, "dead-token")

    assert await isolated_auth.ensure_fresh(Market.MRKT, rejected="dead-token") is None


async def test_untouched_token_is_returned_as_is(isolated_auth):
    """Без отказа свежий токен возвращается без лишней работы."""
    put_token(isolated_auth, "good-token")

    assert await isolated_auth.ensure_fresh(Market.MRKT) == "good-token"


async def test_other_token_survives_a_rejection(isolated_auth):
    """Отвергли один токен, а в хранилище уже лежит другой — он годится."""
    put_token(isolated_auth, "new-token")

    assert await isolated_auth.ensure_fresh(Market.MRKT, rejected="old-token") == "new-token"


# --- Реакция сканера ------------------------------------------------------


async def test_market_is_paused_after_a_dead_token(settings, isolated_auth):
    """Иначе отказ повторяется каждые пятнадцать секунд бесконечно."""
    put_token(isolated_auth, "dead-token")
    scanner = Scanner(settings)

    await scanner._handle_auth_expired(Market.MRKT, AuthExpired("401"), ScanStats())

    assert scanner._is_paused(Market.MRKT)
    assert scanner.disabled_markets.get("mrkt")


async def test_user_is_told_what_to_do(settings, isolated_auth):
    put_token(isolated_auth, "dead-token")
    scanner = Scanner(settings)
    stats = ScanStats()

    await scanner._handle_auth_expired(Market.MRKT, AuthExpired("401"), stats)

    assert any("новый токен" in error for error in stats.errors)


async def test_new_token_wakes_the_market_up(settings, isolated_auth):
    """Пауза до вмешательства обязана сниматься самим вмешательством.

    Без этого площадка оставалась бы выключенной до перезапуска даже
    после того, как пользователь вставил рабочий токен.
    """
    put_token(isolated_auth, "dead-token")
    scanner = Scanner(settings)
    await scanner._handle_auth_expired(Market.MRKT, AuthExpired("401"), ScanStats())
    assert scanner._is_paused(Market.MRKT)

    put_token(isolated_auth, "fresh-token")

    assert not scanner._is_paused(Market.MRKT)
    assert "mrkt" not in scanner.disabled_markets


async def test_same_token_keeps_the_market_paused(settings, isolated_auth):
    """Пересохранение того же самого токена ничего не чинит."""
    put_token(isolated_auth, "dead-token")
    scanner = Scanner(settings)
    await scanner._handle_auth_expired(Market.MRKT, AuthExpired("401"), ScanStats())

    put_token(isolated_auth, "dead-token")

    assert scanner._is_paused(Market.MRKT)


# --- Вход в Telegram ------------------------------------------------------


def test_proxy_url_is_parsed_for_pyrogram():
    """На части сетей Telegram недоступен напрямую — без прокси входа нет."""
    from app.auth.tma import parse_proxy

    assert parse_proxy("socks5://127.0.0.1:1080") == {
        "scheme": "socks5",
        "hostname": "127.0.0.1",
        "port": 1080,
    }


def test_proxy_credentials_are_kept():
    from app.auth.tma import parse_proxy

    proxy = parse_proxy("socks5://user:secret@host.example:9050")
    assert proxy["username"] == "user"
    assert proxy["password"] == "secret"


def test_empty_proxy_means_direct_connection():
    from app.auth.tma import parse_proxy

    assert parse_proxy("") is None
    assert parse_proxy("   ") is None


def test_broken_proxy_is_rejected_before_saving():
    """Иначе ошибка всплыла бы посреди входа, уже после ввода телефона."""
    from app.auth.tma import parse_proxy

    for bad in ("127.0.0.1:1080", "socks5://127.0.0.1", "ftp://host:21"):
        with pytest.raises(ValueError):
            parse_proxy(bad)


async def test_missing_session_file_is_not_an_error(isolated_auth, tmp_path, monkeypatch):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    assert "не было" in await isolated_auth.forget_session()


async def test_session_file_is_removed(isolated_auth, tmp_path, monkeypatch):
    from app import paths

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    (tmp_path / "flipper.session").write_text("данные сессии", encoding="utf-8")

    assert "удалена" in await isolated_auth.forget_session()
    assert not (tmp_path / "flipper.session").exists()


async def test_locked_session_file_explains_itself(isolated_auth, tmp_path, monkeypatch):
    """На Windows файл держит сам клиент; 500 в ответ — не объяснение.

    Именно так и было: маршрут удалял файл напрямую и отдавал
    PermissionError трассировкой в интерфейс.
    """
    from app import paths
    from app.auth import tma as tma_mod

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(tma_mod, "UNLINK_ATTEMPTS", 2)
    path = tmp_path / "flipper.session"
    path.write_text("данные сессии", encoding="utf-8")

    def always_locked(self):
        raise PermissionError(32, "файл занят другим процессом")

    monkeypatch.setattr("pathlib.Path.unlink", always_locked)

    detail = await isolated_auth.forget_session()
    assert "занят" in detail and "Перезапустите" in detail
