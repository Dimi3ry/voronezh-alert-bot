"""Классификатор сообщений об атаке БПЛА, ракетной/авиационной опасности и отбое в Воронеже."""

from __future__ import annotations

import re

from .models import AlertKind, ClassifiedAlert, SourceMessage

_RE_MULTI_SPACE = re.compile(r"[ \t\r\f\v]+")
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")

# Проверка RSS-заголовка: отсекает статьи, где опасность БПЛА попала только в рекламный/редакционный подвал
_RSS_TOPIC_RE = re.compile(
    r"(?:"
    r"\bбпла\b|беспилот\w*|дрон\w*|"
    r"ракетн\w*\s+(?:опасност|угроз|тревог|удар)|"
    r"авиационн\w*\s+(?:опасност|угроз|тревог)|"
    r"воздушн\w*\s+(?:опасност|тревог)|"
    r"(?:опасност|угроз)\w*\s+(?:атаки|прямого\s+удара|непосредственного\s+удара)|"
    r"отбо[йя]\s+(?:опасност|тревог)|"
    r"отмен\w*\s+(?:режима\s+)?(?:опасност|угроз|тревог)"
    r")",
    re.IGNORECASE,
)

# Исключения: учения, проверки сирен и бытовые/погодные опасности
_DRILL_OR_NON_AIR_RE = re.compile(
    r"(?:"
    r"\bучени(?:я|й|ях)\b|учебн\w*\s+тревог|условн\w*\s+пожар|"
    r"проверк\w*\s+(?:систем\w*|готовност\w*)\s+оповещ|"
    r"планов\w*\s+проверк\w*\s+(?:систем|сирен)|"
    r"тестов\w*\s+запуск\w*\s+сирен|тренировк\w*|"
    r"выход\w*\s+на\s+лед|гололед|погодн\w*\s+опасност|пожарн\w*\s+опасност|"
    r"желт\w*\s+уров\w*\s+(?:погод|опасност)|оранжев\w*\s+уров\w*\s+(?:погод|опасност)|"
    r"атак\w*\s+мошенник|кибератак|хакерск\w*\s+атак|информационн\w*\s+атак"
    r")",
    re.IGNORECASE,
)

# Признаки обзорных/итоговых новостей (не являются текущим введением или отменой режима)
_RETROSPECTIVE_RE = re.compile(
    r"(?:"
    r"главные\s+новости|итоги\s+дня|дайджест|"
    r"последстви\w*\s+(?:ночн\w*|массированн\w*)?\s*(?:налет|атак)|"
    r"пострадавш\w*\s+от\s+\w+\s+атак|остается\s+в\s+больниц|"
    r"повредил\w*\s+(?:дом|остеклен|кровл|хозпостройк|автомобил)|"
    r"где\s+сбили\s+ночью|сирены\s+\d+\s+раз\s+срабатывали|"
    r"минобороны\s+(?:рф\s+)?отчитал|"
    r"октябрьск\w*\s+атак|прошл\w*\s+месяц|"
    r"рассказал[аи]?\s+воронежцам"
    r")",
    re.IGNORECASE,
)

# Базовые регулярные выражения для 8 классов событий (проверяются от более точных к общим)
_PATTERNS_MISSILE_CLEAR = (
    re.compile(r"(?:отбо[йя]|отмен[аеиуы]|отменил\w*|снят[аои]?)\s+(?:режима\s+)?ракетн\w*\s+опасност"),
    re.compile(r"ракетн\w*\s+опасност\w*\s+(?:в\s+\S+\s+)?(?:отменен\w*|снят\w*)"),
)

_PATTERNS_MISSILE_ALERT = (
    re.compile(r"ракетн\w*\s+опасност"),
    re.compile(r"(?:угроз|опасност)\w*\s+ракетн\w*\s+удар"),
)

