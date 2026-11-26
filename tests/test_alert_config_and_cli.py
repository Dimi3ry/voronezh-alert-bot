"""Тесты конфигурации, форматирования сообщений и CLI для voronezh_alert_bot."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from voronezh_alert_bot.cli import main
from voronezh_alert_bot.config import load_alert_settings, load_alert_sources
from voronezh_alert_bot.models import AlertCandidate, AlertKind, ClassifiedAlert, SourceMessage
from voronezh_alert_bot.telegram_client import format_alert_post


class AlertConfigAndCLITests(unittest.TestCase):
    def test_load_alert_settings_prefers_alert_prefix_with_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env.alert"
            env_file.write_text(
                "\n".join([
                    "TELEGRAM_BOT_TOKEN=111:BASE",
                    "TELEGRAM_CHANNEL_ID=@base_ch",
                    "ALERT_TELEGRAM_BOT_TOKEN=222:ALERT",
                    "ALERT_GEO_SCOPE=city",
                    "ALERT_MAX_ITEM_AGE_MINUTES=90",
                ]),
                encoding="utf-8",
            )
            keys = (
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHANNEL_ID",
                "ALERT_TELEGRAM_BOT_TOKEN",
                "ALERT_TELEGRAM_CHANNEL_ID",
                "ALERT_GEO_SCOPE",
                "ALERT_MAX_ITEM_AGE_MINUTES",
            )
            clean = {k: v for k, v in os.environ.items() if k not in keys}
            with patch.dict(os.environ, clean, clear=True):
                settings = load_alert_settings(env_file)
                self.assertEqual(settings.telegram_bot_token, "222:ALERT")
                self.assertEqual(settings.telegram_channel_id, "@base_ch")
                self.assertEqual(settings.geo_scope, "city")
                self.assertEqual(settings.max_item_age_minutes, 90)

    def test_format_alert_post_escapes_html_and_shows_partial_clear_note(self) -> None:
        cand = AlertCandidate(
            message=SourceMessage(
                source_id="gusev_36",
                source_name="Александр Гусев <@gusev_36>",
                source_kind="telegram_web",
                official=True,
                priority=1,
                external_id="gusev_36/700",
                url="https://t.me/gusev_36/700",
                title="Отмена угрозы",
                text="Отмена непосредственной угрозы удара БПЛА & ракетной <проверки>.",
                published_at=datetime(2026, 11, 26, 18, 30, tzinfo=timezone.utc),
            ),
            alert=ClassifiedAlert(
                kind=AlertKind.UAV_IMMEDIATE_CLEAR,
                locations=("Лискинский район",),
                location_key="liskinskij",
                is_voronezh_city_or_region_wide=False,
                uav_general_remains=True,
                excerpt="Отмена непосредственной угрозы удара БПЛА & сохраняется общий режим.",
            ),
        )
        rendered = format_alert_post(cand, tz_name="Europe/Moscow", include_safety_memo=True)
        self.assertLessEqual(len(rendered), 4096)
        self.assertIn("ОТМЕНА НЕПОСРЕДСТВЕННОЙ УГРОЗЫ БПЛА", rendered)
        self.assertIn("Общий режим опасности атаки БПЛА в регионе сохраняется.", rendered)
        self.assertIn("Александр Гусев &lt;@gusev_36&gt;", rendered)
        self.assertIn("26.11.2026 21:30", rendered)

    def test_load_default_sources_and_cli_dry_run(self) -> None:
        sources = load_alert_sources(Path("alert_sources.json"))
        self.assertGreaterEqual(len(sources), 3)
        self.assertEqual(sources[0].id, "gusev_36")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alert_dry.db"
            sources_path = Path(tmp) / "alert_sources.json"
            sources_path.write_text("[]", encoding="utf-8")
            with patch.dict(os.environ, {"ALERT_DB_PATH": str(db_path)}, clear=False):
                rc_dry = main([
                    "--dry-run",
                    "--sources", str(sources_path),
                    "--env", str(Path(tmp) / "missing.env"),
                ])
                rc_status = main([
                    "--status",
                    "--sources", str(sources_path),
                    "--env", str(Path(tmp) / "missing.env"),
                ])
            self.assertEqual(rc_dry, 0)
            self.assertEqual(rc_status, 0)


if __name__ == "__main__":
    unittest.main()
