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


def test_status_lists_all_marketplaces(client):
    body = client.get(f"{API_PREFIX}/status").json()
    assert set(body["marketplaces"]) == {"portals", "mrkt", "tonnel", "getgems"}

    # Tonnel и GetGems подключены только как источник цен для сверки.
    assert body["marketplaces"]["portals"]["trade_enabled"] is True
    assert body["marketplaces"]["mrkt"]["trade_enabled"] is True
    assert body["marketplaces"]["tonnel"]["trade_enabled"] is False
    assert body["marketplaces"]["getgems"]["trade_enabled"] is False


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
