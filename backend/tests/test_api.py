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


def test_websocket_echo(client):
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text("ping")
        message = ws.receive_json()
        assert message == {"event": "echo", "payload": "ping"}
