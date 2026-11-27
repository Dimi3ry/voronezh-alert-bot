"""SQLite-хранилище истории оповещений, активных статусов угроз и здоровья источников."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import AlertKind, ClassifiedAlert, SourceMessage


class AlertStorage:
    """Потокобезопасное хранилище состояния бота оповещений в SQLite."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        if str(db_path) != ":memory:":
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedup_key TEXT NOT NULL UNIQUE,
                    source_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    alert_kind TEXT,
                    location_key TEXT,
                    excerpt TEXT,
                    published_at TEXT,
                    created_at TEXT NOT NULL,
                    posted INTEGER NOT NULL DEFAULT 0,
                    telegram_message_id INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_alert_seen_source ON seen_messages(source_id, url);
                CREATE INDEX IF NOT EXISTS idx_alert_seen_created ON seen_messages(created_at);

                CREATE TABLE IF NOT EXISTS threat_state (
                    family TEXT PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 0,
                    last_kind TEXT,
                    active_locations_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    posted_at TEXT NOT NULL,
                    last_source_id TEXT
                );

                CREATE TABLE IF NOT EXISTS recent_alert_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_kind TEXT NOT NULL,
                    location_key TEXT NOT NULL,
                    official INTEGER NOT NULL DEFAULT 0,
                    event_at TEXT NOT NULL,
                    posted_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_recent_events_lookup
                    ON recent_alert_events(alert_kind, posted_at);

                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS source_health (
                    source_id TEXT PRIMARY KEY,
                    last_ok_at TEXT,
                    last_error_at TEXT,
                    last_error TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_item_count INTEGER NOT NULL DEFAULT 0,
                    total_posted INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    def _get_state(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM bot_state WHERE key = ?",
                (key,),
            ).fetchone()
            return str(row["value"]) if row else default

    def _set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bot_state(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    def is_initialized(self) -> bool:
        if self._get_state("initialized") == "1":
            return True
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM seen_messages LIMIT 1").fetchone()
            return row is not None

    def mark_initialized(self) -> None:
        self._set_state("initialized", "1")

    def set_last_poll(self, summary: dict[str, Any]) -> None:
        self._set_state("last_poll_at", datetime.now(timezone.utc).isoformat())
        self._set_state("last_poll_summary", json.dumps(summary, ensure_ascii=False))

    def has_seen(self, message: SourceMessage) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM seen_messages
                WHERE dedup_key = ?
                   OR (url != '' AND source_id = ? AND url = ?)
                LIMIT 1
                """,
                (message.dedup_key, message.source_id, message.url),
            ).fetchone()
            return row is not None

    def record_message(
        self,
        message: SourceMessage,
        *,
        alert: ClassifiedAlert | None = None,
        posted: bool = False,
        telegram_message_id: int | None = None,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        pub_iso = message.published_at.isoformat() if message.published_at else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO seen_messages(
                    dedup_key, source_id, url, alert_kind, location_key,
                    excerpt, published_at, created_at, posted, telegram_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dedup_key) DO UPDATE SET
                    posted = MAX(seen_messages.posted, excluded.posted),
                    telegram_message_id = COALESCE(
                        excluded.telegram_message_id, seen_messages.telegram_message_id
                    )
                """,
                (
                    message.dedup_key,
                    message.source_id,
                    message.url,
                    alert.kind.value if alert else None,
                    alert.location_key if alert else None,
                    alert.excerpt[:300] if alert else message.title[:300],
                    pub_iso,
                    now_iso,
                    1 if posted else 0,
                    telegram_message_id,
                ),
            )
            if posted:
                self._conn.execute(
                    """
                    INSERT INTO source_health(source_id, total_posted)
                    VALUES (?, 1)
                    ON CONFLICT(source_id) DO UPDATE SET
                        total_posted = source_health.total_posted + 1
                    """,
                    (message.source_id,),
                )
            self._conn.commit()

    def is_family_active(
        self,
        family: str,
        *,
        state_ttl_hours: int = 12,
        now: datetime | None = None,
    ) -> bool:
        """Возвращает True, если угроза данного семейства сейчас активна и не устарела по TTL."""
        ref_now = now or datetime.now(timezone.utc)
        with self._lock:
            row = self._conn.execute(
                "SELECT active, updated_at FROM threat_state WHERE family = ?",
                (family,),
            ).fetchone()
            if not row or not int(row["active"]):
                return False
            try:
                updated_at = datetime.fromisoformat(str(row["updated_at"]))
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                return (ref_now - updated_at) <= timedelta(hours=state_ttl_hours)
            except ValueError:
                return True

    def has_recent_event(
        self,
        kind: AlertKind,
        *,
        location_key: str | None = None,
        within_minutes: int = 45,
        now: datetime | None = None,
    ) -> bool:
        """Проверяет, публиковалось ли аналогичное событие недавно (антидубль между источниками)."""
        ref_now = now or datetime.now(timezone.utc)
        cutoff = (ref_now - timedelta(minutes=within_minutes)).isoformat()
        with self._lock:
            if location_key is not None:
                row = self._conn.execute(
                    """
                    SELECT 1 FROM recent_alert_events
                    WHERE alert_kind = ? AND location_key = ? AND posted_at >= ?
                    LIMIT 1
                    """,
                    (kind.value, location_key, cutoff),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """
                    SELECT 1 FROM recent_alert_events
                    WHERE alert_kind = ? AND posted_at >= ?
                    LIMIT 1
                    """,
                    (kind.value, cutoff),
                ).fetchone()
            return row is not None

    def should_suppress_duplicate(
        self,
        message: SourceMessage,
        alert: ClassifiedAlert,
        *,
        dedup_cooldown_minutes: int = 45,
        media_fallback_cooldown_minutes: int = 20,
        state_ttl_hours: int = 12,
        now: datetime | None = None,
    ) -> bool:
        """Определяет, является ли событие повтором уже опубликованного статуса/тревоги."""
        ref_now = now or datetime.now(timezone.utc)
        kind = alert.kind
        family = kind.threat_family

        # 1. Точный повтор того же типа и территории в окне охлаждения
        if self.has_recent_event(
            kind,
            location_key=alert.location_key,
            within_minutes=dedup_cooldown_minutes,
            now=ref_now,
        ):
            return True

        # 2. Неофициальное СМИ не должно дублировать недавно опубликованный тот же тип события
        if not message.official and self.has_recent_event(
            kind,
            location_key=None,
            within_minutes=media_fallback_cooldown_minutes,
            now=ref_now,
        ):
            return True

        # 3. Для общеобластных/городских тревог (UAV/Missile/Aviation):
        #    повторное введение того же режима, пока он уже активен, подавляется.
        if kind in {AlertKind.UAV_ALERT, AlertKind.MISSILE_ALERT, AlertKind.AVIATION_ALERT}:
            if self.is_family_active(family, state_ttl_hours=state_ttl_hours, now=ref_now):
                return True

        # 4. Для отбоя общеобластных режимов подавляем повтор в окне охлаждения вне зависимости от location_key
        if kind in {AlertKind.UAV_CLEAR, AlertKind.MISSILE_CLEAR, AlertKind.AVIATION_CLEAR}:
            if self.has_recent_event(
                kind,
                location_key=None,
                within_minutes=dedup_cooldown_minutes,
                now=ref_now,
            ):
                return True

        return False

    def apply_published_event(
        self,
        message: SourceMessage,
        alert: ClassifiedAlert,
        *,
        now: datetime | None = None,
    ) -> None:
        """Обновляет текущее состояние угроз и журнал недавних публикаций."""
        ref_now = now or datetime.now(timezone.utc)
        event_iso = (message.published_at or ref_now).isoformat()
        posted_iso = ref_now.isoformat()
        kind = alert.kind
        family = kind.threat_family

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO recent_alert_events(alert_kind, location_key, official, event_at, posted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    kind.value,
                    alert.location_key,
                    1 if message.official else 0,
                    event_iso,
                    posted_iso,
                ),
            )

            active_flag = 0 if kind.is_clear else 1
            locations_json = json.dumps(list(alert.locations), ensure_ascii=False)
            self._conn.execute(
                """
                INSERT INTO threat_state(
                    family, active, last_kind, active_locations_json, updated_at, posted_at, last_source_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(family) DO UPDATE SET
                    active = excluded.active,
                    last_kind = excluded.last_kind,
                    active_locations_json = excluded.active_locations_json,
                    updated_at = excluded.updated_at,
                    posted_at = excluded.posted_at,
                    last_source_id = excluded.last_source_id
                """,
                (
                    family,
                    active_flag,
                    kind.value,
                    locations_json,
                    event_iso,
                    posted_iso,
                    message.source_id,
                ),
            )

            # Полный отбой БПЛА снимает также непосредственную угрозу БПЛА
            if kind == AlertKind.UAV_CLEAR:
                self._conn.execute(
                    """
                    INSERT INTO threat_state(
                        family, active, last_kind, active_locations_json, updated_at, posted_at, last_source_id
                    ) VALUES ('uav_immediate', 0, ?, '[]', ?, ?, ?)
                    ON CONFLICT(family) DO UPDATE SET
                        active = 0,
                        last_kind = excluded.last_kind,
                        active_locations_json = '[]',
                        updated_at = excluded.updated_at,
                        posted_at = excluded.posted_at,
                        last_source_id = excluded.last_source_id
                    """,
                    (kind.value, event_iso, posted_iso, message.source_id),
                )

            # Подчищаем события старше 48 часов
            prune_cutoff = (ref_now - timedelta(hours=48)).isoformat()
            self._conn.execute(
                "DELETE FROM recent_alert_events WHERE posted_at < ?",
                (prune_cutoff,),
            )
            self._conn.commit()

    def record_source_success(self, source_id: str, item_count: int) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO source_health(
                    source_id, last_ok_at, last_error, consecutive_failures, last_item_count
                ) VALUES (?, ?, NULL, 0, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_ok_at = excluded.last_ok_at,
                    last_error = NULL,
                    consecutive_failures = 0,
                    last_item_count = excluded.last_item_count
                """,
                (source_id, now_iso, item_count),
            )
            self._conn.commit()

    def record_source_error(self, source_id: str, error: str) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO source_health(
                    source_id, last_error_at, last_error, consecutive_failures
                ) VALUES (?, ?, ?, 1)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_error_at = excluded.last_error_at,
                    last_error = excluded.last_error,
                    consecutive_failures = source_health.consecutive_failures + 1
                """,
                (source_id, now_iso, error[:300]),
            )
            self._conn.commit()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            counts = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total_seen,
                    COALESCE(SUM(CASE WHEN posted = 1 THEN 1 ELSE 0 END), 0) AS total_posted
                FROM seen_messages
                """
            ).fetchone()
            states = self._conn.execute(
                "SELECT family, active, last_kind, active_locations_json, updated_at FROM threat_state"
            ).fetchall()
            health = self._conn.execute(
                """
                SELECT source_id, last_ok_at, last_error_at, last_error,
                       consecutive_failures, last_item_count, total_posted
                FROM source_health
                ORDER BY source_id
                """
            ).fetchall()

        return {
            "initialized": self.is_initialized(),
            "total_seen": int(counts["total_seen"]) if counts else 0,
            "total_posted": int(counts["total_posted"]) if counts else 0,
            "last_poll_at": self._get_state("last_poll_at"),
            "threats": {
                str(r["family"]): {
                    "active": bool(r["active"]),
                    "last_kind": r["last_kind"],
                    "locations": json.loads(r["active_locations_json"] or "[]"),
                    "updated_at": r["updated_at"],
                }
                for r in states
            },
            "sources": [dict(r) for r in health],
        }