_PATTERNS_AVIATION_CLEAR = (
    re.compile(r"(?:отбо[йя]|отмен[аеиуы]|отменил\w*|снят[аои]?)\s+(?:режима\s+)?авиационн\w*\s+опасност"),
    re.compile(r"авиационн\w*\s+опасност\w*\s+(?:в\s+\S+\s+)?(?:отменен\w*|снят\w*)"),
)

_PATTERNS_AVIATION_ALERT = (
    re.compile(r"авиационн\w*\s+опасност"),
)

_PATTERNS_UAV_IMMEDIATE_CLEAR = (
    re.compile(
        r"(?:отмен[аеиуы]|отбо[йя]|отменил\w*|снят[аои]?)\s+"
        r"(?:режима\s+)?(?:непосредственн\w*|прям\w*)\s+угроз\w*\s+"
        r"(?:удар\w*\s+)?(?:бпла|беспилот\w*|дрон\w*)"
    ),
    re.compile(
        r"(?:непосредственн\w*|прям\w*)\s+угроз\w*\s+(?:удар\w*\s+)?(?:бпла|беспилот\w*|дрон\w*)"
        r"[^.\n]{0,80}\b(?:отменен\w*|снят\w*)"
    ),
)

_PATTERNS_UAV_CLEAR = (
    re.compile(
        r"(?:отбо[йя]|отмен[аеиуы]|отменил\w*|снят[аои]?|снял\w*)\s+"
        r"(?:режим\w*\s+)?(?:опасност\w*\s+(?:атаки\s+)?|угроз\w*\s+(?:атаки\s+)?)?"
        r"(?:бпла|беспилотн\w*\s+опасност\w*|беспилотн\w*\s+угроз\w*|атак\w*\s+бпла)"
    ),
    re.compile(
        r"(?:опасност\w*\s+атаки\s+бпла|беспилотн\w*\s+опасност\w*|режим\w*\s+опасност\w*\s+бпла)"
        r"[^.\n]{0,80}\b(?:отменен\w*|снят\w*)"
    ),
)

_PATTERNS_UAV_IMMEDIATE_ALERT = (
    re.compile(
        r"(?:непосредственн\w*\s+(?:угроз|опасност)\w*|прям\w*\s+(?:угроз|опасност)\w*|"
        r"под\s+угрозой\s+прямого\s+удара|опасност\w*\s+(?:прям\w*|непосредственн\w*)\s+удара)"
        r"[^.\n]{0,80}(?:бпла|беспилот\w*|дрон\w*)"
    ),
    re.compile(r"прям\w*\s+беспилотн\w*\s+угроз\w*"),
    re.compile(
        r"(?:включен\w*\s+(?:систем\w*\s+оповещения|сирен\w*)|"
        r"сработал\w*\s+(?:систем\w*\s+оповещения|сирен\w*)|"
        r"активирован\w*\s+средств\w*\s+предупреждения)"
        r"[\s\S]{0,200}(?:бпла|беспилот\w*|дрон\w*)"
    ),
    re.compile(
        r"(?:бпла|беспилот\w*|дрон\w*)[\s\S]{0,160}"
        r"(?:включен\w*\s+(?:систем\w*\s+оповещения|сирен\w*)|"
        r"сработал\w*\s+(?:систем\w*\s+оповещения|сирен\w*)|"
        r"активирован\w*\s+средств\w*\s+предупреждения)"
    ),
)

_PATTERNS_UAV_ALERT = (
    re.compile(r"опасност\w*\s+атаки\s+(?:бпла|беспилот\w*|дрон\w*)"),
    re.compile(r"беспилотн\w*\s+опасност"),
    re.compile(
        r"(?:объявлен\w*|введен\w*|тревог\w*|риск\s+использования|угроз\w*\s+нападения)"
        r"[^.\n]{0,90}(?:бпла|беспилот\w*|дрон\w*)"
    ),
    re.compile(
        r"(?:бпла|беспилотник\w*|дрон\w*(?:\s+всу)?)\s+"
        r"(?:пытаются\s+атаковать|атакуют|заметили\s+на\s+подлете|заметили\s+у\s+границ|движутся\s+к)"
    ),
)

