"""Тесты классификатора оперативных сообщений о БПЛА, ракетной опасности и отбое."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from voronezh_alert_bot.classifier import classify_message
from voronezh_alert_bot.models import AlertKind, SourceMessage


def _tg_msg(text: str, *, source_id: str = "gusev_36", official: bool = True) -> SourceMessage:
    return SourceMessage(
        source_id=source_id,
        source_name="Александр Гусев (@gusev_36)",
        source_kind="telegram_web",
        official=official,
        priority=1,
        external_id=f"{source_id}/100",
        url=f"https://t.me/{source_id}/100",
        title=text.splitlines()[0],
        text=text,
        published_at=datetime(2026, 11, 26, 19, 0, tzinfo=timezone.utc),
    )


def _rss_msg(title: str, text: str = "", *, source_id: str = "gorcom36") -> SourceMessage:
    return SourceMessage(
        source_id=source_id,
        source_name="Горком36",
        source_kind="rss",
        official=False,
        priority=10,
        external_id="https://gorcom36.ru/content/1",
        url="https://gorcom36.ru/content/1",
        title=title,
        text=text,
        published_at=datetime(2026, 11, 26, 19, 0, tzinfo=timezone.utc),
    )


class AlertClassifierTests(unittest.TestCase):
    def test_classifies_uav_alert_and_clear_from_official_tg(self) -> None:
        alert_msg = _tg_msg(
            "Внимание! Уважаемые жители Воронежской области, на территории региона "
            "объявлена опасность атаки БПЛА. Прошу сохранять спокойствие. Силы ПВО наготове."
        )
        res = classify_message(alert_msg)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.kind, AlertKind.UAV_ALERT)
        self.assertTrue(res.is_voronezh_city_or_region_wide)
        self.assertEqual(res.location_key, "region")

        clear_msg = _tg_msg("Внимание! Отбой опасности атаки БПЛА в регионе!")
        res_clear = classify_message(clear_msg)
        self.assertIsNotNone(res_clear)
        assert res_clear is not None
        self.assertEqual(res_clear.kind, AlertKind.UAV_CLEAR)
        self.assertTrue(res_clear.kind.is_clear)

    def test_classifies_missile_alert_and_clear(self) -> None:
        missile = _tg_msg(
            "Внимание! Уважаемые жители Воронежской области, в регионе объявлена ракетная опасность!"
        )
        res = classify_message(missile)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.kind, AlertKind.MISSILE_ALERT)

        clear = _tg_msg("Внимание! Отбой ракетной опасности в регионе.")
        res_clear = classify_message(clear)
        self.assertIsNotNone(res_clear)
        assert res_clear is not None
        self.assertEqual(res_clear.kind, AlertKind.MISSILE_CLEAR)

    def test_classifies_aviation_alert_and_clear(self) -> None:
        av = _tg_msg("В Воронежской области объявлена авиационная опасность.")
        res = classify_message(av)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.kind, AlertKind.AVIATION_ALERT)

        av_clear = _tg_msg("Отбой авиационной опасности в Воронежской области.")
        res_clear = classify_message(av_clear)
        self.assertIsNotNone(res_clear)
        assert res_clear is not None
        self.assertEqual(res_clear.kind, AlertKind.AVIATION_CLEAR)

    def test_immediate_uav_district_vs_city_and_partial_clear(self) -> None:
        district_alert = _rss_msg(
            "Под угрозой прямого удара БПЛА оказались Лискинский и Острогожский районы Воронежской области",
            "Глава Воронежской области Александр Гусев объявил тревогу в Лискинском и Острогожском районах. "
            "Включены системы оповещения.",
        )
        res_dist = classify_message(district_alert)
        self.assertIsNotNone(res_dist)
        assert res_dist is not None
        self.assertEqual(res_dist.kind, AlertKind.UAV_IMMEDIATE_ALERT)
        self.assertFalse(res_dist.is_voronezh_city_or_region_wide)
        self.assertEqual(res_dist.locations, ("Лискинский район", "Острогожский район"))
        self.assertEqual(res_dist.location_key, "liskinskij|ostrogozhskij")

        gorcom_direct = _rss_msg(
            "Бобровский и Бутурлиновский районы Воронежской области подвергаются опасности прямого удара БПЛА",
            (
                "Глава Воронежской области Александр Гусев сообщает о тревоге, "
                "объявленной в Бобровском и Бутурлиновском районах."
            ),
        )
        res_gorcom = classify_message(gorcom_direct)
        self.assertIsNotNone(res_gorcom)
        assert res_gorcom is not None
        self.assertEqual(res_gorcom.kind, AlertKind.UAV_IMMEDIATE_ALERT)
        self.assertEqual(res_gorcom.locations, ("Бобровский район", "Бутурлиновский район"))

        city_alert = _tg_msg(
            "Внимание! В Воронеже объявлена непосредственная угроза удара БПЛА. Включены системы оповещения."
        )
        res_city = classify_message(city_alert)
        self.assertIsNotNone(res_city)
        assert res_city is not None
        self.assertEqual(res_city.kind, AlertKind.UAV_IMMEDIATE_ALERT)
        self.assertTrue(res_city.is_voronezh_city_or_region_wide)
        self.assertIn("Воронеж", res_city.locations)

        partial_clear = _tg_msg(
            "Отмена непосредственной угрозы удара БПЛА в Лискинском и Острогожском районах. "
            "Режим опасности атаки БПЛА сохраняется на всей территории региона!"
        )
        res_part = classify_message(partial_clear)
        self.assertIsNotNone(res_part)
        assert res_part is not None
        self.assertEqual(res_part.kind, AlertKind.UAV_IMMEDIATE_CLEAR)
        self.assertTrue(res_part.uav_general_remains)
        self.assertFalse(res_part.is_voronezh_city_or_region_wide)

    def test_rss_real_world_edge_cases(self) -> None:
        # Баннер о БПЛА в описании посторонней статьи не должен вызывать ложную тревогу
        unrelated_with_banner = _rss_msg(
            "В Воронежской области обсудили 33 кандидатуры на госнаграды",
            "Глава Воронежской области Александр Гусев обращается с предупреждением о том, "
            "что в регионе существует риск использования беспилотников.",
        )
        self.assertIsNone(classify_message(unrelated_with_banner))

        # Заголовок с объявлением отбоя вместе со сводкой сбитых БПЛА должен распознаться как отбой
        combined_clear = _rss_msg(
            "Ликвидировали два БПЛА в Воронежской области и отменили беспилотную опасность",
            "Губернатор Александр Гусев объявил об отмене режима опасности атаки БПЛА в регионе.",
        )
        res_clear = classify_message(combined_clear)
        self.assertIsNotNone(res_clear)
        assert res_clear is not None
        self.assertEqual(res_clear.kind, AlertKind.UAV_CLEAR)

        # Работа систем оповещения при атаке БПЛА -> непосредственная угроза
        tv_alert = _rss_msg(
            "Украинские БПЛА атакуют два района Воронежской области вечером 26 ноября",
            "В муниципалитетах сработали системы оповещения.",
            source_id="tvgubernia",
        )
        res_tv = classify_message(tv_alert)
        self.assertIsNotNone(res_tv)
        assert res_tv is not None
        self.assertEqual(res_tv.kind, AlertKind.UAV_IMMEDIATE_ALERT)

    def test_rejects_retrospective_drills_weather_and_other_regions(self) -> None:
        cases = [
            _rss_msg("Где сбили ночью 26 ноября беспилотники, рассказали воронежцам"),
            _rss_msg("Сирены 11 раз срабатывали в ночь на 26 ноября в Воронежской области", "Прошла тревожная ночь."),
            _rss_msg("Последствия налёта БПЛА и смерть полицейского. Главные новости Воронежа за 26 ноября"),
            _rss_msg("Частные дома и хозпостройки повредили дроны в Воронежской области"),
            _rss_msg("Воронежцев предупредили об опасности выхода на лед"),
            _rss_msg("В Воронеже пройдет плановая проверка систем оповещения с запуском сирен"),
            _tg_msg("В Белгородской области объявлена ракетная опасность", source_id="some_channel", official=False),
            _rss_msg("В Курской области объявили отбой опасности атаки БПЛА"),
            _tg_msg("Напоминаем: опасность атаки БПЛА в регионе сохраняется, будьте внимательны."),
        ]
        for msg in cases:
            with self.subTest(msg=msg.title):
                self.assertIsNone(classify_message(msg))

    def test_classifies_post_clear_pvo_summary_when_enabled(self) -> None:
        official_summary = _tg_msg(
            "За прошедшую ночь дежурными силами ПВО на территории Воронежской области "
            "обнаружены и уничтожены более 10 беспилотников. По предварительным данным, "
            "пострадавших и разрушений нет."
        )
        res = classify_message(official_summary, include_summary=True)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.kind, AlertKind.UAV_SUMMARY)
        self.assertIsNone(classify_message(official_summary, include_summary=False))

        # Отбой с упоминанием сбитых БПЛА всё равно классифицируется как отбой, а не сводка
        combined_clear = _tg_msg(
            "Внимание! Отбой опасности атаки БПЛА в регионе. "
            "За ночь дежурными силами ПВО уничтожены 5 беспилотников, пострадавших нет."
        )
        res_clear = classify_message(combined_clear, include_summary=True)
        self.assertIsNotNone(res_clear)
        assert res_clear is not None
        self.assertEqual(res_clear.kind, AlertKind.UAV_CLEAR)

        # Общероссийская сводка Минобороны без Воронежской области не должна попадать как сводка
        federal_clickbait = _rss_msg(
            "Где сбили ночью 26 ноября беспилотники, рассказали воронежцам",
            "В течение прошедшей ночи дежурными силами ПВО были уничтожены 343 беспилотника, отчитались в Минобороны.",
        )
        self.assertIsNone(classify_message(federal_clickbait, include_summary=True))


if __name__ == "__main__":
    unittest.main()
