"""Отсев манипуляций и ловушек.

Три вещи ломают наивный флиппер:

1. **Wash trading** — продавец гоняет подарок между своими аккаунтами,
   создавая видимость оборота и завышая «историческую цену». Модель
   обучается на этих сделках и переоценивает лот.
2. **Фейковый флор** — одинокий лот сильно ниже рынка. Выглядит как
   скидка, а на деле это либо приманка, либо подарок с испорченным
   атрибутом, который никто не купит.
3. **Выбросы** — единичные сделки по абсурдной цене, тянущие регрессию.

Все три проверки работают до расчёта сигнала: подозрительные продажи
исключаются из калибровки, подозрительные листинги — из покупки.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, timedelta

from app.storage.models import ListingSnapshot, SaleRecord

log = logging.getLogger(__name__)

#: Сколько раз один и тот же подарок должен пройти через рынок за неделю,
#: чтобы это выглядело как прогон, а не как активная торговля.
WASH_REPEAT_THRESHOLD = 3
#: Столько сделок между одной парой участников уже не совпадение.
WASH_PAIR_THRESHOLD = 3
#: Порог отклонения в единицах MAD, за которым цена считается выбросом.
MAD_THRESHOLD = 4.0
#: Насколько лот должен быть ниже кластера, чтобы вызвать подозрение.
FAKE_FLOOR_GAP = 0.45


@dataclass(slots=True)
class FraudReport:
    suspicious_sale_ids: set[int]
    reasons: dict[int, str]

    @property
    def count(self) -> int:
        return len(self.suspicious_sale_ids)


def screen_sales(sales: list[SaleRecord]) -> FraudReport:
    """Помечаем сделки, которые нельзя использовать для калибровки."""
    suspicious: set[int] = set()
    reasons: dict[int, str] = {}

    _flag_wash_pairs(sales, suspicious, reasons)
    _flag_repeat_flips(sales, suspicious, reasons)
    _flag_price_outliers(sales, suspicious, reasons)

    if suspicious:
        log.debug("Антифрод отбраковал %d сделок из %d", len(suspicious), len(sales))
    return FraudReport(suspicious_sale_ids=suspicious, reasons=reasons)


def _flag_wash_pairs(
    sales: list[SaleRecord], suspicious: set[int], reasons: dict[int, str]
) -> None:
    """Повторяющиеся сделки между одной парой участников."""
    pairs: dict[tuple[str, str], list[SaleRecord]] = defaultdict(list)
    for sale in sales:
        if sale.buyer and sale.seller:
            pairs[(sale.seller, sale.buyer)].append(sale)

    for (seller, buyer), group in pairs.items():
        if len(group) < WASH_PAIR_THRESHOLD:
            continue
        # Встречное направление — почти верный признак прогона.
        reverse = len(pairs.get((buyer, seller), []))
        if reverse > 0 or len(group) >= WASH_PAIR_THRESHOLD + 2:
            for sale in group:
                suspicious.add(sale.id)
                reasons[sale.id] = "повторяющиеся сделки между теми же участниками"


def _flag_repeat_flips(
    sales: list[SaleRecord], suspicious: set[int], reasons: dict[int, str]
) -> None:
    """Один и тот же подарок, многократно перепроданный за короткий срок.

    Ключ — идентификатор экземпляра, а не набор атрибутов: тысячи разных
    подарков делят одну модель, и группировка по атрибутам пометила бы
    как прогон всю активную коллекцию. Сделки без идентификатора
    пропускаем — лучше не заметить прогон, чем забраковать чистую историю.
    """
    by_gift: dict[str, list[SaleRecord]] = defaultdict(list)
    for sale in sales:
        if not sale.gift_external_id:
            continue
        by_gift[f"{sale.market}|{sale.gift_external_id}"].append(sale)

    for group in by_gift.values():
        if len(group) < WASH_REPEAT_THRESHOLD:
            continue
        ordered = sorted(group, key=lambda s: _aware(s.sold_at))
        window = _aware(ordered[-1].sold_at) - _aware(ordered[0].sold_at)
        if window <= timedelta(days=7):
            for sale in ordered:
                suspicious.add(sale.id)
                reasons[sale.id] = "подарок перепродан несколько раз за неделю"


def _flag_price_outliers(
    sales: list[SaleRecord], suspicious: set[int], reasons: dict[int, str]
) -> None:
    """Отклонения по медианному абсолютному отклонению внутри среза.

    MAD устойчив к выбросам, в отличие от стандартного отклонения, которое
    сами же выбросы и раздувают.
    """
    by_slice: dict[str, list[SaleRecord]] = defaultdict(list)
    for sale in sales:
        by_slice[f"{sale.collection}|{sale.model}"].append(sale)

    for group in by_slice.values():
        if len(group) < 6:
            continue
        prices = [sale.price_ton for sale in group]
        median = statistics.median(prices)
        deviations = [abs(price - median) for price in prices]
        mad = statistics.median(deviations)
        if mad <= 0:
            continue
        for sale in group:
            if abs(sale.price_ton - median) / mad > MAD_THRESHOLD:
                suspicious.add(sale.id)
                reasons[sale.id] = f"цена отклоняется от медианы среза ({median:.1f} TON)"


def is_suspicious_listing(
    listing: ListingSnapshot, peers: list[ListingSnapshot]
) -> str | None:
    """Проверка листинга перед покупкой. Возвращает причину отказа или None."""
    comparable = [
        other.price_ton
        for other in peers
        if other.id != listing.id and other.model == listing.model and other.price_ton > 0
    ]

    if len(comparable) >= 3:
        median = statistics.median(comparable)
        if median > 0 and listing.price_ton < median * (1 - FAKE_FLOOR_GAP):
            # Дешевле кластера почти вдвое — либо приманка, либо у лота
            # есть изъян, которого не видно в атрибутах.
            second_cheapest = sorted(comparable)[0]
            if listing.price_ton < second_cheapest * (1 - FAKE_FLOOR_GAP):
                return "цена аномально ниже сопоставимых лотов"

    if listing.price_ton <= 0:
        return "некорректная цена"

    return None


def seller_concentration(listings: list[ListingSnapshot]) -> float:
    """Доля книги, принадлежащая одному продавцу.

    Высокая концентрация означает, что «рынок» — это один человек, и
    флор держится ровно до тех пор, пока ему это выгодно.
    """
    sellers = [item.seller for item in listings if item.seller]
    if not sellers:
        return 0.0
    _, top_count = Counter(sellers).most_common(1)[0]
    return top_count / len(sellers)


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)
