"""Генерация скина нейросетью прямо из приложения (панель скинов).

Клиент (SkinGenerator) присылает экран, бриф пользователя, базовый файл
(шаблон экрана или экран скина из библиотеки — «перекрасить»), markdown
контракта скина (собирается во фронте из кода, contract.ts) и — для
итерации исправления — прошлый ответ модели со списком ошибок проверки.
Здесь — промпт, вызов LLM через общий ModelRouter (обычная цепочка
провайдеров с фолбэком или выбранный провайдер) и извлечение HTML-документа
из ответа. Валидация и runtime-проверка скина — на клиенте (engine.ts,
smokeTest.ts): там же живёт цикл автоисправления.

Ответ идёт SSE-потоком (файлы скина большие, генерация — минуты): события
{"status": ...}, {"progress": символов, "elapsed": сек} — раз в
PROGRESS_INTERVAL_SEC, пока модель пишет, и финал {"done": true, "html": ...}
или {"error": ...}. Разрыв соединения отменяет генерацию: следующий токен
стрима обрывает вызов провайдера.

Провайдерам с малым потолком вывода (~8k токенов) большой экран целиком не
влезает: оборванный ответ дописывается запросами-продолжениями (событие
{"status": "continue", "round": k}), куски склеиваются; "truncated": true
в финале — только если файл так и не закрыт и после них.

Inline-ассеты (data-URI картинок и шрифтов) базового файла модели не
отдаются — это сотни КБ base64: они заменяются короткими метками
vpc-asset:N и возвращаются в ответ после генерации.

Арт-направление. До генерации кода короткий вызов (POST
/api/skins/direction, propose_directions) превращает описание пользователя
в конкретные направления: сюжет, палитра с ролями, системные шрифты,
раскладка, фирменный элемент, фактура, движение, клише-запреты. Выбранное
направление клиент шлёт с КАЖДЫМ запросом экрана (и с исправлениями) —
три экрана получаются одним продуктом. Запрос генерации без направления
(«сразу генерировать») сервер дополняет направлением сам: событие
{"status": "direction", "direction": {...}} — клиент подхватывает его для
остальных экранов.

База-шаблон уходит модели без своего CSS и без длинных комментариев-
инструкций: модель видит только функциональный каркас (hook-точки, формы,
скрипты вкладок), а не готовый вид — иначе она перекрашивает шаблон вместо
дизайна. Функциональные CSS-требования, которые раньше обеспечивал CSS
шаблона, перечислены в промпте (FUNCTIONAL_CSS).
"""

import asyncio
import json
import logging
import re
import threading
import time

from pydantic import BaseModel, Field

from app.core.language import user_language_line

logger = logging.getLogger(__name__)

SCREENS = ("chat", "dossier", "room")

MAX_DESCRIPTION = 4000
MAX_CONTRACT_DOC = 120_000
# Файл целиком (с inline-ассетами) — как лимит файла скина (skins_api)
MAX_HTML_BYTES = 3 * 1024 * 1024
# Текст файла, который реально уходит модели (ассеты уже вырезаны)
MAX_HTML_FOR_LLM = 200_000
MAX_ERRORS = 40
MAX_ERROR_LEN = 2000
# Тело POST /api/skins/generate целиком (проверяется до разбора JSON, см.
# security.BodySizeLimit): базовый файл и прошлый ответ — каждый до
# MAX_HTML_BYTES (с ассетами), плюс контракт, ошибки и запас на экранирование
MAX_BODY_BYTES = 2 * MAX_HTML_BYTES + 2 * 1024 * 1024
_MAX_MODEL = 200

# Лимит вывода: файл экрана — 15–50 КБ (до ~20k токенов). Провайдер с
# меньшим потолком отвечает на такой запрос ошибкой — тогда второй заход
# с FALLBACK_MAX_TOKENS
MAX_TOKENS = 32000
FALLBACK_MAX_TOKENS = 8192
# Ответ оборван лимитом вывода (нет </html>; у deepseek/gemini-flash потолок
# ~8k токенов, а файл досье — ~53 КБ) — до стольких запросов-продолжений
# с перепиской «как есть»; куски склеиваются (см. stitch)
MAX_CONTINUATIONS = 4
# Модель, продолжая, иногда повторяет хвост уже написанного: совпадение
# конца накопленного с началом куска длиной от OVERLAP_MIN до OVERLAP_MAX
# символов срезается. Короче OVERLAP_MIN — не трогаем: в HTML короткие
# повторы («</div>\n») бывают и настоящими
OVERLAP_MIN = 24
OVERLAP_MAX = 600
LLM_TIMEOUT_SEC = 300.0
TEMPERATURE = 0.75

# Арт-направление: короткий ответ (JSON на 1–3 направления), температура
# выше — нужна разность идей, а не валидность кода
DIRECTION_MAX_TOKENS = 2500
DIRECTION_TEMPERATURE = 0.95
DIRECTION_TIMEOUT_SEC = 120.0
DIRECTION_MAX_COUNT = 3
# Названия уже показанных направлений («ещё варианты» — не повторяться)
DIRECTION_MAX_EXCLUDE = 12
DIRECTION_MAX_NOTE = 1000
# Тело POST /api/skins/direction: бриф и список исключений
DIRECTION_MAX_BODY_BYTES = 64 * 1024

# Комментарий базы длиннее — документация шаблона, модели не отдаётся
# (правила и так в контракте); короткие пометки REQUIRED/OPTIONAL остаются
LONG_COMMENT = 240

PROGRESS_INTERVAL_SEC = 0.5
# Отменённая генерация освобождает слот на следующем токене стрима —
# новый запрос ждёт её столько, прежде чем ответить «занято»
BUSY_WAIT_SEC = 5.0

_ASSET_RE = re.compile(r"data:[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+(?:;[a-zA-Z0-9=.+-]+)*,[A-Za-z0-9+/=%._-]{200,}")
_ASSET_TOKEN_RE = re.compile(r"vpc-asset:(\d+)")
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+-]{1,200}$")

# Одна генерация на сервер: вызов дорогой, клиент однопользовательский
_busy = threading.Lock()


class SkinGenRequest(BaseModel):
    screen: str
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    # Базовый файл: шаблон экрана ('template') или экран скина ('skin')
    base_html: str = Field(max_length=MAX_HTML_BYTES)
    base_kind: str = "template"
    contract_doc: str = Field(default="", max_length=MAX_CONTRACT_DOC)
    locale: str = "ru"
    # Итерация исправления: прошлый ответ модели (None — он был обрезан
    # или не содержал документа, тогда правится базовый файл) + ошибки
    previous_html: str | None = Field(default=None, max_length=MAX_HTML_BYTES)
    errors: list[str] = Field(default_factory=list, max_length=MAX_ERRORS)
    # None — обычная цепочка провайдеров; иначе id провайдера / local /
    # webchat:<сайт> (одна попытка вне цепочки, неудача — цепочка)
    provider: str | None = None
    model: str | None = Field(default=None, max_length=_MAX_MODEL)
    # Выбранное арт-направление (как его вернул /api/skins/direction, плюс
    # необязательная заметка пользователя note); проверяется normalize_direction.
    # None на обычной генерации — сервер подберёт направление сам
    direction: dict | None = None


