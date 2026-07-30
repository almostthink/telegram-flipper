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


def test_every_market_has_an_adapter():
    """Площадка в перечислении без адаптера — падение при первом же обращении.

    Tonnel сюда вернулся: запись трафика подтвердила, что площадка
    работает. Ровно поэтому проверка теперь про соответствие, а не про
    конкретный список: он меняется, а требование — нет.
    """
    from app.adapters import registry
    from app.domain import Market

    assert {market.value for market in Market} == {
        "portals",
        "mrkt",
        "tonnel",
        "getgems",
    }
    assert set(registry.ADAPTER_CLASSES) == set(Market)
    assert set(registry.DEFAULT_ENDPOINTS) == set(Market)
    assert set(Settings().marketplaces) == set(registry.ADAPTER_CLASSES)


def test_stale_config_drops_removed_marketplaces():
    """Сохранённый конфиг переживает обновления.

    Без чистки закрытая площадка воскресала бы из старого config.json после
    удаления из кода.
    """
    from app.config import _drop_unknown_marketplaces

    stored = {
        "paper_mode": True,
        "marketplaces": {
            "mrkt": {"enabled": True},
            "tonnel": {"enabled": True},
            "выдуманная": {"enabled": True},
        },
    }
    cleaned = _drop_unknown_marketplaces(stored)

    assert set(cleaned["marketplaces"]) == {"mrkt", "tonnel"}
    # Остальные разделы конфига трогать нельзя.
    assert cleaned["paper_mode"] is True
    # Исходный словарь не мутируем.
    assert "выдуманная" in stored["marketplaces"]


def test_config_without_marketplaces_section_is_untouched():
    from app.config import _drop_unknown_marketplaces

    assert _drop_unknown_marketplaces({"paper_mode": False}) == {"paper_mode": False}
