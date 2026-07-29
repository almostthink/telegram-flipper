"""Проверки паузы для неотвечающих площадок.

Неверный адрес или упавший сервис не чинятся повторением запроса. Без
паузы сканер копит таймауты и пишет одну и ту же ошибку в журнал каждые
пять минут — так выглядела работа с ненастроенными Portals и GetGems.
"""

from __future__ import annotations

import time

from app.config import Settings
from app.domain import Market
from app.ingest.scanner import BACKOFF_MINUTES, FAILURE_THRESHOLD, Scanner, ScanStats


def scanner() -> Scanner:
    return Scanner(Settings())


def fail(instance: Scanner, market: Market, times: int) -> ScanStats:
    stats = ScanStats()
    for _ in range(times):
        instance._note_failure(market, "getaddrinfo failed", stats)
    return stats


def test_single_failure_does_not_pause():
    """Разовый сбой сети не повод переставать опрашивать площадку."""
    instance = scanner()
    fail(instance, Market.MRKT, 1)

    assert not instance._is_paused(Market.MRKT)
    assert instance.disabled_markets == {}


def test_pauses_after_threshold():
    instance = scanner()
    fail(instance, Market.GETGEMS, FAILURE_THRESHOLD)

    assert instance._is_paused(Market.GETGEMS)
    assert "getgems" in instance.disabled_markets
    # В интерфейсе видно и причину, и сколько осталось ждать.
    assert "getaddrinfo" in instance.disabled_markets["getgems"]
    assert "пауза" in instance.disabled_markets["getgems"]


def test_pause_grows_with_repeated_failures():
    """Чем дольше площадка молчит, тем реже её беспокоим."""
    instance = scanner()

    fail(instance, Market.PORTALS, FAILURE_THRESHOLD)
    first = instance._health[Market.PORTALS].paused_until

    fail(instance, Market.PORTALS, 1)
    second = instance._health[Market.PORTALS].paused_until

    assert second > first
    # И упирается в потолок, а не растёт бесконечно.
    fail(instance, Market.PORTALS, 20)
    longest = instance._health[Market.PORTALS].paused_until
    assert longest <= time.time() + BACKOFF_MINUTES[-1] * 60 + 5


def test_success_clears_history():
    """Заработавшая площадка возвращается к обычному опросу сразу."""
    instance = scanner()
    fail(instance, Market.MRKT, FAILURE_THRESHOLD)
    assert instance._is_paused(Market.MRKT)

    instance._note_success(Market.MRKT)

    assert not instance._is_paused(Market.MRKT)
    assert instance.disabled_markets == {}


def test_pause_expires_on_its_own():
    """Сетевой сбой проходит сам — вмешательство человека не требуется."""
    instance = scanner()
    fail(instance, Market.MRKT, FAILURE_THRESHOLD)

    instance._health[Market.MRKT].paused_until = time.time() - 1

    assert not instance._is_paused(Market.MRKT)


def test_auth_failure_waits_for_human():
    """Протухший токен паузой не лечится — нужен новый, и об этом говорим."""
    from app.ingest.scanner import MarketHealth

    instance = scanner()
    # Воспроизводим состояние после обработки истёкшей авторизации.
    instance._health[Market.MRKT] = MarketHealth(
        needs_attention=True, reason="требуется новый токен"
    )

    assert instance._is_paused(Market.MRKT)
    assert instance.disabled_markets["mrkt"] == "требуется новый токен"
    # Такая пауза не истекает по времени.
    assert instance._health[Market.MRKT].paused_until == 0.0


def test_manual_reset_resumes_everything():
    instance = scanner()
    fail(instance, Market.MRKT, FAILURE_THRESHOLD)
    fail(instance, Market.GETGEMS, FAILURE_THRESHOLD)

    instance.reset_disabled()

    assert instance.disabled_markets == {}
    assert not instance._is_paused(Market.MRKT)