class SkinDirectionRequest(BaseModel):
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    locale: str = "ru"
    count: int = Field(default=DIRECTION_MAX_COUNT, ge=1, le=DIRECTION_MAX_COUNT)
    # Названия уже предложенных направлений — «ещё варианты» без повторов
    exclude: list[str] = Field(default_factory=list, max_length=DIRECTION_MAX_EXCLUDE)
    provider: str | None = None
    model: str | None = Field(default=None, max_length=_MAX_MODEL)


class SkinGenError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class _Cancelled(Exception):
    """Клиент отключился — оборвать стрим провайдера."""


# ── Промпт ─────────────────────────────────────────────────────────────

# Системные шрифты с характером: сети в песочнице нет, веб-шрифты недоступны.
# Общий набор для арт-директора и для генерации кода
FONT_TOOLBOX = """- old-style serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif
- didone / display: Didot, "Bodoni 72", "Bodoni MT", serif
- humanist sans: Optima, Candara, "Segoe UI", sans-serif
- geometric sans: Futura, "Century Gothic", "Avenir Next", sans-serif
- condensed: "Avenir Next Condensed", "Arial Narrow", sans-serif-condensed, sans-serif
- slab / typewriter: Rockwell, "American Typewriter", "Courier New", serif
- mono: "SF Mono", Menlo, Consolas, "Cascadia Mono", monospace
- engraved caps: Copperplate, "Copperplate Gothic", serif"""

CRAFT = f"""Design craft:
1. Refuse the defaults. No system-ui, Inter, Roboto or Arial as the only typeface. No identical 8–12px-radius cards with soft drop shadows everywhere. No purple/blue gradient blobs, glassmorphism or neon glow unless the direction explicitly asks for it. No emoji as icons — use typographic marks, CSS shapes or tiny inline SVG. No layout where everything is centred and everything has the same weight. No polish sprinkled evenly (a glow here, a shadow there, hover animations on everything). Spend boldness in ONE place — the signature element — and keep everything else quiet, aligned and disciplined.
2. Typography without web fonts. Build characterful stacks from fonts already installed on the system:
{FONT_TOOLBOX}
Use a real type scale with strong contrast (a large display size against small, calm body text) and let case, letter-spacing, weight, italics and numerals (font-variant-numeric: oldstyle-nums, tabular-nums, small-caps) carry the voice. If the concept needs one lettered word (a title, a monogram), draw just that word as a small inline SVG.
3. Texture and ornament without images: layered gradients (repeating-linear, radial, conic), small tileable inline-SVG data-URI patterns and feTurbulence noise, mask and clip-path shapes, border-image, mix-blend-mode, stacked box-shadows for print, emboss or letterpress effects, CSS counters for numbering and ornaments. Keep every SVG tiny — the file must stay compact.
4. Layout is part of the design. The base file's structure (e.g. left sidebar + feed + right sidebar) is not sacred: hook elements can be moved and wrapped freely. The persona list can be a spine, tabs or a drawer; the context panel a ledger margin; messages entries, slips, strips or telegrams; dossier tabs a table of contents, index cards or binder tabs. Use a clear grid, deliberate asymmetry and density, and one dominant element per screen.
5. Colour. Build everything from the direction's palette, defined once as CSS variables. Body text must reach at least 4.5:1 contrast against its background. The app sets html[data-theme="light"] or "dark": the direction's palette defines the theme it naturally belongs to; design a considered variant for the other one (the same idea re-lit — paper instead of night, not an inverted copy) and keep both readable.
6. Motion: at most one or two meaningful motions (a message arriving, the typing indicator), short and eased; switch them off under @media (prefers-reduced-motion: reduce).
7. Self-check before writing code (internally, do not output it): would this screen look almost the same for any other brief in this genre? If yes, change it until it could only belong to this subject."""

SYSTEM_PROMPT = f"""You are a senior product designer and front-end engineer who builds skins for VPC, a web app for AI personas.
A skin is ONE self-contained HTML file for ONE screen (chat, dossier or room). The app renders it in a sandboxed iframe and fills its hook points (data-vpc* attributes) with live data through an injected bridge script. The skin contract in the task is the single source of truth for hook points.

How to work:
- The art direction in the task is BINDING: its subject, palette, fonts, layout idea, signature element, texture, motion and the clichés to avoid. Every visual decision comes from it.
- The base file is a FUNCTIONAL reference — which hook points, forms, tabs and behaviour the screen needs. It is NOT a visual starting point: do not keep its layout, spacing or boxes just because they are there.
- The three screens of a skin (chat, dossier, room) are generated separately from the same direction and must feel like one product: same palette, type, texture and signature motif.

{CRAFT}

Output rules (hard):
- Reply with ONLY the complete HTML document: start with <!DOCTYPE html>, end with </html>. No markdown fences, no explanations before or after.
- Return the WHOLE file, never "…" or "rest unchanged".
- Keep every data-vpc* attribute and every hook element of the base file (required and optional): move, wrap and restyle them freely, never remove or rename them — the app checks that every data-vpc / data-vpc-field / data-vpc-action / data-vpc-setting of the base file is still in your file, and a missing one fails the skin. Keep the behaviour of the base file (tabs, sub-tabs, collapsible lists, forms) and its <script> blocks working.
- Elements with data-vpc-label* are localized by the app: keep the attributes; their visible fallback text may be changed, but the app replaces it with the localized label.
- No network: no http/https URLs, CDNs, <link>, <script src>, @import, web fonts by URL. Images and fonts only as data-URI, CSS or inline SVG. Tokens like vpc-asset:3 are placeholders of inline assets — keep them verbatim where you reuse the asset.
- Keep the VPC-BRIDGE:START / VPC-BRIDGE:END marker block with an empty stub script inside — the app replaces it.
- Keep <meta name="vpc-skin-contract"> at the version from the contract; set <meta name="vpc-skin-name"> to the direction's name (or a short name of your design).
- Keep the file compact: no comments (except the bridge markers), no repeated boilerplate, no huge decorative SVG.
- Declare the --vpc-shell-* variables in :root exactly as the task gives them (they must be identical in all three files of the skin); without given values, derive them from your palette and fonts."""

