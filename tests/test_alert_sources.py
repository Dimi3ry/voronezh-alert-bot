"""Тесты парсера публичных Telegram-каналов (t.me/s) и RSS-лент."""

from __future__ import annotations

import unittest

from voronezh_alert_bot.models import AlertSource
from voronezh_alert_bot.sources_fetcher import parse_rss_xml, parse_telegram_web_html


class AlertSourcesParserTests(unittest.TestCase):
    def test_parse_telegram_web_html_ignores_reply_and_service_messages(self) -> None:
        src = AlertSource(
            id="gusev_36",
            name="Александр Гусев (@gusev_36)",
            kind="telegram_web",
            url="https://t.me/s/gusev_36",
            priority=1,
            official=True,
        )
        html_doc = """
        <html><body>
        <div class="tgme_widget_message service_message">Pinned message</div>
        <div class="tgme_widget_message_wrap">
          <div class="tgme_widget_message text_not_supported_wrap" data-post="gusev_36/4501">
            <div class="tgme_widget_message_bubble">
              <a class="tgme_widget_message_reply">
                <div class="tgme_widget_message_text">Старый текст в цитате ответа</div>
              </a>
              <div class="tgme_widget_message_text" dir="auto">
                Внимание! Уважаемые жители Воронежской области!<br/>
                На территории региона объявлена опасность атаки БПЛА.<wbr/> Силы ПВО наготове.
              </div>
              <div class="tgme_widget_message_footer">
                <a class="tgme_widget_message_date" href="https://t.me/gusev_36/4501">
                  <time datetime="2026-11-26T18:58:12+00:00" class="time">21:58</time>
                </a>
              </div>
            </div>
          </div>
        </div>
        </body></html>
        """
        msgs = parse_telegram_web_html(html_doc, src)
        self.assertEqual(len(msgs), 1)
        msg = msgs[0]
        self.assertEqual(msg.external_id, "gusev_36/4501")
        self.assertEqual(msg.url, "https://t.me/gusev_36/4501")
        self.assertNotIn("Старый текст в цитате", msg.text)
        self.assertIn("объявлена опасность атаки БПЛА", msg.text)
        self.assertIsNotNone(msg.published_at)
        assert msg.published_at is not None
        self.assertEqual(msg.published_at.hour, 18)

    def test_parse_rss_xml(self) -> None:
        src = AlertSource(
            id="gorcom36",
            name="Горком36",
            kind="rss",
            url="https://gorcom36.ru/rss/",
            priority=10,
            official=False,
        )
        xml_bytes = """<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>В Воронежской области объявлена ракетная опасность</title>
              <link>https://gorcom36.ru/content/raketa</link>
              <description>&lt;p&gt;Включены сирены оповещения.&lt;/p&gt;</description>
              <pubDate>Thu, 26 Nov 2026 22:10:00 +0300</pubDate>
            </item>
          </channel>
        </rss>""".encode("utf-8")
        msgs = parse_rss_xml(xml_bytes, src)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].title, "В Воронежской области объявлена ракетная опасность")
        self.assertEqual(msgs[0].text, "Включены сирены оповещения.")
        self.assertIsNotNone(msgs[0].published_at)


if __name__ == "__main__":
    unittest.main()
