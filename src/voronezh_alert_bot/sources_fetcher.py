"""Загрузка и парсинг сообщений из официальных Telegram-каналов (t.me/s) и воронежских RSS."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as DET
import requests

from .classifier import clean_whitespace
from .models import AlertSource, SourceMessage

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_SKIP_TAGS = frozenset({"script", "style", "noscript"})
_BREAK_TAGS = frozenset({"br", "p", "div", "li"})
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "param", "source", "track", "wbr",
})
_RE_HTML_TAG = re.compile(r"<[^>]+>")


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    try:
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


class _PlainTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        if tag_l in _SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag_l in _BREAK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and tag_l in {"p", "div", "li"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def get_text(self) -> str:
        joined = "".join(self._chunks).replace("\xa0", " ")
        return clean_whitespace(joined)


def strip_html(raw: str | None) -> str:
    """Очищает HTML и декодирует сущности."""
    if not raw:
        return ""
    parser = _PlainTextHTMLParser()
    parser.feed(raw)
    parser.close()
    return parser.get_text()


class _TelegramWebParser(HTMLParser):
    """Парсер публичной веб-страницы канала `https://t.me/s/<channel>`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[dict[str, str]] = []
        self._tag_stack: list[tuple[str, set[str]]] = []
        self._in_msg = False
        self._msg_depth = 0
        self._text_depth = 0
        self._reply_depth = 0
        self._skip_depth = 0
        self._cur_post = ""
        self._cur_datetime = ""
        self._text_chunks: list[str] = []

    @staticmethod
    def _class_set(attrs: list[tuple[str, str | None]]) -> tuple[dict[str, str], set[str]]:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        classes = set(attr_map.get("class", "").split())
        return attr_map, classes

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attr_map, classes = self._class_set(attrs)
        self._tag_stack.append((tag_l, classes))

        if tag_l in _SKIP_TAGS:
            self._skip_depth += 1

        if (
            tag_l == "div"
            and "tgme_widget_message" in classes
            and "service_message" not in classes
            and attr_map.get("data-post")
        ):
            self._in_msg = True
            self._msg_depth = 1
            self._cur_post = attr_map["data-post"].strip()
            self._cur_datetime = ""
            self._text_chunks = []
            self._text_depth = 0
            self._reply_depth = 0
            return

        if not self._in_msg:
            return

        if tag_l not in _VOID_TAGS:
            self._msg_depth += 1

        if "tgme_widget_message_reply" in classes:
            self._reply_depth += 1
        elif self._reply_depth > 0 and tag_l not in _VOID_TAGS:
            self._reply_depth += 1

        if "tgme_widget_message_text" in classes and self._reply_depth == 0:
            self._text_depth = 1
        elif self._text_depth > 0 and tag_l not in _VOID_TAGS:
            self._text_depth += 1

        if self._text_depth > 0 and self._reply_depth == 0 and self._skip_depth == 0:
            if tag_l in _BREAK_TAGS:
                self._text_chunks.append("\n")

        if tag_l == "time" and attr_map.get("datetime") and not self._cur_datetime:
            self._cur_datetime = attr_map["datetime"].strip()

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if self._tag_stack:
            self._tag_stack.pop()

        if tag_l in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

        if not self._in_msg:
            return

        if tag_l in _VOID_TAGS:
            return

        if self._text_depth > 0:
            if tag_l in {"p", "div", "li"} and self._reply_depth == 0:
                self._text_chunks.append("\n")
            self._text_depth -= 1

        if self._reply_depth > 0:
            self._reply_depth -= 1

        self._msg_depth -= 1
        if self._msg_depth <= 0:
            text = clean_whitespace("".join(self._text_chunks).replace("\xa0", " "))
            if self._cur_post and text:
                self.messages.append(
                    {
                        "post_id": self._cur_post,
                        "datetime": self._cur_datetime,
                        "text": text,
                    }
                )
            self._in_msg = False
            self._msg_depth = 0
            self._text_depth = 0
            self._reply_depth = 0

    def handle_data(self, data: str) -> None:
        if self._in_msg and self._text_depth > 0 and self._reply_depth == 0 and self._skip_depth == 0:
            if data:
                self._text_chunks.append(data)


def parse_telegram_web_html(html_text: str, source: AlertSource) -> list[SourceMessage]:
    """Парсит HTML-страницу публичного Telegram-канала (`t.me/s/...`) в список сообщений."""
    if not html_text or "tgme_widget_message" not in html_text:
        return []

    parser = _TelegramWebParser()
    parser.feed(html_text)
    parser.close()

    results: list[SourceMessage] = []
    for raw in parser.messages:
        post_id = raw["post_id"]
        text = raw["text"]
        first_line = text.splitlines()[0].strip() if text else ""
        published_at = _parse_datetime(raw.get("datetime"))
        results.append(
            SourceMessage(
                source_id=source.id,
                source_name=source.name,
                source_kind="telegram_web",
                official=source.official,
                priority=source.priority,
                external_id=post_id,
                url=f"https://t.me/{post_id}",
                title=first_line[:180],
                text=text,
                published_at=published_at,
            )
        )
    return results