# Функциональные CSS-требования: у шаблона их обеспечивал его CSS, а модели
# шаблон уходит без стилей (strip_base) — правила перечислены явно.
# На них опираются приложение, bridge и смоук-тест (smokeTest.ts)
FUNCTIONAL_CSS: dict[str, list[str]] = {
    "common": [
        'The screen root is hidden by default and visible under the active screen: [data-vpc-screen] { display: none; } html[data-vpc-active="{screen}"] [data-vpc-screen="{screen}"] { display: flex or grid; }. It fills the frame (height: 100vh) — the page itself must not scroll past it.',
        "Respect the hidden attribute everywhere: [hidden] { display: none !important; } — the app toggles hidden on bars, previews, expand buttons and optional blocks, and your display rules must not override it.",
        "Never give <template> elements a display value: they are blueprints, not content.",
        "Collapsed lists: [data-collapsed]:not([data-expanded]) > [data-extra] { display: none; }. A [data-vpc-expand] button shows its .when-collapsed label by default and its .when-expanded label only with [data-expanded].",
        "Scrolling areas scroll inside the screen: overflow-y: auto plus min-height: 0 on flex/grid children.",
        "Every block that holds hook points stays reachable at every width (the frame can be about 700–900px wide next to the app sidebar): never hide it with display: none or visibility: hidden in a @media query without a replacement — collapse it into a drawer or tab opened by a visible button, or move it below the main content.",
    ],
    "chat": [
        'The message feed [data-vpc="messages"] is the scrolling area of the conversation (flex: 1; min-height: 0; overflow-y: auto); the input row stays pinned below it and always visible.',
        "Input row: the attach button, the input taking the remaining width (flex: 1; min-width: 0), the send button.",
        'Style message roles through [data-role="user"] and [data-role="persona"] on the message root; message text keeps line breaks and wraps long words (white-space: pre-wrap; overflow-wrap: anywhere); message images are width-limited.',
        'The typing indicator is hidden unless it has [data-active]. The reply bar is hidden while [hidden]. The attachment bar shows only while its [data-vpc="attach-preview"] is not hidden (e.g. with :has()).',
        "Side panels scroll on their own when their lists grow; the persona item with [data-active] is visibly selected.",
        "Narrow frames: the persona list and the context panel (mood, todos, inventory, reminders) must not simply disappear below some width — turn them into a slide-out drawer with a visible toggle, or stack them under the feed; the feed and the input row keep priority.",
    ],
    "dossier": [
        "Tabs: the script toggles the class tab-active on [data-tab] buttons and page-active on [data-tab-panel] panels within the same [data-tab-scope]. Inactive panels MUST be hidden — [data-tab-panel]:not(.page-active) { display: none; } — and the active button clearly marked.",
        "The memory sub-tab panels (stm / ltm / diary) stay INSIDE the memory panel, so they disappear together with it on other tabs.",
        "The content area below the tab bar scrolls (overflow-y: auto; min-height: 0); the tab bar stays reachable.",
        'State attributes must be visible: [data-done] todos, [data-active="false"] reminders (dimmed), [data-enabled="true"] feature flags, [data-main="true"] provider rows (hide their make-main button), [data-backup="false"]. Progress fills ([data-vpc-bar]) get their width from the app: give them height and colour, never a fixed width.',
        "Checkboxes and selects stay real form controls: style them, do not replace them.",
    ],
    "room": [
        'The scene [data-vpc="room-scene"] is position: relative; overflow: hidden, with real height (flex: 1; min-height about 320px).',
        "The app moves the avatar through --vpc-x / --vpc-y: position: absolute; left: var(--vpc-x, 50%); top: var(--vpc-y, 78%); transform: translate(-50%, -100%); a short transition on left/top.",
        'The background <img data-vpc="room-bg"> covers the scene (position: absolute; inset: 0; width/height 100%; object-fit: cover) under everything else and stays hidden while [hidden].',
        'The pet [data-vpc="room-pet"] is absolutely positioned in the scene and hidden while [hidden]; its markup has no picture — draw it for [data-pet="cat"] and [data-pet="crow"] with CSS or a tiny inline SVG.',
        "Stats, event feed and inventory live outside the scene and scroll if long.",
    ],
}

_LANG_NAME = {"ru": "Russian", "en": "English"}


def _lang_line(locale: str) -> str:
    # Язык пользователя — локаль UI, из которой он генерирует скин
    return (f"Language of visible fallback texts: {_LANG_NAME.get(locale, 'Russian')}.\n"
            + user_language_line(locale))


def _functional_css(screen: str, fixing: bool = False) -> str:
    rules = [r.replace("{screen}", screen) for r in FUNCTIONAL_CSS["common"]] + FUNCTIONAL_CSS.get(screen, [])
    intro = ("Whatever the file looks like, its CSS must keep these behaviours" if fixing else
             "The base file comes without its styles — you write all CSS. Whatever it looks like, "
             "it must keep these behaviours")
    return ("# Functional CSS requirements\n" + intro + " (the app and its checks rely on them):\n"
            + "\n".join(f"- {r}" for r in rules))


def build_messages(req: SkinGenRequest, base_html: str, previous_html: str | None,
                   direction: dict | None = None) -> list[dict]:
    """Сообщения для LLM. base_html / previous_html — уже без inline-ассетов
    (и, у шаблона, без CSS и длинных комментариев — strip_base); direction —
    нормализованное арт-направление (normalize_direction) или None."""
    brief = req.description.strip() or (
        "(no brief — the art direction is all there is)" if direction else
        "(no brief — invent a fitting, concrete design yourself)")
    screen = req.screen
    parts: list[str] = []
    if req.errors:
        parts.append(
            f'# Task\nYour previous skin file for the "{screen}" screen failed the app checks. '
            "Fix every problem listed below and return the complete corrected file. "
            + ("Keep the design and stay inside the art direction; change only what is needed."
               if direction else "Keep the design; change only what is needed.")
        )
        parts.append("# Problems\n" + "\n".join(f"- {e}" for e in req.errors))
        # Исправление — без стадии направления: только присланное клиентом
        if direction:
            parts.append(format_direction(direction))
        parts.append("# Original brief\n" + brief)
    else:
        if req.base_kind == "skin":
            what = ("an existing skin of this screen: redesign it into the art direction — its CSS is "
                    "yours to rewrite; keep its hook points and behaviour")
        else:
            what = ("the default template of this screen with its styles removed: a functional reference "
                    "(hook points, forms, tabs, scripts), not a look to restyle")
        parts.append(
            f'# Task\nCreate the "{screen}" screen of a skin from the art direction below. '
            f"The base file at the end is {what}."
        )
        parts.append(format_direction(direction) if direction else (
            "# Art direction\n(none given — before writing code, derive one concrete direction from the brief "
            "yourself: a subject that is a concrete thing, not a genre; a palette with roles; system font "
            "stacks; a layout idea; one signature element)"))
        parts.append("# Client's brief\n" + brief)
    parts.append(_lang_line(req.locale))
    # Требования, которые у шаблона обеспечивал его (вырезанный) CSS; у базы-
    # скина CSS свой и остаётся в файле
    if req.base_kind == "template":
        parts.append(_functional_css(screen, fixing=previous_html is not None))
    if req.contract_doc.strip():
        parts.append("# Skin contract\n" + req.contract_doc.strip())
    if previous_html is not None:
        parts.append(f"# Previous file ({screen}) — fix this one\n" + previous_html)
    else:
        parts.append(f"# Base file ({screen}) — functional reference\n" + base_html)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# ── Арт-направление ────────────────────────────────────────────────────

