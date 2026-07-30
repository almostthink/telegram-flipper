"""Схема базы данных.

SQLite в WAL-режиме: приложение однопользовательское, отдельный сервер БД
здесь был бы лишней зависимостью в exe.

Ключевая идея схемы — хранить не только текущее состояние рынка, но и его
историю. Без истории нельзя ни откалибровать справедливую цену, ни измерить
реальное время до продажи, ни проверить стратегию бэктестом.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain import utcnow


class Base(DeclarativeBase):
    pass


class ListingSnapshot(Base):
    """Наблюдение листинга в конкретный момент.

    Пишем повторно при каждом сканировании: по цепочке снимков видно,
    как долго лот висит и менялась ли цена. Исчезновение лота из выдачи —
    сигнал, что он продан или снят.
    """

    __tablename__ = "listing_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(16), index=True)
    listing_id: Mapped[str] = mapped_column(String(128), index=True)
    collection: Mapped[str] = mapped_column(String(128), index=True)
    #: Порядковый номер выпуска. Рынок платит за него надбавку независимо
    #: от редкости атрибутов: #1 стоит кратно дороже #40597 при одинаковых
    #: модели, фоне и символе.
    number: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(128))
    backdrop: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(128))
    model_rarity: Mapped[float | None] = mapped_column(Float)
    backdrop_rarity: Mapped[float | None] = mapped_column(Float)
    symbol_rarity: Mapped[float | None] = mapped_column(Float)
    #: Цвет фона 0xRRGGBB — половина признака монохрома.
    backdrop_color: Mapped[int | None] = mapped_column(Integer)
    price_ton: Mapped[float] = mapped_column(Float)
    seller: Mapped[str | None] = mapped_column(String(128))
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    gone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    url: Mapped[str | None] = mapped_column(Text)
    #: Блокировка перепродажи: подарок куплен, но продать его нельзя ещё
    #: несколько дней. Для флиппера это замороженный капитал, поэтому
    #: такие лоты отсекаются до расчёта экономики.
    resale_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("market", "listing_id", name="uq_listing"),
        Index("ix_listing_slice", "collection", "model"),
    )


class SaleRecord(Base):
    """Состоявшаяся сделка — основа калибровки справедливой цены.

    Считаем только по фактическим продажам: листинги показывают, чего
    хотят продавцы, а не то, за сколько рынок реально покупает.
    """

    __tablename__ = "sales"

    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(16), index=True)
    #: Идентификатор события продажи — для дедупликации ленты.
    external_id: Mapped[str | None] = mapped_column(String(128))
    #: Идентификатор самого подарка. Отличается от external_id: по нему
    #: видно, что один и тот же экземпляр перепродаётся по кругу.
    #: Без него отличить прогон от активной торговли невозможно —
    #: совпадение атрибутов означает лишь общий срез, а не общий предмет.
    gift_external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    collection: Mapped[str] = mapped_column(String(128), index=True)
    #: Номер выпуска. Хранится, чтобы модель выучила надбавку за красивый
    #: номер по фактическим сделкам, а не брала её из моих представлений.
    number: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(128))
    backdrop: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(128))
    model_rarity: Mapped[float | None] = mapped_column(Float)
    backdrop_rarity: Mapped[float | None] = mapped_column(Float)
    symbol_rarity: Mapped[float | None] = mapped_column(Float)
    #: Цвет фона 0xRRGGBB. Хранится и у сделок, чтобы регрессия могла
    #: выучить надбавку за монохром по фактическим ценам, а не брать её
    #: из настройки.
    backdrop_color: Mapped[int | None] = mapped_column(Integer)
    price_ton: Mapped[float] = mapped_column(Float)
    sold_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    buyer: Mapped[str | None] = mapped_column(String(128))
    seller: Mapped[str | None] = mapped_column(String(128))
    #: Время от листинга до продажи в часах. Заполняется, когда удаётся
    #: сопоставить продажу с ранее наблюдавшимся листингом.
    tts_hours: Mapped[float | None] = mapped_column(Float)
    #: Помечена антифродом как подозрительная — в калибровку не идёт.
    suspicious: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    __table_args__ = (
        UniqueConstraint("market", "external_id", name="uq_sale"),
        Index("ix_sale_slice", "collection", "model", "sold_at"),
    )


class ModelColor(Base):
    """Основной цвет модели — то, чего не отдаёт ни одна площадка.

    Нужен для монохрома: цвет фона известен из карточки лота, а цвет
    самой модели приходится доставать из её анимации. Разбор стоит
    запроса к CDN, поэтому результат хранится: внешность модели не
    меняется никогда, и повторять работу незачем.

    Неудачи тоже записываются. Иначе каждый проход заново ходил бы за
    одним и тем же недоступным файлом.
    """

    __tablename__ = "model_colors"

    id: Mapped[int] = mapped_column(primary_key=True)
    collection: Mapped[str] = mapped_column(String(128), index=True)
    model: Mapped[str] = mapped_column(String(128), index=True)
    #: Цвет как 0xRRGGBB. None означает «добыть не удалось».
    rgb: Mapped[int | None] = mapped_column(Integer)
    #: Сколько раз пытались. Растёт только при неудачах.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("collection", "model", name="uq_model_color"),)


class ListingEvent(Base):
    """Момент выставления подарка на продажу — из ленты событий площадки.

    Отдельно от ``ListingSnapshot``, потому что это разные факты. Снимок —
    «лот был в книге, когда мы смотрели»; событие — «лот выставили тогда-то».
    Для времени до продажи нужно именно второе.

    До появления ленты MRKT момент листинга брался из ``receivedDate``
    подарка, а это когда владелец его получил, а не когда выставил: по
    наблюдениям расхождение доходит до нескольких часов, и TTS завышался
    ровно на эту разницу.

    Храним последнее событие на подарок: перевыставление обнуляет отсчёт.
    """

    __tablename__ = "listing_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(16), index=True)
    gift_external_id: Mapped[str] = mapped_column(String(128), index=True)
    collection: Mapped[str] = mapped_column(String(128), index=True)
    price_ton: Mapped[float | None] = mapped_column(Float)
    listed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        UniqueConstraint("market", "gift_external_id", name="uq_listing_event"),
    )


class FloorSnapshot(Base):
    """Флор коллекции на площадке в момент времени — для тренда и сверки."""

    __tablename__ = "floor_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(16), index=True)
    collection: Mapped[str] = mapped_column(String(128), index=True)
    floor_ton: Mapped[float] = mapped_column(Float)
    volume_24h_ton: Mapped[float | None] = mapped_column(Float)
    sales_24h: Mapped[int | None] = mapped_column(Integer)
    listed_count: Mapped[int | None] = mapped_column(Integer)
    #: Верхняя заявка на покупку — цена, по которой можно выйти прямо
    #: сейчас. Хранится рядом с флором, потому что снимается тем же
    #: широким проходом и осмысленна только в паре с ним: важен не сам
    #: бид, а насколько он отстаёт от флора.
    best_offer_ton: Mapped[float | None] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    __table_args__ = (Index("ix_floor_lookup", "collection", "captured_at"),)


class AttributeFloorSnapshot(Base):
    """Флор по значению атрибута — вход для множителей справедливой цены."""

    __tablename__ = "attribute_floors"

    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(16))
    collection: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(128))
    floor_ton: Mapped[float] = mapped_column(Float)
    rarity_permille: Mapped[float | None] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_attr_lookup", "collection", "kind", "name", "captured_at"),)


class Position(Base):
    """Купленный подарок: от покупки до продажи или сброса."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(16))
    gift_external_id: Mapped[str] = mapped_column(String(128), index=True)
    collection: Mapped[str] = mapped_column(String(128), index=True)
    model: Mapped[str | None] = mapped_column(String(128))
    backdrop: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(128))

    buy_price_ton: Mapped[float] = mapped_column(Float)
    bought_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    #: Справедливая цена и прогноз TTS на момент покупки — чтобы потом
    #: сверить прогноз с фактом и понять, врёт модель или нет.
    fair_value_at_buy: Mapped[float | None] = mapped_column(Float)
    expected_tts_hours: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str | None] = mapped_column(Text)

    listing_id: Mapped[str | None] = mapped_column(String(128))
    ask_price_ton: Mapped[float | None] = mapped_column(Float)
    reprice_count: Mapped[int] = mapped_column(Integer, default=0)
    last_repriced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sell_price_ton: Mapped[float | None] = mapped_column(Float)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Чистый результат после комиссий и газа. Заполняется при закрытии.
    net_pnl_ton: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    paper: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    trades: Mapped[list[TradeLog]] = relationship(back_populates="position")


