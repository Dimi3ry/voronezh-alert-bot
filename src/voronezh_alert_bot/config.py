"""Конфигурация бота оповещений о БПЛА, ракетной опасности и отбое в Воронеже."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .models import AlertSource

DEFAULT_ALERT_SOURCES: tuple[AlertSource, ...] = (
    AlertSource(
        id="gusev_36",
        name="Александр Гусев (@gusev_36)",
        kind="telegram_web",
        url="https://t.me/s/gusev_36",
        enabled=True,
        priority=1,
        official=True,
    ),
    AlertSource(
        id="mchs36",
        name="МЧС Воронежской области (@mchs36)",
        kind="telegram_web",
        url="https://t.me/s/mchs36",
        enabled=True,
        priority=2,
        official=True,
    ),
    AlertSource(
        id="govvrn36",
        name="Правительство Воронежской области (@govvrn36)",
        kind="telegram_web",
        url="https://t.me/s/govvrn36",
        enabled=True,
        priority=3,
        official=True,
    ),
    AlertSource(
        id="gorcom36",
        name="Горком36",
        kind="rss",
        url="https://gorcom36.ru/rss/",
        enabled=True,
        priority=10,
        official=False,
    ),
    AlertSource(
        id="tvgubernia",
        name="TV Губерния",
        kind="rss",
        url="https://tv-gubernia.ru/feed/",
        enabled=True,
        priority=11,
        official=False,
    ),
    AlertSource(
        id="riavrn",
        name="РИА «Воронеж»",
        kind="rss",
        url="https://riavrn.ru/rss/",
        enabled=True,
        priority=12,
        official=False,
    ),
    AlertSource(
        id="vestivrn",
        name="Вести Воронеж",
        kind="rss",
        url="https://vestivrn.ru/rss/",
        enabled=True,
        priority=13,
        official=False,
    ),
    AlertSource(
        id="moe",
        name="МОЁ! Online",
        kind="rss",
        url="https://moe-online.ru/rss",
        enabled=True,
        priority=14,
        official=False,
    ),
)

VALID_ALERT_GEO_SCOPES = frozenset({"region", "city"})


def load_dotenv(path: Path) -> None:
    """Загружает переменные из .env файла без перезаписи уже заданных в окружении."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and (
            (value[0] == '"' and value[-1] == '"')
            or (value[0] == "'" and value[-1] == "'")
        ):
            value = value[1:-1]
        elif "#" in value:
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key, value)


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    val = raw.strip().lower()
    if val in {"1", "true", "yes", "on", "да"}:
        return True
    if val in {"0", "false", "no", "off", "нет"}:
        return False
    raise ValueError(f"Некорректное булево значение: {raw!r}")


@dataclass(frozen=True, slots=True)
class AlertSettings:
    """Настройки бота оповещений."""

    telegram_bot_token: str
    telegram_channel_id: str
    geo_scope: str = "region"
    max_item_age_minutes: int = 120
    dedup_cooldown_minutes: int = 45
    media_fallback_cooldown_minutes: int = 20
    state_ttl_hours: int = 12
    initial_post_limit: int = 1
    max_posts_per_cycle: int = 5
    post_delay_seconds: float = 1.5
    include_safety_memo: bool = True
    include_summary: bool = True
    include_source: bool = False
    include_time: bool = False
    disable_notification: bool = False
    disable_clear_notification: bool = False
    timezone: str = "Europe/Moscow"
    db_path: Path = Path("data/alert_bot.db")
    sources_path: Path = Path("alert_sources.json")
    http_timeout_seconds: int = 15
    proxy_url: str | None = None
    log_level: str = "INFO"