DIRECTION_SYSTEM_PROMPT = f"""You are the art director of VPC, a web app for AI personas. A client wants a skin — the visual shell of three screens of one persona: chat (persona list, message feed, input row, context panel with mood, todos, inventory), dossier (tabbed memory, tasks, initiative, learning, files, settings, many small forms) and room (a small scene with the persona's avatar and pet, stats, event feed, inventory). You do not write code: you turn the client's short description into concrete art directions a designer will build all three screens from.

The skin runs in a sandboxed iframe with NO network: no web fonts, no remote images. Type comes only from fonts installed on the viewer's system; surfaces only from colour, CSS gradients and tiny inline SVG.

How to think:
- A genre ("dark library", "cyberpunk", "cosy forest") is not a direction: it has a ready-made default that every generator produces. Each direction starts from a SUBJECT — one concrete thing, person, place, ritual or moment behind the interface ("the returns ledger a night-shift librarian keeps in a closed reading room", not "dark academia"). Every other decision is derived from the subject.
- Directions differ in kind, not in shade: different subjects, different palettes (value and temperature), different type genres, different layouts. One can read the brief literally, one sideways, one through a single unexpected detail.
- palette: 5–6 colours, each with a role and a reason taken from the subject ("the green of the reading-lamp shade", not "calm green"). Roles: background, surface, ink, muted, accent, and optionally accent2. Ink on background at least 4.5:1 contrast; muted on background at least 3:1.
- fonts: SYSTEM stacks only, written as CSS font-family values with fallbacks — display, text and optionally mono. Toolbox:
{FONT_TOOLBOX}
  Never make system-ui, Inter, Roboto, Arial or Helvetica the character of a direction.
- layout: how the screens are organised, 1–2 sentences (e.g. the persona list as the spine of a ledger, messages as dated entries, context as margin notes, dossier tabs as index cards). NOT "left sidebar + centre + right sidebar with rounded cards" unless the subject truly demands it.
- signature: the ONE element people will remember. Boldness is spent there; everything else stays quiet.
- texture: how surfaces are made without images (gradients, SVG noise or pattern, rules and borders, print effects).
- motion: at most one or two restrained motions, each with a reason.
- avoid: 4–6 clichés specific to THIS genre that would make the result look like every other AI-made interface.

Output ONLY a JSON object — no prose, no markdown fences:
{{"directions": [{{"name": "...", "subject": "...", "mood": "...", "palette": [{{"role": "background", "hex": "#1b1a17", "reason": "..."}}], "fonts": {{"display": "Didot, 'Bodoni 72', serif", "text": "'Iowan Old Style', Palatino, Georgia, serif", "mono": "", "why": "..."}}, "layout": "...", "signature": "...", "texture": "...", "motion": "...", "avoid": ["...", "..."]}}]}}
Inside JSON strings never use unescaped double quotes: quote multi-word font names with SINGLE quotes ('Iowan Old Style'), and use «ёлочки» or single quotes for quotations inside text.
Keep values short: name up to 4 words, other text fields 1–2 sentences."""

DIRECTION_RETRY_PROMPT = (
    "Your reply could not be used: {reason}\n"
    "Return ONLY the corrected JSON object {{\"directions\": [...]}} in the exact shape described — "
    "no prose, no markdown fences, double-quoted keys and strings, no trailing commas. Inside strings "
    "never use unescaped double quotes: font names with spaces go in single quotes, e.g. "
    "\"'Iowan Old Style', Palatino, serif\". Every direction needs name, subject, palette colours "
    "with roles background, ink and accent (hex #rrggbb), and fonts.display / fonts.text."
)


def build_direction_messages(req: SkinDirectionRequest) -> list[dict]:
    brief = req.description.strip() or "(no description — surprise the client with concrete, distinct ideas)"
    lang = _LANG_NAME.get(req.locale, "Russian")
    parts = [
        "# Client's description\n" + brief,
        f"Give exactly {req.count} direction{'s' if req.count > 1 else ''}. "
        f"Write all text values in {lang}; keep hex codes and font names as they are.",
    ]
    names = [n.strip()[:80] for n in req.exclude if n.strip()]
    if names:
        parts.append("Already proposed — do not repeat these ideas or their subjects: " + "; ".join(names))
    parts.append(user_language_line(req.locale))
    return [
        {"role": "system", "content": DIRECTION_SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


_JSON_START_RE = re.compile(r"[\[{]")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
# hex внутри строки с пояснением («#1b1a17 — сажа»)
_HEX_IN_TEXT_RE = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])")
# Шрифтовой стек: имена, кавычки, запятые — ничего, что превратит его в URL/CSS-инъекцию
_STACK_BAD_RE = re.compile(r"[;{}<>()\\]|url|https?:|@import", re.IGNORECASE)
_ROLE_ALIASES = {
    "background": "background", "bg": "background", "фон": "background",
    "surface": "surface", "panel": "surface", "paper": "surface", "поверхность": "surface",
    "ink": "ink", "text": "ink", "foreground": "ink", "текст": "ink", "чернила": "ink",
    "muted": "muted", "muted ink": "muted", "muted_ink": "muted", "muted-ink": "muted", "dim": "muted",
    "secondary": "muted", "приглушённый": "muted",
    "accent": "accent", "primary": "accent", "акцент": "accent",
    "accent2": "accent2", "accent 2": "accent2", "accent_2": "accent2", "accent-2": "accent2",
    "second accent": "accent2", "secondary accent": "accent2", "второй акцент": "accent2",
}
_TEXT_LIMITS = {"name": 60, "subject": 400, "mood": 300, "layout": 500, "signature": 400,
                "texture": 400, "motion": 300}
_MAX_PALETTE = 7
_MAX_AVOID = 8
_DEFAULT_MONO = '"SF Mono", Menlo, Consolas, "Cascadia Mono", monospace'


def _clean_text(v, limit: int) -> str:
    if not isinstance(v, str):
        return ""
    v = re.sub(r"\s+", " ", v).strip()
    return v if len(v) <= limit else v[:limit - 1].rstrip() + "…"


def _norm_hex(v) -> str | None:
    if not isinstance(v, str):
        return None
    m = _HEX_RE.match(v.strip()) or _HEX_IN_TEXT_RE.search(v)
    if not m:
        return None
    h = m.group(1).lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


_MAX_STACK = 200


def _norm_stack(v) -> str:
    """CSS-значение font-family или "" (не годится). Стек уходит в :root всех
    трёх файлов: длинный режется по запятой (не посреди имени — «…» внутри
    кавычек сломал бы правило), непарные кавычки — отказ."""
    if not isinstance(v, str):
        return ""
    v = re.sub(r"\s+", " ", v).strip().rstrip(",").strip()
    if len(v) > _MAX_STACK:
        cut = v.rfind(",", 0, _MAX_STACK + 1)
        v = v[:cut].strip() if cut > 0 else ""
    if not v or _STACK_BAD_RE.search(v) or v.count('"') % 2 or v.count("'") % 2:
        return ""
    return v


def normalize_direction(raw) -> dict | None:
    """Проверенное направление или None (не годится). Тексты обрезаются по
    лимитам, hex приводится к #rrggbb (кривые цвета отбрасываются), роли
    палитры — к background/surface/ink/muted/accent/accent2. Обязательны
    name, subject, цвета background/ink/accent и хотя бы один шрифт."""
    return check_direction(raw)[0]


def _palette_items(items) -> list:
    # Палитра словарём {"background": "#…"} или {"background": {"hex": …}} → список
    if isinstance(items, dict):
        out = []
        for role, v in items.items():
            if isinstance(v, dict):
                out.append({**v, "role": v.get("role") or role})
            else:
                out.append({"role": role, "hex": v})
        return out
    return items if isinstance(items, list) else []


def check_direction(raw) -> tuple[dict | None, str]:
    """(направление | None, причина отказа по-английски — для повтора и лога)."""
    if not isinstance(raw, dict):
        return None, "a direction is not a JSON object"
    d: dict = {k: _clean_text(raw.get(k), n) for k, n in _TEXT_LIMITS.items()}
    if not d["name"] or not d["subject"]:
        return None, "missing name or subject"

    palette: list[dict] = []
    seen: set[str] = set()
    for item in _palette_items(raw.get("palette")):
        if not isinstance(item, dict):
            continue
        hx = _norm_hex(item.get("hex"))
        if not hx:
            continue
        role = _clean_text(item.get("role"), 24).lower()
        role = _ROLE_ALIASES.get(role, role) or "extra"
        if role in seen and role in _ROLE_ALIASES.values():
            continue
        seen.add(role)
        palette.append({"role": role, "hex": hx, "reason": _clean_text(item.get("reason"), 160)})
        if len(palette) >= _MAX_PALETTE:
            break
    missing = {"background", "ink", "accent"} - seen
    if missing:
        return None, f'"{d["name"]}": palette lacks valid hex colours for roles ' + ", ".join(sorted(missing))
    d["palette"] = palette

    f = raw.get("fonts")
    if isinstance(f, str):
        f = {"text": f}
    f = f if isinstance(f, dict) else {}
    display, text = _norm_stack(f.get("display")), _norm_stack(f.get("text"))
    if not display and not text:
        return None, f'"{d["name"]}": fonts.display / fonts.text missing or not a plain CSS font-family list'
    d["fonts"] = {
        "display": display or text,
        "text": text or display,
        "mono": _norm_stack(f.get("mono")),
        "why": _clean_text(f.get("why"), 300),
    }

    avoid = raw.get("avoid")
    if isinstance(avoid, str):
        avoid = re.split(r"[;\n]+", avoid)
    d["avoid"] = [a for a in (_clean_text(x, 200) for x in (avoid if isinstance(avoid, list) else [])) if a][:_MAX_AVOID]
    note = _clean_text(raw.get("note"), DIRECTION_MAX_NOTE)
    if note:
        d["note"] = note
    return d, ""


