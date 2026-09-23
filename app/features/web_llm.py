"""Веб-чат LLM как провайдер без API-ключей («webchat»).

Промпт уходит в чат (deepseek/qwen/claude/zai/chatgpt/kimi/google-ai-mode)
в браузере пользователя (Chromium — через CDP-механику computer_control; Safari на
macOS — через Apple Events: служебная вкладка видимая, Enter — System
Events): ОДИН постоянный чат на
сайт НА КАНАЛ НА КОНТЕКСТ (персону) —
после первого сообщения сайт переводит вкладку на постоянный URL чата,
он запоминается в состоянии и переиспользуется (свежий чат открывается,
только если сохранённый сломался: удалён/разлогинен). Каналы: у основных
ответов ("main") и побочных задач вроде LTM ("side") — РАЗНЫЕ чаты и
раздельные квоты, чтобы фоновые задачи не замусоривали контекст беседы.
Стейтless-канал "cc" (_STATELESS_CHANNELS): внутренние вызовы
computer_control (разбор команд, резолв элементов) — каждый вызов идёт в
СВЕЖИЙ чат, адрес не запоминается: системным вызовам нужен только голый
промпт, а история прошлых команд в треде смещала бы ответы (кейс 18.09).
Фоновые каналы (side/proactive) идут через очередь СВОЕГО сайта (один
фоновый вызов на сайт за раз) и общий небольшой семафор процесса (нагрузка
на Chrome), ожидание очереди — короткое, занятость = переход к следующему
провайдеру (см. «Очереди фоновых каналов» ниже); основные ответы
main/vision в очереди не стоят.
Контексты: состояние лежит в data/{context}/computer_control, поэтому у
каждой персоны свой чат — иначе все персоны писали бы в один разговор
и модель видела бы чужие реплики (утечка памяти между персонами).
Быстрый ввод fill() (посимвольный набор для системного промпта занял бы
минуты), Enter, опрос DOM до конца стриминга (ждём НОВЫЙ блок ответа
относительно счётчика до отправки + текст стабилен несколько замеров
подряд). Лимитов по умолчанию нет: пейсинг убран, оконная квота снята
(при желании включается на персону через llm.webchat_limits).
Любая неудача — None: роутер идёт по фолбэк-цепочке дальше (local/API).

Непрерывный чат — осознанное решение: веб-чат копит контекст беседы (это
второй, неконтролируемый слой памяти рядом с STM/LTM бота), зато не
плодятся сотни чатов-однодневок и модель видит недавние реплики. Сброс —
удалить chat_url из web_llm_state.json (или сам чат на сайте); при полной
очистке истории персоны (/api/chat/clear) адреса сбрасываются автоматически
(в снапшот корзины кладутся — undo их возвращает).

Ограничения честно: ToS веб-чатов автоматизацию не приветствует (риск
флага аккаунта — на пользователе; смягчается опциональной квотой
webchat_limits, не устраняется), селекторы
адаптеров могут сломаться редизайном — smoke-ping ловит это заранее.
"""

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from app.core import timeutil
from app.core.atomic_io import atomic_write_json, file_lock, load_json_safe
from app.core.thread_local_attr import ThreadLocalAttr

logger = logging.getLogger(__name__)

QUOTA_PER_HOUR = None       # дефолтный потолок вызовов (на сайт+канал); None — без лимита
QUOTA_WINDOW_HOURS = 1      # окно квоты: ёмкость = per_hour × окно, затем полный сброс
ANSWER_TIMEOUT_SEC = 150.0  # максимум ожидания ответа (длинные дневники!)
POLL_SEC = 3.5              # шаг опроса ответа (было 2.5: реже тикаем — меньше CPU)
STABLE_POLLS = 2            # столько одинаковых непустых замеров подряд = конец стриминга
BANNER_PROBE_EVERY = 4      # баннер-пробник (скан всей страницы) — раз в N тиков
FRESH_CHAT_SETTLE_SEC = 2.0  # пауза после навигации на home (новый чат)
SEND_VERIFY_SEC = 8.0        # сколько ждём появления своего сообщения в ленте

# Тексты-заглушки во время «думания» — ответом не считаются
_THINKING_NOISE = {"", "thinking…", "thinking", "thinking completed",
                   "думаю…", "думаю", "reasoning…",
                   # google AI Mode: плейсхолдеры до начала рендера ответа
                   "ai mode is thinking about your query",
                   "transcribing...", "transcribing…"}


class _ChatBroken(Exception):
    """Сайт вернул ошибку вместо ответа (битый/удалённый чат, разлогин)."""


class _ChatRefused(_ChatBroken):
    """Сайт ОТКАЗАЛ в обслуживании (перегрузка/лимит тарифа, откат отправки):
    чат ни при чём — его адрес НЕ сбрасываем, в карантин уходит сам сайт."""


class _ChatRateLimited(_ChatBroken):
    """Сайт сообщил об исчерпании лимита сообщений free-тарифа (claude/
    chatgpt: «out of free messages», «message limit … resets at …»).
    Чат не битый; сайт уходит в карантин на распарсенное время
    восстановления (ttl=None — время не указано, дефолт карантина)."""

    def __init__(self, reason: str, ttl: float = None):
        super().__init__(reason)
        self.ttl = ttl


class _TooManyImages(_ChatBroken):
    """Сайт отверг сообщение из-за числа картинок («too many images»,
    «only N images allowed»). Не глотается в _get_response_locked —
    пробрасывается в get_response_with_image: та решает по image_overflow
    адаптера (trim — повторить с меньшим числом кадров / followup —
    доотправить остальные вторым сообщением после первого ответа)."""


class _ChatChallenged(Exception):
    """Страница чата под антибот-челленджем (Cloudflare и т.п.)."""


class _TabLost(Exception):
    """Фоновая вкладка вызова исчезла из реестра browser_actions посреди
    вызова (перезапуск Chrome пула, _reset_raw_pool, закрытие извне). Ждать
    ответ в ней бессмысленно: каждый тик опроса падает, а _wait_answer глушит
    исключения тиков — раньше это давало до 150 с «ожидания» мёртвой вкладки,
    после чего фолбэк стартовал слишком поздно. Не _ChatBroken: чат на сайте
    цел, его адрес не сбрасываем и сайт не карантиним."""


# ── Карантин сайтов (антибот-челленджи) ──
# Сайт с капчей уходит в карантин: fallback-цепочка пропускает его мгновенно
# (без тяжёлых попыток через wedged-страницу), пользователь получает
# уведомление (alert забирает bot_instance и доносит со следующим ответом).
# Карантин истекает по TTL — следующий вызов проверяет сайт заново
# (self-healing без фонового прогрева вкладок).
QUARANTINE_TTL_SEC = 1800.0
RATE_LIMIT_DEFAULT_TTL_SEC = 3600.0  # сайт не назвал время восстановления
_QUARANTINE_LOCK = threading.Lock()
_SITE_QUARANTINE: Dict[str, dict] = {}  # site → {"until": ts, "reason": str, "kind": str}
_PENDING_ALERTS: List[dict] = []        # {"site","reason","kind","until","ts"} для пользователя


def _parse_reset_ttl(text: str) -> float | None:
    """TTL карантина из текста о лимите: «через N часов/минут», «in N
    hours/minutes», «resets at 3:00 PM», «восстановится в 18:00».
    None — время не распарсено (caller берёт RATE_LIMIT_DEFAULT_TTL_SEC)."""
    t = " ".join(str(text or "").lower().split())
    m = re.search(r"(?:через|in|after)\s+(\d+)\s*(час|часа|часов|hours?|hrs?)\b", t)
    if m:
        return float(m.group(1)) * 3600
    m = re.search(r"(?:через|in|after)\s+(\d+)\s*(минут[аы]?|минут|minutes?|mins?)\b", t)
    if m:
        return float(m.group(1)) * 60
    if re.search(r"resets?|восстановится|обновится|сбросится", t):
        m = re.search(r"(?:at|в)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t)
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2) or 0)
            ampm = m.group(3)
            if ampm == "pm" and hh < 12:
                hh += 12
            elif ampm == "am" and hh == 12:
                hh = 0
            if 0 <= hh < 24 and 0 <= mm < 60:
                # Время с сайта («resets at 3:00 PM») — часы сайта, то есть
                # часы ПОЛЬЗОВАТЕЛЯ (сайт открыт в его браузере), не системные
                # часы машины бота: timeutil.now() — часовой пояс из TIMEZONE
                # (задача №7 аудита), epoch считаем только через to_ts (не
                # datetime.timestamp() у naive-значения — тот берёт системный
                # пояс и молча даёт сдвиг при несовпадении с TIMEZONE).
                target_dt = timeutil.now().replace(hour=hh, minute=mm,
                                                    second=0, microsecond=0)
                target = timeutil.to_ts(target_dt)
                if target <= time.time():
                    target += 86400  # время уже прошло сегодня — значит, завтра
                return target - time.time()
    return None

# ── Очереди фоновых каналов ──
# Фоновые каналы (side/proactive): извлечение фактов, досье, дневник, мир,
# инициативы, ритм. Раньше их сериализовал ОДИН процессный лок на все сайты
# и персоны (с ожиданием до 600 с на каждый сайт цепочки): «браузер один,
# параллельные побочные вызовы ломают циклы друг друга (ввод/чтение DOM в
# чужой вкладке)». Корень той поломки — выдача id фоновых вкладок
# «max(_RAW_TABS)+1» (два открытия получали один id, промпт уходил в чужую
# вкладку) — уже устранён монотонным счётчиком в browser_actions, а main/
# vision всё это время шли параллельно с фоном в соседних вкладках. От
# глобального лока остались две настоящие причины, их и сохраняем:
#  1) один сайт — один аккаунт и, у одной персоны, один и тот же side-чат,
#     который держат ДВА инстанса (роутер бота и роутер LTM в memory.py —
#     у каждого свой WebChatLLM с собственным локом, адрес чата общий через
#     web_llm_state.json): два параллельных вызова печатали бы в один тред.
#     Поэтому фон сериализуется ПО САЙТУ (_bg_site_queue);
#  2) нагрузка на Chrome пула H (headless, nice 10: каждая генерирующая SPA
#     ест CPU рендерера, а все CDP-операции пула идут через один сокет на
#     пул — лишний параллельный фон удлиняет тики опроса ОСНОВНОГО ответа и
#     растит таймауты, эскалирующие в перезапуск). Поэтому поверх очередей
#     сайтов — небольшой общий семафор BG_MAX_PARALLEL.
# Ожидание очереди короткое: занятость — повод идти к следующему провайдеру
# цепочки (роутер передаёт бюджет через lock_timeout), а не стоять минутами.
# Когда вся цепочка упёрлась в занятость, роутер делает второй заход с
# длинным бюджетом BG_GATE_TIMEOUT_SEC (фоновая работа не теряется).
_BACKGROUND_CHANNELS = ("side", "proactive")

# Каналы БЕЗ памяти: каждый вызов — свежий чат на сайте, адрес чата не
# запоминается, тред не копит историю. «cc» — внутренние вызовы
# computer_control (разбор команд, резолв элементов страницы) и живые
# flavor-реплики; «cc_gen» — долгая фоновая генерация flavor-банка
# (отдельный канал = отдельный инстанс/лок: генерация не блокирует
# живые реплики — кейс 18.09, ответ пользователю ждал генерацию 2.5 мин).
# «burst» — разовый свежий чат для ответа пользователю, когда постоянный
# main-чат занят другой генерацией (контекст не теряется: _join_messages и
# так шлёт его целиком каждый вызов). Burst НЕ в _NO_TIMEOUT_FLOOR_CHANNELS —
# это настоящий ответ, ему нужен пол таймаута 150с.
# Прочие каналы (main/side/proactive/vision) — как прежде, постоянные чаты.
# «search» — веб-поиск через AI Mode (app/features/web_search_race.py): голый
# вопрос пользователя в свежий тред на каждый вызов — диалоговый тред Google
# иначе тащил бы контекст прошлого (в т.ч. брошенного) поиска.
# «inline» — побочный (side-формата) вызов прямо НА ПУТИ ОТВЕТА пользователю
# (router.get_response(user_path=True): кореференция книжного поиска,
# разбор ответа на тест курса…). Разовый свежий чат, а не постоянный side:
#  - фоновую очередь сайта и семафор фона не делит — его ждёт человек;
#  - ожидание ответа ограничено таймаутом вызывающего (без пола 150 с):
#    пол нужен постоянному чату, чтобы не оставить его посреди генерации
#    (следующая отправка упрётся в идущий ответ), а разовый чат следующий
#    вызов всё равно открывает заново — брошенная генерация никому не
#    мешает;
#  - промпты самодостаточны, история side-треда им вредна (праймит формат).
USER_PATH_CHANNEL = "inline"
_STATELESS_CHANNELS = frozenset({"cc", "cc_gen", "burst", "probe", "search",
                                 USER_PATH_CHANNEL})

# Каналы коротких «системных» вызовов на пользовательском пути: пол
# таймаута ANSWER_TIMEOUT_SEC (150с для длинных дневников) к ним не
# применяется — реплика «открыл сайт» не может ждать 2.5 минуты
_NO_TIMEOUT_FLOOR_CHANNELS = frozenset({"cc", "cc_gen", "search",
                                        USER_PATH_CHANNEL})
# Каналы на критическом пути ответа, где секунды на счету: частый опрос
# (POLL_SEC=3.5 давал до 3.5 с запаздывания на КАЖДОМ готовом ответе) и
# завершение по маркеру готовности сайта, как у main/vision
_FAST_POLL_CHANNELS = frozenset({"search", USER_PATH_CHANNEL})
FAST_POLL_SEC = 0.8
# Очередь фона на сайт (см. выше): lazily по ключу сайта
_BG_SITE_QUEUES: Dict[str, threading.Lock] = {}
_BG_SITE_QUEUES_GUARD = threading.Lock()
# Общий потолок одновременных фоновых вызовов процесса (нагрузка на Chrome):
# два — фон разных сайтов не ждёт друг друга, но и не наваливается на пул
BG_MAX_PARALLEL = 2
_BG_PARALLEL = threading.BoundedSemaphore(BG_MAX_PARALLEL)
# Короткое ожидание очереди на ПЕРВОМ заходе роутера: дольше — к следующему
# провайдеру цепочки (qwen отвечает за 8–11 с: ~2 чужих вызова в очереди)
BG_QUEUE_WAIT_SEC = 20.0
# Вызов НА ПУТИ ОТВЕТА (канал USER_PATH_CHANNEL): лок своего инстанса
# (параллельный такой же вызов персоны) ждём не дольше, чем main ждёт свой
# лок перед burst (BURST_LOCK_WAIT_SEC) — занято, значит следующий провайдер
USER_PATH_QUEUE_WAIT_SEC = 3.0
# Потолок ожидания очереди, когда занято ВСЁ (второй заход роутера) и для
# прямых вызовов без бюджета: зависший браузер не держит фон вечно.
# По истечении — None (честный фолбэк).
BG_GATE_TIMEOUT_SEC = 600.0


def _bg_site_queue(site: str) -> threading.Lock:
    with _BG_SITE_QUEUES_GUARD:
        q = _BG_SITE_QUEUES.get(site)
        if q is None:
            q = threading.Lock()
            _BG_SITE_QUEUES[site] = q
        return q


