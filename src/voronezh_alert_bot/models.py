"""Модели данных для бота оповещений о БПЛА, ракетной опасности и отбое в Воронеже."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AlertKind(str, Enum):
    """Тип оперативного оповещения или отбоя."""

    MISSILE_ALERT = "missile_alert"
    MISSILE_CLEAR = "missile_clear"
    UAV_IMMEDIATE_ALERT = "uav_immediate_alert"
    UAV_IMMEDIATE_CLEAR = "uav_immediate_clear"
    UAV_ALERT = "uav_alert"
    UAV_CLEAR = "uav_clear"
    AVIATION_ALERT = "aviation_alert"
    AVIATION_CLEAR = "aviation_clear"
    UAV_SUMMARY = "uav_summary"

    @property
    def is_clear(self) -> bool:
        return self in {
            AlertKind.MISSILE_CLEAR,
            AlertKind.UAV_IMMEDIATE_CLEAR,
            AlertKind.UAV_CLEAR,
            AlertKind.AVIATION_CLEAR,
        }

    @property
    def is_summary(self) -> bool:
        return self == AlertKind.UAV_SUMMARY

    @property
    def threat_family(self) -> str:
        if self in {AlertKind.MISSILE_ALERT, AlertKind.MISSILE_CLEAR}:
            return "missile"
        if self in {AlertKind.UAV_IMMEDIATE_ALERT, AlertKind.UAV_IMMEDIATE_CLEAR}:
            return "uav_immediate"
        if self in {AlertKind.AVIATION_ALERT, AlertKind.AVIATION_CLEAR}:
            return "aviation"
        if self == AlertKind.UAV_SUMMARY:
            return "uav_summary"
        return "uav"


@dataclass(frozen=True, slots=True)
class AlertSource:
    """Источник оперативных сообщений (Telegram web preview или RSS)."""

    id: str
    name: str
    kind: str  # "telegram_web" | "rss"
    url: str
    enabled: bool = True
    priority: int = 10
    official: bool = False


@dataclass(slots=True)
class SourceMessage:
    """Нормализованное сообщение из Telegram-канала или RSS-ленты."""

    source_id: str
    source_name: str
    source_kind: str
    official: bool
    priority: int
    external_id: str
    url: str
    title: str
    text: str
    published_at: datetime | None = None

    @property
    def dedup_key(self) -> str:
        if self.external_id:
            return f"{self.source_id}:{self.external_id}"
        if self.url:
            return f"{self.source_id}:url:{self.url}"
        return f"{self.source_id}:text:{self.title[:80].casefold()}"


@dataclass(frozen=True, slots=True)
class ClassifiedAlert:
    """Результат классификации сообщения как тревоги или отбоя."""

    kind: AlertKind
    locations: tuple[str, ...]
    location_key: str
    is_voronezh_city_or_region_wide: bool
    uav_general_remains: bool = False
    excerpt: str = ""


@dataclass(slots=True)
class AlertCandidate:
    """Кандидат к публикации: исходное сообщение и распознанное событие."""

    message: SourceMessage
    alert: ClassifiedAlert
    duration_seconds: int | None = None


@dataclass(slots=True)
class AlertCycleReport:
    """Отчёт об одном цикле проверки источников и публикации оповещений."""

    fetched_sources: int = 0
    failed_sources: int = 0
    total_messages: int = 0
    matched_alerts: int = 0
    geo_filtered: int = 0
    duplicates_skipped: int = 0
    stale_skipped: int = 0
    seeded_skipped: int = 0
    posted: int = 0
    errors: list[str] = field(default_factory=list)
    posted_events: list[AlertCandidate] = field(default_factory=list)
