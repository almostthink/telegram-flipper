"""Доменные модели — общий язык всех слоёв.

Площадки отдают разный JSON, поэтому адаптеры приводят его к этим типам,
и вся аналитика дальше работает с ними, ничего не зная о конкретном API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class Market(StrEnum):
    PORTALS = "portals"
    MRKT = "mrkt"
    TONNEL = "tonnel"
    GETGEMS = "getgems"


class AttributeKind(StrEnum):
    """Три оси редкости Telegram-подарка."""

    MODEL = "model"
    BACKDROP = "backdrop"
    SYMBOL = "symbol"


class ActivityKind(StrEnum):
    LISTING = "listing"
    SALE = "sale"
    PRICE_UPDATE = "price_update"
    OFFER = "offer"
    DELIST = "delist"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True, frozen=True)
class Attribute:
    """Значение атрибута с его редкостью.

    ``rarity_permille`` — доля выпуска в промилле (‰), как отдают площадки:
    5 означает, что атрибут встречается у 0.5% экземпляров коллекции.
    """

    kind: AttributeKind
    name: str
    rarity_permille: float | None = None

    @property
    def rarity_fraction(self) -> float | None:
        if self.rarity_permille is None:
            return None
        return self.rarity_permille / 1000.0


@dataclass(slots=True)
class Gift:
    """Конкретный экземпляр подарка."""

    collection: str
    external_id: str
    number: int | None = None
    model: Attribute | None = None
    backdrop: Attribute | None = None
    symbol: Attribute | None = None

    def attribute(self, kind: AttributeKind) -> Attribute | None:
        return {
            AttributeKind.MODEL: self.model,
            AttributeKind.BACKDROP: self.backdrop,
            AttributeKind.SYMBOL: self.symbol,
        }[kind]

    @property
    def slice_key(self) -> str:
        """Ключ среза для группировки статистики.

        Срез — это коллекция плюс модель: именно модель определяет цену
        сильнее всего, а дробить ещё и по фону с символом означает
        остаться без выборки.
        """
        model = self.model.name if self.model else "*"
        return f"{self.collection}|{model}"


@dataclass(slots=True)
class Listing:
    """Активное предложение о продаже."""

    market: Market
    listing_id: str
    gift: Gift
    price_ton: float
    seller: str | None = None
    listed_at: datetime | None = None
    url: str | None = None
    seen_at: datetime = field(default_factory=utcnow)
    #: Момент, начиная с которого лот вообще можно перепродать. Площадки
    #: блокируют свежепереданные подарки на несколько дней — купить такой
    #: значит заморозить деньги, а не совершить сделку.
    resale_available_at: datetime | None = None
    #: Явный признак блокировки, если площадка отдаёт его отдельно.
    locked: bool = False

    @property
    def key(self) -> str:
        return f"{self.market}:{self.listing_id}"

    def resalable_at(self, moment: datetime) -> bool:
        """Можно ли будет перепродать лот к указанному моменту."""
        if self.locked:
            return False
        return self.resale_available_at is None or self.resale_available_at <= moment


@dataclass(slots=True)
class ActivityEvent:
    """Событие рынка: листинг, продажа, изменение цены, оффер."""

    market: Market
    kind: ActivityKind
    gift: Gift
    price_ton: float
    happened_at: datetime
    external_id: str | None = None
    buyer: str | None = None
    seller: str | None = None


@dataclass(slots=True)
class CollectionFloor:
    """Флор и объём коллекции на конкретной площадке."""

    market: Market
    collection: str
    floor_ton: float
    volume_24h_ton: float | None = None
    sales_24h: int | None = None
    listed_count: int | None = None
    captured_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class AttributeFloor:
    """Флор по конкретному значению атрибута внутри коллекции."""

    market: Market
    collection: str
    kind: AttributeKind
    name: str
    floor_ton: float
    rarity_permille: float | None = None
    captured_at: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class CollectionOffer:
    """Коллекционный оффер — гарантированная цена выхода из позиции."""

    market: Market
    collection: str
    price_ton: float
    amount: int = 1


@dataclass(slots=True)
class Balance:
    market: Market
    ton: float


@dataclass(slots=True)
class OwnedGift:
    """Подарок в собственности: в инвентаре или уже выставленный."""

    market: Market
    gift: Gift
    listed: bool
    price_ton: float | None = None
    listing_id: str | None = None
