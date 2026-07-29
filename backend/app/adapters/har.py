"""Восстановление эндпоинтов площадки из HAR-файла.

Зачем: у MRKT и Portals нет опубликованного API, их пути меняются. Вместо
того чтобы каждый раз лезть в код, пользователь открывает мини-апп в
браузере, делает нужные действия, сохраняет HAR (DevTools → Network →
Export HAR) и загружает его в Настройках. Отсюда достаются реальные пути,
метод, расположение массива данных в ответе и заголовок авторизации.

HAR содержит содержимое сессии, поэтому файл разбирается локально,
никуда не отправляется и не сохраняется на диск.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.adapters.base import EndpointSpec, MarketEndpoints

log = logging.getLogger(__name__)

#: Ключевые слова пути → имя эндпоинта в MarketEndpoints.
#: Порядок важен: более специфичные проверки идут раньше.
CLASSIFIERS: list[tuple[str, tuple[str, ...]]] = [
    ("collection_floors", ("floor",)),
    ("filter_floors", ("filter", "attribute", "trait")),
    ("activity", ("activit", "action", "history", "event", "sale")),
    ("offers", ("offer", "bid")),
    ("balance", ("balance", "wallet")),
    ("inventory", ("owned", "my", "inventory", "user/gift")),
    ("collections", ("collection",)),
    ("listings", ("search", "nft", "gift", "item", "listing")),
]

#: Ключи, под которыми в ответе обычно лежит массив данных.
ARRAY_KEYS = ("results", "items", "data", "nfts", "gifts", "list", "activities", "collections")

#: Инфраструктурные домены: сам Telegram, CDN, аналитика. Их ответы никогда
#: не относятся к API площадки и только зашумляют выдачу.
NOISE_HOSTS = (
    "telegram.org",
    "telegram-cdn",
    "t.me",
    "google",
    "gstatic",
    "doubleclick",
    "sentry.io",
    "amplitude",
    "analytics",
    "cloudflareinsights",
    "hotjar",
    "intercom",
)


@dataclass(slots=True)
class HarFinding:
    """Один распознанный эндпоинт."""

    endpoint: str
    path: str
    method: str
    json_path: str
    sample_count: int
    base_url: str


@dataclass(slots=True)
class HarImportResult:
    base_url: str | None = None
    findings: list[HarFinding] = field(default_factory=list)
    auth_header: str | None = None
    skipped: int = 0
    #: Домены, отдавшие JSON. Показываем их, когда ничего не распознали —
    #: по ним видно, туда ли вообще смотрел парсер.
    seen_hosts: set[str] = field(default_factory=set)
    #: Эндпоинты найдены только после отключения фильтра по домену:
    #: адрес API площадки не содержит её имени, стоит проверить глазами.
    matched_by_fallback: bool = False

    def to_endpoints(self, fallback: MarketEndpoints) -> MarketEndpoints:
        """Накладываем найденное поверх текущей конфигурации."""
        merged = dict(fallback.endpoints)
        for finding in self.findings:
            merged[finding.endpoint] = EndpointSpec(
                path=finding.path, method=finding.method, json_path=finding.json_path
            )
        return MarketEndpoints(
            base_url=self.base_url or fallback.base_url,
            endpoints=merged,
        )


def classify(path: str) -> str | None:
    """Определяем назначение эндпоинта по его пути."""
    lowered = path.lower()
    for name, keywords in CLASSIFIERS:
        if any(keyword in lowered for keyword in keywords):
            return name
    return None


def find_array_path(payload: Any, depth: int = 0) -> tuple[str, int] | None:
    """Ищем в ответе массив объектов и возвращаем путь к нему и его длину."""
    if isinstance(payload, list):
        objects = [item for item in payload if isinstance(item, dict)]
        return ("", len(objects)) if objects else None

    if not isinstance(payload, dict) or depth > 3:
        return None

    # Сначала пробуем привычные имена, потом обходим остальные ключи.
    ordered = [k for k in ARRAY_KEYS if k in payload] + [
        k for k in payload if k not in ARRAY_KEYS
    ]
    for key in ordered:
        value = payload[key]
        if isinstance(value, list):
            objects = [item for item in value if isinstance(item, dict)]
            if objects:
                return key, len(objects)
        nested = find_array_path(value, depth + 1)
        if nested is not None:
            sub_path, count = nested
            return (f"{key}.{sub_path}" if sub_path else key), count
    return None


def parse_har(raw: bytes | str, *, host_filter: str | None = None) -> HarImportResult:
    """Разбираем HAR и достаём из него эндпоинты площадки.

    ``host_filter`` — подстрока домена (например ``mrkt``), чтобы отсеять
    аналитику, CDN и телеграмовские сервисы.

    Если по фильтру ничего не нашлось, разбираем повторно без него, отбросив
    только заведомо посторонние домены. Площадка вполне может держать API на
    домене, в имени которого её названия нет, и молча возвращать «ничего не
    найдено» в такой ситуации неприемлемо: пользователь не поймёт, что
    именно пошло не так.
    """
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Файл не является корректным HAR (ожидается JSON)") from exc

    entries = document.get("log", {}).get("entries")
    if not isinstance(entries, list):
        raise ValueError("В HAR нет раздела log.entries")

    result = _scan_entries(entries, host_filter=host_filter)
    seen_hosts = set(result.seen_hosts)

    if not result.findings and host_filter:
        log.info("По фильтру '%s' ничего не найдено — повторяю без него", host_filter)
        fallback = _scan_entries(entries, host_filter=None)
        # Домены копим из обоих проходов: отфильтрованный мог не увидеть
        # ничего, и именно они объясняют пользователю причину неудачи.
        seen_hosts |= fallback.seen_hosts
        if fallback.findings:
            fallback.matched_by_fallback = True
            return fallback

    if not result.findings:
        hosts = ", ".join(sorted(seen_hosts)[:8]) or "нет JSON-ответов вовсе"
        raise ValueError(
            "В HAR не найдено ни одного подходящего JSON-запроса. "
            "Убедитесь, что запись велась при открытом мини-аппе, что вы "
            "пролистали список подарков и что HAR сохранён вместе с телами "
            f"ответов. Домены в файле: {hosts}"
        )

    return result


def _scan_entries(entries: list, *, host_filter: str | None) -> HarImportResult:
    """Один проход по записям HAR с заданным фильтром домена."""
    result = HarImportResult()
    best: dict[str, HarFinding] = {}
    host_counts: dict[str, int] = {}

    for entry in entries:
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        url = request.get("url")
        if not isinstance(url, str):
            continue

        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if not parsed.path or parsed.path == "/":
            continue

        if host_filter:
            if host_filter.lower() not in host:
                continue
        elif any(noise in host for noise in NOISE_HOSTS):
            # Без фильтра отсекаем инфраструктуру: сам Telegram, CDN,
            # аналитику. Иначе в выдачу попадут их служебные ответы.
            continue

        # Статику и не-JSON пропускаем: нас интересует только API.
        mime = str((response.get("content") or {}).get("mimeType", ""))
        if "json" not in mime.lower():
            result.skipped += 1
            continue

        result.seen_hosts.add(host)

        if result.auth_header is None:
            result.auth_header = _extract_auth(request.get("headers"))

        endpoint = classify(parsed.path)
        if endpoint is None:
            result.skipped += 1
            continue

        text = (response.get("content") or {}).get("text")
        json_path, count = "", 0
        if isinstance(text, str) and text.strip():
            try:
                found = find_array_path(json.loads(text))
            except (json.JSONDecodeError, ValueError):
                found = None
            if found:
                json_path, count = found

        base_url = f"{parsed.scheme}://{parsed.netloc}"
        host_counts[base_url] = host_counts.get(base_url, 0) + 1

        candidate = HarFinding(
            endpoint=endpoint,
            path=parsed.path,
            method=str(request.get("method", "GET")).upper(),
            json_path=json_path,
            sample_count=count,
            base_url=base_url,
        )
        # При нескольких кандидатах берём ответ с наибольшим числом записей:
        # он с большей вероятностью настоящий список, а не пустая выдача.
        current = best.get(endpoint)
        if current is None or candidate.sample_count > current.sample_count:
            best[endpoint] = candidate

    if best:
        result.findings = sorted(best.values(), key=lambda f: f.endpoint)
        result.base_url = max(host_counts, key=lambda k: host_counts[k])

        for finding in result.findings:
            if finding.base_url != result.base_url:
                log.warning(
                    "Эндпоинт %s найден на другом хосте (%s) — проверьте вручную",
                    finding.endpoint,
                    finding.base_url,
                )
    return result


def _extract_auth(headers: Any) -> str | None:
    """Достаём Authorization: у мини-аппов это 'tma <initData>'."""
    if not isinstance(headers, list):
        return None
    for header in headers:
        if not isinstance(header, dict):
            continue
        if str(header.get("name", "")).lower() == "authorization":
            value = header.get("value")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None
