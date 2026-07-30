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