_UAV_GENERAL_REMAINS_RE = re.compile(
    r"(?:опасност\w*\s+(?:атаки\s+)?бпла|режим\w*\s+опасност\w*\s+атаки\s+бпла)\s+сохраня",
    re.IGNORECASE,
)

# Напоминание без перехода состояния (когда сам текст лишь напоминает «опасность сохраняется»)
_PURE_REMINDER_RE = re.compile(
    r"(?:опасност\w*\s+(?:атаки\s+)?бпла|ракетн\w*\s+опасност\w*)\s+(?:в\s+\S+\s+)?сохраня",
    re.IGNORECASE,
)

# Маркеры Воронежа и области
_VORONEZH_EXPLICIT_RE = re.compile(
    r"(?:\bворонеж\w*\b|\bврн\b|\bгусев\w*\b|правительств\w*\s+(?:воронежской\s+)?области)",
    re.IGNORECASE,
)

_OFFICIAL_REGION_RE = re.compile(
    r"(?:\bна\s+территории\s+региона\b|\bв\s+регионе\b|\bво\s+всех\s+муниципалитетах\b|\bв\s+области\b)",
    re.IGNORECASE,
)

_OTHER_REGION_RE = re.compile(
    r"\b(?:белгородск|курск|липецк|тамбовск|саратовск|волгоградск|ростовск|"
    r"брянск|орловск|тульск|калужск|смоленск|московск|ленинградск|"
    r"краснодарск|нижегородск|рязанск|пензенск|ульяновск|самарск|"
    r"севастопол|крым\w*|татарстан|сочи)\w*\b",
    re.IGNORECASE,
)

_CITY_VORONEZH_RE = re.compile(
    r"(?:\bв\s+(?:городе\s+)?воронеже\b|\bг\.\s*воронеж\b|\bворонеж\w*\s+агломерац\w*|"
    r"\bгород\s+воронеж\b|\bстолиц\w*\s+чернозем)",
    re.IGNORECASE,
)

_REGION_WIDE_RE = re.compile(
    r"(?:\bна\s+территории\s+(?:всей\s+)?(?:воронежской\s+области|региона)\b|"
    r"\bв\s+воронежской\s+области\b|\bпо\s+всей\s+области\b|\bво\s+всех\s+муниципалитетах\b|"
    r"\bв\s+регионе\b)",
    re.IGNORECASE,
)

# Муниципалитеты и города Воронежской области: (regex_stem, display_name, slug)
_VORONEZH_DISTRICTS: tuple[tuple[str, str, str], ...] = (
    (r"борисоглеб\w*", "Борисоглебск", "borisoglebsk"),
    (r"нововоронеж\w*", "Нововоронеж", "novovoronezh"),
    (r"аннинск\w*", "Аннинский район", "anninskij"),
    (r"бобровск\w*", "Бобровский район", "bobrovskij"),
    (r"богучар\w*", "Богучарский район", "bogucharskij"),
    (r"бутурлинов\w*", "Бутурлиновский район", "buturlinovskij"),
    (r"верхнемамон\w*", "Верхнемамонский район", "verhnemamonskij"),
    (r"верхнехав\w*", "Верхнехавский район", "verhnehavskij"),
    (r"воробьев\w*", "Воробьёвский район", "vorobevskij"),
    (r"грибанов\w*", "Грибановский район", "gribanovskij"),
    (r"калачеев\w*", "Калачеевский район", "kalacheevskij"),
    (r"каменск\w*", "Каменский район", "kamenskij"),
    (r"кантемиров\w*", "Кантемировский район", "kantemirovskij"),
    (r"каширск\w*", "Каширский район", "kashirskij"),
    (r"лискинск\w*|\bлиски\b", "Лискинский район", "liskinskij"),
    (r"нижнедевиц\w*", "Нижнедевицкий район", "nizhnedevickij"),
    (r"новоусман\w*|новая\s+усман\w*", "Новоусманский район", "novousmanskij"),
    (r"новохопер\w*", "Новохопёрский район", "novohoperskij"),
    (r"ольховат\w*", "Ольховатский район", "olhovatskij"),
    (r"острогож\w*", "Острогожский район", "ostrogozhskij"),
    (r"павловск\w*", "Павловский район", "pavlovskij"),
    (r"панинск\w*", "Панинский район", "paninskij"),
    (r"петропавлов\w*", "Петропавловский район", "petropavlovskij"),
    (r"поворин\w*", "Поворинский район", "povorinskij"),
    (r"подгорен\w*", "Подгоренский район", "podgorenskij"),
    (r"рамон\w*", "Рамонский район", "ramonskij"),
    (r"репьев\w*", "Репьёвский район", "repevskij"),
    (r"россош\w*", "Россошанский район", "rossoshanskij"),
    (r"семилук\w*", "Семилукский район", "semilukskij"),
    (r"талов\w*", "Таловский район", "talovskij"),
    (r"терновск\w*", "Терновский район", "ternovskij"),
    (r"хохольск\w*", "Хохольский район", "hoholskij"),
    (r"эртиль\w*", "Эртильский район", "ertilskij"),
)