def _color(d: dict, role: str) -> str | None:
    return next((c["hex"] for c in d["palette"] if c["role"] == role), None)


def _mix(a: str, b: str, t: float) -> str:
    # Смесь двух #rrggbb: t — доля b
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def shell_vars(d: dict) -> list[tuple[str, str]]:
    """--vpc-shell-* из палитры и шрифтов направления: считаются здесь, а не
    моделью, — у трёх экранов скина они выходят буква в букву одинаковыми."""
    bg, ink, accent = _color(d, "background"), _color(d, "ink"), _color(d, "accent")
    panel = _color(d, "surface") or bg
    fonts = d["fonts"]
    return [
        ("--vpc-shell-bg", bg),
        ("--vpc-shell-panel", panel),
        ("--vpc-shell-text", ink),
        ("--vpc-shell-dim", _color(d, "muted") or _mix(bg, ink, 0.6)),
        ("--vpc-shell-border", _mix(panel, ink, 0.18)),
        ("--vpc-shell-accent", accent),
        ("--vpc-shell-font", fonts["text"]),
        ("--vpc-shell-font-disp", fonts["display"]),
        ("--vpc-shell-font-mono", fonts["mono"] or _DEFAULT_MONO),
    ]


def format_direction(d: dict) -> str:
    fonts = d["fonts"]
    lines = [
        "# Art direction (binding)",
        f"Name: {d['name']}",
        f"Subject: {d['subject']}",
    ]
    if d.get("mood"):
        lines.append(f"Mood: {d['mood']}")
    lines.append("Palette:")
    lines += [f"- {c['role']}: {c['hex']}" + (f" — {c['reason']}" if c["reason"] else "") for c in d["palette"]]
    lines.append(f"Display type: {fonts['display']}")
    lines.append(f"Text type: {fonts['text']}")
    if fonts["mono"]:
        lines.append(f"Mono: {fonts['mono']}")
    if fonts["why"]:
        lines.append(f"Why these fonts: {fonts['why']}")
    for key, label in (("layout", "Layout"), ("signature", "Signature element (the one bold thing)"),
                       ("texture", "Texture"), ("motion", "Motion")):
        if d.get(key):
            lines.append(f"{label}: {d[key]}")
    if d["avoid"]:
        lines.append("Avoid: " + "; ".join(d["avoid"]))
    if d.get("note"):
        lines.append(f"Client's note on this direction (takes priority): {d['note']}")
    lines.append(
        "All three screens of this skin (chat, dossier, room) are built from this direction and must feel "
        "like one product. Declare these shell variables in :root exactly as written — identical in all "
        "three files:"
    )
    lines += [f"  {k}: {v};" for k, v in shell_vars(d)]
    return "\n".join(lines)


# Значение шрифтового поля на своей строке с двойными кавычками внутри:
#   "display": ""Iowan Old Style", Palatino, serif",
_FONT_LINE_RE = re.compile(r'^(\s*"(?:display|text|mono)"\s*:\s*")(.*)("\s*,?\s*)$', re.MULTILINE)


_NEXT_KEY_RE = re.compile(r'\s*["\u201c][A-Za-z_][A-Za-z0-9_ -]{0,40}["\u201d]\s*:')


def _string_closes(s: str, j: int, in_array: bool) -> bool:
    # Кавычка перед позицией j закрывает строку, если дальше идёт : } ] или
    # запятая со следующим элементом: в объекте — с ключом «"имя":», в
    # массиве — с новым значением. Иначе это кавычка внутри текста
    # (""Iowan Old Style", Palatino" — после запятой не ключ)
    n = len(s)
    while j < n and s[j] in " \t\r\n":
        j += 1
    if j >= n or s[j] in ":}]":
        return True
    if s[j] != ",":
        return False
    if not in_array:
        return bool(_NEXT_KEY_RE.match(s, j + 1))
    j += 1
    while j < n and s[j] in " \t\r\n":
        j += 1
    return j >= n or s[j] in '"{[]\u201c'


def repair_json(text: str) -> str:
    """Типовые поломки JSON от веб-чатов: неэкранированные кавычки внутри
    строк (имена шрифтов, цитаты), типографские кавычки-разделители, живые
    переводы строки в строках, висячие запятые."""
    text = _FONT_LINE_RE.sub(lambda m: m.group(1) + m.group(2).replace('"', "'") + m.group(3), text)
    out: list[str] = []
    stack: list[str] = []  # открытые { и [ вне строк
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if not in_str:
            if c in "\u201c\u201d":
                c = '"'
            if c == '"':
                in_str = True
            elif c in "{[":
                stack.append(c)
            elif c in "}]" and stack:
                stack.pop()
            out.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "\n":
            out.append("\\n")
        elif c in '"\u201d' and _string_closes(text, i + 1, bool(stack) and stack[-1] == "["):
            in_str = False
            out.append('"')
        elif c == '"':
            out.append('\\"')
        else:
            out.append(c)
        i += 1
    return _TRAILING_COMMA_RE.sub(r"\1", "".join(out))


def _decode_at(decoder: json.JSONDecoder, text: str, start: int):
    """(данные, конец | None) JSON с позиции start: как есть, без висячих
    запятых, после repair_json. ValueError — не разобрать."""
    try:
        return decoder.raw_decode(text, start)
    except ValueError:
        pass
    for fixed in (_TRAILING_COMMA_RE.sub(r"\1", text[start:]), repair_json(text[start:])):
        try:
            return decoder.raw_decode(fixed)[0], None
        except ValueError:
            continue
    raise ValueError("unparseable")


def explain_directions_failure(text: str | None) -> str:
    """Почему из ответа не вышло ни одного направления — коротко, по-английски:
    уходит модели в повторный запрос и в лог."""
    if not text or not text.strip():
        return "the reply was empty"
    t = _FENCE_RE.sub("", text)
    m = _JSON_START_RE.search(t)
    if not m:
        return "the reply contains no JSON object"
    try:
        data = json.loads(repair_json(t[m.start():t.rfind("}") + 1] or t[m.start():]))
    except json.JSONDecodeError as e:
        near = e.doc[max(0, e.pos - 40):e.pos + 40].replace("\n", " ")
        return f"invalid JSON ({e.msg}, line {e.lineno} column {e.colno}) near: …{near}…"
    items = data.get("directions") if isinstance(data, dict) else data
    if isinstance(data, dict) and items is None and "palette" in data:
        items = [data]
    if not isinstance(items, list) or not items:
        return 'no "directions" array in the JSON'
    reasons = [check_direction(x)[1] for x in items]
    return "; ".join(r for r in reasons if r)[:600] or "no usable directions"


