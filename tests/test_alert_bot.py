"""Интеграционные тесты цикла VoronezhAlertBot с замоканными источниками и Telegram."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from voronezh_alert_bot.bot import VoronezhAlertBot
from voronezh_alert_bot.config import AlertSettings
from voronezh_alert_bot.models import AlertKind, AlertSource, SourceMessage
from voronezh_alert_bot.storage import AlertStorage


class _FakeFetcher:
    def __init__(self, messages: list[SourceMessage]) -> None:
        self.messages = messages

    def fetch_all(self, sources):
        by_source: dict[str, list[SourceMessage]] = {s.id: [] for s in sources if s.enabled}
        for msg in self.messages:
            if msg.source_id in by_source:
                by_source[msg.source_id].append(msg)
        epoch = datetime.fromtimestamp(0, tz=timezone.utc)
        sorted_msgs = sorted(
            [m for m in self.messages if m.source_id in by_source],
            key=lambda m: ((m.published_at or epoch).timestamp(), -m.priority),
            reverse=True,
        )
        return sorted_msgs, by_source, {}


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, AlertKind, tuple[str, ...], str]] = []
        self._seq = 1

    def publish_alert(self, channel_id, candidate, **kwargs) -> int:
        self._seq += 1
        self.sent.append(
            (str(channel_id), candidate.alert.kind, candidate.alert.locations, candidate.message.source_id)
        )
        return self._seq


class AlertBotWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "alerts.db"
        self.storage = AlertStorage(self.db_path)
        self.settings = AlertSettings(
            telegram_bot_token="123:TEST",
            telegram_channel_id="@vrn_alerts",
            geo_scope="region",
            initial_post_limit=1,
            post_delay_seconds=0.0,
            db_path=self.db_path,
        )
        self.sources = [
            AlertSource(
                id="gusev_36",
                name="Александр Гусев (@gusev_36)",
                kind="telegram_web",
                url="https://t.me/s/gusev_36",
                priority=1,
                official=True,
            ),
            AlertSource(
                id="gorcom36",
                name="Горком36",
                kind="rss",
                url="https://gorcom36.ru/rss/",
                priority=10,
                official=False,
            ),
        ]

    def tearDown(self) -> None:
        self.storage.close()
        self.tmpdir.cleanup()

    def test_official_source_wins_over_media_in_same_cycle(self) -> None:
        now = datetime(2026, 11, 26, 20, 0, tzinfo=timezone.utc)
        fetcher = _FakeFetcher([
            SourceMessage(
                source_id="gorcom36",
                source_name="Горком36",
                source_kind="rss",
                official=False,
                priority=10,
                external_id="g1",
                url="https://gorcom36.ru/1",
                title="В Воронежской области объявлена опасность атаки БПЛА",
                text="Губернатор объявил опасность атаки БПЛА.",
                published_at=now - timedelta(minutes=2),
            ),
            SourceMessage(
                source_id="gusev_36",
                source_name="Александр Гусев (@gusev_36)",
                source_kind="telegram_web",
                official=True,
                priority=1,
                external_id="gusev_36/500",
                url="https://t.me/gusev_36/500",
                title="Внимание!",
                text="Внимание! Уважаемые жители Воронежской области, объявлена опасность атаки БПЛА.",
                published_at=now - timedelta(minutes=1),
            ),
        ])
        tg = _FakeTelegram()
        bot = VoronezhAlertBot(self.settings, self.sources, self.storage, fetcher, tg)
        report = bot.run_once(now=now)

        self.assertEqual(report.posted, 1)
        self.assertEqual(len(tg.sent), 1)
        self.assertEqual(tg.sent[0][1], AlertKind.UAV_ALERT)
        self.assertEqual(tg.sent[0][3], "gusev_36")

    def test_full_alert_lifecycle_across_cycles_and_city_scope(self) -> None:
        t0 = datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc)
        tg = _FakeTelegram()
        # Сначала seed: старая запись уже отмечена, база инициализирована
        fetcher = _FakeFetcher([
            SourceMessage(
                source_id="gusev_36",
                source_name="Александр Гусев (@gusev_36)",
                source_kind="telegram_web",
                official=True,
                priority=1,
                external_id="gusev_36/501",
                url="https://t.me/gusev_36/501",
                title="Опасность БПЛА",
                text="На территории Воронежской области объявлена опасность атаки БПЛА.",
                published_at=t0,
            ),
            SourceMessage(
                source_id="gusev_36",
                source_name="Александр Гусев (@gusev_36)",
                source_kind="telegram_web",
                official=True,
                priority=1,
                external_id="gusev_36/502",
                url="https://t.me/gusev_36/502",
                title="Непосредственная угроза",
                text="Внимание! В Лискинском районе объявлена непосредственная угроза удара БПЛА.",
                published_at=t0 + timedelta(minutes=10),
            ),
            SourceMessage(
                source_id="gusev_36",
                source_name="Александр Гусев (@gusev_36)",
                source_kind="telegram_web",
                official=True,
                priority=1,
                external_id="gusev_36/503",
                url="https://t.me/gusev_36/503",
                title="Ракетная опасность",
                text="Внимание! В Воронеже и Воронежской области объявлена ракетная опасность!",
                published_at=t0 + timedelta(minutes=20),
            ),
        ])

        # В city-режиме Лискинский район должен отфильтроваться, а общеобластные и Воронеж — остаться
        city_settings = AlertSettings(
            telegram_bot_token="123:TEST",
            telegram_channel_id="@vrn_alerts",
            geo_scope="city",
            initial_post_limit=5,
            post_delay_seconds=0.0,
            db_path=self.db_path,
        )
        bot = VoronezhAlertBot(city_settings, self.sources, self.storage, fetcher, tg)
        report1 = bot.run_once(now=t0 + timedelta(minutes=25))

        self.assertEqual(report1.geo_filtered, 1)
        self.assertEqual(report1.posted, 2)
        self.assertEqual([item[1] for item in tg.sent], [AlertKind.UAV_ALERT, AlertKind.MISSILE_ALERT])

        # Следующий цикл: отбой ракетной опасности и затем отбой БПЛА
        fetcher.messages.extend([
            SourceMessage(
                source_id="gusev_36",
                source_name="Александр Гусев (@gusev_36)",
                source_kind="telegram_web",
                official=True,
                priority=1,
                external_id="gusev_36/504",
                url="https://t.me/gusev_36/504",
                title="Отбой ракетной",
                text="Отбой ракетной опасности в Воронежской области.",
                published_at=t0 + timedelta(minutes=30),
            ),
            SourceMessage(
                source_id="gusev_36",
                source_name="Александр Гусев (@gusev_36)",
                source_kind="telegram_web",
                official=True,
                priority=1,
                external_id="gusev_36/505",
                url="https://t.me/gusev_36/505",
                title="Отбой БПЛА",
                text="Отбой опасности атаки БПЛА в регионе.",
                published_at=t0 + timedelta(minutes=35),
            ),
        ])
        report2 = bot.run_once(now=t0 + timedelta(minutes=40))
        self.assertEqual(report2.posted, 2)
        self.assertEqual(
            [item[1] for item in tg.sent[2:]],
            [AlertKind.MISSILE_CLEAR, AlertKind.UAV_CLEAR],
        )

    def test_dry_run_does_not_mutate_db(self) -> None:
        now = datetime(2026, 11, 26, 20, 0, tzinfo=timezone.utc)
        fetcher = _FakeFetcher([
            SourceMessage(
                source_id="gusev_36",
                source_name="Александр Гусев (@gusev_36)",
                source_kind="telegram_web",
                official=True,
                priority=1,
                external_id="gusev_36/600",
                url="https://t.me/gusev_36/600",
                title="Ракетная опасность",
                text="В регионе объявлена ракетная опасность!",
                published_at=now,
            )
        ])
        bot = VoronezhAlertBot(self.settings, self.sources, self.storage, fetcher, None)
        report = bot.run_once(dry_run=True, now=now)
        self.assertEqual(report.posted, 1)
        self.assertFalse(self.storage.is_initialized())
        self.assertEqual(self.storage.get_status()["total_seen"], 0)


if __name__ == "__main__":
    unittest.main()
