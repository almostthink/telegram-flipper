"""Настройки доступа: токены площадок, Telegram-креды, HAR-импорт."""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.adapters import registry
from app.adapters.har import parse_har
from app.auth.tma import COOKIE_MARKETS, auth, has_auth_cookie
from app.auth.vault import vault
from app.domain import Market

log = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

MAX_HAR_BYTES = 40 * 1024 * 1024


class TokenRequest(BaseModel):
    market: str
    token: str


class CredentialsRequest(BaseModel):
    api_id: str
    api_hash: str


@router.get("/auth")
async def auth_status() -> dict:
    """Состояние доступов. Сами токены наружу не отдаём — только факт наличия."""
    markets = {}
    for market in Market:
        state = auth.state(market)
        markets[market.value] = {
            "configured": state is not None and bool(state.value),
            "source": state.source if state else None,
            "age_hours": round(state.age_sec / 3600, 1) if state else None,
            "stale": state.looks_stale if state else True,
        }

    return {
        "markets": markets,
        "vault_backend": vault.backend,
        "vault_secure": vault.is_secure,
        "userbot_available": auth.userbot_available(),
        "credentials_saved": auth.has_credentials(),
    }


@router.post("/auth/token")
async def set_token(request: TokenRequest) -> dict:
    try:
        market = Market(request.market)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Неизвестная площадка") from exc

    try:
        auth.set_manual(market, request.token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    saved = auth.get(market) or ""
    detail = f"Токен {market.value} сохранён"

    if market in COOKIE_MARKETS:
        # Молча принять строку без токена — значит обречь пользователя
        # ловить непонятные 401 вместо ясного сообщения сейчас.
        if not has_auth_cookie(saved):
            detail += ". Внимание: в строке не видно AUTH_TOKEN или JWT_TOKEN — "
            detail += "проверьте, что скопирован весь заголовок Cookie"
        removed = len(request.token.split(";")) - len(saved.split(";"))
        if removed > 0:
            detail += f". Убрано счётчиков аналитики: {removed}"

    return {"ok": True, "detail": detail}


@router.delete("/auth/token/{market_name}")
async def forget_token(market_name: str) -> dict:
    try:
        market = Market(market_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Неизвестная площадка") from exc
    auth.forget(market)
    return {"ok": True}


@router.post("/auth/credentials")
async def set_credentials(request: CredentialsRequest) -> dict:
    """Сохраняем api_id и api_hash. Хранятся локально, наружу не уходят."""
    if not auth.userbot_available():
        raise HTTPException(
            status_code=400,
            detail=(
                "Pyrogram не установлен. Установите: pip install pyrogram tgcrypto — "
                "или используйте ручной ввод токена."
            ),
        )
    try:
        auth.save_credentials(request.api_id, request.api_hash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "detail": "Данные сохранены"}


@router.post("/auth/refresh/{market_name}")
async def refresh_token(market_name: str) -> dict:
    try:
        market = Market(market_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Неизвестная площадка") from exc

    try:
        await auth.refresh_via_userbot(market)
    except Exception as exc:  # noqa: BLE001 — показываем причину пользователю
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "detail": f"Токен {market.value} обновлён"}


@router.get("/endpoints")
async def list_endpoints() -> dict:
    """Текущие адреса API площадок — с учётом пользовательских правок."""
    result = {}
    for market in Market:
        endpoints = registry.endpoints_for(market)
        result[market.value] = {
            "base_url": endpoints.base_url,
            "endpoints": {
                name: {"path": spec.path, "method": spec.method, "json_path": spec.json_path}
                for name, spec in sorted(endpoints.endpoints.items())
            },
        }
    return result


@router.post("/endpoints/{market_name}/import-har")
async def import_har(market_name: str, file: UploadFile = File(...)) -> dict:
    """Восстанавливаем адреса API из HAR-файла.

    Файл разбирается в памяти и не сохраняется: HAR содержит содержимое
    сессии, включая заголовок авторизации.
    """
    try:
        market = Market(market_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Неизвестная площадка") from exc

    raw = await file.read()
    if len(raw) > MAX_HAR_BYTES:
        raise HTTPException(status_code=413, detail="HAR больше 40 МБ — сократите запись")

    try:
        result = parse_har(raw, host_filter=_host_hint(market))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    merged = result.to_endpoints(registry.endpoints_for(market))
    registry.save_override(market, merged)

    token_saved = False
    if result.auth_header:
        try:
            auth.set_manual(market, result.auth_header)
            token_saved = True
        except ValueError:
            pass

    return {
        "ok": True,
        "base_url": result.base_url,
        "token_saved": token_saved,
        # Фильтр по имени площадки не сработал, адреса взяты с другого
        # домена — стоит убедиться глазами, что это действительно её API.
        "matched_by_fallback": result.matched_by_fallback,
        "found": [
            {
                "endpoint": item.endpoint,
                "path": item.path,
                "method": item.method,
                "json_path": item.json_path,
                "records_in_sample": item.sample_count,
            }
            for item in result.findings
        ],
        "skipped": result.skipped,
    }


def _host_hint(market: Market) -> str | None:
    return {
        Market.PORTALS: "portals",
        Market.MRKT: "mrkt",
        Market.GETGEMS: "getgems",
    }.get(market)