def parse_directions(text: str | None, limit: int = DIRECTION_MAX_COUNT) -> list[dict]:
    """Направления из ответа модели: ограды markdown и текст вокруг
    отбрасываются, берётся первый разбираемый JSON-объект (или массив) —
    {"directions": [...]}, [...] или одно направление; висячие запятые
    прощаются. Негодные направления выкидываются; [] — ничего не нашлось."""
    if not text:
        return []
    text = _FENCE_RE.sub("", text)
    decoder = json.JSONDecoder()
    # Внешний объект битый, а отдельные направления в нём целы — собираем их
    singles: list[dict] = []
    pos = 0
    while (m := _JSON_START_RE.search(text, pos)) is not None:
        start, pos = m.start(), m.start() + 1
        try:
            data, end = _decode_at(decoder, text, start)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("directions"), list):
            items = data["directions"]
        elif isinstance(data, list):
            items = data
        elif isinstance(data, dict) and "palette" in data:
            d = normalize_direction(data)
            if d:
                singles.append(d)
                if end is not None:
                    pos = end
            continue
        else:
            continue
        out = [d for d in (normalize_direction(x) for x in items) if d]
        if out:
            return out[:limit]
    return singles[:limit]


# ── Inline-ассеты ──────────────────────────────────────────────────────

def strip_assets(html: str, assets: list[str]) -> str:
    """Длинные data-URI → метки vpc-asset:N (N — индекс в assets; одинаковые
    ассеты получают одну метку)."""
    def repl(m: re.Match) -> str:
        uri = m.group(0)
        try:
            idx = assets.index(uri)
        except ValueError:
            assets.append(uri)
            idx = len(assets) - 1
        return f"vpc-asset:{idx}"
    return _ASSET_RE.sub(repl, html)


def restore_assets(html: str, assets: list[str]) -> str:
    def repl(m: re.Match) -> str:
        idx = int(m.group(1))
        return assets[idx] if idx < len(assets) else m.group(0)
    return _ASSET_TOKEN_RE.sub(repl, html) if assets else html


# ── База для модели ────────────────────────────────────────────────────

