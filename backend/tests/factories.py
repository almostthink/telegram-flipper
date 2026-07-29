"""Синтетические данные для тестов аналитики.

Реальный рынок недоступен без кредов, поэтому корректность модели
проверяется на данных с известным ответом: генерируем продажи по заданной
формуле и смотрим, восстанавливает ли её регрессия.
"""

from __future__ import annotations

import math
import random
from datetime import timedelta

from app.domain import utcnow
from app.storage.models import ListingSnapshot, SaleRecord

#: Истинная модель цены, которую должна восстановить регрессия:
#: base × exp(коэффициент × редкость модели).
TRUE_BASE = 10.0
TRUE_MODEL_COEF = 0.45


def make_sale(
    *,
    sale_id: int,
    collection: str = "Plush Pepe",
    model: str = "Common",
    model_rarity: float = 100.0,
    price_ton: float | None = None,
    hours_ago: float = 24.0,
    suspicious: bool = False,
    buyer: str | None = None,
    seller: str | None = None,
    tts_hours: float | None = None,
    gift_external_id: str | None = None,
) -> SaleRecord:
    if price_ton is None:
        rarity_score = -math.log(model_rarity / 1000.0)
        price_ton = TRUE_BASE * math.exp(TRUE_MODEL_COEF * rarity_score)

    return SaleRecord(
        id=sale_id,
        market="portals",
        external_id=f"sale-{sale_id}",
        gift_external_id=gift_external_id,
        collection=collection,
        model=model,
        backdrop=None,
        symbol=None,
        model_rarity=model_rarity,
        price_ton=price_ton,
        sold_at=utcnow() - timedelta(hours=hours_ago),
        buyer=buyer,
        seller=seller,
        tts_hours=tts_hours,
        suspicious=suspicious,
    )


def make_listing(
    *,
    listing_id: str,
    collection: str = "Plush Pepe",
    model: str = "Common",
    model_rarity: float = 100.0,
    price_ton: float = 10.0,
    seller: str | None = None,
    row_id: int | None = None,
) -> ListingSnapshot:
    return ListingSnapshot(
        id=row_id if row_id is not None else abs(hash(listing_id)) % 1_000_000,
        market="portals",
        listing_id=listing_id,
        collection=collection,
        model=model,
        model_rarity=model_rarity,
        price_ton=price_ton,
        seller=seller,
        seen_at=utcnow(),
    )


def sales_population(
    count: int = 60, *, noise: float = 0.08, seed: int = 42
) -> list[SaleRecord]:
    """Выборка сделок по истинной модели с логнормальным шумом."""
    rng = random.Random(seed)
    models = [("Common", 300.0), ("Rare", 50.0), ("Epic", 10.0), ("Legendary", 2.0)]

    sales = []
    for index in range(count):
        name, rarity = models[index % len(models)]
        rarity_score = -math.log(rarity / 1000.0)
        clean_price = TRUE_BASE * math.exp(TRUE_MODEL_COEF * rarity_score)
        price = clean_price * math.exp(rng.gauss(0, noise))
        sales.append(
            make_sale(
                sale_id=index + 1,
                model=name,
                model_rarity=rarity,
                price_ton=price,
                hours_ago=rng.uniform(1, 24 * 20),
                tts_hours=rng.uniform(4, 40),
            )
        )
    return sales
