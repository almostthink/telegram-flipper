"""Проверки API-скелета."""

from __future__ import annotations

from app.config import API_PREFIX


def test_health_ok(client):
    response = client.get(f"{API_PREFIX}/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["paper_mode"] is True, "paper-режим должен быть включён по умолчанию"
    assert body["auto_trade"] is False, "автомат должен быть выключен по умолчанию"


def test_only_mrkt_is_tradable(client):
    """По умолчанию торгуем только на MRKT.

    Схемы MRKT, Portals и Tonnel восстановлены из записи трафика, но
    подтверждены на них разные вещи. У MRKT торговые пути взяты из кода
    мини-аппа, у остальных пока только чтение. Покупать по
    неподтверждённому пути нельзя, поэтому торговля там включается
    осознанно, а не достаётся по умолчанию.
    """
    body = client.get(f"{API_PREFIX}/status").json()
    assert set(body["marketplaces"]) == {"portals", "mrkt", "tonnel", "getgems"}

    assert body["marketplaces"]["mrkt"]["enabled"] is True
    assert body["marketplaces"]["mrkt"]["trade_enabled"] is True
    assert body["marketplaces"]["tonnel"]["trade_enabled"] is False

    # Portals читаем, но не торгуем.
    assert body["marketplaces"]["portals"]["enabled"] is True
    assert body["marketplaces"]["portals"]["trade_enabled"] is False

    # GetGems на GraphQL с persisted queries — выключен осознанно.
    assert body["marketplaces"]["getgems"]["enabled"] is False


def test_auth_lists_every_market_with_a_name(client):
    """Интерфейс строит список площадок по этому ответу.

    Пока список был набран в вёрстке руками, Tonnel работал в бэкенде, но
    в настройках его просто не было — ни включить, ни вставить токен.
    """
    from app.domain import Market

    markets = client.get(f"{API_PREFIX}/auth").json()["markets"]

    assert set(markets) == {market.value for market in Market}
    assert all(item["label"] for item in markets.values()), "площадка без имени"
    assert markets["mrkt"]["tradable"] is True
    assert markets["tonnel"]["tradable"] is True
    assert markets["getgems"]["tradable"] is False


def test_status_marketplaces_carry_names(client):
    body = client.get(f"{API_PREFIX}/status").json()["marketplaces"]
    assert body["tonnel"]["label"] == "Tonnel"


def test_config_roundtrip(client):
    body = client.get(f"{API_PREFIX}/config").json()
    assert body["analytics"]["min_roi"] > 0
    assert body["risk"]["collection_whitelist"] == []

    updated = client.put(
        f"{API_PREFIX}/config", json={"analytics": {"min_roi": 0.2}}
    ).json()
    assert updated["analytics"]["min_roi"] == 0.2
    # Частичный патч не должен затирать соседние поля секции.
    assert updated["analytics"]["max_tts_hours"] == body["analytics"]["max_tts_hours"]

    original_roi = body["analytics"]["min_roi"]
    client.put(f"{API_PREFIX}/config", json={"analytics": {"min_roi": original_roi}})


def test_notify_settings_survive_a_partial_patch(client):
    """Правка одного поля не должна сбрасывать соседние — их правил человек."""
    body = client.put(f"{API_PREFIX}/config", json={"notify": {"min_spread": 0.12}}).json()

    assert body["notify"]["min_spread"] == 0.12
    assert body["notify"]["on_buy"] is True
    assert body["transfer"]["stars_per_gift"] == 25

    client.put(f"{API_PREFIX}/config", json={"notify": {"min_spread": 0.05}})


def test_bot_token_is_never_returned(client):
    """Токен бота — секрет: наружу отдаём только факт его наличия."""
    body = client.get(f"{API_PREFIX}/notify").json()

    assert set(body) >= {"token_saved", "chat_bound", "polling"}
    assert "token" not in body


def test_freezing_a_missing_position_is_reported(client):
    response = client.post(f"{API_PREFIX}/positions/999999/freeze?frozen=true")
    assert response.status_code == 400
    assert "не найдена" in response.json()["detail"]


def test_websocket_echo(client):
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text("ping")
        message = ws.receive_json()
        assert message == {"event": "echo", "payload": "ping"}