# Блоки, которые разбираются по порядку появления в документе: комментарий
# раньше скрипта (в шапке шаблона есть текст «<script src=...>»), скрипт
# раньше комментария (строка «<!--» внутри JS), стиль — целиком
#
# Комментарий захватывается с отступом строки (если стоит с её начала) и
# переводом строки после себя — вырезанный комментарий на отдельной строке
# не оставляет пустую строку из пробелов (см. _drop_comment)
_BASE_TOKEN_RE = re.compile(
    r"(?P<comment>(?:^[ \t]*)?<!--.*?-->(?:[ \t]*\n)?)"
    r"|(?P<script><script\b[^>]*>.*?</script\s*>)"
    r"|(?P<style>(?P<open><style\b[^>]*>)(?P<css>.*?)(?P<close></style\s*>))",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
_CSS_COMMENT_RE = re.compile(r"(?:^[ \t]*)?/\*.*?\*/(?:[ \t]*\n)?", re.DOTALL | re.MULTILINE)
_BLANK_LINES_RE = re.compile(r"\n[ \t]*(?:\n[ \t]*){2,}")
STYLES_REMOVED = "\n/* styles removed — design your own (see the functional CSS requirements) */\n"
# Комментарии-маркеры блока моста (как BRIDGE_START_RE / BRIDGE_END_RE в engine.ts)
_BRIDGE_MARKER_RE = re.compile(r"^<!--\s*=+\s*VPC-BRIDGE:(?:START|END)\b")


def _is_long_comment(text: str) -> bool:
    text = text.strip()
    return len(text) > LONG_COMMENT and not _BRIDGE_MARKER_RE.match(text)


def _drop_comment(m: re.Match) -> str:
    """Замена длинного комментария (HTML или CSS). Комментарий на своей
    строке уходит вместе с отступом и переводом строки; стоящий после текста
    той же строки оставляет перевод строки — иначе слова по краям склеились
    бы («текст<!-- … -->\\nслово» → «текст\\nслово», а не «текстслово»)."""
    s, start, text = m.string, m.start(), m.group(0)
    if not _is_long_comment(text):
        return text
    ends_line = text.endswith("\n")
    own_line = start == 0 or s[start - 1] == "\n"
    return "\n" if ends_line and not own_line else ""


def strip_base(html: str, kind: str) -> str:
    """Базовый файл для модели: длинные комментарии-инструкции (HTML и CSS)
    вырезаются, у шаблона ('template') — ещё и содержимое <style>: модель
    получает функциональный каркас, а не вид для перекраски. Скрипты,
    разметка, data-vpc*, меты и блок VPC-BRIDGE остаются как есть.

    Один проход по документу: комментарии, скрипты и стили разбираются в
    порядке появления, поэтому «<script src=...>» внутри комментария шапки и
    «<!--» внутри JS не путают разбор; содержимое <script> не трогается."""
    def repl(m: re.Match) -> str:
        if m.group("comment") is not None:
            return _drop_comment(m)
        if m.group("script") is not None:
            return m.group(0)
        if kind == "template":
            return m.group("open") + STYLES_REMOVED + m.group("close")
        css = _CSS_COMMENT_RE.sub(_drop_comment, m.group("css"))
        return m.group("open") + css + m.group("close")
    return _BLANK_LINES_RE.sub("\n\n", _BASE_TOKEN_RE.sub(repl, html))


# ── Разбор ответа ──────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$", re.MULTILINE)
_LEAD_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*[ \t]*\n")
_TRAIL_FENCE_RE = re.compile(r"\n?[ \t]*```\s*$")
_DOC_START_RE = re.compile(r"^\s*(?:<!doctype|<html[\s>])", re.IGNORECASE)

CONTINUE_PROMPT = (
    "Your reply was cut off by the output limit. Continue the HTML file exactly "
    "where you stopped: output only the remaining part, starting with the very next "
    "character. Do not repeat anything already written, do not restart the file, "
    "no markdown fences, no explanations. Finish with </html>."
)


def extract_html(text: str) -> tuple[str | None, bool]:
    """HTML-документ из ответа модели: (html, обрезан ли). Markdown-ограды и
    текст вокруг документа отбрасываются; нет </html> — ответ оборван
    (лимит вывода), отдаётся как есть с флагом truncated. None — документа
    в ответе нет."""
    if not text:
        return None, False
    low = text.lower()
    start = low.find("<!doctype")
    if start == -1:
        start = low.find("<html")
    if start == -1:
        return None, False
    end = low.rfind("</html>")
    if end == -1 or end < start:
        body = _FENCE_RE.sub("", text[start:]).rstrip()
        return body, True
    return text[start:end + len("</html>")], False


def is_complete(text: str | None) -> bool:
    # В ответе есть документ и он закрыт </html>
    html, truncated = extract_html(text or "")
    return html is not None and not truncated


def stitch(acc: str, chunk: str) -> str:
    """Приклеивает кусок-продолжение к накопленному ответу. Ограды markdown
    по краям куска отбрасываются; повтор хвоста накопленного в начале куска
    (OVERLAP_MIN..OVERLAP_MAX символов, в т.ч. после пробельного начала)
    срезается. Кусок, начинающийся заново с <!DOCTYPE/<html, — модель
    начала файл сначала вопреки просьбе: он заменяет накопленное (склейка дала
    бы два документа подряд), дальше продолжается уже он."""
    chunk = _LEAD_FENCE_RE.sub("", chunk, count=1)
    chunk = _TRAIL_FENCE_RE.sub("", chunk, count=1)
    if not chunk.strip():
        return acc
    if _DOC_START_RE.match(chunk):
        return chunk.lstrip()
    for head in (chunk, chunk.lstrip()):
        top = min(OVERLAP_MAX, len(head), len(acc))
        for k in range(top, OVERLAP_MIN - 1, -1):
            if acc.endswith(head[:k]):
                return acc + head[k:]
    return acc + chunk


# ── Вызов LLM ──────────────────────────────────────────────────────────

def _make_router():
    # Отдельный роутер без персоны: глобальная цепочка провайдеров
    # (как у классификаторов в app/features); веб-чаты — изолированный контекст
    from app.core.router import ModelRouter
    return ModelRouter(context="skin_gen")


def _provider_ok(router, provider: str) -> bool:
    if provider == "local":
        return True
    if provider == "webchat":
        return bool(router.webchat_sites)
    if provider.startswith("webchat:"):
        return provider.split(":", 1)[1] in router.webchat_sites
    return provider in router.available


def _answered_by(router) -> tuple[str | None, str | None]:
    # Кто ответил — читать в том же потоке, что вёл вызов (см. server._answer_provider)
    pid = getattr(router, "_last_provider", None)
    if not pid:
        return None, None
    if pid == "local":
        from app.core.config import OLLAMA_MODEL
        return pid, getattr(router, "_last_local_model", None) or OLLAMA_MODEL
    model = router.model_for(pid) if hasattr(router, "model_for") else ""
    return pid, model or None


def _router_for(provider: str | None, model: str | None):
    """(роутер, force_provider): обычная цепочка или выбранный провайдер
    (с моделью-override для облачного)."""
    try:
        router = _make_router()
    except RuntimeError:
        # ModelRouter без единого провайдера (ни ключей, ни веб-чата, ни Ollama)
        raise SkinGenError(503, "Нет настроенных провайдеров LLM — добавьте ключ в разделе «API-ключи».")
    force = None
    if provider:
        if not _provider_ok(router, provider):
            raise SkinGenError(400, f"Провайдер '{provider}' недоступен")
        force = provider
        if model and provider in getattr(router, "available", {}):
            router.model_overrides[provider] = model
    return router, force


def run_directions(req: SkinDirectionRequest, is_cancelled=None) -> tuple[list[dict], bool, str | None, str | None]:
    """Синхронный вызов арт-директора (поток пула): (направления, ответил ли
    кто-нибудь, провайдер, модель). Ответ, из которого не разобрать ни одного
    направления, — один повторный запрос у того же провайдера с просьбой
    вернуть только JSON."""
    router, force = _router_for(req.provider, req.model)
    messages = build_direction_messages(req)

    def _tok(_delta: str):
        if is_cancelled and is_cancelled():
            raise _Cancelled()

    def _call(msgs: list[dict], provider: str | None) -> str | None:
        return router.get_response_stream(
            msgs, _tok, temperature=DIRECTION_TEMPERATURE, max_tokens=DIRECTION_MAX_TOKENS,
            timeout=DIRECTION_TIMEOUT_SEC, force_provider=provider,
            webchat_channel="side", user_path=True,
        )

    text = _call(messages, force)
    provider, model = _answered_by(router)
    dirs = parse_directions(text, req.count)
    if not dirs and text and not (is_cancelled and is_cancelled()):
        reason = explain_directions_failure(text)
        logger.info(f"[skin-gen] направления не разобраны ({len(text)} симв.): {reason} | "
                    f"начало ответа: {text[:300]!r} — повтор с указанием ошибки")
        retry = messages + [
            {"role": "assistant", "content": text[:8000]},
            {"role": "user", "content": DIRECTION_RETRY_PROMPT.format(reason=reason)},
        ]
        text2 = _call(retry, provider or force)
        if text2:
            dirs = parse_directions(text2, req.count)
            provider, model = _answered_by(router)
            if not dirs:
                logger.info(f"[skin-gen] повтор тоже не разобран ({len(text2)} симв.): "
                            f"{explain_directions_failure(text2)} | начало: {text2[:300]!r}")
    return dirs, bool(text), provider, model


def propose_directions(req: SkinDirectionRequest) -> dict:
    """POST /api/skins/direction: {"directions": [...], "provider", "model"}.
    SkinGenError — отказ (статус + текст для пользователя)."""
    if req.locale not in ("ru", "en"):
        raise SkinGenError(422, f"Неизвестная локаль: {req.locale}")
    if req.model and not _MODEL_RE.match(req.model):
        raise SkinGenError(422, "Недопустимое имя модели")
    try:
        dirs, answered, provider, model = run_directions(req)
    except SkinGenError:
        raise
    except Exception:
        # Текст исключения клиента LLM может содержать фрагменты ключа — только в лог
        logger.exception("[skin-gen] ошибка подбора направлений")
        raise SkinGenError(502, "Не удалось получить ответ нейросети. Попробуйте ещё раз.")
    if not answered:
        raise SkinGenError(502, "Ни один провайдер не ответил — проверьте ключи и сеть.")
    if not dirs:
        raise SkinGenError(502, "Нейросеть не вернула направления в нужном формате — попробуйте ещё раз или другую модель.")
    return {"directions": dirs, "provider": provider, "model": model}


def run_llm(req: SkinGenRequest, messages: list[dict], on_token, on_status=None,
            is_cancelled=None) -> tuple[str | None, str | None, str | None]:
    """Синхронный вызов (поток пула): (текст, провайдер, модель); текст None —
    ни один провайдер не ответил.

    Ответ, оборванный лимитом вывода (документ без </html>), дописывается
    запросами-продолжениями (до MAX_CONTINUATIONS): переписка + уже
    написанное как ответ ассистента + CONTINUE_PROMPT, у того же провайдера,
    что начал файл; куски склеиваются stitch. on_status(status, **поля) —
    служебные события для SSE: "retry_small_limit", "continue" (round=k)."""
    router, force = _router_for(req.provider, req.model)
    streamed = {"n": 0}

    def _tok(delta: str):
        streamed["n"] += len(delta)
        on_token(delta)

    def _cancelled() -> bool:
        return bool(is_cancelled and is_cancelled())

    def _call(msgs: list[dict], max_tokens: int, provider: str | None):
        return router.get_response_stream(
            msgs, _tok, temperature=TEMPERATURE, max_tokens=max_tokens,
            timeout=LLM_TIMEOUT_SEC, force_provider=provider,
            # Разовый канал веб-чата: без фоновой очереди и чужого контекста
            webchat_channel="side", user_path=True,
        )

    def _call_with_fallback(msgs: list[dict], provider: str | None) -> str | None:
        nonlocal limit
        before = streamed["n"]
        text = _call(msgs, limit, provider)
        if (not text and streamed["n"] == before and limit != FALLBACK_MAX_TOKENS
                and not _cancelled()):
            # Провайдеры с потолком вывода ниже MAX_TOKENS отвечают 400 на сам
            # запрос — второй заход с лимитом, который принимают все
            if on_status:
                on_status("retry_small_limit")
            limit = FALLBACK_MAX_TOKENS
            text = _call(msgs, limit, provider)
        return text

    limit = MAX_TOKENS
    text = _call_with_fallback(messages, force)
    provider, model = _answered_by(router)

    # Продолжения — у того, кто начал файл: другой провайдер дописал бы
    # чужой документ в своём стиле (не ответит — router уйдёт в цепочку)
    cont_provider = provider or force
    rounds = 0
    while (text and extract_html(text)[0] is not None and not is_complete(text)
           and rounds < MAX_CONTINUATIONS and not _cancelled()):
        rounds += 1
        if on_status:
            on_status("continue", round=rounds)
        msgs = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": CONTINUE_PROMPT},
        ]
        chunk = _call_with_fallback(msgs, cont_provider)
        if not chunk or _cancelled():
            break
        text = stitch(text, chunk)
    if rounds:
        logger.info(f"[skin-gen] продолжений: {rounds}, документ "
                    f"{'закрыт' if is_complete(text) else 'так и не закрыт'}")
    return text, provider, model