def load_alert_settings(
    env_path: Path | None = None,
    *,
    require_telegram: bool = True,
) -> AlertSettings:
    """Загружает и валидирует конфигурацию бота оповещений."""
    if env_path is not None:
        load_dotenv(env_path)
    else:
        for candidate in (Path(".env.alert"), Path(".env")):
            if candidate.is_file():
                load_dotenv(candidate)
                break

    token = (
        os.environ.get("ALERT_TELEGRAM_BOT_TOKEN", "").strip()
        or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    )
    channel_id = (
        os.environ.get("ALERT_TELEGRAM_CHANNEL_ID", "").strip()
        or os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    )

    if require_telegram:
        if not token or token.startswith("123456789:AABB"):
            raise ValueError(
                "Не задан ALERT_TELEGRAM_BOT_TOKEN / TELEGRAM_BOT_TOKEN. "
                "Укажите токен от @BotFather."
            )
        if not channel_id or channel_id in {"@voronezh_news_example", "@voronezh_alerts_example"}:
            raise ValueError(
                "Не задан ALERT_TELEGRAM_CHANNEL_ID / TELEGRAM_CHANNEL_ID. "
                "Укажите @username канала или его ID (-100...)."
            )

    geo_scope = os.environ.get("ALERT_GEO_SCOPE", "region").strip().lower()
    if geo_scope not in VALID_ALERT_GEO_SCOPES:
        raise ValueError(
            f"ALERT_GEO_SCOPE должен быть одним из {sorted(VALID_ALERT_GEO_SCOPES)}, получено {geo_scope!r}"
        )

    proxy = os.environ.get("PROXY_URL", "").strip() or None
    include_source_raw = os.environ.get("ALERT_INCLUDE_SOURCE")
    if include_source_raw is None:
        include_source_raw = os.environ.get("INCLUDE_SOURCE")
    include_time_raw = os.environ.get("ALERT_INCLUDE_TIME")
    if include_time_raw is None:
        include_time_raw = os.environ.get("INCLUDE_TIME")
    include_summary_raw = os.environ.get("ALERT_INCLUDE_SUMMARY")
    if include_summary_raw is None:
        include_summary_raw = os.environ.get("INCLUDE_SUMMARY")

    return AlertSettings(
        telegram_bot_token=token,
        telegram_channel_id=channel_id,
        geo_scope=geo_scope,
        max_item_age_minutes=max(5, int(os.environ.get("ALERT_MAX_ITEM_AGE_MINUTES", "120"))),
        dedup_cooldown_minutes=max(5, int(os.environ.get("ALERT_DEDUP_COOLDOWN_MINUTES", "45"))),
        media_fallback_cooldown_minutes=max(
            1, int(os.environ.get("ALERT_MEDIA_FALLBACK_COOLDOWN_MINUTES", "20"))
        ),
        state_ttl_hours=max(1, int(os.environ.get("ALERT_STATE_TTL_HOURS", "12"))),
        initial_post_limit=max(0, int(os.environ.get("ALERT_INITIAL_POST_LIMIT", "1"))),
        max_posts_per_cycle=max(1, int(os.environ.get("ALERT_MAX_POSTS_PER_CYCLE", "5"))),
        post_delay_seconds=max(0.0, float(os.environ.get("ALERT_POST_DELAY_SECONDS", "1.5"))),
        include_safety_memo=_parse_bool(os.environ.get("ALERT_INCLUDE_SAFETY_MEMO"), True),
        include_summary=_parse_bool(include_summary_raw, True),
        include_source=_parse_bool(include_source_raw, False),
        include_time=_parse_bool(include_time_raw, False),
        disable_notification=_parse_bool(os.environ.get("ALERT_DISABLE_NOTIFICATION"), False),
        disable_clear_notification=_parse_bool(
            os.environ.get("ALERT_DISABLE_CLEAR_NOTIFICATION"), False
        ),
        timezone=os.environ.get("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
        db_path=Path(os.environ.get("ALERT_DB_PATH", "data/alert_bot.db").strip() or "data/alert_bot.db"),
        sources_path=Path(
            os.environ.get("ALERT_SOURCES_PATH", "alert_sources.json").strip() or "alert_sources.json"
        ),
        http_timeout_seconds=max(3, int(os.environ.get("HTTP_TIMEOUT_SECONDS", "15"))),
        proxy_url=proxy,
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )


def load_alert_sources(sources_path: Path | None = None) -> list[AlertSource]:
    """Загружает источники из JSON или возвращает встроенный список."""
    if sources_path is None or not sources_path.is_file():
        return list(DEFAULT_ALERT_SOURCES)

    data = json.loads(sources_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"Файл источников {sources_path} должен содержать JSON-массив")

    sources: list[AlertSource] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        sources.append(
            AlertSource(
                id=str(entry["id"]).strip(),
                name=str(entry["name"]).strip(),
                kind=str(entry.get("kind", "rss")).strip().lower(),
                url=str(entry["url"]).strip(),
                enabled=bool(entry.get("enabled", True)),
                priority=int(entry.get("priority", 10)),
                official=bool(entry.get("official", False)),
            )
        )
    sources.sort(key=lambda item: item.priority)
    return sources
