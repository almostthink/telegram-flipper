"""Проверки предохранителей.

Автомат не должен включаться случайно. Это самая дорогая ошибка в проекте,
поэтому она закрыта тестами с самого первого этапа.
"""

from __future__ import annotations

from app.config import API_PREFIX, Settings


def test_defaults_are_safe():
    fresh = Settings()
    assert fresh.paper_mode is True
    assert fresh.auto_trade is False
    assert fresh.risk.collection_whitelist == [], "пустой whitelist = торговля запрещена"
    assert fresh.host == "127.0.0.1", "сервер не должен слушать внешний интерфейс"


def test_auto_trade_rejected_in_paper_mode(client):
    body = client.put(
        f"{API_PREFIX}/config", json={"auto_trade": True, "paper_mode": True}
    ).json()
    assert body["auto_trade"] is False


def test_auto_trade_rejected_without_whitelist(client):
    body = client.put(
        f"{API_PREFIX}/config",
        json={"auto_trade": True, "paper_mode": False, "risk": {"collection_whitelist": []}},
    ).json()
    assert body["auto_trade"] is False

    client.put(f"{API_PREFIX}/config", json={"paper_mode": True})


def test_auto_trade_allowed_with_live_and_whitelist(client):
    body = client.put(
        f"{API_PREFIX}/config",
        json={
            "auto_trade": True,
            "paper_mode": False,
            "risk": {"collection_whitelist": ["Plush Pepe"]},
        },
    ).json()
    assert body["auto_trade"] is True

    # Возвращаем безопасное состояние, чтобы не влиять на другие тесты.
    client.put(
        f"{API_PREFIX}/config",
        json={"auto_trade": False, "paper_mode": True, "risk": {"collection_whitelist": []}},
    )