# ── Идущие вызовы по пулам: координация перезапуска Chrome ──
# _restart_stuck_browser лечит залипшую страницу перезапуском ВСЕГО Chrome
# пула — вместе с ним умирают вкладки ВСЕХ идущих вызовов, в т.ч. основного
# ответа пользователю (кейс отчёта 23.09: фоновый канал перезапускал пул H,
# вкладка идущего ответа исчезала). Перезапуск разрешён, только когда в пуле
# нет ЧУЖИХ основных (не фоновых) вызовов: фон ждать не заставляет никого,
# а ответ пользователя важнее лечения одной залипшей вкладки. Чужие фоновые
# вызовы перезапуск не блокируют — они быстро выходят по _TabLost и идут
# дальше по цепочке.
# Проверка и перезапуск атомарны относительно ВХОДА основного вызова:
# «проверили → (секунды) → убили Chrome» без этого убивал вызов, начатый в
# промежутке (ревью: закрытие залипшей вкладки само висит до 20 с). Поэтому
# проверка ставит флаг _RESTARTING[pool] под тем же локом, что и счётчик, —
# новый основной вызов при флаге ждёт конца перезапуска (с ограничением,
# INFLIGHT_RESTART_WAIT_SEC) и лишь потом входит.
# Межпроцессно (Chrome пула общий для процессов бота): основной вызов держит
# РАЗДЕЛЯЕМЫЙ flock на файле пула, перезапуск берёт ЭКСКЛЮЗИВНЫЙ без
# ожидания и держит его на ВСЁ время teardown — основной вызов другого
# процесса в это время не получит разделяемый (LOCK_NB с коротким ретраем,
# без вечного блокирования) и честно уйдёт в фолбэк. Без fcntl — только
# внутрипроцессная часть.
# Этот flock — ПОЛИТИКА («не убивать Chrome под чужим ответом»), его
# разделяемую сторону держат только основные вызовы. Фоновые вызовы и
# ленивый старт Chrome в других процессах от убийства по профилю защищает
# МЕХАНИЗМ уровнем ниже — межпроцессный лок жизненного цикла Chrome в
# browser_actions (_pool_h_lifecycle): запуск и teardown не пересекаются, а
# перезапуск не трогает Chrome, уже сменённый соседом. Порядок: этот flock
# → _RAW_LOCKS[H] → flock жизненного цикла (обратного нет).
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_COND = threading.Condition(_INFLIGHT_LOCK)
_INFLIGHT_FG: Dict[str, int] = {}  # pool -> число идущих основных вызовов
# pool -> (fh эксклюзивного flock | None, свой fh разделяемого | None)
_RESTARTING: Dict[str, tuple] = {}
# Сколько основной вызов ждёт конца чужого перезапуска пула (сек): дольше —
# вызов не начинается, роутер идёт по цепочке дальше
INFLIGHT_RESTART_WAIT_SEC = 10.0
_FLOCK_RETRY_SEC = 0.1
_INFLIGHT_DIR = Path("data")       # файлы flock (тесты подменяют на tmp)
_INFLIGHT_TLS = threading.local()  # fd разделяемого flock вызова этого потока


def _inflight_lock_path(pool: str) -> Path:
    return _INFLIGHT_DIR / f".webchat_pool_{pool}.inflight"


