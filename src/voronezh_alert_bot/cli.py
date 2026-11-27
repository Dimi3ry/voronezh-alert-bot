"""CLI-интерфейс бота оповещений о БПЛА, ракетной опасности и отбое в Воронеже."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

from .bot import VoronezhAlertBot
from .config import load_alert_settings, load_alert_sources
from .sources_fetcher import AlertSourcesFetcher
from .storage import AlertStorage
from .telegram_client import TelegramAlertClient, TelegramError, format_alert_post


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voronezh-alert-bot",
        description="Telegram-бот оповещений об атаке БПЛА, ракетной опасности и отбое в Воронеже",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=None,
        help="Путь к .env файлу",
    )
    parser.add_argument(
        "--sources",
        type=Path,
        default=None,
        help="Путь к JSON-файлу со списком источников",
    )
    parser.add_argument(
        "--scope",
        choices=("region", "city"),
        default=None,
        help="Переопределить ALERT_GEO_SCOPE (region или city)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Подробное логирование (DEBUG)",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Выполнить один цикл проверки и публикации (режим по умолчанию для GitHub Actions)",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Проверить источники и показать распознанные тревоги без отправки в Telegram и без записи в БД",
    )
    mode.add_argument(
        "--seed",
        action="store_true",
        help="Сохранить текущие сообщения в БД как прочитанные без публикации в канал",
    )
    mode.add_argument(
        "--check-sources",
        action="store_true",
        help="Проверить доступность всех настроенных Telegram-каналов и RSS-лент",
    )
    mode.add_argument(
        "--check-telegram",
        action="store_true",
        help="Проверить токен бота и доступ к целевому Telegram-каналу",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="Показать текущее состояние угроз из локальной БД",
    )
    return parser


def _cmd_check_sources(sources_path: Path, timeout: int, proxy_url: str | None) -> int:
    sources = load_alert_sources(sources_path)
    fetcher = AlertSourcesFetcher(timeout=timeout, proxy_url=proxy_url)
    _, by_source, errors = fetcher.fetch_all(sources)

    print(f"Проверка источников оповещений ({len(sources)}):")
    for src in sources:
        if not src.enabled:
            print(f"  [OFF] {src.name} ({src.url})")
            continue
        if src.id in errors:
            print(f"  [ERR] {src.name} ({src.url}) -> {errors[src.id]}")
            continue
        msgs = by_source.get(src.id, [])
        latest = msgs[0].title if msgs else "нет сообщений"
        print(f"  [OK ] {src.name} [{src.kind}]: {len(msgs)} сообщений | последнее: {latest[:80]}")
    # Успешно если хотя бы часть источников доступна; ошибка если упали все включённые
    enabled_count = len([s for s in sources if s.enabled])
    return 1 if enabled_count > 0 and len(errors) == enabled_count else 0


def _cmd_check_telegram(token: str, channel_id: str, timeout: int, proxy_url: str | None) -> int:
    client = TelegramAlertClient(token, timeout=timeout, proxy_url=proxy_url)
    try:
        me = client.get_me()
        chat = client.get_chat(channel_id)
    except TelegramError as exc:
        print(f"Ошибка проверки Telegram: {exc}", file=sys.stderr)
        return 1

    print(f"Бот: @{me.get('username')} (id={me.get('id')})")
    print(f"Канал: {chat.get('title') or chat.get('username')} (id={chat.get('id')}, type={chat.get('type')})")
    return 0


def _cmd_dry_run(bot: VoronezhAlertBot) -> int:
    report = bot.run_once(dry_run=True)
    print(
        f"Dry-run: источников={report.fetched_sources}, ошибок={report.failed_sources}, "
        f"сообщений={report.total_messages}, тревог={report.matched_alerts}, "
        f"устаревших={report.stale_skipped}, гео_отфильтровано={report.geo_filtered}, "
        f"дублей={report.duplicates_skipped}, к_публикации={report.posted}"
    )
    if report.errors:
        print("Ошибки источников:", "; ".join(report.errors))
    print("-" * 60)
    for idx, cand in enumerate(report.posted_events, start=1):
        rendered = format_alert_post(
            cand,
            tz_name=bot.settings.timezone,
            include_safety_memo=bot.settings.include_safety_memo,
            include_source=bot.settings.include_source,
            include_time=bot.settings.include_time,
        )
        print(f"[{idx}] {cand.alert.kind.value} | {cand.message.source_id}")
        print(rendered)
        print("-" * 60)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    need_telegram = not (args.dry_run or args.seed or args.check_sources or args.status)
    try:
        settings = load_alert_settings(args.env, require_telegram=need_telegram)
    except ValueError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    if args.sources is not None:
        settings = replace(settings, sources_path=args.sources)
    if args.scope is not None:
        settings = replace(settings, geo_scope=args.scope)
    if args.verbose:
        settings = replace(settings, log_level="DEBUG")

    _configure_logging(settings.log_level)

    if args.check_sources:
        return _cmd_check_sources(
            settings.sources_path,
            settings.http_timeout_seconds,
            settings.proxy_url,
        )

    if args.check_telegram:
        return _cmd_check_telegram(
            settings.telegram_bot_token,
            settings.telegram_channel_id,
            settings.http_timeout_seconds,
            settings.proxy_url,
        )

    sources = load_alert_sources(settings.sources_path)
    storage = AlertStorage(settings.db_path)

    try:
        if args.status:
            status = storage.get_status()
            print(
                f"Инициализирован={status['initialized']}, всего_сообщений={status['total_seen']}, "
                f"опубликовано={status['total_posted']}, последний_опрос={status['last_poll_at']}"
            )
            for family, info in sorted(status["threats"].items()):
                state_str = "АКТИВНА" if info["active"] else "нет"
                print(f"  - {family}: {state_str} ({info['last_kind']}, {info['locations']})")
            return 0

        fetcher = AlertSourcesFetcher(
            timeout=settings.http_timeout_seconds,
            proxy_url=settings.proxy_url,
        )
        telegram = (
            TelegramAlertClient(
                settings.telegram_bot_token,
                timeout=settings.http_timeout_seconds,
                proxy_url=settings.proxy_url,
            )
            if need_telegram
            else None
        )
        bot = VoronezhAlertBot(settings, sources, storage, fetcher, telegram)

        if args.dry_run:
            return _cmd_dry_run(bot)

        if args.seed:
            report = bot.run_once(seed_only=True)
            print(
                f"Seed завершён: сохранено={report.seeded_skipped}, "
                f"устаревших={report.stale_skipped}, ошибок={report.failed_sources}"
            )
            return 0

        # По умолчанию (и с --once) выполняем один цикл для запуска из GitHub Actions / cron
        report = bot.run_once()
        print(
            f"Готово: опубликовано={report.posted}, тревог_найдено={report.matched_alerts}, "
            f"дублей={report.duplicates_skipped}, устаревших={report.stale_skipped}, "
            f"ошибок={len(report.errors)}"
        )
        enabled_sources = [s for s in sources if s.enabled]
        return 1 if report.errors and report.posted == 0 and report.failed_sources == len(enabled_sources) else 0
    finally:
        storage.close()