class TradeLog(Base):
    """Журнал операций: каждое действие движка с обоснованием."""

    __tablename__ = "trade_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(24))
    market: Mapped[str | None] = mapped_column(String(16))
    price_ton: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[str | None] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    paper: Mapped[bool] = mapped_column(Boolean, default=True)

    position: Mapped[Position | None] = relationship(back_populates="trades")


class SignalRecord(Base):
    """Сигнал на покупку — сохраняем и исполненные, и пропущенные.

    Пропущенные не менее важны: по ним видно, была ли отсечка правильной,
    и можно ли ослабить пороги без роста убытков.
    """

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    market: Mapped[str] = mapped_column(String(16))
    listing_id: Mapped[str] = mapped_column(String(128), index=True)
    collection: Mapped[str] = mapped_column(String(128), index=True)
    number: Mapped[int | None] = mapped_column(Integer)
    model: Mapped[str | None] = mapped_column(String(128))
    backdrop: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(128))
    #: Балл коллекционной ценности номера — показываем в интерфейсе, чтобы
    #: было видно, за что именно движок доплачивает.
    number_score: Mapped[float] = mapped_column(Float, default=0.0)
    number_label: Mapped[str | None] = mapped_column(String(64))
    #: Совпадение цвета модели с цветом фона: 0..1. None означает, что
    #: цвет модели ещё не добыт, а не что цвета разные.
    monochrome_score: Mapped[float | None] = mapped_column(Float)

    ask_ton: Mapped[float] = mapped_column(Float)
    fair_value_ton: Mapped[float] = mapped_column(Float)
    net_roi: Mapped[float] = mapped_column(Float)
    liquidity_score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    expected_tts_hours: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float, index=True)

    passed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(64))
    explanation: Mapped[str | None] = mapped_column(Text)
    acted: Mapped[bool] = mapped_column(Boolean, default=False)