def _flock_retry(fh, mode: int, deadline: float) -> bool:
    """flock(mode|LOCK_NB) с коротким ретраем до deadline (monotonic):
    никакого вечного блокирования на чужом процессе."""
    import fcntl
    while True:
        try:
            fcntl.flock(fh.fileno(), mode | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(_FLOCK_RETRY_SEC)


@contextmanager
def _inflight_call(pool: str, foreground: bool,
                   wait_sec: Optional[float] = None):
    """Отметить идущий основной вызов в пуле на время его работы (см.
    выше). Отдаёт True — можно работать; False — пул перезапускается (этим
    или другим процессом) дольше wait_sec: вызов не начинать. Фоновые
    вызовы не отмечаются и не ждут (True сразу)."""
    if not foreground:
        yield True
        return
    wait = INFLIGHT_RESTART_WAIT_SEC if wait_sec is None else wait_sec
    deadline = time.monotonic() + max(0.0, wait)
    with _INFLIGHT_COND:
        while pool in _RESTARTING:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            _INFLIGHT_COND.wait(left)
        registered = pool not in _RESTARTING
        if registered:
            _INFLIGHT_FG[pool] = _INFLIGHT_FG.get(pool, 0) + 1
    if not registered:
        logger.info(f"[WebChat] пул {pool.upper()} перезапускается дольше "
                    f"{wait:.0f}с — вызов не начинаю (фолбэк)")
        yield False
        return
    fh = None
    ok = True
    prev = getattr(_INFLIGHT_TLS, "fh", None)
    try:
        try:
            import fcntl
            path = _inflight_lock_path(pool)
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a+")
            if _flock_retry(fh, fcntl.LOCK_SH, deadline):
                _INFLIGHT_TLS.fh = fh
            else:
                # Эксклюзивный держит перезапуск ДРУГОГО процесса
                fh.close()
                fh = None
                ok = False
        except Exception:
            # Нет fcntl (Windows) или ФС не дала лок — межпроцессная часть
            # недоступна, внутрипроцессный счётчик работает как прежде
            if fh is not None:
                fh.close()
            fh = None
        if not ok:
            logger.info(f"[WebChat] пул {pool.upper()} перезапускает другой "
                        "процесс бота — вызов не начинаю (фолбэк)")
        yield ok
    finally:
        if fh is not None:
            try:
                fh.close()  # закрытие fd снимает flock
            except Exception:
                pass
            _INFLIGHT_TLS.fh = prev
        with _INFLIGHT_COND:
            _INFLIGHT_FG[pool] = max(0, _INFLIGHT_FG.get(pool, 0) - 1)


def _fg_others(pool: str, own_foreground: bool) -> int:
    with _INFLIGHT_LOCK:
        return _INFLIGHT_FG.get(pool, 0) - (1 if own_foreground else 0)


def _begin_restart(pool: str, own_foreground: bool) -> Optional[str]:
    """Захватить право перезапустить Chrome пула. None — захвачено (флаг
    _RESTARTING стоит, эксклюзивный flock держится до _end_restart), иначе
    человеческая причина отказа. own_foreground — вызывающий сам идущий
    основной вызов (его счётчик и разделяемый flock не в счёт)."""
    with _INFLIGHT_LOCK:
        if pool in _RESTARTING:
            return f"пул {pool.upper()} уже перезапускается"
        others = _INFLIGHT_FG.get(pool, 0) - (1 if own_foreground else 0)
        if others > 0:
            return (f"идёт основной вызов веб-чата в пуле {pool.upper()} "
                    f"({others})")
        # Флаг ставится В ТОЙ ЖЕ критической секции, что и проверка:
        # новые основные вызовы процесса с этого момента ждут
        _RESTARTING[pool] = (None, None)
    try:
        import fcntl
    except ImportError:
        return None
    own = getattr(_INFLIGHT_TLS, "fh", None) if own_foreground else None
    fh = None
    try:
        if own is not None:
            fcntl.flock(own.fileno(), fcntl.LOCK_UN)  # свой лок не в счёт
        path = _inflight_lock_path(pool)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            with _INFLIGHT_COND:
                _RESTARTING.pop(pool, None)
                _INFLIGHT_COND.notify_all()
            if own is not None:
                _flock_retry(own, fcntl.LOCK_SH,
                             time.monotonic() + INFLIGHT_RESTART_WAIT_SEC)
            return (f"идёт основной вызов веб-чата в пуле {pool.upper()} "
                    "(другой процесс бота)")
    except OSError:
        # ФС без flock — судим по внутрипроцессному счётчику
        if fh is not None:
            fh.close()
        fh = None
    with _INFLIGHT_LOCK:
        _RESTARTING[pool] = (fh, own)
    return None


def _end_restart(pool: str) -> None:
    """Снять флаг перезапуска: отпустить эксклюзивный flock, разбудить
    ждущие основные вызовы, вернуть свой разделяемый flock."""
    with _INFLIGHT_LOCK:
        fh, own = _RESTARTING.get(pool) or (None, None)
    if fh is not None:
        try:
            fh.close()  # снимает эксклюзивный flock
        except Exception:
            pass
    with _INFLIGHT_COND:
        _RESTARTING.pop(pool, None)
        _INFLIGHT_COND.notify_all()
    if own is not None:
        try:
            import fcntl
            _flock_retry(own, fcntl.LOCK_SH,
                         time.monotonic() + INFLIGHT_RESTART_WAIT_SEC)
        except Exception:
            pass


def quarantine_site(site: str, reason: str, ttl: float = None,
                    kind: str = "challenge"):
    """Карантин сайта. ttl — секунды до снятия (None — QUARANTINE_TTL_SEC);
    kind — природа блокировки: «challenge» (антибот-капча), «ratelimit»
    (исчерпан лимит сообщений, есть время восстановления), «refused»
    (отклонение отправки/перегрузка)."""
    with _QUARANTINE_LOCK:
        already = site in _SITE_QUARANTINE
        eff_ttl = ttl if ttl and ttl > 0 else QUARANTINE_TTL_SEC
        until = time.time() + eff_ttl
        _SITE_QUARANTINE[site] = {"until": until, "reason": reason,
                                  "kind": kind}
        if not already:
            _PENDING_ALERTS.append({"site": site, "reason": reason,
                                    "kind": kind, "until": until,
                                    "ts": time.time()})
    if not already:
        logger.warning(f"[WebChat] {site}: карантин ({kind}) "
                       f"{int(eff_ttl / 60)} мин — {reason}")


def site_quarantined(site: str) -> bool:
    with _QUARANTINE_LOCK:
        q = _SITE_QUARANTINE.get(site)
        if not q:
            return False
        if time.time() >= float(q.get("until") or 0):
            _SITE_QUARANTINE.pop(site, None)
            return False
        return True


def clear_quarantine(site: str):
    with _QUARANTINE_LOCK:
        _SITE_QUARANTINE.pop(site, None)


def quarantine_status() -> dict:
    """Активные карантины {site: {until, reason}} — для API/статуса."""
    with _QUARANTINE_LOCK:
        now = time.time()
        for s in [s for s, q in _SITE_QUARANTINE.items()
                  if now >= float(q.get("until") or 0)]:
            _SITE_QUARANTINE.pop(s, None)
        return {s: dict(q) for s, q in _SITE_QUARANTINE.items()}


def pop_quarantine_alerts() -> List[dict]:
    """Забрать накопленные уведомления о карантинах (одноразово)."""
    with _QUARANTINE_LOCK:
        alerts = list(_PENDING_ALERTS)
        _PENDING_ALERTS.clear()
    return alerts


# ── Постоянные чаты контекста и очистка диалога ──
# Веб-чат — второй слой памяти рядом со STM/LTM: при полном сбросе переписки
# персоны (/api/chat/clear) адреса чатов кладутся в снапшот корзины и
# сбрасываются — следующий вызов откроет НОВЫЙ чат. Undo возвращает адреса.

def _state_file(context: str) -> Path:
    return Path(f"data/{context}/computer_control/web_llm_state.json")


# ── Лок файла состояния: процессный + межпроцессный ──
# web_llm_state.json один на контекст (персону) — в него пишут ВСЕ каналы
# сайта (main/side/vision/burst/…) И, отдельно, все ModelRouter контекста
# (у бота свой, у памяти для LTM — свой, см. memory.py): у каждого — свой
# WebChatLLM с собственным threading.Lock. Тот лок сериализует только
# вызовы ОДНОГО инстанса и никак не защищает read-modify-write файла от
# ДРУГОГО инстанса того же контекста: A прочитал весь st (со свежим
# chat_url канала B), B параллельно дописал свой ключ и сохранил, A
# сохраняет СВОЙ ключ поверх устаревшего снимка — ключ B затёрт целиком
# (lost update: пропадал chat_url или счётчик квоты). Внутрипроцессный
# threading.Lock берётся по пути файла — общий для всех инстансов/каналов/
# роутеров ОДНОГО процесса, что на этот файл пишут.
#
# Хвост задачи №7 аудита: этого мало, если персон/каналов несколько
# независимых ПРОЦЕССОВ (не потоков одного) пишут в общий data/ —
# threading.Lock другого процесса не виден. Поверх него в _update_state
# берётся ещё и межпроцессный atomic_io.file_lock (fcntl/msvcrt) на тот же
# путь. threading.Lock при этом не убирается: fcntl.flock на файл,
# открытый дважды ИЗ ОДНОГО процесса, сам с собой не конфликтует (ядро
# отдаёт лок повторно тому же процессу) — без threading.Lock потоки одного
# процесса по-прежнему гонялись бы друг с другом внутри file_lock.
_STATE_FILE_LOCKS: Dict[str, threading.Lock] = {}
_STATE_FILE_LOCKS_GUARD = threading.Lock()


def _state_file_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _STATE_FILE_LOCKS_GUARD:
        lock = _STATE_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _STATE_FILE_LOCKS[key] = lock
        return lock


def _update_state(path: Path, mutate) -> dict:
    """Единая точка входа для ЛЮБОГО read-modify-write файла состояния
    (весь файл, все сайты/каналы контекста разом): используется и
    точечным патчем одного сайта+канала (WebChatLLM._mutate_state), и
    «плоскими» clear_chat_urls/restore_chat_urls, которые правят сразу
    несколько ключей sites. Один helper — один лок-протокол на файл, вместо
    того чтобы каждый вызывающий код заново собирал
    file_lock+_state_file_lock+чтение+атомарную запись (и не забыл ничего).

    mutate(dict) -> dict получает свежепрочитанный (под локом) словарь
    всего файла ({} если файла нет или он битый — load_json_safe уже
    залогировал причину и увёл битый файл в .corrupt) и возвращает новый
    для записи. Запись пропускается, если mutate ничего не поменял
    (typical: clear_chat_urls на файле без единого chat_url) — так
    несуществующий файл не создаётся из ничего пустым JSON.

    Лок двойной: file_lock (межпроцессный) СНАРУЖИ, _state_file_lock
    (threading, тот же процесс) ВНУТРИ — см. комментарий выше про
    _STATE_FILE_LOCKS."""
    with file_lock(path):
        with _state_file_lock(path):
            st = load_json_safe(path, default={}, label="WebChat")
            if not isinstance(st, dict):
                st = {}
            before = json.dumps(st, sort_keys=True, ensure_ascii=False)
            new = mutate(st)
            if not isinstance(new, dict):
                new = st
            if json.dumps(new, sort_keys=True, ensure_ascii=False) != before:
                try:
                    atomic_write_json(path, new)
                except Exception as e:
                    logger.debug(f"[WebChat] состояние не записано: {e}")
            return new


def collect_chat_urls(context: str) -> Dict[str, str]:
    """Адреса постоянных чатов всех сайтов/каналов контекста
    ({state_key: url}) — для снапшота корзины очистки диалога. Чистое
    чтение (без лока — atomic_write_json/os.replace не даёт читателю
    увидеть частично записанный файл)."""
    st = load_json_safe(_state_file(context), default={}, label="WebChat")
    if not isinstance(st, dict):
        return {}
    sites = st.get("sites", {})
    return {k: str(v["chat_url"]) for k, v in sites.items()
            if isinstance(v, dict) and str(v.get("chat_url") or "").strip()}


def clear_chat_urls(context: str) -> int:
    """Сбросить постоянные чаты контекста — следующий вызов webchat откроет
    новый чат. Живые вкладки перенавигаются на home лениво при следующем
    обращении (_ensure_chat увидит, что сохранённого адреса больше нет).
    Возвращает число сброшенных адресов."""
    n = 0

    def _clear(st: dict) -> dict:
        nonlocal n
        sites = st.get("sites")
        if not isinstance(sites, dict):
            return st
        for _key, val in sites.items():
            if isinstance(val, dict) and str(val.get("chat_url") or "").strip():
                val["chat_url"] = ""
                n += 1
        return st

    _update_state(_state_file(context), _clear)
    if not n:
        return 0
    logger.info(f"[WebChat] {context}: постоянные чаты сброшены ({n}) — "
                "следующий вызов откроет новые")
    return n


def restore_chat_urls(context: str, urls: Dict[str, str]) -> int:
    """Вернуть адреса чатов из снапшота корзины (undo очистки диалога)."""
    if not urls:
        return 0

    def _restore(st: dict) -> dict:
        sites = st.setdefault("sites", {})
        for key, url in urls.items():
            cur = sites.get(key)
            if not isinstance(cur, dict):
                cur = {}
            cur["chat_url"] = str(url)
            sites[key] = cur
        return st

    try:
        _update_state(_state_file(context), _restore)
        logger.info(f"[WebChat] {context}: адреса чатов восстановлены "
                    f"из корзины ({len(urls)})")
        return len(urls)
    except Exception as e:
        logger.debug(f"[WebChat] restore chat_url ({context}) не записался: {e}")
        return 0


# Баннеры ошибок веб-чатов в ленте (qwen: «Oops! There was an issue
# connecting to … Invalid input chat parent_id … is not exist» — чат
# удалён/побит, ответа не будет). Намеренно узкие паттерны — человеческий
# ответ с «oops» в середине не задеть. Матчатся и с текстом последнего
# answer-блока, и с текстом последнего контейнера error_scope (qwen
# рендерит баннер без content-классов — виден только на контейнере).
_CHAT_ERROR_RES = (
    re.compile(r"^oops!\s*there was an issue", re.I),
    re.compile(r"parent_id\s+\S+\s+is not exist", re.I),
    # deepseek: чат упёрся в лимит длины — сайт просит начать новый;
    # баннер живёт отдельным div с хэш-классом (не в блоках ответа),
    # ловим и текстом ответа, и пробой по странице (см. ниже)
    re.compile(r"length limit reached.*start a new chat", re.I),
    # kimi: отказ бесплатному тарифу при перегрузке — «Currently available
    # to Moderato/Plus and higher-tier members…» (тост сайта, кейс 17.09)
    re.compile(r"currently available to .{0,40}members", re.I),
    re.compile(r"servers? (?:is |are )?(?:currently )?overloaded", re.I),
)

# Признак отказа по перегрузке/тарифу в тексте ошибки: такие _ChatBroken
# уходят не в сброс чата (чат не битый), а в карантин сайта
_OVERLOAD_RE = re.compile(r"currently available to|overloaded|перегруж",
                          re.I)

# Отказ от ответа на КОНКРЕТНЫЙ промпт (не битый чат и не отказ сайта):
# google AI Mode «It looks like there's no response available for this
# search. Try asking something else.» — поисковый фильтр контента. Такое
# ждать бессмысленно, но и ретраить тем же промптом на новом чате нечего —
# быстрый None в фолбэк, чат и сайт не трогаем
_NO_ANSWER_RES = (
    re.compile(r"no response available", re.I),
    re.compile(r"try asking something else", re.I),
    # google AI Mode: промпт сверх лимита бэкенда (~>9-10 тыс. симв.) —
    # «Something went wrong and the content wasn't generated.»
    re.compile(r"content wasn't generated", re.I),
)

# Исчерпание лимита сообщений free-тарифа (актуально для claude/chatgpt):
# «You're out of free messages», «You've reached your message limit»,
# «Your message limit will reset at 3:00 PM», «лимит сообщений исчерпан».
# Отличие от _CHAT_ERROR_RES: чат не битый и сайт не в перегрузке — лимит
# временный; карантин на распарсенное время восстановления (_parse_reset_ttl).
# Заведомо широкие паттерны («message limit», «usage limit» и т.п.) — модель
# сама может произнести эти слова, обсуждая лимиты любого сервиса (тема
# разговора), поэтому проверяются они ТОЛЬКО в источниках сайта (баннер/
# тост/error_scope через _classify_site_signal), а не в тексте ответа — см.
# вызов в _wait_answer.
_RATE_LIMIT_RES = (
    re.compile(r"out of free (?:messages|chats)", re.I),
    re.compile(r"(?:you'?ve|you have) (?:reached|hit) (?:your )?"
               r"(?:\w+ )?(?:message |usage )?limit", re.I),
    re.compile(r"(?:message|usage) limit", re.I),
    re.compile(r"free (?:messages|plan) (?:are )?(?:exhausted|used up)", re.I),
    re.compile(r"лимит[а]? (?:сообщений|запросов)", re.I),
    re.compile(r"лимит исчерпан", re.I),
    re.compile(r"бесплатн\w* лимит", re.I),
    re.compile(r"(?:восстановится|обновится|сбросится) через", re.I),
    # Дневной лимит загрузок/картинок (chatgpt free: «You've reached your
    # daily maximum for image uploads» и т.п.)
    re.compile(r"daily (?:maximum|limit|cap)", re.I),
    re.compile(r"(?:image|file|attachment)s? upload limit", re.I),
    re.compile(r"дневн\w* лимит", re.I),
)

# Отказ сайта принимать столько картинок за одно сообщение. Паттерны —
# предварительные (обобщённые); уточняются дословными текстами сайтов
# после живого замера (шаг «vision: замер лимитов»).
_TOO_MANY_IMAGES_RES = (
    re.compile(r"too many (?:images?|files?|attachments?)", re.I),
    re.compile(r"only \d+ (?:images?|files?|attachments?)", re.I),
    re.compile(r"(?:maximum|max) of \d+ (?:images?|files?)", re.I),
    re.compile(r"(?:image|file|attachment) limit", re.I),
    re.compile(r"(?:can'?t|cannot|couldn'?t) (?:upload|attach|add) more", re.I),
    re.compile(r"слишком много (?:изображений|картинок|файлов)", re.I),
    re.compile(r"не более \d+ (?:изображений|картинок|файлов)", re.I),
    re.compile(r"превышен\w* (?:лимит|количество|число)\s*"
               r"(?:изображений|картинок|файлов)?", re.I),
)

# Порог «короткого баннерного блока» — тот же, что у _PAGE_BANNER_ERROR_JS
# (сканер страницы отбрасывает «листья» длиннее этого). Используется, когда
# сигнал сайта разрешено искать в тексте ОТВЕТА (см. _classify_site_signal,
# allow_answer_text): длинный накопленный текст ответом моделью НИКОГДА не
# считается баннером сайта, сколько бы «баннерных» слов в нём ни встретилось
# (кейс аудита: реальный ответ про лимиты чужого сервиса резался пополам и
# уходил в карантин из-за подстроки «message limit» в собственной прозе).
_SITE_SIGNAL_MAX_LEN = 200


def _classify_site_signal(patterns, *, scope_norm: str = "",
                          banner_norm: str = "", cur_norm: str = "",
                          allow_answer_text: bool = False) -> Optional[str]:
    """Единая точка «это сигнал САЙТА (баннер/тост/error-скоуп), а не проза
    ответа модели». По умолчанию источники — ТОЛЬКО site-owned тексты:
    scope_norm (последний видимый error_scope адаптера) и banner_norm
    (сканер отдельных коротких «листьев» страницы, _PAGE_BANNER_ERROR_JS) —
    их структура (короткий текст ВНЕ блоков ответа) сама по себе исключает
    совпадение с содержательной прозой модели.

    cur_norm — текст блока ОТВЕТА — источником сайта НЕ считается и по
    умолчанию не проверяется: пользователь вправе спросить модель про
    лимиты/ошибки любого сервиса, и «message limit»/«too many images» в её
    рассуждении — не сигнал ЭТОГО сайта.

    allow_answer_text=True — исключение для сайтов, где вёрстка кладёт
    баннер сайта ВНУТРЬ селектора ответа мимо error_scope (qwen «Oops!…»,
    deepseek «Length limit reached…» без content-классов — см. комментарии
    у _CHAT_ERROR_RES/_NO_ANSWER_RES). Даже тогда cur_norm разрешён, только
    пока он короткий (<=_SITE_SIGNAL_MAX_LEN) — целиком похож на баннер, а
    не на кусок большого содержательного ответа. Длинный/накопленный ответ
    никогда не даёт site-сигнал через этот путь."""
    for t in (scope_norm, banner_norm):
        if t and any(rx.search(t) for rx in patterns):
            return t
    if allow_answer_text and cur_norm and len(cur_norm) <= _SITE_SIGNAL_MAX_LEN \
            and any(rx.search(cur_norm) for rx in patterns):
        return cur_norm
    return None


# Текст последнего видимого контейнера error_scope (аргумент — селектор).
_ERROR_SCOPE_JS = (
    "(function(){var els=document.querySelectorAll(%s);"
    "for(var i=els.length-1;i>=0;i--){var e=els[i];"
    "var r=e.getBoundingClientRect();var st=getComputedStyle(e);"
    "if(st.display==='none'||st.visibility==='hidden'||r.width<2||r.height<2)continue;"
    "return (e.innerText||'').replace(/\\s+/g,' ').trim();}"
    "return '';})()"
)

# Баннер ошибки ОТДЕЛЬНЫМ элементом страницы (не в блоках ответа и не в
# error_scope): deepseek «Length limit reached…» — короткий видимый текст
# в div с хэш-классом; сканим видимые «листья» без привязки к вёрстке
_PAGE_BANNER_ERROR_JS = (
    "(function(){var rx=__RX__;"
    "var els=document.querySelectorAll('div,span,p');"
    "for(var i=0;i<els.length;i++){var e=els[i];"
    "if(e.children.length>2)continue;"
    "var r=e.getBoundingClientRect();var st=getComputedStyle(e);"
    "if(st.display==='none'||st.visibility==='hidden'||r.width<2||r.height<2)continue;"
    "var t=(e.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(!t||t.length>200)continue;"
    "if(rx.test(t))return t;}"
    "return '';})()"
)

# Поле композера чата: детект ОТКАТА отправки в _wait_answer — сайт вернул
# черновик в поле ввода, значит сообщение отклонено и ответа не будет
# (kimi 17.09: сервер молча отклонял отправку — resource_exhausted в
# ChatService/Chat, SPA восстанавливал черновик без какого-либо баннера)
_CHAT_FIELD_JS = (
    "(function(){var e=document.querySelector(%s);"
    "return e?(e.isContentEditable?e.innerText:e.value):'';})()"
)

# JS переключения режима чата qwen: Auto → Fast (быстрее, и в ленте нет
# блоков «Thinking completed»). Идемпотентно: режим не Auto — ничего не
# делает. evaluate в playwright ждёт промисы — возвращаем Promise.
_QWEN_FAST_MODE_JS = (
    "(function(){"
    "function vis(e){var s=getComputedStyle(e);var r=e.getBoundingClientRect();"
    "return s.display!=='none'&&s.visibility!=='hidden'&&r.width>2&&r.height>2;}"
    "var trig=document.querySelector('.qwen-thinking-selector [class*=trigger]');"
    "if(!trig)return 'no-trigger';"
    "var cur=(trig.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(!/auto/i.test(cur))return 'ok:'+cur;"
    "trig.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));trig.click();"
    "return new Promise(function(res){setTimeout(function(){"
    "var items=document.querySelectorAll('.qwen-chat-v2-dropdown-menu-item');"
    "for(var i=0;i<items.length;i++){var e=items[i];if(!vis(e))continue;"
    "var t=(e.innerText||'').replace(/\\s+/g,' ').trim();"
    "if(/fast|быстр/i.test(t)){"
    "e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));e.click();"
    "res('ok:fast');return;}}"
    "res('no-fast-item');},700);});"
    "})()"
)

# JS выбора модели deepseek (Instant/Expert/Vision): пилюли — div[role=radio]
# на странице НОВОГО чата, активная с aria-checked=true (в существующем чате
# пилюль нет — режим задаётся при создании чата). Идемпотентно: целевая уже
# активна — ничего не делает. %s — имя режима из mode_by_channel/mode_default
_DEEPSEEK_MODE_JS = (
    "(function(want){"
    "var rs=document.querySelectorAll('[role=\"radio\"]');"
    "for(var i=0;i<rs.length;i++){var r=rs[i];"
    "var t=(r.innerText||'').replace(/\\s+/g,' ').trim().toLowerCase();"
    "if(t!==String(want).toLowerCase())continue;"
    "if(r.getAttribute('aria-checked')==='true')return 'ok:'+t;"
    "r.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));r.click();"
    "return 'clicked:'+t;}"
    "return 'no-pill';})"
    "(%s)"
)

ADAPTERS = {
    "deepseek": {
        "host": "chat.deepseek.com",
        "home": "https://chat.deepseek.com/",
        "input": "textarea",
        # Цель поля ввода — фолбэк при протухшем CSS-селекторе (редизайн):
        # поле ищется по подписям в общем снапшоте, без LLM
        "input_goal": ["поле ввода сообщения", "message deepseek",
                       "send a message"],
        "answer": [".ds-assistant-message-main-content",
                   ".ds-markdown"],
        # Своё сообщение: ds-message БЕЗ assistant-контента внутри (класса
        # «user» нет; лента виртуализована — в DOM только последний обмен,
        # поэтому подтверждение отправки текстовое, а не по счётчику).
        "user": [".ds-message:not(:has(.ds-assistant-message-main-content))"],
        # Модель чата задаётся пилюлями Instant/Expert/Vision при СОЗДАНИИ
        # чата (в существующем их нет): vision-канал (картинки) — Vision,
        # текстовые каналы — Instant. Подстановка в mode_js через %s
        "mode_js": _DEEPSEEK_MODE_JS,
        "mode_by_channel": {"vision": "Vision"},
        "mode_default": "Instant",
        # Сайт принимает картинки вставкой (проверено: img[src^=blob:]
        # в композере): vision-запросы идут в отдельный чат в режиме Vision.
        # Лимит сайта — до ~50 файлов за запрос: два кадра глубоко внутри
        "images": True,
        # Кадров за сообщение: основной + чистый скриншот без разметки
        "max_images": 2,
    },
    "qwen": {
        "host": "chat.qwen.ai",
        "home": "https://chat.qwen.ai/",
        "input": "textarea.message-input-textarea",
        "input_goal": ["поле ввода сообщения", "send a message",
                       "ask anything"],
        "answer": [".qwen-chat-message-assistant .response-message-content.phase-answer",
                   ".qwen-chat-message-assistant .chat-response-message"],
        "user": [".qwen-chat-message-user"],
        # Баннер ошибки («Oops! … parent_id … is not exist») рендерится
        # ВНУТРИ assistant-контейнера, но БЕЗ content-классов — селекторы
        # answer его не видят, поэтому текст ошибки читаем с последнего
        # контейнера целиком (см. _wait_answer).
        "error_scope": ".qwen-chat-message-assistant",
        "mode_js": _QWEN_FAST_MODE_JS,
        # Сайт принимает картинки вставкой (проверено Cmd+V вручную):
        # включает vision-фолбэк через веб-чат (chat_paste_image). Лимит
        # сайта — несколько картинок за сообщение (20 МБ/кадр): два внутри
        "images": True,
        # Кадров за сообщение: основной + чистый скриншот без разметки
        "max_images": 2,
    },
    "claude": {
        "host": "claude.ai",
        "home": "https://claude.ai/new",
        "input": "div[contenteditable=true]",
        "input_goal": ["поле ввода сообщения", "reply to claude",
                       "message"],
        # Редизайн 08.2026: .font-claude-response-body — теперь класс каждого
        # абзаца <p> внутри ответа, а не контейнер всего сообщения. Читатели
        # берут последний совпавший блок — до пользователя доходил один
        # последний абзац (кейс: ответ по лору схлопнулся в «Вы хорошо
        # отдохнули сегодня?»). Контейнер всего ответа — div.font-claude-response
        # (один на сообщение ассистента); старый селектор — фолбэк.
        # Внутри контейнера лежит пилюля мышления («Thought for 6s»): видимый
        # текст — кнопка (md() её пропускает), но рядом скрытый span.sr-only
        # для скринридеров — его текст утекал в ответ. Срезаем все .sr-only.
        "answer": ["div.font-claude-response",
                   ".font-claude-response-body"],
        "answer_exclude": ".sr-only",
        # Метки инструментов в начале ответа (замер 23.09): «Pondering»
        # (индикатор размышления), «Updated memorycodewords.md», «Recalled
        # memory…», «Read and updated memory» — уходили в реплику персоны
        "answer_strip": [
            # не трогаем «Pondering over…» — метка только слитно/отдельной строкой
            r"^\s*(?:Pondering(?:…|\.\.\.)?)+(?![ \t]+[a-z])\s*",
            r"^\s*(?:(?:Read and updated|Updated|Recalled|Read|Searched|Viewed"
            r"|Saved|Checked) memory)\S*\s*",
        ],
        "user": ["[data-testid=user-message]"],
        # Картинки: сайт умеет attach; синтетический paste ПОКА не подтверждён
        # живым замером — при неудаче вставки вызов честно уходит в фолбэк
        # (chat_paste_image False → None). Лимит сайта — до 20 файлов/картинок
        # за чат (офиц. справка, 07.2026), двух кадров заведомо внутри; у free
        # ~20-30 сообщений с вложениями в день — исчерпание ловится
        # _RATE_LIMIT_RES (карантин с TTL).
        "images": True,
        "max_images": 2,
    },
    "zai": {
        "host": "chat.z.ai",
        "home": "https://chat.z.ai/",
        "input": "textarea#chat-input",
        "input_goal": ["поле ввода сообщения", "send a message",
                       "ask anything"],
        # Контент ответа — внутренний .markdown-prose в #response-content-container;
        # снаружи лежит .thinking-chain-container («Thought Process») — вырезаем
        # exclude'ом: md() не смотрит на видимость, свёрнутая цепочка иначе
        # попадала бы в текст.
        "answer": [".chat-assistant #response-content-container > .markdown-prose",
                   ".chat-assistant.markdown-prose"],
        "answer_exclude": ".thinking-chain-container",
        "user": [".user-message"],
    },
    "chatgpt": {
        "host": "chatgpt.com",
        "home": "https://chatgpt.com/",
        "input": "#prompt-textarea",
        "input_goal": ["поле ввода сообщения", "ask anything", "message"],
        "answer": ["[data-message-author-role=assistant] .markdown",
                   "[data-message-author-role=assistant]"],
        "user": ["[data-message-author-role=user]"],
        # Картинки: сайт умеет attach; синтетический paste ПОКА не подтверждён
        # живым замером — при неудаче вставки вызов честно уходит в фолбэк
        # (chat_paste_image False → None). За сообщение можно больше двух,
        # но у free-тарифа ДНЕВНОЙ лимит ~3 файла/картинки в сутки — как
        # vision-фолбэк chatgpt почти одноразовый, дневной отказ ловится
        # _RATE_LIMIT_RES (карантин с TTL).
        "images": True,
        "max_images": 2,
    },
    "kimi": {
        "host": "kimi.ai",
        "home": "https://www.kimi.ai/",
        "input": "div.chat-input-editor",
        "input_goal": ["поле ввода сообщения", "send a message",
                       "ask anything"],
        # Ответ — прямой markdown-контейнер content-box; цепочка «Think»
        # лежит в .thinking-container (не direct child) — основной селектор
        # её не видит, exclude — страховка на случай сдвига вёрстки.
        # Фолбэк-селектор :not(:has(.user-content)) — чтобы не зацепить
        # box user-сообщения (у него та же обёртка, но без markdown-container).
        "answer": [".segment-content-box > .markdown-container",
                   ".segment-content-box:not(:has(.user-content))"],
        "answer_exclude": ".thinking-container",
        # Ряд кнопок действий (copy/…) появляется только у ЗАВЕРШЁННОГО
        # ответа — гейт против преждевременного «стабильного» плейсхолдера
        # при сбойной/оборванной генерации (кейс: «High demand…» → пусто).
        "done_selector": ".segment-assistant-actions",
        "user": [".user-content"],
    },
    "google": {
        # AI Mode google.com/search?udm=50 (17.09): чат-тред живёт на той же
        # /search-странице, follow-up — тем же полем внизу треда.
        "host": "www.google.com",
        "home": "https://www.google.com/search?udm=50",
        "input": "textarea.ITIRGe",
        "input_goal": ["поле ввода сообщения", "ask anything",
                       "ask a question"],
        # Обмен = контейнер .tonYlb (1 на реплику): вопрос — её первый
        # (бесклассовый) div. Ответ — .pWvJNd внутри .CKgc1d (замер 19.09;
        # прежний селектор .n6owBd.awi2gc сгнил при ротации обфусцированных
        # классов — генерация банка уходила в таймаут с готовым ответом на
        # странице). Подсказки «Would you like…» в ответ не входят. Чипы
        # источников (span.WBgIic) вырезаем exclude'ом. Маркер завершения —
        # кнопка «Copy text» в контейнере ответа (прежний
        # .segment-assistant-actions сгнул тогда же); на фоновых каналах
        # дополнительно работает стабильность текста.
        "answer": [".pWvJNd",
                   ".mZJni > div > .n6owBd",
                   ".n6owBd.awi2gc:not(.AHmQrc .n6owBd)"],
        # .DBd2Wb — подвал ответа внутри .pWvJNd (замер 23.09): тост копирования
        # «Скопировано в буфер обмена / Не удалось скопировать…», «Поделиться»,
        # «Хороший/Плохой ответ» — уходил в ответ персоне и в промпт поиска
        "answer_exclude": ".WBgIic, .DBd2Wb",
        # Страховка на ротацию обфусцированных классов: текст от первой
        # фразы подвала до конца отрезается (_cut_tail)
        "answer_cut": [r"\n\s*(?:Скопировано в буфер обмена|Copied to clipboard)"],
        # Виджет карты внутри ответа (замер 23.09, «столица франции»): его
        # подписи вырезаются где угодно в тексте, не только в хвосте
        "answer_strip": [
            r"(?:(?:Something went wrong|Что-то пошло не так)\.?\s*)?"
            r"(?:Map data|Картографические данные)[^\n]*"
            r"(?:\s*(?:Expand map|Collapse map|Развернуть карту|Свернуть карту"
            r"|Zoom in|Zoom out|Увеличить|Уменьшить)\b)*",
        ],
        "user": [".tonYlb > div:first-child"],
        # Флаг i — регистронезависимо: в русском UI кнопка «Скопировать
        # текст», а подстрока «Копир» с заглавной её не находила — маркер
        # завершения молчал, ответ снимался недописанным (замер 23.09)
        "done_selector": ".CKgc1d button[aria-label*='copy' i], "
                         ".CKgc1d button[aria-label*='копир' i]",
        # maxlength композера (замер 17.09): fill его обходит, ~9 тыс. симв.
        # уходит и отвечает, ~12 тыс. — бэкенд молча падает («content wasn't
        # generated», ловится _NO_ANSWER_RES). Свыше лимита — предупреждение.
        "max_input": 8192,
        # Идентичность чата — query-параметр mtid (mstk/csuir Google
        # перегенерирует на каждой загрузке, по ним сравнивать нельзя);
        # без url_id home и тред имеют одинаковые host+path и _same_page
        # считал бы их одной страницей — чат не открывался бы заново.
        "url_id": r"[?&]mtid=([^&]+)",
        # www.google.com — рабочая цель команд пользователя («открой гугл»),
        # глушить хост как служебный нельзя; вкладка AI Mode живёт в пуле H,
        # куда команды не целятся.
        "service_host": False,
        # Вопрос прямо адресом: навигация на него открывает НОВЫЙ тред и сама
        # запускает ответ (без ввода/Enter/проверки отправки) — путь канала
        # search (WebChatLLM.ask_by_url). {q} — url-encoded вопрос
        "query_url": "https://www.google.com/search?udm=50&hl=ru&q={q}",
        # AI Mode принимает ОДНУ картинку за сообщение; синтетический paste
        # ПОКА не подтверждён живым замером — при неудаче вызов честно
        # уходит в фолбэк (chat_paste_image False → None)
        "images": True,
        "max_images": 1,
    },
}


def extract_json(text: str):
    """Терпимый разбор ответа веб-чата как JSON: срез ```json-ограждений,
    затем первая сбалансированная {…}/[…] конструкция. None — не JSON."""
    t = str(text or "").strip()
    if not t:
        return None
    m = re.search(r"```(?:json)?\s*(.+?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        if start < 0:
            continue
        depth = 0
        for i in range(start, len(t)):
            ch = t[i]
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except ValueError:
                        break
    try:
        return json.loads(t)
    except ValueError:
        return None


class WebChatLLM:
    """LLM через веб-чат в браузере пользователя. site — ключ ADAPTERS."""

    # Признаки ПОСЛЕДНЕГО вызова — на поток: инстанс сайта+канала один на
    # роутер персоны, а вызывают его параллельно (LTM и досье делят side).
    # Обычным атрибутом флаг одного потока перетирался другим между «вызов
    # вернул None» и «роутер прочитал флаг» — ложный burst или его пропуск.
    # Промах по очереди/локу (занято другой генерацией): main → burst-чат,
    # фон → переход к следующему провайдеру и второй заход роутера
    last_call_lock_miss = ThreadLocalAttr(default=False, shared_fallback=False)
    # Вкладка вызова исчезла (перезапуск Chrome и т.п.): main → burst-чат
    last_call_tab_lost = ThreadLocalAttr(default=False, shared_fallback=False)

    def __init__(self, site: str, context: str = "default",
                 base_dir: Optional[Path] = None, channel: str = "main",
                 quota_per_hour: Optional[int] = QUOTA_PER_HOUR,
                 browser_pool: Optional[str] = None):
        if site not in ADAPTERS:
            raise ValueError(f"неизвестный webchat-сайт «{site}» "
                             f"(есть: {', '.join(ADAPTERS)})")
        self.site = site
        self.adapter = ADAPTERS[site]
        # Пул браузера сайта (web_extended): H — headless Chrome (постоянная
        # фоновая работа без окна), V — headed (агрессивный антибот: chatgpt,
        # claude). Роутер передаёт из llm.webchat_mode персоны; без конфига —
        # дефолт по известной агрессивности сайта
        if browser_pool in ("h", "v"):
            self.browser_pool = browser_pool
        else:
            self.browser_pool = "v" if site in ("chatgpt", "claude") else "h"
        # Канал: у побочных задач ("side") свой чат и своя квота — фоновая
        # активность не замусоривает контекст основной беседы.
        # Стейтless-каналы (_STATELESS_CHANNELS, "cc"): чат не
        # переиспользуется и не запоминается — голый промпт без истории
        self.channel = (channel or "main").strip() or "main"
        self.stateless = self.channel in _STATELESS_CHANNELS
        self._state_key = site if self.channel == "main" \
            else f"{site}#{self.channel}"
        # Лимит вызовов в час (None — без лимита; дефолт).
        # Персона включает через llm.webchat_limits (роутер передаёт).
        self.quota_per_hour = quota_per_hour
        self.base_dir = base_dir or Path(f"data/{context}/computer_control")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self.base_dir / "web_llm_state.json"
        self._tab_id: Optional[int] = None  # наша служебная вкладка (реестр CDP)
        # Вкладки веб-чатов — служебные: они не попапы кликов и не «крайняя
        # страница» для команд (browser_actions исключает _SERVICE_HOSTS).
        # У сайтов с service_host=False (google — рабочая цель команд
        # пользователя) хост не регистрируем.
        if self.adapter.get("service_host", True):
            try:
                from app.features import browser_actions as _ba
                _ba.register_service_host(self.adapter.get("host"))
            except Exception:
                pass
        # Сериализация вызовов: вкладка чата одна — параллельные запросы
        # (ответ в чате + фоновые задачи вроде LTM) иначе ломали бы друг
        # другу DOM (навигация/ввод/чтение посреди чужого цикла)
        self._lock = threading.Lock()
        # Эскалация лечения залипшей отправки: ts последней перезагрузки
        # вкладки (сначала reload, полный перезапуск Chrome — только если
        # reload уже не помог: убийство всего браузера — секундный фриз)
        self._last_tab_reload_ts = 0.0
        # Подряд идущие отказы отправки (откат/залипание): второй подряд —
        # это уже отказ сайта, а не страницы, — карантин вместо перезапуска
        # всего Chrome в цикле (кейс kimi 17.09: сервер молча отклонял
        # отправки бесплатного тарифа, каждое сообщение гасило браузер)
        self._send_fail_streak = 0
        # Флаги последнего вызова (last_call_lock_miss/last_call_tab_lost) —
        # потоко-локальные дескрипторы класса, default False
        # Инстанс выбыл из кэша роутера (retire): вкладка закрывается по
        # завершении идущего/любого следующего вызова
        self._retired = False

    # ── состояние: квота, пейсинг, адрес постоянного чата ──
    # Файл общий для всех сайтов контекста: {"sites": {site: {...}}}

    def _load_state(self) -> dict:
        """Состояние ЭТОГО сайта+канала (квота/пейсинг/chat_url); {} при
        старте. Чистое чтение без лока (см. collect_chat_urls)."""
        st = load_json_safe(self._state_path, default={}, label="WebChat")
        if not isinstance(st, dict):
            return {}
        site_st = st.get("sites", {}).get(self._state_key, {})
        return site_st if isinstance(site_st, dict) else {}

    def _mutate_state(self, mutate) -> dict:
        """Атомарно прочитать-изменить-записать состояние своего сайта+канала.
        mutate(dict) -> dict получает ТЕКУЩИЙ (свежепрочитанный под двойным
        локом — см. _update_state) словарь своего ключа и возвращает новый;
        остальные ключи файла (чужие сайты/каналы) не трогаются. Тонкая
        обвязка над общим _update_state — реализация locking+read+write
        одна на весь модуль (для этого сайта+канала и для «плоских»
        clear_chat_urls/restore_chat_urls). Используется и для точечного
        patch (_save_state), и там, где новое значение зависит от старого
        (_quota_bump: инкремент счётчика — иначе два параллельных вызова
        читали одно и то же старое значение и оба писали +1 вместо +2)."""
        def _whole(st: dict) -> dict:
            sites = st.setdefault("sites", {})
            cur = sites.get(self._state_key)
            if not isinstance(cur, dict):
                cur = {}
            new = mutate(cur)
            if not isinstance(new, dict):
                new = cur
            sites[self._state_key] = new
            return st

        whole = _update_state(self._state_path, _whole)
        site_st = whole.get("sites", {}).get(self._state_key)
        return site_st if isinstance(site_st, dict) else {}

    def _save_state(self, patch: dict):
        """Слить patch в состояние своего сайта+канала, остальные не трогать."""
        def _merge(cur: dict) -> dict:
            cur.update(patch)
            return cur
        self._mutate_state(_merge)

    def set_quota(self, per_hour: Optional[int]):
        """Сменить лимит на живую: вызовов в час; None — без лимита (дефолт)."""
        self.quota_per_hour = None if per_hour is None else max(1, int(per_hour))

    def _quota_capacity(self) -> Optional[int]:
        """Ёмкость окна квоты (per_hour × QUOTA_WINDOW_HOURS); None — без лимита."""
        if self.quota_per_hour is None:
            return None
        return max(1, int(self.quota_per_hour)) * QUOTA_WINDOW_HOURS

    def _quota_check(self) -> bool:
        """True — можно вызывать; оконный счётчик и метка последнего вызова.
        Окно QUOTA_WINDOW_HOURS часов: ёмкость исчерпана — ждём его конца,
        потом счётчик обнуляется полностью (не скользящее окно)."""
        st = self._load_state()
        cap = self._quota_capacity()
        if cap is not None:
            now = time.time()
            start = float(st.get("window_start") or 0)
            count = int(st.get("count", 0)) \
                if now - start < QUOTA_WINDOW_HOURS * 3600 else 0
            if count >= cap:
                left = int((start + QUOTA_WINDOW_HOURS * 3600 - now) / 60) + 1
                logger.warning(f"[WebChat] {self.site}: квота "
                               f"{self.quota_per_hour}/ч исчерпана — "
                               f"сброс через ~{left} мин")
                return False
        return True

    def _quota_bump(self):
        now = time.time()

        def _incr(st: dict) -> dict:
            start = float(st.get("window_start") or 0)
            if now - start >= QUOTA_WINDOW_HOURS * 3600:
                start, count = now, 0
            else:
                count = int(st.get("count", 0))
            st["window_start"] = start
            st["count"] = count + 1
            st["last_ts"] = now
            return st
        # Инкремент читает и пишет count ОДНИМ locked-проходом (_mutate_state),
        # а не load()+save(patch) в два отдельных вызова — иначе два
        # параллельных вызова читали одинаковое старое count и оба писали
        # +1 вместо +2 (та же гонка, что и с chat_url, но для счётчика).
        self._mutate_state(_incr)

    # ── постоянный чат (URL запоминается после первого сообщения) ──

    def _chat_url(self) -> Optional[str]:
        url = str(self._load_state().get("chat_url") or "").strip()
        return url or None

    def _remember_chat_url(self, url: str) -> bool:
        """Запомнить постоянный адрес чата: после первого сообщения сайт
        переводит вкладку с home на URL конкретного чата. True — адрес
        постоянный (запомнен или уже был таким), False — ещё «новый чат»."""
        if not url or self.adapter["host"] not in url:
            return False
        if url.rstrip("/") == self.adapter["home"].rstrip("/"):
            return False  # всё ещё home — чат не создан
        last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
        if last in ("new", "new-chat", "new_chat"):
            return False  # страница «нового чата», не постоянный адрес
        if url != self._chat_url():
            self._save_state({"chat_url": url})
            logger.info(f"[WebChat] {self.site}: постоянный чат {url[:70]}")
        return True

    def _capture_chat_url(self, host: str, tab_id: int):
        """Запомнить постоянный URL чата после ответа: сайт присваивает его
        не мгновенно (qwen — к концу ответа), поэтому опрашиваем несколько
        секунд, пока вкладка не уедет со страницы «нового чата»."""
        from app.features import browser_actions as ba
        for _ in range(4):
            try:
                url = ba.tab_url(host, tab_id)
            except Exception:
                return  # вкладка умерла — в следующий раз откроем home
            if not url or self._remember_chat_url(url):
                return
            time.sleep(1.5)

    # ── вкладка и постоянный чат ──

    @staticmethod
    def _same_page(a: str, b: str) -> bool:
        """URL ведут на одну страницу (хост+путь; query и слэш в конце неважны)."""
        try:
            pa, pb = urlsplit(a), urlsplit(b)
            return (pa.netloc, pa.path.rstrip("/")) == \
                   (pb.netloc, pb.path.rstrip("/"))
        except Exception:
            return False

    def _same_chat(self, a: str, b: str) -> bool:
        """Вкладка уже на нужном чате? Базово — хост+путь (_same_page);
        у сайтов с url_id (google: тред = query-параметр mtid) query несёт
        идентичность чата — сравниваем извлечённый id: по полному query
        нельзя (mstk/csuir Google перегенерирует на каждой загрузке)."""
        if not self._same_page(a, b):
            return False
        rx = self.adapter.get("url_id")
        if not rx:
            return True

        def _id(u: str):
            m = re.search(rx, u or "")
            return m.group(1) if m else None

        return _id(a) == _id(b)

    def _mode_js(self) -> Optional[str]:
        """JS режима чата с подстановкой цели по каналу: у deepseek vision-
        канал (картинки) → «Vision», текстовые — «Instant». Шаблон без %s
        (qwen Auto→Fast) возвращается как есть."""
        mode_js = self.adapter.get("mode_js")
        if not mode_js:
            return None
        if "%s" not in mode_js:
            return mode_js
        target = (self.adapter.get("mode_by_channel", {}) or {}).get(
            self.channel) or self.adapter.get("mode_default")
        if not target:
            return None
        return mode_js % json.dumps(target)

    def _after_nav(self, ba):
        """После навигации/открытия вкладки: дождаться поля ввода (первая
        загрузка чата рендерится дольше settle-паузы — без этого отправка
        падала и вызов уезжал в облачный fallback) и применить режим чата
        (mode_js адаптера — например, qwen Auto→Fast; идемпотентно)."""
        try:
            ba.wait_input(None, self._tab_id, self.adapter["input"],
                          timeout_sec=8.0)
        except Exception:
            pass  # closed-loop отправки сам отловит неготовность поля
        mode_js = self._mode_js()
        if mode_js:
            try:
                out = ba.eval_js(None, self._tab_id, mode_js)
                logger.debug(f"[WebChat] {self.site}#{self.channel}: режим чата: {out}")
            except Exception as e:
                logger.debug(f"[WebChat] {self.site}#{self.channel}: "
                             f"режим не переключён: {e}")

    def _challenge_check(self, ba, tab_id) -> bool:
        """Страница под антибот-челленджем? Одна автопопытка клика по
        чекбоксу, повторный детект — и если не прошли, сайт в карантин
        (fallback-цепочка продолжит без него, пользователь получит
        уведомление). True — челлендж активен, вкладку забываем.
        Сбой САМОГО ЗАМЕРА (вкладка не отвечает, транспорт не поддержан) —
        это «неизвестно», а не «чисто»: карантин по нему не снимается.
        Раньше detect_antibot глушил любую ошибку в None, и на фоновых
        вкладках (где замер не работал вовсе) карантин снимался вслепую."""
        try:
            label = ba.detect_antibot(None, tab_id, strict=True)
        except Exception as e:
            logger.info(f"[WebChat] {self.site}: антибот-проверку выполнить "
                        f"не удалось ({str(e)[:200]}) — состояние страницы "
                        "неизвестно, карантин не трогаю")
            return False
        if not label:
            # Страница чиста. Если сайт был в карантине — челлендж пройден
            # (пользователь в rescue): снимаем карантин, пул H возвращается
            # в штатный режим
            if site_quarantined(self.site):
                clear_quarantine(self.site)
                try:
                    ba.end_rescue_pool_h()
                except Exception:
                    pass
                logger.info(f"[WebChat] {self.site}: челлендж пройден — "
                            "карантин снят")
            return False
        logger.info(f"[WebChat] {self.site}: антибот-челлендж ({label}) — "
                    "одна автопопытка")
        try:
            if ba.try_challenge_autoclick(None, tab_id):
                time.sleep(2.0)
                try:
                    label = ba.detect_antibot(None, tab_id, strict=True)
                except Exception:
                    pass  # перепроверка не удалась — челлендж считаем живым
        except Exception:
            pass
        if label:
            quarantine_site(self.site, label)
            return True
        logger.info(f"[WebChat] {self.site}: челлендж пройден автокликом")
        return False

    def _drop_tab(self, ba, why: str):
        """Отпустить служебную вкладку: ЗАКРЫТЬ её в браузере и забыть.
        Раньше вкладку просто забывали — страница SPA-чата оставалась жить
        в Chrome пула H, и такие сироты копились до конца процесса."""
        tab_id, self._tab_id = self._tab_id, None
        if tab_id is None:
            return
        try:
            ba.close_background_tab(tab_id)
        except Exception as e:
            logger.debug(f"[WebChat] {self.site}: вкладка #{tab_id} не "
                         f"закрылась ({why}): {e}")

    def _ensure_chat(self, fresh: bool = False) -> Optional[int]:
        """Служебная вкладка сайта на «нашем» чате (сохранённый chat_url;
        fresh=True — принудительно home, т.е. новый чат). Навигация — только
        если вкладка ещё не там, без лишних перезагрузок страницы.
        None — не удалось (браузер недоступен/сайт в карантине). → tab_id|None"""
        from app.features import browser_actions as ba
        target = self.adapter["home"] if fresh \
            else (self._chat_url() or self.adapter["home"])
        try:
            if self._tab_id is not None:
                try:
                    # fresh — навигация ВСЕГДА: у google после ответа адрес
                    # треда (search?udm=50&q=…) до появления mtid совпадает с
                    # home по host+path и без id — _same_chat считал вкладку
                    # уже «на новом чате», и stateless-вызов уходил follow-up'ом
                    # в тред прошлого (замер 23.09: 2 обмена в «свежем» треде)
                    # tab_url — заодно проверка, что вкладка жива (исключение
                    # → замена вкладки ниже), поэтому зовём его и при fresh
                    cur_url = ba.tab_url(tab_id=self._tab_id)
                    if fresh or not self._same_chat(cur_url, target):
                        ba.navigate_tab(target, tab_id=self._tab_id)
                        time.sleep(FRESH_CHAT_SETTLE_SEC)
                        self._after_nav(ba)
                    if self._challenge_check(ba, self._tab_id):
                        return None
                    return self._tab_id
                except Exception as e:
                    # Вкладка не отвечает/закрыта — закрываем её явно и
                    # открываем новую (брошенная страница иначе осталась бы
                    # жить в браузере пула)
                    self._drop_tab(ba, f"замена вкладки: {str(e)[:60]}")
            self._tab_id = ba.open_new_tab(target, background=True,
                                           pool=self.browser_pool)
            self._remember_tab_snap(ba, self._tab_id)
            time.sleep(FRESH_CHAT_SETTLE_SEC)
            self._after_nav(ba)
            if self._challenge_check(ba, self._tab_id):
                return None
            return self._tab_id
        except Exception as e:
            logger.info(f"[WebChat] {self.site}: вкладка чата не открылась: {e}")
            return None

    # ── главный вызов ──

    def _fill_send(self, ba, host: str, tab_id: int, prompt: str):
        """Ввод + Enter в поле чата: по CSS-селектору адаптера; при промахе
        (редизайн фронта, хэшированные классы меняются без предупреждения) —
        поле ищется по ЦЕЛИ через общий DOM-снапшот (input_goal адаптера)
        и заполняется по DOM-метке. Детерминированно, без LLM: web_llm сам
        является LLM-провайдером роутера — звать модель для поиска её же
        поля ввода было бы круговой зависимостью."""
        try:
            ba.chat_fill_send(host, tab_id, self.adapter["input"], prompt)
            return
        except Exception as e:
            idx = self._find_input_by_goal(ba, host, tab_id)
            if idx is None:
                raise
            logger.info(f"[WebChat] {self.site}: поле по селектору не "
                        f"нашлось ({str(e)[:60]}) — ввод по цели (idx {idx})")
            ba.chat_fill_send_tagged(host, tab_id, idx, prompt)

    def _find_input_by_goal(self, ba, host: str, tab_id: int):
        """idx поля ввода по целям адаптера (input_goal): снапшот вкладки +
        детерминированный скоринг подписей editable-полей (как у команды
        «введи X в поле Y», но без LLM-ярусов). Единственное видимое поле
        страницы берётся без скоринга (у чата один композер). None — поле
        не нашлось или целей в адаптере нет."""
        goals = self.adapter.get("input_goal") or []
        if isinstance(goals, str):
            goals = [goals]
        try:
            _url, _host, items = ba.snapshot_elements(host, tab_id=tab_id)
        except Exception:
            return None
        inputs = [it for it in items if it.get("ed")]
        if not inputs:
            return None
        if goals:
            from app.features.computer_control import (
                ComputerControlManager as _CCM, LEADER_MIN_SCORE)
            best = None
            for goal in goals:
                scored = _CCM._score_candidates(inputs, goal)
                if scored and (best is None or scored[0][0] > best[0]):
                    best = scored[0]
            if best is not None and best[0] >= LEADER_MIN_SCORE:
                return int(best[1]["idx"])
        if len(inputs) == 1:
            return int(inputs[0]["idx"])
        return None

    def _send_verified(self, ba, host: str, tab_id: int, prompt: str,
                       wait_upload: bool = False):
        """chat_fill_send + подтверждение, что сообщение РЕАЛЬНО появилось
        в ленте (последний user-блок содержит наш текст). Иначе hydration-
        гонка на свеженавигированной странице обнуляет контролируемое
        React-поле до отправки, closed-loop «поле очистилось» ложно
        срабатывает, и мы 150с ждём ответ на никогда не отправленное
        сообщение (кейс 22.08). Одна повторная отправка — к тому времени
        страница уже прогрета. Без user-селектора у адаптера — как раньше.
        Проверка текстовая, а не по счётчику: лента deepseek виртуализована
        (старый обмен уходит из DOM — счётчик не растёт).
        wait_upload — отправка с картинкой: перед повтором ждём конца
        аплоада (первая могла упереться в тост «files still uploading»).
        Возвращает marker (нормализованное начало промпта) — якорь для
        _wait_answer; None — у адаптера нет user-селекторов."""
        user_sels = self.adapter.get("user")
        if not user_sels:
            self._fill_send(ba, host, tab_id, prompt)
            return None
        want = " ".join(prompt[:80].split()).lower()

        def _seen(wait_sec: float) -> bool:
            deadline = time.time() + wait_sec
            while time.time() < deadline:
                try:
                    last = ba.last_block_text(host, tab_id, user_sels)
                except Exception:
                    last = ""
                if want and want in " ".join(last.split()).lower():
                    return True
                time.sleep(0.7)
            return False

        for attempt in (1, 2):
            if attempt > 1 and wait_upload:
                ba.chat_wait_uploaded(host, tab_id, self.adapter["input"])
            try:
                self._fill_send(ba, host, tab_id, prompt)
            except ba.FillUncertain as e:
                # Enter не очистил поле — ниже проверим ленту и кнопку
                logger.info(f"[WebChat] {self.site}#{self.channel}: {str(e)[:70]}")
            if _seen(SEND_VERIFY_SEC):
                return want
            # Текст так и лежит в поле — Enter утонул (claude.ai на холодной
            # вкладке, замер 23.09: 2 из 2, повторный Enter тоже тонет, а
            # клик по кнопке отправки проходит). Клик безопасен от дублей:
            # жмём только при неотправленном тексте в поле
            if self._click_send_if_unsent(ba, host, tab_id, want) and _seen(SEND_VERIFY_SEC):
                logger.info(f"[WebChat] {self.site}#{self.channel}: отправлено "
                            "кнопкой после неудачного Enter")
                return want
            logger.info(f"[WebChat] {self.site}#{self.channel}: сообщение не "
                        f"появилось в ленте (попытка {attempt})")
        raise TimeoutError("сообщение не появилось в ленте после отправки")

    # Кнопка отправки композера: aria-label/title «Send…»/«Отправ…», видимая и
    # активная. adapter["send_button"] — явный селектор, если эвристика мимо
    _CLICK_SEND_JS = (
        "(function(fieldSel, want, btnSel){"
        "var f=document.querySelector(fieldSel); if(!f) return 'no-field';"
        "var t=(f.isContentEditable?f.innerText:f.value)||'';"
        "t=t.replace(/\\s+/g,' ').trim().toLowerCase();"
        "if(!want||t.indexOf(want.slice(0,40))<0) return 'not-unsent';"
        "var bs=btnSel?[].slice.call(document.querySelectorAll(btnSel)):"
        "[].slice.call(document.querySelectorAll('button')).filter(function(b){"
        "var l=(b.getAttribute('aria-label')||b.getAttribute('title')||'');"
        "return /^(send|отправ)/i.test(l.trim());});"
        "bs=bs.filter(function(b){var r=b.getBoundingClientRect();"
        "return !b.disabled&&r.width>0&&r.height>0;});"
        "if(!bs.length) return 'no-button';"
        "bs[bs.length-1].click(); return 'clicked';})(%s,%s,%s)"
    )

    def _click_send_if_unsent(self, ba, host: str, tab_id: int, want: str) -> bool:
        """Клик по кнопке отправки, если наш текст всё ещё в поле ввода."""
        try:
            out = ba.eval_js(host, tab_id, self._CLICK_SEND_JS % (
                json.dumps(self.adapter["input"]), json.dumps(want or ""),
                json.dumps(self.adapter.get("send_button") or "")))
        except Exception as e:
            logger.debug(f"[WebChat] {self.site}: клик отправки не удался: {e}")
            return False
        if out != "clicked":
            logger.debug(f"[WebChat] {self.site}: кнопка отправки: {out}")
        return out == "clicked"

    _TAB_RELOAD_COOLDOWN_SEC = 120.0  # чаще, чем раз в — reload уже не помогает

    def _restart_stuck_browser(self, ba, cause: str):
        """Лечение залипшей отправки (поле не очистилось после Enter /
        сообщение не появилось в ленте после двух попыток) — эскалация
        по ступеням: сначала ПЕРЕЗАГРУЗКА ВКЛАДКИ (дёшево, страница
        собирается заново; вкладка остаётся нашей — _tab_id сохраняем),
        и только если она была недавно и не помогла — перезапуск всего
        Chrome (тяжело: SIGTERM + восстановление всех вкладок — секундный
        фриз системы). Текущий вызов честно уходит в фолбэк в обоих
        случаях, свежая страница подхватит следующий."""
        now = time.time()
        if self._tab_id is not None and \
                now - self._last_tab_reload_ts > self._TAB_RELOAD_COOLDOWN_SEC:
            self._last_tab_reload_ts = now
            try:
                ba.reload_tab(self._tab_id)
                logger.info(f"[WebChat] {self.site}: вкладка перезагружена "
                            f"({cause}) — следующий вызов на свежей странице")
                return
            except Exception as e:
                # Вкладка мертва/не перезагрузилась — забываем её; если дело
                # не во вкладке, следующая неудача уйдёт уже в перезапуск
                # браузера (cooldown reload'а к тому моменту исчерпан)
                logger.info(f"[WebChat] {self.site}: reload вкладки не "
                            f"удался ({e}) — закрываю её")
                self._drop_tab(ba, "reload не удался")
                return
        # Перезапуск убьёт вкладки ВСЕХ идущих вызовов пула — под чужим
        # основным ответом (этот процесс или другой процесс бота) не
        # перезапускаем: закрываем только свою вкладку (следующий вызов
        # откроет свежую), а если залипание повторится — сработает
        # карантин сайта по _send_fail_streak, Chrome цел.
        # _begin_restart атомарно проверяет и ставит флаг перезапуска (новые
        # основные вызовы ждут его снятия), межпроцессно держит эксклюзивный
        # flock до _end_restart — окна «проверили → вызов начался → убили»
        # нет. Свою залипшую вкладку закрываем ПОСЛЕ перезапуска: закрытие на
        # залипшем браузере само висит до таймаута CDP, а после перезапуска
        # вкладки уже нет в реестре — закрытие мгновенное.
        own_fg = self.channel not in _BACKGROUND_CHANNELS
        blocker = _begin_restart(self.browser_pool, own_foreground=own_fg)
        if blocker:
            logger.info(f"[WebChat] {self.site}#{self.channel}: перезапуск "
                        f"браузера отложен — {blocker}; закрываю только свою "
                        "вкладку")
            self._drop_tab(ba, "перезапуск браузера отложен")
            return
        try:
            # Страховка: флаг стоит, новых основных вызовов быть не может —
            # но перед убийством Chrome перепроверяем счётчик ещё раз
            others = _fg_others(self.browser_pool, own_fg)
            if others > 0:
                logger.info(f"[WebChat] {self.site}: перезапуск отменён — "
                            f"в пуле идёт основной вызов ({others})")
            elif ba.restart_browser(reason=f"webchat {self.site}: {cause}",
                                    pool=self.browser_pool):
                logger.info(f"[WebChat] {self.site}: браузер бота "
                            "перезапущен — следующий вызов откроет свежую "
                            "вкладку")
        except Exception as e:
            logger.warning(f"[WebChat] {self.site}: перезапуск браузера "
                           f"не удался: {e}")
        finally:
            _end_restart(self.browser_pool)
        # Перезапуск прошёл — вкладки нет в реестре, это просто «забыть»;
        # пропущен (cooldown) — настоящее закрытие, уже вне флага
        self._drop_tab(ba, "перезапуск браузера")

    @contextmanager
    def _call_serialized(self, lock_timeout: Optional[float] = None):
        """Порядок локов всегда один: очередь сайта → общий семафор фона →
        per-instance лок (фон держит не больше одной очереди сайта за раз —
        взаимных блокировок нет). Фон (side/proactive) — через очередь своего
        сайта и семафор BG_MAX_PARALLEL (см. «Очереди фоновых каналов»).
        Основные каналы (main/vision) в очереди не стоят — только свой
        per-instance лок.

        lock_timeout — бюджет ожидания ДО начала работы (сек):
        - основные каналы: потолок per-instance лока (пользовательский путь
          не ждёт чужую генерацию инстанса — кейс 18.09: живой flavor-вызов
          висел 2.5 мин за генерацией банка); None — ждать без ограничения;
        - фон: ОБЩИЙ бюджет на очередь сайта + семафор + лок инстанса
          (раньше лок инстанса фона ждался без ограничения); None —
          BG_GATE_TIMEOUT_SEC (прямые вызовы без цепочки фолбэков).
        False — не дождались: вызывающий возвращает None в фолбэк.
        Промах по занятости отмечается last_call_lock_miss — роутер
        отличает его от прочих None: main уходит в burst-чат, фон — к
        следующему провайдеру (и во второй заход, если занято всё).
        На время работы вызов отмечен в реестре идущих вызовов пула —
        перезапуск Chrome под основным ответом запрещён (_begin_restart)."""
        self.last_call_lock_miss = False
        self.last_call_tab_lost = False
        bg = self.channel in _BACKGROUND_CHANNELS
        budget = lock_timeout
        if bg and budget is None:
            budget = BG_GATE_TIMEOUT_SEC
        deadline = None if budget is None else time.monotonic() + max(0.0, budget)

        def _left() -> float:
            return max(0.0, deadline - time.monotonic())

        site_q = _bg_site_queue(self.site) if bg else None
        q_held = sem_held = locked = False
        try:
            if bg:
                q_held = site_q.acquire(timeout=_left())
                if q_held:
                    sem_held = _BG_PARALLEL.acquire(timeout=_left())
                if not (q_held and sem_held):
                    # debug: второй заход роутера опрашивает занятые сайты
                    # квантами по 5 с — info-строка на каждый квант зашумила
                    # бы лог; итог занятости пишет роутер
                    self.last_call_lock_miss = True
                    logger.debug(f"[WebChat] {self.site}#{self.channel}: очередь "
                                 f"фона {'сайта' if not q_held else 'процесса'} "
                                 f"занята дольше {budget:.1f}с — пропуск")
                    yield False
                    return
            if deadline is None:
                self._lock.acquire()
                locked = True
            else:
                locked = self._lock.acquire(timeout=_left())
            if not locked:
                self.last_call_lock_miss = True
                yield False
                return
            with _inflight_call(self.browser_pool, foreground=not bg) as ok:
                # ok=False — пул перезапускается дольше
                # INFLIGHT_RESTART_WAIT_SEC: вызов не начинаем (фолбэк)
                yield ok
        finally:
            if locked:
                self._lock.release()
                # Выбыл из кэша (retire), пока шёл вызов — вкладка больше не
                # нужна. Проверка ПОСЛЕ release: retire, пришедший между
                # «проверили флаг» и «отпустили лок», свой неблокирующий захват
                # проиграл бы, и вкладку не закрыл бы никто. Захват здесь тоже
                # неблокирующий: занято — значит лок у нового вызова, и его
                # конец закроет вкладку сам
                if self._retired and self._lock.acquire(blocking=False):
                    try:
                        self.close()
                    except Exception:
                        pass
                    finally:
                        self._lock.release()
            if sem_held:
                _BG_PARALLEL.release()
            if q_held:
                site_q.release()

    def _quarantine_skip(self, ba=None) -> bool:
        """Сайт на карантине и rescue не активен — вызов пропускается
        мгновенно (цепочка идёт дальше без тяжёлых попыток через страницу
        с капчей). Исключение — активный rescue (пул H видимый, пользователь
        решает капчу): даём сайту шанс, челлендж мог быть уже пройден
        (снимется в _challenge_check). Зовётся ДО очереди (иначе фон
        отстаивал очередь ради сайта, который сразу же пропустит) и ещё раз
        под локом (карантин мог начаться, пока ждали)."""
        if not site_quarantined(self.site):
            return False
        try:
            if ba is None:
                from app.features import browser_actions as ba
            rescue = self.browser_pool == "h" and ba.pool_h_rescue_active()
        except Exception:
            rescue = False
        if not rescue:
            logger.info(f"[WebChat] {self.site}: карантин активен — пропуск")
            return True
        logger.info(f"[WebChat] {self.site}: rescue активен — проверяем, "
                    "не пройден ли челлендж")
        return False

    def retire(self):
        """Инстанс выбыл из кэша роутера (смена сайта/движка): закрыть его
        вкладку. Свободен — сразу; через него идёт вызов — по завершении
        этого вызова (_call_serialized), чтобы не оборвать ответ, который
        кто-то ждёт. Поток, успевший взять ссылку на инстанс до сброса кэша,
        может открыть вкладку и позже — её тоже закроет конец вызова."""
        self._retired = True
        if self._lock.acquire(blocking=False):
            try:
                self.close()
            finally:
                self._lock.release()

    def close(self):
        """Закрыть служебную вкладку инстанса (разовые burst-чаты: инстанс
        не кэшируется, и без закрытия каждый burst оставлял в пуле живую
        SPA-вкладку до конца процесса)."""
        if self._tab_id is None:
            return
        try:
            from app.features import browser_actions as ba
        except Exception:
            return
        self._drop_tab(ba, "разовый чат завершён")

    @staticmethod
    def _tab_token(ba, tab_id) -> Optional[tuple]:
        """Снимок «жизни» вкладки для _tab_vanished: (пул, поколение пула,
        targetId) для фоновой вкладки из реестра browser_actions; None —
        вкладка не из реестра (Safari, видимая вкладка-фолбэк) — её так не
        проверяем. Пул берём из записи реестра (вкладка могла открыться
        фолбэком не в своём пуле), поколение — raw_pool_generation (растёт на
        каждом сбросе пула: перезапуск Chrome, rescue, смена режима)."""
        try:
            if not ba.is_raw_tab(tab_id):
                return None
        except Exception:
            return None
        pool = gen = target = None
        try:
            rec = ba._raw_tab(tab_id) or {}
            pool, target = rec.get("pool"), rec.get("targetId")
        except Exception:
            pass
        if pool:
            try:
                gen = ba.raw_pool_generation(pool)
            except Exception:
                gen = None
        return (pool, gen, target)

    def _remember_tab_snap(self, ba, tab_id) -> None:
        """Снимок вкладки берётся ОДИН раз — сразу при открытии. Пересчёт
        позже (перед ожиданием ответа) отключал детект: пул, сброшенный
        между открытием и ожиданием, давал снимок None — «не из реестра»."""
        self._tab_snap = (tab_id, self._tab_token(ba, tab_id))

    def _snap_for(self, ba, tab_id) -> Optional[tuple]:
        snap = getattr(self, "_tab_snap", None)
        if snap and snap[0] == tab_id:
            return snap[1]
        # Вкладку открыли в обход _ensure_chat/ask_by_url (тесты, старые
        # пути) — снимок по текущему реестру, лучше, чем ничего
        token = self._tab_token(ba, tab_id)
        self._tab_snap = (tab_id, token)
        return token

    @staticmethod
    def _tab_vanished(ba, tab_id, token: Optional[tuple]) -> bool:
        """Фоновая вкладка, которая БЫЛА в реестре browser_actions, умерла:
        пропала из реестра (перезапуск Chrome пула / _reset_raw_pool /
        закрытие) или её пул сменил поколение (Chrome пула перезапущен —
        вкладка умерла вместе с ним). Для вкладок не из реестра — False."""
        if not token:
            return False
        try:
            if not ba.is_raw_tab(tab_id):
                return True
        except Exception:
            return False
        pool, gen = token[0], token[1]
        if pool and gen is not None:
            try:
                return ba.raw_pool_generation(pool) != gen
            except Exception:
                return False
        return False

    def _on_tab_lost(self, why: str, ba=None, tab_id=None,
                     token: Optional[tuple] = None) -> None:
        """Вкладка вызова умерла: забыть её и отметить промах — main-путь
        роутера уйдёт в burst. Если поколение пула НЕ сменилось (Chrome не
        перезапускался — вкладку лишь выкинули из реестра), страница может
        жить в Chrome — закрываем её по targetId. Закрытие — в фоне: на
        залипшем браузере оно висит до таймаута CDP, а фолбэк ответа ждать
        его не должен."""
        self._tab_id = None
        self.last_call_tab_lost = True
        logger.warning(f"[WebChat] {self.site}#{self.channel}: вкладка чата "
                       f"исчезла ({why}) — выход без ожидания, фолбэк")
        if ba is None or not token:
            return
        pool, gen, target = (tuple(token) + (None, None, None))[:3]

        def _close_orphan():
            try:
                if ba.is_raw_tab(tab_id):
                    ba.close_background_tab(tab_id)
                    return
                if not (pool and target) or gen is None \
                        or ba.raw_pool_generation(pool) != gen:
                    return  # Chrome перезапускался — страница умерла с ним
                ba._raw_call("Target.closeTarget", {"targetId": target},
                             pool=pool)
                logger.info(f"[WebChat] {self.site}: осиротевшая вкладка "
                            f"#{tab_id} закрыта по targetId")
            except Exception as e:
                logger.debug(f"[WebChat] {self.site}: осиротевшая вкладка "
                             f"#{tab_id} не закрылась: {e}")

        threading.Thread(target=_close_orphan, daemon=True,
                         name=f"webchat-orphan-{self.site}").start()

    def get_response(self, messages: list, temperature: float = 0.7,
                     max_tokens: int = 2000, top_p: float = 0.9,
                     timeout: float = 60.0,
                     lock_timeout: Optional[float] = None) -> Optional[str]:
        """messages как у OpenAI-роутера → текст ответа или None (фолбэк
        вызывающего). system-части склеиваются вводным блоком инструкций.
        temperature/max_tokens/top_p приняты для совместимости сигнатуры с
        API-провайдерами, но НЕ действуют: веб-чаты не дают выставить их на
        запрос — модель отвечает с дефолтами сайта. lock_timeout — бюджет
        ожидания до начала работы (см. _call_serialized)."""
        self.last_call_lock_miss = False
        self.last_call_tab_lost = False
        if self._quarantine_skip():
            return None
        with self._call_serialized(lock_timeout=lock_timeout) as allowed:
            if not allowed:
                (logger.debug if self.channel in _BACKGROUND_CHANNELS
                 else logger.info)(
                    f"[WebChat] {self.site}#{self.channel}: вызов не "
                    "дождался очереди/лока — пропуск (фолбэк)")
                return None
            return self._get_response_locked(messages, temperature, max_tokens,
                                             top_p, timeout)

    def ask_by_url(self, query: str, timeout: float = 15.0,
                   lock_timeout: Optional[float] = 0.0) -> Optional[str]:
        """Голый вопрос через адрес (adapter["query_url"]) → текст ответа или
        None. Для веб-поиска через AI Mode (канал search): свежий тред на
        каждый вызов, без префикса ролей и инструкций, одна навигация вместо
        home + ввод + проверка отправки (замер 23.09: −2.5…3 с на вызове).
        lock_timeout=0 — занятый инстанс (прошлый поиск ещё дописывается во
        вкладке) не ждём: вызывающий берёт другой источник."""
        from urllib.parse import quote_plus
        tpl = self.adapter.get("query_url")
        q = " ".join((query or "").split())
        if not tpl or not q:
            return None
        with self._call_serialized(lock_timeout=lock_timeout) as allowed:
            if not allowed:
                logger.info(f"[WebChat] {self.site}#{self.channel}: инстанс "
                            "занят прошлым вызовом — пропуск")
                return None
            if not self._quota_check():
                return None
            if site_quarantined(self.site):
                logger.info(f"[WebChat] {self.site}: карантин активен — пропуск")
                return None
            from app.features import browser_actions as ba
            host = self.adapter["host"]
            url = tpl.format(q=quote_plus(q))
            try:
                if self._tab_id is not None:
                    try:
                        ba.navigate_tab(url, tab_id=self._tab_id)
                    except Exception as e:
                        self._drop_tab(ba, f"замена вкладки: {str(e)[:60]}")
                if self._tab_id is None:
                    self._tab_id = ba.open_new_tab(url, background=True,
                                                   pool=self.browser_pool)
                    self._remember_tab_snap(ba, self._tab_id)
                if self._challenge_check(ba, self._tab_id):
                    return None
            except Exception as e:
                logger.info(f"[WebChat] {self.site}: вкладка вопроса не "
                            f"открылась: {e}")
                return None
            self._quota_bump()
            # Якорь — наш вопрос в ленте треда (user-селектор адаптера):
            # ответ ищется строго после него. Ждём, пока вопрос появится в
            # ленте: сразу после навигации его ещё нет, _wait_answer тогда
            # откатывался в baseline-режим без маркера завершения и снимал
            # первый же абзац стрима (замер 23.09: 300 симв. из 3300)
            marker = None
            user_sels = self.adapter.get("user")
            if user_sels:
                want = " ".join(q[:80].split()).lower()
                seen_by = time.time() + SEND_VERIFY_SEC
                while time.time() < seen_by:
                    try:
                        last = ba.last_block_text(host, self._tab_id, user_sels)
                    except Exception:
                        last = ""
                    if want in " ".join(last.split()).lower():
                        marker = want
                        break
                    time.sleep(0.3)
                if marker is None:
                    logger.info(f"[WebChat] {self.site}#{self.channel}: вопрос "
                                "не появился в треде — нет ответа")
                    return None
            q_tab = self._tab_id
            try:
                answer = self._wait_answer(host, q_tab, timeout=timeout,
                                           marker=marker)
            except _TabLost as e:
                self._on_tab_lost(str(e), ba, q_tab, self._snap_for(ba, q_tab))
                return None
            except _ChatRateLimited as e:
                quarantine_site(self.site, str(e)[:100], kind="ratelimit",
                                ttl=e.ttl or RATE_LIMIT_DEFAULT_TTL_SEC)
                return None
            except _ChatBroken as e:
                # Отказ/ошибка на ОДНОМ вопросе — не повод карантинить сайт
                # (им же пользуются другие каналы): просто нет ответа
                logger.info(f"[WebChat] {self.site}#{self.channel}: {str(e)[:80]}")
                return None
        if answer:
            logger.info(f"[WebChat] {self.site}#{self.channel}: ответ "
                        f"{len(answer)} симв.")
        return answer

    def get_response_with_image(self, prompt: str, image_bytes: bytes,
                                timeout: float = 150.0,
                                image_mime: str = "image/png",
                                extra_image: Optional[bytes] = None
                                ) -> Optional[str]:
        """Текст + картинка (vision-фолбэк резолва): изображение вставляется
        в композер синтетическим paste, буфер обмена пользователя не
        трогаем. Только для сайтов с adapter["images"] (проверено, что сайт
        paste принимает). image_mime — реальный тип байтов (jpeg легче png
        для аплоада; имя/тип File в paste-событии ставится по нему).
        extra_image — второй кадр (чистый скриншот без разметки для сверки):
        прикрепляется, только если adapter["max_images"] >= 2 (google AI
        Mode принимает одну картинку — второй кадр туда не шлём вовсе).
        Переполнение вопреки max_images (сайт ответил «too many images»):
        adapter["image_overflow"] — trim (дефолт): повтор с одним кадром;
        followup: оставшийся кадр доотправляется вторым сообщением после
        первого ответа, финальным считается уточнённый (второй).
        None — сайт без картинок / вставка или ответ не удались (честный
        фолбэк вызывающего)."""
        if not self.adapter.get("images") or not image_bytes:
            return None
        max_images = int(self.adapter.get("max_images", 2) or 1)
        extra_allowed = extra_image if (extra_image and max_images >= 2) else None
        if extra_image and extra_allowed is None:
            logger.info(f"[WebChat] {self.site}: лимит {max_images} кадр(а) — "
                        "второй кадр в это сообщение не прикрепляем")
        with self._call_serialized() as allowed:
            if not allowed:
                logger.info(f"[WebChat] {self.site}#{self.channel}: очередь "
                            "фоновых вызовов не дождалась — пропуск (фолбэк)")
                return None
            try:
                answer = self._get_response_locked(
                    [{"role": "user", "content": prompt}], 0.7, 2000, 0.9,
                    timeout, image_bytes=image_bytes, image_mime=image_mime,
                    extra_image_bytes=extra_allowed)
            except _TooManyImages:
                # Лимит сайта оказался меньше сконфигурированного — повтор
                # одним кадром (max_images адаптера тогда стоит уточнить)
                logger.warning(f"[WebChat] {self.site}: сайт отверг число "
                               "кадров — повторяю с одним")
                try:
                    answer = self._get_response_locked(
                        [{"role": "user", "content": prompt}], 0.7, 2000, 0.9,
                        timeout, image_bytes=image_bytes, image_mime=image_mime,
                        extra_image_bytes=None)
                except _TooManyImages:
                    return None
            # followup: оставшийся кадр — вторым сообщением в тот же чат,
            # финальным считается уточнённый (второй) ответ
            if answer and extra_image and extra_allowed is None and \
                    self.adapter.get("image_overflow") == "followup":
                try:
                    follow = self._get_response_locked(
                        [{"role": "user", "content": (
                            "Это тот же кадр, но без разметки-номерков. "
                            "Уточни свой предыдущий ответ по нему (если "
                            "уточнять нечего — коротко подтверди его).")}],
                        0.7, 2000, 0.9, timeout,
                        image_bytes=extra_image, image_mime=image_mime,
                        extra_image_bytes=None)
                    if follow:
                        answer = follow
                except _TooManyImages:
                    pass  # одна картинка уже принята — остаёмся на ней
            return answer

    def _get_response_locked(self, messages: list, temperature: float,
                             max_tokens: float, top_p: float,
                             timeout: float,
                             image_bytes: Optional[bytes] = None,
                             image_mime: str = "image/png",
                             extra_image_bytes: Optional[bytes] = None
                             ) -> Optional[str]:
        from app.features import browser_actions as ba
        prompt = self._join_messages(messages)
        if not prompt:
            return None
        cap = self.adapter.get("max_input")
        if cap and len(prompt) > cap:
            logger.warning(f"[WebChat] {self.site}: промпт {len(prompt)} симв. "
                           f"длиннее лимита поля ({cap}) — сайт может не "
                           "ответить (замер google 17.09: >~9-10 тыс. → "
                           "«content wasn't generated»)")
        if not self._quota_check():
            return None
        # Карантин (антибот-челлендж и т.п.): повторная проверка под локом —
        # сайт мог уйти в карантин, пока вызов ждал очереди (первая — до
        # очереди, в get_response)
        if self._quarantine_skip(ba):
            return None
        # headed-сайт (пул V), а режим управления нигде не включён: видимый
        # Chrome не поднимаем, сайт пропускается (override — конфиг
        # browser.headed_fallback_without_control)
        if self.browser_pool == "v":
            try:
                if not ba.pool_v_webchat_allowed():
                    logger.info(f"[WebChat] {self.site}: пул V выключен (нет "
                                "режима управления) — пропуск")
                    return None
            except Exception:
                pass
        host = self.adapter["host"]
        answer = None
        # Стейтless-каналы — всегда свежий чат (одна попытка: сохранённого
        # чата не бывает по определению, ретрай на «сломанный чат» не нужен)
        for fresh in ((True,) if self.stateless else (False, True)):
            tab_id = self._ensure_chat(fresh=fresh)
            if tab_id is None:
                return None
            # Вкладка из реестра фоновых (raw-CDP)? Тогда её исчезновение
            # посреди вызова детектируется мгновенно (_tab_vanished)
            tab_token = self._snap_for(ba, tab_id)
            marker = None
            try:
                excl = self.adapter.get("answer_exclude")
                baseline = ba.count_blocks(host, tab_id, self.adapter["answer"])
                # baseline в plain-тексте: тики опроса тоже plain (markdown
                # снимается один раз по стабилизации) — сравнение консистентно
                baseline_text = ba.last_block_text(host, tab_id,
                                                   self.adapter["answer"],
                                                   markdown=False,
                                                   exclude=excl)
                if image_bytes:
                    # Картинка — ДО текста: композер пуст, фокус чистый;
                    # сайт вставку не подтвердил — нет смысла слать голый
                    # текст, честный фолбэк (и квоту не тратим)
                    if not ba.chat_paste_image(host, tab_id,
                                               self.adapter["input"],
                                               image_bytes,
                                               mime=image_mime):
                        logger.warning(f"[WebChat] {self.site}: картинка не "
                                       "прикрепилась — запрос без ответа")
                        return None
                    # Аплоад идёт ПОСЛЕ появления аттача: «отправить» в это
                    # окно даёт только тост «files still uploading», а
                    # сообщение теряется — ждём маркера готовности
                    if not ba.chat_wait_uploaded(host, tab_id,
                                                 self.adapter["input"]):
                        logger.info(f"[WebChat] {self.site}: аплоад не "
                                    "подтвердился — шлём с перестраховкой")
                    if extra_image_bytes:
                        # Второй кадр (чистый скриншот без разметки) — best
                        # effort: не прикрепился — отвечаем по одной
                        if not ba.chat_paste_image(host, tab_id,
                                                   self.adapter["input"],
                                                   extra_image_bytes,
                                                   mime=image_mime):
                            logger.info(f"[WebChat] {self.site}: второй кадр "
                                        "не прикрепился — идём с одним")
                        else:
                            ba.chat_wait_uploaded(host, tab_id,
                                                  self.adapter["input"])
                marker = self._send_verified(ba, host, tab_id, prompt,
                                             wait_upload=bool(image_bytes))
                self._send_fail_streak = 0
            except Exception as e:
                if self._tab_vanished(ba, tab_id, tab_token):
                    # Вкладку убили извне (перезапуск Chrome пула и т.п.):
                    # это не залипшая страница и не битый чат — без reload/
                    # перезапуска браузера (иначе каскад перезапусков) и без
                    # сброса адреса чата
                    self._on_tab_lost(f"при отправке: {str(e)[:60]}", ba,
                                      tab_id, tab_token)
                    return None
                if not fresh and self._chat_url():
                    # Сохранённый чат сломался (удалён/разлогинен) — свежий
                    logger.info(f"[WebChat] {self.site}: сохранённый чат не "
                                f"принял ввод ({e}) — уходим на новый")
                    self._save_state({"chat_url": ""})
                    continue
                logger.warning(f"[WebChat] {self.site}: отправка не удалась: {e}")
                # Причина «залипания» может быть в антибот-челлендже,
                # всплывшем на странице, — тогда не лечим вкладку, а
                # карантиним сайт (fallback-цепочка продолжит без него)
                try:
                    challenged = self._tab_id is not None and \
                        self._challenge_check(ba, self._tab_id)
                except Exception:
                    challenged = False
                if challenged:
                    # Карантин: вкладка простаивает открытой до конца карантина
                    return None
                # Страница залипла в состоянии, которое не чинится
                # навигацией (двойная неудача закрытого цикла) — лечим
                # эскалацией: reload вкладки, затем перезапуск браузера
                self._send_fail_streak += 1
                if self._send_fail_streak >= 2:
                    # Повторный отказ отправки после reload — это уже не
                    # залипшая страница, а отказ сайта (перегрузка/лимит
                    # тарифа): карантин вместо перезапуска всего Chrome
                    # в цикле (кейс kimi 17.09: сервер молча отклонял
                    # отправки бесплатного тарифа, каждое сообщение гасило
                    # браузер бота). TTL карантина — самолечение.
                    self._send_fail_streak = 0
                    quarantine_site(self.site, "отправка отклоняется "
                                    f"подряд ({str(e)[:60]})", kind="refused")
                    return None
                self._restart_stuck_browser(ba, str(e))
                return None
            self._quota_bump()
            # Ответ уже ушёл в чат — повторной отправки не делаем, даже
            # если ожидание оборвётся таймаутом (во избежание дублей)
            try:
                # Каналам на пользовательском пути (cc/cc_gen) пол
                # ANSWER_TIMEOUT_SEC не нужен: вызывающий знает свой бюджет
                # (кейс 18.09: переданные 30с превращались в 150с молчания).
                # Остальным пол нужен именно ЗДЕСЬ, после отправки: таймауты
                # вызывающих откалиброваны под API (LTM: 15 с), а сайт отвечает
                # 8–80 с. Бросить ожидание раньше — потерять уже отправленный
                # вызов, а постоянный чат оставить посреди генерации: следующая
                # отправка в него упирается в идущий ответ, не проходит
                # проверку ленты и эскалирует в reload/перезапуск Chrome.
                # Короткий таймаут вызывающего уважается ДО отправки: роутер
                # отдаёт его как бюджет очереди фона (lock_timeout), где
                # отказ ничего не стоит — см. ModelRouter._try_webchat
                eff_timeout = (timeout if self.channel
                               in _NO_TIMEOUT_FLOOR_CHANNELS
                               else max(timeout, ANSWER_TIMEOUT_SEC))
                answer = self._wait_answer(host, tab_id,
                                           timeout=eff_timeout,
                                           baseline=baseline,
                                           baseline_text=baseline_text,
                                           marker=marker,
                                           had_image=bool(image_bytes))
            except _TabLost as e:
                # Сообщение ушло, но вкладку убили: ответ в ЭТОЙ вкладке не
                # снять. Повтор в тот же постоянный чат дал бы дубль —
                # роутер переспросит в burst (свежий чат) или пойдёт дальше
                self._on_tab_lost(str(e), ba, tab_id, tab_token)
                return None
            except _ChatBroken as e:
                if isinstance(e, _TooManyImages):
                    raise  # решает get_response_with_image (trim/followup)
                if isinstance(e, _ChatRateLimited):
                    # Лимит сообщений с явным временем восстановления:
                    # карантин на распарсенный TTL (не распарсен — дефолт
                    # RATE_LIMIT_DEFAULT_TTL_SEC), чат не трогаем, цепочка
                    # мгновенно идёт к следующим провайдерам
                    quarantine_site(self.site, str(e)[:100], kind="ratelimit",
                                    ttl=e.ttl or RATE_LIMIT_DEFAULT_TTL_SEC)
                    return None
                if isinstance(e, _ChatRefused) or _OVERLOAD_RE.search(str(e)):
                    # Сайт отказал в обслуживании (перегрузка/тариф, откат
                    # отправки): чат не битый — его адрес сохраняем; сайт
                    # уходит в карантин, цепочка продолжается мгновенно
                    quarantine_site(self.site, str(e)[:100], kind="refused")
                    return None
                if not fresh and self._chat_url():
                    # Сохранённый чат сломан НА САЙТЕ (удалён/битый parent_id)
                    # — сбрасываем адрес и уходим на свежий чат
                    logger.info(f"[WebChat] {self.site}: {e} — уходим на новый чат")
                    self._save_state({"chat_url": ""})
                    continue
                logger.warning(f"[WebChat] {self.site}: {e}")
                return None
            # Стейтless-каналы не запоминают адрес чата — следующий вызов
            # снова начнёт со свежего (памяти у системных вызовов нет)
            if not self.stateless:
                self._capture_chat_url(host, tab_id)
            break
        if answer:
            logger.info(f"[WebChat] {self.site}: ответ {len(answer)} симв.")
        return answer

    @staticmethod
    def _join_messages(messages: list) -> str:
        """OpenAI-messages → один текст для веб-чата: system — блоком
        инструкций в начале, дальше реплики с префиксами ролей."""
        sys_parts: List[str] = []
        convo: List[str] = []
        for m in messages or []:
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                continue  # мультимодальные куски веб-чату не отдать
            role = m.get("role")
            if role == "system":
                sys_parts.append(content.strip())
            else:
                prefix = {"user": "Пользователь",
                          "assistant": "Ассистент"}.get(role)
                convo.append(f"{prefix}: {content.strip()}" if prefix
                             else content.strip())
        parts = []
        if sys_parts:
            parts.append("Инструкции (соблюдай строго, не пересказывай):\n"
                         + "\n\n".join(sys_parts))
        parts.extend(convo)
        return "\n\n".join(parts).strip()

    def _wait_answer(self, host: str, tab_id: int, timeout: float,
                     baseline: int = 0, baseline_text: str = "",
                     marker: Optional[str] = None,
                     had_image: bool = False) -> Optional[str]:
        """Опрос ответа до конца стриминга. Два режима детекции новизны:

        1. Якорный (marker задан, у адаптера есть user-селекторы): новизна =
           блоки ответа ПОСЛЕ нашего user-сообщения в DOM (answer_blocks_after).
           Не зависит от прогретости страницы на момент baseline — без него
           при медленном рендере истории SPA старый завершённый ответ
           выглядел «новым» и возвращался вместо настоящего (кейс 22.08:
           реформулировка coref уходила пользователю как ответ). Якорь не
           найден (лента виртуализована и съела своё сообщение?) — разовый
           откат на baseline-режим.
        2. Baseline: новый ответ = блоков стало БОЛЬШЕ baseline (непрерывный
           чат: прошлые ответы уже в DOM — ждём именно новый) ИЛИ текст
           последнего блока СМЕНИЛСЯ относительно досылочного: сайты с
           виртуализацией ленты (deepseek держит в DOM только последний
           обмен) не наращивают счётчик — по одному числу блоков ответ не
           отличить от старого.

        Текст непустой и стабилен. Сколько замеров стабильности ждать —
        зависит от канала: пользовательские (main/vision) ответы ждут
        живые люди, поэтому при done-маркере адаптера (кнопки действий у
        завершённого ответа) текст берётся СРАЗУ, а без маркера — один
        подтверждающий тик против захвата середины стрима; фоновые задачи
        (side/proactive) никто не ждёт — полные STABLE_POLLS.
        None по таймауту — честный фолбэк.

        Экономика тика (фризы UI от постоянной нагрузки на рендерер):
        замеры идут в plain-тексте (markdown-сериализация всего поддерева —
        один раз в конце, по стабилизации), а баннер-пробник, сканирующий
        всю страницу с getComputedStyle, — раз в BANNER_PROBE_EVERY тиков
        (и каждый тик, пока подтверждаем увиденную ошибку)."""
        from app.features import browser_actions as ba
        sels = self.adapter["answer"]
        # Пробник баннера ошибки по контейнеру error_scope (см. ADAPTERS):
        # qwen рендерит «Oops!…» без content-классов — блоки ответа его не
        # видят, без пробника битый чат = вечный таймаут без самолечения.
        err_js = (_ERROR_SCOPE_JS % json.dumps(self.adapter["error_scope"])
                  if self.adapter.get("error_scope") else None)
        # Проба баннера-ошибки по странице (deepseek length limit; kimi
        # отказ по тарифу): div с хэш-классом вне блоков ответа
        banner_rx = ("/length limit reached.{0,40}start a new chat"
                     "|currently available to.{0,60}members"
                     "|servers? (is|are )?(currently )?overloaded"
                     "|no response available|try asking something else"
                     "|content wasn't generated"
                     "|out of free (messages|chats)|message limit|usage limit"
                     "|you'?ve reached your.{0,30}limit"
                     "|лимит[а]? (сообщений|запросов)|лимит исчерпан")
        if had_image:
            # Ошибки числа картинок — только когда картинка реально слалась,
            # иначе проза ответа («too many images» в тексте) даст ложный сбой
            banner_rx += ("|too many (images|files|attachments?)"
                          "|only \\d+ (image|file|attachment)"
                          "|слишком много (изображений|картинок|файлов)")
        banner_js = _PAGE_BANNER_ERROR_JS.replace("__RX__", banner_rx + "/i")
        # Откат отправки без баннера: композер снова содержит наш промпт
        # (kimi вернул черновик — сообщение отклонено, ждать бессмысленно)
        field_js = _CHAT_FIELD_JS % json.dumps(self.adapter["input"])
        rollback_seen = 0
        deadline = time.time() + timeout
        base_norm = " ".join((baseline_text or "").split()).strip().lower()
        anchored = bool(marker and self.adapter.get("user"))
        excl = self.adapter.get("answer_exclude")
        fast = self.channel in ("main", "vision") or self.channel in _FAST_POLL_CHANNELS
        poll_sec = FAST_POLL_SEC if self.channel in _FAST_POLL_CHANNELS else POLL_SEC
        stable_need = (0 if self.adapter.get("done_selector") else 1) \
            if fast else STABLE_POLLS
        prev = None
        stable = 0
        err_seen = 0
        na_seen = 0
        rl_seen = 0
        im_seen = 0
        tick = 0
        # Вкладка из реестра фоновых: её смерть (перезапуск Chrome пула,
        # _reset_raw_pool) видна по реестру/поколению пула за микросекунды. Без этой
        # проверки каждый тик падал внутри try ниже, исключение глушилось как
        # «пустой замер», и мёртвая вкладка «ждалась» до дедлайна (до 150 с)
        tab_token = self._snap_for(ba, tab_id)
        while time.time() < deadline:
            if self._tab_vanished(ba, tab_id, tab_token):
                raise _TabLost("вкладка пропала из реестра во время ожидания "
                               "ответа (перезапуск браузера?)")
            n, cur, cnt, done = 0, "", None, False
            try:
                if anchored:
                    cnt, cur, done = ba.answer_blocks_after(
                        host, tab_id, self.adapter["user"], sels,
                        marker, markdown=False, exclude=excl,
                        done_selector=self.adapter.get("done_selector"))
                    if cnt is None:
                        anchored = False  # якорь не нашёлся — baseline-путь
                if not anchored:
                    n = ba.count_blocks(host, tab_id, sels)
                    cur = ba.last_block_text(host, tab_id, sels,
                                             markdown=False, exclude=excl)
                    done = True  # baseline-путь: маркера завершения нет
            except Exception:
                n, cur, cnt, done = 0, "", None, False
            scope_txt = ""
            if err_js:
                try:
                    scope_txt = ba.eval_js(host, tab_id, err_js)
                except Exception:
                    scope_txt = ""
            banner_txt = ""
            # Полный скан страницы — самый тяжёлый пробник: не каждый тик.
            # НО как только баннер (лимит/картинки/ошибка/отказ) хоть раз
            # замечен — пробник форсируется на КАЖДЫЙ следующий тик, пока
            # подтверждение не дойдёт до порога или баннер не исчезнет:
            # иначе редкий скан (раз в BANNER_PROBE_EVERY тиков) физически
            # не может дать два ПОДРЯД идущих замера — промежуточный тик
            # молча читает пустой banner_norm и сбрасывает счётчик, порог
            # «2 подряд» не набирался бы никогда (без rl_seen/im_seen в этом
            # условии сайты без error_scope, у которых лимит/картинки видны
            # только через баннер страницы, вообще не детектировались бы).
            if banner_js and (tick % BANNER_PROBE_EVERY == 0
                              or err_seen or na_seen or rl_seen or im_seen):
                try:
                    banner_txt = ba.eval_js(host, tab_id, banner_js)
                except Exception:
                    banner_txt = ""
            tick += 1
            cur_norm = " ".join(cur.split()).strip().lower()
            scope_norm = " ".join(scope_txt.split()).strip().lower()
            banner_norm = " ".join(banner_txt.split()).strip().lower()
            is_new = bool(cnt) if anchored else \
                (n > baseline or (bool(cur_norm) and cur_norm != base_norm))
            # Готовый содержательный ответ ЭТОГО тика — новый, за пределами
            # «думания» и с маркером завершения. Используется ниже, чтобы не
            # выбрасывать его карантином сайта по сомнительному совпадению:
            # rl_hit/im_hit сами по себе уже не видят текст ответа (источник
            # — только баннер/error_scope), но сайт технически МОЖЕТ отдать
            # готовый ответ и показать баннер лимита одновременно (последнее
            # сообщение перед отсечкой) — в этом случае приоритет за ответом.
            ready = bool(cur_norm and cur_norm not in _THINKING_NOISE
                        and is_new and done)
            # Лимит сообщений free-тарифа (claude/chatgpt): чат не битый,
            # сайт не перегружен — временный лимит со (обычно) явным временем
            # восстановления. Карантин на распарсенный TTL, чат не трогаем.
            # Источник — ТОЛЬКО баннер/тост сайта (scope/banner), НЕ текст
            # ответа: сайт показывает лимит отдельным UI-элементом, а не
            # message-бабблом, а «message limit»/«лимит сообщений» — рабочая
            # тема разговора (пользователь спрашивает про лимиты сервисов) —
            # раньше это резало готовый ответ и слало сайт в часовой карантин.
            rl_hit = _classify_site_signal(_RATE_LIMIT_RES,
                                           scope_norm=scope_norm,
                                           banner_norm=banner_norm)
            if rl_hit is not None:
                rl_seen += 1
                # not ready — не топим уже готовый ответ карантином, даже
                # если баннер лимита подтверждён дважды подряд
                if rl_seen >= 2 and not ready:
                    raise _ChatRateLimited(
                        f"лимит сообщений: {rl_hit[:100]}",
                        ttl=_parse_reset_ttl(rl_hit))
            else:
                rl_seen = 0
            # Отказ по числу картинок (только если картинка слалась): два
            # замера подряд → _TooManyImages, обработку решает обёртка
            # get_response_with_image (trim/followup). Источник — тоже
            # только баннер/тост сайта, не текст ответа (та же причина, что
            # и у rl_hit: обсуждение лимитов картинок в прозе — не отказ).
            if had_image:
                im_hit = _classify_site_signal(_TOO_MANY_IMAGES_RES,
                                               scope_norm=scope_norm,
                                               banner_norm=banner_norm)
                if im_hit is not None:
                    im_seen += 1
                    if im_seen >= 2 and not ready:
                        raise _TooManyImages(f"сайт отверг число кадров: "
                                             f"{im_hit[:100]}")
                else:
                    im_seen = 0
            # Баннер ошибки сайта (битый чат): два подряд замера — не путаем
            # с мимолётным состоянием стриминга. allow_answer_text=True —
            # qwen/deepseek кладут свой баннер БЕЗ content-классов внутрь
            # того же контейнера, что и answer-селектор (см. комментарии у
            # _CHAT_ERROR_RES); cur_norm при этом всё равно должен остаться
            # коротким баннером (_SITE_SIGNAL_MAX_LEN) — длинный настоящий
            # ответ с похожими словами это условие не пройдёт.
            err_hit = _classify_site_signal(_CHAT_ERROR_RES,
                                            scope_norm=scope_norm,
                                            banner_norm=banner_norm,
                                            cur_norm=cur_norm,
                                            allow_answer_text=True)
            if err_hit is not None:
                err_seen += 1
                if err_seen >= 2:
                    raise _ChatBroken(f"сайт вернул ошибку: {err_hit[:100]}")
            else:
                err_seen = 0
            # Отказ от ответа на промпт (поисковый фильтр google AI Mode и
            # т.п.): не ошибка чата и не отказ сайта — быстрый None в фолбэк,
            # чат и карантин не трогаем (кейс 17.09: «no response available
            # for this search» жёг 150 с на сообщении). allow_answer_text=True
            # — здесь сайт САМ рендерит отказ КАК ответ ассистента (нет
            # отдельного баннера); cur_norm короткий-и-целиком-отказ
            # (_SITE_SIGNAL_MAX_LEN) отсекает случай, когда эти же слова —
            # просто часть настоящего длинного ответа.
            na_hit = _classify_site_signal(_NO_ANSWER_RES,
                                           banner_norm=banner_norm,
                                           cur_norm=cur_norm,
                                           allow_answer_text=True)
            if na_hit is not None:
                na_seen += 1
                if na_seen >= 2:
                    logger.info(f"[WebChat] {self.site}: сайт отказался "
                                f"отвечать на промпт («{na_hit[:60]}») — "
                                "фолбэк без ретрая")
                    return None
            else:
                na_seen = 0
            # Откат отправки: черновик вернулся в композер — сообщение
            # отклонено сайтом (перегрузка/тариф), ответа не будет
            if marker:
                try:
                    field_txt = ba.eval_js(host, tab_id, field_js)
                except Exception:
                    field_txt = ""
                if marker in " ".join(field_txt.split()).lower():
                    rollback_seen += 1
                    if rollback_seen >= 2:
                        raise _ChatRefused("сайт откатил отправку — черновик "
                                           "вернулся в поле ввода")
                else:
                    rollback_seen = 0
            # done — маркер завершения генерации (кнопки действий и т.п.):
            # без него плейсхолдер сбойной генерации залипал как «ответ»
            # (is_new/ready посчитаны выше — используются и rl_hit/im_hit)
            if ready:
                if stable_need == 0:
                    return self._final_markdown(ba, host, tab_id, sels,
                                                excl, marker, anchored,
                                                fallback=cur)
                if cur == prev:
                    stable += 1
                    if stable >= stable_need:
                        return self._final_markdown(ba, host, tab_id, sels,
                                                    excl, marker, anchored,
                                                    fallback=cur)
                else:
                    stable = 0
                    prev = cur
            time.sleep(poll_sec)
        logger.warning(f"[WebChat] {self.site}: ответ не дождались за "
                       f"{int(timeout)}с")
        return None

    def _final_markdown(self, ba, host: str, tab_id: int, sels, excl,
                        marker: Optional[str], anchored: bool,
                        fallback: str) -> str:
        """Финальное снятие ответа с markdown=True — один раз, когда текст
        стабилизировался (в тиках опроса читаем дешёвый plain: полная
        md-сериализация поддерева каждые POLL_SEC грузила рендерер).
        Не удалось — отдаём plain-вариант, он уже проверен."""
        return self._cut_tail(self._final_markdown_raw(
            ba, host, tab_id, sels, excl, marker, anchored, fallback))

    def _cut_tail(self, text: str) -> str:
        """Чистка ответа от служебного UI сайта: adapter["answer_strip"] —
        вырезать где угодно; adapter["answer_cut"] — первое совпадение любого
        шаблона и всё после него до конца."""
        if not text:
            return text
        for p in self.adapter.get("answer_strip") or ():
            text = re.sub(p, "", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        pats = self.adapter.get("answer_cut") or ()
        if not pats:
            return text
        cut = len(text)
        for p in pats:
            m = re.search(p, text)
            if m:
                cut = min(cut, m.start())
        return text[:cut].rstrip() if cut < len(text) else text

    def _final_markdown_raw(self, ba, host: str, tab_id: int, sels, excl,
                            marker: Optional[str], anchored: bool,
                            fallback: str) -> str:
        try:
            if anchored and marker:
                cnt, txt, _done = ba.answer_blocks_after(
                    host, tab_id, self.adapter["user"], sels, marker,
                    markdown=True, exclude=excl,
                    done_selector=self.adapter.get("done_selector"))
                if cnt and txt:
                    return txt
            else:
                txt = ba.last_block_text(host, tab_id, sels, markdown=True,
                                         exclude=excl)
                if txt:
                    return txt
        except Exception:
            pass
        return fallback
