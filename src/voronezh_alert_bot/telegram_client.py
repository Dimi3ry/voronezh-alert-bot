"""Форматирование тревог/отбоев и отправка сообщений в Telegram-канал."""

from __future__ import annotations

import html
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .models import AlertCandidate, AlertKind

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

_EVENT_META: dict[AlertKind, tuple[str, str, tuple[str, ...]]] = {
    AlertKind.MISSILE_ALERT: (
        "🚨",
        "РАКЕТНАЯ ОПАСНОСТЬ",
        ("#Воронеж", "#РакетнаяОпасность"),
    ),
    AlertKind.MISSILE_CLEAR: (
        "🟢",
        "ОТБОЙ РАКЕТНОЙ ОПАСНОСТИ",
        ("#Воронеж", "#Отбой", "#РакетнаяОпасность"),
    ),
    AlertKind.UAV_IMMEDIATE_ALERT: (
        "🔴",
        "НЕПОСРЕДСТВЕННАЯ УГРОЗА УДАРА БПЛА",
        ("#Воронеж", "#БПЛА", "#УгрозаУдара"),
    ),
    AlertKind.UAV_IMMEDIATE_CLEAR: (
        "🟡",
        "ОТМЕНА НЕПОСРЕДСТВЕННОЙ УГРОЗЫ БПЛА",
        ("#Воронеж", "#БПЛА", "#ОтменаУгрозы"),
    ),
    AlertKind.UAV_ALERT: (
        "⚠️",
        "ОПАСНОСТЬ АТАКИ БПЛА",
        ("#Воронеж", "#БПЛА"),
    ),
    AlertKind.UAV_CLEAR: (
        "✅",
        "ОТБОЙ ОПАСНОСТИ АТАКИ БПЛА",
        ("#Воронеж", "#БПЛА", "#Отбой"),
    ),
    AlertKind.AVIATION_ALERT: (
        "✈️",
        "АВИАЦИОННАЯ ОПАСНОСТЬ",
        ("#Воронеж", "#АвиационнаяОпасность"),
    ),
    AlertKind.AVIATION_CLEAR: (
        "🟢",
        "ОТБОЙ АВИАЦИОННОЙ ОПАСНОСТИ",
        ("#Воронеж", "#Отбой", "#АвиационнаяОпасность"),
    ),
}


def format_alert_post(
    candidate: AlertCandidate,
    *,
    tz_name: str = "Europe/Moscow",
    include_safety_memo: bool = True,
    include_source: bool = False,
    include_time: bool = False,
    max_length: int = 4096,
) -> str:
    """Формирует HTML-сообщение об опасности или отбое для Telegram-канала."""
    alert = candidate.alert
    msg = candidate.message
    emoji, headline, tags = _EVENT_META[alert.kind]

    parts: list[str] = [f"{emoji} <b>{html.escape(headline, quote=False)}</b>"]

    locations_str = ", ".join(alert.locations) if alert.locations else "Воронеж и область"
    parts.append(f"📍 <b>Территория:</b> {html.escape(locations_str, quote=False)}")

    if alert.kind == AlertKind.UAV_IMMEDIATE_CLEAR and alert.uav_general_remains:
        parts.append("ℹ️ <i>Общий режим опасности атаки БПЛА в регионе сохраняется.</i>")

    excerpt_idx: int | None = None
    if alert.excerpt:
        excerpt_idx = len(parts)
        parts.append(html.escape(alert.excerpt, quote=False))

    if include_safety_memo and not alert.kind.is_clear:
        if alert.kind in {AlertKind.MISSILE_ALERT, AlertKind.UAV_IMMEDIATE_ALERT}:
            parts.append(
                "🛡 <i>Отойдите от окон, перейдите в помещение со сплошными стенами "
                "(коридор, ванная, кладовая) или в ближайшее укрытие.</i>"
            )
        else:
            parts.append(
                "🛡 <i>Сохраняйте спокойствие, следите за официальными сообщениями "
                "властей и МЧС, при сирене укройтесь вдали от окон.</i>"
            )

    time_only = ""
    if include_time and msg.published_at is not None:
        try:
            dt_local = msg.published_at.astimezone(ZoneInfo(tz_name))
            time_only = dt_local.strftime("%d.%m.%Y %H:%M")
        except Exception:
            time_only = ""

    tags_line = " ".join(tags)
    if include_source:
        source_escaped = html.escape(msg.source_name, quote=False)
        time_suffix = f" • {time_only}" if time_only else ""
        if msg.url:
            url_escaped = html.escape(msg.url, quote=True)
            source_line = (
                f'🔗 <a href="{url_escaped}">{source_escaped}</a>'
                f"{html.escape(time_suffix, quote=False)}"
            )
        else:
            source_line = f"🔗 {source_escaped}{html.escape(time_suffix, quote=False)}"
        parts.append(f"{tags_line}\n{source_line}")
    else:
        if time_only:
            parts.append(f"{tags_line} • {html.escape(time_only, quote=False)}")
        else:
            parts.append(tags_line)

    rendered = "\n\n".join(parts)
    if len(rendered) <= max_length:
        return rendered

    # При редком превышении лимита укорачиваем только цитату (excerpt)
    if excerpt_idx is not None:
        overhead = len(rendered) - len(parts[excerpt_idx])
        budget = max(80, max_length - overhead - 8)
        short_excerpt = alert.excerpt[:budget].rstrip() + "…"
        parts[excerpt_idx] = html.escape(short_excerpt, quote=False)
    return "\n\n".join(parts)[:max_length]


