"""
Управление компьютером пользователя — уровень 1 (детерминированный, без
vision-агента): открыть сайт, открыть приложение, запустить именованную
задачу (на macOS в том числе шаблон Shortcuts). Кросс-платформа: macOS/Windows
(linux — best effort).

Механика — маркеры в ответе LLM (тот же паттерн, что [TODO_ADD:…]):

  [OPEN_URL:https://example.com]   открыть сайт (только http/https)
  [OPEN_APP:ключ]                  запустить приложение из allowlist apps
  [RUN_TASK:ключ]                  выполнить именованную команду из allowlist tasks

Безопасность (это машина пользователя, blast radius большой):

  * исполняется ТОЛЬКО то, что описано в allowlist'ах yaml персоны —
    свободный текст от LLM в shell не попадает никогда;
  * по умолчанию действие не исполняется сразу: маркер складывается в
    pending (TTL 5 мин), бот в видимом тексте спрашивает подтверждение,
    следующее «да»/«нет» пользователя перехватывается process_message;
    `confirm: false` — исполнение сразу по маркеру;
  * каждое исполнение пишется в аудит-лог audit.jsonl.

Конфиг (features персоны):

  features:
    computer_control:
      confirm: true             # подтверждение в чате перед исполнением
      allow_domains: []         # пусто = любые http(s); иначе whitelist доменов
      apps:                     # ключ → что запускать (строка или per-OS)
        safari: Safari
        chrome: {darwin: "Google Chrome", win32: "chrome"}
      tasks:                    # ключ → shell-команда (строка или per-OS)
        музыка: {darwin: 'shortcuts run "Музыка"'}

Выключение: `computer_control: false` или `enabled: false` внутри dict —
во втором случае allowlist'ы сохраняются (так пишет веб-настройка фич).
"""

import json
import logging
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MARKER_RE = re.compile(r"\[(OPEN_URL|OPEN_APP|RUN_TASK):([^\]\n]{1,300})\]")

PENDING_TTL_SEC = 300


def config_enabled(cfg) -> bool:
    """Включён ли режим по значению features.computer_control.

    False/None/пустой dict → выключен; непустой dict с `enabled: false` →
    тоже выключен (веб-настройка фич так гасит режим, сохраняя allowlist'ы).
    """
    if isinstance(cfg, dict):
        return bool(cfg) and cfg.get("enabled") is not False
    return bool(cfg)

# Ответ на предложение действия. UNKNOWN («расскажи подробнее») не
# перехватывается — сообщение уходит в обычный поток, pending живёт до TTL.
_YES_RE = re.compile(
    r"\b(?:да|давай|ок|окей|ok|okay|yes|конечно|поехали|угу|ага|"
    r"открывай|запускай|включай|go|sure)\b", re.IGNORECASE)
_NO_RE = re.compile(
    r"\b(?:нет|не\s+надо|не\s+нужно|не\s+хочу|отмена|отменяй|стоп|"
    r"no|nope|cancel)\b", re.IGNORECASE)

_KIND_BY_MARKER = {"OPEN_URL": "url", "OPEN_APP": "app", "RUN_TASK": "task"}


def classify_confirmation(text: str) -> str:
    """Ответ пользователя на «выполнить действие?» → YES | NO | UNKNOWN."""
    if not text:
        return "UNKNOWN"
    if _NO_RE.search(text):
        return "NO"
    if _YES_RE.search(text):
        return "YES"
    return "UNKNOWN"


def _norm_match(s) -> str:
    """Нормализация для матчинга цели: дефисы/тире → пробелы, lower, сжатие
    пробелов. «айс-ти» в команде и «Айс ти» на странице — одна и та же цель."""
    t = str(s or "").lower()
    for ch in ("-", "‑", "–", "—"):
        t = t.replace(ch, " ")
    return " ".join(t.split())


def _word_in(word: str, hay: str) -> bool:
    """Слово/основа в тексте: совпадение с НАЧАЛА слова, а не подстрока внутри
    чужого («айс» ≠ «гавАЙСкая»). hay — уже через _norm_match."""
    return bool(word) and re.search(
        r"(?<![a-z0-9а-яё])" + re.escape(word), hay) is not None


def _edit_dist_leq(a: str, b: str, limit: int) -> bool:
    """Дамерау-Левенштейн ≤ limit (с ранним выходом): опечатки и перестановки
    пар соседних букв («кешбэк»↔«кэшбек» — 2 замены, «дук»↔«лук» — 1)."""
    if abs(len(a) - len(b)) > limit:
        return False
    prev2 = None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        row_min = i
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if (prev2 is not None and i > 1 and j > 1
                    and ca == b[j - 2] and a[i - 2] == cb):
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            if v < row_min:
                row_min = v
        if row_min > limit:
            return False
        prev2, prev = prev, cur
    return prev[-1] <= limit


def _word_fuzzy_in(word: str, hay: str, anchored: bool) -> bool:
    """Слово цели с опечаткой в тексте: расстояние ≤2 для слов от 6 букв,
    ≤1 для 4-5; трёхбуквенные — только при «якоре» (anchored: другое слово
    цели совпало точно), иначе ложняки («дом»~«дум»). hay — через _norm_match."""
    n = len(word)
    if n <= 2 or (n == 3 and not anchored):
        return False
    limit = 2 if n >= 6 else 1
    for hw in re.findall(r"[a-z0-9а-яё]+", hay):
        if abs(len(hw) - n) <= limit and _edit_dist_leq(word, hw, limit):
            return True
    return False