def prepare(req: SkinGenRequest, direction: dict | None = None) -> tuple[list[dict], list[str]]:
    """Проверка запроса и промпт: (messages, ассеты). SkinGenError — отказ.
    direction — уже нормализованное направление вместо req.direction
    (его подобрал сам сервер, см. generate_events)."""
    if req.screen not in SCREENS:
        raise SkinGenError(422, f"Неизвестный экран: {req.screen}")
    if req.locale not in ("ru", "en"):
        raise SkinGenError(422, f"Неизвестная локаль: {req.locale}")
    if req.base_kind not in ("template", "skin"):
        raise SkinGenError(422, f"Неизвестный тип базы: {req.base_kind}")
    if req.model and not _MODEL_RE.match(req.model):
        raise SkinGenError(422, "Недопустимое имя модели")
    for e in req.errors:
        if len(e) > MAX_ERROR_LEN:
            raise SkinGenError(422, f"Слишком длинное описание ошибки (больше {MAX_ERROR_LEN} символов)")
    for label, html in (("базовый файл", req.base_html), ("прошлый ответ", req.previous_html)):
        if html is not None and len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise SkinGenError(413, f"{label} больше 3 МБ")

    if direction is None and req.direction is not None:
        direction = normalize_direction(req.direction)
        if direction is None:
            raise SkinGenError(422, "Некорректное арт-направление: нужны name, subject, цвета background/ink/accent и шрифт")

    assets: list[str] = []
    base = strip_assets(strip_base(req.base_html, req.base_kind), assets)
    prev = strip_assets(req.previous_html, assets) if req.previous_html is not None else None
    sent = prev if prev is not None else base
    if len(sent) > MAX_HTML_FOR_LLM:
        raise SkinGenError(
            413,
            f"Файл для нейросети слишком большой ({len(sent) // 1024} КБ без inline-ассетов, "
            f"лимит {MAX_HTML_FOR_LLM // 1024} КБ) — возьмите за основу шаблон экрана",
        )
    return build_messages(req, base, prev, direction), assets


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


async def generate_events(req: SkinGenRequest, messages: list[dict], assets: list[str]):
    """SSE-генератор генерации (см. шапку модуля). Слот генерации берётся
    здесь, а не в эндпоинте: если клиент ушёл до старта стрима, генератор
    не запустится и слот не повиснет."""
    loop = asyncio.get_running_loop()
    if not _busy.acquire(blocking=False):
        got = await asyncio.to_thread(_busy.acquire, True, BUSY_WAIT_SEC)
        if not got:
            yield _sse({"error": "Уже идёт другая генерация скина — дождитесь её или отмените.", "code": "busy"})
            return

    cancel = threading.Event()
    progress = {"chars": 0}
    status: list[dict] = []

    def on_token(delta: str):
        if cancel.is_set():
            raise _Cancelled()
        progress["chars"] += len(delta)

    def on_status(name: str, **fields):
        status.append({"status": name, **fields})

    def with_direction() -> list[dict]:
        # «Сразу генерировать»: направления нет — стадия направления всё равно
        # идёт, здесь, одним вариантом (тот же провайдер/модель, что у
        # генерации); клиент берёт его для остальных экранов. Любая неудача
        # стадии — только событие direction_failed: генерация идёт по одному
        # брифу (модель выведет направление сама). Ошибку роутера (нет
        # провайдеров, провайдер недоступен) сообщит сама генерация
        on_status("direction_start")
        dreq = SkinDirectionRequest(description=req.description, locale=req.locale, count=1,
                                    provider=req.provider, model=req.model)
        msgs, direction = messages, None
        try:
            dirs, *_ = run_directions(dreq, cancel.is_set)
            if dirs and not cancel.is_set():
                direction = dirs[0]
                msgs = prepare(req, direction)[0]
        except (SkinGenError, _Cancelled) as e:
            logger.info(f"[skin-gen] направление перед генерацией не подобрано: {type(e).__name__}")
            msgs, direction = messages, None
        except Exception:
            # Текст исключения клиента LLM — только в лог
            logger.exception("[skin-gen] ошибка подбора направления перед генерацией")
            msgs, direction = messages, None
        if direction is None:
            on_status("direction_failed")
        else:
            on_status("direction", direction=direction)
        return msgs

    def job():
        # Слот освобождает поток: он живёт дольше отменённого запроса
        try:
            msgs = messages
            # Только первая генерация экрана без направления; исправление
            # (errors / прошлый файл) — никогда: у него направление клиента
            if req.direction is None and not req.errors and req.previous_html is None:
                msgs = with_direction()
                if cancel.is_set():
                    # Клиент ушёл во время стадии направления — генерацию не начинать
                    return None, None, None
            return run_llm(req, msgs, on_token, on_status, cancel.is_set)
        finally:
            _busy.release()

    started = time.monotonic()
    # run_in_executor отправляет задачу в пул сразу (в отличие от to_thread
    # внутри задачи, которую можно отменить до старта — слот бы не освободился)
    fut = loop.run_in_executor(None, job)
    try:
        yield _sse({"status": "started"})
        sent_status = 0
        while True:
            done, _ = await asyncio.wait({fut}, timeout=PROGRESS_INTERVAL_SEC)
            while sent_status < len(status):
                yield _sse(status[sent_status])
                sent_status += 1
            if done:
                break
            yield _sse({"progress": progress["chars"], "elapsed": round(time.monotonic() - started)})

        try:
            text, provider, model = fut.result()
        except SkinGenError as e:
            yield _sse({"error": e.detail})
            return
        except Exception:
            # Текст исключения клиента LLM может содержать фрагменты ключа — только в лог
            logger.exception("[skin-gen] ошибка генерации")
            yield _sse({"error": "Не удалось получить ответ нейросети. Попробуйте ещё раз."})
            return
        if not text:
            yield _sse({"error": "Ни один провайдер не ответил — проверьте ключи и сеть."})
            return
        html, truncated = extract_html(text)
        if html is None:
            yield _sse({
                "error": "В ответе нейросети нет HTML-документа.",
                "code": "no_html",
                "raw": text[:500],
                "provider": provider,
                "model": model,
            })
            return
        html = restore_assets(html, assets)
        yield _sse({
            "done": True,
            "html": html,
            "truncated": truncated,
            "chars": len(text),
            "provider": provider,
            "model": model,
            "elapsed": round(time.monotonic() - started),
        })
    finally:
        # Клиент ушёл (или всё закончилось) — следующий токен оборвёт стрим
        cancel.set()