class TelegramError(RuntimeError):
    """Ошибка ответа Telegram Bot API."""

    def __init__(
        self,
        description: str,
        *,
        code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(description)
        self.code = code
        self.description = description
        self.retry_after = retry_after


class TelegramAlertClient:
    """Клиент Telegram Bot API для публикации оповещений в канал."""

    def __init__(
        self,
        token: str,
        *,
        timeout: int = 15,
        proxy_url: str | None = None,
        session: requests.Session | None = None,
        api_base: str = TELEGRAM_API_BASE,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.api_base = api_base.rstrip("/")
        self._session = session or requests.Session()
        if proxy_url and session is None:
            self._session.proxies.update({"http": proxy_url, "https": proxy_url})

    def _endpoint(self, method: str) -> str:
        return f"{self.api_base}/bot{self.token}/{method}"

    def _request(
        self,
        method: str,
        *,
        data: dict[str, Any] | None = None,
        timeout: int | None = None,
        max_attempts: int = 3,
    ) -> Any:
        url = self._endpoint(method)
        req_timeout = timeout or self.timeout
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                resp = self._session.post(url, data=data, timeout=req_timeout)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < max_attempts:
                    time.sleep(min(2 * attempt, 5))
                    continue
                raise TelegramError(f"Сетевая ошибка Telegram API ({method}): {exc}") from exc

            try:
                payload = resp.json()
            except ValueError as exc:
                if resp.status_code >= 500 and attempt < max_attempts:
                    time.sleep(min(2 * attempt, 5))
                    continue
                raise TelegramError(
                    f"Некорректный JSON от Telegram API ({method}): HTTP {resp.status_code}",
                    code=resp.status_code,
                ) from exc

            if payload.get("ok"):
                return payload.get("result")

            code = int(payload.get("error_code") or resp.status_code or 0)
            description = str(payload.get("description") or f"HTTP {resp.status_code}")
            params = payload.get("parameters") or {}
            retry_after = params.get("retry_after")

            if code == 429 and retry_after is not None and attempt < max_attempts:
                wait_seconds = min(int(retry_after) + 1, 60)
                logger.warning("Telegram rate limit (429), пауза %s сек.", wait_seconds)
                time.sleep(wait_seconds)
                continue

            if code >= 500 and attempt < max_attempts:
                time.sleep(min(2 * attempt, 5))
                continue

            raise TelegramError(
                description,
                code=code,
                retry_after=int(retry_after) if retry_after is not None else None,
            )

        raise TelegramError(f"Не удалось выполнить {method}: {last_exc}")

    def get_me(self) -> dict[str, Any]:
        return dict(self._request("getMe"))

    def get_chat(self, chat_id: str | int) -> dict[str, Any]:
        return dict(self._request("getChat", data={"chat_id": str(chat_id)}))

    def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        disable_web_page_preview: bool = True,
        disable_notification: bool = False,
    ) -> int:
        result = self._request(
            "sendMessage",
            data={
                "chat_id": str(chat_id),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true" if disable_web_page_preview else "false",
                "disable_notification": "true" if disable_notification else "false",
            },
        )
        return int(result["message_id"])

    def publish_alert(
        self,
        channel_id: str | int,
        candidate: AlertCandidate,
        *,
        tz_name: str = "Europe/Moscow",
        include_safety_memo: bool = True,
        include_source: bool = False,
        include_time: bool = False,
        disable_notification: bool = False,
    ) -> int:
        text = format_alert_post(
            candidate,
            tz_name=tz_name,
            include_safety_memo=include_safety_memo,
            include_source=include_source,
            include_time=include_time,
        )
        return self.send_message(
            channel_id,
            text,
            disable_web_page_preview=True,
            disable_notification=disable_notification,
        )
