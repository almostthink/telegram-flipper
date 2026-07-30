"""Реестр площадок: создание адаптеров по конфигу и хранение эндпоинтов.

Переопределения эндпоинтов (из HAR-импорта) лежат отдельным файлом в папке
данных, чтобы правка путей переживала обновление exe.
"""

from __future__ import annotations

import json
import logging

from app import paths
from app.adapters import mrkt, portals, reference
from app.adapters.base import EndpointSpec, MarketEndpoints, Marketplace, RateLimiter
from app.auth.tma import auth
from app.config import Settings
from app.domain import Market

log = logging.getLogger(__name__)

DEFAULT_ENDPOINTS: dict[Market, MarketEndpoints] = {
    Market.PORTALS: portals.DEFAULT_ENDPOINTS,
    Market.MRKT: mrkt.DEFAULT_ENDPOINTS,
    Market.GETGEMS: reference.GETGEMS_ENDPOINTS,
}

ADAPTER_CLASSES: dict[Market, type[Marketplace]] = {
    Market.PORTALS: portals.PortalsAdapter,
    Market.MRKT: mrkt.MrktAdapter,
    Market.GETGEMS: reference.GetGemsAdapter,
}


def _overrides_path():
    return paths.data_dir() / "endpoints.json"


def load_overrides() -> dict[Market, MarketEndpoints]:
    path = _overrides_path()
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("Не удалось прочитать %s — использую пути по умолчанию", path)
        return {}

    result: dict[Market, MarketEndpoints] = {}
    for market_name, spec in raw.items():
        try:
            market = Market(market_name)
        except ValueError:
            continue
        endpoints = {
            name: EndpointSpec(
                path=item["path"],
                method=item.get("method", "GET"),
                json_path=item.get("json_path", ""),
            )
            for name, item in (spec.get("endpoints") or {}).items()
            if isinstance(item, dict) and item.get("path")
        }
        result[market] = MarketEndpoints(
            base_url=spec.get("base_url") or DEFAULT_ENDPOINTS[market].base_url,
            endpoints=endpoints,
        )
    return result


def save_override(market: Market, endpoints: MarketEndpoints) -> None:
    path = _overrides_path()
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing[market.value] = {
        "base_url": endpoints.base_url,
        "endpoints": {
            name: {"path": spec.path, "method": spec.method, "json_path": spec.json_path}
            for name, spec in endpoints.endpoints.items()
        },
    }
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Эндпоинты %s сохранены в %s", market.value, path)


def reset_override(market: Market) -> bool:
    """Убираем пользовательские правки путей, возвращая значения по умолчанию.

    Нужно, когда импорт HAR записал в конфиг что-то не то: без сброса
    испорченные адреса переживают перезапуск и чинятся только правкой
    файла руками.
    """
    path = _overrides_path()
    if not path.exists():
        return False

    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        path.unlink(missing_ok=True)
        return True

    if existing.pop(market.value, None) is None:
        return False

    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Правки эндпоинтов %s сброшены к значениям по умолчанию", market.value)
    return True


def endpoints_for(market: Market) -> MarketEndpoints:
    """Пути по умолчанию, поверх которых наложены пользовательские правки."""
    overrides = load_overrides().get(market)
    base = DEFAULT_ENDPOINTS[market]
    if overrides is None:
        return base
    merged = dict(base.endpoints)
    merged.update(overrides.endpoints)
    return MarketEndpoints(base_url=overrides.base_url or base.base_url, endpoints=merged)


#: Ограничители частоты живут дольше адаптеров. Площадка считает запросы
#: по аккаунту, а адаптеры создаются заново на каждый проход: собственный
#: счётчик у каждого означал бы, что выученное замедление сразу забыто и
#: следующий проход снова упирается в тот же лимит.
_LIMITERS: dict[Market, RateLimiter] = {}


def limiter_for(market: Market, base_interval_sec: float) -> RateLimiter:
    limiter = _LIMITERS.get(market)
    if limiter is None or limiter.base_interval != base_interval_sec:
        limiter = RateLimiter(base_interval_sec)
        _LIMITERS[market] = limiter
    return limiter


def build_adapter(market: Market, settings: Settings) -> Marketplace:
    cfg = settings.marketplaces.get(market.value)
    adapter_cls = ADAPTER_CLASSES[market]
    delay = cfg.request_delay_sec if cfg else 1.0
    adapter = adapter_cls(
        endpoints=endpoints_for(market),
        fee_sell=cfg.fee_sell if cfg else 0.05,
        fee_buy=cfg.fee_buy if cfg else 0.0,
        request_delay_sec=delay,
        auth_header=auth.get(market),
        limiter=limiter_for(market, delay),
    )
    return adapter


def build_enabled(settings: Settings) -> dict[Market, Marketplace]:
    """Адаптеры всех включённых площадок."""
    adapters: dict[Market, Marketplace] = {}
    for market in Market:
        cfg = settings.marketplaces.get(market.value)
        if cfg is None or not cfg.enabled:
            continue
        adapters[market] = build_adapter(market, settings)
    return adapters


def trading_markets(settings: Settings) -> list[Market]:
    """Площадки, где разрешено торговать, а не только читать цены."""
    return [
        market
        for market in Market
        if (cfg := settings.marketplaces.get(market.value))
        and cfg.enabled
        and cfg.trade_enabled
        and ADAPTER_CLASSES[market].supports_trading
    ]