def _child_text(elem: Element, *names: str) -> str:
    target = set(names)
    for child in elem:
        if _local_tag(child.tag) in target and child.text:
            return child.text.strip()
    return ""


def parse_rss_xml(xml_bytes: bytes, source: AlertSource) -> list[SourceMessage]:
    """Парсит RSS 2.0 или Atom-ленту в список SourceMessage."""
    if not xml_bytes or not xml_bytes.strip():
        return []

    root = DET.fromstring(xml_bytes)
    root_tag = _local_tag(root.tag)
    messages: list[SourceMessage] = []

    if root_tag == "feed":
        for child in root:
            if _local_tag(child.tag) != "entry":
                continue
            title = strip_html(_child_text(child, "title"))
            url = ""
            for sub in child:
                if _local_tag(sub.tag) == "link":
                    href = (sub.attrib.get("href") or "").strip()
                    rel = (sub.attrib.get("rel") or "alternate").lower()
                    if href and rel in {"alternate", ""}:
                        url = href
                        break
                    if href and not url:
                        url = href
            guid = _child_text(child, "id") or url
            if not title or not url:
                continue
            body = strip_html(_child_text(child, "summary") or _child_text(child, "content"))
            published_at = _parse_datetime(_child_text(child, "published", "updated"))
            messages.append(
                SourceMessage(
                    source_id=source.id,
                    source_name=source.name,
                    source_kind="rss",
                    official=source.official,
                    priority=source.priority,
                    external_id=guid,
                    url=url,
                    title=title,
                    text=body,
                    published_at=published_at,
                )
            )
        return messages

    for elem in root.iter():
        if _local_tag(elem.tag) != "item":
            continue
        title = strip_html(_child_text(elem, "title"))
        url = _child_text(elem, "link", "pdalink")
        guid = _child_text(elem, "guid") or url
        if not title or not url:
            continue
        desc = strip_html(_child_text(elem, "description"))
        encoded = strip_html(_child_text(elem, "encoded"))
        body = desc or encoded
        published_at = _parse_datetime(_child_text(elem, "pubdate", "date"))
        messages.append(
            SourceMessage(
                source_id=source.id,
                source_name=source.name,
                source_kind="rss",
                official=source.official,
                priority=source.priority,
                external_id=guid,
                url=url,
                title=title,
                text=body,
                published_at=published_at,
            )
        )

    return messages


class AlertSourcesFetcher:
    """Параллельный HTTP-клиент для сбора сообщений из Telegram-каналов и RSS."""

    def __init__(
        self,
        *,
        timeout: int = 15,
        proxy_url: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
        if proxy_url and session is None:
            self._session.proxies.update({"http": proxy_url, "https": proxy_url})

    def fetch_source(self, source: AlertSource) -> list[SourceMessage]:
        response = self._session.get(source.url, timeout=self.timeout)
        response.raise_for_status()

        if source.kind == "telegram_web":
            messages = parse_telegram_web_html(response.text, source)
            if not messages:
                logger.warning(
                    "Источник %s (%s) не содержит сообщений tgme_widget_message",
                    source.id,
                    source.url,
                )
            return messages

        if source.kind == "rss":
            return parse_rss_xml(response.content, source)

        raise ValueError(f"Неизвестный тип источника {source.kind!r} для {source.id}")

    def fetch_all(
        self,
        sources: list[AlertSource],
        *,
        max_workers: int = 6,
    ) -> tuple[list[SourceMessage], dict[str, list[SourceMessage]], dict[str, str]]:
        """Параллельно опрашивает включённые источники и сортирует сообщения от новых к старым."""
        enabled = [src for src in sources if src.enabled]
        by_source: dict[str, list[SourceMessage]] = {}
        errors: dict[str, str] = {}
        all_messages: list[SourceMessage] = []

        if not enabled:
            return all_messages, by_source, errors

        workers = min(max_workers, len(enabled))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {pool.submit(self.fetch_source, src): src for src in enabled}
            for future in as_completed(future_map):
                src = future_map[future]
                try:
                    msgs = future.result()
                    by_source[src.id] = msgs
                    all_messages.extend(msgs)
                except Exception as exc:
                    errors[src.id] = str(exc)
                    logger.warning("Ошибка источника %s (%s): %s", src.name, src.url, exc)

        epoch = datetime.fromtimestamp(0, tz=timezone.utc)
        # Сортируем от новых к старым; при равном времени выше приоритет у меньшего priority
        all_messages.sort(
            key=lambda m: ((m.published_at or epoch).timestamp(), -m.priority),
            reverse=True,
        )
        return all_messages, by_source, errors
