"""Конфигурация приложения.

Все торговые пороги вынесены сюда и редактируются из UI — ничего не
захардкожено. Значения по умолчанию консервативные: paper-режим включён,
авто-торговля выключена.

Приоритет источников: config.json в папке данных > переменные окружения
(префикс ``FLIPPER_``) > значения по умолчанию.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import paths

APP_VERSION = "0.1.0"
API_PREFIX = "/api/v1"

MarketplaceName = Literal["portals", "mrkt", "getgems"]


class MarketplaceConfig(BaseModel):
    """Настройки одной площадки.

    ``trade_enabled`` отделён от ``enabled`` намеренно: Portals и GetGems
    подключены только как источник цен для сверки, торговать там не будем.
    """

    enabled: bool = True
    trade_enabled: bool = False
    #: Издержка сделки как удержание с цены продажи.
    #:
    #: Все цены в приложении — покупательские, поэтому комиссия любой формы
    #: выражается здесь. У MRKT надбавка 2% берётся с покупателя сверх цены
    #: продавца: получить за лот Y/1.02 при цене показа Y эквивалентно
    #: удержанию 1.961% — это подтверждено записью трафика площадки.
    fee_sell: float = Field(default=0.05, ge=0, le=0.5)
    #: Комиссия, добавляемая к нашей цене покупки. Обычно 0: у площадок
    #: надбавка уже включена в отображаемую цену.
    fee_buy: float = Field(default=0.0, ge=0, le=0.5)
    #: Пауза между запросами, секунды — бережём аккаунт от rate-limit.
    request_delay_sec: float = Field(default=1.0, ge=0)


class AnalyticsConfig(BaseModel):
    """Пороги отбора сделок. Лот проходит, только если выполнены все."""

    #: Минимальная чистая доходность после всех комиссий и газа.
    min_roi: float = Field(default=0.12, ge=0)
    #: Минимальный балл ликвидности (0..1). Главный фильтр от «мёртвых» лотов.
    min_liquidity_score: float = Field(default=0.35, ge=0, le=1)
    #: Максимальное ожидаемое время до продажи.
    max_tts_hours: float = Field(default=48.0, gt=0)
    #: Не покупаем, если флор коллекции упал сильнее этого за 24ч (падающий нож).
    max_floor_drop_24h: float = Field(default=0.15, ge=0, le=1)
    #: Меньше этого числа сделок в срезе — откат на baseline и снижение confidence.
    min_samples_for_regression: int = Field(default=30, ge=1)
    #: Период полураспада веса исторической сделки, дни.
    history_halflife_days: float = Field(default=7.0, gt=0)
    #: Оценка расхода газа TON на одну транзакцию.
    gas_ton: float = Field(default=0.08, ge=0)


class CollectibleConfig(BaseModel):
    """Коллекционные признаки: номер выпуска и ценные фоны.

    Рынок платит за них надбавку, не связанную с редкостью модели: подарок
    #1 стоит кратно дороже #40597 при одинаковых атрибутах, а чёрный фон
    ценится отдельно от своей доли выпуска.

    Эти настройки влияют на baseline-оценку. Как только накопится история
    сделок, регрессия оценит те же факторы по фактическим ценам, и вес
    настроек снизится сам — подгонять их вручную не придётся.
    """

    #: Максимальная надбавка к baseline за идеальный номер (#1).
    #: Ноль полностью отключает учёт номера.
    number_bonus: float = Field(default=0.35, ge=0, le=2.0)
    #: Фоны, которые рынок ценит отдельно от их редкости.
    preferred_backdrops: list[str] = Field(
        default_factory=lambda: ["Black", "Onyx Black", "Midnight Blue", "Gold"]
    )
    #: Надбавка за попадание фона в список выше.
    backdrop_bonus: float = Field(default=0.15, ge=0, le=1.0)
    #: Покупать только лоты с коллекционными признаками: заметный номер
    #: либо предпочитаемый фон. Сильно сужает выдачу — по умолчанию выкл.
    require_collectible: bool = False
    #: Порог балла номера, начиная с которого он считается заметным.
    min_number_score: float = Field(default=0.5, ge=0, le=1)


class RiskLimits(BaseModel):
    """Жёсткие лимиты. Действуют в обоих режимах, но критичны для автомата."""

    daily_budget_ton: float = Field(default=30.0, ge=0)
    max_position_ton: float = Field(default=10.0, ge=0)
    max_open_positions: int = Field(default=8, ge=1)
    #: Не больше N позиций в одной коллекции — защита от концентрации.
    max_positions_per_collection: int = Field(default=3, ge=1)
    #: Автомат торгует только эти коллекции. Пустой список = торговля запрещена.
    collection_whitelist: list[str] = Field(default_factory=list)
    collection_blacklist: list[str] = Field(default_factory=list)
    #: N ошибок подряд — автомат выключается сам.
    circuit_breaker_errors: int = Field(default=5, ge=1)
    #: Убыток за сутки сверх этого — автомат выключается сам.
    circuit_breaker_daily_loss_ton: float = Field(default=5.0, ge=0)


class SellStrategy(BaseModel):
    """Лестница переоценки: выставляем дороже, затем плавно снижаем."""

    #: Стартовая наценка к справедливой цене.
    initial_markup: float = Field(default=0.18, ge=0)
    #: Как часто снижаем цену.
    reprice_interval_hours: float = Field(default=6.0, gt=0)
    #: Шаг снижения за итерацию.
    reprice_step: float = Field(default=0.03, ge=0, le=0.5)
    #: Ниже этой чистой маржи не опускаемся, пока не сработает max_hold.
    floor_margin: float = Field(default=0.02)
    #: Держим дольше — сбрасываем по бид-сайду.
    max_hold_hours: float = Field(default=72.0, gt=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FLIPPER_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- Сервер ---
    host: str = "127.0.0.1"
    port: int = 8731
    open_browser: bool = True
    log_level: str = "INFO"
    #: Разрешить CORS с Vite dev-сервера. В собранном exe не нужен.
    dev_mode: bool = False
    #: Фоновое расписание сканирования. Тесты выключают его, чтобы не
    #: ходить в сеть и не мешать проверкам.
    enable_scheduler: bool = True

    # --- Торговля ---
    #: Симуляция без реальных денег. По умолчанию ВКЛЮЧЕНА.
    paper_mode: bool = True
    #: Тумблер автомата. Работает только при paper_mode=False и заполненном whitelist.
    auto_trade: bool = False

    # --- Секции ---
    analytics: AnalyticsConfig = Field(default_factory=AnalyticsConfig)
    collectible: CollectibleConfig = Field(default_factory=CollectibleConfig)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    sell: SellStrategy = Field(default_factory=SellStrategy)
    marketplaces: dict[str, MarketplaceConfig] = Field(
        default_factory=lambda: {
            # Portals — только источник цен для сверки. Торговля ведётся
            # исключительно на MRKT: её схема подтверждена записью трафика,
            # а пути Portals остались предположением, и покупать вслепую
            # по неподтверждённому API нельзя.
            "portals": MarketplaceConfig(enabled=True, trade_enabled=False, fee_sell=0.05),
            # MRKT: подтверждено salePrice / salePriceWithoutFee = 1.02.
            "mrkt": MarketplaceConfig(
                enabled=True, trade_enabled=True, fee_sell=1 - 1 / 1.02
            ),
            # Только сверка цен — торговлю не ведём.
            "getgems": MarketplaceConfig(enabled=True, trade_enabled=False),
        }
    )

    def save(self) -> None:
        """Пишем конфиг в папку данных. Секреты сюда не попадают — они в keyring."""
        paths.config_path().write_text(
            json.dumps(self.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _drop_unknown_marketplaces(stored: dict) -> dict:
    """Выкидываем площадки, которых больше нет в приложении.

    Сохранённый конфиг переживает обновления, и без этой чистки закрытый
    Tonnel продолжал бы висеть в интерфейсе после его удаления из кода.
    """
    markets = stored.get("marketplaces")
    if not isinstance(markets, dict):
        return stored

    from app.domain import Market

    known = {market.value for market in Market}
    unknown = set(markets) - known
    if unknown:
        stored = {**stored, "marketplaces": {k: v for k, v in markets.items() if k in known}}
    return stored


def load_settings() -> Settings:
    """Читаем config.json, если он есть, иначе стартуем на значениях по умолчанию."""
    path = paths.config_path()
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            return Settings(**_drop_unknown_marketplaces(stored))
        except (json.JSONDecodeError, ValueError):
            # Битый конфиг не должен мешать запуску — откатываемся на дефолты.
            backup = path.with_suffix(".json.broken")
            path.rename(backup)
    return Settings()


settings = load_settings()