_COMPILED_DISTRICTS: tuple[tuple[re.Pattern[str], str, str], ...] = tuple(
    (re.compile(rf"\b{pat}", re.IGNORECASE), label, slug)
    for pat, label, slug in _VORONEZH_DISTRICTS
)


def fold_yo(text: str) -> str:
    """Приводит текст к нижнему регистру и заменяет 'ё' на 'е'."""
    return text.lower().replace("ё", "е")


def clean_whitespace(text: str) -> str:
    lines = [_RE_MULTI_SPACE.sub(" ", line).strip() for line in text.splitlines()]
    joined = "\n".join(line for line in lines if line)
    return _RE_MULTI_NEWLINE.sub("\n\n", joined).strip()


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(p.search(text) for p in patterns)


def _extract_locations(folded_text: str) -> tuple[tuple[str, ...], str, bool]:
    """Извлекает затронутые территории и определяет, касается ли сигнал города Воронежа / всей области."""
    has_city = bool(_CITY_VORONEZH_RE.search(folded_text))
    found_labels: list[str] = []
    found_slugs: list[str] = []

    if has_city:
        found_labels.append("Воронеж")
        found_slugs.append("voronezh")

    for pattern, label, slug in _COMPILED_DISTRICTS:
        if pattern.search(folded_text):
            found_labels.append(label)
            found_slugs.append(slug)

    is_region_wide = bool(_REGION_WIDE_RE.search(folded_text))
    # Если перечислены конкретные районы (без города Воронежа и без «во всех муниципалитетах»),
    # фраза «районы Воронежской области» не означает общеобластную сирену.
    specific_districts_only = bool(found_slugs) and not has_city and "во всех муниципалитетах" not in folded_text

    if found_labels and not (is_region_wide and not specific_districts_only):
        unique_slugs = sorted(set(found_slugs))
        return tuple(found_labels), "|".join(unique_slugs), (has_city or not specific_districts_only)

    if has_city and is_region_wide:
        return ("Воронеж и область",), "region", True

    if is_region_wide or not found_labels:
        return ("Воронеж и область",), "region", True

    return tuple(found_labels), "|".join(sorted(set(found_slugs))), True


def _is_voronezh_alert(folded_text: str, *, official: bool) -> bool:
    has_voronezh = bool(_VORONEZH_EXPLICIT_RE.search(folded_text)) or any(
        pat.search(folded_text) for pat, _, _ in _COMPILED_DISTRICTS
    )
    has_other = bool(_OTHER_REGION_RE.search(folded_text))

    if has_other and not has_voronezh:
        return False

    if official:
        return has_voronezh or bool(_OFFICIAL_REGION_RE.search(folded_text))

    return has_voronezh