def _draw_candidate_boxes(shot: bytes, cands: List[dict]) -> Optional[bytes]:
    """Скриншот вьюпорта (jpeg) + пронумерованные красные рамки вокруг
    кандидатов (визуальный фолбэк резолва, п.4). Координаты элементов —
    CSS-пиксели, скриншот снят со scale='css' (1:1); масштаб по ширине
    картинки против vw снапшота оставлен как страховка, если бэкенд
    вернёт device-scale. На выходе тоже jpeg — в разы легче png для
    пересылки в веб-чат/API. None — Pillow/картинка не сработали
    (фолбэк пропускаем)."""
    try:
        import io
        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(shot)).convert("RGB")
        vw = next((float(it.get("vw")) for it in cands if it.get("vw")),
                  float(img.width))
        scale = img.width / vw if vw else 1.0
        d = ImageDraw.Draw(img)
        for n, it in enumerate(cands, 1):
            x = float(it.get("x") or 0) * scale
            y = float(it.get("y") or 0) * scale
            x2 = x + max(8.0, float(it.get("w") or 0)) * scale
            y2 = y + max(8.0, float(it.get("h") or 0)) * scale
            d.rectangle([x, y, x2, y2], outline=(255, 0, 0), width=3)
            d.text((x + 3, y + 3), str(n), fill=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        return buf.getvalue()
    except Exception:
        return None


# ── Быстрый путь «открой X» ──────────────────────────────

# Вся фраза — одна голая команда открытия/запуска. Такие резолвятся кодом,
# без LLM-пайплайна (fast-path в process_message): экономия ~10+ секунд.
_OPEN_VERB_RE = re.compile(
    r"^(?:открой|открыть|запусти|запустить|включи|включить|open|launch|start)\s+",
    re.IGNORECASE)
_OPEN_FILLER_RE = re.compile(
    r"(?:пожалуйста|плиз|мне|нам|сайт|страницу|страница|вкладку|вкладка|"
    r"приложение|программу|программа)\s+", re.IGNORECASE)
_OPEN_TAIL_RE = re.compile(r"\s+(?:пожалуйста|плиз)\s*$", re.IGNORECASE)

# Поиск на конкретном сайте: «включи интерстеллар на кинопоиске»,
# «открой utopia show на ютуб», «open expedition 33 on youtube»
_SEARCH_ON_SITE_RE = re.compile(
    r"^\s*(включи|включить|найди|найти|поищи|посмотри|посмотреть|глянь|поставь|"
    r"открой|открыть|запусти|запустить|open|play|watch|find|search|launch|start)"
    r"\s+(.+?)\s+(?:на|в|во|on|in)\s+(\S+)\s*[.!?…]*\s*$",
    re.IGNORECASE)

# Глаголы-«поисковики»: с них открывается СТРАНИЦА ПОИСКА сайта, даже если у
# сайта есть regex first. Остальные глаголы — «открыть непосредственно»:
# при наличии first открывается сам первый результат
_SEARCH_PAGE_VERBS = {"найди", "найти", "поищи", "find", "search"}

# Номерные результаты выдачи: «третье видео», «2 результат» → recipe:search_pick:N
_ORDINALS = {
    "первый": 1, "первое": 1, "первая": 1,
    "второй": 2, "второе": 2, "вторая": 2,
    "третий": 3, "третье": 3, "третья": 3,
    "четвёртый": 4, "четвертый": 4, "четвёртое": 4, "четвертое": 4,
    "пятый": 5, "пятое": 5, "пятая": 5,
    "шестой": 6, "шестое": 6, "седьмой": 7, "седьмое": 7,
    "восьмой": 8, "восьмое": 8, "девятый": 9, "девятое": 9,
    "десятый": 10, "десятое": 10,
}
_ORDINAL_TARGET_RE = re.compile(
    r"^(результат|видео|ролик|ссылка|сайт|фильм|сериал|result|video|link)$", re.IGNORECASE)


# Хвост-скоп у номерной команды: «третье видео в плейлисте» / «2 результат
# в выдаче» — скоп срезается; «плейлист» даёт отдельный рецепт
_ORDINAL_SCOPE_RE = re.compile(
    r"\s+(?:в|во|на|in)\s+(плейлисте|плейлиста|плейлист|выдаче|поиске|списке|"
    r"playlist|results)\s*$", re.IGNORECASE)
_ORDINAL_PLAYLIST_SCOPES = {"плейлисте", "плейлиста", "плейлист", "playlist"}


def ordinal_recipe(name: str) -> Optional[str]:
    """«третье видео» / «2 результат» → «search_pick:3»; со скопом плейлиста
    («третье видео в плейлисте») → «playlist_pick:3». None — не такая команда."""
    scope = None
    m = _ORDINAL_SCOPE_RE.search(name)
    if m:
        scope = m.group(1).lower()
        name = name[:m.start()].strip()
    words = name.lower().split()
    if len(words) != 2:
        return None
    w1, w2 = words
    n = _ORDINALS.get(w1) or (int(w1) if w1.isdigit() else None)
    if n is not None and _ORDINAL_TARGET_RE.match(w2):
        if scope in _ORDINAL_PLAYLIST_SCOPES:
            return f"playlist_pick:{n}"
        return f"search_pick:{n}"
    return None


# ── Агентный клик «нажми X» ─────────────────────────────

_CLICK_REQUEST_RE = re.compile(
    r"^\s*(?:нажми|нажать|кликни|кликнуть|тыкни|щёлкни|щелкни|click|press|tap)\s+"
    r"(.+?)\s*[.!?…]*\s*$",
    re.IGNORECASE)
# «кнопку/ссылку» в начале цели срезаем — LLM ищет по тексту элемента
_CLICK_FILLER_RE = re.compile(
    r"^(?:(?:на|по)\s+)?(?:кнопку|кнопка|ссылку|ссылка|пункт|иконку|значок)\s+",
    re.IGNORECASE)
_CLICK_SITE_RE = re.compile(r"\s+(?:на|в|во|on|in)\s+(\S+)\s*$", re.IGNORECASE)
# Скоуп-клик «выбрать на Цезарь с беконом»: действие + контекст карточки.
# Срабатывает только когда плоский матч по тексту элемента ничего не нашёл
_SCOPE_SPLIT_RE = re.compile(r"^(.+?)\s+(?:на|в|во|on|in)\s+(.+)$", re.IGNORECASE)
# Пространственный скоп: «в левой части/панели/разделе», «в части слева»
# (существительное первым — та же мысль), «слева», «справа» — фильтр по
# позиции элемента (x/vw из снапшота), а не по тексту карточки
_SPATIAL_SCOPE_RE = re.compile(
    r"^(?:(лев\w*|прав\w*)\s+(?:част\w*|панел\w*|раздел\w*|сторон\w*|"
    r"половин\w*|колонк\w*|област\w*)|"
    r"(?:част\w*|панел\w*|раздел\w*|сторон\w*|половин\w*|колонк\w*|област\w*)"
    r"\s+(слева|справа)|(слева|справа))$", re.IGNORECASE)
# «омлет сырный справа» — пространственный скоп без предлога (общий сплит
# требует «на/в»). Только слева/справа хвостом, чтобы не ломать обычные цели
_SCOPE_SPATIAL_SPLIT_RE = re.compile(r"^(.+?)\s+(слева|справа)$", re.IGNORECASE)
# «на странице/сайте/вкладке» — не скоп карточки, а пустое указание места:
# такое слово в цель скоупа не возвращаем
_NOOP_SITE_WORDS = frozenset({
    "странице", "страницы", "сайте", "сайта", "вкладке", "вкладки",
    "окне", "окна", "page", "site", "tab"})
# «закрой (модальное) окно/модалку/попап» → цель сводится к «закрыть»: слов
# «окна» в тексте страницы нет, а нужен единственный контрол — крестик
# попапа (подписан «закрыть» ранним проходом снапшота)
_CLOSE_GOAL_RE = re.compile(
    r"\b(?:закр|сверн)\w*\s+(?:модал\w*|попап\w*|диалог\w*|окошк\w*|окн\w*|"
    r"баннер\w*|уведомлен\w*|подсказк\w*|анкет\w*|форм\w*)", re.IGNORECASE)
# Глагол закрытия в начале цели: «закрой окно», «сверни анкету»,
# «закрой соусы к бортикам» — объект в группе 1 (пустой — «закрой» без объекта)
_CLOSE_VERB_RE = re.compile(r"^(?:закр\w+|скро\w*|скрыть|сверн\w*)\s*(.*)$",
                            re.IGNORECASE)
# Объект закрытия без привязки («окно», «модальное окно», «попап», «это») —
# цель сводится к «закрыть»; всё остальное — целевое закрытие по контексту
_CLOSE_GENERIC_RE = re.compile(
    r"^(?:(?:модальн\w*|всплывающ\w*|текущ\w*|это|этот|эту)\s+)?"
    r"(?:окн\w*|окошк\w*|модал\w*|попап\w*|диалог\w*|баннер\w*|уведомлен\w*|"
    r"подсказк\w*|анкет\w*|форм\w*|это|его|её|их)\s*$", re.IGNORECASE)

# Контролы-«разрушители»: если LLM выбрала такой элемент, а в цели нет
# намерения закрывать/удалять — почти всегда промах zero-match резолва
# («сырный в части слева» → крестик модалки). Вето вместо клика.
_DESTRUCTIVE_ROOTS = ("закр", "скрой", "скрыть", "close", "удал", "убрать",
                      "убер", "выйти", "выход", "очист", "сброс")
_DESTRUCTIVE_ICONS = frozenset({"x", "х", "×", "✕", "✖", "✘"})
_DESTRUCTIVE_INTENT_RE = re.compile(
    r"закр|скро\w*|скрыть|close|удал|убрать|убер|выйти|выход|очист|сброс|"
    r"крестик", re.IGNORECASE)


def _destructive_mismatch(goal: str, it: dict) -> bool:
    """Выбранный элемент — контрол закрытия/удаления, а в цели такого
    намерения нет: LLM «угадала» разрушительный клик на zero-match.
    True — клик ветируем (честный отказ безопаснее)."""
    lab = _norm_match(it.get("text") or it.get("aria") or it.get("title"))
    if not lab:
        return False
    words = lab.split()
    if lab not in _DESTRUCTIVE_ICONS and (
            not words or not words[0].startswith(_DESTRUCTIVE_ROOTS)):
        return False
    return not _DESTRUCTIVE_INTENT_RE.search(_norm_match(goal))


# «перетащи/поставь слайдер X на N»: ползунок (input[type=range]/role=slider)
_SLIDER_REQUEST_RE = re.compile(
    r"^\s*(?:перетащи|перетащить|передвинь|передвинуть|двинь|поставь|"
    r"поставить|установи|установить|выставь|выставить)\s+"
    r"(?:(?:ползунок|слайдер)\s+)?(.*?)\s+на\s+(\d{1,4})\s*[.!?…]*\s*$",
    re.IGNORECASE)


def parse_slider_request(
        text: str) -> Optional[Tuple[Tuple[str, int], Optional[str]]]:
    """«перетащи слайдер рабочие часы на 8» → ((«рабочие часы», 8), None).
    None — не команда слайдера. Числовой хвост «на N» обязателен и должен
    завершать фразу — иначе это не про ползунок. Сайт не выделяем («в день»
    из подписи не должно становиться сайтом) — целимся в текущую/названную
    вкладку как есть."""
    if not text or len(text) > 80:
        return None
    m = _SLIDER_REQUEST_RE.match(text)
    if not m:
        return None
    label = m.group(1).strip().strip('"«»').strip()
    if len(label) > 40:
        return None
    return (label, int(m.group(2))), None


# «перейди в режим управления» / «режим управления» — включить computer control;
# вне режима управления CC-команды («нажми», «открой сайт») не перехватываются,
# зато работают напоминания/дела/инвентарь/обучение (в режиме — они молчат)
_CONTROL_MODE_ON_RE = re.compile(
    r"^\s*(?:(?:перейди|переключись|войди|зайди|включи|активируй)\s+"
    r"(?:в\s+|на\s+)?)?режим\s+управлени\w*\s*[.!…]*\s*$",
    re.IGNORECASE)
# «выйди из режима управления» / «выключи режим управления» — выключить
_CONTROL_MODE_OFF_RE = re.compile(
    r"^\s*(?:выйди|выйти|выключи|отключи|покинь|покинуть|деактивируй)\s+"
    r"(?:из\s+)?режима?\s+управлени\w*\s*[.!…]*\s*$",
    re.IGNORECASE)


def parse_control_mode(text: str) -> Optional[bool]:
    """Команда переключения режима управления: True — включить
    («перейди в режим управления»), False — выключить («выйди из режима
    управления»), None — не про режим."""
    if not text or len(text) > 60:
        return None
    if _CONTROL_MODE_OFF_RE.match(text):
        return False
    if _CONTROL_MODE_ON_RE.match(text):
        return True
    return None


def parse_close_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«закрой окно» / «закрой соусы к бортикам (на ютубе)» → (цель С
    глаголом, сайт) — разбор закрытия (generic/целевое) делает
    resolve_click. None — не команда закрытия."""
    if not text or len(text) > 80:
        return None
    m = re.match(r"^\s*(закрой|закрыть|скрой|скрыть|сверни|свернуть)\s+(.+?)\s*[.!?…]*\s*$",
                 text, re.IGNORECASE)
    if not m:
        return None
    goal = f"{m.group(1).lower()} {m.group(2).strip()}"
    site = None
    goal, is_page = _strip_page_ref(goal)
    if is_page:
        site = PAGE_REF
    else:
        sm = _CLICK_SITE_RE.search(goal)
        if sm:
            site = sm.group(1).strip().lower()
            goal = goal[:sm.start()].strip()
    goal = goal.strip().strip('"«»').strip()
    if not goal or len(goal) > 40:
        return None
    return goal, site or None

# Отслеживаемая вкладка как цель: «на этой странице», «на открывшейся», …
PAGE_REF = "__page__"
_PAGE_REF_RE = re.compile(
    r"\s+(?:на|в|во|on|in)\s+(?:этой|той|открывшейся|этой\s+же)\s+"
    r"(?:странице|вкладке)\s*$", re.IGNORECASE)
# Тот же оборот в НАЧАЛЕ цели: «открой на этой странице студентам»
_PAGE_REF_HEAD_RE = re.compile(
    r"^(?:на|в|во|on|in)\s+(?:этой|той|открывшейся|этой\s+же)\s+"
    r"(?:странице|вкладке)\s+", re.IGNORECASE)


def _strip_page_ref(goal: str) -> Tuple[str, bool]:
    """Срезать оборот «на этой/открывшейся странице» с края цели (начало —
    «на этой странице студентам», конец — «студентам на этой странице»)."""
    m = _PAGE_REF_HEAD_RE.match(goal)
    if m:
        return goal[m.end():].strip(), True
    m = _PAGE_REF_RE.search(goal)
    if m:
        return goal[:m.start()].strip(), True
    return goal, False


def parse_click_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«нажми кнопку скачать на гитхабе» → («скачать», «гитхабе»).
    «на этой странице»/«на открывшейся» → сайт PAGE_REF (отслеживаемая
    вкладка). None — не команда клика."""
    if not text or len(text) > 80:
        return None
    m = _CLICK_REQUEST_RE.match(text)
    if not m:
        return None
    goal = m.group(1).strip()
    site = None
    goal, is_page = _strip_page_ref(goal)
    if is_page:
        site = PAGE_REF
    else:
        sm = _CLICK_SITE_RE.search(goal)
        if sm:
            site = sm.group(1).strip().lower()
            goal = goal[:sm.start()].strip()
    goal = _CLICK_FILLER_RE.sub("", goal).strip().strip('"«»').strip()
    if not goal or len(goal) > 40:
        return None
    return goal, site or None


# «перейди на вкладку X» / «переключись на X»: переключение активной вкладки.
# Со словом «вкладку/таб» — безусловно команда переключения; голое «перейди
# на X» — мягкая форма: сначала ищем среди открытых вкладок, промах — сайт
# из алиасов/истории, иначе фраза уходит в обычный диалог (None)
_TAB_SWITCH_RE = re.compile(
    r"^\s*(?:перейди|перейти|переключись|переключи|переключить|покажи|"
    r"показать)\s+(?:на\s+)?(?:вкладку|вкладка|таб|табу|tab)\s+(.+?)"
    r"\s*[.!?…]*\s*$",
    re.IGNORECASE)
_TAB_SWITCH_SOFT_RE = re.compile(
    r"^\s*(?:перейди|перейти|переключись|переключи|переключить)\s+"
    r"(?:на|во)\s+(.{2,60}?)\s*[.!?…]*\s*$",
    re.IGNORECASE)
_TAB_LIST_RE = re.compile(
    r"(?:какие|что за)\s+[^.?!]{0,20}?вкладки|"
    r"(?:покажи|назови)\s+(?:у меня\s+)?(?:открыты\w*\s+)?вкладки|"
    r"список\s+(?:открытых\s+)?вкладок|"
    r"вкладки\s+(?:открыты|какие)",
    re.IGNORECASE)


def parse_tab_switch(text: str) -> Optional[Tuple[str, bool]]:
    """«перейди на вкладку ютуб» → («ютуб», True — явная форма);
    «переключись на гитхаб» → («гитхаб», False — мягкая). None — не команда
    переключения вкладки."""
    if not text or len(text) > 80:
        return None
    m = _TAB_SWITCH_RE.match(text)
    if m:
        goal = m.group(1).strip().strip('"«»').strip()
        return (goal, True) if goal else None
    m = _TAB_SWITCH_SOFT_RE.match(text)
    if not m:
        return None
    goal = m.group(1).strip().strip('"«»').strip()
    # «перейди на эту/текущую страницу» — бессмысленно, не команда
    if not goal or goal.lower() in ("эту страницу", "текущую страницу",
                                    "эту вкладку", "текущую вкладку",
                                    "сайт", "страницу"):
        return None
    return goal, False


def parse_tab_list_query(text: str) -> bool:
    """«какие вкладки открыты» / «покажи список вкладок» → True. Это
    вопрос-чтение: ответ — текст со списком, а не действие."""
    if not text or len(text) > 80:
        return False
    return bool(_TAB_LIST_RE.search(text))


_DOWNLOAD_REQUEST_RE = re.compile(
    r"^\s*(?:скачай|скачать|сохрани|сохранить|download|fetch)\s+"
    r"(.+?)\s*[.!?…]*\s*$",
    re.IGNORECASE)
# «файл/документ» в начале цели скачивания срезаем; в общий клик-филлер их не
# добавляем — там «нажми файл» это про меню «Файл»
_DOWNLOAD_FILLER_RE = re.compile(
    r"^(?:(?:на|по)\s+)?(?:файл|файлы|документ|документы|pdf|ссылку|ссылка)\s+",
    re.IGNORECASE)


def parse_download_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«скачай файл методичку по sql на ciu.nstu.ru» → («методичку по sql»,
    «ciu.nstu.ru»); «на этой странице» → сайт PAGE_REF. None — не команда
    скачивания."""
    if not text or len(text) > 80:
        return None
    m = _DOWNLOAD_REQUEST_RE.match(text)
    if not m:
        return None
    goal = m.group(1).strip()
    site = None
    goal, is_page = _strip_page_ref(goal)
    if is_page:
        site = PAGE_REF
    else:
        sm = _CLICK_SITE_RE.search(goal)
        if sm:
            site = sm.group(1).strip().lower()
            goal = goal[:sm.start()].strip()
    goal = _DOWNLOAD_FILLER_RE.sub("", goal).strip().strip('"«»').strip()
    if not goal or len(goal) > 40:
        return None
    return goal, site or None


def parse_open_on_page(text: str) -> Optional[str]:
    """«открой методические указания на этой странице» / «открой на этой
    странице студентам» → цель клика по отслеживаемой вкладке. None — не
    такая команда (нет оборота «на этой/открывшейся странице»)."""
    if not text or len(text) > 80:
        return None
    t = text.strip().rstrip(".!?…").strip()
    if not _OPEN_VERB_RE.match(t):
        return None
    body = _OPEN_VERB_RE.sub("", t, count=1).strip()
    goal, is_page = _strip_page_ref(body)
    if not is_page:
        return None
    goal = _CLICK_FILLER_RE.sub("", goal).strip().strip('"«»').strip()
    return goal or None


# ── Ввод текста «введи X в поле Y» ────────────────────────

_TYPE_REQUEST_RE = re.compile(
    r"^\s*(?:введи|ввести|напиши|написать|набери|набрать|впиши|вписать|"
    r"заполни|заполнить|type|enter|fill)\s+(.+?)\s*[.!?…]*\s*$",
    re.IGNORECASE)
# «ТЕКСТ в поле ПОЛЕ»: сепаратор поля (крайнее вхождение — сам текст тоже
# может содержать «в поле»)
_TYPE_FIELD_SEP_RE = re.compile(r"\s+(?:в поле|в форму|into|in field)\s+",
                                re.IGNORECASE)
# «привет в поле» — сепаратор на краю без названия поля
_TYPE_FIELD_END_RE = re.compile(r"\s+(?:в поле|в форму|into|in field)\s*$",
                                re.IGNORECASE)
# «в поле ПОЛЕ ТЕКСТ»: ведущий предлог+филлер срезается, дальше поле ищется
# префиксным матчем подписи на странице. Голое «в» («напиши в чат …») — не
# маркер поля, а повод попробовать матч подписи: совпадёт — наше, нет — LLM
_TYPE_FIELD_HEAD_RE = re.compile(r"^(?:в поле|в форму|into|in field)\s+",
                                 re.IGNORECASE)
# Предлог, прилипший к тексту после съёма подписи поля («привет в» → «привет»)
_TYPE_PREP_EDGE_RE = re.compile(
    r"^(?:в|во|на|in|on|at)\s+|\s+(?:в|во|на|in|on|at)$", re.IGNORECASE)
# «мой город»/«город» как текст ввода — подставляется город из местоположения
# пользователя (env_location.json; досье → «Настройки» → местоположение)
_GEO_TEXT_RE = re.compile(r"(?:(?:мой|моего|моём|моем|наш)\s+)?город|my city",
                          re.IGNORECASE)
# Хвост «…и отправь»: после ввода жмём Enter в том же поле (чаты, где кнопка
# отправки — безымянная иконка, как на chat.deepseek.com)
_TYPE_SUBMIT_RE = re.compile(
    r"\s+(?:и\s+)?(?:отправь|отправить|отправляй|пошли|шли|send|submit)\s*$",
    re.IGNORECASE)
# «введи X в поиск»: «поиск» — само название поля, слова «поле» нет. Без
# этого фраза не считалась явной командой ввода и при несовпадении подписей
# уходила в LLM-поток, который «изображал» ввод, ничего не делая
_TYPE_SEARCH_SEP_RE = re.compile(r"\s+(?:в|во)\s+поиск\w*\s*[.!?…]*$",
                                 re.IGNORECASE)


def parse_type_request(text: str) -> Optional[str]:
    """«введи в поле выберите город новосибирск» → тело команды. Поле, текст
    и сайт здесь НЕ разделяются — это делает resolve_type по снапшоту
    страницы (грамматика не различает «в поле ПОЛЕ ТЕКСТ» и «ТЕКСТ в поле
    ПОЛЕ», а подписи полей на странице — различают). None — не команда ввода."""
    if not text or len(text) > 200:
        return None
    m = _TYPE_REQUEST_RE.match(text)
    if not m:
        return None
    body = m.group(1).strip()
    # «введи меня/нас в курс дела» — идиома («расскажи»), не ввод в страницу
    if re.match(r"^(?:меня|нас)\s", body, re.IGNORECASE):
        return None
    return body or None

# Слова, которые командами открытия сайта НЕ являются: сущности соседних фич
# и бытовые объекты из ролеплея — их забирать в fast-path нельзя
_OPEN_STOPLIST = {
    "напоминание", "напоминания", "задачу", "задача", "список", "инвентарь",
    "урок", "курс", "тест", "дверь", "дверцу", "окно", "глаза", "рот", "рту",
    "мне", "нам",  # «открой мне» без названия — не команда, пусть спросит LLM
    "сайт", "страницу", "страница", "вкладку", "вкладка",  # «открой сайт» — что именно?
}


def parse_open_many(text: str) -> Optional[List[str]]:
    """«открой ютуб и запусти музыку» → [«ютуб», «музыку»]. None — не голая
    команда (длинная фраза, стоп-слова). Части после «и» могут иметь свой
    глагол и филлеры («…и сайт нгту»)."""
    if not text or len(text) > 80:
        return None
    t = text.strip().rstrip(".!?…").strip()
    if not _OPEN_VERB_RE.match(t):
        return None
    body = _OPEN_VERB_RE.sub("", t, count=1).strip()
    parts: List[str] = []
    for part in re.split(r"\s+и\s+", body, flags=re.IGNORECASE):
        part = part.strip()
        if _OPEN_VERB_RE.match(part):
            part = _OPEN_VERB_RE.sub("", part, count=1).strip()
        prev = None
        while prev != part:  # филлеры-префиксы срезаем до упора («мне сайт …»)
            prev = part
            part = _OPEN_FILLER_RE.sub("", part, count=1).strip()
        part = _OPEN_TAIL_RE.sub("", part).strip()
        if not part or len(part) > 40 or part.lower() in _OPEN_STOPLIST:
            return None
        parts.append(part)
    return parts or None


def parse_open_request(text: str) -> Optional[str]:
    """Одна голая цель «открой X» → «X». Несколько целей («…и …») — None,
    их разбирает parse_open_many."""
    parts = parse_open_many(text)
    return parts[0] if parts and len(parts) == 1 else None


# Явный адрес где-то в команде открытия: «открой на ciu.nstu.ru/827 студентам — …».
# Схема (http/https) сохраняется, если указана
_URL_TOKEN_RE = re.compile(
    r"\b((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/[^\s]*)?)",
    re.IGNORECASE)
# Хвост после адреса — путь по странице: филлеры/предлоги в начале срезаем,
# сегменты разделяются « - », « — », « > », «→» (дефис — только с пробелами,
# чтобы не рвать слова вроде «англо-русский»)
_NAV_FILLER_RE = re.compile(
    r"^(?:пожалуйста|плиз|мне|нам|сайт|страницу|страница|вкладку|вкладка|"
    r"раздел|пункт)\s+", re.IGNORECASE)
_NAV_PREP_RE = re.compile(r"^(?:на|в|во|on|in)\s+", re.IGNORECASE)
_NAV_SPLIT_RE = re.compile(r"\s+[-–—>]\s+|\s*→\s*")
NAV_MAX_STEPS = 5
# Ожидание загрузки страницы и пауза между попытками снапшота: первая
# загрузка сайта может занять секунды. Паузы после действий — НЕ слепые
# слипы, а ba.wait_dom_idle (стабильность DOM); NAV_SETTLE_SEC — его бюджет
NAV_SETTLE_SEC = 2.0
NAV_LOAD_TIMEOUT_SEC = 10.0
NAV_POLL_SEC = 0.7

# Скоринг кандидатов (п.3/п.4): явный лидер — без LLM; иначе top-N в LLM;
# невалидный ответ — фолбэк на лучшего, если его скор внятен
LEADER_MIN_SCORE = 60.0    # минимум скора для детерминированного выбора
LEADER_MARGIN = 15.0       # минимальный отрыв от второго кандидата
LLM_TOP_N = 5              # столько кандидатов уходит в LLM-промпт
FALLBACK_MIN_SCORE = 50.0  # минимум для фолбэка на лучшего без/после LLM
LLM_WIDE_MAX = 30          # столько элементов снапшота уходит в широкий LLM-резолв

# Синонимы к доступным именам иконочных кнопок: aria-label кнопки —
# «Меню аккаунта», а пользователь зовёт её «аватар». Ключ — слово цели,
# значения — корни-подстроки, засчитываемые за совпадение слова
_GOAL_SYNONYMS = {
    "аватар": ("аккаунт", "профил", "учётн", "учетн"),
    "аватарка": ("аккаунт", "профил", "учётн", "учетн"),
    "ава": ("аккаунт", "профил"),
    "аккаунт": ("профил", "аватар", "учётн", "учетн"),
    "учётка": ("аккаунт", "профил"),
    "учетка": ("аккаунт", "профил"),
    "профиль": ("аккаунт", "аватар", "учётн", "учетн"),
    "бургер": ("гид", "guide"),   # ru-YouTube зовёт кнопку «Гид»; «меню»
    # НЕ синоним: на той же странице есть «Меню аккаунта» — с ним ничья
    "гамбургер": ("гид", "guide"),
    "полоски": ("гид", "guide"),  # «нажми три полоски» — тот же бургер-гид
    "троеточие": ("ещё", "еще", "параметр", "действ"),  # «действ» — основа:
    # prefix-матч ловит и «Меню действий» YouTube, и «Действия»
    "лупа": ("поиск",),
    "колокольчик": ("уведомлен",),
    "колокол": ("уведомлен",),
    "шестерёнка": ("настрой",),
    "шестеренка": ("настрой",),
    "закрой": ("закрыт", "закрыть"),
    "закрытие": ("закрыть",),
    "закрытия": ("закрыть",),
    "крестик": ("закрыт",),
    # «нажми состав» на странице товара — это кнопка «i» («Показать
    # дополнительную информацию» / «Калорийность и состав» в модалке).
    # Синонимы — усечённые префиксы («информац» матчит и «информация»,
    # и «информацию» — _word_in ищет с начала слова)
    "состав": ("информац", "калорийност", "кбжу"),
    "калорийность": ("состав", "информац"),
}

# Однобуквенные/иконочные имена целей, которые не переживают фильтр слов
# (len>=3): «нажми i» — кнопка информации о продукте. Плюс КОМПАУНДЫ
# иконок: составная цель требует совпадения ВСЕХ слов, а «бургер-меню» /
# «три полоски» — одно понятие, «меню»/«три» в тексте кнопки нет
_GOAL_ALIAS = {
    "i": "информация",
    "і": "информация",  # кириллическая i — частая опечатка раскладки
    "бургер меню": "бургер",
    "бургер-меню": "бургер",
    "меню бургер": "бургер",
    "три полоски": "бургер",
    "полоски меню": "бургер",
    "меню полоски": "бургер",
}


def _goal_with_synonyms(goal: str) -> str:
    """Цель + стем-синонимы её слов: целевой снапшот ищет по тексту страницы
    и словаря не знает — «состав» должно находить кнопку «Показать
    дополнительную информацию» (aria), а не только футер «Калорийность и
    состав»."""
    words = re.findall(r"[a-z0-9а-яё]+", _norm_match(goal))
    extra: List[str] = []
    for w in words:
        for s in _GOAL_SYNONYMS.get(w, ()):
            if s not in words and s not in extra:
                extra.append(s)
    return " ".join([*words, *extra]) if extra else goal

# Глаголы-действия в начале цели («заменить барбекю»): в ярусе «слова
# разделены текстом и контекстом» элемент, у которого действие — в его
# собственном тексте (кнопка «Заменить»), сильнее элемента, у которого в
# тексте лишь объект (строка «Барбекю»)
_ACTION_WORD_ROOTS = ("замен", "выбра", "выбер", "поменя", "смени",
                      "переключ", "включ", "измен")


def parse_open_with_url(text: str) -> Optional[Tuple[str, List[str]]]:
    """Команда открытия с ЯВНЫМ адресом в фразе → (токен адреса, шаги пути).
    «открой на ciu.nstu.ru/827 студентам - Технологии баз данных» →
    («ciu.nstu.ru/827», [«студентам», «технологии баз данных»]). Шагов может
    не быть — тогда просто открыть страницу. None — не команда открытия или
    явного адреса нет (тогда шанс есть у parse_open_many)."""
    if not text or len(text) > 200:
        return None
    t = text.strip().rstrip(".!?…").strip()
    if not _OPEN_VERB_RE.match(t):
        return None
    body = _OPEN_VERB_RE.sub("", t, count=1).strip()
    m = _URL_TOKEN_RE.search(body)
    if not m:
        return None
    # Предлог, висевший прямо перед адресом («кутузовой на ciu.nstu.ru»),
    # срезаем точечно — общий хвостовой срез съедал бы контент («б в» → «б»)
    before = re.sub(r"(?:^|\s+)(?:на|в|во|on|in)$", "", body[:m.start()].strip())
    rest = (before + " " + body[m.end():]).strip()
    prev = None
    while prev != rest:  # филлеры/предлоги в начале хвоста — до упора
        prev = rest
        rest = _NAV_FILLER_RE.sub("", rest, count=1).strip()
        rest = _NAV_PREP_RE.sub("", rest, count=1).strip()
    steps = []
    for s in _NAV_SPLIT_RE.split(rest):
        s = s.strip().strip('"«»').strip()
        if s and len(s) <= 60:
            steps.append(s)
    return m.group(1), steps[:NAV_MAX_STEPS]


# Чтение со страницы: «прочитай последнее сообщение (на кладе)»,
# «прочитай страницу», «что ответил клод»
_READ_REQUEST_RE = re.compile(
    r"^\s*(?:прочитай|прочти|зачитай|прочитать|прочесть|read)\s+"
    r"(.+?)\s*[.!?…]*\s*$", re.IGNORECASE)
_READ_LAST_RE = re.compile(
    r"последн\w*\s+(?:сообщени|ответ|реплик|мессаг)|ответ\b|reply|last\s+message",
    re.IGNORECASE)
_READ_PAGE_RE = re.compile(r"страниц|текст|содержим|page", re.IGNORECASE)
# Вопросительная форма: «что (мне) ответил/написал/прислал клод»
_READ_WHAT_RE = re.compile(
    r"^\s*что\s+(?:мне\s+)?(?:ответил|ответила|написал|написала|прислал|прислала)"
    r"\s+(\S+.*?)\s*[.?…]*\s*$", re.IGNORECASE)


def parse_read_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«прочитай последнее сообщение на кладе» → ("last", "кладе");
    «прочитай страницу» → ("page", None); «что ответил клод» → ("last", "клод").
    None — не команда чтения."""
    if not text or len(text) > 120:
        return None
    m = _READ_WHAT_RE.match(text)
    if m:
        return "last", (m.group(1).strip().lower() or None)
    m = _READ_REQUEST_RE.match(text)
    if not m:
        return None
    body = m.group(1).strip()
    site = None
    sm = _CLICK_SITE_RE.search(body)
    if sm:
        site = sm.group(1).strip().lower()
        body = body[:sm.start()].strip()
    if _READ_PAGE_RE.search(body):
        return "page", site
    if _READ_LAST_RE.search(body) or body in ("", "это", "её", "его"):
        return "last", site
    return None


# Филлер в начале запроса («открой ВИДЕО winter is here… на ютуб»): в поиск
# не уходит и не ест лимит длины запроса
_SEARCH_QUERY_FILLER_RE = re.compile(
    r"^(?:видео|ролик|фильм|сериал|трек|песню|песня|музыку|клип|"
    r"video|movie|song|track|clip)(?:\s+|$)", re.IGNORECASE)


def parse_search_on_site(text: str) -> Optional[Tuple[str, str, bool]]:
    """«включи интерстеллар на кинопоиске» → («интерстеллар», «кинопоиске», True).
    Третий элемент — «открыть непосредственно»: False у глаголов-поисковиков
    (найди/поищи/find/search — открываем страницу поиска, а не первый результат).
    None — не поисковая команда на сайте."""
    if not text or len(text) > 120:
        return None
    m = _SEARCH_ON_SITE_RE.match(text)
    if not m:
        return None
    verb = m.group(1).lower()
    query, site_word = m.group(2).strip(), m.group(3).strip().lower()
    query = _SEARCH_QUERY_FILLER_RE.sub("", query).strip()
    if not query or len(query) > 80:
        return None
    return query, site_word, verb not in _SEARCH_PAGE_VERBS


# Standalone «отправь»/«send» — Enter в поле ввода (без ввода текста)
_SEND_REQUEST_RE = re.compile(
    r"^\s*(?:отправь|отправить|отправляй|пошли|шли|send|submit)"
    r"(?:\s+(?:сообщение|мессагу|мессадж|ответ|это|его|её|message|it))?"
    r"(?:\s+(?:на|в|во|on|in)\s+(\S+))?\s*[.!?…]*\s*$", re.IGNORECASE)


def parse_send_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«отправь (сообщение)» → ("send", None); «отправь на кладе» →
    ("send", "кладе"). None — не команда отправки."""
    if not text or len(text) > 60:
        return None
    m = _SEND_REQUEST_RE.match(text)
    if not m:
        return None
    site = (m.group(1) or "").strip().lower()
    return "send", site or None


# «нажми пробел/энтер/эскейп…» — специальные клавиши в страницу: без выбора
# элемента (не агентный клик), клавиша летит в активный фокус/документ —
# плеер (play/pause), игра, модалка. Проверяется ДО parse_click_request:
# «нажми esc» иначе станет целью клика «esc»
_KEY_REQUEST_RE = re.compile(
    r"^\s*(?:нажми|нажать|press)\s+"
    r"(пробел|space|энтер|интер|enter|return|escape|эскейп|esc|tab|таб|"
    r"backspace|бэкспейс)"
    r"(?:\s+(?:на|в|во|on|in)\s+(\S+))?"
    r"(?:[,\s]+(?:пожалуйста|плиз|please))?\s*[.!?…]*\s*$",
    re.IGNORECASE)

# Слово команды → имя клавиши playwright/CDP
_KEY_MAP = {
    "пробел": "Space", "space": "Space",
    "энтер": "Enter", "интер": "Enter", "enter": "Enter", "return": "Enter",
    "escape": "Escape", "эскейп": "Escape", "esc": "Escape",
    "tab": "Tab", "таб": "Tab",
    "backspace": "Backspace", "бэкспейс": "Backspace",
}
# Имя клавиши → русский текст для шаблонов ответа
_KEY_RU = {
    "Space": "пробел", "Enter": "Enter", "Escape": "Escape",
    "Tab": "Tab", "Backspace": "Backspace",
}


def parse_key_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«нажми пробел» → ("Space", None); «press esc на ютуб» → ("Escape",
    "ютуб"); «нажми энтер на этой странице» → ("Enter", PAGE_REF).
    None — не команда клавиши (обычный клик по элементу и т.п.)."""
    if not text or len(text) > 60:
        return None
    t = text.strip()
    is_page = bool(_PAGE_REF_RE.search(t))
    if is_page:
        t = _PAGE_REF_RE.sub("", t)
    m = _KEY_REQUEST_RE.match(t)
    if not m:
        return None
    key = _KEY_MAP.get(m.group(1).lower())
    if key is None:
        return None
    site = ((m.group(2) or "").strip().rstrip(",").strip().lower()) or None
    if is_page:
        site = PAGE_REF
    return key, site


# Медиа-команды плеера — клавишами YouTube (работают и на music.youtube.com):
# пауза/плей — пробел, громкость — стрелки (±10% за нажатие), звук — m.
# Голые слова без «нажми»: «пауза», «тише», «громче» — в режиме управления
_MEDIA_REQUESTS = [
    (re.compile(
        r"^\s*(?:пауза|паузу|поставь\s+на\s+паузу|поставить\s+на\s+паузу|"
        r"сними\s+с\s+паузы|плей|play|"
        r"продолжи(?:ть)?(?:\s+(?:видео|воспроизведение))?)"
        r"\s*[.!…]*\s*$", re.IGNORECASE), ("Space", 1, "toggle")),
    (re.compile(
        r"^\s*(?:(?:сделай\s+)?тише|громкость\s+(?:вниз|меньше)|звук\s+тише|"
        r"убавь\s+(?:громкость|звук)|уменьши\s+(?:громкость|звук)|"
        r"звук\s+(?:вниз|меньше))\s*[.!…]*\s*$", re.IGNORECASE),
     ("ArrowDown", 2, "vol_down")),
    (re.compile(
        r"^\s*(?:(?:сделай\s+)?громче|громкость\s+(?:вверх|больше)|звук\s+громче|"
        r"прибавь\s+(?:громкость|звук)|увеличь\s+(?:громкость|звук)|"
        r"звук\s+(?:вверх|больше)|громкость\s+добавь)\s*[.!…]*\s*$",
        re.IGNORECASE), ("ArrowUp", 2, "vol_up")),
    (re.compile(
        r"^\s*(?:(?:выключи|включи)\s+звук|без\s+звука|мьют|mute|unmute)"
        r"\s*[.!…]*\s*$", re.IGNORECASE), ("m", 1, "mute")),
]


def parse_media_request(text: str):
    """«пауза» / «поставь на паузу» → ("Space", 1, "toggle"); «тише» →
    ("ArrowDown", 2, "vol_down"); «громче» → ("ArrowUp", 2, "vol_up");
    «без звука» → ("m", 1, "mute"). None — не медиа-команда. Кортеж:
    (клавиша playwright, нажатий, вид — для текста ответа)."""
    if not text or len(text) > 60:
        return None
    t = text.strip()
    for rx, val in _MEDIA_REQUESTS:
        if rx.match(t):
            return val
    return None


# Авто-листание «промотай страницу»: бот крутит вкладку короткими шагами,
# пока не скажут «стоп» (или не кончится страница). Оба слова — целые
# короткие сообщения: фраза вроде «ну ладно, листай дальше сам» не команда.
# «промотай раздел слева» — листается внутренняя прокручиваемая панель
# (левая/правая половина вьюпорта), а не окно: у карточек-модалок dodo
# своя колонка скролла. «промотай вверх» — направление вверх (по умолч. вниз)
_SCROLL_START_RE = re.compile(
    r"^\s*(?:промотай|промотать|пролистай|пролистать|полистай|полистать|"
    r"прокрути|прокрутить|проскролль|проскроллить|скролль|скроллить|"
    r"покрути|покрутить|листай|листать|scroll)\b"
    r"(?:\s+(?:эту\s+)?(?:страниц\w+|страничк\w+|лент\w+|фид|лист|ее|её|"
    r"дальше|вниз|ниже))*"
    # Сторона: «раздел слева» и «левый раздел» — один смысл; в обоих
    # порядках («пролистай правую часть» = «пролистай часть справа»)
    r"(?:\s+(?:(прав\w+|лев\w+)\s+(?:раздел\w*|список\w*|панел\w*|блок\w*|"
    r"част\w*|колонк\w*|сторон\w*|половин\w*|меню)|(?:(?:раздел\w*|список\w*|"
    r"панел\w*|блок\w*|част\w*|колонк\w*|меню)\s+)?(слева|справа|left|right)))?"
    r"(?:\s+(вверх|выше|наверх|up))?"
    r"(?:\s+(?:на|в|во|on|in)\s+(\S+))?\s*[.!?…]*\s*$",
    re.IGNORECASE)
# «стоп» — бытовое слово: резолвер пропускает его дальше в диалог, когда
# листание не активно (resolver решает по состоянию менеджера)
_SCROLL_STOP_RE = re.compile(
    r"^\s*(?:стоп|стой|погоди|остановись|останови|остановить|хватит|"
    r"прекрати|прекращай|заканчивай|закончи|закончить|достаточно|stop)"
    r"(?:\s+(?:листать|прокрутку|прокручивать|скроллить|мотать|листание|"
    r"прокрутка|скролл|читать|это|уже))?\s*[.!?…]*\s*$",
    re.IGNORECASE)
# Как часто дозорный поток опрашивает состояние листания в странице
# (анимация крутится сама; поток лишь ловит конец ленты/смерть вкладки)
_SCROLL_POLL_SEC = 0.9


def parse_scroll_request(text: str) -> Optional[Tuple[str, Optional[str], Optional[str], Optional[str]]]:
    """«промотай страницу (на ютубе)» → ("start", сайт|None, None, None);
    «промотай раздел слева» / «пролистай левый раздел» → ("start", None,
    "left", None); «промотай вверх» → ("start", None, None, "up");
    «стоп»/«хватит листать» → ("stop", None, None, None).
    None — не команда листания."""
    if not text or len(text) > 60:
        return None
    t = text.strip()
    m = _SCROLL_START_RE.match(t)
    if m:
        adj = (m.group(1) or "").strip().lower()
        bare = (m.group(2) or "").strip().lower()
        side = None
        if adj:  # «правую часть» / «левый раздел» — прилагательное первым
            side = "left" if adj.startswith("лев") else "right"
        elif bare:
            side = {"слева": "left", "справа": "right",
                    "left": "left", "right": "right"}.get(bare)
        direction = "up" if m.group(3) else None
        return "start", (m.group(4) or "").strip().lower() or None, side, direction
    if _SCROLL_STOP_RE.match(t):
        return "stop", None, None, None
    return None


# ── Корзина сайта: убрать/убавить/прибавить/изменить товар ──
# Филлер-слова в названии товара («гавайскую пиццу» → «гавайскую»): в тексте
# карточки корзины их нет, и all-words матчинг промахивался бы
_CART_FILLER_RE = re.compile(
    r"\b(?:пицц\w*|товар\w*|продукт\w*|штук\w*|шт\.?|порци\w*|позици\w*)\b",
    re.IGNORECASE)
_CART_REMOVE_RE = re.compile(
    r"^\s*(?:убери|удали|выкинь|выбрось|убрать|удалить)\s+(.+?)\s+из\s+"
    r"(?:корзины|заказа)\s*[.!?…]*\s*$", re.IGNORECASE)
_CART_DEC_NUM_RE = re.compile(
    r"^\s*(?:убавь|уменьши|убери|минус)\s+(?:одну|один|1)\s+(.+?)\s*[.!?…]*\s*$",
    re.IGNORECASE)
_CART_DEC_RE = re.compile(
    r"^\s*(?:убавь|уменьши)\s+(.+?)\s*[.!?…]*\s*$", re.IGNORECASE)
_CART_INC_NUM_RE = re.compile(
    r"^\s*(?:добавь|кинь|возьми)\s+ещ[её]\s+(?:одну|один|1)\s+(.+?)\s*[.!?…]*\s*$",
    re.IGNORECASE)
_CART_INC_RE = re.compile(
    r"^\s*(?:прибавь|увеличь|плюс)\s+(.+?)\s*[.!?…]*\s*$", re.IGNORECASE)
_CART_EDIT_RE = re.compile(
    r"^\s*(?:измени|изменить|поменяй)\s+(.+?)\s+в\s+корзине\s*[.!?…]*\s*$",
    re.IGNORECASE)
# «убавь громкость/яркость» — настройки устройства, не корзина
_CART_NOT_PRODUCT_RE = re.compile(
    r"\b(?:громкост\w*|звук\w*|яркост\w*|скорост\w*|свет\w*|температур\w*|"
    r"шрифт\w*|масштаб\w*)\b", re.IGNORECASE)


def parse_cart_request(text: str) -> Optional[Tuple[str, str]]:
    """Команда операции с корзиной сайта → (op, product):
    «убери гавайскую из корзины» → ("remove", "гавайскую");
    «убери одну гавайскую пиццу»/«убавь додстер» → decrease;
    «прибавь колу»/«добавь ещё одну колу» → increase;
    «измени песто в корзине» → edit. None — не команда корзины.
    «убери X» БЕЗ «из корзины» сюда не ловится — остаётся инвентарю бота
    (его маркеры: инвентарь/рюкзак/карман)."""
    t = " ".join(str(text or "").split()).strip()
    if not t or len(t) > 80:
        return None
    op = None
    m = _CART_REMOVE_RE.match(t)
    if m:
        op = "remove"
    if not m:
        m = _CART_DEC_NUM_RE.match(t) or _CART_DEC_RE.match(t)
        if m:
            op = "decrease"
    if not m:
        m = _CART_INC_NUM_RE.match(t) or _CART_INC_RE.match(t)
        if m:
            op = "increase"
    if not m:
        m = _CART_EDIT_RE.match(t)
        if m:
            op = "edit"
    if not m:
        return None
    product = _CART_FILLER_RE.sub(" ", m.group(1))
    product = " ".join(product.split()).strip(" ,.;!?")
    # «плюс один додстер» — квантификатор не часть названия
    product = re.sub(r"^(?:одну|один|1)\s+", "", product)
    if len(product) < 2:
        return None
    if _CART_NOT_PRODUCT_RE.search(product):
        return None
    return op, product


# ── Вопрос о содержимом секции открытой страницы ──
# «что находится в "Добавить по вкусу"?» — бот читает текст секции со
# страницы и подаёт его в общий LLM-поток контекстом (список формулирует
# модель, не шаблон). Секция может не найтись — тогда вопрос молча уходит
# в обычный диалог (решает read_page_section, ошибок пользователю нет)
_PAGE_QUESTION_RE = re.compile(
    r"^\s*(?:а\s+)?что\s+(?:там\s+)?(?:находится|есть|лежит|отображается|"
    r"показывается|имеется)?\s*во?\s+"
    r"(?:блоке\s+|разделе\s+|секции\s+|вкладке\s+|панели\s+|меню\s+|списке\s+)?"
    r"[«\"']?(.+?)[»\"']?\s*[?？!…]*\s*$", re.IGNORECASE)
_PAGE_QUESTION_EN_RE = re.compile(
    r"^\s*what(?:'s|\s+is|\s+are)(?:\s+there)?\s+in\s+(?:the\s+)?"
    r"(?:section\s+|block\s+|panel\s+|menu\s+|list\s+)?"
    r"[«\"']?(.+?)[»\"']?\s*[?？!…]*\s*$", re.IGNORECASE)
# Двухсловный хвост-место: «… на этой странице/сайте/вкладке» — срезается до
# _CLICK_SITE_RE (тот ловит только одно слово после «на»)
_PAGE_QUESTION_TAIL_RE = re.compile(
    r"\s+(?:на|в|во|on|in)\s+(?:этой|этом|открытой|открытом|текущей|текущем|"
    r"the|this|current|open)\s+(?:странице|сайте|вкладке|окне|page|site|tab)$",
    re.IGNORECASE)


def parse_page_question(text: str) -> Optional[Tuple[str, Optional[str], str]]:
    """«что находится в добавить по вкусу?» → ("добавить по вкусу", None, та же строка);
    «что в разделе напитки на додо?» → ("напитки", "додо", "напитки на додо");
    "what's in the drinks section?" → ("drinks", None, "drinks").
    Третий элемент — запрос БЕЗ срезки хвоста: «на двоих» может быть частью
    названия («завтрак на двоих»), а не сайтом — решает read_page_section
    (хвост не алиас и не домен → ищем по полному запросу).
    None — не вопрос о содержимом страницы."""
    t = " ".join(str(text or "").split()).strip()
    if not t or len(t) > 100:
        return None
    m = _PAGE_QUESTION_RE.match(t) or _PAGE_QUESTION_EN_RE.match(t)
    if not m:
        return None
    query = m.group(1).strip(" ,.;")
    query = _PAGE_QUESTION_TAIL_RE.sub("", query).strip(" ,.;")
    full_query = query
    # Хвост «… на додо» — сайт; «на странице/на сайте» — пустое указание места
    site = None
    sm = _CLICK_SITE_RE.search(query)
    if sm:
        cand = sm.group(1).strip().lower()
        query = query[:sm.start()].strip(" ,.;")
        if cand not in _NOOP_SITE_WORDS:
            site = cand
    # Хвостовые слова-места англ. формы: "drinks section" → "drinks"
    tail_words = r"\s+(?:section|block|panel|menu|list)$"
    query = re.sub(tail_words, "", query).strip()
    if site is None:
        # Сайт не выделен — полный запрос равен запросу (фолбэк не нужен)
        full_query = query
    else:
        full_query = re.sub(tail_words, "", full_query).strip()
    if len(query) < 2 or len(query) > 50 or len(query.split()) > 5:
        return None
    return query, site, full_query


class ComputerControlManager:
    """Разбор маркеров, allowlist-валидация, pending-подтверждения, исполнение."""

    def __init__(self, context: str = "default", config: Optional[dict] = None,
                 base_dir: Optional[Path] = None):
        self.context = context
        self._pending: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self.stats = {"markers": 0, "executed": 0, "failed": 0,
                      "confirmed": 0, "declined": 0, "rejected": 0,
                      # Выбор элемента (п.3/п.4 плана): сколько решений принял
                      # детерминированный скоринг и сколько ушло в LLM-фолбэк
                      "choices": 0, "llm_calls": 0,
                      "llm_valid": 0, "llm_invalid": 0}
        self.base_dir = base_dir or Path(f"data/{context}/computer_control")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # Последняя вкладка, открытая/тронутая ботом — цель клика по умолчанию
        self._last_host: Optional[str] = None
        # Стабильный AppleScript-id этой вкладки (macOS): точнее _last_host —
        # «на этой странице …» целится в неё, а не в любую вкладку того же сайта
        self._last_tab_id: Optional[int] = None
        # URL последней страницы — для строки в инструкции LLM и логов
        self._last_url: Optional[str] = None
        # Кэш живых вкладок (обновляется при каждом list_open_tabs) —
        # «хранимый» список для подсказок и аудита
        self._known_tabs: List[dict] = []
        # Авто-листание страницы («промотай страницу» → фон до «стоп»):
        # {thread, stop(Event), box(причина конца), host, tab_id} или None
        self._scroll: Optional[dict] = None
        self._scroll_lock = threading.Lock()
        self.update_config(config)
        # Контекст «с каким сайтом работали» переживает перезапуск процесса
        self._restore_last_page()

    # ── Контекст открытой страницы (диск) ─────────────────

    def _save_last_page(self, url: Optional[str] = None):
        """Последняя открытая ботом страница → last_tab.json: после перезапуска
        бот помнит, с каким сайтом работали. id вкладки не сохраняем — между
        процессами он не стабилен, вкладка находится заново по хосту.
        Служебные хосты веб-чатов не пишем: они не рабочая страница
        пользователя, а попап-детект их уже фильтрует (страховка на диске)."""
        try:
            if not self._last_host:
                return
            from app.features import browser_actions as _ba
            if _ba.is_service_host(self._last_host):
                return
            if url:
                self._last_url = url
            (self.base_dir / "last_tab.json").write_text(
                json.dumps({"host": self._last_host,
                            "url": self._last_url or "",
                            "ts": time.time()}, ensure_ascii=False),
                encoding="utf-8")
        except Exception as e:
            logger.debug(f"[CompControl] last_tab.json не записан: {e}")

    def _restore_last_page(self):
        try:
            data = json.loads(
                (self.base_dir / "last_tab.json").read_text(encoding="utf-8"))
            host = str(data.get("host") or "").strip()
            if host:
                # Грязный контекст прошлых запусков: служебная вкладка веб-чата
                # (chat.deepseek.com и т.п.) — не рабочая страница пользователя
                from app.features import browser_actions as _ba
                if _ba.is_service_host(host):
                    return
                self._last_host = host
                self._last_url = str(data.get("url") or "") or None
                logger.info(f"[CompControl] Контекст страницы с диска: {host}")
        except Exception:
            pass

    def update_config(self, config: Optional[dict]):
        """(Пере)прочитать конфиг: allowlist'ы и confirm применяются на живую
        (правки из веб-настроек — без перезапуска). Pending-подтверждения,
        статистика и аудит-лог сохраняются."""
        cfg = config if isinstance(config, dict) else {}
        self.confirm: bool = bool(cfg.get("confirm", True))
        # Агентный клик «нажми X» — отдельный под-переключатель: клики можно
        # выключить, оставив открытие сайтов/приложений и поиск на сайте
        self.click: bool = bool(cfg.get("click", True))
        # Визуальный фолбэк резолва (п.4): скриншот вьюпорта + vision-модель
        # для иконочных UI, где текстовый скоринг бессилен
        self.vision_fallback: bool = bool(cfg.get("vision_fallback", True))
        # Широкий LLM-резолв (zero-match): скоринг не дал ни одного кандидата
        # (слова пользователя не совпали с подписями на странице) — LLM
        # выбирает элемент из компактного списка снапшота; дешевле vision
        self.llm_wide_resolve: bool = bool(cfg.get("llm_wide_resolve", True))
        self.allow_domains: List[str] = [
            str(d).strip().lower() for d in (cfg.get("allow_domains") or []) if str(d).strip()]
        self.apps: Dict[str, object] = {
            str(k).strip().lower(): v for k, v in (cfg.get("apps") or {}).items()}
        self.tasks: Dict[str, object] = {
            str(k).strip().lower(): v for k, v in (cfg.get("tasks") or {}).items()}
        # Алиасы сайтов (ютуб → youtube.com): поисковый резолв ненадёжен для
        # брендов (DDG по «кинопоиск» не отдаёт kinopoisk.ru в топе) и медленный
        # — личные частые сайты описываются здесь, мгновенно и детерминированно
        self.sites: Dict[str, str] = {}
        for k, v in (cfg.get("sites") or {}).items():
            url = self._normalize_url(str(v))
            if url:
                self.sites[str(k).strip().lower()] = url
        # Поисковые шаблоны «включи X на ютубе»: ключ — слово сайта (как в
        # sites), значение — URL с {q} на месте запроса. Форма словарём
        # ({url, first}) добавляет regex ссылки первого результата — тогда
        # открываем сразу его (само видео/фильм), а не страницу поиска
        self.search_urls: Dict[str, str] = {}
        self.search_first: Dict[str, str] = {}
        for k, v in (cfg.get("search") or {}).items():
            url_t = str(v.get("url") or "") if isinstance(v, dict) else str(v)
            if "{q}" not in url_t:
                continue
            key = str(k).strip().lower()
            self.search_urls[key] = url_t.strip()
            first = str(v.get("first") or "") if isinstance(v, dict) else ""
            if first:
                self.search_first[key] = first
        # Браузерный бэкенд (CDP-порт/профиль/канал/запуск) — применяется
        # в browser_actions; блока нет — действуют дефолты бэкенда
        if isinstance(cfg.get("browser"), dict):
            from app.features import browser_actions as _ba
            _ba.set_browser_config(cfg["browser"])

    # ── Метрики выбора элемента (п.7 плана) ────────────────

    def metrics(self) -> dict:
        """Доля решений о выборе элемента, ушедших в LLM-фолбэк, и доля
        валидных ответов LLM — показывает, где детерминированный слой слабый."""
        s = self.stats
        choices, llm = s.get("choices", 0), s.get("llm_calls", 0)
        return {
            "choices": choices,
            "llm_calls": llm,
            "llm_share": round(llm / choices, 3) if choices else 0.0,
            "llm_valid_share": (round(s.get("llm_valid", 0) / llm, 3)
                                if llm else None),
        }

    # ── Конфиг → промпт ──────────────────────────────────

    def available_apps(self) -> List[str]:
        return sorted(self.apps)

    def available_tasks(self) -> List[str]:
        return sorted(self.tasks)

    def instruction_block(self) -> str:
        """Инструкция о маркерах для system_prompt (когда фича включена).
        На русском: отвечающие модели (в т.ч. маленькие локальные) следуют
        русским инструкциям в русском промпте заметно стабильнее, а прямое
        «у тебя ЕСТЬ доступ» гасит шаблонный отказ «нет доступа к ОС»."""
        domain_rule = ""
        if self.allow_domains:
            domain_rule = f" Разрешены только домены: {', '.join(self.allow_domains)}."
        apps = ", ".join(self.available_apps()) or "(не настроены)"
        tasks = ", ".join(self.available_tasks()) or "(не настроены)"
        # Номерные результаты («второй результат», «третье видео») не перечислены
        # в tasks ключами — сообщаем модели, что такие ключи валидны
        ordinal_note = ""
        if any(str(v).startswith("recipe:") for v in self.tasks.values()):
            ordinal_note = ("  Также доступны номерные результаты выдачи: ключи вида "
                            "«второй результат», «третье видео» (1–10) — как RUN_TASK.\n")
        if self.confirm:
            flow = (
                "Действие НЕ выполняется сразу — пользователь должен подтвердить. "
                "Поэтому твой видимый ответ ОБЯЗАН спрашивать подтверждение в твоём "
                "стиле (например, «Открыть YouTube?»), с маркером в конце."
            )
            example = "Открыть YouTube? [OPEN_URL:youtube.com]"
        else:
            flow = "Действие выполняется сразу — скажи, что именно открываешь или запускаешь."
            example = "Открываю YouTube. [OPEN_URL:youtube.com]"
        block = (
            "[COMPUTER CONTROL — system capability]\n"
            "У тебя ЕСТЬ доступ к компьютеру пользователя: ты можешь открывать сайты, "
            "запускать приложения и выполнять именованные задачи. Никогда не утверждай, "
            "что у тебя нет такого доступа, — действие выполняет система по твоему маркеру.\n"
            "Когда пользователь ЯВНО просит что-то открыть или запустить, добавь ОДИН "
            "маркер в самый конец ответа:\n"
            "  [OPEN_URL:https://example.com] — открыть сайт (только http/https)."
            f"{domain_rule}\n"
            f"  [OPEN_APP:ключ] — запустить приложение. Доступные: {apps}\n"
            f"  [RUN_TASK:ключ] — выполнить задачу. Доступные: {tasks}\n"
            f"{ordinal_note}"
            f"{flow}\n"
            "Пример. Пользователь: «открой ютуб».\n"
            f"Твой ответ: {example}\n"
            "Маркеры используются только по явной просьбе пользователя, никогда — по "
            "твоей инициативе. Существуют только перечисленные ключи приложений и задач, "
            "другие не выдумывай. Маркер скрыт от пользователя; без маркера ничего не "
            "произойдёт. Если не знаешь ТОЧНЫЙ адрес запрошенного сайта — маркер НЕ "
            "ставь и URL не выдумывай: ответь текстом и уточни, какой сайт открыть. "
            "Клики по элементам страницы и ввод текста в поля выполняет отдельная "
            "система по точным фразам пользователя («нажми X», «введи X в поле Y») — "
            "у тебя таких маркеров НЕТ: никогда не пиши от себя «Нажато»/«Введено» "
            "и не описывай результат таких действий — это ложь, без системного "
            "действия ничего не происходит. Листание страницы («промотай страницу») "
            "и его остановка («стоп») — тоже системные команды, не твои маркеры."
        )
        # Контекст открытой страницы: чтобы модель понимала «мы на сайте X»,
        # а не отвечала в отрыве от браузерного контекста
        if self._last_host:
            block += (f"\nСейчас открытая мной страница: {self._last_host}"
                      + (f" ({self._last_url})" if self._last_url else "")
                      + ". Просьбы нажать/ввести/скачать без явного названия "
                        "сайта относятся к ней.")
        return block

    # ── Маркеры ──────────────────────────────────────────

    def process_markers(self, answer: str, chat_id: str) -> Tuple[str, List[str]]:
        """Срезает маркеры из ответа. Возвращает (чистый текст, уведомления
        пользователю). confirm-режим: маркер → pending (уведомлений нет,
        вопрос задаёт текст самого ответа); иначе — исполнение сразу,
        уведомление только при неудаче."""
        notices: List[str] = []
        if not answer:
            return answer, notices
        matches = list(MARKER_RE.finditer(answer))
        if not matches:
            return answer, notices
        clean = MARKER_RE.sub("", answer)
        clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
        accepted: Optional[dict] = None
        for m in matches:
            self.stats["markers"] += 1
            kind = _KIND_BY_MARKER[m.group(1)]
            target = m.group(2).strip()
            action = self._build_action(kind, target)
            if action is None:
                self.stats["rejected"] += 1
                logger.info(f"[CompControl] Маркер отклонён allowlist'ом: "
                            f"{m.group(1)}:{target[:80]}")
                if not self.confirm:
                    notices.append(f"⚠️ Не могу выполнить «{target[:60]}» — нет в списке разрешённых.")
                continue
            accepted = action
            if self.confirm:
                self.set_pending(chat_id, action)
                logger.info(f"[CompControl] Ожидаю подтверждения: {self.describe(action)}")
            else:
                ok, detail = self.execute(action, chat_id)
                if not ok:
                    notices.append(f"⚠️ Не удалось {self.describe(action)}: {detail}")
        if not clean and accepted is not None:
            # Маркер был единственным содержимым ответа — без видимого текста
            # пользователь получит пустое сообщение; подставляем шаблон
            clean = (self.confirm_question(accepted) if self.confirm
                     else f"Готово, {self.describe_done(accepted)}.")
        return clean, notices

    def _build_action(self, kind: str, target: str) -> Optional[dict]:
        """Валидация по allowlist'ам. None — маркер отклонён."""
        if kind == "url":
            alias = self.sites.get(target.strip().lower())
            url = alias or self._normalize_url(target)
            if url is None or (alias is None and not self._domain_allowed(url)):
                return None
            return {"kind": "url", "value": url}
        table = self.apps if kind == "app" else self.tasks
        value = self._resolve_platform(table.get(target.strip().lower()))
        if value is None and kind == "task":
            # «N-ый результат/видео» — номерной рецепт без явного ключа в yaml
            oc = ordinal_recipe(target)
            if oc:
                value = f"recipe:{oc}"
        if value is None:
            return None
        return {"kind": kind, "key": target.strip().lower(), "value": value}

    @staticmethod
    def _resolve_platform(entry) -> Optional[str]:
        """Значение allowlist'а: строка (одна на все ОС) или dict per-OS."""
        if isinstance(entry, str) and entry.strip():
            return entry.strip()
        if isinstance(entry, dict):
            key = {"darwin": "darwin", "win32": "win32"}.get(sys.platform, "linux")
            val = entry.get(key) or entry.get("other")
            if isinstance(val, str) and val.strip():
                return val.strip()
        return None

    @staticmethod
    def _normalize_url(target: str) -> Optional[str]:
        url = target.strip()
        if not url or re.search(r"\s", url):
            return None
        if "://" not in url:
            # Схемоподобный префикс (javascript:, mailto:, data:…) не
            # подменяем https; «host:port/…» (после двоеточия цифра) — можно
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url) and \
                    not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:\d", url):
                return None
            url = "https://" + url
        if urlparse(url).scheme not in ("http", "https"):
            return None  # file:// и прочие схемы — нельзя
        return url

    def _domain_allowed(self, url: str) -> bool:
        if not self.allow_domains:
            return True
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self.allow_domains)

    # ── Pending-подтверждение ────────────────────────────

    def set_pending(self, chat_id: str, action: dict):
        with self._lock:
            self._pending[str(chat_id)] = {
                "action": action, "expires_at": time.time() + PENDING_TTL_SEC}

    def get_pending(self, chat_id: str) -> Optional[dict]:
        with self._lock:
            entry = self._pending.get(str(chat_id))
            if not entry:
                return None
            if time.time() > entry["expires_at"]:
                self._pending.pop(str(chat_id), None)
                return None
            return dict(entry["action"])

    def clear_pending(self, chat_id: str):
        with self._lock:
            self._pending.pop(str(chat_id), None)

    # ── Исполнение ───────────────────────────────────────

    @staticmethod
    def describe(action: dict) -> str:
        if action["kind"] == "multi":
            return " и ".join(
                ComputerControlManager.describe(a) for a in action["items"])
        if action["kind"] == "nav":
            return (f"открыть {action.get('host', '')} и пройти: "
                    f"{' → '.join(action.get('steps', []))}")
        if action["kind"] == "download":
            return f"скачать «{action.get('element', '')}» с {action.get('host', '')}"
        if action["kind"] == "click":
            return f"нажать «{action.get('element', '')}» на {action.get('host', '')}"
        if action["kind"] == "type":
            tail = " и отправить" if action.get("submit") else ""
            return (f"ввести «{str(action.get('text') or '')[:40]}» в поле "
                    f"«{action.get('element', '')}» на {action.get('host', '')}{tail}")
        if action["kind"] == "read":
            what = ("страницу" if action.get("mode") == "page"
                    else "последнее сообщение")
            return f"прочитать {what} на {action.get('host', '')}"
        if action["kind"] == "send":
            return f"отправить сообщение на {action.get('host', '')} (Enter)"
        if action["kind"] == "press":
            return (f"нажать Escape на {action.get('host', '')} "
                    "(закрыть окно)")
        if action["kind"] == "key":
            m = action.get("media")
            if m == "vol_down":
                return f"уменьшить громкость на {action.get('host', '')}"
            if m == "vol_up":
                return f"увеличить громкость на {action.get('host', '')}"
            if m == "mute":
                return f"переключить звук на {action.get('host', '')} (m)"
            if m == "toggle":
                return (f"поставить на паузу/продолжить "
                        f"на {action.get('host', '')}")
            return (f"нажать {_KEY_RU.get(action.get('key'), action.get('key'))}"
                    f" на {action.get('host', '')}")
        if action["kind"] == "slider":
            return (f"перетащить слайдер «{action.get('slider_label', '')}» "
                    f"на {action.get('slider_value', '')} "
                    f"на {action.get('host', '')}")
        if action["kind"] == "scroll":
            _what = "страницу"
            if action.get("side"):
                _what = ("раздел слева" if action.get("side") == "left"
                         else "раздел справа")
            if action.get("dir") == "up":
                _what += " вверх"
            return f"листать {_what} на {action.get('host', '')}"
        if action["kind"] == "scroll_stop":
            return "остановить прокрутку страницы"
        if action["kind"] == "tab_switch":
            return (f"перейти на вкладку "
                    f"«{action.get('element') or action.get('host', '')}»")
        if action["kind"] == "cart":
            op_ru = {"remove": "убрать", "decrease": "убавить",
                     "increase": "прибавить", "edit": "изменить"}.get(
                action.get("op"), "изменить")
            return (f"{op_ru} «{action.get('product', '')}» "
                    f"в корзине на {action.get('host', '')}")
        if action["kind"] == "url":
            return f"открыть {action['value']}"
        if action["kind"] == "app":
            return f"запустить приложение «{action['key']}»"
        return f"выполнить задачу «{action['key']}»"

    @staticmethod
    def _host(action: dict) -> str:
        p = urlparse(action["value"])
        host = (p.hostname or action["value"]).lower().removeprefix("www.")
        # Значимый сегмент пути показываем: «Открыть google.com/maps?»,
        # а не «Открыть google.com?» (последний: путь может начинаться
        # со служебных /intl/ru/…)
        seg = next((s for s in reversed(p.path.split("/")) if s), "")
        return f"{host}/{seg}" if seg else host

    @classmethod
    def confirm_question(cls, action: dict) -> str:
        """Шаблон вопроса на подтверждение для fast-path (без LLM)."""
        if action["kind"] == "multi":
            q = " и ".join(cls.describe(a) for a in action["items"])
            return q[0].upper() + q[1:] + "?"
        if action["kind"] == "nav":
            return (f"Открыть {action.get('host', '')} и пройти: "
                    f"{' → '.join(action.get('steps', []))}?")
        if action["kind"] == "download":
            return f"Скачать «{action.get('element', '')}» с {action.get('host', '')}?"
        if action["kind"] == "click":
            return f"Нажать «{action.get('element', '')}» на {action.get('host', '')}?"
        if action["kind"] == "type":
            tail = " и отправить" if action.get("submit") else ""
            return (f"Ввести «{str(action.get('text') or '')[:40]}» в поле "
                    f"«{action.get('element', '')}» на {action.get('host', '')}{tail}?")
        if action["kind"] == "read":
            what = ("страницу" if action.get("mode") == "page"
                    else "последнее сообщение")
            return f"Прочитать {what} на {action.get('host', '')}?"
        if action["kind"] == "send":
            return f"Отправить сообщение на {action.get('host', '')} (Enter)?"
        if action["kind"] == "press":
            return (f"Нажать Escape на {action.get('host', '')}, "
                    "чтобы закрыть окно?")
        if action["kind"] == "key":
            m = action.get("media")
            if m in ("vol_down", "vol_up", "mute", "toggle"):
                q = cls.describe(action)
                return q[0].upper() + q[1:] + "?"
            return (f"Нажать "
                    f"{_KEY_RU.get(action.get('key'), action.get('key'))} "
                    f"на {action.get('host', '')}?")
        if action["kind"] == "slider":
            return (f"Перетащить слайдер «{action.get('slider_label', '')}» "
                    f"на {action.get('slider_value', '')} "
                    f"на {action.get('host', '')}?")
        if action["kind"] == "scroll":
            _what = "страницу"
            if action.get("side"):
                _what = ("раздел слева" if action.get("side") == "left"
                         else "раздел справа")
            if action.get("dir") == "up":
                _what += " вверх"
            return (f"Начать листать {_what} на {action.get('host', '')}? "
                    "Скажи «стоп», чтобы остановить.")
        if action["kind"] == "scroll_stop":
            return "Остановить прокрутку страницы?"
        if action["kind"] == "tab_switch":
            return (f"Перейти на вкладку "
                    f"«{action.get('element') or action.get('host', '')}»?")
        if action["kind"] == "cart":
            q = cls.describe(action)
            return q[0].upper() + q[1:] + "?"
        if action["kind"] == "url" and action.get("search_query"):
            if action.get("direct"):
                return f"Открыть «{action['search_query']}» на {action['search_site']}?"
            return f"Найти «{action['search_query']}» на {action['search_site']}?"
        if action["kind"] == "url":
            return f"Открыть {cls._host(action)}?"
        if action["kind"] == "app":
            return f"Запустить «{action['key']}»?"
        return f"Выполнить задачу «{action['key']}»?"

    @classmethod
    def describe_done(cls, action: dict) -> str:
        """«Готово, …» — прошедшее время для шаблонного подтверждения."""
        if action["kind"] == "multi":
            return ", ".join(cls.describe_done(a) for a in action["items"])
        if action["kind"] == "nav":
            steps = action.get("steps", [])
            return (f"открыл {action.get('host', '')} и прошёл до "
                    f"«{steps[-1] if steps else ''}»")
        if action["kind"] == "download":
            return f"скачал «{action.get('element', '')}» с {action.get('host', '')}"
        if action["kind"] == "click":
            return f"нажал «{action.get('element', '')}» на {action.get('host', '')}"
        if action["kind"] == "type":
            tail = " и отправил" if action.get("submit") else ""
            return (f"ввёл «{str(action.get('text') or '')[:40]}» в поле "
                    f"«{action.get('element', '')}» на {action.get('host', '')}{tail}")
        if action["kind"] == "send":
            return f"отправил сообщение на {action.get('host', '')}"
        if action["kind"] == "press":
            return (f"нажал Escape на {action.get('host', '')} — "
                    "окно закрыто")
        if action["kind"] == "key":
            # без обещания эффекта: клавиша может не менять видимый DOM
            # (canvas-игры) — честно только само нажатие
            m = action.get("media")
            if m == "vol_down":
                return f"уменьшил громкость на {action.get('host', '')}"
            if m == "vol_up":
                return f"увеличил громкость на {action.get('host', '')}"
            if m == "mute":
                return (f"переключил звук на {action.get('host', '')} "
                        "(m — выкл/вкл)")
            if m == "toggle":
                return (f"нажал пробел на {action.get('host', '')} — "
                        "пауза/продолжение")
            return (f"нажал "
                    f"{_KEY_RU.get(action.get('key'), action.get('key'))} "
                    f"на {action.get('host', '')}")
        if action["kind"] == "slider":
            got = action.get("slider_done") or action.get("slider_value", "")
            return (f"выставил слайдер «{action.get('slider_label', '')}» "
                    f"на {got} на {action.get('host', '')}")
        if action["kind"] == "scroll":
            _what = "страницу"
            if action.get("side"):
                _what = ("раздел слева" if action.get("side") == "left"
                         else "раздел справа")
            if action.get("dir") == "up":
                _what += " вверх"
            return (f"начал листать {_what} на {action.get('host', '')} — "
                    "скажи «стоп», и остановлюсь")
        if action["kind"] == "scroll_stop":
            reason = action.get("end_reason")
            if reason == "bottom":
                return "остановил прокрутку — страница уже была долистана до конца"
            if reason == "lost":
                return "остановил прокрутку — вкладка уже закрылась"
            return "остановил прокрутку"
        if action["kind"] == "tab_switch":
            return (f"переключился на вкладку "
                    f"«{action.get('element') or action.get('host', '')}»")
        if action["kind"] == "cart":
            prod = action.get("product", "")
            op = action.get("op")
            if op == "remove":
                return f"убрал «{prod}» из корзины на {action.get('host', '')}"
            if op == "edit":
                return f"открыл редактирование «{prod}» в корзине"
            qty = action.get("qty_new")
            if op == "decrease" and qty == 0:
                return f"убрал «{prod}» из корзины (была последняя штука)"
            verb = "убавил" if op == "decrease" else "прибавил"
            if qty is None:
                return f"{verb} «{prod}» в корзине"
            return f"{verb} «{prod}» — теперь {qty} шт. в корзине"
        if action["kind"] == "url" and action.get("search_query"):
            if action.get("direct"):
                return f"открыл «{action['search_query']}» на {action['search_site']}"
            return f"открыл поиск «{action['search_query']}» на {action['search_site']}"
        if action["kind"] == "url":
            return f"открыл {cls._host(action)}"
        if action["kind"] == "app":
            return f"запустил «{action['key']}»"
        return f"выполнил задачу «{action['key']}»"

    # ── Резолв для fast-path «открой X» ──────────────────

    @staticmethod
    def _lookup(table: Dict[str, object], key: str) -> Optional[str]:
        """Ключ таблицы: точное совпадение, иначе по основе слова
        («музыку» → «музыка», «на ютубе» → «ютуб»)."""
        if key in table:
            return key
        from app.features.web_search import _stem
        sk = _stem(key)
        if len(sk) < 4:
            return None
        return next((k for k in table if _stem(k) == sk), None)

    def resolve(self, name: str) -> Optional[dict]:
        """«ютуб» → действие: allowlist apps/tasks → алиасы sites → домен с
        точкой → история браузера → лёгкий поисковый резолв сайта.
        None — пусть разбирает LLM-путь."""
        key = " ".join(name.lower().split())
        k = self._lookup(self.apps, key)
        if k is not None:
            value = self._resolve_platform(self.apps[k])
            return {"kind": "app", "key": k, "value": value} if value else None
        k = self._lookup(self.tasks, key)
        if k is not None:
            value = self._resolve_platform(self.tasks[k])
            return {"kind": "task", "key": k, "value": value} if value else None
        k = self._lookup(self.sites, key)
        if k is not None:
            return {"kind": "url", "value": self.sites[k]}
        # «третье видео» / «2 результат» — номерной результат выдачи (recipe
        # search_pick), без явного ключа в yaml
        oc = ordinal_recipe(key)
        if oc:
            return {"kind": "task", "key": key, "value": f"recipe:{oc}"}
        if "." in key and " " not in key:
            url = self._normalize_url(key)
            return {"kind": "url", "value": url} if url and self._domain_allowed(url) else None
        # История браузера: личные частые сайты — персональнее и быстрее поиска
        try:
            from app.features.browser_history import find_in_history
            url = find_in_history(name)
        except Exception as e:
            logger.debug(f"[CompControl] Резолв по истории не удался: {e}")
            url = None
        if url and self._domain_allowed(url):
            return {"kind": "url", "value": url}
        try:
            from app.features.web_search import find_site_url
            url = find_site_url(name)
        except Exception as e:
            logger.debug(f"[CompControl] Резолв сайта не удался: {e}")
            url = None
        if url and self._domain_allowed(url):
            # Поисковый резолв — единственный путь, где адрес не подтверждён
            # ни автором конфига (алиас), ни прошлыми визитами (история):
            # пометка для мягкой верификации title после навигации (п.5)
            return {"kind": "url", "value": url, "expect_name": name}
        return None

    def resolve_url(self, token: str) -> Optional[dict]:
        """Явный адрес из фразы («ciu.nstu.ru/827») → url-действие.
        None — адрес не прошёл нормализацию/whitelist доменов."""
        url = self._normalize_url(token)
        return {"kind": "url", "value": url} if url and self._domain_allowed(url) else None

    def resolve_nav(self, token: str, steps: List[str]) -> Optional[dict]:
        """Адрес + путь по странице («студентам» → «Технологии баз данных») →
        nav-действие. Без шагов — обычное открытие страницы."""
        act = self.resolve_url(token)
        if act is None or not steps:
            return act
        return {"kind": "nav", "value": act["value"], "steps": steps,
                "host": self._host(act)}

    def resolve_many(self, names: List[str]) -> Optional[dict]:
        """«ютуб и музыку» → multi-действие. Резолвятся должны ВСЕ цели,
        иначе None — сообщение целиком уходит в LLM-путь."""
        actions = []
        for n in names:
            a = self.resolve(n)
            if a is None:
                return None
            actions.append(a)
        if not actions:
            return None
        return actions[0] if len(actions) == 1 else {"kind": "multi", "items": actions}

    def resolve_search(self, query: str, site_word: str,
                       direct: bool = True) -> Optional[dict]:
        """Поиск на сайте («интерстеллар», «кинопоиске») → url по шаблону из
        `search`. Сайт-реципиент матчится по основе слова («кинопоиске» →
        «кинопоиск»). Нет шаблона — None (путь LLM).
        direct=True и задан regex `first` — открываем сразу ПЕРВЫЙ результат
        (само видео/фильм), а не страницу поиска; неудача извлечения — фолбэк
        на страницу поиска. direct=False (глаголы найди/поищи/find/search) —
        всегда страница поиска."""
        k = self._lookup(self.search_urls, " ".join(site_word.lower().split()))
        if k is None:
            return None
        from urllib.parse import quote_plus
        url = self.search_urls[k].replace("{q}", quote_plus(query))
        if not self._domain_allowed(url):
            return None
        action = {"kind": "url", "value": url,
                  "search_query": query, "search_site": k}
        if direct:
            first = self._first_result_url(k, url)
            if first and self._domain_allowed(first):
                action["value"] = first
                action["direct"] = True
        return action

    # ── Скоринг кандидатов и выбор элемента (п.3/п.4) ──────

    @staticmethod
    def _score_candidates(items: List[dict], goal: str) -> List[Tuple[float, dict]]:
        """Скоринг вместо бинарного substring-матча: точный текст > точный
        aria-label/title > совпадение по основам слов > слова, разделённые
        текстом и контекстом > частичная подстрока > опечаточное совпадение
        слов (fuzzy) > голый контекст; штрафы за крошечный размер, позицию
        вне вьюпорта и позднее место в DOM-порядке.
        → [(score, item)] по убыванию, только score > 0."""
        from app.features.web_search import _stem
        g = _norm_match(goal)
        g_words = [w for w in re.findall(r"[a-z0-9а-яё]+", g) if len(w) >= 3]
        scored: List[Tuple[float, dict]] = []
        for pos, it in enumerate(items):
            text = _norm_match(it.get("text"))
            aria = _norm_match(it.get("aria"))
            title = _norm_match(it.get("title"))
            hay = f"{text} {aria} {title}"
            ctx = _norm_match(it.get("ctx"))
            strong = [bool(_word_in(w, hay) or _word_in(_stem(w), hay)
                           or any(_word_in(s, hay)
                                  for s in _GOAL_SYNONYMS.get(w, ())))
                      for w in g_words]
            if g and g == text:
                s = 100.0
            elif g and (g == aria or g == title):
                s = 90.0
            elif g_words and all(strong):
                s = 70.0
            elif g_words and ctx \
                    and not _SCOPE_SPLIT_RE.match(" ".join(goal.split())) \
                    and not _SCOPE_SPATIAL_SPLIT_RE.match(" ".join(goal.split())) \
                    and any(_word_in(w, hay) or _word_in(_stem(w), hay)
                            for w in g_words) \
                    and all(_word_in(w, f"{hay} {ctx}")
                            or _word_in(_stem(w), f"{hay} {ctx}")
                            for w in g_words):
                # Слова цели разделились: часть — в тексте элемента,
                # остальные — в контексте места («сырный соус» → кнопка
                # «Сырный · 49 ₽» внутри модалки соусов). Сильнее голого
                # контекста (40), слабее полного совпадения в тексте (70)
                s = 65.0
                if g_words[0].startswith(_ACTION_WORD_ROOTS) and (
                        _word_in(g_words[0], hay)
                        or _word_in(_stem(g_words[0]), hay)):
                    # «заменить барбекю»: действие в ТЕКСТЕ элемента важнее,
                    # чем объект в тексте, а действие в контексте — иначе
                    # строка «Барбекю» перебивала кнопку «Заменить» штрафом
                    # позиции и клик уходил не туда
                    s = 67.0
            elif g and g in hay:
                s = 50.0
            elif g_words and len(g_words) > 1 and sum(strong) >= 1 \
                    and any(any(_word_in(s, hay)
                                for s in _GOAL_SYNONYMS.get(w, ()))
                            for w in g_words):
                # Составная цель с иконкой-синонимом («крестик у джема» →
                # кнопка «Закрыть»): уточняющего слова в тексте кнопки нет,
                # all(strong) не сходится — но совпадение по СИНОНИМУ иконки
                # сильнее контекста (40): раньше такой крестик не находился
                # вовсе (zero-match → LLM-вето)
                s = 52.0
            elif g_words and not all(strong) and all(
                    st or _word_fuzzy_in(w, hay, anchored=any(strong))
                    for st, w in zip(strong, g_words)):
                # Опечатка в слове цели: «кешбэк»→«кэшбек», «красный дук»→
                # «красный лук». Все слова совпали, но хотя бы одно —
                # неточно; ярус ниже полного совпадения (70), выше
                # голого контекста (40): точный кандидат всегда перебьёт
                s = 55.0
            elif g_words and ctx and all(
                    _word_in(w, ctx) or _word_in(_stem(w), ctx) for w in g_words) \
                    and not _SCOPE_SPLIT_RE.match(" ".join(goal.split())) \
                    and not _SCOPE_SPATIAL_SPLIT_RE.match(" ".join(goal.split())):
                # Слова цели — только в контексте предка: кнопка «Выбрать»
                # на карточке «Додстер». Слабый ярус: выигрывает, лишь когда
                # текстовых совпадений нет вовсе. Скоуп-цели («выбрать на
                # Цезарь», «омлет сырный справа») сюда не пускаем — их
                # разруливает _score_scoped
                s = 40.0
            else:
                continue
            if (it.get("w") or 0) < 8 or (it.get("h") or 0) < 8:
                s -= 15.0
            if not it.get("vp", True):
                s -= 10.0
            if it.get("md"):
                # Элемент внутри открытой модалки/диалога: модалка — текущий
                # контекст пользователя, её «Калорийность и состав» важнее
                # одноимённой ссылки в футере страницы
                s += 10.0
            if it.get("dd"):
                # Пункт открытого выпадающего списка: список — то, что
                # пользователь видит прямо сейчас (ещё «горячее» модалки —
                # закроется при любом клике мимо); одноимённый фон страницы
                # (карточка вакансии «Пиццамейкер» при открытом списке
                # вакансий) — не цель
                s += 20.0
            if it.get("sf"):
                # Чип/поле виджета выбора (multiselect/v-select/combobox):
                # «нажми пиццамейкер» — это про открыть список, а не про
                # одноимённую карточку/заголовок (те кликаются впустую)
                s += 20.0
            if it.get("ext"):
                # Внешняя ссылка: уводит со страницы (футер dodo «Калорийность
                # и состав» → drive.google.com), on-page контрол важнее
                s -= 15.0
            s -= min(pos, 20) * 0.5  # штраф за позднюю позицию в DOM
            scored.append((s, it))
        scored.sort(key=lambda x: -x[0])
        return scored

    @staticmethod
    def _score_scoped(items: List[dict], goal: str) -> List[Tuple[float, dict]]:
        """Фолбэк для «выбрать на Цезарь с беконом»: слова действия — в тексте
        самого элемента (кнопка «Выбрать»), слова скопа — в контексте предка
        (поле ctx: текст карточки). Шкала и штрафы — как у _score_candidates,
        +бонус за совпавший скоп, чтобы скоуп-лидер отрывался от мусора.
        Пространственный скоп «в левой/правой части (панели, разделе)» —
        фильтр по ПОЗИЦИИ элемента (центр в своей половине вьюпорта, поля
        x/vw из снапшота), а не по тексту карточки."""
        from app.features.web_search import _stem
        m = _SCOPE_SPLIT_RE.match(" ".join(goal.split()))
        if not m:
            # «омлет сырный справа» — пространственный скоп без предлога
            m = _SCOPE_SPATIAL_SPLIT_RE.match(" ".join(goal.split()))
        if not m:
            return []
        act, scope = _norm_match(m.group(1)), _norm_match(m.group(2))
        act_w = [w for w in re.findall(r"[a-z0-9а-яё]+", act) if len(w) >= 3]
        sc_w = [w for w in re.findall(r"[a-z0-9а-яё]+", scope) if len(w) >= 3]
        if not act_w or (not sc_w and not _SPATIAL_SCOPE_RE.match(scope)):
            return []
        spatial = _SPATIAL_SCOPE_RE.match(scope)
        side = None
        if spatial:
            sw = next((g for g in spatial.groups() if g), "")
            side = "left" if sw.startswith(("лев", "слев")) else "right"
        scored: List[Tuple[float, dict]] = []
        phrase: List[bool] = []
        for pos, it in enumerate(items):
            text = _norm_match(it.get("text"))
            aria = _norm_match(it.get("aria"))
            title = _norm_match(it.get("title"))
            hay = f"{text} {aria} {title}"
            ctx = _norm_match(it.get("ctx"))
            if act and act == text:
                s = 100.0
            elif act and (act == aria or act == title):
                s = 90.0
            elif all(_word_in(w, hay) or _word_in(_stem(w), hay) for w in act_w):
                s = 70.0
            elif act and act in hay:
                s = 50.0
            else:
                continue
            if side:
                # Скоуп-позиция: центр элемента в своей половине вьюпорта.
                # Нет геометрии (старые снапшоты/тесты) — не режем
                vw = float(it.get("vw") or 0)
                if vw:
                    cx = float(it.get("x") or 0) + float(it.get("w") or 0) / 2
                    if side == "left" and cx >= vw / 2:
                        continue
                    if side == "right" and cx < vw / 2:
                        continue
                s += 25.0
                phrase.append(True)
            else:
                scope_hay = f"{hay} {ctx}"
                if not all(_word_in(w, scope_hay) or _word_in(_stem(w), scope_hay) for w in sc_w):
                    continue
                s += 25.0
                # «цезарь с беконом» фразой сильнее, чем «цезарь с сыром и беконом»
                phrase.append(scope in scope_hay)
            if (it.get("w") or 0) < 8 or (it.get("h") or 0) < 8:
                s -= 15.0
            if not it.get("vp", True):
                s -= 10.0
            if it.get("md"):
                s += 10.0  # модальный контекст — как в _score_candidates
            if it.get("dd"):
                s += 20.0  # открытый выпадающий список — как в _score_candidates
            if it.get("sf"):
                s += 20.0  # виджет выбора — как в _score_candidates
            if it.get("ext"):
                s -= 15.0  # внешняя ссылка — как в _score_candidates
            s -= min(pos, 20) * 0.5
            scored.append((s, it))
        # Есть точное фразовое попадание скопа — словесные совпадения отбрасываем
        if len(scored) > 1 and any(phrase):
            scored = [t for t, ph in zip(scored, phrase) if ph]
        scored.sort(key=lambda x: -x[0])
        return scored

    def _choose_element(self, goal: str, items: List[dict],
                        router=None) -> Tuple[Optional[int], dict]:
        """Выбор элемента: явный лидер по скору — без LLM; близкие кандидаты —
        top-5 в LLM, ответ строго одной цифрой; невалидный ответ — фолбэк на
        лучшего по скору (без «докручивания» парсинга) или честный отказ.
        → (idx|None, meta) — meta (путь/кандидаты/сырой ответ LLM) идёт в аудит."""
        meta: Dict[str, object] = {"path": None, "candidates": [],
                                   "llm_response": None}
        scored = self._score_candidates(items, goal)
        if not scored:
            # «выбрать на Цезарь с беконом»: плоско не матчится ни один элемент —
            # пробуем скоуп (кнопка в контексте карточки)
            scored = self._score_scoped(items, goal)
            if scored:
                meta["scoped"] = True
        meta["candidates"] = [
            {"idx": it["idx"], "text": str(it.get("text") or "")[:60],
             "score": round(s, 1)}
            for s, it in scored[:LLM_TOP_N]]
        self.stats["choices"] += 1
        if not scored:
            meta["path"] = "none"
            return None, meta
        top_s, top = scored[0]
        second_s = scored[1][0] if len(scored) > 1 else None
        if second_s is not None and top_s >= LEADER_MIN_SCORE:
            t0 = _norm_match(top.get("text"))
            t1 = _norm_match(scored[1][1].get("text"))
            if t0 and t0 == t1:
                # Одноимённые кандидаты (чип поля и пункт открытого списка
                # «Пиццамейкер», карточка вакансии с тем же текстом): в
                # списке для LLM они неразличимы — её выбор был бы жребием.
                # Берём лучшего по скору: вьюпорт/позиция/контекст открытого
                # списка (dd) уже заложены в баллы
                meta["path"] = "score"
                return int(top["idx"]), meta
        n_llm = min(LLM_TOP_N, len(scored))
        if (top_s >= LEADER_MIN_SCORE
                and (second_s is None or top_s - second_s >= LEADER_MARGIN)) \
                or (top_s >= FALLBACK_MIN_SCORE and second_s is None):
            meta["path"] = "score"  # явный отрыв лидера — LLM не нужна
            return int(top["idx"]), meta
        if router is not None:
            lines = "\n".join(
                f"{n}) [{it.get('tag')}/{it.get('role') or '-'}] "
                f"{str(it.get('text') or '')[:80]}"
                for n, (s, it) in enumerate(scored[:n_llm], 1))
            prompt = (
                f"Задача: нажать «{goal}».\nКандидаты:\n{lines}\n"
                f"Ответь ТОЛЬКО одной цифрой (1-{n_llm}) — номером подходящего "
                "элемента. Если ничего не подходит — ответь «нет».")
            try:
                resp = router.get_response([{"role": "user", "content": prompt}],
                                           temperature=0.0, max_tokens=8, top_p=0.1)
            except Exception as e:
                logger.debug(f"[CompControl] LLM-выбор элемента не удался: {e}")
                resp = None
            self.stats["llm_calls"] += 1
            meta["llm_response"] = (resp or "")[:200]
            m = re.fullmatch(r"\s*(\d{1,2})\s*", resp or "")
            if m and 1 <= int(m.group(1)) <= n_llm:
                picked_it = scored[int(m.group(1)) - 1][1]
                if _destructive_mismatch(goal, picked_it):
                    # LLM ткнула в «закрыть»/«удалить» без такого намерения
                    # в цели — считаем ответ промахом, а не командой
                    self.stats["llm_invalid"] += 1
                    logger.info(f"[CompControl] LLM-выбор «"
                                f"{str(picked_it.get('text'))[:30]}» ветирован "
                                f"(деструктивный без запроса) для «{goal[:40]}»")
                    resp = None
                else:
                    self.stats["llm_valid"] += 1
                    meta["path"] = "llm"
                    return int(picked_it["idx"]), meta
            elif (resp or "").strip().lower().startswith("нет"):
                self.stats["llm_valid"] += 1  # валидный ответ: подходящего нет
                meta["path"] = "none"
                logger.info(f"[CompControl] LLM: нет подходящего элемента "
                            f"для «{goal[:40]}»")
                return None, meta
            else:
                self.stats["llm_invalid"] += 1
                logger.info(f"[CompControl] LLM-ответ невалиден: {(resp or '')[:60]!r}")
        # LLM недоступна/ошиблась — не докручиваем парсинг: фолбэк на лучшего
        # по скору, если он внятен, иначе честный отказ
        if top_s >= FALLBACK_MIN_SCORE:
            meta["path"] = "llm_fallback" if router is not None else "score"
            return int(top["idx"]), meta
        meta["path"] = "none"
        return None, meta

    @staticmethod
    def _element_by_idx(items: List[dict], idx: int) -> Optional[dict]:
        return next((it for it in items if it.get("idx") == idx), None)

    def _snapshot_for(self, site_word: Optional[str], chat_id: str = "",
                      auto_dismiss: bool = True):
        """Общее для клика, скачивания и ввода: вкладка (алиас/явный домен/
        «на этой странице»/отслеживаемая/последняя/активная) и её снапшот;
        отслеживаемую, которая ещё грузится, опрашиваем до NAV_LOAD_TIMEOUT_SEC,
        умершую забываем и падаем на последний хост. Перед снапшотом —
        авто-закрытие типового оверлея (куки/подписка), кроме целей-закрытия.
        → (url, host, items, tab_id, None) или (None, None, None, None, причина)."""
        host_part = None
        tab_id = None
        if site_word == PAGE_REF:
            # «на этой/открывшейся странице» — отслеживаемая вкладка
            tab_id = self._last_tab_id
            if tab_id is None and not self._last_host:
                return None, None, None, None, (
                    "Пока нет открытой мной страницы — сначала «открой …», "
                    "потом уточняй «на этой странице …».")
        elif site_word:
            k = self._lookup(self.sites, " ".join(site_word.lower().split()))
            if k:
                host_part = urlparse(self.sites[k]).hostname
            elif "." in site_word and " " not in site_word.strip():
                # Явный домен без алиаса («на ciu.nstu.ru») — целимся напрямую
                host_part = site_word.strip().lower()
        if host_part is None and tab_id is None:
            # Без указания места: отслеживаемая вкладка точнее хоста
            tab_id = self._last_tab_id
            if tab_id is None:
                host_part = self._last_host
        # Оверлей-блокер (куки-баннер, подписка, geo-попап) снимаем ДО
        # снапшота: он перекрывает контент и съедает бюджет снапшота, а его
        # контролы забирают приоритетные проходы. Цели-закрытия («закрой
        # окно») — исключение: там крестик ищет скоринг, авто-клик мешает
        if auto_dismiss:
            try:
                from app.features import browser_actions as _ba
                dismissed = _ba.dismiss_overlay(host_part, tab_id=tab_id)
            except Exception:
                dismissed = None
            if dismissed:
                self._audit(chat_id, {"kind": "overlay_dismiss",
                                      "value": dismissed,
                                      "host": host_part or self._last_host or ""},
                            True, "auto")
        # Панель плеера YouTube прячется автохайдом — раскрываем ДО снапшота:
        # кнопки паузы/звука/настроек становятся видимыми для скоринга и
        # кликов (и просто видны пользователю). Не-YouTube — тихий no-op
        try:
            from app.features import browser_actions as _ba
            _ba.reveal_player_controls(host_part, tab_id=tab_id)
        except Exception:
            pass
        url = host = items = None
        try:
            from app.features.browser_actions import snapshot_elements
            url, host, items = snapshot_elements(host_part, tab_id=tab_id)
        except Exception as e:
            if tab_id is None:
                logger.info(f"[CompControl] Снапшот страницы не удался: {e}")
                return None, None, None, None, f"Не удалось: {e}"
            # Отслеживаемая вкладка может ещё грузиться (только что открыта,
            # фоновые вкладки Chrome грузятся небыстро) — опрашиваем, прежде
            # чем считать её мёртвой
            deadline = time.time() + NAV_LOAD_TIMEOUT_SEC
            while items is None and time.time() < deadline:
                time.sleep(NAV_POLL_SEC)
                try:
                    url, host, items = snapshot_elements(host_part, tab_id=tab_id)
                except Exception as e2:
                    e = e2
            if items is None:
                # Вкладка умерла (закрыли?) — забываем и пробуем по хосту
                logger.info(f"[CompControl] Отслеживаемая вкладка #{tab_id} "
                            f"недоступна: {e}")
                self._last_tab_id = None
                tab_id = None
                try:
                    url, host, items = snapshot_elements(self._last_host)
                except Exception as e3:
                    logger.info(f"[CompControl] Снапшот страницы не удался: {e3}")
                    return None, None, None, None, f"Не удалось: {e3}"
        return url, host, items, tab_id, None

    @staticmethod
    def _resolve_fail_kind(meta: dict) -> str:
        """Почему выбор элемента не состоялся: пусто в снапшоте / кандидаты
        были, но LLM сказала «нет» / кандидаты были, но скор слабый."""
        if not meta.get("candidates"):
            return "not_in_snapshot"
        if str(meta.get("llm_response") or "").strip().lower().startswith("нет"):
            return "llm_veto"
        return "low_score"

    def _scroll_hunt(self, _ba, host: str, tab_id: Optional[int],
                     search_goal: str):
        """Доскролл-поиск цели для виртуализированных списков/лент: текст цели
        появляется в DOM только после прокрутки в область. Фаза 1 — до 3
        экранов ОКНА вниз; фаза 2 — крупнейший внутренний контейнер (очередь
        YouTube #items: окно стоит, а пункты подгружаются только при
        прокрутке самого списка), до 10 шагов; перед фазой 2 окно
        возвращается на исходную позицию, иначе список уже за пределами
        вьюпорта и контейнерный шаг его не видит. Пересъёмка целевого
        снапшота после каждого шага; промах — прокрутку возвращаем, где была
        (пользователь не должен обнаружить страницу уехавшей).
        → (url, items) последнего целевого снапшота."""
        from app.features.browser_actions import snapshot_for_goal
        y0 = _ba.scroll_position(host, tab_id)
        g_url, g_items = "", []
        for _ in range(3):
            step = _ba.scroll_step(host, tab_id)
            if not step.get("moved"):
                break
            _ba.wait_dom_idle(host, tab_id, timeout_sec=1.5, min_wait=0.2)
            try:
                g_url, g_items = snapshot_for_goal(host, search_goal,
                                                   tab_id=tab_id)
            except Exception:
                g_items = []
            if g_items:
                logger.info(f"[CompControl] «{search_goal[:40]}» нашлось "
                            "после доскролла")
                break
        if not g_items:
            # Фаза 2: виртуализированный список ВНУТРИ страницы (очередь
            # плеера) — окно его не прокручивает. Сначала возвращаем ОКНО на
            # исходную позицию: фаза 1 проскроллила страницу вниз, и
            # внутренний контейнер (панель плейлиста YouTube, #items в
            # ytd-playlist-panel-renderer) уехал из вьюпорта — а шаг
            # контейнера видит только ВИДИМЫЕ контейнеры и крутил бы левое
            # меню вместо списка (кейс 03.09: «включи X из плейлиста» не
            # находил ничего за пределами отрендеренных пунктов)
            if y0 is not None:
                _ba.scroll_restore(host, tab_id, y0)
            cy0 = None
            for _ in range(10):
                step = _ba.scroll_container_step(host, tab_id)
                if cy0 is None and step.get("y0") is not None:
                    cy0 = step["y0"]
                if not step.get("moved"):
                    break
                _ba.wait_dom_idle(host, tab_id, timeout_sec=1.5, min_wait=0.2)
                try:
                    g_url, g_items = snapshot_for_goal(host, search_goal,
                                                       tab_id=tab_id)
                except Exception:
                    g_items = []
                if g_items:
                    logger.info(f"[CompControl] «{search_goal[:40]}» нашлось "
                                "после доскролла списка")
                    break
            if cy0 is not None and not g_items:
                _ba.scroll_container_restore(host, tab_id, cy0)
        if not g_items and y0 is not None:
            _ba.scroll_restore(host, tab_id, y0)
        return g_url, g_items

    def _resolve_element(self, goal: str, site_word: Optional[str], router,
                         chat_id: str = "", auto_dismiss: bool = True):
        """Общее для клика и скачивания: вкладка и снапшот (_snapshot_for),
        скоринг, при неоднозначности — выбор номера через LLM (top-5, один
        токен). → (url, host, items, idx, tab_id, meta, None) или (None…, причина).
        Неудачи резолва пишутся в audit.jsonl с классом причины (_audit_resolve):
        раньше они в лог не попадали вообще и чинились вслепую."""
        from app.features import browser_actions as _ba
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id, auto_dismiss=auto_dismiss)
        if err:
            self._audit_resolve(
                chat_id, goal, None, err,
                "no_page" if str(err).startswith("Пока нет") else "snapshot_error")
            return None, None, None, None, None, None, err
        # Активная вкладка — наш чат: кликать там нечего, а другую вкладку
        # гадать опасно (промах по чужому сайту хуже отказа)
        p = urlparse(url)
        if p.hostname in ("localhost", "127.0.0.1") and p.port in (5173, 8000):
            return None, None, None, None, None, None, (
                "Сейчас активна вкладка чата — там кликать нечего. "
                "Назови сайт («нажми X на ютубе») или переключись на страницу.")
        idx, meta = self._choose_element(goal, items, router)
        gen_top = meta["candidates"][0]["score"] if meta["candidates"] else None
        # Слабый лидер общего снапшота (ниже точного попадания в текст/aria)
        # — цель могла просто не влезть в его бюджет: «соусы» уехало в
        # «2 соуса 89 ₽» по основе слова, а карточка «Соусы» не влезла в
        # сотню. Даём целевому снапшоту шанс найти точный текст по всему DOM
        # и заменяем выбор, только если он увереннее
        if idx is None or (gen_top is not None and gen_top < 90.0):
            # Элемент мог просто не влезть в снапшот: бюджет 100 на богатых
            # страницах съедают шапка и верхние разделы каталога (dodo: 350+
            # кликабельных — «Додстер» из закусок в снапшоте отсутствует).
            # Целевой снапшот: по ВСЕМУ DOM элементы, чей текст содержит цель
            # (для скоуп-цели «выбрать на Цезарь» ищем скоуп — «цезарь»)
            from app.features.browser_actions import snapshot_for_goal
            m_scope = _SCOPE_SPLIT_RE.match(" ".join(goal.split()))
            search_goal = goal
            if m_scope:
                # «сырный в части слева»: искать на странице «части слева»
                # бессмысленно — для пространственного скопа ищем ДЕЙСТВИЕ
                # («сырный»); для карточного («выбрать на Цезарь») — скоп
                search_goal = m_scope.group(1) \
                    if _SPATIAL_SCOPE_RE.match(_norm_match(m_scope.group(2))) \
                    else m_scope.group(2)
            search_goal = _goal_with_synonyms(search_goal)
            try:
                g_url, g_items = snapshot_for_goal(host, search_goal,
                                                   tab_id=tab_id)
            except Exception as e:
                g_items = []
                logger.debug(f"[CompControl] Целевой снапшот не удался: {e}")
            if not g_items:
                # Виртуализированный список/бесконечная лента: цель не
                # отрендерена, пока её не доскроллили — доскролл-поиск
                g_url, g_items = self._scroll_hunt(_ba, host, tab_id,
                                                   search_goal)
            if g_items:
                g_idx, g_meta = self._choose_element(goal, g_items, router)
                llm_veto = str(g_meta.get("llm_response") or "")
                if g_idx is None and not llm_veto.strip().lower().startswith("нет"):
                    # Кандидаты уже отфильтрованы по тексту цели на странице:
                    # единственный из них безопасен и без LLM (а вето LLM
                    # уважаем — она посмотрела и сказала «нет»)
                    sole = self._score_candidates(g_items, goal) \
                        or self._score_scoped(g_items, goal)
                    if len(sole) == 1:
                        g_idx = int(sole[0][1]["idx"])
                        g_meta = {"path": "goal_sole",
                                  "candidates": [{
                                      "idx": g_idx,
                                      "text": str(sole[0][1].get("text") or "")[:60],
                                      "score": round(sole[0][0], 1)}],
                                  "llm_response": None}
                if g_idx is None and m_scope \
                        and not _SPATIAL_SCOPE_RE.match(
                            _norm_match(m_scope.group(2))) \
                        and not llm_veto.strip().lower().startswith("нет"):
                    # Скоуп — секция страницы («сыры чеддер и пармезан В
                    # ДОБАВИТЬ ПО ВКУСУ»), а не текст карточки: её слов нет в
                    # ctx контролов секции, и скоуп-скоринг полной цели пуст.
                    # Целевой снапшот УЖЕ отфильтровал область страницы —
                    # выбираем по части-действию. Для «выбрать на Цезарь»
                    # (кнопка в карточке) не сработает — там скоуп-скоринг
                    # выше отрабатывает; для пространственного скопа часть-
                    # действие теряет сторону — его пропускаем
                    a_idx, a_meta = self._choose_element(
                        m_scope.group(1), g_items, router)
                    if a_idx is not None:
                        a_meta["act_part"] = True
                        g_idx, g_meta = a_idx, a_meta
                if g_idx is not None:
                    g_score = next((c["score"] for c in g_meta["candidates"]
                                    if c["idx"] == g_idx), 0.0)
                    # Равный скор — тоже заменяем: кандидаты целевого
                    # снапшота уже отфильтрованы по тексту цели на странице,
                    # а общий лидер может быть совпадением по основе слова
                    # («соусы» → «2 соуса 89 ₽» — добавил бы лишнее в заказ)
                    if idx is None or g_score >= (gen_top or 0.0):
                        g_meta["via"] = "goal_snapshot"
                        url, items, idx, meta = g_url or url, g_items, g_idx, g_meta
        if idx is None:
            # Элемента нет на целевой вкладке. Сайт назван явно — ищем по
            # остальным вкладкам ЭТОГО ЖЕ хоста (page_for берёт последнюю из
            # подходящих, а пользователь может смотреть другую); сайт не
            # назван — по всем страницам (окно входа могло открыться раньше
            # клика, треки его не поймали). В обоих случаях берём только
            # ЕДИНСТВЕННЫЙ явный лидер без LLM — гадать опаснее отказа
            alt = self._element_on_other_pages(
                goal, url, only_host=host if site_word else None)
            if alt is not None:
                url, host, items, idx, tab_id, meta = alt
        if idx is None:
            # Контент мог не дорендериться после прошлого действия (панель
            # выбора dodo подгружается лениво ~1с): ждём стабилизации DOM
            # (не слепой слип) + ОДИН повторный снапшот с обычным выбором.
            # Второй промах — уже честный отказ
            logger.info(f"[CompControl] «{goal[:40]}» не нашлось — повторный "
                        "снапшот после стабилизации DOM")
            _ba.wait_dom_idle(host, tab_id, timeout_sec=3.0, min_wait=1.0)
            url2, host2, items2, tab_id2, err2 = self._snapshot_for(
                site_word, chat_id=chat_id, auto_dismiss=auto_dismiss)
            if not err2:
                idx2, meta2 = self._choose_element(goal, items2, router)
                if idx2 is not None:
                    meta2["retried"] = True
                    return url2, host2, items2, idx2, tab_id2, meta2, None
                meta = meta2  # свежая картина кандидатов — в аудит
                items = items2  # и свежие метки idx — для визуального фолбэка
        if idx is None:
            # Широкий LLM-резолв (zero-match): скоринг не дал ни одного
            # кандидата — слова цели не совпали с подписями на странице
            # («почта» при «Электронная почта»). LLM выбирает из компактного
            # списка элементов снапшота: дешевле скриншота vision-фолбэка
            # и закрывает кейс «другое название»
            widx, wmeta = self._llm_wide_pick(goal, items, router)
            if widx is not None:
                return url, host, items, widx, tab_id, wmeta, None
            if wmeta is not None:
                meta = wmeta  # LLM посмотрела страницу — её вердикт в аудит
        if idx is None:
            # Визуальный фолбэк (п.4): текстовый скоринг структурно бессилен
            # при иконочных UI (пустые accessible name) — скриншот вьюпорта
            # с пронумерованными рамками кандидатов в vision-модель
            vidx, vmeta = self._visual_resolve(host, tab_id, items, goal, router)
            if vidx is not None:
                return url, host, items, vidx, tab_id, vmeta, None
        if idx is None:
            fail_kind = self._resolve_fail_kind(meta)
            # Антибот-стена: ретраи выше уже отработали, дальше — только
            # ручное прохождение проверки; честный отказ вместо «не нашёл»
            antibot = None
            try:
                antibot = _ba.detect_antibot(host, tab_id)
            except Exception:
                pass
            if antibot:
                fail_kind = "captcha"
                reason = (f"Похоже, {host} показывает проверку «я не робот» "
                          f"({antibot}) — пройди её в браузере вручную и повтори.")
            else:
                reason = f"На странице {host} не нашёл элемента для «{goal}»."
            self._audit_resolve(chat_id, goal, host, reason, fail_kind,
                                meta=meta)
            return None, None, None, None, None, meta, reason
        return url, host, items, idx, tab_id, meta, None

    def _llm_wide_pick(self, goal: str, items: List[dict], router,
                       for_field: bool = False
                       ) -> Tuple[Optional[int], Optional[dict]]:
        """Широкий LLM-резолв (zero-match): текстовый скоринг не дал ни
        одного кандидата — слова пользователя не совпали с подписями на
        странице («почта» при поле «Электронная почта», иконка без aria).
        LLM получает компактный список интерактивных элементов снапшота и
        выбирает номер — тот же договор, что у top-5 в _choose_element, но
        без предфильтра скорингом. Ярус срабатывает только на пути
        гарантированного отказа, поэтому детерминированные попадания не
        вытесняет. → (idx, meta) либо (None, None): фича выключена, нет
        router/кандидатов, ошибка вызова; (None, meta): LLM сказала «нет»
        или ответ невалиден (meta — в аудит причины отказа)."""
        if not self.llm_wide_resolve or router is None:
            return None, None
        # Безымянные элементы (ни текста, ни aria — иконки-SVG) текстовой
        # LLM нечем сопоставить с целью — их разбирает vision-фолбэк
        named = [it for it in items
                 if it.get("text") or it.get("aria") or it.get("title")]
        # Порядок для промпта: для цели ввода — поля первыми, затем видимые
        # во вьюпорте, дальше в DOM-порядке; компактность важнее полноты
        pool = sorted(
            named,
            key=lambda it: (0 if (for_field and it.get("ed")) else 1,
                            0 if it.get("vp", True) else 1))
        pool = pool[:LLM_WIDE_MAX]
        if not pool:
            return None, None

        def _line(n: int, it: dict) -> str:
            lab = str(it.get("text") or it.get("aria") or it.get("title") or "")
            ctx = str(it.get("ctx") or "")
            # Короткая подпись («закрыть», «×») без контекста блока LLM не
            # привязать к скоуп-цели («закрыть на корзина» — крестик сам по
            # себе «корзины» не содержит) — добавляем контекст
            if ctx and len(lab) <= 15:
                lab = f"{lab} (блок: {ctx[:60]})"
            return (f"{n}) [{it.get('tag')}/{it.get('role') or '-'}] "
                    f"{lab[:120]}")

        lines = "\n".join(_line(n, it) for n, it in enumerate(pool, 1))
        task = "выбрать поле ввода" if for_field else "нажать"
        scope_hint = ""
        if not for_field:
            m_sc = _SCOPE_SPLIT_RE.match(" ".join(goal.split()))
            if m_sc:
                # Скоуп-форма «закрыть на корзина»: LLM ищет в списке всю
                # фразу и, не находя, отвечает «нет» — поясняем, что искомый
                # элемент может называться только действием («закрыть»)
                scope_hint = (f" (элемент «{m_sc.group(1)}», относящийся к "
                              f"«{m_sc.group(2)}»; может быть подписан "
                              f"просто «{m_sc.group(1)}»)")
        prompt = (
            f"Задача: {task} «{goal}»{scope_hint}.\nЭлементы страницы:\n{lines}\n"
            f"Ответь ТОЛЬКО одной цифрой (1-{len(pool)}) — номером подходящего "
            "элемента. Если ничего не подходит — ответь «нет».")
        try:
            resp = router.get_response([{"role": "user", "content": prompt}],
                                       temperature=0.0, max_tokens=8, top_p=0.1)
        except Exception as e:
            logger.debug(f"[CompControl] Широкий LLM-резолв не удался: {e}")
            return None, None
        self.stats["llm_calls"] += 1
        meta: Dict[str, object] = {
            "path": "llm_wide",
            "candidates": [{"idx": int(it["idx"]),
                            "text": str(it.get("text") or "")[:60],
                            "score": 0.0} for it in pool[:LLM_TOP_N]],
            "llm_response": str(resp or "")[:200]}
        m = re.fullmatch(r"\s*(\d{1,2})\s*", str(resp or ""))
        if m and 1 <= int(m.group(1)) <= len(pool):
            picked = pool[int(m.group(1)) - 1]
            if _destructive_mismatch(goal, picked):
                # Zero-match промах: LLM ткнула в крестик/закрытие, хотя в
                # цели намерения закрывать нет («сырный в части слева» →
                # «Закрыть») — честный отказ вместо разрушительного клика
                self.stats["llm_valid"] += 1
                meta["llm_response"] = f"{resp} → вето (закрытие без запроса)"
                logger.info(f"[CompControl] Широкий резолв «{goal[:40]}»: "
                            f"выбор «{str(picked.get('text'))[:30]}» "
                            "ветирован (деструктивный без намерения)")
                return None, meta
            self.stats["llm_valid"] += 1
            idx = int(picked["idx"])
            logger.info(f"[CompControl] «{goal[:40]}» выбрано широким "
                        f"LLM-резолвом: кандидат {m.group(1)} (idx {idx})")
            return idx, meta
        if str(resp or "").strip().lower().startswith("нет"):
            self.stats["llm_valid"] += 1
            logger.info(f"[CompControl] Широкий LLM-резолв: нет подходящего "
                        f"элемента для «{goal[:40]}»")
        else:
            self.stats["llm_invalid"] += 1
            logger.info(f"[CompControl] Широкий LLM-резолв: ответ невалиден: "
                        f"{str(resp or '')[:60]!r}")
        return None, meta

    def _visual_resolve(self, host: str, tab_id: Optional[int],
                        items: List[dict], goal: str, router):
        """Визуальный фолбэк резолва (п.4): весь текстовый скоринг бессилен,
        когда у кандидатов пустые accessible name (иконочные тулбары). Скриншот
        вьюпорта + пронумерованные рамки кандидатов → vision-модель выбирает
        номер (тот же договор, что top-5 у текстовой LLM, но по картинке).
        → (idx, meta) или (None, None): фича выключена, нет vision, нет
        скриншота/кандидатов, ответ невалиден."""
        if not self.vision_fallback or router is None:
            return None, None
        try:
            if not router.supports_vision():
                return None, None
        except Exception:
            return None, None
        # Кандидаты — видимые и не мелкие; безымянные впереди (именно для них
        # фолбэк), затем по убыванию площади
        cands = [it for it in items
                 if it.get("vp") and (it.get("w") or 0) >= 10
                 and (it.get("h") or 0) >= 10]
        cands.sort(key=lambda it: (bool(it.get("text") or it.get("aria")),
                                   -(float(it.get("w") or 0)
                                     * float(it.get("h") or 0))))
        cands = cands[:8]
        if not cands:
            return None, None
        from app.features import browser_actions as ba
        shot = ba.screenshot_viewport(host, tab_id)
        if not shot:
            return None, None
        boxed = _draw_candidate_boxes(shot, cands)
        if boxed is None:
            return None, None
        lines = "\n".join(
            f"{n}) {str(it.get('text') or it.get('aria') or it.get('tag') or '?')[:40]}"
            for n, it in enumerate(cands, 1))
        prompt = (
            f"Скриншот страницы браузера. Красные рамки отмечают элементы "
            f"1..{len(cands)}:\n{lines}\n"
            f"Какой из них — «{goal}»? Ответь ТОЛЬКО цифрой. "
            "Если ничего не подходит — ответь «нет».")
        try:
            resp = router.get_response_with_image(prompt, boxed,
                                                  image_mime="image/jpeg")
        except Exception as e:
            logger.debug(f"[CompControl] Визуальный фолбэк не удался: {e}")
            return None, None
        m = re.fullmatch(r"\s*(\d{1,2})\s*", str(resp or ""))
        if not m or not (1 <= int(m.group(1)) <= len(cands)):
            return None, None
        picked = cands[int(m.group(1)) - 1]
        if _destructive_mismatch(goal, picked):
            logger.info(f"[CompControl] Визуальный выбор «"
                        f"{str(picked.get('text') or picked.get('aria'))[:30]}» "
                        f"ветирован (деструктивный без запроса) для «{goal[:40]}»")
            return None, None
        idx = int(picked["idx"])
        logger.info(f"[CompControl] «{goal[:40]}» выбрано визуально: "
                    f"кандидат {m.group(1)} (idx {idx})")
        return idx, {"path": "vision",
                     "candidates": [{"idx": int(it["idx"]),
                                     "text": str(it.get("text") or "")[:60],
                                     "score": 0.0} for it in cands],
                     "llm_response": str(resp or "")[:200]}

    def _element_on_other_pages(self, goal: str, cur_url: str,
                                only_host: Optional[str] = None):
        """Кросс-страничный поиск элемента: снапшот каждой открытой страницы
        (кроме текущей, чата и пустых; only_host — только вкладки этого сайта,
        когда сайт назван явно), детерминированный явный лидер ровно на одной
        странице → (url, host, items, idx, tab_id, meta); иначе None.
        Снапшот целим по полному URL (host_part — подстрока URL): по голому
        хосту page_for взял бы одну и ту же вкладку для всех страниц сайта."""
        from app.features import browser_actions as ba
        try:
            pages = ba.list_pages()
        except Exception:
            return None
        found = None
        for purl, phost in pages:
            if not phost or purl == cur_url:
                continue
            if phost in ("localhost", "127.0.0.1"):
                continue  # чат и служебное — не кликаем
            if only_host and phost != only_host:
                continue
            try:
                u2, h2, items2 = ba.snapshot_elements(purl)
            except Exception:
                continue
            scored = self._score_candidates(items2, goal) \
                or self._score_scoped(items2, goal)
            if not scored:
                continue
            top_s = scored[0][0]
            second_s = scored[1][0] if len(scored) > 1 else None
            if top_s < LEADER_MIN_SCORE or (
                    second_s is not None and top_s - second_s < LEADER_MARGIN):
                continue  # явного лидера на этой странице нет
            if found is not None:
                logger.info(f"[CompControl] «{goal[:40]}»: лидеры на двух "
                            f"страницах ({found[1]}, {h2}) — не гадаю")
                return None  # две страницы с лидером — честный отказ
            found = (u2, h2, items2, int(scored[0][1]["idx"]),
                     ba.find_tab_id(purl),
                     {"path": "page_fallback", "candidates": [
                         {"idx": scored[0][1]["idx"],
                          "text": str(scored[0][1].get("text") or "")[:60],
                          "score": round(top_s, 1)}],
                      "llm_response": None})
        if found is not None:
            logger.info(f"[CompControl] «{goal[:40]}» нашлось на другой "
                        f"странице: {found[1]}")
        return found

    def resolve_click(self, goal: str, site_word: Optional[str],
                      router, chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """«нажми „скачать“ (на гитхабе)» → (действие click, None) или
        (None, текст причины). «выбрать на Цезарь с беконом» — скоуп-клик:
        кнопка ищется в контексте карточки. «закрой окно» — крестик;
        «закрой соусы к бортикам» — целевое закрытие: крестик в контексте
        названного блока, промах — обычный крестик. LLM-путь клик не получает
        никогда: «сыграть» его он может."""
        if site_word and site_word != PAGE_REF:
            k = self._lookup(self.sites, " ".join(site_word.lower().split()))
            if not k and not ("." in site_word and " " not in site_word.strip()) \
                    and site_word.lower() not in _NOOP_SITE_WORDS:
                # «на маргарите» — не алиас и не домен: это скоп карточки,
                # а не сайт; возвращаем слово в цель скоупа
                goal = f"{goal} на {site_word}"
                site_word = None
        goal_n = " ".join(goal.lower().split())
        # «нажми i»: однобуквенное имя иконки не переживает фильтр слов
        # (len>=3) — подменяем на полное («информация»)
        if goal_n in _GOAL_ALIAS:
            goal = _GOAL_ALIAS[goal_n]
            goal_n = " ".join(goal.lower().split())
        close_m = _CLOSE_VERB_RE.match(goal_n)
        close_goal = bool(close_m) or bool(_CLOSE_GOAL_RE.search(goal_n))
        close_obj = close_m.group(1).strip() if close_m else ""
        if close_goal:
            # «закрытие модального окна» → «закрыть»: ищем крестик, а не текст.
            # Авто-закрытие оверлея перед снапшотом тут ВЫКЛЮЧАЕМ: оно съело
            # бы крестик раньше явной команды, и «закрой окно» ответило бы
            # «не нашёл» на уже закрытом попапе
            goal = "закрыть"
        if close_goal and close_obj and not _CLOSE_GENERIC_RE.fullmatch(close_obj):
            # Целевое закрытие («закрой соусы к бортикам»): крестик в контексте
            # названного блока — скоуп-форма «закрыть на X» (крестик модалки
            # соусов, а не «×» у товара в корзине). Промах — ниже обычный
            # крестик общим проходом
            r = self._resolve_element(f"закрыть на {close_obj}", site_word,
                                      router, chat_id=chat_id,
                                      auto_dismiss=False)
            if r[6] is None:
                url, host, items, idx, tab_id, meta, _ = r
                item = self._element_by_idx(items, idx) or {}
                text = str(item.get("text") or "")
                logger.info(f"[CompControl] Целевое закрытие "
                            f"«{close_obj[:40]}» → [{idx}] {text[:40]} "
                            f"на {host} (путь: {meta.get('path')})")
                act = {"kind": "click", "idx": idx,
                       "element": text or f"#{idx}", "host": host,
                       "value": url, "choose": meta,
                       "goal": f"закрыть на {close_obj}"}
                if meta.get("via") == "goal_snapshot":
                    act["gidx"] = True
                if tab_id is not None:
                    act["tab_id"] = tab_id
                return act, None
        url, host, items, idx, tab_id, meta, err = self._resolve_element(
            goal, site_word, router, chat_id=chat_id,
            auto_dismiss=not close_goal)
        if err:
            if close_goal or "крест" in goal_n:
                # «Свернуть» — это ИМЯ кнопки, а не только глагол закрытия:
                # панель очереди YouTube («Джем») сворачивается кнопкой
                # «Свернуть», крестика в ней нет. Пробуем свернуть-глагол
                # как клик (голый — кнопка; с объектом — в его контексте)
                # ДО Escape-фолбэка. Только для свернуть-форм: «закрой»
                # кнопкой не бывает
                verb = close_m.group(0) if close_m else ""
                collapse_try = ""
                if verb.startswith("сверн"):
                    collapse_try = "свернуть"
                    if close_obj and not _CLOSE_GENERIC_RE.fullmatch(close_obj):
                        collapse_try = f"свернуть на {close_obj}"
                elif close_obj and not _CLOSE_GENERIC_RE.fullmatch(close_obj):
                    # «закрой джем»: крестика в панели нет, но её штатное
                    # закрытие — кнопка «Свернуть» в контексте объекта
                    collapse_try = f"свернуть на {close_obj}"
                if collapse_try:
                    r = self._resolve_element(
                        collapse_try, site_word, router, chat_id=chat_id,
                        auto_dismiss=False)
                    if r[6] is None:
                        url2, host2, items2, idx2, tab_id2, meta2, _ = r
                        it = self._element_by_idx(items2, idx2) or {}
                        txt = str(it.get("text") or "")
                        logger.info(
                            f"[CompControl] Закрытие через «{collapse_try[:30]}» "
                            f"→ [{idx2}] {txt[:40]} на {host2}")
                        act3 = {"kind": "click", "idx": idx2,
                                "element": txt or f"#{idx2}", "host": host2,
                                "value": url2, "choose": meta2,
                                "goal": collapse_try}
                        if meta2.get("via") == "goal_snapshot":
                            act3["gidx"] = True
                        if tab_id2 is not None:
                            act3["tab_id"] = tab_id2
                        return act3, None
                # Крестика нет в снапшоте (модалки без close-контрола —
                # анкета dodo, шторки подтверждений, открытые меню YouTube
                # без Х внутри): если диалог/меню реально виден, закрываем
                # клавишей Escape — «нажми крестик» при открытом меню это
                # и значит
                act2 = self._escape_fallback(site_word, chat_id)
                if act2 is not None:
                    return act2, None
            return None, err
        item = self._element_by_idx(items, idx) or {}
        text = str(item.get("text") or "")
        logger.info(f"[CompControl] Клик «{goal[:40]}» → [{idx}] {text[:40]} "
                    f"на {host} (путь: {meta.get('path')})")
        act = {"kind": "click", "idx": idx, "element": text or f"#{idx}",
               "host": host, "value": url, "choose": meta, "goal": goal}
        # Метки целевого снапшота живут в data-vpc-gidx (общий снапшот
        # они не затирают) — клик должен искать по ним
        if meta.get("via") == "goal_snapshot":
            act["gidx"] = True
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act, None

    def resolve_download(self, goal: str, site_word: Optional[str],
                         router, chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """«скачай „методичку по sql" (на ciu.nstu.ru)» → (download-действие,
        None) или (None, причина). Тот же снапшот/скоринг, что у клика; у
        найденного элемента берём href — без него (иконка меню, кнопка)
        честный отказ: скачивать нечего."""
        url, host, items, idx, tab_id, meta, err = self._resolve_element(
            goal, site_word, router, chat_id=chat_id)
        if err:
            return None, err
        item = self._element_by_idx(items, idx) or {}
        text = str(item.get("text") or "")
        from app.features.browser_actions import href_of_tagged
        href = href_of_tagged(host, idx, tab_id=tab_id)
        if not href.startswith(("http://", "https://")):
            logger.info(f"[CompControl] Скачивание «{goal[:40]}»: у [{idx}] нет href")
            return None, (f"Элемент «{text or goal}» — не ссылка на файл, "
                          "скачивать нечего.")
        logger.info(f"[CompControl] Скачивание «{goal[:40]}» → [{idx}] "
                    f"{text[:40]} ({href[:60]})")
        act = {"kind": "download", "url": href,
               "element": text or f"#{idx}", "host": host, "choose": meta}
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act, None

    def resolve_read(self, mode: str, site_word: Optional[str],
                     chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """«прочитай последнее сообщение (на кладе)» → (read-действие, None)
        или (None, причина). Вкладка — та же адресация, что у клика
        (_snapshot_for: алиас/домен/PAGE_REF/отслеживаемая/последняя)."""
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        act = {"kind": "read", "mode": mode, "host": host, "value": url}
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act, None

    def read_page_section(self, query: str, site_word: Optional[str],
                          full_query: Optional[str] = None
                          ) -> Optional[Tuple[str, str, str]]:
        """Вопрос «что находится в X?» — живой текст секции X открытой
        страницы. → (текст секции, host, использованный запрос) или None:
        страницу не открывали, секция не нашлась — тогда вопрос молча уходит
        в обычный LLM-диалог (он мог быть вообще не о странице — ошибок
        пользователю не показываем). Хвост-сайт, который не алиас и не домен
        («на двоих» в «завтрак на двоих»), — часть названия: ищем полный
        запрос. Та же адресация вкладки, что у клика. Чтение без побочек."""
        # Страницу не трогали и сайт не назван — нечего читать, не тратим вызов
        if not site_word and self._last_tab_id is None and not self._last_host:
            return None
        host_part = None
        tab_id = None
        if site_word == PAGE_REF:
            tab_id = self._last_tab_id
        elif site_word:
            k = self._lookup(self.sites, " ".join(site_word.lower().split()))
            if k:
                host_part = urlparse(self.sites[k]).hostname
            elif "." in site_word and " " not in site_word.strip():
                host_part = site_word.strip().lower()
            else:
                # Не алиас и не домен — это часть названия секции
                # («завтрак на двоих»), а не сайт: ищем по полному запросу
                query = full_query or query
        if host_part is None and tab_id is None:
            tab_id = self._last_tab_id
            if tab_id is None:
                host_part = self._last_host
        from app.features.browser_actions import read_section
        try:
            text = read_section(host_part, query, tab_id=tab_id)
        except Exception as e:
            # Отслеживаемая вкладка могла умереть — пробуем последний хост
            logger.debug(f"[CompControl] чтение секции не удалось: {e}")
            if tab_id is None or not self._last_host:
                return None
            self._last_tab_id = None
            try:
                text = read_section(self._last_host, query)
                host_part = self._last_host
            except Exception as e2:
                logger.debug(f"[CompControl] чтение секции не удалось: {e2}")
                return None
        text = (text or "").strip()
        if len(text) < 3:
            return None
        return text, (host_part or self._last_host or ""), query

    def resolve_send(self, _goal, site_word: Optional[str],
                     router=None, chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """«отправь» — Enter в поле ввода вкладки (та же адресация, что у
        клика). Поле выберет JS при исполнении (непустое/фокусное/единственное).
        _goal не используется — сигнатура общая с резолверами клика/ввода."""
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        act = {"kind": "send", "host": host, "value": url}
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act, None

    def resolve_key(self, goal, site_word: Optional[str],
                    router=None, chat_id: str = ""
                    ) -> Tuple[Optional[dict], Optional[str]]:
        """«нажми пробел/энтер/эскейп» — клавиша в страницу: адресация вкладки
        как у клика (алиас/домен/«на этой странице»/последняя), но БЕЗ выбора
        элемента — клавиша летит в активный фокус или документ. goal — имя
        клавиши playwright (Space/Enter/…) или кортеж (клавиша, нажатий,
        вид) от медиа-команд («пауза», «тише»): вид украшает текст ответа,
        нажатий >1 — громкость стрелками."""
        if isinstance(goal, tuple):
            key, times, mkind = goal
        else:
            key, times, mkind = goal, 1, None
        url, host, _items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        if mkind == "toggle" and "youtube" in (host or ""):
            # На YouTube пробел капризен (первое нажатие играет, повтор не
            # ставит на паузу) — штатный шорткат k переключает стабильно
            key = "k"
        act = {"kind": "key", "host": host, "value": url, "key": key,
               "times": int(times or 1)}
        if mkind:
            act["media"] = mkind
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act, None

    def resolve_slider(self, goal, site_word: Optional[str],
                       router=None, chat_id: str = ""
                       ) -> Tuple[Optional[dict], Optional[str]]:
        """«перетащи слайдер рабочие часы на 8»: goal=(подпись, значение).
        Адресация вкладки — как у клика; сам ползунок (input[type=range]/
        role=slider) и кламп значения — JS при исполнении."""
        label, value = goal
        url, host, _items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        act = {"kind": "slider", "host": host, "value": url,
               "slider_label": label, "slider_value": int(value),
               "element": label or "слайдер"}
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act, None

    def _escape_fallback(self, site_word: Optional[str],
                         chat_id: str = "") -> Optional[dict]:
        """«закрой окно», а крестика в снапшоте нет (модалки без close-контрола,
        открытый выпадающий список): если на странице виден диалог/оверлей
        или раскрытый список — действие «нажать Escape». None — ничего
        такого не видно (вернём исходную ошибку резолва)."""
        try:
            # auto_dismiss=False — авто-закрытие оверлея съело бы модалку
            # раньше Escape (и вернуло бы «не нашёл» на уже закрытом окне)
            url, host, _items, tab_id, err = self._snapshot_for(
                site_word, chat_id=chat_id, auto_dismiss=False)
        except Exception:
            return None
        if err:
            return None
        from app.features import browser_actions as ba
        try:
            if not (ba.modal_visible(host, tab_id=tab_id)
                    or ba.open_list_visible(host, tab_id=tab_id)):
                return None
        except Exception:
            return None
        logger.info(f"[CompControl] Крестик не нашёлся, модалка/список "
                    f"видны — Escape на {host}")
        act = {"kind": "press", "element": "Escape", "host": host,
               "value": url, "goal": "закрыть"}
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act

    # ── Авто-листание «промотай страницу» / «стоп» ─────────

    def _scroll_active(self) -> bool:
        """Идёт ли фоновая прокрутка прямо сейчас (поток жив и не остановлен)."""
        with self._scroll_lock:
            s = self._scroll
        return bool(s and s["thread"].is_alive() and not s["stop"].is_set())

    def _scroll_start(self, action: dict):
        """Авто-листание вкладки анимацией внутри самой страницы (rAF,
        ступенями ~56px — репейнт на каждом кадре ронял fps системы):
        запуск — один вызов, дальше страница крутится сама, а этот поток —
        дозорный: конец ленты (легли на дно и оно не подросло) или смерть/
        навигация вкладки завершают сеанс сами. Запуск синхронно: вкладка
        мертва или страница уже внизу — честная ошибка, а не «листаю» без
        движения. Один сеанс на бота: повторный старт при живом отсекается
        резолвером."""
        from app.features import browser_actions as ba
        host, tab_id = action.get("host"), action.get("tab_id")
        side = action.get("side")
        direction = action.get("dir")
        res = ba.scroll_start(host, tab_id=tab_id, side=side,
                              direction=direction)
        if res.get("side_missed"):
            raise ba.BrowserUnavailable(
                "не вижу прокручиваемого раздела "
                + ("слева" if side == "left" else "справа")
                + " на странице")
        if not res.get("ok") or res.get("bottom"):
            raise ba.BrowserUnavailable(
                "страница уже в самом верху — листать некуда"
                if direction == "up" else
                "страница уже в самом низу — листать некуда")
        stop_evt = threading.Event()
        box: Dict[str, str] = {}

        def _loop():
            while not stop_evt.wait(_SCROLL_POLL_SEC):
                try:
                    st = ba.scroll_status(host, tab_id=tab_id)
                except Exception as e:
                    box["end"] = "lost"  # вкладку закрыли/браузер ушёл
                    logger.info(f"[CompControl] Листание прервано: {e}")
                    return
                if st.get("done"):
                    box["end"] = "bottom"
                    return
                if not st.get("active"):
                    # Страница ушла навигацией/перезагрузкой — анимации нет
                    box["end"] = "lost"
                    return

        t = threading.Thread(target=_loop, daemon=True, name="vpc-scroll")
        with self._scroll_lock:
            self._scroll = {"thread": t, "stop": stop_evt, "box": box,
                            "host": host, "tab_id": tab_id}
        t.start()
        logger.info(f"[CompControl] Начал листать страницу: {host}")

    def _scroll_stop_now(self) -> Optional[str]:
        """Остановить листание: сначала гасим анимацию в самой странице
        (один вызов — страница замирает сразу, на месте «стоп»), затем
        дозорный поток. → причина самостоятельного завершения ('bottom' —
        долистал до конца, 'lost' — вкладка умерла/ушла) или None, если
        остановлен пользователем."""
        with self._scroll_lock:
            s, self._scroll = self._scroll, None
        if not s:
            return None
        from app.features import browser_actions as ba
        ba.scroll_stop(s.get("host"), tab_id=s.get("tab_id"))
        s["stop"].set()
        s["thread"].join(timeout=3)
        return s["box"].get("end")

    def resolve_scroll(self, mode, site_word: Optional[str],
                       router=None, chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """«промотай страницу (на ютубе)» → (scroll-действие, None);
        «промотай раздел слева» — mode приезжает кортежем ("start", "left"):
        листается внутренняя панель, а не окно.
        «стоп»/«хватит листать» → (scroll_stop, None). «стоп» без сеанса
        листания — (None, None): бытовое слово уходит в обычный диалог."""
        side = None
        direction = None
        if isinstance(mode, tuple):
            if len(mode) == 3:
                mode, side, direction = mode
            else:
                mode, side = mode
        if mode == "stop":
            with self._scroll_lock:
                has = self._scroll is not None
            if not has:
                return None, None
            return {"kind": "scroll_stop"}, None
        if self._scroll_active():
            return None, ("Я уже листаю страницу — скажи «стоп», "
                          "и остановлюсь.")
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        p = urlparse(url)
        if p.hostname in ("localhost", "127.0.0.1") and p.port in (5173, 8000):
            return None, ("Сейчас активна вкладка чата — листать там нечего. "
                          "Назови сайт («промотай страницу на ютубе») или "
                          "переключись на неё.")
        act = {"kind": "scroll", "host": host, "value": url}
        if side:
            act["side"] = side
        if direction:
            act["dir"] = direction
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act, None

    # ── Вкладки: «перейди на вкладку X», «какие вкладки открыты» ──

    def list_open_tabs(self) -> List[dict]:
        """Живые вкладки браузера (кроме служебных и вкладки чата); свежий
        список кэшируется в _known_tabs — «хранимый» список для подсказок
        и аудита. Ошибки браузера пробрасываются вызывающему."""
        from app.features import browser_actions as ba
        tabs = ba.list_tabs()
        self._known_tabs = [{"tab_id": tid, "url": url, "host": host,
                             "title": title}
                            for tid, url, host, title in tabs]
        return list(self._known_tabs)

    def list_open_tabs_text(self) -> str:
        """«какие вкладки открыты» → человеческий список (вопрос-чтение,
        без действия и подтверждения)."""
        try:
            tabs = self.list_open_tabs()
        except Exception as e:
            return f"Не вижу браузер: {e}"
        if not tabs:
            return "В браузере бота нет открытых вкладок."
        parts = [f"«{(t['title'] or t['host'])[:40]}» ({t['host']})"
                 for t in tabs[:10]]
        tail = "" if len(tabs) <= 10 else f" и ещё {len(tabs) - 10}"
        return "Открытые вкладки: " + ", ".join(parts) + tail + "."

    def resolve_tab_switch(self, goal: str, explicit: bool = True,
                           chat_id: str = ""
                           ) -> Tuple[Optional[dict], Optional[str]]:
        """«перейди на вкладку ютуб» → (tab_switch-действие, None) —
        переключение ничего не меняет, поэтому без подтверждения. Матч по
        живым вкладкам: алиас сайта → хост, далее точное совпадение
        хоста/заголовка > подстрока в хосте > подстрока в заголовке > основы
        слов; явный лидер (отрыв ≥10) — переключаем. Несколько подходящих —
        честный перечень. Ни одной: мягкая форма («перейди на X» без слова
        «вкладку») — фолбэк на открытие сайта из алиасов/истории (без
        поискового резолва — гадать адрес на мягкую фразу не берёмся), либо
        (None, None) в обычный диалог; явная — отказ со списком открытых."""
        try:
            tabs = self.list_open_tabs()
        except Exception as e:
            return None, f"Не вижу браузер: {e}"
        g = " ".join(goal.lower().split())
        # Алиас («ютуб») → хост сайта: вкладки матчатся по латинскому хосту
        alias_host = ""
        k = self._lookup(self.sites, g)
        if k:
            alias_host = (urlparse(self.sites[k]).hostname or "") \
                .lower().removeprefix("www.")
        from app.features.web_search import _stem
        gw = [w for w in re.findall(r"[a-z0-9а-яё]+", g) if len(w) >= 3]
        scored: List[Tuple[float, dict]] = []
        for t in tabs:
            h = t["host"].removeprefix("www.")
            title = " ".join(t["title"].lower().split())
            if alias_host and (h == alias_host
                               or h.endswith("." + alias_host)
                               or alias_host.endswith("." + h)):
                s = 100.0
            elif g and (g == h or g == title):
                s = 95.0
            elif g and g in h:
                s = 80.0
            elif g and g in title:
                s = 70.0
            elif gw and all(_word_in(w, f"{h} {title}")
                            or _word_in(_stem(w), f"{h} {title}") for w in gw):
                s = 60.0
            else:
                continue
            scored.append((s, t))
        scored.sort(key=lambda x: -x[0])
        if scored and (len(scored) == 1
                       or scored[0][0] - scored[1][0] >= 10.0):
            t = scored[0][1]
            label = t["title"] or t["host"]
            logger.info(f"[CompControl] Переключение на вкладку "
                        f"#{t['tab_id']} «{label[:40]}» ({t['host']})")
            return {"kind": "tab_switch", "tab_id": t["tab_id"],
                    "value": t["url"], "host": t["host"],
                    "element": label[:80]}, None
        names = ", ".join(f"«{(t['title'] or t['host'])[:30]}»"
                          for t in tabs[:5]) or "—"
        if len(scored) > 1:
            cands = ", ".join(f"«{(t['title'] or t['host'])[:30]}»"
                              for _s, t in scored[:4])
            return None, (f"Под «{goal}» подходят несколько вкладок: "
                          f"{cands}. Уточни, какую.")
        if not explicit:
            # Мягкая форма и вкладки нет — возможно, имелось в виду «открой»:
            # алиас/история (поисковый резолв на мягкую фразу не гоняем)
            alt = self.resolve(goal)
            if alt and not alt.get("expect_name"):
                return alt, None
            return None, None  # не наша команда — пусть разбирает диалог
        return None, (f"Вкладка «{goal}» не найдена. Открыты: {names}. "
                      "Скажи «открой …», если нужна новая.")

    # ── Корзина сайта: «убери X из корзины», «убавь/прибавь X» ──

    def resolve_cart(self, parsed, site_word: Optional[str],
                     router=None, chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """(op, product) из parse_cart_request → (cart-действие, None) или
        (None, причина). Вкладка — та же адресация, что у клика; клик по
        контролу карточки и проверка эффекта — при исполнении (cart_op)."""
        op, product = parsed
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        p = urlparse(url)
        if p.hostname in ("localhost", "127.0.0.1") and p.port in (5173, 8000):
            return None, ("Сейчас активна вкладка чата — корзины там нет. "
                          "Переключись на страницу магазина.")
        act = {"kind": "cart", "op": op, "product": product, "host": host}
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act, None

    # ── Ввод текста «введи X в поле Y» ─────────────────────

    @staticmethod
    def _type_fields_hint(msg: str, inputs: List[dict]) -> str:
        labels = [str(it.get("text") or "")[:30] for it in inputs[:5]]
        labels = [l for l in labels if l]
        return msg + (f" Вижу поля: {', '.join(f'«{l}»' for l in labels)}."
                      if labels else "")

    def _hidden_fields_note(self, host: str, tab_id: Optional[int],
                            goal: str) -> Optional[str]:
        """Подсказка «поле есть, но скрыто»: снапшот отбрасывает невидимые
        поля (свёрнутое меню, закрытый попап), и без проверки бот честно
        отвечал бы «нет поля» при живом поле поиска за кнопкой-лупой.
        Совпадение — по основам слов (как в скоринге) либо по флагу
        поисковости для цели «поиск» (плейсхолдер скрытого поля может
        слова «поиск» не содержать). None — скрытых полей нет или ни одно
        не подходит под цель."""
        from app.features import browser_actions as ba
        from app.features.web_search import _stem
        hidden = ba.hidden_editable_labels(host, tab_id=tab_id)
        if not hidden:
            return None
        g_words = [w for w in re.findall(r"[a-z0-9а-яё]+", goal.lower())
                   if len(w) >= 3]
        want_search = any(_stem(w) == "поиск" for w in g_words)
        for item in hidden:
            label = str(item.get("t") or "")
            hay = label.lower()
            if (g_words and all(w in hay or _stem(w) in hay for w in g_words)) \
                    or (want_search and item.get("q")):
                return (f"Поле «{label[:40]}» на странице {host} есть, "
                        "но сейчас скрыто (свёрнутое меню или закрытый "
                        "попап). Открой его и повтори — тогда введу.")
        return None

    @staticmethod
    def _home_city() -> Optional[str]:
        """Город пользователя из местоположения (env_location.json).
        «Новосибирск, Россия» → «Новосибирск». None — местоположение выключено."""
        try:
            from app.features import env_context
            city = str(env_context.load_location().get("city") or "")
            return city.split(",")[0].strip() or None
        except Exception:
            return None

    @staticmethod
    def _match_field_prefix(rem: str, inputs: List[dict]):
        """«ПОДПИСЬ ПОЛЯ + текст» → (поле, текст): подпись — префикс фразы
        (по основам слов); из подходящих берём самую длинную подпись.
        Спаны токенов сохраняем, чтобы текст шёл в исходном регистре."""
        toks = [(m.span(), m.group(0))
                for m in re.finditer(r"[a-z0-9а-яё]+", rem, re.IGNORECASE)]
        if not toks:
            return None, None
        from app.features.web_search import _stem
        stems = [_stem(w.lower()) for _, w in toks]
        best = None  # (длина подписи в словах, поле)
        for it in inputs:
            label = str(it.get("text") or it.get("aria") or it.get("title") or "")
            lw = [w.lower() for w in re.findall(r"[a-z0-9а-яё]+", label,
                                                re.IGNORECASE)]
            if not lw or len(lw) > len(toks):
                continue
            if [_stem(w) for w in lw] == stems[:len(lw)]:
                if best is None or len(lw) > best[0]:
                    best = (len(lw), it)
        if best is None:
            return None, None
        k, it = best
        text = rem[toks[k][0][0]:].strip() if k < len(toks) else ""
        text = _TYPE_PREP_EDGE_RE.sub("", text).strip()
        return it, text or None

    @staticmethod
    def _match_field_anywhere(body: str, inputs: List[dict]):
        """Подпись поля — непрерывная цепочка слов внутри фразы (без предлогов):
        снимаем её, остальное — текст. Берём самую длинную подпись."""
        toks = [(m.span(), m.group(0))
                for m in re.finditer(r"[a-z0-9а-яё]+", body, re.IGNORECASE)]
        if len(toks) < 2:
            return None, None
        from app.features.web_search import _stem
        stems = [_stem(w.lower()) for _, w in toks]
        best = None  # (длина подписи, начало в токенах, поле)
        for it in inputs:
            label = str(it.get("text") or it.get("aria") or it.get("title") or "")
            lw = [w.lower() for w in re.findall(r"[a-z0-9а-яё]+", label,
                                                re.IGNORECASE)]
            if not lw or len(lw) >= len(toks):
                continue
            ls = [_stem(w) for w in lw]
            for j in range(len(stems) - len(ls) + 1):
                if stems[j:j + len(ls)] == ls and (best is None or len(ls) > best[0]):
                    best = (len(ls), j, it)
        if best is None:
            return None, None
        k, j, it = best
        tail = body[toks[j + k][0][1]:] if j + k < len(toks) else ""
        text = (body[:toks[j][0][0]] + " " + tail).strip()
        text = _TYPE_PREP_EDGE_RE.sub("", text).strip()
        return it, text or None

    def resolve_type(self, body: str, site_word: Optional[str],
                     router, chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """«введи …» → (type-действие, None) | (None, честная причина) |
        (None, None) — «не наша команда», и то только ДО снапшота (просьба
        сгенерировать текст — «напиши мне письмо», «эссе в стиле классиков»).
        Команде, безусловно адресованной странице (сепаратор «в поле», сайт,
        гео-плейсхолдер, одно слово-значение), неудача возвращает честную
        причину — в LLM-поток её пускать нельзя: модель «изобразит» ввод.
        Поле и текст разделяются по снапшоту: грамматика «ТЕКСТ в поле ПОЛЕ»
        либо префиксный матч подписи поля «в поле ПОЛЕ ТЕКСТ». LLM-путь ввод
        не получает никогда — по той же причине, что и клик: «сыграть»
        выполнение («Введено») он может."""
        body = str(body or "").strip()
        if not body:
            return None, None
        # Хвост «…и отправь»: после ввода жмём Enter в том же поле
        submit = False
        sub_m = _TYPE_SUBMIT_RE.search(body)
        if sub_m:
            submit = True
            body = body[:sub_m.start()].strip()
            if not body:
                return None, None
        # Сайт срезаем с конца только если он РАЗРЕШАЕТСЯ (алиас/домен):
        # «напиши привет в чат» — «чат» скорее поле, чем сайт
        if site_word is None:
            b, is_page = _strip_page_ref(body)
            if is_page:
                site_word, body = PAGE_REF, b
            else:
                sm = _CLICK_SITE_RE.search(body)
                if sm:
                    cand = sm.group(1).strip().lower().rstrip(".!?…")
                    if self._lookup(self.sites, cand) is not None or "." in cand:
                        site_word = cand
                        body = body[:sm.start()].strip()
        has_sep = bool(_TYPE_FIELD_SEP_RE.search(body)
                       or _TYPE_FIELD_END_RE.search(body))
        has_head = bool(_TYPE_FIELD_HEAD_RE.match(body))
        # Голое «в/во» — повод попробовать матч подписи поля по снапшоту
        # («привет в чат с оператором»); не совпадёт — вернём «не наше»
        has_in = bool(re.search(r"\s+(?:в|во|in)\s+", body, re.IGNORECASE))
        # «мой город» целиком — явная команда ввода гео-плейсхолдера
        geo_body = bool(_GEO_TEXT_RE.fullmatch(body))
        # Снимаемся снапшотом только при явных признаках ввода в страницу —
        # иначе это почти наверняка просьба написать текст, не наше
        if not (has_sep or has_head or has_in or geo_body
                or site_word is not None or len(body.split()) == 1):
            return None, None
        # Безусловно «наша» команда (сепаратор «в поле», сайт, гео, «в поиск»,
        # одно слово-значение): неудаче — честная причина, а не (None, None),
        # иначе LLM «изобразит» ввод («Успешно введено» ни в какое поле).
        # Голое «в/во» явным не считаем: «напиши эссе в стиле классиков» —
        # генерация, ей нужен LLM-поток
        explicit = bool(has_sep or has_head or site_word is not None
                        or geo_body or len(body.split()) == 1
                        or _TYPE_SEARCH_SEP_RE.search(body))
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            if explicit:
                return None, err
            return None, None  # «в стиле …», но страницы нет — не наше
        p = urlparse(url)
        if p.hostname in ("localhost", "127.0.0.1") and p.port in (5173, 8000):
            return None, ("Сейчас активна вкладка чата — туда вводить нечего. "
                          "Назови сайт («введи X в поле Y на ютубе») или "
                          "переключись на страницу.")
        inputs = [it for it in items if it.get("ed")]
        if not inputs:
            if explicit:
                hidden = self._hidden_fields_note(host, tab_id, body)
                if hidden:
                    return None, hidden
                self._audit_resolve(chat_id, body, host,
                                    f"На странице {host} нет полей ввода.",
                                    "no_fields")
                return None, f"На странице {host} нет полей ввода."
            return None, None
        field_goal: Optional[str] = None
        text: Optional[str] = None
        item: Optional[dict] = None
        meta: Dict[str, object] = {"path": "match", "candidates": [],
                                   "llm_response": None}
        seps = [m for m in _TYPE_FIELD_SEP_RE.finditer(body)
                if body[:m.start()].strip()]
        if seps:
            # «ТЕКСТ в поле ПОЛЕ» — крайний сепаратор: сам текст может тоже
            # содержать «в поле»
            text = body[:seps[-1].start()].strip()
            field_goal = body[seps[-1].end():].strip() or None
        elif _TYPE_FIELD_END_RE.search(body):
            # «привет в поле» — сепаратор на краю, названия поля нет
            return None, self._type_fields_hint(
                "Не понял, в какое поле ввести. "
                "Скажи так: «введи ТЕКСТ в поле НАЗВАНИЕ».", inputs)
        elif _TYPE_SEARCH_SEP_RE.search(body):
            # «X в поиск»: «поиск» — само название поля (search-инпут)
            m = _TYPE_SEARCH_SEP_RE.search(body)
            text = body[:m.start()].strip()
            field_goal = "поиск"
        if field_goal is None and not seps:
            rem = _TYPE_FIELD_HEAD_RE.sub("", body, count=1).strip() \
                if has_head else body
            item, text = self._match_field_prefix(rem, inputs)
            if item is None and not has_head:
                item, text = self._match_field_anywhere(body, inputs)
            if item is None and not has_head \
                    and (len(body.split()) == 1 or geo_body) and len(inputs) == 1:
                # «введи новосибирск» / «введи мой город» + единственное поле
                item, text = inputs[0], body
            if item is None:
                if explicit:
                    return None, self._type_fields_hint(
                        f"Не разобрал, что и куда ввести из «{body[:60]}». "
                        "Скажи так: «введи ТЕКСТ в поле НАЗВАНИЕ».", inputs)
                return None, None
            if not text:
                return None, (f"Не понял, какой текст ввести в "
                              f"«{str(item.get('text') or '')[:40]}» — "
                              "добавь текст после названия поля.")
        if field_goal is None and item is None:
            # «текст в поле» — сепаратор есть, а названия поля после него нет
            return None, self._type_fields_hint(
                "Не понял, в какое поле ввести. "
                "Скажи так: «введи ТЕКСТ в поле НАЗВАНИЕ».", inputs)
        if field_goal is not None:
            idx, meta = self._choose_element(field_goal, inputs, router)
            if idx is None:
                # «введи X в поиск»: подпись поля может не содержать слова
                # «поиск» («Искать в Википедии») — единственное видимое
                # поисковое поле (флаг q снапшота) берём без LLM
                from app.features.web_search import _stem
                gw = [w for w in re.findall(r"[a-z0-9а-яё]+", field_goal.lower())
                      if len(w) >= 3]
                if any(w == "search" or _stem(w) == "поиск" for w in gw):
                    qf = [it for it in inputs if it.get("q")]
                    if len(qf) == 1:
                        idx = int(qf[0]["idx"])
                        meta = {"path": "search_field",
                                "candidates": [{
                                    "idx": idx,
                                    "text": str(qf[0].get("text") or "")[:60],
                                    "score": 0.0}],
                                "llm_response": None}
            if idx is None:
                # «почта» при подписи «Электронная почта»: скоринг не совпал —
                # широкий LLM-резолв по списку полей (полей мало, кейс для
                # LLM идеальный); пользователю не нужно знать точное имя поля
                widx, wmeta = self._llm_wide_pick(field_goal, inputs, router,
                                                  for_field=True)
                if widx is not None:
                    idx, meta = widx, wmeta
            if idx is None:
                hidden = self._hidden_fields_note(host, tab_id, field_goal)
                if hidden:
                    return None, hidden
                msg = self._type_fields_hint(
                    f"На странице {host} не нашёл поля «{field_goal}».", inputs)
                self._audit_resolve(chat_id, field_goal, host, msg,
                                    self._resolve_fail_kind(meta), meta=meta)
                return None, msg
            item = self._element_by_idx(inputs, idx)
        text = (text or "").strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'«»":
            text = text[1:-1].strip()
        if _GEO_TEXT_RE.fullmatch(text.lower()):
            # «мой город» — город из местоположения пользователя
            city = self._home_city()
            if city is None:
                return None, ("Не знаю твой город: местоположение выключено "
                              "(досье → «Настройки» → местоположение). "
                              "Назови город текстом.")
            text = city
        if not text or len(text) > 200:
            return None, "Не понял, какой текст ввести."
        label = str(item.get("text") or f"#{item['idx']}")
        logger.info(f"[CompControl] Ввод «{label[:40]}» ← {len(text)} симв. "
                    f"на {host} (путь: {meta.get('path')})")
        act = {"kind": "type", "idx": int(item["idx"]), "text": text,
               "element": label, "host": host, "value": url, "choose": meta}
        if submit:
            act["submit"] = True
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act, None

    def _first_result_url(self, site_key: str, search_url: str) -> Optional[str]:
        """URL первого результата поиска на сайте (regex `first` из конфига).
        Одна загрузка страницы, ~1с. None при любой неудаче (сеть, капча,
        смена вёрстки) — тогда открывается сама страница поиска."""
        pattern = self.search_first.get(site_key)
        if not pattern:
            return None
        try:
            from urllib.parse import urljoin
            import httpx
            with httpx.Client(follow_redirects=True, timeout=8, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "ru,en;q=0.9",
            }) as client:
                html = client.get(search_url).text
            m = re.search(pattern, html)
            if not m:
                logger.info(f"[CompControl] Первый результат ({site_key}): regex не сматчился")
                return None
            direct = urljoin(search_url, m.group(1) if m.groups() else m.group(0))
            logger.info(f"[CompControl] Первый результат ({site_key}): {direct[:80]}")
            return direct
        except Exception as e:
            logger.info(f"[CompControl] Извлечение первого результата ({site_key}) "
                        f"не удалось: {e}")
            return None

    def execute(self, action: dict, chat_id: str = "",
                router=None) -> Tuple[bool, str]:
        """Исполняет действие из allowlist'а. Возвращает (ok, detail).
        router — для LLM-выбора элемента в многошаговой навигации (п.4/п.5)."""
        error_class = None
        try:
            self._dispatch(action, router=router)
            ok = True
            # read: detail — это прочитанный текст, он и есть ответ пользователю
            detail = (str(action.pop("_result", "") or "")
                      if action["kind"] == "read" else "")
        except Exception as e:
            ok, detail = False, str(e)[:200]
            # Класс ошибки для аудита (п.7): «не уверен, что сработало» —
            # отдельный класс от «элемент не найден»/«браузер недоступен»
            error_class = getattr(e, "error_class", None) or "error"
        self.stats["executed" if ok else "failed"] += 1
        self._audit(chat_id, action, ok, detail, error_class=error_class)
        if ok:
            # Запоминаем хост последней открытой вкладки — цель клика по умолчанию
            if action["kind"] == "url":
                self._last_host = self._host(action)
            elif action["kind"] == "nav":
                # путь после переходов меняется — хост устойчивее
                self._last_host = urlparse(action["value"]).hostname
            elif action["kind"] == "multi":
                for a in action["items"]:
                    if a["kind"] == "url":
                        self._last_host = self._host(a)
            if action["kind"] in ("url", "nav", "multi") and self._last_host:
                self._save_last_page(action.get("value"))
            logger.info(f"[CompControl] Выполнено: {self.describe(action)}")
        else:
            logger.warning(f"[CompControl] Не удалось {self.describe(action)}: {detail}")
        return ok, detail

    def _dispatch(self, action: dict, router=None):
        """Системный вызов (в тестах подменяется)."""
        if action["kind"] == "multi":
            for a in action["items"]:
                self._dispatch(a, router=router)
            return
        if action["kind"] == "tab_switch":
            # Переключение активной вкладки: навигации нет, страница не
            # меняется (поэтому и без подтверждения). Вкладка становится
            # отслеживаемой — следующие «нажми X»/«введи …» целятся в неё
            from app.features import browser_actions as ba
            url, title = ba.activate_tab(int(action["tab_id"]))
            self._last_tab_id = int(action["tab_id"])
            if action.get("host"):
                self._last_host = action["host"]
            self._last_url = url
            if title and not action.get("element"):
                action["element"] = title[:80]
            return
        if action["kind"] == "click":
            from app.features import browser_actions as ba
            pre = ba.page_urls()
            # Метки целевого снапшота — в data-vpc-gidx (независимы от
            # data-vpc-idx общего); gidx ставит резолвер (via=goal_snapshot)
            mark = "data-vpc-gidx" if action.get("gidx") else "data-vpc-idx"
            try:
                ba.click_tagged(action.get("host"), int(action["idx"]),
                                tab_id=action.get("tab_id"), mark=mark)
            except Exception as e:
                from app.features.browser_actions import ClickUncertain
                retryable = isinstance(e, ClickUncertain) \
                    or "элемент потерян" in str(e)
                goal = action.get("goal")
                if not retryable or not goal:
                    raise
                # Между резолвом и кликом лежит подтверждение пользователя,
                # живые страницы (карусель баннеров dodo) за эти секунды
                # перерисовываются и метка протухает: свежий снапшот →
                # свежий выбор → ОДИН повторный клик (как у шагов nav)
                _, host2, items2 = ba.snapshot_elements(
                    action.get("host"), tab_id=action.get("tab_id"))
                idx2, _meta2 = self._choose_element(goal, items2, router)
                if idx2 is None:
                    raise RuntimeError(
                        "элемент потерян — страница изменилась, "
                        f"и «{str(goal)[:40]}» заново не нашёлся")
                ba.click_tagged(action.get("host"), int(idx2),
                                tab_id=action.get("tab_id"))
                action["idx"] = int(idx2)
                found = self._element_by_idx(items2, int(idx2))
                if found:
                    action["element"] = str(found.get("text") or "")[:80]
            pop = ba.follow_popup(pre)
            if pop is not None:
                # Клик открыл новое окно (вход в аккаунт Google и т.п.) —
                # следующие «введи …»/«нажми …» работают уже в нём
                tid, host, url = pop
                self._last_tab_id, self._last_host, self._last_url = tid, host, url
                self._save_last_page(url)
                logger.info(f"[CompControl] Отслеживаю попап: {host}")
            else:
                self._remember_tab(action)
            return
        if action["kind"] == "type":
            from app.features import browser_actions as ba
            ba.fill_tagged(action.get("host"), int(action["idx"]),
                           action["text"], tab_id=action.get("tab_id"),
                           submit=bool(action.get("submit")))
            self._remember_tab(action)
            return
        if action["kind"] == "read":
            # Чтение текста со страницы: результат уезжает в ответ через
            # action["_result"] (execute заберёт в detail)
            from app.features import browser_actions as ba
            action["_result"] = ba.read_text(
                action.get("host"), tab_id=action.get("tab_id"),
                mode=str(action.get("mode") or "last"))
            self._remember_tab(action)
            return
        if action["kind"] == "send":
            # «отправь» — Enter в поле ввода (цель выберет JS при исполнении)
            from app.features import browser_actions as ba
            ba.press_enter(action.get("host"), tab_id=action.get("tab_id"))
            self._remember_tab(action)
            return
        if action["kind"] == "press":
            # «закрой окно» без крестика — Escape по видимой модалке
            from app.features import browser_actions as ba
            ba.press_escape(action.get("host"), tab_id=action.get("tab_id"))
            self._remember_tab(action)
            return
        if action["kind"] == "key":
            # «нажми пробел/энтер/…» и медиа («пауза», «тише») — клавиша в
            # страницу без выбора элемента. Best effort без closed-loop
            # проверки: клавиша может не менять DOM (canvas-рендер в играх)
            from app.features import browser_actions as ba
            ba.press_key(action.get("host"), action["key"],
                         tab_id=action.get("tab_id"),
                         times=int(action.get("times") or 1))
            self._remember_tab(action)
            return
        if action["kind"] == "slider":
            # «перетащи слайдер X на N» — JS находит ползунок по подписи
            # и выставляет значение; фактическое — в отчёт ответа
            from app.features import browser_actions as ba
            action["slider_done"] = ba.set_slider(
                action.get("host"), action.get("slider_label") or "",
                int(action.get("slider_value") or 0),
                tab_id=action.get("tab_id"))
            self._remember_tab(action)
            return
        if action["kind"] == "scroll":
            # «промотай страницу» — фоновое листание до «стоп»
            self._scroll_start(action)
            self._remember_tab(action)
            return
        if action["kind"] == "scroll_stop":
            # «стоп» — глушим цикл; причина самозавершения — в отчёт ответа
            action["end_reason"] = self._scroll_stop_now()
            return
        if action["kind"] == "cart":
            # Операция с корзиной сайта: детерминированный клик по контролу
            # карточки товара + closed-loop проверка (новое количество — в
            # отчёт ответа через describe_done)
            from app.features import browser_actions as ba
            res = ba.cart_op(action.get("host"), action["product"],
                             action["op"], tab_id=action.get("tab_id"))
            if res.get("qty") is not None:
                action["qty_new"] = res["qty"]
            self._remember_tab(action)
            return
        if action["kind"] == "nav":
            self._navigate(action, router=router)
            return
        if action["kind"] == "download":
            from app.features import browser_actions as ba
            ba.download_in_tab(action.get("host"), action["url"],
                               tab_id=action.get("tab_id"))
            self._remember_tab(action)
            return
        kind, value = action["kind"], action["value"]
        if kind == "url":
            # Открываем отслеживаемой вкладкой (стабильный id): следующие
            # «на этой странице …»/«нажми …» целятся точно в неё. Бэкенд
            # (CDP на обеих ОС / AppleScript-фолбэк) выбирает browser_actions.
            # На не-macOS в auto-режиме без автоматизационного браузера —
            # системный браузер по умолчанию (простое открытие сайта работает
            # всегда); при явно выбранном бэкенде ошибку не маскируем
            from app.features import browser_actions as ba
            if sys.platform == "darwin" or ba.backend_forced():
                self._last_tab_id = ba.open_new_tab(value)
                self._verify_opened_site(action)
                return
            try:
                self._last_tab_id = ba.open_new_tab(value)
                self._verify_opened_site(action)
                return
            except ba.BrowserUnavailable:
                pass
            if not webbrowser.open(value):
                raise RuntimeError("webbrowser.open вернул False")
            return
        if kind == "app":
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-a", value])
            elif sys.platform == "win32":
                subprocess.Popen(["cmd", "/c", "start", "", value])
            else:
                subprocess.Popen(value, shell=True)  # noqa: S602 — строка из yaml автора персоны
            return
        # task: команда целиком из yaml персоны (доверенный автор), на macOS
        # сюда же ложится 'shortcuts run "…"'. Значения "recipe:<id>" — не
        # shell, а браузерные рецепты из реестра browser_actions (этап 3b)
        if value.startswith("recipe:"):
            from app.features.browser_actions import run_recipe
            run_recipe(value.removeprefix("recipe:").strip())
            return
        subprocess.Popen(value, shell=True)  # noqa: S602

    def _verify_opened_site(self, action: dict):
        """Мягкая верификация после навигации (п.5): если сайт резолвился
        поиском (expect_name), сверяем title/og:site_name открывшейся
        страницы с запрошенным именем. Несовпадение — НЕ отказ (страница уже
        открыта), а пометка name_check в аудите + предупреждение в лог."""
        name = str(action.get("expect_name") or "").strip()
        if not name or self._last_tab_id is None:
            return
        from app.features import browser_actions as ba
        from app.features.web_search import _stem, _google_translate
        ident = ""
        deadline = time.time() + 3.0
        while time.time() < deadline:
            try:
                ident = ba.page_identity(tab_id=self._last_tab_id) or ""
            except Exception:
                break  # бэкенд без eval (AppleScript-Chrome) — не проверяем
            if ident.strip(" |"):
                break
            time.sleep(0.5)  # title ещё не поднялся — страница грузится
        ok = None
        if ident.strip(" |"):
            hay = _norm_match(ident)
            words = [w for w in re.findall(r"[a-z0-9а-яё]+", _norm_match(name))
                     if len(w) >= 3]
            # Кириллическое имя против латинского title («ютуб» vs YouTube):
            # тот же перевод, что использует find_site_url при матче домена
            try:
                alt = _google_translate(name)
            except Exception:
                alt = None
            if alt:
                words += [w for w in re.findall(r"[a-z0-9а-яё]+",
                                                _norm_match(alt))
                          if len(w) >= 3]
            ok = bool(words) and any(
                _word_in(w, hay) or _word_in(_stem(w), hay) for w in words)
        action["name_check"] = {"expect": name[:40], "ok": ok,
                                "title": ident[:80]}
        if ok is False:
            logger.warning(f"[CompControl] Открытая страница не похожа на "
                           f"«{name}»: {ident[:60]}")

    def _remember_tab(self, action: dict):
        """Запомнить вкладку действия: следующие «на этой странице»/«нажми X»
        без сайта целятся в неё точно. Работает на обоих бэкендах (CDP-реестр
        / AppleScript-id). Best effort: не нашли id — остаётся host-таргетинг."""
        if action.get("tab_id") is not None:
            self._last_tab_id = action["tab_id"]
            return
        host = action.get("host")
        if not host:
            return
        try:
            from app.features import browser_actions as ba
            tid = ba.find_tab_id(host)
        except Exception:
            return
        if tid is not None:
            self._last_tab_id = tid

    def _navigate(self, action: dict, router=None):
        """Многошаговая навигация — детерминированная state-machine (п.5):
        структура шагов в коде, каждый шаг — снапшот → выбор элемента
        (скоринг → при неоднозначности LLM) → клик → проверка эффекта →
        следующий шаг; таймаут и честная ошибка, если застряли.
        Вкладка открывается отслеживаемой (стабильный id) — ни старые вкладки
        того же сайта, ни порядок окон навигации не мешают.
        Осечка — RuntimeError с честным текстом: что прошли и где встали."""
        from app.features import browser_actions as ba
        tab_id = None
        tab_host = ""
        if sys.platform == "darwin" or ba.backend_forced():
            tab_id = ba.open_new_tab(action["value"])
        else:
            try:
                tab_id = ba.open_new_tab(action["value"])
            except ba.BrowserUnavailable:
                # Нет автоматизационного браузера — открываем системным и
                # целимся по хосту + первому сегменту пути (одного хоста мало:
                # рядом может висеть старая вкладка-заглушка того же сайта)
                if not webbrowser.open(action["value"]):
                    raise RuntimeError("webbrowser.open вернул False")
                _p = urlparse(action["value"])
                _segs = [s for s in _p.path.split("/") if s]
                tab_host = (_p.hostname or action.get("host") or "") + (
                    "/" + _segs[0] if _segs else "")
        done: List[str] = []
        step_paths: List[str] = []
        for step in action["steps"]:
            # Ждём страницу с кликабельными элементами: первая загрузка и
            # переходы между страницами занимают секунды — опрашиваем снапшот
            url = host = items = None
            last_err: Optional[Exception] = None
            deadline = time.time() + NAV_LOAD_TIMEOUT_SEC
            while time.time() < deadline:
                try:
                    _, host, items = ba.snapshot_elements(tab_host, tab_id=tab_id)
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(NAV_POLL_SEC)
            if items is None:
                where = f"после «{' → '.join(done)}»" if done else "после открытия"
                raise RuntimeError(f"не читается страница {where}: {last_err}")
            # Оверлей-блокер (куки/подписка/geo-попап) снимаем до выбора
            # элемента: он перекрывает цель шага. Сняли — снапшот протух,
            # переснимаем (индексы разметки относились к удалённым узлам)
            try:
                dismissed = ba.dismiss_overlay(tab_host or host, tab_id=tab_id)
            except Exception:
                dismissed = None
            if dismissed:
                action.setdefault("overlays", []).append(dismissed)
                _, host, items = ba.snapshot_elements(tab_host, tab_id=tab_id)
            so_far = f" (прошёл: {' → '.join(done)})" if done else ""
            # Клик по шагу. DOM живых сайтов перерисовывается между снапшотом
            # и кликом (меню с таймерами) — «элемент потерян» лечим одним
            # повтором: свежий снапшот → свежий выбор → повторный клик.
            # «Клик без эффекта» (closed-loop, п.6) — тоже один повтор
            for attempt in (1, 2):
                idx, meta = self._choose_element(step, items, router)
                step_paths.append(str(meta.get("path") or "?"))
                if idx is None:
                    raise RuntimeError(
                        f"не нашёл на странице пункт «{step}»{so_far}")
                try:
                    ba.click_tagged(host, int(idx), tab_id=tab_id)
                    break
                except Exception as e:
                    from app.features.browser_actions import ClickUncertain
                    retryable = isinstance(e, ClickUncertain) \
                        or "элемент потерян" in str(e)
                    if attempt == 2 or not retryable:
                        raise RuntimeError(f"на шаге «{step}»{so_far}: {e}")
                    time.sleep(NAV_POLL_SEC)
                    _, host, items = ba.snapshot_elements(tab_host, tab_id=tab_id)
            done.append(step)
            # Переход + загрузка следующей страницы: ждём стабилизации DOM,
            # а не слепой слип — статичная страница отпускает раньше, живая
            # (дорендер SPA) — держит дольше в пределах бюджета
            ba.wait_dom_idle(tab_host or host, tab_id,
                             timeout_sec=NAV_SETTLE_SEC + 2.0, min_wait=0.5)
        if step_paths:
            # Пути выбора шагов — в аудит (п.7): видно, где понадобилась LLM
            action["choose"] = {"path": ",".join(step_paths),
                                "candidates": meta.get("candidates") if done else [],
                                "llm_response": meta.get("llm_response") if done else None}
        if tab_id is not None:
            # Финальная страница пути — «открывшаяся страница» для следующих
            # команд («скачай на открывшейся странице …»)
            self._last_tab_id = tab_id

    def _audit(self, chat_id: str, action: dict, ok: bool, detail: str,
               error_class: Optional[str] = None,
               extra: Optional[Dict[str, object]] = None):
        try:
            record = {
                "ts": time.time(), "chat_id": str(chat_id), "ok": ok,
                "kind": action.get("kind"), "key": action.get("key"),
                "value": (action.get("value") if action.get("kind") != "multi"
                          else " ; ".join(self.describe(a) for a in action["items"])),
                "detail": detail,
            }
            # Семантика действия для записи сценариев (ScenarioManager строит
            # шаги по тексту элемента, а не по idx — он между сессиями нестабилен)
            if action.get("element"):
                record["element"] = str(action["element"])[:80]
            if action.get("host"):
                record["host"] = action["host"]
            if action["kind"] == "type" and action.get("text"):
                record["text"] = str(action["text"])[:80]
            # Наблюдаемость (п.7): какой путь сработал (score/LLM/fallback),
            # какие кандидаты рассматривались и с каким скором, сырой ответ
            # LLM, результат closed-loop проверки и класс ошибки
            choose = action.get("choose") or {}
            if choose.get("path"):
                record["path"] = choose["path"]
            if choose.get("candidates"):
                record["candidates"] = choose["candidates"]
            if choose.get("llm_response"):
                record["llm_response"] = choose["llm_response"]
            if action.get("kind") in ("click", "download", "nav", "type"):
                record["verify"] = ("ok" if ok else
                                    "uncertain" if error_class == "uncertain"
                                    else "failed")
            if error_class:
                record["error_class"] = error_class
            if action.get("overlays"):
                # Оверлеи, авто-закрытые на пути nav: что именно нажимали
                record["overlays"] = [str(o)[:60] for o in action["overlays"]]
            if action.get("name_check"):
                # Мягкая верификация «тот ли сайт открыли» (п.5)
                record["name_check"] = action["name_check"]
            if extra:
                record.update(extra)
            with open(self.base_dir / "audit.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[CompControl] Аудит-лог не записан: {e}")

    def _audit_resolve(self, chat_id: str, goal: str, host: Optional[str],
                       detail: str, fail_reason: str,
                       meta: Optional[dict] = None):
        """Неудача РЕЗОЛВА элемента (до построения действия) — в тот же
        audit.jsonl. Раньше такие промахи в лог не попадали вообще, и причины
        чинились вслепую по жалобам. fail_reason: no_page | snapshot_error |
        not_in_snapshot | low_score | llm_veto | no_fields | captcha — база
        для авто-классификации причин отказов."""
        extra: Dict[str, object] = {"fail_reason": fail_reason}
        if meta:
            if meta.get("path"):
                extra["path"] = meta["path"]
            if meta.get("candidates"):
                extra["candidates"] = meta["candidates"]
            if meta.get("llm_response"):
                extra["llm_response"] = meta["llm_response"]
        self._audit(chat_id, {"kind": "resolve_fail",
                              "value": str(goal)[:80],
                              "host": host or ""},
                    False, detail, extra=extra)
        logger.info(f"[CompControl] Резолв «{str(goal)[:40]}» не удался "
                    f"({fail_reason}): {str(detail)[:80]}")
