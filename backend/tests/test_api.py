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
    """Торгуем исключительно на MRKT.

    Остальные площадки подключены как источник цен для кросс-маркет
    сверки: их схемы не подтверждены записью трафика, и покупать по
    неподтверждённому API нельзя.
    """
    body = client.get(f"{API_PREFIX}/status").json()
    assert set(body["marketplaces"]) == {"portals", "mrkt", "getgems"}
    assert "tonnel" not in body["marketplaces"], "Tonnel закрылся и удалён"

    assert body["marketplaces"]["mrkt"]["trade_enabled"] is True
    for reference in ("portals", "getgems"):
        assert body["marketplaces"][reference]["trade_enabled"] is False
        assert body["marketplaces"][reference]["enabled"] is True


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
