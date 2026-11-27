"""Тесты SQLite-хранилища статусов угроз и антидублирования оповещений."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from voronezh_alert_bot.models import AlertKind, ClassifiedAlert, SourceMessage
from voronezh_alert_bot.storage import AlertStorage


def _make_msg(source_id: str, ext_id: str, *, official: bool, published_at: datetime) -> SourceMessage:
    return SourceMessage(
        source_id=source_id,
        source_name=source_id,
        source_kind="telegram_web" if official else "rss",
        official=official,
        priority=1 if official else 10,
        external_id=ext_id,
        url=f"https://example.test/{ext_id}",
        title="Alert",
        text="Alert",
        published_at=published_at,
    )


class AlertStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "alert_test.db"
        self.storage = AlertStorage(self.db_path)

    def tearDown(self) -> None:
        self.storage.close()
        self.tmpdir.cleanup()

    def test_uav_state_lifecycle_and_cross_source_dedup(self) -> None:
        t0 = datetime(2026, 11, 26, 19, 0, tzinfo=timezone.utc)
        alert = ClassifiedAlert(
            kind=AlertKind.UAV_ALERT,
            locations=("Воронеж и область",),
            location_key="region",
            is_voronezh_city_or_region_wide=True,
        )
        m1 = _make_msg("gusev_36", "1", official=True, published_at=t0)
        self.assertFalse(self.storage.should_suppress_duplicate(m1, alert, now=t0))
        self.storage.apply_published_event(m1, alert, now=t0)
        self.assertTrue(self.storage.is_family_active("uav", now=t0))

        # Повтор от МЧС или СМИ пока тревога активна должен подавляться
        m2 = _make_msg("mchs36", "2", official=True, published_at=t0 + timedelta(minutes=2))
        self.assertTrue(
            self.storage.should_suppress_duplicate(m2, alert, now=t0 + timedelta(minutes=2))
        )

        # Отбой снимает активность
        clear_alert = ClassifiedAlert(
            kind=AlertKind.UAV_CLEAR,
            locations=("Воронеж и область",),
            location_key="region",
            is_voronezh_city_or_region_wide=True,
        )
        m3 = _make_msg("gusev_36", "3", official=True, published_at=t0 + timedelta(hours=3))
        self.assertEqual(
            self.storage.get_active_duration_seconds("uav", t0 + timedelta(hours=3)),
            3 * 3600,
        )
        self.assertFalse(
            self.storage.should_suppress_duplicate(m3, clear_alert, now=t0 + timedelta(hours=3))
        )
        self.storage.apply_published_event(m3, clear_alert, now=t0 + timedelta(hours=3))
        self.assertFalse(self.storage.is_family_active("uav", now=t0 + timedelta(hours=3)))
        self.assertIsNone(self.storage.get_active_duration_seconds("uav", t0 + timedelta(hours=4)))

        # Повторный отбой из другого источника подавляется
        m4 = _make_msg("gorcom36", "4", official=False, published_at=t0 + timedelta(hours=3, minutes=5))
        self.assertTrue(
            self.storage.should_suppress_duplicate(
                m4, clear_alert, now=t0 + timedelta(hours=3, minutes=5)
            )
        )

        # Следующая тревога на следующий вечер снова публикуется
        m5 = _make_msg("gusev_36", "5", official=True, published_at=t0 + timedelta(hours=24))
        self.assertFalse(
            self.storage.should_suppress_duplicate(m5, alert, now=t0 + timedelta(hours=24))
        )

    def test_immediate_uav_allows_different_districts_and_suppresses_same(self) -> None:
        t0 = datetime(2026, 11, 26, 20, 0, tzinfo=timezone.utc)
        dist1 = ClassifiedAlert(
            kind=AlertKind.UAV_IMMEDIATE_ALERT,
            locations=("Лискинский район",),
            location_key="liskinskij",
            is_voronezh_city_or_region_wide=False,
        )
        dist2 = ClassifiedAlert(
            kind=AlertKind.UAV_IMMEDIATE_ALERT,
            locations=("Россошанский район",),
            location_key="rossoshanskij",
            is_voronezh_city_or_region_wide=False,
        )
        vague_media = ClassifiedAlert(
            kind=AlertKind.UAV_IMMEDIATE_ALERT,
            locations=("Воронеж и область",),
            location_key="region",
            is_voronezh_city_or_region_wide=True,
        )

        m1 = _make_msg("gusev_36", "10", official=True, published_at=t0)
        self.storage.apply_published_event(m1, dist1, now=t0)

        # Другой район — публикуется
        m2 = _make_msg("gusev_36", "11", official=True, published_at=t0 + timedelta(minutes=10))
        self.assertFalse(
            self.storage.should_suppress_duplicate(m2, dist2, now=t0 + timedelta(minutes=10))
        )

        # Неофициальный дубль без указания района в окне 20 минут — подавляется
        m3 = _make_msg("tvgubernia", "12", official=False, published_at=t0 + timedelta(minutes=12))
        self.assertTrue(
            self.storage.should_suppress_duplicate(m3, vague_media, now=t0 + timedelta(minutes=12))
        )


if __name__ == "__main__":
    unittest.main()
