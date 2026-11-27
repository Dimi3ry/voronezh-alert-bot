"""Оркестратор проверки источников, дедупликации угроз и публикации оповещений."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from .classifier import classify_message
from .config import AlertSettings
from .models import AlertCandidate, AlertCycleReport, AlertSource, SourceMessage
from .sources_fetcher import AlertSourcesFetcher
from .storage import AlertStorage
from .telegram_client import TelegramAlertClient, TelegramError

logger = logging.getLogger(__name__)


class VoronezhAlertBot:
    """Бот мониторинга воздушных/ракетных тревог и отбоев в Воронеже и области."""

    def __init__(
        self,
        settings: AlertSettings,
        sources: list[AlertSource],
        storage: AlertStorage,
        fetcher: AlertSourcesFetcher,
        telegram: TelegramAlertClient | None = None,
    ) -> None:
        self.settings = settings
        self.sources = sources
        self.storage = storage
        self.fetcher = fetcher
        self.telegram = telegram

    def _is_stale(self, message: SourceMessage, now: datetime) -> bool:
        if message.published_at is None:
            return False
        return (now - message.published_at) > timedelta(minutes=self.settings.max_item_age_minutes)

    def _matches_geo_scope(self, candidate: AlertCandidate) -> bool:
        if self.settings.geo_scope == "region":
            return True
        # В режиме city оставляем сообщения, касающиеся города Воронежа или всей области целиком
        return candidate.alert.is_voronezh_city_or_region_wide

    def _select_best_per_cluster(
        self,
        candidates_chronological: list[AlertCandidate],
    ) -> tuple[list[AlertCandidate], list[AlertCandidate]]:
        """В рамках одного цикла выбирает официальный источник при совпадении события с новостями СМИ."""
        accepted: list[AlertCandidate] = []
        suppressed: list[AlertCandidate] = []
        window = timedelta(minutes=self.settings.media_fallback_cooldown_minutes)

        # Сначала обрабатываем с учётом приоритета источников в близких временных окнах
        ordered_by_priority = sorted(
            candidates_chronological,
            key=lambda c: (
                not c.message.official,
                c.message.priority,
                c.message.published_at or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )

        for cand in ordered_by_priority:
            cand_dt = cand.message.published_at
            is_dup = False
            for chosen in accepted:
                if chosen.alert.kind != cand.alert.kind:
                    continue
                same_loc = chosen.alert.location_key == cand.alert.location_key
                both_recent = True
                if cand_dt is not None and chosen.message.published_at is not None:
                    both_recent = abs(cand_dt - chosen.message.published_at) <= window
                if not both_recent:
                    continue
                is_region_family = cand.alert.kind not in {
                    cand.alert.kind.UAV_IMMEDIATE_ALERT,
                    cand.alert.kind.UAV_IMMEDIATE_CLEAR,
                }
                vague_media = not cand.message.official and cand.alert.location_key == "region"
                if same_loc or is_region_family or vague_media or (
                    chosen.message.official and not cand.message.official
                ):
                    is_dup = True
                    break

            if is_dup:
                suppressed.append(cand)
            else:
                accepted.append(cand)

        epoch = datetime.fromtimestamp(0, tz=timezone.utc)
        accepted.sort(
            key=lambda c: ((c.message.published_at or epoch).timestamp(), -c.message.priority)
        )
        return accepted, suppressed

    def run_once(
        self,
        *,
        dry_run: bool = False,
        seed_only: bool = False,
        now: datetime | None = None,
    ) -> AlertCycleReport:
        """Выполняет один цикл проверки всех источников и отправки новых оповещений."""
        report = AlertCycleReport()
        ref_now = now or datetime.now(timezone.utc)

        all_messages, by_source, fetch_errors = self.fetcher.fetch_all(self.sources)
        report.fetched_sources = len(by_source)
        report.failed_sources = len(fetch_errors)
        report.total_messages = len(all_messages)

        if not dry_run:
            for source_id, msgs in by_source.items():
                self.storage.record_source_success(source_id, len(msgs))
            for source_id, err in fetch_errors.items():
                self.storage.record_source_error(source_id, err)
                report.errors.append(f"{source_id}: {err}")
        else:
            for source_id, err in fetch_errors.items():
                report.errors.append(f"{source_id}: {err}")

        already_initialized = self.storage.is_initialized()
        fresh_candidates: list[AlertCandidate] = []
        non_alert_or_stale: list[tuple[SourceMessage, AlertCandidate | None]] = []

        for msg in all_messages:
            if self.storage.has_seen(msg):
                report.duplicates_skipped += 1
                continue

            classified = classify_message(msg, include_summary=self.settings.include_summary)
            if classified is None:
                non_alert_or_stale.append((msg, None))
                continue

            report.matched_alerts += 1
            cand = AlertCandidate(message=msg, alert=classified)

            if self._is_stale(msg, ref_now):
                report.stale_skipped += 1
                non_alert_or_stale.append((msg, cand))
                continue

            if not self._matches_geo_scope(cand):
                report.geo_filtered += 1
                non_alert_or_stale.append((msg, cand))
                continue

            fresh_candidates.append(cand)

        if not dry_run:
            for msg, cand in non_alert_or_stale:
                self.storage.record_message(
                    msg,
                    alert=cand.alert if cand else None,
                    posted=False,
                )

        if seed_only:
            for cand in fresh_candidates:
                self.storage.record_message(cand.message, alert=cand.alert, posted=False)
                report.seeded_skipped += 1
            self.storage.mark_initialized()
            self.storage.set_last_poll(self._report_summary(report))
            return report

        # Хронологически от старых к новым, затем дедупликация внутри текущего пакета
        epoch = datetime.fromtimestamp(0, tz=timezone.utc)
        fresh_candidates.sort(
            key=lambda c: ((c.message.published_at or epoch).timestamp(), -c.message.priority)
        )
        deduped_candidates, cycle_suppressed = self._select_best_per_cluster(fresh_candidates)
        report.duplicates_skipped += len(cycle_suppressed)
        if not dry_run:
            for sup in cycle_suppressed:
                self.storage.record_message(sup.message, alert=sup.alert, posted=False)

        # Защита от спама при первом запуске: не более initial_post_limit самых свежих
        if not already_initialized:
            limit = self.settings.initial_post_limit
            to_process = deduped_candidates[-limit:] if limit > 0 else []
            to_seed = deduped_candidates[:-limit] if limit > 0 else deduped_candidates
            report.seeded_skipped += len(to_seed)
            if not dry_run:
                for cand in to_seed:
                    self.storage.record_message(cand.message, alert=cand.alert, posted=False)
        else:
            to_process = deduped_candidates[: self.settings.max_posts_per_cycle]
            overflow = deduped_candidates[self.settings.max_posts_per_cycle:]
            if overflow:
                report.seeded_skipped += len(overflow)
                if not dry_run:
                    for cand in overflow:
                        self.storage.record_message(cand.message, alert=cand.alert, posted=False)

        sent_any = False
        for cand in to_process:
            if self.storage.should_suppress_duplicate(
                cand.message,
                cand.alert,
                dedup_cooldown_minutes=self.settings.dedup_cooldown_minutes,
                media_fallback_cooldown_minutes=self.settings.media_fallback_cooldown_minutes,
                state_ttl_hours=self.settings.state_ttl_hours,
                now=ref_now,
            ):
                report.duplicates_skipped += 1
                if not dry_run:
                    self.storage.record_message(cand.message, alert=cand.alert, posted=False)
                continue

            if cand.alert.kind.is_clear:
                cand.duration_seconds = self.storage.get_active_duration_seconds(
                    cand.alert.kind.threat_family,
                    cand.message.published_at or ref_now,
                    max_hours=max(self.settings.state_ttl_hours, 24),
                )

            if dry_run:
                report.posted += 1
                report.posted_events.append(cand)
                continue

            if self.telegram is None:
                raise RuntimeError("TelegramAlertClient не настроен для отправки сообщений")

            if sent_any and self.settings.post_delay_seconds > 0:
                time.sleep(self.settings.post_delay_seconds)

            disable_notif = self.settings.disable_notification or (
                self.settings.disable_clear_notification and cand.alert.kind.is_clear
            )

            try:
                msg_id = self.telegram.publish_alert(
                    self.settings.telegram_channel_id,
                    cand,
                    tz_name=self.settings.timezone,
                    include_safety_memo=self.settings.include_safety_memo,
                    include_source=self.settings.include_source,
                    include_time=self.settings.include_time,
                    disable_notification=disable_notif,
                )
                self.storage.record_message(
                    cand.message,
                    alert=cand.alert,
                    posted=True,
                    telegram_message_id=msg_id,
                )
                self.storage.apply_published_event(cand.message, cand.alert, now=ref_now)
                report.posted += 1
                report.posted_events.append(cand)
                sent_any = True
                logger.info(
                    "Опубликовано оповещение [%s] (%s): %s",
                    cand.alert.kind.value,
                    cand.message.source_id,
                    cand.alert.location_key,
                )
            except TelegramError as exc:
                err_msg = f"Ошибка отправки Telegram ({cand.alert.kind.value}): {exc}"
                report.errors.append(err_msg)
                logger.error(err_msg)
                if exc.code in {401, 403, 404}:
                    break

        if not dry_run:
            self.storage.mark_initialized()
            self.storage.set_last_poll(self._report_summary(report))

        return report

    @staticmethod
    def _report_summary(report: AlertCycleReport) -> dict[str, int]:
        return {
            "fetched_sources": report.fetched_sources,
            "failed_sources": report.failed_sources,
            "total_messages": report.total_messages,
            "matched_alerts": report.matched_alerts,
            "geo_filtered": report.geo_filtered,
            "duplicates_skipped": report.duplicates_skipped,
            "stale_skipped": report.stale_skipped,
            "seeded_skipped": report.seeded_skipped,
            "posted": report.posted,
        }