def _build_excerpt(message: SourceMessage) -> str:
    """Формирует чистую выдержку сообщения для поста в канал."""
    body = clean_whitespace(message.text)
    if message.source_kind == "rss":
        title = clean_whitespace(message.title)
        if body and fold_yo(body) != fold_yo(title):
            combined = f"{title}\n\n{body}"
        else:
            combined = title
    else:
        combined = body or clean_whitespace(message.title)

    if len(combined) <= 650:
        return combined
    cut = combined[:649].rstrip()
    space_idx = cut.rfind(" ")
    if space_idx > 400:
        cut = cut[:space_idx].rstrip(" ,;:-")
    return f"{cut}…"


def classify_message(message: SourceMessage) -> ClassifiedAlert | None:
    """Классифицирует сообщение как оперативную тревогу/отбой по Воронежу или возвращает None."""
    title_clean = clean_whitespace(message.title)
    body_clean = clean_whitespace(message.text)

    if message.source_kind == "rss":
        if not title_clean or not _RSS_TOPIC_RE.search(fold_yo(title_clean)):
            return None
        # Для RSS оцениваем заголовок и начало описания (ограничиваем объём против рекламных подвалов)
        lead = body_clean[:500]
        analysis_raw = f"{title_clean}\n{lead}" if lead else title_clean
    else:
        analysis_raw = body_clean or title_clean

    if not analysis_raw.strip():
        return None

    folded = fold_yo(analysis_raw)

    if _DRILL_OR_NON_AIR_RE.search(folded):
        return None

    if not _is_voronezh_alert(folded, official=message.official):
        return None

    # Явные декларации отбоя/опасности проверяются с приоритетом от самых специфичных к общим
    kind: AlertKind | None = None
    uav_general_remains = bool(_UAV_GENERAL_REMAINS_RE.search(folded))

    if _matches_any(folded, _PATTERNS_MISSILE_CLEAR):
        kind = AlertKind.MISSILE_CLEAR
    elif _matches_any(folded, _PATTERNS_MISSILE_ALERT):
        if _PURE_REMINDER_RE.search(folded) and "объявлен" not in folded and "введен" not in folded:
            return None
        kind = AlertKind.MISSILE_ALERT
    elif _matches_any(folded, _PATTERNS_AVIATION_CLEAR):
        kind = AlertKind.AVIATION_CLEAR
    elif _matches_any(folded, _PATTERNS_AVIATION_ALERT):
        kind = AlertKind.AVIATION_ALERT
    elif _matches_any(folded, _PATTERNS_UAV_IMMEDIATE_CLEAR):
        kind = AlertKind.UAV_IMMEDIATE_CLEAR
    elif _matches_any(folded, _PATTERNS_UAV_CLEAR):
        kind = AlertKind.UAV_CLEAR
    elif _matches_any(folded, _PATTERNS_UAV_IMMEDIATE_ALERT):
        kind = AlertKind.UAV_IMMEDIATE_ALERT
    elif _matches_any(folded, _PATTERNS_UAV_ALERT):
        # Чистое напоминание без нового объявления и без указания об атаке/подлёте отбрасываем
        if _PURE_REMINDER_RE.search(folded) and "объявлен" not in folded and "введен" not in folded:
            return None
        kind = AlertKind.UAV_ALERT

    if kind is None:
        return None

    # Итоговые/архивные статьи не должны срабатывать как активная тревога или отбой,
    # если в них нет прямого объявления отбоя/введения режима.
    if _RETROSPECTIVE_RE.search(folded):
        has_explicit_declaration = (
            kind.is_clear
            and bool(re.search(r"(?:объявил\w*|отменил\w*|отбо[йя]|отмена)\s+", folded))
            and "сирены" not in folded
            and "последстви" not in folded
            and "повредил" not in folded
            and "пострадавш" not in folded
        )
        if not has_explicit_declaration:
            return None

    locations, location_key, is_city_or_region_wide = _extract_locations(folded)
    return ClassifiedAlert(
        kind=kind,
        locations=locations,
        location_key=location_key,
        is_voronezh_city_or_region_wide=is_city_or_region_wide,
        uav_general_remains=uav_general_remains,
        excerpt=_build_excerpt(message),
    )
