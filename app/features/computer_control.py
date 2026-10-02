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
      risk_overrides:           # подтверждение по типу действия (needs_confirm)
        click: false            #   клик/скролл/безопасные клавиши — сразу
        navigate_new_domain: true  # новый домен (не из sites/allow_domains)
        type_text_safe_fields: false # поисковые поля — сразу; пароль/email/tel — всегда confirm
      allow_domains: []         # пусто = любые http(s); иначе whitelist доменов
      private_hosts: []         # скриншоты/текст этих страниц — не в облачные LLM
                                # (плюс встроенные: bank/pay/login/auth/id.…)
      apps:                   # ключ → что запускать (строка или per-OS)
        safari: Safari
        chrome: {darwin: "Google Chrome", win32: "chrome"}
      tasks:                    # ключ → shell-команда (строка или per-OS)
        музыка: {darwin: 'shortcuts run "Музыка"'}

Выключение: `computer_control: false` или `enabled: false` внутри dict —
во втором случае allowlist'ы сохраняются (так пишет веб-настройка фич).
"""

import contextlib
import contextvars
import functools
import json
import logging
import re
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from app.core.language import detect_language, user_language_line
from app.core.paths import data_dir

logger = logging.getLogger(__name__)

MARKER_RE = re.compile(r"\[(OPEN_URL|OPEN_APP|RUN_TASK):([^\]\n]{1,300})\]")
# Последняя фраза модели прямо перед маркером — её собственный вопрос/анонс
# действия; заменяется шаблоном confirm_question (скобки исключены, чтобы
# не съесть соседний маркер)
# Точка внутри слова («evil.com») — не конец фразы
_MARKER_LEAD_RE = re.compile(
    r"(?:[^.!?…\n\[\]]|\.(?=[^\s.\[]))*[.!?…]*[ \t]*"
    r"(?=\[(?:OPEN_URL|OPEN_APP|RUN_TASK):)")
# Человек сам просит открыть/запустить — только тогда маркер из хода с
# недоверенным текстом (страница/веб/OCR/файл/цитата) допустим
_MARKER_CMD_RE = re.compile(
    r"(?<![a-zа-яё])(?:открой|откройте|открыть|запусти|запустите|запустить|"
    r"включи|включите|перейди|зайди|выполни|поставь|"
    r"open|launch|start|run|play|go\s+to)(?![a-zа-яё])", re.IGNORECASE)

# Подтверждение живёт минуту: «да» спустя полчаса разговора — уже ответ на
# что-то другое, а не согласие на отложенный клик/открытие
PENDING_TTL_SEC = 60
# «Берусь за задачу «X»?» — дольше: «да» только запускает агента, каждый
# рискованный шаг прогона всё равно спрашивается отдельно (минуты на ответ
# хватало не всем — «да» через 2 минуты получало «подтверждение истекло»)
TASK_START_TTL_SEC = 600
# Список вариантов сайта читается дольше, чем «Открыть X?» — минуты мало
CHOICE_TTL_SEC = 180
# Сколько вариантов показывать в списке «какой сайт открыть?»
SITE_CHOICES_MAX = 5
# Сколько помним, что pending протух по TTL: голое «да» вдогонку получает
# честное «подтверждение истекло», а не уходит молча в болтовню
PENDING_EXPIRED_GRACE_SEC = 600

# Клавиши, чьё нажатие — обратимое взаимодействие со страницей (плеер,
# закрытие диалога); Enter/Tab/Backspace сюда не входят — могут отправить
# форму. Используется needs_confirm: безопасные клавиши подпадают под
# risk_overrides.click, остальные — всегда общий confirm
_RISK_SAFE_KEYS = frozenset({
    "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
    # «k» — штатный play/pause YouTube: resolve_key сам подставляет его
    # вместо капризного пробела, и без него то же действие внезапно
    # требовало подтверждения
    "m", "k", "Escape"})

# ── Рискованные подписи элементов (единый источник для needs_confirm,
# task_agent и scenario_manager) ──
# Финальный коммит заказа/формы — подтверждение человеком всегда (поверх
# risk_overrides и правила промпта «спроси перед необратимым шагом»). Узко:
# «купить» на карточке каталога обычно кладёт в корзину — его не трогаем
_COMMIT_RE = re.compile(
    r"оформить\s+заказ|подтвердить\s+заказ|заказать\s+сейчас|сделать\s+заказ|"
    r"отправить\s+заказ|забронировать|place\s+order|confirm\s+order|"
    r"submit\s+order|complete\s+(?:order|purchase)|buy\s+(?:it\s+)?now|book\s+now|"
    # «Купить сейчас» — русская «Buy now» (мгновенная покупка, не корзина)
    r"купить\s+сейчас|"
    # «Оформить» (но не «Оформить подписку» — это оплата, см. _PAYMENT_RE),
    # «Перейти к оформлению», «Купить в 1 клик», «Заказать» в начале
    # подписи, Amazon «Place your order», «Order now»
    r"(?<![а-яё])оформить(?!\s+подписк)(?![а-яё])|к\s+оформлению|"
    r"купить\s+в\s+(?:1|один|одн)\s*клик|^\s*заказать(?![а-яё])|"
    r"place\s+(?:your\s+)?order|order\s+now|"
    # Другие языки (de/fr/es/it/pt/pl/uk/tr): заказ, бронь, «к кассе». Голые
    # «купить» (Kaufen, Comprar, Acquista, Kup, Купити) — нет, как и
    # «Купить»: на карточке каталога они кладут в корзину
    r"(?<![a-zäöü])bestell(?:en|ung\s+(?:absenden|abschlie(?:ß|ss)en|"
    r"best[äa]tigen|aufgeben))(?![a-zäöü])|zur\s+kasse|"
    r"(?<![a-z])command(?:er|ez)(?![a-z])|"
    r"(?:valider|passer|confirmer|finaliser)\s+(?:la\s+|ma\s+|votre\s+)?"
    r"commande|(?<![a-z])r[ée]server(?![a-z])|"
    r"(?:realizar|confirmar|hacer|tramitar|finalizar|fazer|enviar)\s+"
    r"(?:el\s+|o\s+|mi\s+|meu\s+)?pedido|finalizar\s+(?:la\s+|a\s+)?compra|"
    r"terminer\s+(?:ma\s+|la\s+|votre\s+)?commande|je\s+commande|"
    r"(?<![a-z])reserve(?![a-z])|al[ıi][şs]veri[şs]i\s+tamamla|"
    r"(?<![a-z])(?:reservar|encomendar)(?![a-z])|"
    r"(?:conferma|invia|completa|procedi\s+(?:con\s+)?)\s*"
    r"(?:l['’]\s*|all['’]\s*)?ordine|(?<![a-z])ordina\s+(?:ora|adesso)|"
    r"(?<![a-z])prenota(?![a-z])|"
    r"zamawiam|z[łl][óo][żz]\s+zam[óo]wienie|potwierd[źz]\s+zam[óo]wienie|"
    r"(?<![a-ząćęłńóśźż])zam[óo]w(?![a-ząćęłńóśźż])|zarezerwuj|"
    r"(?<![а-яёіїє])замов(?:ити|ляю)(?![а-яёіїє])|"
    r"(?<![а-яёіїє])оформити(?![а-яёіїє])|підтвердити\s+замовлення|"
    r"забронювати|"
    r"sipari[şs]\w*\s+(?:ver|onayla|tamamla)\w*|sat[ıi]n\s+al|"
    r"rezervasyon\s+yap|"
    # Отмена заказа/брони/поездки/записи, вызов такси/курьера — тоже
    # необратимо (поездку спишут с сохранённой карты)
    r"(?<![а-яё])отменить\s+(?:мой\s+|этот\s+)?(?:заказ\w*|брон\w*|"
    r"поездк\w*|запис\w*)|\bcancel\s+(?:my\s+|this\s+)?(?:order|booking|"
    r"reservation|ride|trip|appointment)\b|"
    r"(?<![а-яё])(?:вызвать|заказать)\s+(?:такси|машину|курьер\w*)|"
    r"\brequest\s+(?:a\s+)?(?:ride|uber\w*|car|taxi|courier)\b|"
    r"\bterminer\s+(?:la\s+)?r[ée]servation|\bfinaliser\b|\bvalider\b",
    re.IGNORECASE)
# Мгновенная покупка — заказ сразу со списанием с сохранённой карты («Buy
# now», «Купить в 1 клик», Amazon «Place your order»). Для агента задач это
# оплата (передача человеку), а не коммит с «да»: одно «да» списывало деньги
_INSTANT_BUY_RE = re.compile(
    r"buy\s+(?:it\s+)?now|купить\s+сейчас|купить\s+в\s+(?:1|один|одн)\s*клик|"
    r"(?:1|one)[\s-]?click|в\s+(?:1|один)\s+клик|"
    r"place\s+(?:your\s+)?order|order\s+now|complete\s+(?:your\s+)?purchase|"
    r"confirm\s+(?:your\s+)?purchase|jetzt\s+kaufen|sofort\s+kaufen|"
    r"acheter\s+maintenant|comprar\s+(?:ahora|agora|ya)|"
    r"(?:acquista|compra)\s+ora|kup\s+teraz|купити\s+(?:зараз|в\s+(?:1|один)\s*клік)|"
    r"hemen\s+al|[şs]imdi\s+(?:sat[ıi]n\s+)?al",
    re.IGNORECASE)
# Отправка формы/сообщения/публикация кнопкой — необратимо уходит на сервер.
# Только в НАЧАЛЕ подписи: «Отправить», «Send message», но не «Способы
# отправки» и не «Отправленные» (папка почты)
_SUBMIT_LABEL_RE = re.compile(
    # «подписать» (документ), но не «Подписаться» на канал
    r"^\s*(?:отправить|отправь|опубликовать|подтвердить|подписать(?![а-яё])|"
    r"send(?![a-z])|submit\w*|publish\w*|post(?![a-z])|confirm(?![a-z])|"
    # запись, регистрация, ответ — тоже уходят на сервер
    r"записаться|запишите(?:сь)?|записать(?:ся)?|зарегистрир\w*|ответить|"
    r"reply(?![a-z])|register(?![a-z])|sign\s*up|"
    # de/fr/es/pt/it/pl/uk/tr
    r"(?:ab)?senden(?![a-z])|best[äa]tigen|ver[öo]ffentlichen|"
    r"envoyer|confirmer|publier|s['’]inscrire|"
    r"enviar|confirmar|publicar|registrar(?:se)?|inscribir(?:se)?|"
    r"invia(?:re)?(?![a-z])|conferma(?:re)?(?![a-z])|pubblica(?:re)?(?![a-z])|"
    r"wy[śs]lij|potwierd[źz]|opublikuj|zarejestruj|"
    r"надіслати|відправити|підтвердити|опублікувати|зареєструват\w*|"
    r"g[öo]nder|onayla|yay[ıi]nla|kay[ıi]t\s+ol|"
    # Заявка, голос, пост; разрешение доступа (OAuth) — тоже уходят на
    # сервер/открывают доступ. Голое «Allow all» (cookie) — нет
    r"подать\s+заявк\w*|apply\s+now|проголосовать|vote(?![a-z])|"
    r"tweet(?![a-z])|allow(?:\s+access)?\s*[.!]*$|grant\s+access|"
    r"разрешить(?:\s+доступ)?\s*[.!]*$|подключить(?:\s+услугу|\s+тариф\w*)?\s*[.!]*$|"
    r"активировать\s*[.!]*$|продлить\s*[.!]*$|renew\s*[.!]*$)",
    re.IGNORECASE)
# Платёжный текст. Узко, но с запасом в сторону «отрезать» (деньги — всегда
# за человеком):
#   * открытое «карт[аоые]» ловит «карточку» и «картошку» — поэтому формы
#     слова «карта» перечислены явно;
#   * голое «\bмир\b» ловит любое слово «мир» — «Мир Pay»/«mirpay» отдельно.
_PAYMENT_RE = re.compile(
    # платёж как действие/предмет
    r"оплат\w*|оплач\w*|\bплат[еёи]ж\w*|\bплатить\b|\bзаплат\w*|"
    # бренды и способы оплаты
    r"visa|mastercard|maestro|\bpay\b|apple\s?pay|google\s?pay|samsung\s?pay|"
    r"\bmir\s?pay\b|\bmirpay\b|сбербанк|\bсбп\b|тинькофф|альфа-?банк|"
    r"\bcheckout\b|\bcvv\b|\bcvc\b|"
    # английские формы: подписи на англоязычных сайтах и шаги, которые LLM
    # обобщения (промпт на английском) может переписать по-английски;
    # «card» — как одиночное «карта»: в спорном случае шаг лучше отрезать
    r"\bpayments?\b|\bpaying\b|\bbilling\b|\bcards?\b|"
    # деньги со счёта: перевод/вывод (с суммой или «деньги/средства/на
    # карту» — голое «Перевести» — это перевод текста), пополнение,
    # пожертвование, платная подписка
    r"(?<![а-яё])(?:перевести|вывести)\s+(?:\d|деньг|средств|на\s+(?:карт|сч[её]т)"
    r"|по\s+номеру)|(?<![а-яё])пополни(?:ть|те)?(?![а-яё])|"
    r"пополнени[ея]\s+(?:баланса|сч[её]та|кошелька|карты)|"
    r"(?<![а-яё])(?:пожертвова\w*|задонат\w*)|оформить\s+подписк\w*|"
    r"\bdonat(?:e|ion)s?\b|\bwithdraw\w*|\btransfer\s+(?:money|funds)\b|"
    r"\bsend\s+money\b|\btop[\s-]?up\b|\b(?:wire|bank)\s+transfer\b|"
    # «Transfer» в начале подписи — перевод денег (кроме файлов/данных/
    # владения); «Перевод» — только с денежным контекстом: голое «Перевод» —
    # это и переключатель перевода текста (см. _BARE_TRANSFER_RE)
    r"^\s*transfer(?![a-z])(?!\s+(?:files?|data|photos?|ownership|domain|"
    r"call|chat|to\s+(?:a\s+)?(?:new\s+)?(?:device|phone)))|"
    r"(?<![а-яё])перевод(?:а|ы|ом)?\s+(?:\d[\d\s.,]*\s*(?:₽|руб|р\b|\$|€|usd|"
    r"eur|rub)|деньг|денег|средств|на\s+(?:карт|сч[её]т)|"
    r"по\s+(?:номеру|телефону|реквизитам)|между\s+(?:сч|сво)|клиенту|"
    r"в\s+другой\s+банк)|"
    # закрытие вклада/депозита — деньги уходят со вклада («Досрочно закрыть
    # вклад», «Закрытие вклада»); «вкладку» (вкладку браузера) не ловит
    r"(?<![а-яё])(?:закрыть|закрой(?:те)?|закрыти[ея]|расторгнуть|"
    r"расторжени[ея])\s+(?:(?:мой|свой|этот|my|this)\s+)?"
    r"(?:вклад(?:а|у|ом|е|ы|ов)?|депозит(?:а|у|ом|е|ы|ов)?)(?![а-яё])|"
    r"\bclose\s+(?:(?:my|your|this|the)\s+)?(?:deposit|savings)\b|"
    # формы слова «карта» (но не «карточка», «картинка», «картошка»)
    r"\bкарт(?:а|ы|е|у|ой|ою|ам|ами|ах)\b|"
    # способы оплаты одним словом (\bpay\b их не ловит) и рассрочка
    r"paypal|sber\s?pay|сбер\s?(?:пэй|пей|pay)|tinkoff\s?pay|t-pay|"
    r"yoo?money|юmoney|ю\s?касс\w*|yookassa|klarna|afterpay|"
    r"(?<![а-яё])долями(?![а-яё])|(?<![а-яё-])сплит(?:ом)?(?![а-яё-])|"
    # de/fr/es/pt/it/pl/uk/tr: «оплатить/оплата»
    # Глаголы и фразы «заплатить», а не существительные: «Métodos de pago»,
    # «Zahlungsart», «Metody płatności» — пункты меню, не платёж
    r"bezahl\w*|zahlungspflichtig|kostenpflichtig\s+bestellen|"
    r"(?<![a-z])payer(?![a-z])|proc[ée]der\s+au\s+paiement|"
    r"(?<![a-z])pagar(?![a-z])|(?<![a-z])paga\s+(?:ora|adesso)|"
    r"(?:realizar|efectuar|efetuar|confirmar|procesar|completar)\s+"
    r"(?:el\s+|o\s+)?pag(?:o|amento)|"
    # «płacę» — только с диакритикой: «place» английского не ловим
    r"zap[łl]a[ćc]\w*|zap[łl]at[yę]|(?<![a-z])(?:płac[ęe]|plac[ę])(?![a-z])|"
    r"(?<![а-яёіїє])сплат\w*|"
    r"(?<![a-zçğıöşü])öde(?:me\w*|yin|yiniz)?(?![a-zçğıöşü])", re.IGNORECASE)
# «Карта» бывает и географической — единственное исключение, явным списком
# сочетаний, а не смягчением правила выше
_MAP_SENSE_RE = re.compile(
    r"\bкарт(?:а|ы|е|у|ой|ою|ам|ами|ах)\s+"
    r"(?:сайта|города|метро|мира|местности|проезда|памяти|"
    r"маршрут\w*|окрестност\w*)\b", re.IGNORECASE)
# Голая кнопка «Перевод/Перевести/Переводы» — платёж только на денежном
# хосте (банк/кошелёк/платёжка): на обычном сайте это перевод текста
_BARE_TRANSFER_RE = re.compile(
    r"^\s*(?:перевод|переводы|перевести|перевод\s+денег)\s*[.!…]*\s*$",
    re.IGNORECASE)
_MONEY_HOST_RE = re.compile(
    r"bank|tinkoff|tbank|qiwi|yoomoney|paypal|revolut|wise\.com|"
    r"(?:^|\.)(?:pay|payment|payments|wallet|billing|checkout)\.",
    re.IGNORECASE)
# Закрытие аккаунта/профиля/счёта — разрушительно, как «Удалить аккаунт».
# Голое «Закрыть» (диалог/попап) сюда не попадает: нужен объект-аккаунт
_ACCOUNT_CLOSE_RE = re.compile(
    r"(?<![а-яё])(?:закрыть|закрой|close)\s+(?:(?:мой|свой|my|your|this)\s+)?"
    r"(?:аккаунт\w*|учётн\w*\s+запис\w*|учетн\w*\s+запис\w*|профил[ьяюе]\w*|"
    r"сч[её]т(?:а|у|ом)?(?![а-яё])|account|profile)",
    re.IGNORECASE)
# Покупка по ПОДПИСИ кнопки (не по вводимому тексту: поисковый запрос
# «купить айфон за 50 000 ₽» — не оплата, поэтому не в _PAYMENT_RE).
# Цена в подписи + глагол покупки/подписки/продления — деньги уходят кликом
_PRICE_RE = re.compile(
    r"[$€£₽¥₴₸]\s*\d|\d[\d\s  .,]*\s*(?:₽|\$|€|£|¥|₴|₸|"
    r"руб(?:\.|л\w*|\b)|р\.|(?:usd|eur|rub|gbp|uah|kzt|грн|тг)(?![a-zа-яё]))",
    re.IGNORECASE)
_PURCHASE_VERB_RE = re.compile(
    r"(?<![a-zа-яё])(?:"
    r"buy|purchase|pay|subscribe|order|pre-?order|renew|rent|upgrade|get|"
    r"unlock|start|join|donate|tip|try|continue|proceed|checkout|check\s+out|"
    r"go\s+(?:pro|premium|plus)|add\s+funds|top\s+up|"
    r"купить|купи(?:те)?|оплатить|оплати(?:те)?|подписаться|подпишись|"
    r"подпишитесь|продлить|продли(?:те)?|заказать|закажи(?:те)?|арендовать|"
    r"арендуй(?:те)?|взять\s+(?:в\s+аренду|напрокат)|приобрести|приобретите|"
    r"оформить|подключить|подключи(?:те)?|активировать|получить|"
    r"разблокировать|открыть\s+доступ|перейти\s+на|попробовать|пополнить|"
    r"продолжить|задонатить)(?![a-zа-яё])", re.IGNORECASE)
# Покупка без цены в подписи: платный объект при глаголе покупки/продления,
# «Buy»/«Rent» целиком (магазины приложений/кино: мгновенная покупка).
# Голое «Купить» — нет: на карточке каталога оно кладёт в корзину
_PURCHASE_LABEL_RE = re.compile(
    r"^\s*(?:buy|purchase|rent|pre-?order)\s*[.!]*\s*$|"
    r"(?<![a-z])(?:buy|purchase)\s+(?:(?:a|the|this|now|more)\s+)?(?:"
    r"subscriptions?|premium|pro|plus|membership|access|licen[cs]e|credits?|"
    r"coins?|gems?|tokens?|tickets?|gift\s+cards?|full\s+version|game|album|"
    r"movie|season|episode|book|app|upgrade)(?![a-z])|"
    r"(?<![a-z])(?:renew|extend)\s+(?:(?:my|your|the)\s+)?(?:subscription|"
    r"membership|plan|licen[cs]e|premium|domain|hosting)(?![a-z])|"
    r"(?<![a-z])upgrade\s+(?:to\s+|now|plan|account|(?:my|your)\s+plan)|"
    r"(?<![a-z])(?:subscribe|join)\s+(?:to\s+|for\s+)?(?:premium|pro|plus|"
    r"membership)(?![a-z])|"
    r"(?<![a-z])start\s+(?:(?:my|your)\s+)?(?:free\s+)?(?:trial|"
    r"subscription|membership)(?![a-z])|"
    r"(?<![a-z])rent\s+(?:now|for|movie|this|it)(?![a-z])|"
    r"(?<![а-яё])(?:купить|купите|приобрести)\s+(?:подписк\w*|премиум\w*|"
    r"доступ\w*|тариф\w*|pro|plus|билет\w*|лицензи\w*|полную\s+версию|игру|"
    r"фильм|книгу|абонемент\w*|сертификат\w*)|"
    r"(?<![а-яё])(?:продлить|продлите|продли)\s+(?:подписк\w*|тариф\w*|"
    r"доступ\w*|лицензи\w*|премиум\w*|аренд\w*|абонемент\w*|план\w*|"
    r"услуг\w*|домен\w*|хостинг\w*)|"
    r"(?<![а-яё])(?:арендовать|взять\s+(?:в\s+аренду|напрокат))(?![а-яё])|"
    r"(?<![а-яё])(?:подключить|перейти\s+на|оформить)\s+(?:премиум\w*|"
    r"платн\w*|тариф\w*|pro|plus)(?![а-яёa-z])|"
    r"(?<![а-яё])(?:попробовать|начать)\s+(?:бесплатн\w*\s+)?(?:пробн\w*\s+)?"
    r"(?:период|подписк\w*)", re.IGNORECASE)


def _label_purchase(label: str) -> bool:
    """Подпись кнопки — покупка/подписка/продление за деньги."""
    s = " ".join(str(label or "").split())
    if not s:
        return False
    if _PURCHASE_LABEL_RE.search(s):
        return True
    return bool(_PRICE_RE.search(s) and _PURCHASE_VERB_RE.search(s))


# Латинские двойники кириллицы и обратно: «Удaлить» (латинская a),
# «Dеlete» (кириллическая е) — подпись со страницы недоверенная
_LAT2CYR = str.maketrans("AaBCcEeHKMOoPpTXxYy", "АаВСсЕеНКМОоРрТХхУу")
_CYR2LAT = str.maketrans("АаВСсЕеНКМОоРрТХхУу", "AaBCcEeHKMOoPpTXxYy")
_CYR_CH_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_LAT_CH_RE = re.compile(r"[a-z]", re.IGNORECASE)


_ZW_SPACES = frozenset("​‌‍⁠﻿")


def _risk_text(text) -> str:
    """Подпись для словаря риска: NFKC, без невидимых символов (мягкий
    перенос «Уда\\xadлить», zero-width), слово со смесью алфавитов — к
    алфавиту большинства его букв."""
    s = unicodedata.normalize("NFKC", str(text or ""))
    # Невидимые разделители слов (zero-width space/joiner, word joiner, BOM)
    # — пробел: «Оформить​заказ» иначе склеился бы в одно слово мимо
    # словаря; прочие Cf (мягкий перенос внутри слова) — убрать
    s = "".join(" " if ch in _ZW_SPACES else ch for ch in s
                if ch in _ZW_SPACES or unicodedata.category(ch) != "Cf")
    out = []
    for w in s.split(" "):
        nc, nl = len(_CYR_CH_RE.findall(w)), len(_LAT_CH_RE.findall(w))
        if nc and nl:
            w = w.translate(_LAT2CYR if nc >= nl else _CYR2LAT)
        out.append(w)
    return " ".join(out)


# Кнопка-согласие без своего смысла — смысл в тексте окна вокруг неё:
# «Подтвердите заказ на 1 299 ₽ [ОК]», «Удалить аккаунт? [Да]», «Списать
# 1 299 ₽ с карты? [Продолжить]»
_GENERIC_CONFIRM_RE = re.compile(
    r"^\s*(?:ок|ok|okay|да|yes|ага|продолжить|continue|далее|next|готово|"
    r"done|принять|принимаю|accept|согласен|согласна|agree|хорошо|sure|"
    r"понятно|proceed|go|верно|вс[её]\s+верно|подтверждаю|ja|oui|s[ií]|"
    r"weiter|continuer|continuar|continua|dalej|tamam|evet)\s*[!.…]*\s*$",
    re.IGNORECASE)
_DIALOG_CHARGE_RE = re.compile(
    r"спис\w*|автоплат\w*|charge|debit|с\s+(?:вашей\s+)?карты|"
    r"from\s+(?:your\s+)?card", re.IGNORECASE)
_DIALOG_DESTRUCTIVE_RE = re.compile(
    r"удал\w*|сотр\w*|стер\w*|очист\w*|выйти|выход\w*|"
    r"\bdelete|\bremove|\berase|\bclear\b|sign\s*out|log\s*out", re.IGNORECASE)
_DIALOG_COMMIT_RE = re.compile(
    r"заказ\w*|оформ\w*|отправ\w*|опубликов\w*|подтверд\w*|брон\w*|запис\w*|"
    r"подпис\w*|order|send|publish|confirm|book|submit|recipients|"
    r"получател\w*|subscri\w*", re.IGNORECASE)


# «Да»-подобная кнопка — ответ на вопрос окна/блока; «Далее/Продолжить» —
# шаг формы (на оформлении их решает гейт submit-кнопок, A3)
_YES_LIKE_RE = re.compile(
    r"^\s*(?:ок|ok|okay|да|yes|ага|sure|верно|вс[её]\s+верно|подтверждаю|"
    r"ja|oui|s[ií]|evet|tamam)\s*[!.…]*\s*$", re.IGNORECASE)
# Текст про cookie/согласие на обработку данных — не про заказ, даже если
# там «in order to», «оформлять заказы» или «payments»
_COOKIE_CTX_RE = re.compile(r"cookie|куки", re.IGNORECASE)
# Опасно и в cookie-тексте: удаление аккаунта, выход, подтверждение/
# отправка заказа («Удалить аккаунт? Мы используем cookie»). Просто «удалить
# cookie», «отправки уведомлений», «при выходе» — нет
_DIALOG_HARD_RE = re.compile(
    r"удал\w*\s+(?:ваш\w*\s+|мой\s+|свой\s+)?(?:аккаунт|профил|учётн|учетн)|"
    r"\bdelete\s+(?:your\s+|my\s+)?(?:account|profile)|выйти\s+из|"
    r"\blog\s*out|\bsign\s*out|закрыть\s+(?:аккаунт|сч[её]т)|"
    r"подтверд\w*\s+(?:ваш\w*\s+)?заказ|\bconfirm\s+(?:your\s+)?order|"
    r"оформить\s+заказ|\bplace\s+(?:your\s+)?order|"
    r"отправить\s+(?:ваш\w*\s+)?(?:заявк|письм|сообщ|заказ|анкет|отзыв)|"
    r"опубликова\w*|\bpublish|\bsubmit\s+(?:your\s+)?application|"
    r"\bsend\s+(?:to|the|this|your)\b", re.IGNORECASE)
# Списание денег: глагол списания рядом с суммой/картой (не «Список товаров»,
# не «Списать 120 бонусов», не «free of charge» / «Delivery charge: $0»)
_STRONG_CHARGE_RE = re.compile(
    r"(?<![а-яё])спис(?:ать|ание|ано|аны|ывается|ываем|ывать|ем|ут|ала|ал)\s+"
    r"(?!бонус|балл)[^.!?]{0,40}?(?:\d[\d\s\u00a0.,]*\s*(?:₽|руб|\$|€|£)|"
    r"карт[аыуе]|\*\d{2,4})|"
    r"\d[\d\s\u00a0.,]*\s*(?:₽|руб|\$|€|£)[^.!?]{0,40}?"
    r"(?:будет\s+списан|спиш)|"
    r"с\s+(?:вашей\s+)?карты\s+(?:\*?\d|будет|спиш)|автоплат\w*|автопродл\w*|"
    r"\bcharge[sd]?\s+(?:you\s+)?(?:[$€£]\s*[1-9]|(?:to\s+)?your\s+card)|"
    r"\bdebit(?:ed)?\s+(?:from\s+)?your\s+(?:card|account)", re.IGNORECASE)
_AMOUNT_RE = re.compile(
    r"\d[\d\s\u00a0.,]*\s*(?:₽|руб|\$|€|£)|[$€£]\s*\d", re.IGNORECASE)


# Переход к странице оформления — не создание заказа и не оплата: «К
# оформлению заказа», «Перейти к оформлению», «Proceed to checkout», «Zur
# Kasse». Подпись целиком (с суммой или «Оформить заказ» — как раньше)
_CHECKOUT_STEP_RE = re.compile(
    r"(?:перейти\s+)?к\s+оформлению(?:\s+заказа)?|"
    r"(?:proceed|go|continue)\s+to\s+(?:the\s+)?checkout|"
    r"zur\s+kasse(?:\s+gehen)?", re.IGNORECASE)


def checkout_step_label(action: Optional[dict]) -> bool:
    """Клик — переход к оформлению (_CHECKOUT_STEP_RE): подпись и aria/title,
    если они есть, — только такие. Заказ создаёт финальная кнопка на
    странице оформления — её гейт и спрашивает, платит человек."""
    if not isinstance(action, dict) or action.get("kind") != "click":
        return False
    labs = [re.sub(r"^\W+|\W+$", "", " ".join(str(action.get(k) or "").split()))
            for k in ("element", "aria", "title")]
    labs = [x for x in labs if x]
    return bool(labs) and all(_CHECKOUT_STEP_RE.fullmatch(x) for x in labs)


def dialog_risk(label, context, in_dialog: bool = True) -> Optional[str]:
    """Кнопка-согласие («ОК», «Да», «Продолжить») — риск по тексту окна/
    блока вокруг неё: 'payment' | 'destructive' | 'commit' | None. Сама
    подпись нейтральна, и гейт по одной подписи пропускал «ОК» в «Подтвердите
    заказ на 1 299 ₽». in_dialog=False — кнопка в блоке страницы, не в
    окне: «Да/ОК» — по полному правилу, «Далее/Продолжить» — только
    удаление (оформление по шагам не превращается в «оформляю заказ?»)."""
    lab = _risk_text(label)
    if not _GENERIC_CONFIRM_RE.match(lab):
        return None
    t = _risk_text(context)
    if not t.strip():
        return None
    strong = _STRONG_CHARGE_RE.search(t)
    if _COOKIE_CTX_RE.search(t) and not strong and not _AMOUNT_RE.search(t) \
            and not _DIALOG_HARD_RE.search(t):
        return None  # «Принять» в cookie-баннере
    full = in_dialog or bool(_YES_LIKE_RE.match(lab))
    if strong or (full and (_is_payment(t) or _label_purchase(t))):
        # Списание с карты — оплата и для «Продолжить/Готово» вне окна
        return "payment"
    if _DIALOG_DESTRUCTIVE_RE.search(t) or _ACCOUNT_CLOSE_RE.search(t):
        return "destructive"
    if full and (_DIALOG_COMMIT_RE.search(t) or _COMMIT_RE.search(t)):
        return "commit"
    return None


def _is_payment(text: str) -> bool:
    """Про оплату ли этот текст (цель клика, подпись поля, значение).
    Единственная точка решения «это платёжный шаг» — needs_confirm, обрезка
    сценария, граница оплаты агента."""
    s = _risk_text(text)
    # Географическую карту вычёркиваем и смотрим, осталось ли платёжное:
    # «карта города» — нет, «карта города и оплата картой» — да
    s = _MAP_SENSE_RE.sub(" ", s)
    return bool(_PAYMENT_RE.search(s))


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
#
# Это граница безопасности, поэтому голый поиск да-/нет-слова по всему тексту
# не годится: «не открывай» содержит и отрицание, и голое «открывай», и
# отрицание должно жить в пределах своей клаузы, а не всего сообщения — иначе
# «давай не будем», «подожди, не открывай» уходят в YES. Общий принцип
# «сомнение = UNKNOWN, отрицание побеждает»:
#   1. текст режется на клаузы по пунктуации (запятая/точка/etc. — граница
#      смысловых кусков), да- и нет-сигналы ищутся ВНУТРИ каждой клаузы;
#   2. если в клаузе есть нет-слово ИЛИ отрицание рядом с да-словом/глаголом
#      согласия — вся клауза NO, и NO любой клаузы выигрывает у YES остальных
#      (не нужен список фраз-заплаток на каждое «не X»);
#   3. YES — только если во всём сообщении нет ни одной NO-клаузы, оно не
#      вопрос («?») и достаточно короткое (да-слово — это и есть ответ,
#      а не случайное слово в длинном тексте/OCR).
#   4. YES — только для ГОЛОГО согласия: кроме да-слов в реплике допустимы
#      лишь слова-наполнители (пожалуйста/please/ну…), имя персоны, знаки и
#      эмодзи. «давай лучше посмотрим котиков», «ок, а теперь нажми войти» —
#      не согласие на отложенное действие, а новая реплика/команда.
_YES_WORDS = frozenset({
    "да", "давай", "давайте", "ок", "окей", "оке", "ok", "okay", "yes", "yeah",
    "yep", "yup", "конечно", "поехали", "угу", "ага", "открывай", "запускай",
    "включай", "go", "sure", "хорошо", "ладно", "подтверждаю", "confirm",
    "confirmed", "действуй", "делай", "выполняй", "жми", "нажимай", "вперед",
    "вперёд", "го",
})
# Наполнители голого согласия: сами по себе не ответ, но и не «другое
# содержание» — «да, пожалуйста», «ну давай», «go ahead», «yes please»
_YES_FILLER = frozenset({
    "пожалуйста", "плиз", "please", "pls", "plz", "ну", "же", "ж", "уж",
    "тогда", "так", "можно", "конечно", "ahead", "do", "it", "then", "sure",
    "уже", "быстрее", "скорее", "именно", "верно", "точно", "absolutely",
    "of", "course",
})
# «хватит»/«stop» — тоже отказ: при живом pending это и «не выполняй»,
# и (при идущем листании) его остановка, см. stop_scroll_if_active
_NO_WORDS = frozenset({
    "нет", "отмена", "отменяй", "стоп", "хватит", "no", "nope", "cancel",
    "stop",
})
# Слова, которые сами по себе не ответ («надо» голое — не «да»), но под
# отрицанием складываются в отказ: «не надо», «не нужно», «не хочу», «не буду».
_DESIRE_WORDS = frozenset({"надо", "нужно", "хочу", "буду", "готов", "согласен"})
_NEGATORS = frozenset({"не", "not", "never", "никогда"})
# Клаузы режем по пунктуации/паузам — «подожди, не открывай» это ДВЕ клаузы,
# отрицание не должно «выходить» за пределы своего куска на чужой да-сигнал.
_CLAUSE_SPLIT_RE = re.compile(r"[.,!?;:…—–]+")
_WORD_RE = re.compile(r"[a-zа-яё]+")
_DONT_RE = re.compile(r"\bdon[’']?t\b", re.IGNORECASE)
# YES — только для короткой реплики: да-слово должно быть самим ответом,
# а не случайным словом внутри длинного текста (например OCR с фото).
_MAX_YES_WORDS = 10

_KIND_BY_MARKER = {"OPEN_URL": "url", "OPEN_APP": "app", "RUN_TASK": "task"}


def _clause_verdict(clause: str) -> Optional[str]:
    """YES/NO/DOUBT/None для одной клаузы (без учёта длины всего сообщения/«?»).
    DOUBT — отрицание есть, но относится не к известному да-слову («не сейчас»,
    «не сегодня»): само по себе YES не даёт, но не даёт и уверенности другой
    клаузе с «да» — итог по всему сообщению снижается до UNKNOWN."""
    tokens = _WORD_RE.findall(clause.lower())
    if not tokens:
        return None
    has_no = any(t in _NO_WORDS for t in tokens)
    if has_no:
        return "NO"
    has_neg = any(t in _NEGATORS for t in tokens)
    has_yes = any(t in _YES_WORDS for t in tokens)
    if has_neg and (has_yes or any(t in _DESIRE_WORDS for t in tokens)):
        return "NO"
    if has_yes:
        return "YES"
    if has_neg:
        return "DOUBT"
    return None


_ANY_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _bare_yes(text: str, names=None) -> bool:
    """Реплика — голое согласие: каждое слово (включая цифры) — да-слово,
    наполнитель или имя персоны (names — обращения к ней), хотя бы одно —
    да-слово. Знаки и эмодзи в \\w не входят и не мешают."""
    extra = set()
    for n in names or ():
        extra.update(t.lower() for t in _ANY_WORD_RE.findall(str(n or "")))
    tokens = [t.lower() for t in _ANY_WORD_RE.findall(text)]
    if not tokens or not any(t in _YES_WORDS for t in tokens):
        return False
    return all(t in _YES_WORDS or t in _YES_FILLER or t in extra
               for t in tokens)


# Голая команда остановки — одно правило для «стоп» до лока хода (бот) и
# отмены задачи агента (раньше у агента был свой список: «стой», «abort»,
# «останови», «отмени всё» задачу не отменяли)
STOP_CMD_RE = re.compile(
    r"^\s*(?:отмена|отмени(?:\s+(?:задачу|это|всё|все))?|стоп|стой|хватит|"
    r"прекрати|брось|не\s+надо|останови(?:сь)?|остановить|"
    r"cancel|stop|abort|halt)"
    # «хватит листать» / «stop scrolling» / «stop the scroll» — та же остановка
    r"(?:\s+(?:листать|листание|прокрутку|скроллить|мотать|"
    r"(?:the\s+)?scroll(?:ing)?))?\s*[.!…]*\s*$"
    r"|^\s*enough\s+scrolling\s*[.!…]*\s*$", re.IGNORECASE)
# «не надо»/«хватит» в ответ на вопрос или «да/нет» агента — это «нет» на
# вопрос, а не отмена всей задачи
SOFT_STOP_RE = re.compile(r"^\s*(?:не\s+надо|хватит)\s*[.!…]*\s*$",
                          re.IGNORECASE)


def classify_confirmation(text: str, names=None) -> str:
    # Ответ пользователя на «выполнить действие?» → YES | NO | UNKNOWN.
    # names — как обращаются к персоне («Коннор, да») — не «другое содержание»
    if not text:
        return "UNKNOWN"
    norm = _DONT_RE.sub("do not", text)
    overall = None
    doubt = False
    for clause in _CLAUSE_SPLIT_RE.split(norm):
        v = _clause_verdict(clause)
        if v == "NO":
            return "NO"  # отрицание побеждает сразу, независимо от прочих клауз
        if v == "YES":
            overall = "YES"
        elif v == "DOUBT":
            doubt = True
    if overall != "YES" or doubt:
        return "UNKNOWN"  # да-сигнал есть, но где-то рядом необъяснённое «не» — не уверены
    if "?" in text:
        return "UNKNOWN"  # «да?», «...да или нет?» — вопрос, не ответ
    if len(text.split()) > _MAX_YES_WORDS:
        return "UNKNOWN"  # длинный текст (в т.ч. составной ввод с OCR) — да-слово внутри не в счёт
    if not _bare_yes(norm, names):
        return "UNKNOWN"  # «ок, а теперь нажми войти» — новая команда, не согласие
    return "YES"


# Ответ номером на список вариантов («2», «второй», «открой 2-й», «second»):
# в реплике ровно один номер, остальное — наполнители выбора
_CHOICE_ORDINALS = {
    **{w: 1 for w in ("первый", "первая", "первое", "первую", "первого",
                      "первой", "first", "1st")},
    **{w: 2 for w in ("второй", "вторая", "второе", "вторую", "второго",
                      "second", "2nd")},
    **{w: 3 for w in ("третий", "третья", "третье", "третью", "третьего",
                      "третьей", "third", "3rd")},
    **{w: 4 for w in ("четвертый", "четвертая", "четвертое", "четвертую",
                      "четвертого", "четвертой", "fourth", "4th")},
    **{w: 5 for w in ("пятый", "пятая", "пятое", "пятую", "пятого", "пятой",
                      "fifth", "5th")},
}
_CHOICE_FILLER = frozenset({
    "номер", "вариант", "ссылку", "ссылка", "сайт", "открой", "открывай",
    "давай", "выбираю", "беру", "мне", "нужен", "нужна", "нужно", "тот",
    "та", "то", "ту", "й", "я", "е", "ю", "го", "ой",
    "number", "option", "link", "site", "open", "the", "one", "take",
})


def parse_choice(text: Optional[str], names=None) -> Optional[int]:
    """Номер варианта (с 1) из ответа на список или None — реплика не
    выбор. Как classify_confirmation: сомнение = не выбор — вопрос,
    отрицание («не 2»), второй номер или постороннее слово дают None.
    names — обращения к персоне («Коннор, 2»). Диапазон не проверяем:
    «7» при пяти вариантах вызывающий переспрашивает сам."""
    if not text or "?" in text or len(text.split()) > 6:
        return None
    extra = set()
    for n in names or ():
        extra.update(t.lower() for t in _ANY_WORD_RE.findall(str(n or "")))
    num = None
    for t in _ANY_WORD_RE.findall(_fold_diacritics(text.lower())):
        v = int(t) if t.isdigit() and len(t) <= 2 else _CHOICE_ORDINALS.get(t)
        if v is not None:
            if num is not None:
                return None  # «1 или 2» — не выбор
            num = v
        elif t in _NO_WORDS or t in _NEGATORS:
            return None
        elif t not in _CHOICE_FILLER and t not in _YES_FILLER \
                and t not in extra:
            return None
    return num if num else None


def _fold_diacritics(t: str) -> str:
    """Снятие диакритики с ЛАТИНИЦЫ (café → cafe) и ё → е.
    Остальную кириллицу не трогаем: NFKD разлагает и «й» (и + бреве), а
    склейка й→и ломает основы («действие» ≠ «действий»)."""
    out = []
    for ch in t:
        if ch == "ё":
            out.append("е")
            continue
        d = unicodedata.normalize("NFKD", ch)
        if len(d) >= 2 and "a" <= d[0] <= "z" \
                and all(unicodedata.combining(c) for c in d[1:]):
            out.append(d[0])
        else:
            out.append(ch)
    return "".join(out)


# Апострофы разных начертаний — к одному ASCII: «l'oreal»/«l’oreal»/«lʼoreal» —
# одна и та же цель (умные кавычки на странице, прямая на клавиатуре
# пользователя). Общий набор — тот же, что зеркалит JS-нормализатор
# (_VPC_NORM_JS в browser_actions.py — держать в согласии).
_APOSTROPHES = ("’", "ʼ")


def _norm_match(s) -> str:
    """Нормализация для матчинга цели: дефисы/тире → пробелы, апострофы —
    к одному виду, lower, сжатие пробелов. «айс-ти» в команде и «Айс ти» на
    странице — одна и та же цель.
    Диакритика снимается: «cafe» в команде = «Café» в названии видео —
    пользователь редко повторяет акценты."""
    t = str(s or "").lower()
    for ch in ("-", "‑", "–", "—"):
        t = t.replace(ch, " ")
    for ch in _APOSTROPHES:
        t = t.replace(ch, "'")
    return " ".join(_fold_diacritics(t).split())


def _page_key(url: str) -> str:
    """Ключ «та же страница» для сверки перед повтором действия: хост +
    путь + query без фрагмента (якорь/хэш прокрутки — та же страница) и
    хвостового слэша."""
    p = urlparse(str(url or ""))
    return ((p.hostname or "").lower() + (p.path or "").rstrip("/")
            + ("?" + p.query if p.query else ""))


def _word_in(word: str, hay: str) -> bool:
    """Слово/основа в тексте: совпадение с НАЧАЛА слова, а не подстрока внутри
    чужого («айс» ≠ «гавАЙСкая»). hay — уже через _norm_match."""
    return bool(word) and re.search(
        r"(?<![a-z0-9а-яё])" + re.escape(word), hay) is not None


_NEG_WORD_RE = re.compile(r"(?<![a-z0-9а-яё])не\s+([a-z0-9а-яё]+)")


def _negated_word_in(word: str, hay: str) -> bool:
    # Слово в тексте под отрицанием: «не нравится».
    return bool(word) and re.search(
        r"(?<![a-z0-9а-яё])не\s+" + re.escape(word), hay) is not None


def _strip_negated(hay: str, goal: str) -> str:
    """Убирает из hay слова, стоящие под отрицанием «не», если в самой цели
    отрицания нет: «нравится» ≠ кнопка «Поставить отметку "Не нравится"» —
    без этого обе кнопки матчились одинаково и выбор уходил на дизлайк.
    Цель сама с «не» («не нравится») — hay не трогаем: тогда голое слово
    у кандидата отсекается отдельной проверкой в скоринге."""
    if _NEG_WORD_RE.search(goal):
        return hay
    return _NEG_WORD_RE.sub("не", hay)


def _user_page_host(host: str) -> bool:
    """Видимая вкладка — обычная пользовательская страница, а не служебная
    машинерия бота (его чат-UI на localhost, вкладки веб-LLM): те нельзя
    захватывать молча при выборе цели команды — клики туда ломают сессии
    самого бота."""
    h = (host or "").lower().removeprefix("www.")
    if not h or h in ("localhost", "127.0.0.1"):
        return False
    try:
        from app.features.web_llm import ADAPTERS
        for a in (ADAPTERS or {}).values():
            ah = (str(a.get("host") or "")).lower().removeprefix("www.")
            if ah and (h == ah or h.endswith("." + ah)):
                return False
    except Exception:
        pass
    return True


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
    """Скриншот вьюпорта (jpeg) + пронумерованные рамки вокруг кандидатов
    (визуальный фолбэк резолва). Рамки — из палитры (красный первым),
    номер — в залитом бейдже цвета рамки: мелкий красный текст поверх
    видеоряда читается плохо, соседние номера сливаются. Бейдж —
    над левым верхним углом, при коллизии с уже поставленным — другой
    угол. Координаты элементов — CSS-пиксели, скриншот снят со scale='css'
    (1:1); масштаб по ширине картинки против vw снапшота оставлен как
    страховка, если бэкенд вернёт device-scale. На выходе тоже jpeg — в
    разы легче png для пересылки в веб-чат/API. None — Pillow/картинка
    не сработали (фолбэк пропускаем)."""
    try:
        import io
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(io.BytesIO(shot)).convert("RGB")
        vw = next((float(it.get("vw")) for it in cands if it.get("vw")),
                  float(img.width))
        scale = img.width / vw if vw else 1.0
        d = ImageDraw.Draw(img)
        # 12 цветов — под бюджет гибридного яруса (HYBRID_BOX_MAX); первых
        # 8 хватает визуальному фолбэку (топ-8), остальные четыре — вне
        # их тонов: лайм, коричневый, почти чёрный, серый
        palette = [(220, 38, 38), (37, 99, 235), (5, 150, 105),
                   (217, 119, 6), (147, 51, 234), (219, 39, 119),
                   (8, 145, 178), (234, 88, 12), (101, 163, 13),
                   (120, 72, 30), (23, 23, 23), (107, 114, 128)]
        # Кегль и толщина рамки — от ширины картинки: vision-модель даун-
        # скейлит скриншот под своё разрешение, фиксированный мелкий шрифт
        # на широком кадре (фоновая вкладка с большим канвасом) схлопывался
        fs = max(18, min(42, int(img.width / 60)))
        lw = max(3, fs // 6)
        try:
            font = ImageFont.load_default(size=fs)
        except TypeError:
            font = ImageFont.load_default()  # старый Pillow без size=
        placed: List[tuple] = []
        for n, it in enumerate(cands, 1):
            col = palette[(n - 1) % len(palette)]
            x = float(it.get("x") or 0) * scale
            y = float(it.get("y") or 0) * scale
            x2 = x + max(8.0, float(it.get("w") or 0)) * scale
            y2 = y + max(8.0, float(it.get("h") or 0)) * scale
            d.rectangle([x, y, x2, y2], outline=col, width=lw)
            label = str(n)
            bb = d.textbbox((0, 0), label, font=font)
            bw, bh = (bb[2] - bb[0]) + 12, (bb[3] - bb[1]) + 8
            spots = [(x, y - bh - 2), (x2 - bw, y - bh - 2),
                     (x + 2, y + 2), (x2 - bw - 2, y + 2)]
            bx = by = None
            for sx, sy in spots:
                if sy < 0:
                    continue
                hit = False
                for pr in placed:
                    if (sx < pr[2] and sx + bw > pr[0]
                            and sy < pr[3] and sy + bh > pr[1]):
                        hit = True
                        break
                if not hit:
                    bx, by = sx, sy
                    break
            if bx is None:
                bx, by = x + 2, y + 2
            placed.append((bx, by, bx + bw, by + bh))
            d.rectangle([bx, by, bx + bw, by + bh], fill=col)
            d.text((bx + 6, by + 4), label, fill=(255, 255, 255),
                   font=font)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        return buf.getvalue()
    except Exception:
        return None


# ── Дедуп дублей одной карточки (для vision-рамок) ───────

# Хелперы дедупа (_link_key/_rects_close/_texts_dup/_card_text_key) живут
# в browser_actions — там же их использует дедуп текстового снапшота
# (_dedup_snapshot_items); импорт сюда — для _dedup_same_target_cards
# ниже и совместимости внешних вызовов (тесты адресуют _cc_mod._link_key)
from app.features.browser_actions import (  # noqa: E402
    _card_text_key, _link_key, _rects_close, _snap_frag, _texts_dup)


def _active_layer(items: List[dict]) -> List[dict]:
    """Кандидаты только из активного слоя страницы — общий фильтр для
    текстового выбора (_choose_element), широкого LLM-резолва
    (_llm_wide_pick), гибридного яруса (_hybrid_pick) и vision-рамок
    (_vision_box_pool: _hybrid_pick, _visual_resolve). Два сигнала снапшота:
    1) sc — «в слое поверх бэкдропа»: снапшот нашёл затемняющий бэкдроп в
       центре вьюпорта и проверил, чем перекрыт центр каждого элемента;
       требует, чтобы бэкдроп прошёл цветовую эвристику (rgba с 0<α<0.98);
    2) cov — «центр элемента перекрыт чужим фиксированным слоем» (vpcCov,
       elementFromPoint): считается для каждого элемента независимо от
       детекта бэкдропа — на сайтах с карточками товара попап («Заменить
       позицию в комбо») не всегда даёт опознаваемый бэкдроп (sc остаётся
       1 у всех элементов), и без cov карточка каталога ПОД попапом с тем
       же текстом товара обходит пункт попапа в скоринге, уводя выбор LLM
       в каталог. Клик по перекрытому элементу физически попадает в слой
       сверху (force-клик playwright бьёт по координатам) — такие кандидаты
       недоступны.
    Правило «не режем в ноль» для обоих сигналов: если фильтр выкинул ВСЕ
    элементы (ложный детект слоя / всё перекрыто одинаково) — возвращаем
    список как есть; отсутствие ключа = элемент доступен."""
    scoped = [it for it in items if it.get("sc", True)]
    if scoped and len(scoped) < len(items):
        items = scoped
    uncovered = [it for it in items if not it.get("cov")]
    if uncovered and len(uncovered) < len(items):
        items = uncovered
    return items


def _layer_note(it: dict) -> str:
    """Пометка контекста слоя к строке кандидата в промпте LLM: открытое
    окно (md) / открытый список (dd) / перекрыт затемнением (cov). Компактно
    — одна пометка, самая «горячая» первой: список → окно → затемнение."""
    if it.get("dd"):
        return " — in an open list"
    if it.get("md"):
        return " — in an open dialog"
    if it.get("cov"):
        return " — under a dimmed overlay"
    return ""


# Значимые подписи-символы: крестик, плюс/минус количества, стрелки,
# бургер, троеточие — законная цель клика, в отличие от «/» и «•»
_GLYPH_LABELS = frozenset("+-−–×✕✖✗⨯←→‹›<>«»↑↓☰≡⋮⋯…?★☆♥❤")


def _wide_label_ok(it: dict) -> bool:
    """Подпись элемента годится в список широкого LLM-резолва: не служебный
    обрывок — бейдж длительности «0:13», разделитель «/»/«•», строка
    метаданных «33 тыс. 1 г. назад». Номер страницы («2») и значимый
    символ (×, +) — годятся. Хоть одна годная подпись из text/aria/title —
    элемент берём."""
    from app.features.browser_actions import (
        _DURATION_TEXT_RE, _SERVICE_WORDS_RE)
    for k in ("text", "aria", "title"):
        lab = " ".join(str(it.get(k) or "").split())
        if not lab:
            continue
        if lab in _GLYPH_LABELS or re.fullmatch(r"\d{1,4}", lab):
            return True
        if not re.search(r"[^\W\d_]", lab) or _DURATION_TEXT_RE.match(lab):
            continue  # ни одной буквы: «0:48 / 8:13», «/», «•»
        if re.search(r"\d", lab) and len(lab) <= 48 and (
                _SERVICE_WORDS_RE.search(lab) or re.search(r"[•·|]", lab)):
            continue  # метаданные карточки: просмотры/давность
        return True
    return False


def _boxes_coincide(a: dict, b: dict) -> bool:
    """Две рамки (зона vision и элемент снапшота, координаты вьюпорта) —
    один и тот же контрол: центр одной лежит внутри другой."""
    def _rect(r: dict) -> Tuple[float, float, float, float]:
        x, y = float(r.get("x") or 0), float(r.get("y") or 0)
        return x, y, x + float(r.get("w") or 0), y + float(r.get("h") or 0)

    ax0, ay0, ax1, ay1 = _rect(a)
    bx0, by0, bx1, by1 = _rect(b)
    if ax1 <= ax0 or ay1 <= ay0 or bx1 <= bx0 or by1 <= by0:
        return False
    acx, acy = (ax0 + ax1) / 2, (ay0 + ay1) / 2
    bcx, bcy = (bx0 + bx1) / 2, (by0 + by1) / 2
    return (bx0 <= acx <= bx1 and by0 <= acy <= by1) \
        or (ax0 <= bcx <= ax1 and ay0 <= bcy <= ay1)


def _zone_same_label(box: dict, it: dict) -> bool:
    """Подпись зоны не противоречит подписи элемента снапшота: у одного
    контрола они из одного DOM и делят слово. Совпадения одной геометрии
    мало — зона-обёртка («Подписаться» во всю плашку) накрывает и чужой
    элемент. Пустая подпись с любой стороны — не противоречие."""
    zw = set(re.findall(r"[a-z0-9а-яё]+", _norm_match(str(box.get("text") or ""))))
    lw = set(re.findall(r"[a-z0-9а-яё]+", _norm_match(" ".join(
        str(it.get(k) or "") for k in ("text", "aria", "title")))))
    return not zw or not lw or bool(zw & lw)


def _cand_line(n: int, it: dict, lab_max: int = 120) -> str:
    """Строка кандидата в текстовом списке для LLM (широкий резолв и
    текстовая часть гибридного яруса): «N) [тег/роль] подпись» + пометка
    слоя. lab_max — обрезка подписи (гибрид ужимает её под лимит промпта);
    тег/роль тоже режем — длинные custom elements (ytd-…-renderer) иначе
    съедали бюджет промпта."""
    lab = str(it.get("text") or it.get("aria") or it.get("title") or "")
    ctx = str(it.get("ctx") or "")
    if not lab:
        # Безымянная иконка — сопоставить её с целью может только vision;
        # контекст блока — внутри той же скобки, без пустой подписи впереди
        lab = f"(no label, block: {ctx[:60]})" if ctx else "(no label)"
    elif ctx and len(lab) <= 15:
        # Короткая подпись («закрыть», «×») без контекста блока LLM не
        # привязать к скоуп-цели («закрыть на корзина» — крестик сам по
        # себе «корзины» не содержит) — добавляем контекст
        lab = f"{lab} (block: {ctx[:60]})"
    return (f"{n}) [{str(it.get('tag'))[:24]}/{str(it.get('role') or '-')[:24]}] "
            f"{lab[:lab_max]}{_layer_note(it)}")


# Разбор ответа модели на выбор по номеру (_parse_pick_answer). Уверенность:
# «C=0.8», «С: 0,8», «C=.8», «C=80%» (C — латиница или кириллица)
_PICK_CONF_RE = re.compile(
    r"(?<![a-zа-яё])[cс]\s*[=:]\s*(\d+(?:[.,]\d+)?|[.,]\d+)(\s*%)?([.,](?!\d))?",
    re.IGNORECASE)
# Строгая грамматика ответа: «[T=]N [C=…][.]» и ничего больше (разметку и
# «Ответ:» снимаем до сверки) — как fullmatch(\d{1,2}) у широкого
# текстового резолва. Только она годится для выбора строки текстового списка и
# безымянной рамки: там подпись с целью не сверяется
_PICK_STRICT_RE = re.compile(
    r"(?:[tт]\s*=\s*)?(\d{1,2})\s*[.,;]?\s*"
    r"(?:[(\[]?\s*[cс]\s*[=:]\s*(?:\d+(?:[.,]\d+)?|[.,]\d+)\s*%?\s*[)\]]?)?"
    r"\s*\.?\s*", re.IGNORECASE)
# Голая уверенность без «C=»: «3, 0.8» / «3 80%»
_PICK_BARE_CONF_RE = re.compile(r"(?<![\d.,])(\d*[.,]\d+(?:\s*%)?|\d+\s*%)")
# Кавычки — подписи элементов: их цифры («2:43 World Map») не номер ответа
_PICK_QUOTED_RE = re.compile(r"«[^»]*»|\"[^\"]*\"|“[^”]*”|'[^']*'")
_PICK_INT_RE = re.compile(r"(?<![\w.,])\d+(?![\w%]|[.,]\d)")
# Отказ — целым словом: «Нетфликс» — не «нет»
_PICK_REFUSAL_RE = re.compile(r"(?:нет|no|none)\b", re.IGNORECASE)
# Отказ фразой без номера: «Подходящего элемента нет», «ничего не подходит»,
# «Не вижу подходящего», «nothing matches»
_PICK_REFUSAL_PHRASE_RE = re.compile(
    r"(?<![a-zа-яё])(?:нет|ничего|nothing|none|no match\w*|not found|"
    r"не подход\w*|не наш[её]л\w*|не найден\w*|не вижу)(?![a-zа-яё])",
    re.IGNORECASE)
# Отрицание/сомнение где угодно в ответе с номером — не выбор: «Не вижу
# подходящего, возможно 4», «Не уверен, но 3», «I think 3», «Подходящего
# нет, возможно 3» (подписи в кавычках вырезаны до проверки)
_PICK_HEDGE_RE = re.compile(
    r"(?<![a-zа-яё])(?:не|ни|нет|no|none|not|maybe|possibly|perhaps|probably|"
    r"likely|think|guess|возможно|наверн\w*|кажется|вероятно|скорее)"
    r"(?![a-zа-яё])", re.IGNORECASE)
_PICK_PREFIX_RE = re.compile(r"^(?:ответ|answer)\s*[:\-—]?\s*", re.IGNORECASE)
_PICK_T_RE = re.compile(r"(?<![a-zа-яё])[tт]\s*=\s*", re.IGNORECASE)


def _pick_conf(val: str, pct: bool) -> Optional[float]:
    """Уверенность из текста: проценты → доля; вне [0, 1] — None (не повод
    браковать номер: уверенность на выбор не влияет, пишется в аудит)."""
    try:
        v = float(val.replace(",", "."))
    except ValueError:
        return None
    if pct:
        v /= 100.0
    return v if 0.0 <= v <= 1.0 else None


def _parse_pick_answer(resp, strict: bool = False
                       ) -> Tuple[Optional[int], Optional[float], bool]:
    """Разбор ответа vision/LLM на выбор номера → (номер|None, уверенность|
    None, «нет»). Отказ — «нет»/«no»/«none» целым словом в начале или
    фраза-отказ без единой цифры («Подходящего элемента нет»). Сомнение/
    отрицание при номере («возможно 4», «не уверен, но 3», «I think 3») —
    невалидно всегда. strict — только грамматика «[T=]N [C=…][.]»
    (_PICK_STRICT_RE). Нестрогий разбор терпит пояснение после номера
    («3 — кнопка «Оплата»»), уверенность в любом месте, процентами, «.8»,
    голой дробью («3, 0.8»); номер — ПЕРВОЕ отдельно стоящее целое, второе
    ДРУГОЕ целое вне кавычек («3 или 5», «3 — 2:43») — неоднозначно,
    невалидно (None, None, False), как и номер длиннее двух цифр."""
    s = re.sub(r"[`*_]", "", str(resp or "")).strip()
    s = _PICK_PREFIX_RE.sub("", s)
    if _PICK_REFUSAL_RE.match(s):
        return None, None, True
    bare = _PICK_QUOTED_RE.sub(" ", s)
    if not re.search(r"\d", bare) and _PICK_REFUSAL_PHRASE_RE.search(bare):
        return None, None, True
    if _PICK_HEDGE_RE.search(bare):
        return None, None, False
    if strict and not _PICK_STRICT_RE.fullmatch(s):
        return None, None, False
    s = bare
    conf = None
    mc = _PICK_CONF_RE.search(s)
    if mc:
        # «C=0.» — число оборвано точкой: уверенность не читаем
        cut = bool(mc.group(3)) and not re.search(r"[.,]", mc.group(1))
        conf = None if cut else _pick_conf(mc.group(1), bool(mc.group(2)))
        s = s[:mc.start()] + " " + s[mc.end():]
    else:
        mb = _PICK_BARE_CONF_RE.search(s)
        if mb:
            val = mb.group(1)
            conf = _pick_conf(val.rstrip("% "), val.endswith("%"))
            s = s[:mb.start()] + " " + s[mb.end():]
    s = _PICK_T_RE.sub(" ", s)
    nums = _PICK_INT_RE.findall(s)
    if not nums or len(nums[0]) > 2 or any(int(x) != int(nums[0])
                                          for x in nums[1:]):
        return None, None, False
    return int(nums[0]), conf, False


def _llm_said_no(resp) -> bool:
    """Ответ «ничего не подходит» на промпт выбора по номеру: промпты на
    английском просят «no», старые/русскоязычные модели отвечают «нет»."""
    s = str(resp or "").strip().lower().lstrip("«\"'*`")
    return s.startswith("нет") or bool(re.match(r"(?:no|none)\b", s))


def _llm_said_skip(resp) -> bool:
    # Ответ «шаг уже не нужен» (skip) — принимаем и русское «пропустить».
    s = str(resp or "").strip().lower().lstrip("«\"'*`")
    return s.startswith("пропуст") or s.startswith("skip")


def _dedup_same_target_cards(items: List[dict]) -> List[dict]:
    """Схлопывание дублей одной карточки перед нарезкой топ-N для vision:
    на одну ссылку у карточки часто висит несколько разных a[href]
    (обёртка, заголовок, строка метаданных «N просмотров • дата») — все
    проходят фильтр снапшота и конкурируют за номера рамок, vision
    достаются тонкие бесполезные полосы. Дубль =
    совпал ключ href ИЛИ длинный текст (с префиксной обрезкой «…»), И при
    этом рядом геометрически: дубли href в другом конце страницы
    (пагинация сверху и снизу) не трогаем. Из группы остаётся кандидат с
    максимальной площадью (обычно заголовок/обёртка, а не строка «N
    просмотров»), при равенстве — с подписью."""
    keys = [(_link_key(it.get("href")), _card_text_key(it)) for it in items]
    kept: List[int] = []
    for i, it in enumerate(items):
        lk, tk = keys[i]
        dup_j = None
        for j in kept:
            lk2, tk2 = keys[j]
            # Разные href — разные цели: текст-дедуп против таких пар не
            # работает (две разные кнопки «Подробнее» рядом — не дубли)
            diff_href = lk is not None and lk2 is not None and lk != lk2
            same = ((lk is not None and lk == lk2)
                    or (not diff_href and _texts_dup(tk or "", tk2 or "")))
            if same and _rects_close(it, items[j]):
                dup_j = j
                break
        if dup_j is None:
            kept.append(i)
            continue
        old = items[dup_j]
        area_new = float(it.get("w") or 0) * float(it.get("h") or 0)
        area_old = float(old.get("w") or 0) * float(old.get("h") or 0)
        lab_new = bool(it.get("text") or it.get("aria"))
        lab_old = bool(old.get("text") or old.get("aria"))
        if area_new > area_old or (area_new == area_old and lab_new
                                   and not lab_old):
            kept[kept.index(dup_j)] = i
    if len(kept) < len(items):
        logger.debug(f"[CompControl] Дедуп карточек для vision: "
                     f"{len(items)} → {len(kept)} кандидатов")
    return [items[i] for i in kept]


# ── Быстрый путь «открой X» ──────────────────────────────

# Вся фраза — одна голая команда открытия/запуска. Такие резолвятся кодом,
# без LLM-пайплайна (fast-path в process_message): экономия ~10+ секунд.
_OPEN_VERB_RE = re.compile(
    r"^(?:открой|открыть|запусти|запустить|включи|включить|open|launch|start)\s+",
    re.IGNORECASE)
# Вежливые слова — одно определение для всех парсеров команд: хвост
# «…, пожалуйста» / «… please» и голова «пожалуйста, …» срезаются одинаково
_POLITE_ALT = r"пожалуйста|плиз|плз|please|pls|plz"
_POLITE_TAIL_RE = re.compile(
    rf"[\s,]*(?:{_POLITE_ALT})\s*[.!?…]*\s*$", re.IGNORECASE)
_POLITE_HEAD_RE = re.compile(rf"^\s*(?:{_POLITE_ALT})[\s,]+", re.IGNORECASE)
# Обрамление цели: запятые/кавычки/точки по краям («ютуб,», «"ютуб"»)
_TARGET_EDGE_CHARS = " \t,;:.!?…\"'«»“”„`"


def _strip_polite(s: str) -> str:
    """«нажми войти, пожалуйста» → «нажми войти»; «пожалуйста, открой
    ютуб» → «открой ютуб». Вежливость в середине фразы не трогаем."""
    s = _POLITE_HEAD_RE.sub("", s or "")
    return _POLITE_TAIL_RE.sub("", s).strip()


_OPEN_FILLER_RE = re.compile(
    rf"^(?:{_POLITE_ALT}|мне|нам|сайт|страницу|страница|вкладку|вкладка|"
    r"приложение|программу|программа)[\s,]+", re.IGNORECASE)
_OPEN_TAIL_RE = _POLITE_TAIL_RE

# Поиск на конкретном сайте: «включи фильм на стриминге»,
# «открой шоу на ютуб», «open a movie on youtube»
_SEARCH_ON_SITE_RE = re.compile(
    r"^\s*(включи|включить|найди|найти|поищи|посмотри|посмотреть|глянь|поставь|"
    r"открой|открыть|запусти|запустить|open|play|watch|find|search|launch|start)"
    r"\s+(.+?)\s+(?:на|в|во|on|in)\s+(\S+)\s*[.!?…]*\s*$",
    re.IGNORECASE)
# Элементы интерфейса страницы/плеера: «открой комментарии на ютубе»,
# «включи субтитры» — это клик (или медиа-клавиша), а не поиск ролика с
# таким названием. Вся цель целиком — одно из этих слов
_UI_ELEMENT_RE = re.compile(
    r"^(?:комментари\w*|коммент\w*|настройк\w*|параметр\w*|звук|субтитр\w*|"
    r"полн\w*\s+экран\w*|полноэкранн\w*(?:\s+режим\w*)?|описани\w*|"
    r"плейлист\w*|меню|чат\w*|уведомлени\w*|профил\w*|аккаунт\w*|корзин\w*|"
    r"фильтр\w*|сортировк\w*|главн\w*(?:\s+страниц\w*)?|"
    r"comments?|settings|subtitles|captions|full\s*screen|description|"
    r"playlist|sound|notifications|menu|cart|chat)$", re.IGNORECASE)
# Элементы плеера, которые и без сайта в фразе — про открытую страницу
# («включи субтитры»), а не про приложение/сайт
_UI_PLAYER_RE = re.compile(
    r"^(?:субтитр\w*|полн\w*\s+экран\w*|полноэкранн\w*(?:\s+режим\w*)?|"
    r"subtitles|captions|full\s*screen)$", re.IGNORECASE)

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
# Потолок номера (один на все номерные рецепты): «0 результат» давал
# search_pick:0 — рецепт с бессмысленным номером, «99 видео» — тоже не
# команда (столько элементов в выдаче не размечается)
_ORDINAL_MAX = 20


# Хвост-скоп у номерной команды: «третье видео в плейлисте» / «2 результат
# в выдаче» — скоп срезается; «плейлист» даёт отдельный рецепт
_ORDINAL_SCOPE_RE = re.compile(
    r"\s+(?:в|во|на|in)\s+(плейлисте|плейлиста|плейлист|выдаче|поиске|списке|"
    r"playlist|results|shorts|шортс\w*)\s*$", re.IGNORECASE)
_ORDINAL_PLAYLIST_SCOPES = {"плейлисте", "плейлиста", "плейлист", "playlist"}
_ORDINAL_SHORTS_SCOPES = {"shorts"}
# Само слово «шортс» целью: «первый шортс» — полка shorts без скопа
_ORDINAL_SHORTS_WORDS = {"шортс", "шортсы", "шортса", "шортсов", "shorts"}


def ordinal_recipe(name: str) -> Optional[str]:
    """«третье видео» / «2 результат» → «search_pick:3»; со скопом плейлиста
    («третье видео в плейлисте») → «playlist_pick:3»; скоп shorts
    («первое видео в shorts») или цель-шортс («первый шортс») →
    «shorts_pick:1». None — не такая команда."""
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
    if n is None or not 1 <= n <= _ORDINAL_MAX:
        return None
    if w2 in _ORDINAL_SHORTS_WORDS \
            or (scope is not None
                and (scope in _ORDINAL_SHORTS_SCOPES
                     or scope.startswith("шортс"))):
        return f"shorts_pick:{n}"
    if _ORDINAL_TARGET_RE.match(w2):
        if scope in _ORDINAL_PLAYLIST_SCOPES:
            return f"playlist_pick:{n}"
        return f"search_pick:{n}"
    return None


# «Следующее видео» — кнопка .ytp-next-button плеера (recipe youtube_next):
# встроенная фраза, как номерные результаты — без ключа в yaml. Консервативно:
# только эти формы, «дальше»/«вперёд» — слишком общие слова для других команд
# (листание, навигация по истории и т.п.), их recipe не заберёт
_NEXT_VIDEO_RE = re.compile(
    r"^(?:следующее\s+видео|следующий\s+(?:ролик|трек)|next\s+video)"
    r"\s*[.!?…]*\s*$", re.IGNORECASE)


def next_video_recipe(name: str) -> Optional[str]:
    """«следующее видео» / «следующий ролик» / «следующий трек» / «next
    video» → «youtube_next». Глагол («включи»/«нажми») к этому моменту уже
    срезан вызывающей стороной (parse_open_many/_CLICK_REQUEST_RE) — фраза
    голая. None — не такая команда."""
    if not name:
        return None
    if _NEXT_VIDEO_RE.match(" ".join(name.strip().lower().split())):
        return "youtube_next"
    return None


# ── Агентный клик «нажми X» ─────────────────────────────

_CLICK_REQUEST_RE = re.compile(
    r"^\s*(?:нажми|нажать|кликни|кликнуть|тыкни|щёлкни|щелкни|click|press|tap)\s+"
    r"(.+?)\s*[.!?…]*\s*$",
    re.IGNORECASE)
# «(на/по) кнопку/ссылку» в начале цели срезаем — LLM ищет по тексту
# элемента: «на войти», «по кнопке войти», «on the login button» → «войти»/
# «login». Существительные — в любом падеже
_CLICK_FILLER_RE = re.compile(
    r"^(?:(?:на|по|on|onto)\s+)?(?:the\s+)?"
    r"(?:(?:кнопк\w*|ссылк\w*|пункт\w*|иконк\w*|значк\w*|значок|"
    r"button|link|icon)\s+)?", re.IGNORECASE)
# Англ. порядок «the login button» — носитель хвостом
_CLICK_FILLER_TAIL_RE = re.compile(r"\s+(?:button|link|icon)$", re.IGNORECASE)
# Хвост-место «… на <сайт>» — ОДНО определение для клика/наведения/
# скачивания/чтения/закрытия
_CLICK_SITE_RE = re.compile(r"\s+(?:на|в|во|on|in)\s+(\S+)\s*$", re.IGNORECASE)
# То же место в НАЧАЛЕ цели: «нажми на ютубе подписаться». Сайт — слово в
# предложном падеже («ютубе», «почте»); носители элемента и области
# страницы («на кнопке», «в корзине», «в меню») сайтом не считаем
_CLICK_SITE_HEAD_RE = re.compile(
    r"^(?:на|в|во)\s+([a-zа-яё0-9-]+е)\s+(\S.*)$", re.IGNORECASE)
_CLICK_SITE_HEAD_NOT_RE = re.compile(
    r"^(?:кнопк|ссылк|пункт|иконк|значк|строк|панел|вкладк|корзин|меню|"
    r"списк|раздел|блок|карточк|окн|модалк|форм|пол[ея]|шапк|футер|"
    r"главн|верх|низ|угл|центр|середин|конц|начал)", re.IGNORECASE)
# Прилагательное/числительное на -ое/-ее/-ье/-ые/-ие — не сайт в предложном
# падеже: «нажми на красное платье», «на последнее сообщение», «на третье
# видео», «на мое имя» — это часть цели
_CLICK_SITE_HEAD_ADJ_RE = re.compile(r"(?:ое|ее|ье|ые|ие)$", re.IGNORECASE)
# Открытие/включение элемента интерфейса: «открой комментарии на ютубе»
_UI_OPEN_RE = re.compile(
    r"^\s*(?:открой|открыть|включи|включить|покажи|показать|open|show)\s+"
    r"(.+?)\s*[.!?…]*\s*$", re.IGNORECASE)
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
    "окне", "окна", "page", "site", "tab",
    # Области медиа-страниц, а не сайты и не скопы карточек: «нажми ‹трек›
    # в джеме/миксе/очереди» — цель «‹трек›», а «джем» склеивался в скоп
    # цели («‹трек› на джем») и убивал весь резолв (уходило в vision,
    # модель угадывала случайную строку очереди на YouTube)
    "джем", "джеме", "микс", "миксе", "микса", "очередь", "очереди",
    "радио", "плейлист", "плейлисте", "плейлиста"})
# «закрой (модальное) окно/модалку/попап» → цель сводится к «закрыть»: слов
# «окна» в тексте страницы нет, а нужен единственный контрол — крестик
# попапа (подписан «закрыть» ранним проходом снапшота)
_CLOSE_GOAL_RE = re.compile(
    r"\b(?:закры\w*|закро[йеюям]\w*|сверн\w*|сворач\w*)"
    r"\s+(?:модал\w*|попап\w*|диалог\w*|окошк\w*|окн\w*|"
    r"баннер\w*|уведомлен\w*|подсказк\w*|анкет\w*|форм\w*)", re.IGNORECASE)
# Глагол закрытия в начале цели: «закрой окно», «сверни анкету»,
# «закрой соусы к бортикам» — объект в группе 1 (пустой — «закрой» без
# объекта). Корни закрыты окончаниями: открытые «закр\w+»/«скро\w*» подменяли
# намерение на «закрыть» у «закрепить», «закрась», «закрути», «скролл»
_CLOSE_VERB_RE = re.compile(
    r"^(?:закры\w*|закро[йеюям]\w*|скры\w*|скро[йеюя]\w*|сверн\w*|сворач\w*)"
    r"\s*(.*)$", re.IGNORECASE)
# Объект закрытия без привязки («окно», «модальное окно», «попап», «это») —
# цель сводится к «закрыть»; всё остальное — целевое закрытие по контексту
_CLOSE_GENERIC_RE = re.compile(
    r"^(?:(?:модальн\w*|всплывающ\w*|текущ\w*|это|этот|эту)\s+)?"
    r"(?:окн\w*|окошк\w*|модал\w*|попап\w*|диалог\w*|баннер\w*|уведомлен\w*|"
    r"подсказк\w*|анкет\w*|форм\w*|это|его|её|их)\s*$", re.IGNORECASE)

# Свайп-ленты (shorts/reels): шаг прокрутки тут не «показать ещё элементы
# списка», а «перелистнуть основной контент» — крупнейший скроллящийся
# контейнер такой страницы и ЕСТЬ лента видео. Доскролл-поиск кнопки
# (_scroll_hunt) на ней бессмысленен (кнопки в ленте не появляются
# прокруткой) и деструктивен: 10 контейнерных шагов = 10 перелистнутых
# роликов, и возврат scrollTop смену видео не отменяет — «нажми сортировать»
# на странице shorts дал бы бесконечное листание ленты
_SWIPE_FEED_URL_RE = re.compile(r"/(?:shorts|reels?)/", re.IGNORECASE)

# Контролы-«разрушители»: клик по ним уничтожает состояние (крестик модалки,
# «Удалить», «Очистить очередь», «Выйти»), и если в цели нет такого намерения
# — это промах резолва, а не команда. Правило одно на весь каскад выбора:
# формы глаголов закрытия/удаления/очистки (RU+EN) в НАЧАЛЕ подписи. Окончания
# перечислены морфологически, а не открытым корнем: «закр\w*» ловил
# «закрепить», «скро\w*» — «скролл», «удал» — «удалённая работа», и вето либо
# срабатывало на безобидной кнопке, либо (в цели) молча отключалось
# Разрушители делятся на КЛАССЫ — намерение в цели снимает вето только с
# контролов своего класса: «нажми закрыть» разрешает крестик, но не «Log
# out». Без разделения на классы «закрыть» и «выйти» были бы одним флагом,
# и при отсутствии крестика в снапшоте широкий LLM-резолв мог бы тихо
# ткнуть в «Log out», приняв его за разрешённое закрытие
_DESTRUCTIVE_CLASS_RES = {
    "close": re.compile(
        r"(?<![a-z0-9а-яё])(?:"
        r"закрыть\w*|закрыва\w*|закрыл\w*|закрыти[ея]\w*|закро[йеюям]\w*|"
        r"скрыть\w*|скрыва\w*|скрыл\w*|скро[йеюя]\w*|сверн\w*|сворач\w*|"
        r"close(?![a-z])|closing|dismiss\w*"
        r")", re.IGNORECASE),
    "delete": re.compile(
        r"(?<![a-z0-9а-яё])(?:"
        r"удали\w*|удаля\w*|удален(?:и[ея]|ий)\w*|"
        r"убрать|убрал\w*|убери\w*|уберит\w*|уберем\w*|"
        r"стереть|сотри\w*|очист(?!ител)\w*|очищ\w*|сброс\w*|сбрось\w*|сбрас\w*|"
        r"delete\w*|remove\w*|removal|"
        r"clear(?:s|ed|ing)?(?![a-z])|discard\w*|reset\w*|trash\w*|erase\w*"
        r")", re.IGNORECASE),
    "leave": re.compile(
        r"(?<![a-z0-9а-яё])(?:"
        r"отпис\w*|покин\w*|"
        r"выйти|выйди\w*|выхожу|выход(?:а|у|ом|е)?(?![а-яё])|"
        # quit — без «quite»
        r"unsubscribe\w*|unfollow\w*|quit(?:s|ting)?(?![a-z])|exit\w*|"
        r"log\s?out|logout|sign\s?out|signout|log\s?off|"
        # отмена подписки/членства, деактивация аккаунта
        r"отменить\s+подписк\w*|отмени(?:те)?\s+подписк\w*|"
        r"деактивир\w*|деактиваци\w*|"
        r"cancel\s+(?:my\s+|your\s+)?(?:subscription|membership|plan)\w*|"
        r"deactivat\w*"
        r")", re.IGNORECASE),
}
_DESTRUCTIVE_WORD_RE = re.compile(
    "|".join(f"(?:{r.pattern})" for r in _DESTRUCTIVE_CLASS_RES.values()),
    re.IGNORECASE)
# Безымянные иконки закрытия/удаления: вся подпись — один крестик/корзина.
# Крестик — и «закрыть», и «убрать» (× у товара в корзине), поэтому оба
# класса; корзина — только удаление
_DESTRUCTIVE_ICONS = {
    **{s: frozenset({"close", "delete"})
       for s in ("x", "х", "×", "✕", "✖", "✘", "✗", "⨯", "❌", "✖️", "❎")},
    **{s: frozenset({"delete"}) for s in ("🗑", "🗑️")},
}
# Намерение в цели, которое глаголом не выражено: «нажми крестик у джема»
_DESTRUCTIVE_HINT_RE = re.compile(r"(?<![а-яё])крестик\w*", re.IGNORECASE)


def _destructive_classes(text: str, anchored: bool) -> frozenset:
    """Классы разрушительности текста: подпись — по началу (anchored),
    цель — по любому месту. Иконка целиком — по словарю."""
    if not text:
        return frozenset()
    if text in _DESTRUCTIVE_ICONS:
        return _DESTRUCTIVE_ICONS[text]
    out = set()
    for cls, rx in _DESTRUCTIVE_CLASS_RES.items():
        if rx.match(text) if anchored else rx.search(text):
            out.add(cls)
    return frozenset(out)


# ── Разрушительность ПОДПИСИ для гейта подтверждения (risky_label) ──
# Вето резолвера сверяет глагол по началу подписи; гейту этого мало: кнопка
# финального шага часто несёт префикс подтверждения или глагол в середине
# («Да, удалить», «Yes, delete», «Навсегда удалить», «Empty trash»,
# «Leave server», «Report and block»)
_CONFIRM_PREFIX_RE = re.compile(
    r"^\s*(?:да|yes|yep|ok|ок|окей|okay|хорошо|конечно|sure|точно|верно|"
    r"confirm|подтвердить|подтверждаю|подтвердите|continue\s+and|"
    r"продолжить\s+и|вс[её]\s+равно|anyway|"
    r"навсегда|безвозвратно|окончательно|permanently|forever|irreversibly|"
    r"i\s+understand(?:\s+[^,]{0,40})?|я\s+понимаю(?:\s+[^,]{0,40})?|"
    r"понятно|got\s+it)(?![a-zа-яё])\s*[,:!.;]*\s*", re.IGNORECASE)
# Глагол в ЛЮБОМ месте подписи — узкий словарь (инфинитив/императив, без
# существительных): «Удалённые», «Deleted items», «Hair remover» мимо
_DESTRUCTIVE_ANY_RE = re.compile(
    r"(?<![a-z0-9а-яё])(?:"
    r"удалить|удалите|удали|удалиться|удалим|стереть|сотрите|сотри|"
    r"уничтожить|уничтожьте|"
    r"очистить\s+(?:корзину|историю|вс[её]|чат|переписку|кэш|кеш|данные|"
    r"папку|диалог)|"
    r"выйти|выйдите|покинуть|покиньте|покинь|отписаться|отпишитесь|"
    r"отпишись|заблокировать|заблокируйте|заблокируй|пожаловаться|"
    r"деактивировать|отменить\s+подписку|"
    r"delete|remove|erase|wipe|destroy|terminate|purge|uninstall|"
    r"empty\s+(?:the\s+)?(?:trash|bin|recycle\s+bin|folder|spam|junk)|"
    # «В корзину» без глагола — это корзина магазина, не удаление
    r"(?:move|send)\s+to\s+(?:the\s+)?(?:trash|bin|recycle\s+bin)|"
    r"(?:переместить|перенести|отправить)\s+в\s+корзину|"
    r"clear\s+(?:all|history|data|everything|chat|conversation|messages?|"
    r"cache|cookies|browsing\s+data)|"
    r"leave(?:\s+(?:the|this))?\s+(?:server|group|workspace|channel|team|"
    r"chat|conversation|community|organi[sz]ation|org|space|room|guild|"
    r"project|household|family)|"
    r"unsubscribe|unfollow|unfriend|deactivate|log\s?out|sign\s?out|"
    r"block\s+(?:user|this|contact|account|number|sender|person|profile|"
    r"channel|page|him|her|them|@\S+)|(?:and|&)\s+block|"
    r"report\s+(?:user|spam|abuse|account|profile|post|comment|message|"
    r"channel|group|this)|"
    r"cancel\s+(?:(?:my|your)\s+)?(?:subscription|membership|plan|account)"
    r")(?![a-z0-9а-яё])|"
    # Кнопка одним словом: «Leave»/«Block»/«Report» в модалке
    r"^\s*(?:leave|block|report)\s*[.!]*\s*$", re.IGNORECASE)
# Папки/разделы, а не действия: «Trash», «Deleted items», «Удалённые (3)»
_DESTRUCTIVE_FOLDER_RE = re.compile(
    r"\s*(?:trash|bin|recycle\s+bin|deleted(?:\s+(?:items|messages|files|"
    r"mail|posts))?|recently\s+deleted|removed|"
    r"(?:недавно\s+)?удал[её]нн\w*(?:\s+\w+)?|корзина)"
    r"\s*[(\[]?\s*\d*\s*[)\]]?\s*", re.IGNORECASE)
# Заголовок статьи/вопрос, а не кнопка: «Как удалить аккаунт в Telegram?»
_QUESTION_LABEL_RE = re.compile(
    r"\?|^\s*(?:как|почему|зачем|что|можно\s+ли|how|why|what|can\s+i|"
    r"should\s+i)(?![a-zа-яё])", re.IGNORECASE)


def _label_destructive(label: str) -> bool:
    """Подпись — удаление/выход/блокировка (классы delete/leave): по началу
    подписи, после префикса подтверждения и узким словарём в любом месте.
    «Закрыть» диалог — рутина, сюда не входит."""
    s = _norm_match(label)
    if not s or _DESTRUCTIVE_FOLDER_RE.fullmatch(s):
        return False
    t = s
    for _ in range(3):
        if _destructive_classes(t, anchored=True) & {"delete", "leave"}:
            return True
        t2 = _CONFIRM_PREFIX_RE.sub("", t, count=1)
        if t2 == t or not t2:
            break
        t = t2
    return (len(s) <= 64 and not _QUESTION_LABEL_RE.search(s)
            and bool(_DESTRUCTIVE_ANY_RE.search(s)))


def _label_submit(label: str) -> bool:
    """Подпись начинается с отправки/подтверждения — и после префикса
    подтверждения («Да, отправить», «Yes, publish»)."""
    s = str(label or "")
    if _SUBMIT_LABEL_RE.match(s):
        return True
    t = _CONFIRM_PREFIX_RE.sub("", _norm_match(s), count=1)
    return bool(t) and t != _norm_match(s) and bool(_SUBMIT_LABEL_RE.match(t))


def _destructive_label_classes(it: dict) -> frozenset:
    """Классы разрушительности элемента: ЛЮБАЯ из его подписей (текст,
    aria-label, title) начинается с формы глагола закрытия/удаления/выхода
    или целиком является иконкой-крестиком. Иконку часто подписывает только
    aria («Remove from queue» при пустом тексте) — поэтому смотрим все три,
    а не первую непустую."""
    out: set = set()
    for key in ("text", "aria", "title"):
        out |= _destructive_classes(_norm_match(it.get(key)), anchored=True)
    return frozenset(out)


def _destructive_label(it: dict) -> bool:
    # Элемент — разрушительный контрол (любого класса).
    return bool(_destructive_label_classes(it))


def _destructive_intent_classes(goal: str) -> frozenset:
    """Классы намерения в цели: закрыть/удалить/выйти (глаголом или
    подсказкой-иконкой «крестик»)."""
    g = _norm_match(goal)
    out = set(_destructive_classes(g, anchored=False))
    if g and _DESTRUCTIVE_HINT_RE.search(g):
        out |= _DESTRUCTIVE_ICONS["×"]
    return frozenset(out)


def _destructive_intent(goal: str) -> bool:
    """В самой цели есть намерение закрыть/удалить/очистить/выйти — тогда
    разрушительный контрол ЭТОГО класса и есть то, о чём просили."""
    return bool(_destructive_intent_classes(goal))


# Операции, которые элемент не активируют: наведение курсора ничего не
# жмёт, поэтому разрушительный контрол под целью для него не запрещён
# («наведи на очистить очередь» было отказом — строже нужного). Вето
# зависит от типа действия, а не только от подписи элемента
_NON_ACTIVATING_OPS = frozenset({"hover"})


def _destructive_mismatch(goal: str, it: dict, op: str = "click") -> bool:
    """Инвариант вето: элемент разрушительный, а намерения в цели нет.
    True — такой элемент не показываем скорингу и не отдаём в клик.
    op — тип операции (см. _NON_ACTIVATING_OPS): наведению разрушительный
    контрол не опасен."""
    if op in _NON_ACTIVATING_OPS:
        return False
    label_cls = _destructive_label_classes(it)
    if not label_cls:
        return False
    # Намерение снимает вето только с контролов СВОЕГО класса: «закрой»
    # разрешает крестик/«Закрыть», но не «Log out» и не «Удалить»
    return not (label_cls & _destructive_intent_classes(goal))


def _goal_in_label(goal: str, label: str, host: Optional[str] = None,
                   ctx: Optional[str] = None) -> bool:
    """Хотя бы одно значимое слово цели (стем/синоним) встречается в подписи
    элемента. Пустая цель или пустая подпись — True (проверять нечего:
    безымянные иконки — легальная цель vision-резолва). Слова, которых в
    подписи не бывает по природе (номер, образ иконки, закрытие — см.
    _label_goal_check), совпадения не требуют."""
    return _label_goal_check(goal, label, host, ctx) != "mismatch"


# Слова цели, которых в подписи элемента нет по природе: порядковый номер
# («третье на новости» — позиция, а не текст), образ иконки («крестик» —
# открытый бургер рисуется крестиком и подписан «бургер-меню»), действие
# закрытия («закрыть» у безымянного крестика). Сверка подписи с целью их
# не требует — иначе верный выбор vision ветировался как галлюцинация
_LABEL_FREE_WORDS = frozenset(_ORDINALS) | frozenset({
    "последний", "последнее", "последняя", "предпоследний",
    "предпоследнее", "предпоследняя"})
_LABEL_FREE_ROOTS = ("закры", "закро", "close", "крест", "сверн", "сворач",
                     "dismiss")


# Порядковое во всех падежах («нажми третью ссылку»), не только словарные
# формы _ORDINALS
_ORDINAL_WORD_RE = re.compile(
    r"^(?:перв|втор|трет|четв[её]рт|пят|шест|седьм|восьм|девят|десят)"
    r"(?:ый|ий|ой|ое|ье|ая|ья|ую|ью|ого|ему|ому|ым|ом)$")
# Род элемента при номере («третье ВИДЕО», «вторую ССЫЛКУ»): подпись у такого
# элемента — заголовок, а не слово «видео»
_ORDINAL_KIND_ROOTS = ("видео", "ролик", "ссылк", "кнопк", "результат",
                       "пункт", "элемент", "стать", "карточк", "товар",
                       "пост", "запис", "video", "link", "button", "result",
                       "item")


def _label_free_word(w: str) -> bool:
    return (w in _LABEL_FREE_WORDS or w.isdigit()
            or bool(_ORDINAL_WORD_RE.match(w))
            or w.startswith(_LABEL_FREE_ROOTS)
            or w.startswith(_ICON_WORD_ROOTS))


def _label_goal_check(goal: str, label: str, host: Optional[str] = None,
                      ctx: Optional[str] = None) -> str:
    """Сверка подписи выбранного моделью элемента с целью:
    "match" — слово цели (стем/синоним) есть в подписи;
    "unverified" — сверять нечем (остались только номер/иконка/закрытие)
    или оставшиеся слова нашлись лишь в контексте блока (ctx): выбор
    принимаем, но с подтверждением человеком;
    "mismatch" — значимые слова цели есть, а в подписи/контексте их нет."""
    words = [w for w in re.findall(r"[a-z0-9а-яё]+", _norm_match(goal))
             if len(w) >= 3]
    hay = _norm_match(label)
    if not words or not hay:
        return "match"
    from app.features.web_search import _stem

    def _in(w: str, h: str) -> bool:
        return bool(_word_in(w, h) or _word_in(_stem(w), h)
                    or any(_word_in(s, h) for s in _goal_synonyms(w, host)))

    if any(_in(w, hay) for w in words):
        return "match"
    rest = [w for w in words if not _label_free_word(w)]
    if len(rest) < len(words) and any(
            w in _LABEL_FREE_WORDS or w.isdigit() or _ORDINAL_WORD_RE.match(w)
            for w in words):
        # Цель с номером: род элемента в подписи не пишут
        rest = [w for w in rest if not w.startswith(_ORDINAL_KIND_ROOTS)]
    if not rest:
        return "unverified"
    cx = _norm_match(ctx)
    if cx and any(_in(w, cx) for w in rest):
        return "unverified"
    return "mismatch"


# «перетащи/поставь слайдер X на N [единиц]»: ползунок (input[type=range]/
# role=slider). Единицы: %/проценты → доля шкалы (громкость 0..1 — 50% это
# 0.5, а не максимум), минуты/секунды — шкала медиа-прогресса (минуты в
# секунды конвертирует JS; у плееров max обычно в секундах)
_SLIDER_REQUEST_RE = re.compile(
    r"^\s*(?:перетащи|перетащить|передвинь|передвинуть|двинь|поставь|"
    r"поставить|установи|установить|выставь|выставить)\s+"
    r"(?:(?:ползунок|слайдер)\s+)?(.*?)\s+на\s+(\d{1,4})"
    r"(?:\s*(%|процент\w*|мин\.?|минут\w*|сек\.?|секунд\w*))?"
    r"\s*[.!?…]*\s*$",
    re.IGNORECASE)
_SLIDER_DRAG_VERB_RE = re.compile(
    r"^\s*(?:перетащи|перетащить|двинь)\s", re.IGNORECASE)
# Без глагола шкалы: «слайдер громкости на 70», «сделай звук 20%»,
# «громкость 50 процентов» — громкость (исполнение уводит её в <video>)
_SLIDER_VOLUME_BARE_RE = re.compile(
    r"^\s*(?:(?:сделай|поставь|выставь|установи|сделать)\s+)?"
    r"(?:(?:ползун\w*|слайдер\w*|регулятор\w*)\s+)?"
    r"(громкост\w*|звук\w*|volume)\s+(?:на\s+|в\s+|to\s+)?(\d{1,3})"
    r"\s*(%|процент\w*|percent)?\s*[.!?…]*\s*$", re.IGNORECASE)
# «перемотай ползунок на 2 минуты» — ползунок медиа-прогресса (абсолютная
# позиция). Без слова «ползунок/слайдер» «перемотай на 2 минуты» — не про
# абсолютную позицию (это может быть и относительный сдвиг) — не берём
_SLIDER_SEEK_RE = re.compile(
    r"^\s*(?:перемотай|перемотать|промотай|промотать|отмотай|отмотать)\s+"
    r"(?:видео\s+|ролик\s+)?(?:ползун\w*|слайдер\w*|бегун\w*)"
    r"(?:\s+(?:видео|ролика|прогресса|перемотки))?\s+(?:на|до)\s+(\d{1,4})"
    r"\s*(мин\.?|минут\w*|сек\.?|секунд\w*)\s*[.!?…]*\s*$", re.IGNORECASE)
# Слова шкалы: сам контрол или величина, которую ползунком задают
_SLIDER_WORD_RE = re.compile(
    r"(?<![а-яёa-z])(?:ползун\w*|бегун\w*|слайдер\w*|шкал\w*|громкост\w*|"
    r"звук\w*|яркост\w*|контраст\w*|прогресс\w*|перемотк\w*|позици\w*|"
    r"значени\w*|уровен\w*|уровн\w*|масштаб\w*|скорост\w*|"
    r"slider|volume|progress|brightness|level|value)", re.IGNORECASE)


def parse_slider_request(
        text: str) -> Optional[Tuple[Tuple[str, int, str], Optional[str]]]:
    """«перетащи слайдер рабочие часы на 8» → ((«рабочие часы», 8, ""), None);
    «выставь громкость на 50 процентов» → ((«громкость», 50, "pct"), None).
    None — не команда слайдера. Числовой хвост «на N» (с опциональной
    единицей) обязателен и должен завершать фразу — иначе это не про
    ползунок. Сайт не выделяем («в день» из подписи не должно становиться
    сайтом) — целимся в текущую/названную вкладку как есть."""
    if not text or len(text) > 80:
        return None
    mv = _SLIDER_VOLUME_BARE_RE.match(text)
    if mv:
        # Единица — как у общей формы: без явного % пусто; громкость 0..100
        # исполнение и так уводит в <video> долей (N/100)
        return (mv.group(1).lower(), int(mv.group(2)),
                "pct" if mv.group(3) else ""), None
    ms = _SLIDER_SEEK_RE.match(text)
    if ms:
        unit = "min" if ms.group(2).lower().startswith("мин") else "sec"
        # «перемотка» — синоним шкалы прогресса у JS-поиска ползунка
        return ("перемотка", int(ms.group(1)), unit), None
    m = _SLIDER_REQUEST_RE.match(text)
    if not m:
        return None
    label = m.group(1).strip().strip('"«»').strip()
    if len(label) > 40:
        return None
    # «установи будильник на 7», «передвинь встречу на 15 минут» — не
    # ползунок: «поставь/установи/выставь/передвинь» слишком общие, им
    # нужно слово шкалы. Перетаскивание («перетащи/двинь») — само про контрол
    if not (_SLIDER_DRAG_VERB_RE.match(text) or _SLIDER_WORD_RE.search(text)):
        return None
    raw_unit = (m.group(3) or "").lower().rstrip(".")
    unit = ""
    if raw_unit:
        if raw_unit == "%" or raw_unit.startswith("проц"):
            unit = "pct"
        elif raw_unit.startswith("мин"):
            unit = "min"
        elif raw_unit.startswith("сек"):
            unit = "sec"
    return (label, int(m.group(2)), unit), None


# Отображение единицы слайдера в вопросах/отчётах: 50% / 10 мин / 30 сек
_SLIDER_UNIT_RU = {"pct": "%", "min": " мин", "sec": " сек"}


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
# Англ.: «enter/exit control mode», «control mode on/off», «turn on/off
# control mode», «switch to control mode», «leave control mode»
_CONTROL_MODE_EN_ON_RE = re.compile(
    r"^\s*(?:(?:enter|start|enable|activate|turn\s+on|switch\s+(?:on|to)|"
    r"go\s+(?:in)?to)\s+(?:the\s+)?control\s+mode|"
    r"control\s+mode(?:\s+on)?)\s*[.!…]*\s*$", re.IGNORECASE)
_CONTROL_MODE_EN_OFF_RE = re.compile(
    r"^\s*(?:(?:exit|leave|quit|stop|disable|deactivate|turn\s+off|"
    r"switch\s+off|get\s+out\s+of)\s+(?:the\s+)?control\s+mode|"
    r"control\s+mode\s+off)\s*[.!…]*\s*$", re.IGNORECASE)


def parse_control_mode(text: str) -> Optional[bool]:
    """Команда переключения режима управления: True — включить
    («перейди в режим управления», «enter control mode»), False —
    выключить («выйди из режима управления», «control mode off»),
    None — не про режим."""
    if not text or len(text) > 60:
        return None
    text = _strip_polite(text)
    if _CONTROL_MODE_OFF_RE.match(text) or _CONTROL_MODE_EN_OFF_RE.match(text):
        return False
    if _CONTROL_MODE_ON_RE.match(text) or _CONTROL_MODE_EN_ON_RE.match(text):
        return True
    return None


# Объекты закрытия, которые бывают на странице (слово целиком, любой падеж)
_CLOSE_UI_OBJECT_RE = re.compile(
    r"^(?:окн\w*|окошк\w*|модал\w*|попап\w*|pop-?up\w*|диалог\w*|баннер\w*|"
    r"реклам\w*|уведомлен\w*|подсказк\w*|анкет\w*|форм[ауые]?|формочк\w*|"
    r"меню|крестик\w*|видео|плеер\w*|миниплеер\w*|панел\w*|сайдбар\w*|"
    r"чат\w*|комментари\w*|описани\w*|спис(?:ок|ка|ке)|раздел\w*|секци\w*|"
    r"блок\w*|карточк\w*|превью|субтитр\w*|фильтр\w*|корзин\w*|куки|"
    r"cookies?|оверле\w*|шторк\w*|поиск\w*|предупреждени\w*|сообщени\w*|"
    r"это|этот|эту|его|её|ее|их)$", re.IGNORECASE)


def parse_close_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«закрой окно» / «закрой соусы к бортикам (на ютубе)» → (цель С
    глаголом, сайт) — разбор закрытия (generic/целевое) делает
    resolve_click. None — не команда закрытия."""
    if not text or len(text) > 80:
        return None
    text = _strip_polite(text)
    m = re.match(r"^\s*(закрой|закрыть|скрой|скрыть|сверни|свернуть)\s+(.+?)\s*[.!?…]*\s*$",
                 text, re.IGNORECASE)
    if not m:
        return None
    if re.match(r"(?:(?:эту|этот|эта|текущую|текущий|текущее|все|всё)\s+)?"
                r"(?:вкладк\w*|страниц\w*|таб\w*)\b", m.group(2),
                re.IGNORECASE):
        return None  # «закрой вкладку/страницу» — команда вкладке (parse_tab_op)
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
    # «закрой рот/глаза/тему», «скрой свои эмоции», «сверни разговор» — речь,
    # а не страница. Без явного места (сайт/«на этой странице») объект
    # должен быть элементом интерфейса; прочее решает LLM-ярус
    if not site and not any(_CLOSE_UI_OBJECT_RE.match(w)
                            for w in goal.split()[1:4]):
        return None
    return goal, site or None


_TAB_OP_MOD_RE = re.compile(
    r"^(?:(?:эту|этот|эта|этой|этого|текущую|текущий|текущее|текущей|"
    r"данную|данной|это)(?:\s+|$))",
    re.IGNORECASE)


# Хвост-место у голого «назад»/«вперёд»: только «на <одно слово>»
# («назад на ютубе»). Всё остальное («назад в будущее») — не команда вкладки
_TAB_OP_PLACE_RE = re.compile(r"(?:на|on|in)\s+\S+", re.IGNORECASE)
_TAB_OP_PLACE_PREP_RE = re.compile(r"^(?:на|on|in)\s+", re.IGNORECASE)
# Слово кнопки браузера → op (для «нажми назад» и англ. форм)
_TAB_OP_WORDS = (
    ("back", re.compile(r"^(?:назад|обратно|back)$", re.IGNORECASE)),
    ("forward", re.compile(r"^(?:вперёд|вперед|forward)$", re.IGNORECASE)),
    ("reload", re.compile(r"^(?:обнови\w*|перезагрузи\w*|refresh|reload)$",
                          re.IGNORECASE)),
)
_TAB_OP_CLICK_RE = re.compile(
    r"^\s*(?:нажми|нажать|кликни|кликнуть|тыкни|щёлкни|щелкни|click|press|"
    r"tap|hit)\s+(?:(?:на|по|on)\s+)?(?:кнопк\w+\s+|the\s+)?"
    r"(назад|обратно|вперёд|вперед|обновить|обнови|перезагрузить|"
    r"перезагрузи|back|forward|refresh|reload)(?:\s+button)?\s*[.!?…]*\s*$",
    re.IGNORECASE)
_TAB_OP_NEW_RE = re.compile(
    r"^\s*(?:(?:открой|открыть|создай|создать|сделай|добавь|open|create)\s+)?"
    r"(?:(?:a|an)\s+)?(?:нов\w+\s+(?:вкладк\w*|таб\w*)|new\s+tab)"
    r"\s*[.!?…]*\s*$", re.IGNORECASE)
_TAB_OP_CLOSE_ALL_RE = re.compile(
    r"^\s*(?:закрой|закрыть|close)\s+(?:все|всё|all)(?:\s+(?:the\s+)?"
    r"(?:вкладк\w*|страниц\w*|окна|tabs|pages|windows))?"
    r"(?:\s+(?:кроме\s+\S+|except\s+\S+))?\s*[.!?…]*\s*$", re.IGNORECASE)
# «close the tab», «close this tab», «close the youtube tab»
_TAB_OP_EN_CLOSE_RE = re.compile(
    r"^\s*close\s+(?:the\s+|this\s+|current\s+|that\s+)*(?:(.+?)\s+)?tab"
    r"\s*[.!?…]*\s*$", re.IGNORECASE)
_TAB_OP_EN = (
    ("back", re.compile(
        r"^\s*(?:go\s+back|back|go\s+to\s+(?:the\s+)?previous\s+page|"
        r"previous\s+page)(?:\s+(?:a\s+)?page)?\s*[.!?…]*\s*$",
        re.IGNORECASE)),
    ("forward", re.compile(
        r"^\s*(?:go\s+)?forward(?:\s+(?:a\s+)?page)?\s*[.!?…]*\s*$",
        re.IGNORECASE)),
    ("reload", re.compile(
        r"^\s*(?:refresh|reload)(?:\s+(?:the\s+|this\s+|current\s+)*"
        r"(?:page|tab))?\s*[.!?…]*\s*$", re.IGNORECASE)),
)


def parse_tab_op(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«обнови (эту) страницу» / «перезагрузи вкладку» / «закрой вкладку
    ютуба» / «закрой вкладку со стримингом» / «вернись назад (на ютубе)» /
    «перейди вперёд» → (op, цель|None); op — "reload" | "close" | "back" |
    "forward". Цель — какая вкладка (как в «перейди на вкладку X»);
    None — текущая видимая. Голое «обнови»/«назад»/«вперёд» без слова
    «вкладка/страница» принимаем (в режиме управления это может быть только
    про вкладку), а «закрой» без него — нет: «закрой окно/попап» остаётся
    клик-закрытием (parse_close_request). «обнови ленту» — тоже не сюда.
    Особые op: "new" («открой новую вкладку») и "close_all» («закрой все
    вкладки») — resolve_tab_op на них отвечает подсказкой, а не действием."""
    if not text or len(text) > 80:
        return None
    text = _strip_polite(text)
    # «нажми назад/обновить», «кликни вперёд» — это кнопки браузера, а не
    # элементы страницы
    m = _TAB_OP_CLICK_RE.match(text)
    if m:
        w = m.group(1).lower()
        return next(op for op, rx in _TAB_OP_WORDS if rx.match(w)), None
    if _TAB_OP_NEW_RE.match(text):
        return "new", None
    if _TAB_OP_CLOSE_ALL_RE.match(text):
        return "close_all", None
    m = _TAB_OP_EN_CLOSE_RE.match(text)
    if m:
        goal = _strip_tab_filler(m.group(1) or "").strip(_TARGET_EDGE_CHARS)
        return "close", (goal or None)
    for op, rx in _TAB_OP_EN:
        if rx.match(text):
            return op, None
    m = re.match(r"^\s*(?:обнови|обновить|перезагрузи|перезагрузить)\b"
                 r"\s*(.*?)\s*[.!?…]*\s*$", text, re.IGNORECASE)
    op = "reload"
    if not m:
        m = re.match(r"^\s*(?:закрой|закрыть)\b\s*(.*?)\s*[.!?…]*\s*$",
                     text, re.IGNORECASE)
        op = "close"
    if not m:
        m = re.match(r"^\s*(?:(?:вернись|вернуться)\s+)?(?:шаг\s+)?"
                     r"(?:назад|обратно)\b\s*(.*?)\s*[.!?…]*\s*$",
                     text, re.IGNORECASE)
        op = "back"
    if not m:
        m = re.match(r"^\s*(?:(?:перейди|переходи|иди)\s+)?(?:шаг\s+)?"
                     r"впер[её]д\b\s*(.*?)\s*[.!?…]*\s*$",
                     text, re.IGNORECASE)
        op = "forward"
    if not m:
        # «вернись на предыдущую/прошлую страницу»
        m = re.match(r"^\s*(?:вернись|вернуться)\s+на\s+"
                     r"(?:предыдущ\w+|прошл\w+|прошедш\w+)\s+страниц\w*\b"
                     r"\s*(.*?)\s*[.!?…]*\s*$", text, re.IGNORECASE)
        op = "back"
    if not m:
        return None
    rest = m.group(1).strip()
    tm = re.search(r"(?:^|\s)(вкладк\w*|страниц\w*|таб\w*)\b", rest,
                   re.IGNORECASE)
    if tm:
        goal = (rest[:tm.start()] + " " + rest[tm.end():]).strip()
        goal = re.sub(r"^(?:на|в|во)\s+", "", goal, flags=re.IGNORECASE)
        goal = _TAB_OP_MOD_RE.sub("", goal.strip()).strip()
        goal = re.sub(r"^(?:с|со)\s+", "", goal, flags=re.IGNORECASE).strip()
    else:
        if op == "close":
            return None  # «закрой окно» — клик-закрытие, не вкладка
        if op == "reload" and rest:
            return None  # «обнови ленту» — не про вкладку
        # «назад на ютубе» — цель без слова «вкладка». Хвост допускаем ТОЛЬКО
        # в форме указания места «на <одно слово>»: «назад в будущее» —
        # название фильма, а не «назад» на вкладке «будущее» (предлог «в»
        # тут винительный, а не о месте)
        if rest and not _TAB_OP_PLACE_RE.fullmatch(rest):
            return None
        goal = _TAB_OP_PLACE_PREP_RE.sub("", rest)
        goal = _TAB_OP_MOD_RE.sub("", goal.strip()).strip()
    goal = re.sub(r"\bпожалуйста\b", "", goal, flags=re.IGNORECASE).strip()
    # Второй носитель в цели («закрой вкладку сайта иванов») — тоже не имя
    goal = _strip_tab_filler(goal)
    goal = goal.strip().strip('"«»').strip(" ,").strip()
    if len(goal) > 40:
        return None
    return op, goal or None

# Отслеживаемая вкладка как цель: «на этой странице», «на открывшейся», …
# Расширения файлов, которые доменом не бывают: «открой config.py» —
# файл проекта, а не сайт (уходило на https://config.py молча). Одно
# определение «похоже на домен» для всех путей (открытие, снапшот, клик,
# наведение, ввод, чтение секции) — общий источник истины вместо
# дублирующихся проверок «точка в слове» в каждом из них
_FILE_EXT_NOT_TLD = frozenset({
    "py", "js", "ts", "tsx", "jsx", "json", "yaml", "yml", "toml", "ini",
    "cfg", "conf", "env", "lock", "md", "txt", "log", "csv", "tsv", "xml",
    "html", "htm", "css", "scss", "sh", "bat", "ps1", "sql", "pdf", "doc",
    "docx", "xls", "xlsx", "ppt", "pptx", "rtf", "zip", "rar", "tar", "gz",
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "mp3", "mp4", "mov",
    "avi", "mkv", "wav", "exe", "dmg", "pkg", "apk", "iso", "bin", "dat",
    "db", "sqlite", "bak", "tmp", "epub", "fb2"})


def _looks_like_domain(token: str) -> bool:
    """«site.example.ru», «example.com/page», «127.0.0.1:8000» — адрес;
    «config.py», «отчёт.docx», «слово», «два слова» — нет. Хост берём до
    первого /?#, порт отбрасываем; TLD — только буквы (или IPv4 целиком).
    Одно-словное «имя.расширение» без пути считаем файлом."""
    t = " ".join(str(token or "").split()).strip().lower()
    if not t or " " in t:
        return False
    if "://" in t:
        return True  # явная схема — адрес (нормализацию делает _normalize_url)
    host = t.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if ":" in host:
        host = host.split(":", 1)[0]
    if "." not in host or host.startswith(".") or host.endswith("."):
        return False
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        return True  # IPv4 (localhost-адреса веб-интерфейса)
    tld = host.rsplit(".", 1)[1]
    if not re.fullmatch(r"[a-zа-яё]{2,24}", tld):
        return False
    return not (tld in _FILE_EXT_NOT_TLD and host == t)


def _get_safe_redirects(client, url: str):
    """GET стороннего URL с SSRF-фильтром на каждом хопе — тонкая обёртка
    над общим web_search.get_with_safe_redirects (одна политика и один
    обход цепочки на проект). Клиент — с follow_redirects=False.
    → (ответ, финальный URL) или (None, причина отказа)."""
    from app.features.web_search import get_with_safe_redirects
    return get_with_safe_redirects(client, url)


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
    """«нажми кнопку скачать на почте» → («скачать», «почте»).
    «на этой странице»/«на открывшейся» → сайт PAGE_REF (отслеживаемая
    вкладка). None — не команда клика."""
    if not text or len(text) > 80:
        return None
    text = _strip_polite(text)
    m = _CLICK_REQUEST_RE.match(text)
    ui_open = False
    if not m:
        # «открой комментарии/настройки на ютубе», «включи субтитры» —
        # элемент интерфейса, а не сайт и не поиск ролика
        m = _UI_OPEN_RE.match(text)
        if not m:
            return None
        ui_open = True
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
        else:
            hm = _CLICK_SITE_HEAD_RE.match(goal)
            if hm and not _CLICK_SITE_HEAD_NOT_RE.match(hm.group(1)) \
                    and not _CLICK_SITE_HEAD_ADJ_RE.search(hm.group(1)):
                word = hm.group(1).lower()
                goal = hm.group(2).strip()
                # «на странице/сайте войти» — пустое указание места
                site = None if word in _NOOP_SITE_WORDS else word
    goal = _CLICK_FILLER_RE.sub("", goal, count=1).strip()
    goal = _CLICK_FILLER_TAIL_RE.sub("", goal).strip(_TARGET_EDGE_CHARS)
    if not goal or len(goal) > 40:
        return None
    if ui_open and not (_UI_ELEMENT_RE.match(goal)
                        and (site or _UI_PLAYER_RE.match(goal))):
        return None
    return goal, site or None


# «наведи (курсор/мышь) на X» — hover без клика: раскрыть hover-меню,
# hover-кнопки карточки («в очередь», «посмотреть позже»), свёрнутый
# слайдер громкости. Исполнение — реальное движение мыши (browser_actions.
# hover_tagged), синтетические события CSS :hover не включают
_HOVER_REQUEST_RE = re.compile(
    r"^\s*(?:наведи|навести|подведи|подвести|подержи|подержать)\s+"
    r"(?:(?:курсор|мышь|мышку|мышкой)\s+)?(?:на|над|к|ко|во?)\s+"
    r"(.+?)\s*[.!?…]*\s*$",
    re.IGNORECASE)
_HOVER_REQUEST_EN_RE = re.compile(
    r"^\s*hover\s+(?:over\s+)?(.+?)\s*[.!?…]*\s*$", re.IGNORECASE)


def parse_hover_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«наведи курсор на меню (на ютубе)» → («меню», «ютубе»).
    None — не команда наведения."""
    if not text or len(text) > 80:
        return None
    m = _HOVER_REQUEST_RE.match(text) or _HOVER_REQUEST_EN_RE.match(text)
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
# «открой вкладку с почтой» — тоже переключение: со словом «вкладку» это
# про открытую вкладку, а не про сайт «с почтой» из поисковика
_TAB_SWITCH_RE = re.compile(
    r"^\s*(?:перейди|перейти|переключись|переключи|переключить|покажи|"
    r"показать|открой|открыть|вернись|вернуться|зайди|зайти)\s+"
    r"(?:на\s+|во?\s+)?(?:вкладку|вкладка|вкладке|таб|табу|tab)\s+(.+?)"
    r"\s*[.!?…]*\s*$",
    re.IGNORECASE)
# «switch to the youtube tab», «go to tab mail», «open the mail tab»
_TAB_SWITCH_EN_RE = re.compile(
    r"^\s*(?:(?:switch|go|jump|move|change|get)\s+(?:back\s+)?to|open)\s+"
    r"(?:the\s+)?(?:(?!new\s)(.+?)\s+tab|tab\s+(.+?))\s*[.!?…]*\s*$",
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
# Слова-«носители» в цели вкладки: «(на) страницу иванов», «вкладка
# ютуба», «сайт банка», «окно почты» — называют ВИД объекта, а не какой
# именно. В заголовке вкладки их нет, а матч требует совпадения КАЖДОГО
# слова цели — «страницу иванов» не находило вкладку «Университет —
# ИВАНОВ А. А.». Все падежи: вкладк*/страниц*/страничк*/сайт*/окн*/таб*
_TAB_GOAL_FILLER_RE = re.compile(
    r"(?<![\wё])(?:вкладк\w*|страниц\w*|страничк\w*|сайт\w*|"
    r"окн(?:о|а|е|у|ом|ами|ах)?|окошк\w*|таб(?:а|у|е|ом|ы|ов)?|"
    r"web-?site|site|pages?|tabs?)(?![\wё])",
    re.IGNORECASE)


def _strip_tab_filler(goal: str) -> str:
    """Цель вкладки без слов-носителей («страницу иванов» → «иванов»,
    «вкладка с ютубом» → «ютубом»: «с/со/with» после носителя — тоже
    служебное). Пусто — цель была одним носителем."""
    g = _TAB_GOAL_FILLER_RE.sub(" ", goal or "")
    g = " ".join(g.split()).strip(" ,.-—–").strip()
    return re.sub(r"^(?:с|со|with)\s+", "", g, flags=re.IGNORECASE).strip()


_TAB_WORD_RE = re.compile(r"[a-z0-9а-я]+")
# Окончания, которыми могут различаться формы одного слова при сверке
# основ («ивановым» ~ «ИВАНОВ», «иванову» ~ «Иванова»)
_TAB_WORD_TAILS = frozenset(("", "ым", "им", "ою", "ею", "ом", "ем",
                             "ами", "ями", "ах", "ях", "ов", "ев",
                             "ой", "ей", "ую", "юю", "ая", "ое", "ые", "ие",
                             "а", "я", "у", "ю", "е", "и", "ы", "о", "ь"))


def _fold_yo(s: str) -> str:
    # ё → е: в заголовках и речи пишут по-разному («Вёрстка»/«верстка»).
    return s.replace("ё", "е")


def _tab_word_stem_hit(w: str, words: List[str]) -> bool:
    """Слово цели ~ слово заголовка/хоста по основе: общий префикс ≥ 4 и
    ≥ длины короткого без 2 букв, а хвосты обоих — падежные окончания
    («ивановым» ~ «иванов», «иванову» ~ «иванова»; «иван-чай» ≠ «иванов»)."""
    from app.features.web_search import _stem
    sw = _stem(w)
    for x in words:
        if len(sw) >= 4 and x.startswith(sw):
            return True
        n = min(len(w), len(x))
        if n < 4:
            continue
        p = 0
        while p < n and w[p] == x[p]:
            p += 1
        if (p >= max(4, n - 2) and w[p:] in _TAB_WORD_TAILS
                and x[p:] in _TAB_WORD_TAILS):
            return True
    return False


def parse_tab_switch(text: str) -> Optional[Tuple[str, bool]]:
    """«перейди на вкладку ютуб» → («ютуб», True — явная форма);
    «переключись на банк» → («банк», False — мягкая);
    «переключись на страницу иванов» → («иванов», False) — слова-носители
    (страница/сайт/окно, любой падеж) из цели срезаются. None — не команда
    переключения вкладки."""
    if not text or len(text) > 80:
        return None
    text = _strip_polite(text)
    m = _TAB_SWITCH_RE.match(text)
    if m:
        goal = m.group(1).strip().strip(_TARGET_EDGE_CHARS)
        goal = _strip_tab_filler(goal).strip(_TARGET_EDGE_CHARS) or goal
        return (goal, True) if goal else None
    m = _TAB_SWITCH_EN_RE.match(text)
    if m:
        goal = (m.group(1) or m.group(2) or "").strip(_TARGET_EDGE_CHARS)
        goal = _strip_tab_filler(goal).strip(_TARGET_EDGE_CHARS) or goal
        return (goal, True) if goal else None
    m = _TAB_SWITCH_SOFT_RE.match(text)
    if not m:
        return None
    goal = _strip_tab_filler(m.group(1).strip().strip('"«»').strip())
    goal = goal.strip('"«»').strip()
    # «перейди на эту/текущую страницу», «на сайт» — бессмысленно, не
    # команда: после среза носителя осталась пустота или «эту/текущую»
    if not goal or not _TAB_OP_MOD_RE.sub("", goal + " ").strip():
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
    r"^(?:(?:мне|нам|на|по)\s+)*(?:(?:файл|файлы|документ|документы|pdf|"
    r"ссылку|ссылка)\s+)?", re.IGNORECASE)
# «сохрани» — бытовое слово («сохрани это в памяти», «сохрани мой номер»):
# скачиванием оно становится только с объектом-файлом или явным местом
_SAVE_VERB_RE = re.compile(r"^\s*(?:сохрани|сохранить)\s", re.IGNORECASE)
_SAVE_FILE_OBJECT_RE = re.compile(
    r"(?<![а-яёa-z])(?:файл\w*|pdf|пдф\w*|картинк\w*|изображени\w*|фото\w*|"
    r"фотк\w*|снимок|снимк\w*|документ\w*|видео|ролик\w*|скриншот\w*|"
    r"вложени\w*|архив\w*|книг\w*|методичк\w*|презентаци\w*|таблиц\w*|"
    r"image|picture|photo|file|document|video)", re.IGNORECASE)
# «на телефоне/компе/память» — куда сохранить, а не сайт
_SAVE_NOT_SITE_WORDS = frozenset({
    "телефоне", "телефон", "компе", "компьютере", "ноуте", "ноутбуке",
    "диске", "флешке", "память", "памяти", "потом", "будущее", "всякий",
    "завтра", "всегда", "время", "later", "phone", "computer", "disk"})


def parse_download_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«скачай файл методичку по sql на site.example.ru» → («методичку по sql»,
    «site.example.ru»); «на этой странице» → сайт PAGE_REF. None — не команда
    скачивания."""
    if not text or len(text) > 80:
        return None
    text = _strip_polite(text)
    m = _DOWNLOAD_REQUEST_RE.match(text)
    if not m:
        return None
    goal = m.group(1).strip()
    site = None
    site_prep = ""
    goal, is_page = _strip_page_ref(goal)
    if is_page:
        site = PAGE_REF
    else:
        sm = _CLICK_SITE_RE.search(goal)
        if sm:
            site = sm.group(1).strip().lower()
            site_prep = sm.group(0).split()[0].lower()
            goal = goal[:sm.start()].strip()
    # «сохрани это в памяти» — «в X» тут КУДА сохранить, а не сайт;
    # «сохрани» без объекта-файла и без явного сайта («на гитхабе»,
    # домен) — не скачивание
    if _SAVE_VERB_RE.match(text) and not is_page \
            and not _SAVE_FILE_OBJECT_RE.search(goal) \
            and not (site and (_looks_like_domain(site)
                               or (site_prep in ("на", "on")
                                   and site not in _SAVE_NOT_SITE_WORDS))):
        return None
    goal = _DOWNLOAD_FILLER_RE.sub("", goal, count=1).strip()
    goal = goal.strip(_TARGET_EDGE_CHARS)
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
# отправки — безымянная иконка, как в веб-чатах LLM)
_TYPE_SUBMIT_RE = re.compile(
    r"\s+(?:и\s+)?(?:отправь|отправить|отправляй|пошли|шли|send|submit)\s*$",
    re.IGNORECASE)
# «введи X в поиск»: «поиск» — само название поля, слова «поле» нет. Без
# этого фраза не считалась явной командой ввода и при несовпадении подписей
# уходила в LLM-поток, который «изображал» ввод, ничего не делая
_TYPE_SEARCH_SEP_RE = re.compile(r"\s+(?:в|во)\s+поиск\w*\s*[.!?…]*$",
                                 re.IGNORECASE)
# Указание поля где угодно в теле: «в поле/форму/поиск/чат/строку/
# комментарий…», «into …», «in the search box». Без него «напиши X» — не ввод
_TYPE_FIELD_MARK_RE = re.compile(
    r"\s(?:(?:в|во)\s+(?:пол[еяю]\w*|форм\w*|поиск\w*|поисков\w*|чат\w*|"
    r"строк\w*|строчк\w*|окошк\w*|окн[оеа]\w*|комментари\w*|коммент\w*|"
    r"сообщени\w*|адресн\w*|граф[уеы]\w*|ячейк\w*|инпут\w*|input\w*)|"
    r"into|in\s+(?:the\s+)?(?:field|search|chat|box|input|form))(?![\wё])",
    re.IGNORECASE)
_TYPE_EXPLICIT_VERBS = frozenset({
    "введи", "ввести", "впиши", "вписать", "набери", "набрать",
    "type", "enter"})


def parse_type_request(text: str) -> Optional[str]:
    """«введи в поле поиск кофе» → тело команды. Поле, текст
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
    # «enter control mode» — переключение режима, а не ввод текста
    if parse_control_mode(text) is not None:
        return None
    verb = text.split(None, 1)[0].lower()
    marked = bool(_TYPE_FIELD_MARK_RE.search(f" {body}")
                  or _TYPE_SUBMIT_RE.search(body))
    # «напиши рассказ/привет» — просьба к собеседнику, а не ввод в страницу:
    # «напиши» — команда только с указанием поля («в поле/в поиск/в чат…»)
    if verb in ("напиши", "написать") and not marked:
        return None
    # Одно слово без поля — явная команда только у «введи/впиши/набери»
    # («заполни анкету» — не текст для ввода)
    if len(body.split()) == 1 and not marked \
            and verb not in _TYPE_EXPLICIT_VERBS:
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
_OPEN_TAB_BODY_RE = re.compile(
    r"^(?:(?:нов\w+|эту|текущую|другую|соседнюю|следующую|предыдущую|"
    r"the|a|new)\s+)?(?:вкладк\w*|tab)(?![\wё])", re.IGNORECASE)
# Бренд-подобное имя для поискового резолва в fast-path: латиница/цифры,
# одно-два слова («figma», «hh», «chat gpt»). Кириллица («душу», «свет»,
# «мне секрет») и англ. фразы с артиклем/местоимением — обычные слова
_BRAND_LIKE_RE = re.compile(
    r"[a-z0-9][a-z0-9&+.'_-]*(?:\s+[a-z0-9][a-z0-9&+.'_-]*)?")
_BRAND_NOT_WORDS = frozenset({
    "the", "a", "an", "my", "your", "our", "his", "her", "their", "this",
    "that", "it", "me", "up", "door", "window", "light", "lights", "eyes",
    "mind", "heart", "new", "tab", "page", "site", "app"})


def _brand_like_name(name: str) -> bool:
    key = " ".join(str(name or "").lower().split())
    if not _BRAND_LIKE_RE.fullmatch(key):
        return False
    return not any(w in _BRAND_NOT_WORDS for w in key.split())


def parse_open_many(text: str, known=None) -> Optional[List[str]]:
    """«открой ютуб и запусти музыку» → [«ютуб», «музыку»]. None — не голая
    команда (длинная фраза, стоп-слова). Части после «и» могут иметь свой
    глагол и филлеры («…и сайт универа»).
    Англ. «and» — часть названия («Barnes and Noble», «Tom and Jerry»):
    делим по нему, только если КАЖДАЯ часть — известная цель: known(имя)
    → True (алиас/приложение/домен, ComputerControlManager.is_known_target);
    без known — только явные домены."""
    if not text or len(text) > 80:
        return None
    t = _strip_polite(text.strip().rstrip(".!?…").strip())
    t = t.strip().rstrip(".!?…").strip()
    if not _OPEN_VERB_RE.match(t):
        # Голое «следующее видео» — тот же встроенный рецепт, что «открой
        # следующее видео» (кнопка плеера), без глагола
        return [t] if next_video_recipe(t) else None
    body = _OPEN_VERB_RE.sub("", t, count=1).strip()
    # «открой (новую) вкладку (с почтой)» — про вкладки (parse_tab_op /
    # parse_tab_switch), а не сайт «с почтой» из поисковика
    if _OPEN_TAB_BODY_RE.match(body):
        return None
    parts: List[str] = []
    chunks: List[str] = []
    for chunk in re.split(r"\s+и\s+", body, flags=re.IGNORECASE):
        subs = re.split(r"\s+and\s+", chunk, flags=re.IGNORECASE)
        if len(subs) > 1:
            names = []
            for si, s in enumerate(subs):
                s = s.strip(_TARGET_EDGE_CHARS)
                if _OPEN_VERB_RE.match(s):
                    s = _OPEN_VERB_RE.sub("", s, count=1).strip()
                elif s and si:
                    h = s.split(None, 1)[0].lower().split("-")[0]
                    if h in _SC_RU_VERBS or h in _SC_EN_VERBS:
                        # «open youtube and play music» — составная команда
                        return None
                names.append(_OPEN_TAIL_RE.sub("", s).strip(_TARGET_EDGE_CHARS))
            _known = known if callable(known) else _looks_like_domain
            try:
                split_ok = all(n and _known(n) for n in names)
            except Exception:
                split_ok = False
            if not split_ok:
                subs = [chunk]  # «Barnes and Noble» — одно название
        chunks.extend(subs)
    for part in chunks:
        part = part.strip(_TARGET_EDGE_CHARS)
        if _OPEN_VERB_RE.match(part):
            part = _OPEN_VERB_RE.sub("", part, count=1).strip()
        else:
            # «…и нажми на пепперони», «…, введи X и отправь» — составная
            # команда (split_compound_command), а не второй сайт «нажми …»
            w0 = part.split(None, 1)[0].lower().split("-")[0] if part else ""
            if w0 in _SC_RU_VERBS or w0 in _SC_EN_VERBS:
                return None
        prev = None
        while prev != part:  # филлеры-префиксы срезаем до упора («мне сайт …»)
            prev = part
            part = _OPEN_FILLER_RE.sub("", part, count=1).strip()
        part = _OPEN_TAIL_RE.sub("", part).strip(_TARGET_EDGE_CHARS)
        if not part or len(part) > 40 or part.lower() in _OPEN_STOPLIST:
            return None
        parts.append(part)
    return parts or None


def parse_open_request(text: str) -> Optional[str]:
    """Одна голая цель «открой X» → «X». Несколько целей («…и …») — None,
    их разбирает parse_open_many."""
    parts = parse_open_many(text)
    return parts[0] if parts and len(parts) == 1 else None


# Явный адрес где-то в команде открытия: «открой на example.com/827 студентам — …».
# Схема (http/https) сохраняется, если указана
_URL_TOKEN_RE = re.compile(
    r"\b((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:/[^\s]*)?)",
    re.IGNORECASE)
# Хвост после адреса — путь по странице: филлеры/предлоги в начале срезаем,
# сегменты разделяются « - », « — », « > », «→» (дефис — только с пробелами,
# чтобы не рвать слова вроде «англо-русский»)
_NAV_FILLER_RE = re.compile(
    rf"^(?:{_POLITE_ALT}|мне|нам|сайт|страницу|страница|вкладку|вкладка|"
    r"раздел|пункт)(?:[\s,]+|$)", re.IGNORECASE)
_NAV_PREP_RE = re.compile(r"^(?:на|в|во|on|in)\s+", re.IGNORECASE)
_NAV_SPLIT_RE = re.compile(r"\s+[-–—>]\s+|\s*→\s*")
NAV_MAX_STEPS = 5
# Ожидание загрузки страницы и пауза между попытками снапшота: первая
# загрузка сайта может занять секунды. Паузы после действий — НЕ слепые
# слипы, а ba.wait_dom_idle (стабильность DOM); NAV_SETTLE_SEC — его бюджет
NAV_SETTLE_SEC = 2.0
NAV_LOAD_TIMEOUT_SEC = 10.0
NAV_POLL_SEC = 0.7

# Органы управления страницей — в кандидаты LLM-восстановления шага
# навигации попадают принудительно (именно они открывают скрытые разделы:
# меню/бургер раскрывают панель, крестик закрывает незапланированный попап)
_NAV_CTL_RE = re.compile(
    r"меню|menu|бургер|burger|закрыт|close|войти|кабинет|назад|главн|home",
    re.IGNORECASE)

# Скоринг кандидатов: явный лидер — без LLM; иначе top-N в LLM;
# невалидный ответ — фолбэк на лучшего, если его скор внятен
LEADER_MIN_SCORE = 60.0    # минимум скора для детерминированного выбора
LEADER_MARGIN = 15.0       # минимальный отрыв от второго кандидата
LLM_TOP_N = 5              # столько кандидатов уходит в LLM-промпт
FALLBACK_MIN_SCORE = 50.0  # минимум для фолбэка на лучшего без/после LLM
LLM_WIDE_MAX = 30          # столько элементов снапшота уходит в широкий LLM-резолв
# Гибридный ярус (wide_mode: hybrid) — один vision-вызов вместо пары
# «широкий текстовый резолв → vision-рамки»: рамки на скриншоте + текстовый
# список элементов вне экрана, нумерация сквозная
# Записи аудита, где выбирался элемент страницы: на них штампуется wide_mode
_RESOLVE_KINDS = ("click", "download", "hover", "nav", "type",
                  "resolve_fail")
HYBRID_BOX_MAX = 12        # рамок на скриншоте (= размер палитры рамок)
HYBRID_TEXT_MAX = 30       # строк «без рамки» в промпте (бюджет символов — ниже)
HYBRID_PROMPT_MAX = 6000   # символов промпта (у vision-провайдера жёсткий лимит 8192)
# Резерв бюджета каскада под vision-ярусы (гибрид/рамки/зоны): доскролл и
# повторный снапшот его не трогают; не больше доли бюджета — иначе при малом
# resolve_budget_sec дешёвые ярусы не успевали бы вовсе
VISION_RESERVE_SEC = 9.0
VISION_RESERVE_SHARE = 0.4

# Синонимы к доступным именам иконочных кнопок: aria-label кнопки —
# «Меню аккаунта», а пользователь зовёт её «аватар». Ключ — слово цели,
# значения — корни-подстроки, засчитываемые за совпадение слова
_GOAL_SYNONYMS = {
    # Латинские стемы — для англоязычных интерфейсов и data-testid/class
    # иконок (встречаются сайты, где в aria только «Close»/«Notifications»
    # и ничего по-русски; без них «крестик» не находил «Close» и уходил в
    # текстовый LLM-резолв наугад)
    "аватар": ("аккаунт", "профил", "учётн", "учетн", "avatar", "profile",
               "account"),
    "аватарка": ("аккаунт", "профил", "учётн", "учетн", "avatar", "profile",
                 "account"),
    "ава": ("аккаунт", "профил", "avatar", "profile"),
    "аккаунт": ("профил", "аватар", "учётн", "учетн", "account", "profile",
                "avatar"),
    "учётка": ("аккаунт", "профил", "account", "profile"),
    "учетка": ("аккаунт", "профил", "account", "profile"),
    "профиль": ("аккаунт", "аватар", "учётн", "учетн", "profile", "account",
                "avatar"),
    "троеточие": ("ещё", "еще", "параметр", "действ"),  # «действ» — основа:
    # prefix-матч ловит и «Меню действий» YouTube, и «Действия»
    "лупа": ("поиск", "search"),
    "колокольчик": ("уведомлен", "notif", "bell"),
    "колокол": ("уведомлен", "notif", "bell"),
    "шестерёнка": ("настрой", "setting"),
    "шестеренка": ("настрой", "setting"),
    "закрой": ("закрыт", "закрыть", "close"),
    "закрытие": ("закрыть", "close"),
    "закрытия": ("закрыть", "close"),
    "крестик": ("закрыт", "close", "dismiss"),
    # «нажми состав» на странице товара — это кнопка «i» («Показать
    # дополнительную информацию» / «Калорийность и состав» в модалке).
    # Синонимы — усечённые префиксы («информац» матчит и «информация»,
    # и «информацию» — _word_in ищет с начала слова)
    "состав": ("информац", "калорийност", "кбжу"),
    "калорийность": ("состав", "информац"),
}

# Синонимы бургер-кнопки («три полоски»/«бургер-меню» → «бургер») —
# хост-зависимые, поэтому в общий _GOAL_SYNONYMS не входят: на YouTube она
# подписана «Гид»/«Guide», и «меню» там в синонимы НЕЛЬЗЯ — на той же
# странице есть «Меню аккаунта», будет ничья. На остальных сайтах бургер —
# это «Меню»/«Открыть меню»/«Menu»/«Открыть навигацию»; «burger» —
# латинский стем для data-testid безтекстовых иконок (например
# «MobileHeader.BurgerButton» на образовательных платформах)
_BURGER_WORDS = frozenset({"бургер", "гамбургер", "полоски"})
_BURGER_SYN_YT = ("гид", "guide")
_BURGER_SYN_GENERIC = ("меню", "menu", "навигац", "гид", "guide", "burger")


def _goal_synonyms(word: str, host: Optional[str] = None) -> Tuple[str, ...]:
    """Синонимы слова цели с учётом сайта: бургер на YouTube — «Гид», на
    остальных хостах — «Меню»/«навигация». Прочие слова — общий словарь."""
    if word in _BURGER_WORDS:
        h = (host or "").lower()
        if "youtube." in h or "youtu.be" in h:
            return _BURGER_SYN_YT
        return _BURGER_SYN_GENERIC
    return _GOAL_SYNONYMS.get(word, ())

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


# Слова цели, означающие ИКОНКУ (графический образ, а не подпись): «крестик»,
# «колокольчик», «лупа», «бургер»… Если скоринг (с синонимами выше) такую
# цель не нашёл — подписи у иконки нет, и текстовому LLM-резолву в списке
# подписей искать нечего: он лишь угадает чужой элемент (например
# «крестик» → «More», «колокольчик» → пункт меню «Notifications» на
# сайте без русских подписей у иконок). Такие цели минуют текстовый
# ярус и уходят в vision — ярус с рамками вокруг безымянных кнопок,
# который для иконок и существует
_ICON_WORD_ROOTS = ("крестик", "колокол", "лупа", "лупу", "лупы", "шестер",
                    "троеточ", "аватар", "бургер", "гамбургер", "полоск",
                    "иконк", "значок", "значк", "галочк")


def _icon_goal(goal: str) -> bool:
    # Цель названа иконкой: хотя бы одно слово — образ, а не подпись.
    words = re.findall(r"[a-z0-9а-яё]+", _norm_match(goal))
    return any(w.startswith(_ICON_WORD_ROOTS) for w in words)


def _goal_with_synonyms(goal: str, host: Optional[str] = None) -> str:
    """Цель + стем-синонимы её слов: целевой снапшот ищет по тексту страницы
    и словаря не знает — «состав» должно находить кнопку «Показать
    дополнительную информацию» (aria), а не только футер «Калорийность и
    состав». Хост нужен хост-зависимым синонимам (бургер: «Гид» на YouTube,
    «Меню» на остальных сайтах)."""
    words = re.findall(r"[a-z0-9а-яё]+", _norm_match(goal))
    extra: List[str] = []
    for w in words:
        for s in _goal_synonyms(w, host):
            if s not in words and s not in extra:
                extra.append(s)
    return " ".join([*words, *extra]) if extra else goal

# Глаголы-действия в начале цели («заменить барбекю»): в ярусе «слова
# разделены текстом и контекстом» элемент, у которого действие — в его
# собственном тексте (кнопка «Заменить»), сильнее элемента, у которого в
# тексте лишь объект (строка «Барбекю»)
_ACTION_WORD_ROOTS = ("замен", "выбра", "выбер", "поменя", "смени",
                      "переключ", "включ", "измен")


def parse_open_with_url(text: str, with_rest: bool = False
                        ) -> Optional[Tuple[str, List[str]]]:
    """Команда открытия с ЯВНЫМ адресом в фразе → (токен адреса, шаги пути).
    «открой на example.com/827 студентам - Технологии баз данных» →
    («example.com/827», [«студентам», «технологии баз данных»]). Шагов может
    не быть — тогда просто открыть страницу. None — не команда открытия или
    явного адреса нет (тогда шанс есть у parse_open_many).
    Хвост с глаголом-командой («…и нажми X», «, введи в поле Y …») в путь
    НЕ идёт; with_rest=True → (токен, шаги, [команды-хвост]) — их
    вызывающий исполняет отдельными шагами."""
    if not text or len(text) > 200:
        return None
    t = _strip_polite(text.strip().rstrip(".!?…").strip())
    if not _OPEN_VERB_RE.match(t):
        return None
    body = _OPEN_VERB_RE.sub("", t, count=1).strip()
    m = _URL_TOKEN_RE.search(body)
    if not m:
        return None
    # Предлог, висевший прямо перед адресом («иванова на example.com»),
    # срезаем точечно — общий хвостовой срез съедал бы контент («б в» → «б»)
    before = re.sub(r"(?:^|\s+)(?:на|в|во|on|in)$", "", body[:m.start()].strip())
    rest = (before + " " + body[m.end():]).strip()
    # «…и нажми на пепперони», «, введи в поле X и отправь» — отдельные
    # команды, а не пункты меню для клика: путь режем на первом глаголе
    rest, tail_cmds = _nav_split_commands(rest)
    prev = None
    while prev != rest:  # филлеры/предлоги в начале хвоста — до упора
        prev = rest
        rest = _NAV_FILLER_RE.sub("", rest, count=1).strip()
        rest = _NAV_PREP_RE.sub("", rest, count=1).strip()
    steps = []
    for s in _NAV_SPLIT_RE.split(rest):
        s = s.strip().strip(_TARGET_EDGE_CHARS)
        if s and len(s) <= 60:
            steps.append(s)
    if with_rest:
        return m.group(1), steps[:NAV_MAX_STEPS], tail_cmds
    return m.group(1), steps[:NAV_MAX_STEPS]


# Связки перед глаголом-командой в хвосте адреса: «и», «, потом», «then»…
_NAV_CMD_JOINERS = frozenset({"и", "а", "потом", "затем", "and", "then"})


def _nav_split_commands(rest: str) -> Tuple[str, List[str]]:
    """Хвост после адреса → (путь до первой команды, [команды]). Команда —
    кусок, начинающийся с глагола из словаря split_compound_command и
    стоящий в начале хвоста, после связки («и/потом/and/then») или после
    запятой/точки с запятой. Команды дальше режет split_compound_command
    («введи X и отправь» остаётся одной)."""
    words = rest.split()
    for i, raw in enumerate(words):
        w = raw.strip(_TARGET_EDGE_CHARS).lower().split("-")[0]
        if w not in _SC_RU_VERBS and w not in _SC_EN_VERBS:
            continue
        j = i
        while j > 0 and words[j - 1].strip(",;").lower() in _NAV_CMD_JOINERS:
            j -= 1
        after_sep = i == 0 or j < i or words[i - 1].endswith((",", ";"))
        if not after_sep:
            continue
        head = " ".join(words[:j]).strip(" ,;")
        tail = " ".join(words[i:]).strip(" ,;")
        return head, split_compound_command(tail)
    return rest, []


# Чтение со страницы: «прочитай последнее сообщение (на почте)»,
# «прочитай страницу», «что ответил бот»
_READ_REQUEST_RE = re.compile(
    r"^\s*(?:прочитай|прочти|зачитай|прочитать|прочесть|read)\s+"
    r"(.+?)\s*[.!?…]*\s*$", re.IGNORECASE)
_READ_LAST_RE = re.compile(
    r"последн\w*\s+(?:сообщени|ответ|реплик|мессаг)|ответ\b|reply|last\s+message",
    re.IGNORECASE)
# Чтение страницы — ВСЁ тело про страницу/её текст целиком: «прочитай
# текст песни Yesterday» — не про открытую вкладку (раньше «текст» где
# угодно в фразе давал чтение страницы)
_READ_PAGE_RE = re.compile(
    r"^(?:(?:эту|всю|текущую|открытую|открывшуюся|мне)\s+)*"
    r"(?:страниц\w*|страничк\w*|вкладк\w*)(?:\s+(?:целиком|полностью))?$|"
    r"^(?:(?:весь|этот|мне)\s+)*текст(?:\s+(?:страниц\w*|вкладк\w*|"
    r"на\s+(?:этой\s+)?странице|с\s+экрана))?(?:\s+(?:целиком|полностью))?$|"
    r"^(?:(?:всё|все)\s+)?содержим\w*(?:\s+(?:страниц\w*|вкладк\w*))?$|"
    r"^(?:(?:the|this|current|whole)\s+)*(?:page|tab)(?:\s+text)?$|"
    r"^(?:the\s+)?(?:page\s+)?text(?:\s+on\s+(?:the|this)\s+page)?$",
    re.IGNORECASE)
# Вопросительная форма: «что (мне) ответил/написал/прислал бот»
_READ_WHAT_RE = re.compile(
    r"^\s*что\s+(?:мне\s+)?(?:ответил|ответила|написал|написала|прислал|прислала)"
    r"\s+(\S+.*?)\s*[.?…]*\s*$", re.IGNORECASE)
# Кто «ответил» — бот/чат/модель в браузере; «что ответила мама» — вопрос
# о жизни, а не чтение вкладки «мама». Латиница/домен («chatgpt») — тоже
# про веб-чат
_READ_WHAT_SUBJ_RE = re.compile(
    r"^(?:бот\w*|чат\w*|ии|ai|нейросет\w*|нейронк\w*|модел\w*|ассистент\w*|"
    r"помощник\w*|собеседник\w*|сайт\w*|клод\w*|гпт|чатгпт|чат\s*gpt|"
    r"дипсик\w*|гигачат\w*|алис\w*|грок\w*|гемини|джемини|квен\w*|"
    r"перплексит\w*|копилот\w*|[a-z0-9][a-z0-9.\- ]*)$", re.IGNORECASE)


def parse_read_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«прочитай последнее сообщение на почте» → ("last", "почте");
    «прочитай страницу» → ("page", None); «что ответил бот» → ("last", "бот").
    None — не команда чтения."""
    if not text or len(text) > 120:
        return None
    text = _strip_polite(text)
    m = _READ_WHAT_RE.match(text)
    if m:
        subj = m.group(1).strip().lower()
        sm = _CLICK_SITE_RE.search(subj)
        if sm:
            # «что ответила мама в телеграме» — место названо явно
            return "last", sm.group(1).strip().lower()
        if not _READ_WHAT_SUBJ_RE.match(subj):
            return None
        return "last", (subj or None)
    m = _READ_REQUEST_RE.match(text)
    if not m:
        return None
    body = m.group(1).strip()
    site = None
    # «прочитай текст на этой странице» — текущая вкладка, не сайт «странице»
    body, _is_page = _strip_page_ref(body)
    sm = None if _is_page else _CLICK_SITE_RE.search(body)
    if sm:
        site = sm.group(1).strip().lower()
        body = body[:sm.start()].strip()
    if _READ_PAGE_RE.match(body.strip(_TARGET_EDGE_CHARS)):
        return "page", site
    if _READ_LAST_RE.search(body) or body in ("", "это", "её", "его"):
        return "last", site
    return None


# ── Состояние открытой страницы: «что на странице?», «пришли скриншот» ──
# Read-only отчёт (список элементов + скриншот вьюпорта), без подтверждения,
# как чтение. Проверяется в fast-path до LLM-яруса интентов. Конфликтов с
# соседними парсерами нет: _PAGE_QUESTION_RE требует «в/во» + имя секции
# («на странице» не матчится), _TAB_SWITCH_RE — «вкладку» с целью,
# parse_read_request — глагол «прочти»/«что ответил».
_PAGE_VIEW_WHAT_RE = re.compile(
    r"^\s*(?:а\s+)?что\s+(?:(?:там|сейчас|тут|у\s+тебя)\s+){0,2}"
    r"(?:есть|находится|лежит|происходит|отображается|показывается|"
    r"имеется|осталось|интересного)?\s*на\s+"
    r"(?:(?:этой|этом|открытой|открытом|текущей|текущем)\s+)?"
    r"(?:страниц\w*|сайт\w*|экран\w*|вкладк\w*|окн\w*)"
    r"(?:\s+(?:на|в|во)\s+(\S+))?"
    r"(?:\s+(?:сейчас|там|тут))?\s*[?？!…]*\s*$", re.IGNORECASE)
_PAGE_VIEW_SEE_RE = re.compile(
    r"^\s*(?:а\s+)?что\s+(?:ты\s+)?(?:сейчас\s+)?видишь"
    r"(?:\s+на\s+(?:страниц\w*|экран\w*|сайт\w*|вкладк\w*|окн\w*))?"
    r"\s*[?？!…]*\s*$", re.IGNORECASE)
_PAGE_VIEW_WHAT_EN_RE = re.compile(
    r"^\s*what(?:'s|\s+is|\s+are)(?:\s+there)?\s+on\s+(?:the\s+|this\s+)?"
    r"(?:page|screen|site|tab)\s*[?？!…]*\s*$|"
    r"^\s*what\s+do\s+you\s+see(?:\s+on\s+(?:the\s+)?(?:page|screen))?"
    r"\s*[?？!…]*\s*$", re.IGNORECASE)
# «покажи страницу/экран»: «вкладку» сюда не берём — «покажи вкладку X» это
# переключение вкладки (_TAB_SWITCH_RE), оно проверяется раньше в fast-path
_PAGE_VIEW_SHOW_RE = re.compile(
    r"^\s*(?:покажи|показать|show)\s+(?:мне\s+)?(?:что\s+(?:есть\s+)?на\s+)?"
    r"(?:страниц\w*|экран|сайт|окн\w*|(?:the\s+)?(?:page|screen|site))"
    r"(?:\s+(?:на|в|во|on|in)\s+(\S+))?\s*[.!?…]*\s*$", re.IGNORECASE)
_PAGE_VIEW_SHOT_RE = re.compile(
    r"^\s*(?:(?:сделай|сними|пришли|скинь|покажи|дай|take|send|show|make)"
    r"\s+(?:мне\s+)?(?:a\s+)?)?"
    r"(?:скриншот\w*|screenshot)\s*"
    r"(?:страниц\w*|экран\w*|сайт\w*|окн\w*|of\s+the\s+page)?\s*"
    r"(?:\s+(?:на|в|во|on|in)\s+(\S+))?\s*[.!?…]*\s*$", re.IGNORECASE)
# «покажи всю страницу» / «покажи страницу целиком» / «сделай полный
# скриншот страницы» — ПОЛНОСТРАНИЧНЫЙ захват (скролл-стичинг, альбом
# кусков + текстовое оглавление). Маркер полноты («всю»/«целиком»/
# «полный») обязателен — без него срабатывают обычные _PAGE_VIEW_*
_PAGE_VIEW_FULL_RE = re.compile(
    r"^\s*(?:покажи|пришли|скинь|сфотай|сфотографируй|сними|сделай|дай|"
    r"show|send)\s+(?:мне\s+)?"
    r"(?:(?:всю|весь|всё|полную|целую)\s+"
    r"(?:страниц\w*|страничк\w*|сайт\w*|лент\w*)(?:\s+(?:целиком|полностью))?|"
    r"(?:страниц\w*|страничк\w*|сайт\w*|лент\w*)\s+(?:целиком|полностью)|"
    r"полн\w*\s+скриншот\w*(?:\s+страниц\w*)?|"
    r"скриншот\w*\s+(?:всей|полной)\s+страниц\w*)"
    r"(?:\s+(?:на|в|во)\s+(\S+))?\s*[.!?…]*\s*$", re.IGNORECASE)

# «увеличь/уменьши масштаб (страницы)» / «сбрось масштаб» / «масштаб 100%» —
# зум вкладки (Chrome per-host zoom). Второе слово обязано быть
# масштаб/зум/страница — иначе бы «прибавь громкость» уходило в зум
_ZOOM_RESET_RE = re.compile(
    r"^\s*(?:(?:сбрось|верни|убери|отмени)(?:те)?\s+"
    r"(?:масштаб\w*|зум\w*|увеличени\w+)|"
    r"(?:масштаб|зум)\s*(?:на\s+)?(?:100|сто)\s*(?:%|процент\w*)?|"
    r"(?:обычн\w*|нормальн\w*|стандартн\w*|фактическ\w*)\s+"
    r"(?:масштаб|размер)|"
    r"(?:масштаб|зум)\s+(?:обычный|нормальный|стандартный))"
    r"\s*[.!?…]*\s*$", re.IGNORECASE)
_ZOOM_STEP_RE = re.compile(
    r"^\s*(увелич\w+|уменьш\w+|прибав\w+|убав\w+|приблиз\w*|отодвин\w*)\s+"
    r"(?:масштаб\w*|зум\w*|страниц\w*)"
    r"(?:\s+(?:на|в|во)\s+(\S+))?\s*[.!?…]*\s*$", re.IGNORECASE)


def parse_zoom_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«уменьши масштаб» → ("out", None); «увеличь масштаб на почте» →
    ("in", "почте"); «сбрось масштаб» → ("reset", None). None — не про зум."""
    if not text or len(text) > 60:
        return None
    if _ZOOM_RESET_RE.match(text):
        return "reset", None
    m = _ZOOM_STEP_RE.match(text)
    if not m:
        return None
    direction = ("in" if re.match(r"увелич|прибав|приблиз", m.group(1).lower())
                 else "out")
    site = (m.group(2) or "").strip().strip('"«»').lower() or None
    if site in _NOOP_SITE_WORDS:
        site = None
    return direction, site


def parse_page_view_request(text: str) -> Optional[Tuple[Optional[str], bool, bool]]:
    """«что на странице?» → (None, False, False); «пришли скриншот» →
    (None, True, False — скриншот запрошен явно); «покажи всю страницу» /
    «страницу целиком» → (None, True, True — полностраничный захват);
    «что на странице на почте» → ("почте", False, False).
    None — не команда состояния страницы."""
    if not text or len(text) > 100:
        return None
    # Полностраничный захват — раньше остальных: маркер полноты там
    # обязателен, а «покажи всю страницу» иначе съел бы _PAGE_VIEW_SHOW_RE
    m = _PAGE_VIEW_FULL_RE.match(text)
    if m:
        site = (m.group(1) or "").strip().strip('"«»').lower() or None
        if site in _NOOP_SITE_WORDS:
            site = None
        return site, True, True
    site = None
    m = _PAGE_VIEW_WHAT_RE.match(text)
    if m:
        site = m.group(1)
    elif _PAGE_VIEW_SEE_RE.match(text) or _PAGE_VIEW_WHAT_EN_RE.match(text):
        pass
    else:
        m = _PAGE_VIEW_SHOW_RE.match(text)
        if m:
            site = m.group(1)
        else:
            m = _PAGE_VIEW_SHOT_RE.match(text)
            if not m:
                return None
            site = m.group(1)
            site = (site or "").strip().strip('"«»').lower() or None
            if site in _NOOP_SITE_WORDS:
                site = None
            return site, True, False
    site = (site or "").strip().strip('"«»').lower() or None
    if site in _NOOP_SITE_WORDS:
        site = None
    return site, False, False


# «Что на странице»: пунктов в группе и длина подписи. Группа — заголовок
# и пункты построчно: одной строкой через «;» сорок подписей читались сплошной
# кашей, особенно в Telegram
PAGE_VIEW_GROUP_MAX = 8
PAGE_VIEW_LABEL_MAX = 60


def page_view_text(url: str, host: str, items: List[dict],
                   lang: Optional[str] = None) -> str:
    """Текстовая часть отчёта «что на странице»: сгруппированный список
    элементов снапшота — открытое окно (модалка — то, с чем сейчас
    работают), поля ввода, кнопки, ссылки, прочее; заголовок группы жирным,
    элементы построчно. Внутри группы первыми — элементы вьюпорта: их же
    видно на скриншоте, который уходит вместе с текстом. lang — язык
    подписей (cc_texts; None — русский)."""
    from app.features import cc_texts
    from app.features.cc_privacy import scrub_url
    head = f"**{cc_texts.t('pv_page', lang)}** {host or '—'}"
    if url:
        # Отчёт уходит облачной модели и в историю — URL без токенов/кодов
        u = scrub_url(str(url))
        head += f"\n{u[:100] + '…' if len(u) > 100 else u}"
    if not items:
        return head + "\n\n" + cc_texts.t("pv_no_items", lang)

    def _label(it: dict) -> str:
        t = " ".join(str(it.get("text") or it.get("aria")
                     or it.get("title") or "").split())
        if not t:
            t = scrub_url(str(it.get("href") or ""))
        return (t[:PAGE_VIEW_LABEL_MAX - 1] + "…"
                if len(t) > PAGE_VIEW_LABEL_MAX else t)

    def _is_btn(it: dict) -> bool:
        return (it.get("tag") == "button" or it.get("role") == "button"
                or (it.get("tag") == "input" and not it.get("ed")))

    blocks = []
    rest = list(items)
    for name, pred in ((cc_texts.t("pv_group_modal", lang),
                        lambda it: it.get("md")),
                       (cc_texts.t("pv_group_fields", lang),
                        lambda it: it.get("ed")),
                       (cc_texts.t("pv_group_buttons", lang), _is_btn),
                       (cc_texts.t("pv_group_links", lang),
                        lambda it: it.get("tag") == "a"),
                       (cc_texts.t("pv_group_other", lang), lambda it: True)):
        picked = [it for it in rest if pred(it)]
        rest = [it for it in rest if not pred(it)]
        # Вьюпорт первым, дедуп по подписи, лимит группы
        picked.sort(key=lambda it: not it.get("vp"))
        labels, seen = [], set()
        for it in picked:
            t = _label(it)
            if not t or t.lower() in seen:
                continue
            seen.add(t.lower())
            labels.append(t)
        if not labels:
            continue
        shown = labels[:PAGE_VIEW_GROUP_MAX]
        lines = [f"**{name}** ({len(labels)}):"] + [f"• {t}" for t in shown]
        if len(labels) > len(shown):
            lines.append(cc_texts.t("pv_more", lang,
                                    n=len(labels) - len(shown)))
        blocks.append("\n".join(lines))
    if not blocks:
        return head + "\n\n" + cc_texts.t("pv_no_items", lang)
    return "\n\n".join([head] + blocks)


def page_view_full_text(url: str, host: str, outline: List[dict],
                        truncated: bool = False,
                        lang: Optional[str] = None) -> str:
    """Текстовое оглавление к полностраничному альбому («покажи всю
    страницу»): разделы и их позиции сверху вниз. outline собирается по
    всему DOM при захвате (уже с лимитами 12 разделов × 8 позиций).
    Сами кадры уходят альбомом — тут только навигация по ним."""
    from app.features import cc_texts
    from app.features.cc_privacy import scrub_url
    head = f"{cc_texts.t('pv_page', lang)} {host or '—'}"
    if url:
        u = scrub_url(str(url))
        head += f"\n{u[:100] + '…' if len(u) > 100 else u}"
    lines = [head]
    secs = [s for s in (outline or []) if isinstance(s, dict)
            and (s.get("head") or s.get("items"))]
    if secs:
        lines.append(cc_texts.t("pv_top_down", lang))
        for s in secs[:12]:
            label = " ".join(str(s.get("head") or "").split())[:60]
            items = [" ".join(str(t).split())[:50]
                     for t in (s.get("items") or [])][:8]
            row = "; ".join(t for t in items if t)
            if label and row:
                lines.append(f"{label}: {row}")
            elif label or row:
                lines.append(label or row)
    else:
        lines.append(cc_texts.t("pv_no_outline", lang))
    if truncated:
        lines.append(cc_texts.t("pv_truncated", lang))
    return "\n".join(lines)


# Филлер в начале запроса («открой ВИДЕО ‹название› на ютуб»): в поиск
# не уходит и не ест лимит длины запроса
_SEARCH_QUERY_FILLER_RE = re.compile(
    r"^(?:видео|ролик|фильм|сериал|трек|песню|песня|музыку|клип|"
    r"video|movie|song|track|clip)(?:\s+|$)", re.IGNORECASE)


def parse_search_on_site(text: str) -> Optional[Tuple[str, str, bool]]:
    """«включи фильм на стриминге» → («фильм», «стриминге», True).
    Третий элемент — «открыть непосредственно»: False у глаголов-поисковиков
    (найди/поищи/find/search — открываем страницу поиска, а не первый результат).
    None — не поисковая команда на сайте."""
    if not text or len(text) > 120:
        return None
    m = _SEARCH_ON_SITE_RE.match(_strip_polite(text))
    if not m:
        return None
    verb = m.group(1).lower()
    query = m.group(2).strip(_TARGET_EDGE_CHARS)
    site_word = m.group(3).strip(_TARGET_EDGE_CHARS).lower()
    query = _SEARCH_QUERY_FILLER_RE.sub("", query).strip(_TARGET_EDGE_CHARS)
    # «поставь громкость на 50» — число после «на» это значение (ползунок),
    # а не сайт
    if not query or len(query) > 80 or not site_word \
            or site_word[0].isdigit():
        return None
    # «открой комментарии/настройки на ютубе», «включи звук на ютубе» —
    # элемент страницы (клик/медиа-клавиша), а не поиск ролика
    if _UI_ELEMENT_RE.match(query):
        return None
    return query, site_word, verb not in _SEARCH_PAGE_VERBS


# Standalone «отправь»/«send» — Enter в поле ввода (без ввода текста)
_SEND_REQUEST_RE = re.compile(
    r"^\s*(?:отправь|отправить|отправляй|пошли|шли|send|submit)"
    r"(?:\s+(?:сообщение|мессагу|мессадж|ответ|это|его|её|message|it))?"
    r"(?:\s+(?:на|в|во|on|in)\s+(\S+))?\s*[.!?…]*\s*$", re.IGNORECASE)


def parse_send_request(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """«отправь (сообщение)» → ("send", None); «отправь на почте» →
    ("send", "почте"). None — не команда отправки."""
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


# «удали 5 символов» / «сотри последние три буквы» — стирание набранного
# текста серией Backspace. Число цифрой или словом; без числа — один символ.
# «удали сообщение/вкладку/чикен» сюда НЕ попадают (нет «символ/буква/
# знак») — их разбирают close/tab/cart дальше по цепочке
_ERASE_NUM_WORDS = {
    "один": 1, "одну": 1, "пару": 2, "два": 2, "две": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
    "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
    "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20,
}
_ERASE_REQUEST_RE = re.compile(
    r"^\s*(?:удали|удалить|сотри|стереть)\s+"
    r"(?:последни[ех]\s+)?"
    r"(?:(\d+|" + "|".join(sorted(_ERASE_NUM_WORDS, key=len, reverse=True))
    + r")\s+)?"
    r"(?:символ\w*|букв\w*|знак\w*|штук\w*)"
    r"(?:\s+(?:на|в|во|on|in)\s+(\S+))?"
    r"\s*[.!?…]*\s*$",
    re.IGNORECASE)
# Потолок за одну команду: «удали 5000 символов» — опечатка, а не намерение
from app.features.browser_actions import PRESS_TIMES_MAX as _ERASE_MAX  # noqa: E402


def parse_erase_request(text: str):
    """«удали 5 символов» → (("Backspace", 5, "erase"), None); «сотри
    последние три буквы на ютубе» → (("Backspace", 3, "erase"), "ютубе").
    Кортеж — в формате медиа-goal для resolve_key (клавиша, нажатий, вид).
    None — не команда стирания."""
    if not text or len(text) > 60:
        return None
    t = text.strip()
    is_page = bool(_PAGE_REF_RE.search(t))
    if is_page:
        t = _PAGE_REF_RE.sub("", t)
    m = _ERASE_REQUEST_RE.match(t)
    if not m:
        return None
    raw = (m.group(1) or "").strip().lower()
    if not raw:
        n = 1
    elif raw.isdigit():
        n = int(raw)
    else:
        n = _ERASE_NUM_WORDS.get(raw, 1)
    n = max(1, min(n, _ERASE_MAX))
    site = ((m.group(2) or "").strip().rstrip(",").strip().lower()) or None
    if is_page:
        site = PAGE_REF
    return ("Backspace", n, "erase"), site


def _chars_ru(n: int) -> str:
    # Плюрализация «символ»: 1 символ, 3 символа, 5 символов.
    if 10 <= n % 100 <= 20:
        return "символов"
    return {1: "символ", 2: "символа", 3: "символа", 4: "символа"}.get(
        n % 10, "символов")


# Медиа-команды плеера — клавишами YouTube (работают и на music.youtube.com):
# пауза/плей — пробел, громкость — стрелки (±10% за нажатие), звук — m.
# Голые слова без «нажми»: «пауза», «тише», «громче» — в режиме управления
_MEDIA_REQUESTS = [
    (re.compile(
        r"^\s*(?:(?:нажми|нажать|press)\s+)?"
        r"(?:пауза|паузу|поставь\s+на\s+паузу|поставить\s+на\s+паузу|"
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
    # Звук направленно: «выключи звук» ≠ «включи звук». Клавиша одна («m» —
    # переключатель), но вид разный — ответ обещает то, о чём просили, а на
    # shorts (media_vol у <video>) op тоже направленный
    (re.compile(
        r"^\s*(?:(?:выключи|отключи|убери|заглуши)\s+звук|без\s+звука|"
        r"мьют|mute)\s*[.!…]*\s*$", re.IGNORECASE), ("m", 1, "mute")),
    (re.compile(
        r"^\s*(?:(?:включи|верни)\s+звук|со\s+звуком|unmute)"
        r"\s*[.!…]*\s*$", re.IGNORECASE), ("m", 1, "unmute")),
    # Англ. формы: «pause (the video)», «volume up», «turn the sound off»
    (re.compile(
        r"^\s*(?:(?:press|hit|click)\s+)?(?:pause|resume|unpause|play)"
        r"(?:\s+(?:the\s+)?(?:video|music|song|track|playback|it))?"
        r"\s*[.!…]*\s*$", re.IGNORECASE), ("Space", 1, "toggle")),
    (re.compile(
        r"^\s*(?:volume\s+down|quieter|(?:turn|bring)\s+(?:it|the\s+volume|"
        r"the\s+sound)\s+down|turn\s+down\s+(?:the\s+)?(?:volume|sound)|"
        r"(?:lower|decrease|reduce)\s+(?:the\s+)?(?:volume|sound))"
        r"\s*[.!…]*\s*$", re.IGNORECASE), ("ArrowDown", 2, "vol_down")),
    (re.compile(
        r"^\s*(?:volume\s+up|louder|(?:turn|bring)\s+(?:it|the\s+volume|"
        r"the\s+sound)\s+up|turn\s+up\s+(?:the\s+)?(?:volume|sound)|"
        r"(?:raise|increase)\s+(?:the\s+)?(?:volume|sound))"
        r"\s*[.!…]*\s*$", re.IGNORECASE), ("ArrowUp", 2, "vol_up")),
    (re.compile(
        r"^\s*(?:mute\s+(?:the\s+)?(?:video|sound|audio|it)|"
        r"turn\s+(?:the\s+)?sound\s+off|turn\s+off\s+(?:the\s+)?sound)"
        r"\s*[.!…]*\s*$", re.IGNORECASE), ("m", 1, "mute")),
    (re.compile(
        r"^\s*(?:unmute\s+(?:the\s+)?(?:video|sound|audio|it)|"
        r"turn\s+(?:the\s+)?sound\s+(?:back\s+)?on|"
        r"turn\s+on\s+(?:the\s+)?sound)"
        r"\s*[.!…]*\s*$", re.IGNORECASE), ("m", 1, "unmute")),
]
# Хвост-место у медиа-команды: «включи звук на ютубе» — та же клавиша
_MEDIA_SITE_TAIL_RE = re.compile(
    r"\s+(?:на|в|во|on|in)\s+(\S+?)\s*[.!…]*\s*$", re.IGNORECASE)


def parse_media_request(text: str, with_site: bool = False):
    """«пауза» / «поставь на паузу» → ("Space", 1, "toggle"); «тише» →
    ("ArrowDown", 2, "vol_down"); «громче» → ("ArrowUp", 2, "vol_up");
    «без звука» → ("m", 1, "mute"); «включи звук» → ("m", 1, "unmute").
    None — не медиа-команда. Кортеж:
    (клавиша playwright, нажатий, вид — для текста ответа).
    Хвост «на <сайте>» допускается («включи звук на ютубе»); with_site=True
    → (кортеж, сайт|None) — сайт для resolve_key."""
    if not text or len(text) > 60:
        return None
    t = _strip_polite(text.strip())
    site = None
    for cand in (t, None):
        if cand is None:
            sm = _MEDIA_SITE_TAIL_RE.search(t)
            if not sm or sm.group(1).lower() in ("паузу", "паузе", "pause"):
                break
            site = sm.group(1).strip(_TARGET_EDGE_CHARS).lower() or None
            cand = t[:sm.start()]
        for rx, val in _MEDIA_REQUESTS:
            if rx.match(cand):
                return (val, site) if with_site else val
    return None


# Авто-листание «промотай страницу»: бот крутит вкладку короткими шагами,
# пока не скажут «стоп» (или не кончится страница). Оба слова — целые
# короткие сообщения: фраза вроде «ну ладно, листай дальше сам» не команда.
# «промотай раздел слева» — листается внутренняя прокручиваемая панель
# (левая/правая половина вьюпорта), а не окно: у карточек-модалок на
# сайтах с товарами своя колонка скролла. «промотай вверх» — направление
# вверх (по умолч. вниз). Слова направления и стороны листания — ОДНО
# определение: из них собираются и regex (префикс/исключения/хвост), и
# срезка направления с имени контейнера — иначе они расходятся, и
# «пролистай комментарии вниз» даёт контейнер «комментарии вниз» вместо
# направления «вниз»
_SCROLL_UP_WORDS = ("вверх", "выше", "наверх", "up")
_SCROLL_DOWN_WORDS = ("вниз", "ниже", "down")
_SCROLL_SIDE_WORDS = {"слева": "left", "справа": "right",
                      "left": "left", "right": "right"}
_SCROLL_DIR_ALT = "|".join(_SCROLL_UP_WORDS + _SCROLL_DOWN_WORDS)
# Хвостовые слова, которые именем контейнера быть не могут (их разбирают
# соседние группы): направление, сторона, существительные стороны, предлоги
_SCROLL_NOT_CONTAINER_ALT = "|".join(
    [rf"{w}\b" for w in _SCROLL_UP_WORDS + _SCROLL_DOWN_WORDS
     + tuple(_SCROLL_SIDE_WORDS)]
    + [r"прав\w*\b", r"лев\w*\b", r"на\b", r"в\b", r"во\b", r"on\b",
       r"in\b",
       r"раздел\w*\b", r"список\w*\b", r"панел\w*\b", r"блок\w*\b",
       r"част\w*\b", r"колонк\w*\b", r"сторон\w*\b", r"половин\w*\b",
       r"меню\b"])
_SCROLL_START_RE = re.compile(
    r"^\s*(?:промотай|промотать|пролистай|пролистать|полистай|полистать|"
    r"прокрути|прокрутить|проскролль|проскроллить|скролль|скроллить|"
    r"покрути|покрутить|листай|листать|scroll)\b"
    r"(?:\s+(?:эту\s+)?(?:страниц\w+|страничк\w+|лент\w+|фид|лист|ее|её|"
    r"дальше|page|feed|it|" + "|".join(_SCROLL_DOWN_WORDS) + r"))*"
    # Именованный контейнер: «пролистай комментарии», «промотай чат» —
    # листается названный блок (панель комментариев), а не страница. Слова,
    # занятые соседними группами (сторона/направление/предлог сайта/существи-
    # тельные стороны), исключены заглядением — иначе «раздел слева»
    # разваливался бы на контейнер «раздел»
    r"(?:\s+(?!" + _SCROLL_NOT_CONTAINER_ALT + r")"
    r"([a-zа-яё][\wа-яё-]*(?:\s+[a-zа-яё][\wа-яё-]*)?))?"
    # Сторона: «раздел слева» и «левый раздел» — один смысл; в обоих
    # порядках («пролистай правую часть» = «пролистай часть справа»)
    r"(?:\s+(?:(прав\w+|лев\w+)\s+(?:раздел\w*|список\w*|панел\w*|блок\w*|"
    r"част\w*|колонк\w*|сторон\w*|половин\w*|меню)|(?:(?:раздел\w*|список\w*|"
    r"панел\w*|блок\w*|част\w*|колонк\w*|меню)\s+)?("
    + "|".join(_SCROLL_SIDE_WORDS) + r")))?"
    r"(?:\s+(" + _SCROLL_DIR_ALT + r"))?"
    r"(?:\s+(?:на|в|во|on|in)\s+(\S+))?\s*[.!?…]*\s*$",
    re.IGNORECASE)
# Мера и частицы в команде листания: «немного/чуть/слегка/ещё», «a bit»,
# артикль «the» — срезаются до матча (иначе становились контейнером)
_SCROLL_SOFT_RE = re.compile(
    r"(?<![\wё])(?:немного|немножко|немножечко|чуть(?:-чуть)?|чуточку|"
    r"слегка|ещё|еще|a\s+bit|a\s+little|a\s+little\s+bit|bit|the|way)"
    r"(?![\wё])",
    re.IGNORECASE)
# «стоп» — бытовое слово: резолвер пропускает его дальше в диалог, когда
# листание не активно (resolver решает по состоянию менеджера)
_SCROLL_STOP_RE = re.compile(
    r"^\s*(?:стоп|стой|погоди|остановись|останови|остановить|хватит|"
    r"прекрати|прекращай|заканчивай|закончи|закончить|достаточно|stop|"
    r"enough|halt)"
    r"(?:\s+(?:листать|прокрутку|прокручивать|скроллить|мотать|листание|"
    r"прокрутка|скролл|читать|это|уже|"
    # «stop scrolling» / «stop the scroll» / «enough scrolling»; до двух
    # слов хвоста — «stop scrolling now», «хватит листать уже»
    r"(?:the\s+)?scroll(?:ing)?|reading|it|now|already)){0,2}"
    r"\s*[.!?…]*\s*$",
    re.IGNORECASE)
# Русское имя прокручиваемого контейнера → англ. корень для DOM-матча
# (id/aria-label/class у зарубежных сайтов английские: ytd-comments#comments)
_SCROLL_CONTAINER_ALIAS = {
    "комментарии": "comment", "комментарий": "comment",
    "комментария": "comment", "комментариев": "comment",
    "комменты": "comment", "комментов": "comment", "коммента": "comment",
    "чат": "chat", "чата": "chat",
    "ответы": "replie", "ответов": "replie",
    "описание": "description", "рекомендации": "related",
}

# Как часто дозорный поток опрашивает состояние листания в странице
# (анимация крутится сама; поток лишь ловит конец ленты/смерть вкладки)
_SCROLL_POLL_SEC = 0.9
# Абсолютный потолок сеанса листания у дозорного: страница может не ответить
# ни «done», ни «не active» (вкладка жива, но анимация застряла) — тогда
# сеанс жил бы вечно и на любую просьбу листать шло «я уже листаю»
_SCROLL_MAX_SEC = 660.0
# Сколько секунд после САМОСТОЯТЕЛЬНОГО конца листания «стоп» ещё считается
# командой (и получает честный отчёт, чем всё кончилось). Дальше это снова
# бытовое слово и уходит в обычный диалог
_SCROLL_END_GRACE_SEC = 15.0


def parse_scroll_request(text: str) -> Optional[Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str]]]:
    """«промотай страницу (на ютубе)» → ("start", сайт|None, None, None, None);
    «промотай раздел слева» / «пролистай левый раздел» → ("start", None,
    "left", None, None); «промотай вверх» → ("start", None, None, "up", None);
    «пролистай комментарии» / «пролистай комментарии вниз» → ("start",
    None, None, None, "комментарии") — именованный контейнер;
    «стоп»/«хватит листать» → ("stop", None, None, None, None).
    None — не команда листания."""
    if not text or len(text) > 60:
        return None
    t = text.strip()
    # «scroll to comments» — доскролл до цели (parse_scroll_to_goal), а не
    # контейнер «to comments»
    if _SCROLL_TO_GOAL_EN_RE.match(t):
        return None
    # «прокрути немного вниз», «чуть ниже», «…, пожалуйста» — наречия меры и
    # вежливость не имя контейнера
    t = " ".join(_SCROLL_SOFT_RE.sub(" ", _strip_polite(t)).split())
    m = _SCROLL_START_RE.match(t)
    if m:
        container = (m.group(1) or "").strip().lower() or None
        adj = (m.group(2) or "").strip().lower()
        bare = (m.group(3) or "").strip().lower()
        dir_word = (m.group(4) or "").strip().lower()
        direction = "up" if dir_word in _SCROLL_UP_WORDS else None
        # Имя контейнера жадно забирает хвостовые служебные слова
        # («пролистай комментарии вниз» → контейнер «комментарии вниз»):
        # возвращаем их своим группам, пока хвост служебный. Слова берём из
        # общих наборов — ровно те же, что в regex
        while container:
            parts = container.rsplit(" ", 1)
            last = parts[-1]
            if last in _SCROLL_UP_WORDS:
                direction = direction or "up"
            elif last in _SCROLL_DOWN_WORDS:
                pass  # вниз — направление по умолчанию, отдельной группы нет
            elif last in _SCROLL_SIDE_WORDS and not adj and not bare:
                bare = last
            else:
                break
            container = parts[0].strip() if len(parts) > 1 else None
        side = None
        if adj:  # «правую часть» / «левый раздел» — прилагательное первым
            side = "left" if adj.startswith("лев") else "right"
        elif bare:
            side = _SCROLL_SIDE_WORDS.get(bare)
        return ("start", (m.group(5) or "").strip().lower() or None,
                side, direction, container)
    if _SCROLL_STOP_RE.match(t):
        return "stop", None, None, None, None
    return None


# ── Ограниченный доскролл ДО ЦЕЛИ: «пролистай до напитков» ──
# Отличие от автолистания (_SCROLL_START_RE): есть цель или граница —
# крутим сами до находки и останавливаемся, «стоп» не нужен — при удалённом
# управлении человек не видит страницу и не может сказать «стоп» вовремя.
# Проверяется в fast-path ДО parse_scroll_request:
# «пролистай до напитков» тот съел бы как контейнер «до напитков» и
# включил бы бесконечное листание
_SCROLL_TO_GOAL_RE = re.compile(
    r"^\s*(?:пролистай|пролистать|промотай|промотать|полистай|полистать|"
    r"прокрути|прокрутить|проскролль|проскроллить|докрути|докрутить|"
    r"долистай|долистать|доскролль|доскроллить|скролль)\s+"
    r"(?:страниц\w*\s+|лент\w*\s+|сайт\w*\s+)?до\s+(.+?)\s*[.!?…]*\s*$",
    re.IGNORECASE)
# «найди пепперони на странице/здесь» — поиск ВНУТРИ открытой страницы.
# Голое «найди X (на сайте магазина)» сюда не входит — остаётся поиском
# на сайте (_SEARCH_ON_SITE_RE)
_SCROLL_FIND_RE = re.compile(
    r"^\s*(?:найди|найти|поищи|поискать|отыщи)\s+(.+?)\s+"
    r"(?:на\s+(?:этой\s+)?страниц\w*|на\s+(?:этом\s+)?сайте|здесь|тут)"
    r"\s*[.!?…]*\s*$", re.IGNORECASE)
# Англ.: «scroll (down) to (the) comments», «scroll until X», «find X on
# this page»
_SCROLL_TO_GOAL_EN_RE = re.compile(
    r"^\s*scroll\s+(?:(?:down|up)\s+)?(?:(?:the\s+)?page\s+)?"
    r"(?:(?:down|up)\s+)?(?:to|until|till)\s+(?:the\s+)?(.+?)\s*[.!?…]*\s*$",
    re.IGNORECASE)
_SCROLL_FIND_EN_RE = re.compile(
    r"^\s*find\s+(.+?)\s+(?:on\s+(?:this|the)\s+(?:page|site)|here)"
    r"\s*[.!?…]*\s*$", re.IGNORECASE)
# Край страницы как цель: «докрути до конца/низа» / «до начала/верха»
_SCROLL_EDGE_WORDS = {
    "bottom": ("конца", "конец", "низа", "низ", "дна", "дно", "подвала",
               "футера", "footer", "bottom", "end"),
    "top": ("начала", "начало", "верха", "верх", "шапки", "header", "top",
            "beginning"),
}


def parse_scroll_to_goal(text: str) -> Optional[str]:
    """«пролистай до напитков» → «напитки»; «докрути до конца» → «конца»;
    «найди пепперони на странице» → «пепперони». None — не доскролл до
    цели (голое «пролистай» — автолистание, голое «найди X» — поисковая
    страница сайта)."""
    if not text or len(text) > 90:
        return None
    text = _strip_polite(text)
    m = (_SCROLL_TO_GOAL_RE.match(text) or _SCROLL_FIND_RE.match(text)
         or _SCROLL_TO_GOAL_EN_RE.match(text) or _SCROLL_FIND_EN_RE.match(text))
    if not m:
        return None
    goal = " ".join(m.group(1).strip(_TARGET_EDGE_CHARS).split())
    if not 2 <= len(goal) <= 60:
        return None
    return goal


# «ещё» / «покажи остальное» — досылка остатка полностраничного альбома
# (бот держит остаток кадров в _pending_more_photos; модульный уровень
# тут, рядом с парсерами, — тестируется без telegram-зависимостей)
_MORE_PHOTOS_RE = re.compile(
    r"^\s*(?:ещё|еще|дальше|продолжай|продолжить|давай\s+дальше|"
    r"(?:покажи|пришли|скинь|дай)\s+(?:остальн\w+|дальше))"
    r"\s*[.!?…]*\s*$", re.IGNORECASE)


# ── Корзина сайта: убрать/убавить/прибавить/изменить товар ──
# Филлер-слова в названии товара («гавайскую пиццу» → «гавайскую»): в тексте
# карточки корзины их нет, и all-words матчинг промахивался бы
_CART_FILLER_RE = re.compile(
    r"\b(?:пицц\w*|товар\w*|продукт\w*|штук\w*|шт\.?|порци\w*|позици\w*)\b",
    re.IGNORECASE)
_CART_REMOVE_RE = re.compile(
    r"^\s*(?:убери|удали|выкинь|выбрось|убрать|удалить)\s+(.+?)\s+из\s+"
    r"(?:корзины|заказа)\s*[.!?…]*\s*$", re.IGNORECASE)
# Голая форма удаления БЕЗ «из корзины»: «удали чикен», «нажми удалить
# чикен». Парсится как обычный клик, а при промахе текстового резолва
# уходит корзинному фолбэку resolve_click (_cart_op_fallback) — он
# срабатывает, только если товар реально виден в открытой корзине
_CART_REMOVE_GOAL_RE = re.compile(
    r"^(?:удали\w*|убери|убрать|выкинь\w*|выбрось\w*)\s+(.+)$",
    re.IGNORECASE)
# Редактирование товара в корзине (ссылка «Изменить» в карточке):
# «изменить состав в гавайская» (парсер клика отдаёт «в X» сайтом, и
# resolve_click собирает обратно в «... на X»), «поменять гавайскую»
_CART_EDIT_GOAL_RE = re.compile(
    r"^(?:измен\w*|поменя\w*)\s+(.+)$", re.IGNORECASE)
# Количество товара через клик по кнопке ряда: «нажми + в двойная
# пепперони», «нажми минус на кола» — символ/слово в начале цели клика.
# Без этого шла общая резолвация и цепляла упоминание товара в описании
# состава комбо (клик по «+» мог открыть строку описания вида «3 пиццы
# 30 или 35 см» вместо кнопки количества)
_CART_INC_GOAL_RE = re.compile(
    r"^(?:[+＋]\s*|плюс\s+)(.+)$", re.IGNORECASE)
_CART_DEC_GOAL_RE = re.compile(
    r"^(?:[-−–]\s*|минус\s+)(.+)$", re.IGNORECASE)
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
# Служебные части корзинной команды (квантификатор/«состав»/предлог/«из
# корзины»): отдельными константами с тем же IGNORECASE, что у остальных
# корзинных regex — инлайновые re.sub флаг теряли, и «убери Одну Гавайскую
# пиццу из корзины» давало товар «Одну Гавайскую»
_CART_QTY_HEAD_RE = re.compile(r"^(?:одну|один|1)\s+", re.IGNORECASE)
_CART_COMPOSITION_HEAD_RE = re.compile(r"^состав\s+", re.IGNORECASE)
_CART_PREP_HEAD_RE = re.compile(r"^(?:на|в|во)\s+", re.IGNORECASE)
_CART_FROM_CART_TAIL_RE = re.compile(
    r"\s+из\s+(?:корзины|заказа)\s*$", re.IGNORECASE)
# «убавь громкость/яркость» — настройки устройства, не корзина
_CART_NOT_PRODUCT_RE = re.compile(
    r"\b(?:громкост\w*|звук\w*|яркост\w*|скорост\w*|свет\w*|температур\w*|"
    r"шрифт\w*|масштаб\w*)\b", re.IGNORECASE)
# Слова корзины: с ними голое «увеличь/убавь X» — точно про товар
_CART_WORD_RE = re.compile(
    r"(?<![а-яёa-z])(?:корзин\w*|штук\w*|шт\.?|количеств\w*|порци\w*|"
    r"позици\w*|товар\w*|заказ\w*)", re.IGNORECASE)
_CART_IN_CART_TAIL_RE = re.compile(
    r"\s+(?:в|во|из)\s+(?:корзин\w*|заказ\w*)\s*$", re.IGNORECASE)
_CART_QTY_WORD_RE = re.compile(r"(?<![а-яё])количеств\w*", re.IGNORECASE)
# Идиомы и абстракции после «убавь/прибавь»: «убавь пыл», «прибавь шагу/
# газу/ходу», «убавь аппетиты» — речь, а не товар
_CART_IDIOM_RE = re.compile(
    r"^(?:пыл\w*|шаг\w*|темп\w*|газ[уа]?|оборот\w*|ход[уа]?|жар\w*|огон\w*|"
    r"огн\w*|аппетит\w*|амбици\w*|зарплат\w*|оклад\w*|цен[уыа]|сил[уы]?|"
    r"мощност\w*|энерги\w*|усили\w*|настроени\w*|голос\w*|громк\w*|пафос\w*|"
    r"спес\w*|гонор\w*|эмоци\w*|рвени\w*|ставк\w*|срок\w*|нагрузк\w*|"
    r"расход\w*|вес[а]?|шанс\w*|себе|мне|ему|ей|нам|им|тебе|немного|чуть)"
    r"(?:\s|$)", re.IGNORECASE)


def parse_cart_request(text: str) -> Optional[Tuple[str, str]]:
    """Команда операции с корзиной сайта → (op, product):
    «убери гавайскую из корзины» → ("remove", "гавайскую");
    «убери одну гавайскую пиццу»/«убавь начос» → decrease;
    «прибавь колу»/«добавь ещё одну колу» → increase;
    «измени песто в корзине» → edit. None — не команда корзины.
    «убери X» БЕЗ «из корзины» сюда не ловится — остаётся инвентарю бота
    (его маркеры: инвентарь/рюкзак/карман)."""
    t = " ".join(str(text or "").split()).strip()
    if not t or len(t) > 80:
        return None
    op = None
    bare = False  # голое «убавь/прибавь X» без количества и «из корзины»
    m = _CART_REMOVE_RE.match(t)
    if m:
        op = "remove"
    if not m:
        m = _CART_DEC_NUM_RE.match(t)
        if not m:
            m = _CART_DEC_RE.match(t)
            bare = bool(m)
        if m:
            op = "decrease"
    if not m:
        m = _CART_INC_NUM_RE.match(t)
        if not m:
            m = _CART_INC_RE.match(t)
            bare = bool(m)
        if m:
            op = "increase"
    if bare and not _CART_WORD_RE.search(t):
        # «увеличь зарплату», «убавь пыл», «прибавь шагу» — не корзина.
        # «увеличь/уменьши» слишком общие — без слова корзины не берём;
        # «убавь/прибавь X» — товар, если X не идиома/абстракция
        if re.match(r"^\s*(?:увеличь|уменьши)\b", t, re.IGNORECASE):
            return None
        if _CART_IDIOM_RE.match(m.group(1).strip()):
            return None
    if not m:
        m = _CART_EDIT_RE.match(t)
        if m:
            op = "edit"
    if not m:
        return None
    # «увеличь количество колы в корзине» — служебные слова не название
    product = _CART_IN_CART_TAIL_RE.sub("", m.group(1))
    product = _CART_QTY_WORD_RE.sub(" ", product)
    product = _CART_FILLER_RE.sub(" ", product)
    product = " ".join(product.split()).strip(" ,.;!?")
    # «плюс один начос» — квантификатор не часть названия
    product = _CART_QTY_HEAD_RE.sub("", product)
    if len(product) < 2:
        return None
    if _CART_NOT_PRODUCT_RE.search(product):
        return None
    return op, product


def _cart_product_name(raw: str, op: str) -> str:
    """Название товара из хвоста корзинной команды: срезаем «из корзины»,
    у edit-форм — слово «состав» («изменить состав на гавайская»), предлог
    «на/в/во» в начале и филлеры («пиццу», «товар»). Может выйти пустым —
    «изменить состав» без уточнения."""
    product = _CART_FROM_CART_TAIL_RE.sub("", str(raw or ""))
    if op == "edit":
        product = _CART_COMPOSITION_HEAD_RE.sub("", product)
    product = _CART_PREP_HEAD_RE.sub("", product)
    return " ".join(
        _CART_FILLER_RE.sub(" ", product).split()).strip(" ,.;!?")


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
    «что в разделе напитки на сайте?» → ("напитки", "сайте", "напитки на сайте");
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
    # Хвост «… на <сайт>» — сайт; «на странице/на сайте» — пустое указание места
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


# ── Нормализация команды перед лесенкой парсеров ──
# Парсеры якорятся на голый императив в начале фразы («^открой …»), а люди
# пишут «Коннор, можешь открыть ютуб, пожалуйста?». Снимаем обращение,
# вежливость, частицы и обрамляющую пунктуацию — парсеры видят «открой
# ютуб». Оригинал фразы остаётся вызывающему (классификация «да/нет»,
# запись в историю).

_NC_POLITE_ALT = (
    r"(?:пожалуйста|плиз|плз|please|pls|plz|kindly|"
    r"будь(?:те)?\s+(?:добр(?:а|ы)?|любезн(?:а|ы)?)|"
    r"если\s+(?:не\s+)?(?:трудно|сложно|можно))")
# Вежливость снимаем только по краям фразы и сразу после глагола:
# внутри запроса/текста/кавычек она — часть содержимого («найди Please
# Please Me», «напиши в чат «приходи, пожалуйста, завтра»»)
_NC_POLITE_HEAD_RE = re.compile(
    rf"^{_NC_POLITE_ALT}(?:\s*[,!]\s*|\s+)", re.IGNORECASE)
_NC_POLITE_TAIL_RE = re.compile(
    rf"(?:\s*,\s*|\s+){_NC_POLITE_ALT}\s*[.!?…]*\s*$", re.IGNORECASE)
# После первого слова-глагола: «открой пожалуйста ютуб» (одно слово — и без
# запятых), «включи, будь добр, музыку» (многословное — только в запятых)
_NC_POLITE_AFTER_VERB_RE = re.compile(
    r"^(?P<verb>[\w-]+)(?:\s*,\s*|\s+)(?:"
    r"(?:пожалуйста|плиз|плз)(?:\s*,\s*|\s+)|"
    rf"{_NC_POLITE_ALT}\s*,\s*)", re.IGNORECASE)
# Вводные слова в начале: «а», «ну», «давай», «слушай», «теперь»…
_NC_LEAD_RE = re.compile(
    r"^(?:(?:а|ну|давай(?:те)?|так|слушай|эй|hey|ok(?:ay)?|окей|ок|ладно|"
    r"теперь|now)(?:\s*[,!]\s*|\s+))+",
    re.IGNORECASE)
# «(ты) можешь (ли ты) / не мог бы ты» + инфинитив → императив
_NC_CAN_RE = re.compile(
    r"^(?:ты\s+)?(?:не\s+)?(?:можешь|сможешь|мог(?:ла|ли)?\s+бы|можете|"
    r"сможете)(?:\s+ли)?(?:\s+ты|\s+вы)?\s+"
    r"(?P<verb>[а-яё]+(?:ть|ти|чь)(?:ся|сь)?)(?P<rest>(?:\s.*)?)$",
    re.IGNORECASE | re.DOTALL)
_NC_CAN_EN_RE = re.compile(
    r"^(?:(?:can|could|would|will)\s+you\s+|"
    r"(?:i\s+(?:want|need)\s+you\s+to|i'?d\s+like\s+you\s+to)\s+)",
    re.IGNORECASE)
# Частица «-ка»: «открой-ка», «нажми-ка»
_NC_KA_RE = re.compile(r"(?<=[а-яё])-ка\b", re.IGNORECASE)
_NC_EDGE_PUNCT = " \t\n\r,.!?;:…"
_NC_QUOTE_CHARS = "\"'«»“”„`"
_NC_QUOTE_PAIRS = {"«": "»", "“": "”", "„": "“", '"': '"', "'": "'", "`": "`"}


def _nc_strip_edges(s: str) -> str:
    """Обрамляющая пунктуация; кавычки — только парой вокруг ВСЕЙ фразы
    («"открой ютуб"»). Закрывающую кавычку текста («напиши в чат
    «привет»») не трогаем."""
    s = s.strip(_NC_EDGE_PUNCT)
    while len(s) >= 2 and _NC_QUOTE_PAIRS.get(s[0]) == s[-1] \
            and not any(ch in _NC_QUOTE_CHARS for ch in s[1:-1]):
        s = s[1:-1].strip(_NC_EDGE_PUNCT)
    return s


def _nc_is_type_command(s: str) -> bool:
    # Ввод с указанием поля: хвост фразы — текст для поля, его не чистим
    return bool(_TYPE_REQUEST_RE.match(s)
                and _TYPE_FIELD_MARK_RE.search(f" {s}"))


# Инфинитив → императив только для частых глаголов команд: без морфологии
# остальное надёжнее оставить как есть (фраза уйдёт дальше без замены)
_NC_INF2IMP = {
    "открыть": "открой", "закрыть": "закрой", "нажать": "нажми",
    "кликнуть": "кликни", "включить": "включи", "выключить": "выключи",
    "найти": "найди", "показать": "покажи", "поставить": "поставь",
    "перейти": "перейди", "зайти": "зайди", "вернуться": "вернись",
    "прокрутить": "прокрути", "пролистать": "пролистай",
    "промотать": "промотай", "листать": "листай", "скачать": "скачай",
    "ввести": "введи", "написать": "напиши", "набрать": "набери",
    "отправить": "отправь", "прочитать": "прочитай", "прочесть": "прочти",
    "обновить": "обнови", "перезагрузить": "перезагрузи",
    "увеличить": "увеличь", "уменьшить": "уменьши", "добавить": "добавь",
    "удалить": "удали", "убрать": "убери", "выбрать": "выбери",
    "заказать": "закажи", "купить": "купи", "запустить": "запусти",
    "остановить": "останови", "переключить": "переключи",
    "переключиться": "переключись", "навести": "наведи",
    "сделать": "сделай", "посмотреть": "посмотри", "поискать": "поищи",
    "стереть": "сотри", "сбросить": "сбрось", "перетащить": "перетащи",
    "оформить": "оформи", "вставить": "вставь", "отметить": "отметь",
    "свернуть": "сверни", "развернуть": "разверни",
    "пролистнуть": "пролистни", "докрутить": "докрути",
}


def normalize_command(text: str, persona_names=()) -> str:
    """«Коннор, можешь открыть ютуб, пожалуйста?» → «открой ютуб».
    persona_names — как обращаются к персоне (BotInstance._address_names).
    Снимает обращение по имени в начале/конце, вежливые слова, вводные
    частицы, «-ка», «можешь X-ть» → императив (по словарю частых глаголов),
    «can you …» и обрамляющую пунктуацию/кавычки. Пустой результат —
    исходная строка (без обрамляющих пробелов)."""
    src = (text or "").strip()
    s = src
    if not s:
        return ""
    names = sorted({str(n).strip() for n in (persona_names or ())
                    if n and len(str(n).strip()) >= 2}, key=len, reverse=True)
    for _ in range(3):
        prev = s
        s = _nc_strip_edges(s)
        for n in names:
            # «Коннор, открой…» / «эй Коннор открой…» / «открой ютуб, Коннор»
            s = re.sub(rf"^(?:(?:эй|hey)\s*,?\s*)?{re.escape(n)}(?:\s*[,:!]\s*|\s+)",
                       "", s, flags=re.IGNORECASE)
            s = re.sub(rf"\s*,\s*{re.escape(n)}\s*[.!?…]*$", "", s,
                       flags=re.IGNORECASE)
        s = _NC_LEAD_RE.sub("", s)
        s = _NC_POLITE_HEAD_RE.sub("", s)
        s = _NC_POLITE_AFTER_VERB_RE.sub(lambda mm: mm.group("verb") + " ", s)
        if not _nc_is_type_command(s):
            s = _NC_POLITE_TAIL_RE.sub("", s)
        s = _NC_KA_RE.sub("", s)
        m = _NC_CAN_RE.match(s)
        if m and m.group("verb").lower() in _NC_INF2IMP:
            s = _NC_INF2IMP[m.group("verb").lower()] + m.group("rest")
        s = _NC_CAN_EN_RE.sub("", s)
        s = re.sub(r"\s+([,.!?])", r"\1", re.sub(r"\s{2,}", " ", s)).strip()
        if s == prev:
            break
    s = _nc_strip_edges(s)
    return s or src


# ── Гейт LLM-яруса: похоже ли сообщение на команду ──
# LLM-разбор — лишний вызов модели ДО основного ответа; на «как дела?» в
# режиме управления он только тормозит. Дешёвая эвристика: императив/
# «хочу X-ть»/слова про страницу. Языки, для которых regex не судья (не
# ru/en), пропускаем в LLM-ярус как раньше.
_LC_RU_VERB_RE = re.compile(
    r"^[а-яё]{2,}(?:ай|яй|ей|уй|юй|ой|и|ь|ите|йте|ьте|ись|йся|ься|ьтесь)$",
    re.IGNORECASE)
_LC_RU_NOT_VERB = frozenset({
    "мой", "твой", "свой", "какой", "такой", "никакой", "другой", "иной",
    "любой", "простой", "большой", "плохой", "хороший", "эй", "ой", "мои",
    "твои", "свои", "эти", "те", "все", "они", "люди", "дети", "мысли",
    "деньги", "новости", "очень", "почти", "кстати", "зачем", "почему",
    "сегодня", "день", "жизнь", "вещь", "ночь", "мать", "дочь", "путь",
    "очередь", "здравствуй", "здравствуйте", "прости", "извини",
    "извините", "простите", "спокойной", "доброй", "добрый", "вообщем",
    "ладно", "спасибо", "привет", "пожалуйста", "кажется", "похоже",
})
_LC_RU_WANT_RE = re.compile(
    r"^(?:я\s+)?(?:хочу|хотел(?:а)?\s+бы|надо|нужно|необходимо|пора)\s+"
    r"[а-яё]+(?:ть|ти|чь)(?:ся|сь)?\b", re.IGNORECASE)
_LC_EN_VERBS = frozenset(
    "open click press tap hit go navigate visit browse scroll swipe play "
    "pause resume stop mute unmute search find look type enter write fill "
    "send submit close show display read zoom add remove delete buy order "
    "purchase checkout turn switch download select choose pick check "
    "uncheck toggle start launch run reload refresh back forward drag move "
    "set put hover watch listen book sign log subscribe like follow copy "
    "paste erase clear increase decrease raise lower skip next previous "
    "rewind expand collapse accept reject dismiss cancel confirm save "
    "upload attach take".split())
_LC_EN_LEAD_RE = re.compile(
    r"^(?:i\s+(?:want|need|would\s+like)\s+to|i'd\s+like\s+to|let'?s|"
    r"go\s+ahead\s+and)\b", re.IGNORECASE)
_LC_HINT_RE = re.compile(
    r"(?:вкладк|вкладок|страниц|сайт|кнопк|браузер|ссылк|корзин|масштаб|"
    # Плеер и навигация: «погромче», «на паузу», «субтитры», «полный экран»
    r"громч|потиш|тише|пауз|звук|субтитр|полноэкран|полный\s+экран|"
    r"перемот|плеер|избранн|"
    r"\btabs?\b|\bpage\b|\bbutton\b|\bsite\b|\bbrowser\b|\bcart\b|\blink\b|"
    r"\bvolume\b|\bsubtitles?\b|\bfullscreen\b|\bcaptions?\b|"
    r"https?://|\bwww\.|\b[\w-]+\.(?:ru|com|org|net|io|ua|by|kz|рф|tv|me|"
    r"app|dev)\b)", re.IGNORECASE)
# Буква вне базовых латиницы/кириллицы (é, ö, ñ, і, ї, иероглифы, арабица…)
# — язык не ru/en: detect_language скриптовый и такое отдаёт как ru/en
_LC_OTHER_LETTER_RE = re.compile(r"[^\W\d_a-zA-Zа-яА-ЯёЁ]")
# Частые слова/глаголы-команды других латинских языков без диакритики
# («abre youtube», «haz clic») — ASCII-фраза без них считается английской
_LC_FOREIGN_WORDS = frozenset(
    "abre abrir abra pon ponme haz busca buscar cierra cierre puedes "
    "quiero por una muestra oeffne mach mache "
    "bitte zeig zeige schliesse suche kannst ich und der das ein eine "
    "ouvre ouvrir ferme clique cherche mets montre peux veux les des une "
    "apri chiudi cerca metti mostra clicca puoi voglio favore feche "
    "clique pesquise mostre quero voce".split())
# Короткая фраза (1–3 слова) в режиме управления — скорее команда без
# глагола («вниз», «дальше», «на главную», «в избранное»), кроме болтовни:
# первое слово — местоимение/вопрос/междометие/благодарность
_LC_SHORT_MAX_WORDS = 3
_LC_SHORT_NOT_FIRST = frozenset(
    "я мне меня мой моя ты тебе тебя твой мы нам он она оно они это этот "
    "как что чё че почему зачем кто когда где куда откуда какой какая "
    "привет здравствуй здравствуйте спасибо спс благодарю пока ну да нет "
    "ага угу ок окей хорошо ладно понял поняла понятно ясно круто класс "
    "супер отлично норм нормально ого вау ура хаха ахах лол доброе добрый "
    "спокойной i me my you your we it its this that how what why who when "
    "where which thanks thank hi hello hey bye yes no yeah yep nope ok "
    "okay cool nice great good lol wow sure fine hmm".split())


# Секрет в САМОЙ команде («введи пароль Kotik2019 …», «ivan / Kotik2019!»)
# — единый список слов/пар в cc_privacy (им же пользуется фильтр логов);
# здесь реэкспорт для bot_instance/task_agent
from app.features.cc_privacy import (  # noqa: E402,F401
    _CMD_SECRET_FILLER, _CMD_SECRET_VERB_RE, _CMD_SECRET_WORD_RE,
    audit_pop_chat, audit_restore_lines,
    command_has_secret, command_secret_values)


def looks_like_command(text: str, lang: Optional[str] = None) -> bool:
    """Дешёвый гейт LLM-яруса разбора команды. text — уже нормализованная
    фраза (normalize_command); lang — код языка (detect_language), None —
    неизвестен. True — стоит спросить LLM, что это за действие."""
    s = " ".join(str(text or "").split())
    if not s:
        return False
    if _LC_HINT_RE.search(s):
        return True
    # Язык не ru/en (по буквам — detect_language их не различает): regex
    # не судья, решает LLM-ярус
    if _LC_OTHER_LETTER_RE.search(s):
        return True
    low = s.lower()
    words = re.findall(r"[\w'-]+", low)
    if not words:
        return False
    if any(w in _LC_FOREIGN_WORDS for w in words):
        return True
    w0 = re.split(r"['-]", words[0])[0].replace("ё", "е")
    if len(words) <= _LC_SHORT_MAX_WORDS and w0 \
            and w0 not in _LC_SHORT_NOT_FIRST \
            and words[0] not in _LC_SHORT_NOT_FIRST:
        return True
    if re.match(r"^[а-яё'-]+$", words[0]):
        if _LC_RU_WANT_RE.match(low):
            return True
        for w in words[:2]:
            w = w.split("-")[0]
            # «-шь» — 2-е лицо («думаешь», «знаешь»): вопрос, не приказ
            if w in _LC_RU_NOT_VERB or w.endswith("шь"):
                continue
            if _LC_RU_VERB_RE.match(w):
                return True
        # Кириллица не русская (uk/be/…): regex не судья
        return lang not in (None, "ru", "en")
    if re.match(r"^[a-z'-]+$", words[0]):
        if _LC_EN_LEAD_RE.match(low) or words[0] in _LC_EN_VERBS:
            return True
        # Латиница не английская (es/de/fr…)
        return lang not in (None, "ru", "en")
    # Другие алфавиты: решает LLM-ярус
    return lang not in ("ru", "en")


def tag_origin(action: Optional[dict], origin: str) -> Optional[dict]:
    """Пометка источника действия для аудита (action["origin"]: fast /
    intent_llm / marker / pending / scenario / task). Уже заданный источник
    не перетирается; multi — вместе с вложенными действиями."""
    if isinstance(action, dict):
        action.setdefault("origin", origin)
        for it in action.get("items") or ():
            if isinstance(it, dict):
                it.setdefault("origin", origin)
    return action


# ── Составная команда: «открой додо и нажми на пепперони фреш» ──
# Режем только там, где после связки стоит глагол-команда: «открой ютуб и
# гитхаб» — одна команда на два сайта (parse_open_many), «найди чёрный и
# белый чай» — один запрос. Русские глаголы — по словарю (окончание «-и/-й»
# носят и существительные: «ссылки», «чай»)
_SC_RU_VERBS = frozenset(_NC_INF2IMP.values()) | frozenset({
    "тыкни", "жми", "иди", "вруби", "выруби", "подожди", "листни",
    "проверь", "ткни", "набей", "скинь", "сохрани", "залогинься", "войди",
    "выйди", "перейди", "открой", "нажми", "включи", "поставь", "запусти",
    "покажи", "пришли", "скопируй", "прибавь", "убавь", "сними", "отметь",
    "листай", "мотай", "промотай", "пролистай", "кликни", "наведи",
})
_SC_EN_VERBS = _LC_EN_VERBS - frozenset({
    "back", "next", "previous", "like", "set", "order", "book", "watch",
    "run", "log", "sign", "check", "lower", "raise", "take"})
_SC_SEP_RE = re.compile(
    r"\s*(?:,\s*)?(?:\s(?:и|а)\s+(?:потом|затем|после\s+этого|ещё)\s+|"
    r"\sи\s+|\s*,\s*(?:потом|затем|после\s+этого)\s+|\s(?:потом|затем)\s+|"
    r"\s*;\s*|\s*,\s*|\sand\s+(?:then\s+)?|\s*,?\s*then\s+)",
    re.IGNORECASE)
# «введи X и отправь» — ввод с Enter одной командой (parse_type_request)
_SC_SUBMIT_TAIL_RE = re.compile(
    r"^(?:отправь|отправить|пошли|send|submit)\s*[.!]*$", re.IGNORECASE)
# Явная связка шагов: только она режет тело ввода и подпись клика
_SC_EXPLICIT_SEP_RE = re.compile(r"потом|затем|после\s+этого|\bthen\b",
                                 re.IGNORECASE)
# Англ. клик: «click Accept and close» — подпись кнопки целиком (инфинитив
# и императив в английском совпадают); по голому «and» не режем
_SC_CLICK_HEAD_RE = re.compile(
    r"^(?:click|press|tap|hit|нажми|кликни|тыкни|ткни|щёлкни|щелкни|жми)\s",
    re.IGNORECASE)


def _sc_type_piece(piece: str) -> bool:
    """Кусок начинается командой ввода («введи …», «напиши в чат …»): его
    тело — текст для поля, связки внутри относятся к тексту."""
    m = _TYPE_REQUEST_RE.match(piece)
    if not m:
        return False
    verb = piece.split(None, 1)[0].lower()
    return verb in _TYPE_EXPLICIT_VERBS \
        or bool(_TYPE_FIELD_MARK_RE.search(f" {m.group(1)}"))


def split_compound_command(text: str) -> List[str]:
    """«открой додо и нажми на пепперони фреш» → [«открой додо», «нажми на
    пепперони фреш»]; «открой ютуб, потом включи музыку» → два шага.
    Режет по связкам (и / потом / затем / , / ; / and / then), только если
    следующий кусок начинается с глагола-команды и вне кавычек. Тело ввода
    («напиши в чат …») и англ. подпись клика («click Save and close») режутся
    только явной связкой (потом/затем/после этого/then). Не составная —
    [text]."""
    s = " ".join(str(text or "").split())
    if not s:
        return []
    parts: List[str] = []
    start = 0
    for m in _SC_SEP_RE.finditer(s):
        head = s[:m.start()]
        # Внутри кавычек («введи «привет и пока»») не режем
        if head.count("«") > head.count("»") or head.count('"') % 2:
            continue
        nxt = s[m.end():]
        w = re.match(r"[\w'-]+", nxt)
        if not w:
            continue
        word = w.group(0).lower().split("-")[0]
        if word not in _SC_RU_VERBS and word not in _SC_EN_VERBS:
            continue
        if _SC_SUBMIT_TAIL_RE.match(nxt):
            continue
        sep = m.group(0)
        explicit = bool(_SC_EXPLICIT_SEP_RE.search(sep))
        rest_piece = s[start:]
        if _sc_type_piece(rest_piece):
            # «напиши в чат ок, открой ссылку» — «, открой ссылку» часть
            # текста. Режем только по явной связке и только после поля
            # (если поле вообще названо): «напиши в чат привет, потом
            # открой ютуб»
            if not explicit:
                continue
            if _TYPE_FIELD_MARK_RE.search(f" {rest_piece}") \
                    and not _TYPE_FIELD_MARK_RE.search(
                        f" {s[start:m.start()]}"):
                continue
        elif _SC_CLICK_HEAD_RE.match(rest_piece) and not explicit \
                and not re.search(r"[,;]", sep) \
                and re.search(r"\band\b", sep, re.IGNORECASE):
            continue
        piece = s[start:m.start()].strip(" ,;")
        if piece:
            parts.append(piece)
        start = m.end()
    tail = s[start:].strip(" ,;")
    if tail:
        parts.append(tail)
    return parts or [s]


def is_goal_task(action: Optional[dict]) -> bool:
    """Многошаговая цель для агента ({"kind": "task", "goal"} от LLM-яруса),
    а не task-рецепт конфига ({"kind": "task", "key", "value"} — ярлык/
    рецепт, исполняется execute)."""
    return (isinstance(action, dict) and action.get("kind") == "task"
            and bool(action.get("goal")) and not action.get("value"))


# ── LLM-ярус разбора команды (последний, после regex-каскада) ──
# Текстовый JSON-протокол вместо tool-calling: работает на всех провайдерах
# роутера, включая webchat (модель там видит только текст) и локальные
# модели без tools=. Regex-парсеры выше — бесплатные и мгновенные; этот ярус
# ловит формулировки, которые никто не предсказал регэкспом. Результат
# проходит те же резолверы и тот же confirm/allowlist, что и regex-путь:
# протокол меняет только то, КАК определено намерение, не то, что разрешено
# исполнять.

_INTENT_KEYS = frozenset({
    "Space", "Enter", "Escape", "Tab", "Backspace",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "m"})

_INTENT_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _intent_int(v) -> Optional[int]:
    """Число из JSON LLM-разбора (int/float/«8»); мусор — None."""
    if isinstance(v, bool):
        return None
    try:
        return int(round(float(str(v).strip())))
    except (TypeError, ValueError):
        return None


def intent_prompt(text: str, lang: Optional[str] = None) -> str:
    """Промпт классификации команды управления в JSON-действие (плоский
    объект, без вложенности — parse_intent_action его и ждёт). lang — язык
    пользователя; None — по самой фразе."""
    return (
        "Mode: controlling the user's computer. Determine which ACTION the "
        "phrase asks for and reply with ONLY one JSON object, no explanations.\n"
        "Actions (site — the site/tab name if it is named explicitly, otherwise "
        "null; omit fields that have no value). Copy goal/target/text/field/"
        "query values from the phrase in its own wording, do not translate "
        "them:\n"
        '{"action":"click","goal":"what to click","site":null} — click/select '
        "a page element\n"
        '{"action":"hover","goal":"what to hover","site":null} — move the '
        "cursor over an element (no click: reveal a hover menu/buttons)\n"
        '{"action":"open","target":"site or app"} — open a site, '
        "launch a program\n"
        '{"action":"type","text":"text","field":"field","site":null} — type '
        "text into a field on the page\n"
        '{"action":"download","goal":"what to download","site":null} — download '
        "a file from the page\n"
        '{"action":"scroll","side":"left|right","direction":"up|down",'
        '"stop":false} — keep scrolling the page (auto-scroll)/stop '
        "scrolling\n"
        '{"action":"scroll_to","goal":"what to find|top|bottom"} — scroll '
        "the page until a named thing is visible, or to its top/bottom\n"
        '{"action":"key","key":"Space|Enter|Escape|Tab|Backspace|ArrowUp|'
        'ArrowDown|ArrowLeft|ArrowRight","times":1} — press a key in the page '
        "(player pause/play is Space; times — how many presses)\n"
        '{"action":"zoom","direction":"in|out|reset","site":null} — change '
        "the page zoom\n"
        '{"action":"slider","goal":"slider label","value":50,'
        '"unit":"pct|min|sec|"} — set a slider on the page to a number\n'
        '{"action":"cart","op":"remove|increase|decrease|edit",'
        '"product":"item"} — change an item in the site\'s shopping cart\n'
        '{"action":"page_view","screenshot":false,"full":false,"site":null}'
        " — describe/show what is on the open page (screenshot — a picture "
        "was asked for; full — the whole page, not just the visible part)\n"
        '{"action":"send","site":null} — send the typed message (Enter)\n'
        '{"action":"close","goal":"what to close"} — close a window/popup/block\n'
        '{"action":"reload_tab","site":null} — refresh/reload the '
        "browser tab (F5)\n"
        '{"action":"close_tab","goal":"which tab","site":null} — close '
        "a browser tab (goal — if it is named)\n"
        '{"action":"back","site":null} — go back in the tab history\n'
        '{"action":"forward","site":null} — go forward in the tab '
        "history\n"
        '{"action":"read","mode":"last|page","site":null} — read the '
        "last chat message / the page\n"
        '{"action":"switch_tab","goal":"tab name"} — switch '
        "to an open tab\n"
        '{"action":"search","query":"query","site":"site"} — search on '
        "a specific site\n"
        '{"action":"task","goal":"the whole goal"} — a GOAL that needs a chain '
        "of several actions across a site/sites, not one command (order food, "
        "buy/book something, find and download a file somewhere, fill in and "
        "send a form); goal — the user's request in their own wording\n"
        '{"action":"none"} — this is NOT a computer/browser control command '
        "(ordinary conversation, a question, a request to write something)\n"
        f"Phrase: \"{text}\"\n"
        + user_language_line(lang or detect_language(text)))


def parse_intent_action(resp: str) -> Optional[dict]:
    """Ответ LLM-разбора команды → нормализованный dict действия или None.
    Строго: плоский JSON, известный action, строковые поля с лимитами длины.
    Выход модели не исполняется напрямую — дальше те же резолверы."""
    m = _INTENT_JSON_RE.search(str(resp or ""))
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("action") or "").strip().lower()
    if kind == "none":
        return {"action": "none"}

    def _s(key: str, cap: int) -> Optional[str]:
        v = data.get(key)
        if v is None:
            return None
        v = " ".join(str(v).split()).strip()
        return v[:cap] or None

    out: Dict[str, object] = {"action": kind}
    site = _s("site", 40)
    if site:
        out["site"] = site
    if kind in ("click", "download", "close", "hover"):
        goal = _s("goal", 60)
        if not goal:
            return None
        out["goal"] = goal
    elif kind == "open":
        target = _s("target", 60)
        if not target:
            return None
        out["target"] = target
    elif kind == "type":
        text = _s("text", 200)
        if not text:
            return None
        out["text"] = text
        field = _s("field", 60)
        if field:
            out["field"] = field
    elif kind == "scroll":
        if data.get("stop"):
            out["stop"] = True
        side = _s("side", 10)
        if side in ("left", "right"):
            out["side"] = side
        if _s("direction", 10) == "up":
            out["direction"] = "up"
    elif kind == "scroll_to":
        goal = _s("goal", 60)
        if not goal:
            return None
        out["goal"] = goal
    elif kind == "key":
        key = _s("key", 20)
        if not key:
            return None
        key = _KEY_MAP.get(key.lower(), key)
        if key not in _INTENT_KEYS:
            return None
        out["key"] = key
        times = _intent_int(data.get("times"))
        if times and times > 1:
            out["times"] = min(times, _ERASE_MAX)
    elif kind == "zoom":
        direction = _s("direction", 10)
        if direction not in ("in", "out", "reset"):
            return None
        out["direction"] = direction
    elif kind == "slider":
        goal = _s("goal", 40)
        value = _intent_int(data.get("value"))
        if not goal or value is None:
            return None
        out["goal"] = goal
        out["value"] = value
        unit = _s("unit", 10)
        out["unit"] = unit if unit in ("pct", "min", "sec") else ""
    elif kind == "cart":
        op = _s("op", 10)
        product = _s("product", 60)
        if op not in ("remove", "increase", "decrease", "edit") \
                or not product:
            return None
        out["op"] = op
        out["product"] = product
    elif kind == "page_view":
        out["screenshot"] = bool(data.get("screenshot"))
        out["full"] = bool(data.get("full"))
    elif kind == "send":
        pass
    elif kind == "read":
        out["mode"] = "page" if data.get("mode") == "page" else "last"
    elif kind == "switch_tab":
        goal = _s("goal", 60)
        if not goal:
            return None
        out["goal"] = goal
    elif kind in ("reload_tab", "close_tab", "back", "forward"):
        goal = _s("goal", 60)  # какая вкладка; None — текущая видимая
        if goal:
            out["goal"] = goal
    elif kind == "task":
        goal = _s("goal", 400)
        if not goal:
            return None
        out["goal"] = goal
    elif kind == "search":
        query = _s("query", 80)
        if not query or not site:
            return None
        out["query"] = query
    else:
        return None
    return out


def intent_pseudo_action(act: dict) -> Optional[dict]:
    """LLM-разбор → действие, которое исполняет сам бот, а не резолверы:
    task (цель для агента), scroll_goal (доскролл до цели), page_view
    (отчёт о странице) — тем же кодом, что regex-ветки. None — обычное
    действие для резолверов."""
    kind = act.get("action")
    if kind == "task":
        # От task-рецептов конфига ({"kind": "task", "key", "value"})
        # отличается ключом goal; подтверждение (force_confirm) ставит бот
        return {"kind": "task", "goal": str(act["goal"])}
    if kind == "scroll_to":
        goal = str(act["goal"])
        goal = {"top": "начала", "bottom": "конца"}.get(goal.lower(), goal)
        return {"kind": "scroll_goal", "goal": goal}
    if kind == "page_view":
        return {"kind": "page_view", "site": act.get("site"),
                "screenshot": bool(act.get("screenshot")),
                "full": bool(act.get("full"))}
    return None


# «закрыть на на джем» — сдвоенный предлог из склейки цели и скопа
_DUP_PREP_RE = re.compile(
    r"\b(на|в|во|по|к|ко|с|со|у|о|об|за|из|от|до)\s+\1\b", re.IGNORECASE)
# Цель клика, которая на деле клавиша («ArrowDown», «PageDown», «пробел»)
_NON_CLICK_KEY_RE = re.compile(
    r"^(?:arr?o?w\s*(?:up|down|left|right)|arrordown|page\s*(?:up|down)"
    # «home» и «стрелку вправо» не берём: ссылка «Home» и стрелка карусели —
    # обычные цели клика на сайтах
    r"|" + "|".join(map(re.escape, sorted(_KEY_MAP, key=len, reverse=True)))
    + r")$", re.IGNORECASE)
# …листание (в т.ч. с опечатками: «пролситать»)
_NON_CLICK_SCROLL_RE = re.compile(
    r"^(?:прол\w*с\w*т\w*|прокрут\w*|листа\w*|скрол\w*|scroll\w*)\b",
    re.IGNORECASE)
# …название сайта («сайт додо пицца»)
_NON_CLICK_SITE_RE = re.compile(r"^(?:сайт|site|website)\s+(\S.*)$",
                                re.IGNORECASE)
# …звукоподражание из повторённого слога («тук-тук», «кап кап»)
_NON_CLICK_ONOMATOPOEIA_RE = re.compile(r"^([^\W\d_]{2,5})(?:[-\s]\1){1,3}$",
                                        re.IGNORECASE)


# ── Состояние браузера по чатам ──────────────────────────
# Браузер один на персону, а «где я» — у каждого чата своё: «нажми войти» в
# личке Telegram не должно уходить во вкладку, открытую из веб-чата, «стоп»
# одного чата — гасить листание другого. Ключ — ключ режима управления
# (chat_id, у веб-чата без chat_id — user_id), как у cc_turn_enter/request_stop.

# Текущий чат вызова: ставят входные точки (execute, резолвы с chat_id,
# ход бота) на время вызова, снимается токеном — в потоке пула не залипает
_CC_CHAT: contextvars.ContextVar = contextvars.ContextVar("vpc_cc_chat",
                                                          default=None)
# Сколько чатов держит last_tab.json (свежие по ts)
LAST_TAB_MAX_CHATS = 50


@dataclass
class ChatBrowserState:
    """«Где я» одного чата: отслеживаемая вкладка (id/хост/URL), кэш списка
    вкладок, сеанс авто-листания и видимый хост на последнем действии бота
    (база _follow_visible_tab)."""
    last_host: Optional[str] = None
    last_tab_id: Optional[int] = None
    last_url: Optional[str] = None
    known_tabs: List[dict] = field(default_factory=list)
    scroll: Optional[dict] = None
    scroll_ended: Optional[Tuple[float, str]] = None
    vis_baseline: Optional[str] = None
    ts: float = 0.0  # последняя запись на диск — обрезка last_tab.json


def _chat_key(chat_id) -> str:
    # «None» — след str(chat_id) у веб-чата без chat_id, не ключ чата
    k = "" if chat_id is None else str(chat_id).strip()
    return "" if k == "None" else k


class _ChatAttr:
    """Атрибут менеджера (_last_host, _scroll, …) → поле состояния ТЕКУЩЕГО
    чата: код и тесты читают/пишут mgr._last_host как раньше."""

    def __init__(self, name: str):
        self.name = name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        return getattr(obj._st(), self.name)

    def __set__(self, obj, value):
        setattr(obj._st(), self.name, value)


def _in_chat(fn):
    """Входная точка менеджера: на время вызова текущий чат — аргумент
    chat_id (пустой — остаётся чат хода)."""
    import inspect
    names = list(inspect.signature(fn).parameters)
    pos = names.index("chat_id") - 1  # без self

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        cid = kwargs.get("chat_id") if "chat_id" in kwargs else (
            args[pos] if len(args) > pos else None)
        k = _chat_key(cid)
        if not k:
            return fn(self, *args, **kwargs)
        tok = _CC_CHAT.set(k)
        try:
            return fn(self, *args, **kwargs)
        finally:
            _CC_CHAT.reset(tok)
    return wrapper


# ── Инвариант подтверждения (единая точка — execute) ─────
# Рискованное действие (подпись оплаты/коммита/удаления, force_confirm,
# непроверенная подпись, клик по точке vision, ввод в чувствительное поле,
# маркер модели, адрес из поиска) исполняется ТОЛЬКО с токеном
# подтверждения. Токен — объект, а не строка: из JSON/маркера/ответа LLM его
# не собрать; ставит его только grant_confirmation (pending «да» владельца,
# «да» агенту задач, «да» шагу сценария). needs_confirm решает, СПРАШИВАТЬ
# ли; execute отказывает, если вызывающий спросить забыл.

class _ConfirmToken:
    """Подтверждение человека. steps — для nav-продолжения: {номер шага:
    подпись элемента, которую человек видел в вопросе}."""

    def __init__(self, via: str, by=None, steps: Optional[Dict[int, str]] = None):
        self.via = str(via or "")
        self.by = None if by is None else str(by)
        self.ts = time.time()
        self.steps = dict(steps or {})

    def __repr__(self) -> str:
        return f"<confirmed via={self.via}>"


class NeedsConfirm(RuntimeError):
    """Исполнение остановлено гейтом: действие (или шаг маршрута) требует
    подтверждения, а токена нет. info — что спросить (action["confirm_required"])."""
    error_class = "needs_confirm"

    def __init__(self, msg: str, info: Optional[dict] = None):
        super().__init__(msg)
        self.info = dict(info or {})


# Причины гейта → пояснение в вопросе (cc_texts: gate_risk_<причина>)
GATE_RISKS = ("payment", "commit", "destructive", "force_confirm",
              "label_unverified", "point", "sensitive_field", "marker",
              "via_search")
# Явная команда пользователя в этом ходе (regex-разбор его фразы): отправку
# для неё решает политика confirm, а не гейт. LLM-ярус сюда не входит —
# «а как тут отправить?» модель может прочитать как команду
_USER_CMD_ORIGINS = ("fast",)
# Клавиши, которые могут отправить форму/нажать кнопку в фокусе
_SUBMIT_KEYS = frozenset({"Enter", "Space", "Tab"})


def _gate_label_norm(s) -> str:
    return " ".join(str(s or "").lower().replace("ё", "е").split())[:80]


def forget_chat_files(base_dir, chat_id) -> dict:
    """Файловая часть очистки диалога (без живого менеджера — режим
    управления выключен, а файлы прошлых запусков остались): запись чата из
    last_tab.json и его строки аудита → {"last_tab": …, "audit": […]}
    (+ "last_tab_legacy" — старый формат без chats, одна страница на
    персону — удалена целиком)."""
    base = Path(base_dir)
    ck = _chat_key(chat_id)
    out: dict = {}
    if not ck:
        return out
    path = base / "last_tab.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if isinstance(data, dict):
        chats = data.get("chats")
        if isinstance(chats, dict):
            rec = chats.pop(ck, None)
            if rec is not None:
                out["last_tab"] = rec
                # «Последняя запись» наверху — справочная копия свежего чата:
                # если это был он, наверх — следующий по свежести или ничего
                if isinstance(rec, dict) and data.get("url") == rec.get("url") \
                        and data.get("host") == rec.get("host"):
                    rest = sorted((r for r in chats.values()
                                   if isinstance(r, dict)),
                                  key=lambda r: float(r.get("ts") or 0))
                    for k in ("host", "url", "ts"):
                        data.pop(k, None)
                    if rest:
                        data.update(host=rest[-1].get("host"),
                                    url=rest[-1].get("url"),
                                    ts=rest[-1].get("ts"))
        elif data.get("host"):
            out["last_tab_legacy"] = dict(data)
            data = {}
        if out:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(path)
    audit = audit_pop_chat(base / "audit.jsonl", ck)
    if audit:
        out["audit"] = audit
    return out


def restore_chat_files(base_dir, chat_id, data: dict) -> None:
    # Обратно к forget_chat_files: запись last_tab.json и строки аудита
    base = Path(base_dir)
    ck = _chat_key(chat_id)
    if not ck or not data:
        return
    rec, legacy = data.get("last_tab"), data.get("last_tab_legacy")
    if isinstance(rec, dict) or isinstance(legacy, dict):
        path = base / "last_tab.json"
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
        if not isinstance(cur, dict):
            cur = {}
        if isinstance(rec, dict):
            cur.setdefault("chats", {}).setdefault(ck, rec)
            if not cur.get("host"):
                cur.update(host=rec.get("host"), url=rec.get("url"),
                           ts=rec.get("ts"))
        elif not cur:
            cur = dict(legacy)
        base.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    if data.get("audit"):
        audit_restore_lines(base / "audit.jsonl", data["audit"])


class ComputerControlManager:
    # Разбор маркеров, allowlist-валидация, pending-подтверждения, исполнение.

    def __init__(self, context: str = "default", config: Optional[dict] = None,
                 base_dir: Optional[Path] = None):
        self.context = context
        self._pending: Dict[str, dict] = {}
        self._lock = threading.Lock()
        # Браузер один на всех: действия двух чатов не должны чередоваться
        # (клик одного посреди навигации другого). RLock — execute
        # вложенно зовётся из сценариев/задач того же потока
        self._exec_lock = threading.RLock()
        self.stats = {"markers": 0, "executed": 0, "failed": 0,
                      "confirmed": 0, "declined": 0, "rejected": 0,
                      # Выбор элемента: сколько решений принял
                      # детерминированный скоринг и сколько ушло в LLM-фолбэк
                      "choices": 0, "llm_calls": 0,
                      "llm_valid": 0, "llm_invalid": 0,
                      # Vision-ярусы (гибрид/рамки/зоны) — отдельно от
                      # текстовых llm_*: иначе llm_share превышал 1
                      "vision_calls": 0, "vision_valid": 0,
                      "vision_invalid": 0}
        self.base_dir = base_dir or data_dir() / context / "computer_control"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # «Где я» — по чатам (ChatBrowserState): отслеживаемая вкладка
        # (_last_host/_last_tab_id/_last_url — цель команд без сайта), кэш
        # вкладок (_known_tabs), авто-листание (_scroll/_scroll_ended), база
        # видимой вкладки (_vis_baseline). Атрибуты с этими именами читают и
        # пишут состояние текущего чата (_ChatAttr, _cur_chat)
        self._chat_state_map: Dict[str, ChatBrowserState] = {}
        self._chat_state_lock = threading.RLock()
        # Сеансы листания всех чатов — под одним локом
        self._scroll_lock = threading.Lock()
        self.update_config(config)
        # Контекст «с каким сайтом работали» переживает перезапуск процесса
        self._restore_last_page()

    # ── Состояние браузера по чатам ───────────────────────

    _last_host = _ChatAttr("last_host")
    _last_tab_id = _ChatAttr("last_tab_id")
    _last_url = _ChatAttr("last_url")
    _known_tabs = _ChatAttr("known_tabs")
    _scroll = _ChatAttr("scroll")
    _scroll_ended = _ChatAttr("scroll_ended")
    _vis_baseline = _ChatAttr("vis_baseline")

    def _chat_states(self) -> Dict[str, ChatBrowserState]:
        # Через __dict__ — и у менеджеров тестов, собранных без __init__
        return self.__dict__.setdefault("_chat_state_map", {})

    def _state_lock(self):
        lk = self.__dict__.get("_chat_state_lock")
        if lk is None:
            lk = self.__dict__.setdefault("_chat_state_lock", threading.RLock())
        return lk

    def _cur_chat(self) -> str:
        """Чей контекст браузера сейчас: чат вызова (chat_scope / входная
        точка с chat_id) → чат хода этого потока (set_turn) → последний
        активный чат (вызов вне хода: тесты, служебные пути) → "" (шаблон)."""
        k = _CC_CHAT.get() or _chat_key(self.turn_key())
        if k:
            self.__dict__["_last_chat"] = k
            return k
        return self.__dict__.get("_last_chat") or ""

    def _st(self, key: Optional[str] = None) -> ChatBrowserState:
        """Состояние чата (key None — текущего). Новый чат начинается с
        шаблона "": умолчание из старого last_tab.json (в тестах — то, что
        выставили до первого чата); состояния других чатов в него не пишутся."""
        k = self._cur_chat() if key is None else _chat_key(key)
        states = self._chat_states()
        st = states.get(k)
        if st is not None:
            return st
        with self._state_lock():
            st = states.get(k)
            if st is None:
                tpl = states.get("") if k else None
                st = (ChatBrowserState(
                    last_host=tpl.last_host, last_tab_id=tpl.last_tab_id,
                    last_url=tpl.last_url, known_tabs=list(tpl.known_tabs),
                    vis_baseline=tpl.vis_baseline)
                    if tpl is not None else ChatBrowserState())
                states[k] = st
        return st

    @contextlib.contextmanager
    def chat_scope(self, chat_id):
        """Контекст браузера чата chat_id на время блока (ход бота, поток
        листания). Пустой ключ ничего не меняет."""
        k = _chat_key(chat_id)
        if not k:
            yield
            return
        tok = _CC_CHAT.set(k)
        try:
            yield
        finally:
            _CC_CHAT.reset(tok)

    def _init_vis_baseline(self, opened: Optional[str]) -> None:
        """Бот открыл/переключил вкладку (opened — её URL или хост) — база
        _follow_visible_tab: что пользователь видит сейчас. Открытие и
        переключение поднимают вкладку (focus=True), так что видимая — она;
        браузер не спрашиваем (лишний вызов на каждое открытие). Ставится
        ВСЕМ чатам: видимая вкладка сменилась действием бота, а не человеком,
        — команда другого чата не примет это за ручное переключение."""
        h = str(opened or "").strip()
        host = (urlparse(h).hostname if "://" in h
                else h.split("/")[0]) or ""
        host = host.lower()
        if not _user_page_host(host):
            return
        with self._state_lock():
            for st in list(self._chat_states().values()):
                st.vis_baseline = host
        self._st().vis_baseline = host

    # ── Контекст открытой страницы (диск) ─────────────────

    def _save_last_page(self, url: Optional[str] = None):
        """Страница текущего чата → last_tab.json: после перезапуска каждый
        чат помнит свой сайт и базу видимой вкладки. id вкладки не сохраняем —
        между процессами он не стабилен, вкладка находится заново по хосту.
        Служебные хосты веб-чатов не пишем: они не рабочая страница
        пользователя, а попап-детект их уже фильтрует (страховка на диске)."""
        try:
            st = self._st()
            if not st.last_host:
                return
            from app.features import browser_actions as _ba
            if _ba.is_service_host(st.last_host):
                return
            if url:
                st.last_url = url
            st.ts = time.time()
            self._write_last_tabs(st)
        except Exception as e:
            logger.debug(f"[CompControl] last_tab.json не записан: {e}")

    def _write_last_tabs(self, latest: ChatBrowserState) -> None:
        """{"chats": {ключ: {host, url, vis, ts}}, host/url/ts — последняя
        запись (справочно: умолчанием для чатов читается только старый
        формат без chats). URL — без токенов/фрагмента (правила аудита);
        свежие LAST_TAB_MAX_CHATS чатов."""
        from app.features import browser_actions as _ba
        from app.features.cc_privacy import scrub_url
        with self._state_lock():
            items = sorted(
                ((k, s) for k, s in self._chat_states().items()
                 if k and s.last_host and not _ba.is_service_host(s.last_host)),
                key=lambda kv: kv[1].ts, reverse=True)[:LAST_TAB_MAX_CHATS]
            data = {"chats": {k: {"host": s.last_host,
                                  "url": scrub_url(s.last_url or ""),
                                  "vis": s.vis_baseline or "", "ts": s.ts}
                              for k, s in items},
                    "host": latest.last_host,
                    "url": scrub_url(latest.last_url or ""), "ts": latest.ts}
            path = self.base_dir / "last_tab.json"
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(path)

    def _restore_last_page(self):
        """last_tab.json → контекст страниц чатов. Старый формат (один хост
        на персону, без chats) — умолчание для всех чатов (шаблон "")."""
        try:
            data = json.loads(
                (self.base_dir / "last_tab.json").read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(data, dict):
            return
        from app.features import browser_actions as _ba

        def _ok(host: str) -> bool:
            # Грязный контекст прошлых запусков: служебная вкладка веб-чата
            # LLM — не рабочая страница пользователя
            return bool(host) and not _ba.is_service_host(host)

        chats = data.get("chats")
        if isinstance(chats, dict):
            states = self._chat_states()
            best = None
            for k, rec in chats.items():
                key = _chat_key(k)
                host = (str(rec.get("host") or "").strip()
                        if isinstance(rec, dict) else "")
                if not key or not _ok(host):
                    continue
                try:
                    ts = float(rec.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0.0
                states[key] = ChatBrowserState(
                    last_host=host, last_url=str(rec.get("url") or "") or None,
                    vis_baseline=str(rec.get("vis") or "") or None, ts=ts)
                if best is None or ts >= states[best].ts:
                    best = key
            if best:
                # Вызовы вне хода (тесты, служебные пути) — к свежему чату
                self.__dict__["_last_chat"] = best
                logger.info(f"[CompControl] Контекст страниц с диска: "
                            f"{len(states) - ('' in states)} чат(ов)")
            return
        host = str(data.get("host") or "").strip()
        if _ok(host):
            tpl = self._st("")
            tpl.last_host = host
            tpl.last_url = str(data.get("url") or "") or None
            logger.info(f"[CompControl] Контекст страницы с диска (старый "
                        f"формат, умолчание для чатов): {host}")

    def update_config(self, config: Optional[dict]):
        """(Пере)прочитать конфиг: allowlist'ы и confirm применяются на живую
        (правки из веб-настроек — без перезапуска). Pending-подтверждения,
        статистика и аудит-лог сохраняются."""
        cfg = config if isinstance(config, dict) else {}
        self.confirm: bool = bool(cfg.get("confirm", True))
        # Агентный клик «нажми X» — отдельный под-переключатель: клики можно
        # выключить, оставив открытие сайтов/приложений и поиск на сайте
        self.click: bool = bool(cfg.get("click", True))
        # Поисковик резолва «открой X» без алиаса/истории: google (дефолт) —
        # веб-выдача Google в пуле H (точнее на узких запросах; пул H не
        # поднят/капча — DDG) или ddg. Неизвестное значение — google
        engine = str(cfg.get("site_search") or "google").strip().lower()
        self.site_search: str = engine if engine in ("ddg", "google") else "google"
        # Визуальный фолбэк резолва: скриншот вьюпорта + vision-модель
        # для иконочных UI, где текстовый скоринг бессилен
        self.vision_fallback: bool = bool(cfg.get("vision_fallback", True))
        # Широкий LLM-резолв (zero-match): скоринг не дал ни одного кандидата
        # (слова пользователя не совпали с подписями на странице) — LLM
        # выбирает элемент из компактного списка снапшота; дешевле vision
        self.llm_wide_resolve: bool = bool(cfg.get("llm_wide_resolve", True))
        # Режим zero-match яруса: hybrid — один vision-вызов видит и рамки
        # на скриншоте, и текстовый список элементов вне экрана (_hybrid_pick;
        # без vision — та же пара ярусов раздельно); text — раздельными
        # шагами: широкий текстовый резолв, затем vision-рамки. Неизвестное — hybrid.
        # В hybrid «другое название» («почта» при «Электронная почта») решает
        # vision_provider (vision-цепочка роутера), а не cc_provider, как в
        # text; cc_provider остаётся запасным — когда vision лежит или
        # ответил невалидно
        wm = str(cfg.get("wide_mode", "hybrid")).strip().lower()
        self.wide_mode: str = wm if wm in ("hybrid", "text") else "hybrid"
        # Общий бюджет каскада резолва элемента (снапшоты, доскролл, LLM и
        # vision-ярусы вместе): за ним оставшиеся ярусы не зовутся, ответ —
        # честный отказ, а не минута тишины
        try:
            self.resolve_budget_sec: float = max(
                1.0, float(cfg.get("resolve_budget_sec", 25)))
        except (TypeError, ValueError):
            self.resolve_budget_sec = 25.0
        # Подтверждение по типу действия (риск), поверх общего confirm:
        # {click: false, navigate_known_domain: false, navigate_new_domain: true,
        #  type_text: true, type_text_safe_fields: false, download: true}
        # Ключа нет — действует общий confirm. Логика — needs_confirm()
        self.risk_overrides: Dict[str, bool] = {
            str(k).strip(): bool(v)
            for k, v in (cfg.get("risk_overrides") or {}).items()}
        self.allow_domains: List[str] = [
            str(d).strip().lower() for d in (cfg.get("allow_domains") or []) if str(d).strip()]
        # Приватные страницы (cc_privacy.is_private_page): скриншоты и текст
        # страницы не уходят облачным/веб-чат моделям — vision-ярусы
        # пропускаются, LLM-выбор только локальной моделью или по скорингу.
        # Плюс встроенные признаки входа/оплаты (bank/pay/login/id.…),
        # private_hosts_builtin: false — только явный список
        self.private_hosts: List[str] = [
            str(d).strip().lower() for d in (cfg.get("private_hosts") or [])
            if str(d).strip()]
        self.private_hosts_builtin: bool = bool(
            cfg.get("private_hosts_builtin", True))
        self.apps: Dict[str, object] = {
            str(k).strip().lower(): v for k, v in (cfg.get("apps") or {}).items()}
        self.tasks: Dict[str, object] = {
            str(k).strip().lower(): v for k, v in (cfg.get("tasks") or {}).items()}
        # Алиасы сайтов (короткое слово → полный URL): поисковый резолв
        # ненадёжен для брендовых сайтов — поисковик не всегда отдаёт нужный
        # сайт в топе, к тому же медленнее — личные частые сайты описываются
        # здесь, мгновенно и детерминированно
        self.sites: Dict[str, str] = {}
        for k, v in (cfg.get("sites") or {}).items():
            url = self._normalize_url(str(v))
            if url:
                self.sites[str(k).strip().lower()] = url
        # Поисковые шаблоны «включи X на сайте»: ключ — слово сайта (как в
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

    # ── Метрики выбора элемента ────────────────

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
            "vision_calls": s.get("vision_calls", 0),
        }

    # ── Конфиг → промпт ──────────────────────────────────

    def available_apps(self) -> List[str]:
        return sorted(self.apps)

    def available_tasks(self) -> List[str]:
        return sorted(self.tasks)

    def instruction_block(self, lang: Optional[str] = None) -> str:
        """Инструкция о маркерах для system_prompt (когда фича включена).
        На английском, язык ответа — строкой языка пользователя в конце
        (lang — язык диалога, detect_dialogue_language; None — нейтральная
        строка). Прямое «у тебя ЕСТЬ доступ» гасит шаблонный отказ «нет
        доступа к ОС»."""
        domain_rule = ""
        if self.allow_domains:
            domain_rule = f" Only these domains are allowed: {', '.join(self.allow_domains)}."
        apps = ", ".join(self.available_apps()) or "(not configured)"
        tasks = ", ".join(self.available_tasks()) or "(not configured)"
        # Номерные результаты («второй результат», «третье видео») и
        # «следующее видео» — встроенные рецепты (_build_action), не
        # перечислены в tasks ключами (yaml персоны их знать не обязан) —
        # сообщаем модели, что такие ключи валидны, независимо от того, есть
        # ли в tasks персоны свои recipe-записи. Ключи — русские: их ждёт
        # _build_action
        ordinal_note = ("  Numbered search results are also available: keys like "
                        "«второй результат», «третье видео» (1–10), and «следующее "
                        "видео» (next video) — as RUN_TASK.\n")
        # Маркерное действие всегда ждёт «да» (process_markers), а вопрос
        # задаёт система шаблоном — модель его не формулирует и не
        # объявляет действие сделанным
        flow = (
            "The action is NOT executed immediately: the system itself appends a "
            "fixed confirmation question and waits for the user's yes. Do not ask "
            "for confirmation yourself and never say it is already done — just "
            "put the marker at the very end of your reply."
        )
        example = "Sure. [OPEN_URL:youtube.com]"
        block = (
            "[COMPUTER CONTROL — system capability]\n"
            "You DO HAVE access to the user's computer: you can open sites, "
            "launch applications and run named tasks. Never claim "
            "that you have no such access — the system performs the action from your marker.\n"
            "When the user EXPLICITLY asks to open or launch something, add ONE "
            "marker at the very end of the reply:\n"
            "  [OPEN_URL:https://example.com] — open a site (http/https only)."
            f"{domain_rule}\n"
            f"  [OPEN_APP:key] — launch an application. Available: {apps}\n"
            f"  [RUN_TASK:key] — run a task. Available: {tasks}\n"
            f"{ordinal_note}"
            f"{flow}\n"
            "Example. User: \"open youtube\" / «открой ютуб».\n"
            f"Your reply (in the user's language): {example}\n"
            "Markers are used only at the user's explicit request, never on "
            "your own initiative. Only the listed application and task keys exist, "
            "do not invent others. The marker is hidden from the user; without a marker nothing "
            "happens. If you do not know the EXACT address of the requested site — do NOT "
            "put a marker and do not invent a URL: reply with text and ask which site to open. "
            "Clicks on page elements and typing text into fields are performed by a separate "
            "system from the user's exact phrases (\"click X\", \"type X into field Y\") — "
            "you have NO such markers: never write \"Clicked\"/\"Typed\" on your own "
            "and do not describe the result of such actions — that is a lie, without a system "
            "action nothing happens. Scrolling the page (\"scroll the page\") "
            "and stopping it (\"stop\") are also system commands, not your markers."
        )
        # Контекст открытой страницы: чтобы модель понимала «мы на сайте X»,
        # а не отвечала в отрыве от браузерного контекста
        if self._last_host:
            # URL в system prompt (часто облачный провайдер): без токенов,
            # фрагмента и секретных параметров; приватной страницы — только хост
            from app.features.cc_privacy import scrub_url
            page_url = (scrub_url(self._last_url) if self._last_url
                        and not self.is_private_page(self._last_url) else "")
            block += (f"\nThe page I currently have open: {self._last_host}"
                      + (f" ({page_url})" if page_url else "")
                      + ". Requests to click/type/download without an explicit site "
                        "name refer to it.")
        return block + "\n" + user_language_line(lang)

    # ── Маркеры ──────────────────────────────────────────

    @_in_chat
    def process_markers(self, answer: str, chat_id: str, user_id=None,
                        untrusted: bool = False,
                        user_text: Optional[str] = None,
                        lang: Optional[str] = None
                        ) -> Tuple[str, List[str]]:
        """Срезает маркеры из ответа. Возвращает (чистый текст, уведомления
        пользователю). Маркер пишет модель, а она видит недоверенный текст —
        поэтому маркерное действие ВСЕГДА уходит в pending (origin="marker"),
        а вопрос — шаблон confirm_question, не слова модели (её фраза перед
        маркером вырезается: инъекция не подменит, ЧТО спрашивается).
        untrusted — в ходе был чужой текст (страница, веб-выдача, OCR/файл,
        цитата): маркеры тогда отбрасываются целиком, если сам человек
        (user_text — то, что он написал) не просил открыть/запустить.
        Обычный ответ модели делает старый pending этого пользователя
        недействительным — он снимается.
        lang — язык уведомлений и вопроса (None — язык хода, set_turn)."""
        from app.features import cc_texts
        lang = lang or self.turn_lang()
        notices: List[str] = []
        try:
            self.clear_pending(chat_id, user_id)
        except Exception:
            pass
        if not answer:
            return answer, notices
        matches = list(MARKER_RE.finditer(answer))
        if not matches:
            return answer, notices
        # Фраза модели прямо перед маркером («Открыть YouTube?») — её вопрос;
        # спрашиваем шаблоном, поэтому её убираем вместе с маркером
        clean = _MARKER_LEAD_RE.sub("", answer)
        clean = MARKER_RE.sub("", clean)
        clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
        if untrusted and not _MARKER_CMD_RE.search(str(user_text or "")):
            self.stats["rejected"] += len(matches)
            logger.warning(
                f"[CompControl] Маркеры отброшены: в ходе недоверенный текст, "
                f"а пользователь не просил действия "
                f"({', '.join(self._marker_log(m) for m in matches)})")
            return clean, notices
        passed: List[dict] = []
        for m in matches:
            self.stats["markers"] += 1
            kind = _KIND_BY_MARKER[m.group(1)]
            target = m.group(2).strip()
            action = self._build_action(kind, target)
            if action is None:
                self.stats["rejected"] += 1
                logger.info(f"[CompControl] Маркер отклонён allowlist'ом: "
                            f"{self._marker_log(m)}")
                notices.append(cc_texts.t("marker_not_allowed", lang,
                                          target=target[:60]))
                continue
            _pu = urlparse(str(action.get("value") or "")) \
                if action.get("kind") == "url" else None
            if _pu is not None and (_pu.query or _pu.fragment) \
                    and not _MARKER_CMD_RE.search(str(user_text or "")):
                # Адрес с данными в query/fragment — канал утечки
                # (?d=<телефон> из чужой реплики в истории): такой маркер —
                # недоверенный, без глагола человека в этой реплике не идёт
                self.stats["rejected"] += 1
                logger.warning(f"[CompControl] Маркер с параметрами в адресе "
                               f"без команды человека отброшен: "
                               f"{self._marker_log(m)}")
                _nt = cc_texts.t("marker_url_params", lang)
                if _nt not in notices:
                    notices.append(_nt)
                continue
            action["origin"] = "marker"
            passed.append(action)
        # Несколько маркеров в одном ответе — ОДНО действие multi (например,
        # «открой сайт А и сайт Б»): раздельная обработка через set_pending
        # затёрла бы предыдущий маркер следующим, и «да» исполняло бы только
        # последний, а в immediate-режиме действия шли бы вразнобой
        # отдельными вызовами
        accepted: Optional[dict] = None
        if len(passed) == 1:
            accepted = passed[0]
        elif passed:
            accepted = {"kind": "multi", "items": passed, "origin": "marker"}
        if accepted is not None:
            # needs_confirm для origin="marker" — всегда True: исполнения
            # сразу по маркеру нет ни при каком confirm/risk_overrides
            self.set_pending(chat_id, accepted, user_id=user_id)
            logger.info(f"[CompControl] Ожидаю подтверждения: "
                        f"{self._describe_log(accepted)}")
            q = self.confirm_question(accepted, lang=lang)
            clean = f"{clean}\n\n{q}" if clean else q
        return clean, notices

    @staticmethod
    def _marker_log(m) -> str:
        # Маркер для лога: URL — без токенов/фрагмента (scrub_url), прочее —
        # redact_inline (email/карты/телефоны/токены маской)
        from app.features.cc_privacy import redact_inline, scrub_url
        target = m.group(2).strip()
        if m.group(1) == "OPEN_URL":
            if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target):
                target = scrub_url(target)
            else:
                # «evil.com/cb?token=…» без схемы — чистим как https
                target = scrub_url("https://" + target)[len("https://"):]
        return f"{m.group(1)}:{redact_inline(target, 80)}"

    def _build_action(self, kind: str, target: str) -> Optional[dict]:
        # Валидация по allowlist'ам. None — маркер отклонён.
        if kind == "url":
            alias = self.sites.get(target.strip().lower())
            url = alias or self._normalize_url(target)
            if url is None or (alias is None and not self._domain_allowed(url)):
                return None
            return {"kind": "url", "value": url}
        table = self.apps if kind == "app" else self.tasks
        value = self._resolve_platform(table.get(target.strip().lower()))
        if value is None and kind == "task":
            # «N-ый результат/видео» / «следующее видео» — встроенные
            # рецепты без явного ключа в yaml
            oc = ordinal_recipe(target) or next_video_recipe(target)
            if oc:
                value = f"recipe:{oc}"
        if value is None:
            return None
        return {"kind": kind, "key": target.strip().lower(), "value": value}

    @staticmethod
    def _resolve_platform(entry) -> Optional[str]:
        # Значение allowlist'а: строка (одна на все ОС) или dict per-OS.
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

    def _known_domain(self, url: str) -> bool:
        """Домен явно настроен у пользователя: алиас в sites или запись в
        allow_domains. Открытие по алиасу — известный домен; домен из
        поисковой выдачи — новый."""
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        known = list(self.allow_domains)
        for u in self.sites.values():
            h = (urlparse(u).hostname or "").lower()
            if h:
                known.append(h)
        return any(host == d or host.endswith("." + d) for d in known)

    def needs_confirm(self, action: Optional[dict]) -> bool:
        """Нужно ли подтверждение в чате перед исполнением действия.
        База — общий confirm; risk_overrides переопределяют по типу действия
        (ключа нет — действует confirm). Ввод в чувствительное поле
        (пароль/email/tel — флаг sn снапшота) требует подтверждения всегда,
        переопределить нельзя."""
        if not isinstance(action, dict):
            return self.confirm
        if action.get("force_confirm"):
            # Вызывающий требует подтверждения независимо от политики
            # (цель-задача из LLM-яруса): risk_overrides не отменяют
            return True
        kind = action.get("kind")
        if kind == "multi":
            return any(self.needs_confirm(a) for a in action.get("items") or [])
        if action.get("origin") == "marker":
            # Маркер пишет модель, а она видит недоверенный текст (страница,
            # веб-выдача, OCR, файлы) — prompt injection не должен исполнять
            # действия без «да» человека, какими бы ни были confirm/overrides
            return True
        if action.get("via_search"):
            # Адрес выбрал поисковик, а не пользователь/конфиг/история:
            # «открой душу» → случайный домен из выдачи — только после «да»
            return True
        if self.risky_label(action):
            # Оплата / финальный коммит заказа / отправка / удаление / выход
            # из аккаунта — необратимо, risk_overrides.click это не отменяет
            return True
        if kind == "nav" and any(
                self.risky_label({"kind": "click", "element": str(s),
                                  "host": action.get("host")})
                for s in action.get("steps") or []):
            # Шаг маршрута назван рискованным («… - Оформить заказ») —
            # спрашиваем до открытия; найденный элемент шага ещё раз сверит
            # гейт маршрута (_nav_gate)
            return True
        ov = self.risk_overrides
        if kind == "tab_switch":
            # Переключение вкладки — обратимо и ничего не активирует; одно
            # правило для regex- и LLM-пути разбора
            return False
        if kind == "scroll_stop":
            # Остановка СВОЕГО же листания — не действие на странице, а отбой
            # своей команды: в режиме с подтверждением «стоп» уходил в pending,
            # а повторное «стоп» читалось как отказ — остановить было нельзя
            return False
        if kind in ("click", "hover", "slider", "scroll", "media_vol"):
            if kind == "click" and action.get("point"):
                # Координатный клик зонального vision-фолбэка: сигнал «что
                # именно нажали» слабее — подтверждение всегда
                return True
            # hover — даже по точке: наведение ничего не активирует
            return bool(ov.get("click", self.confirm))
        if kind in ("key", "press"):
            # Пробел/стрелки/Escape — обратимое взаимодействие со страницей;
            # Enter/Tab/Backspace могут отправить форму — остаются на confirm
            if kind == "press" or str(action.get("key") or "") in _RISK_SAFE_KEYS:
                return bool(ov.get("click", self.confirm))
            return self.confirm
        if kind == "type":
            if action.get("field_sensitive"):
                return True
            # Ввод + Enter (submit) — уже не «текст в поле»: форма уходит на
            # сервер (заказ, сообщение, комментарий). Послабление для
            # безопасных полей (поиск) на него не распространяется
            key = ("type_text_safe_fields"
                   if action.get("field_safe") and not action.get("submit")
                   else "type_text")
            return bool(ov.get(key, ov.get("type_text", self.confirm)))
        if kind == "download":
            return bool(ov.get("download", self.confirm))
        if kind == "zoom":
            # Зум — чисто обратимая вьюшная настройка, ничего не активирует
            return False
        if kind in ("url", "nav"):
            known = self._known_domain(str(action.get("value") or ""))
            key = "navigate_known_domain" if known else "navigate_new_domain"
            return bool(ov.get(key, self.confirm))
        return self.confirm

    @staticmethod
    def risky_label(action: Optional[dict]) -> Optional[str]:
        """Необратимый элемент по подписи → 'payment' | 'commit' |
        'destructive' | None. Для click/type/key/press/send: подпись —
        element (+ aria/title, если резолвер их положил). Разрушительность —
        только классы delete/leave: «Закрыть» диалог — рутина, а «Удалить
        аккаунт»/«Выйти» — нет."""
        if not isinstance(action, dict) or action.get("kind") not in (
                "click", "type", "key", "press", "send"):
            return None
        labels = [_risk_text(action.get(k)) for k in ("element", "aria", "title")]
        labels = [s for s in labels if s.strip() and not s.startswith("#")]
        if not labels:
            return None
        if any(_is_payment(s) for s in labels):
            return "payment"
        if (_MONEY_HOST_RE.search(str(action.get("host") or ""))
                and any(_BARE_TRANSFER_RE.match(s) for s in labels)):
            # Голое «Перевод» в банке — перевод денег, а не текста
            return "payment"
        if action.get("kind") == "type":
            # Для ввода подпись — имя поля: «Удалить…»/«Отправить» у поля
            # не бывает, а коммит заказа вводом не делается
            return None
        if any(_label_purchase(s) for s in labels):
            # «Купить за 299 ₽», «Subscribe — $9.99/mo», «Renew subscription»
            return "payment"
        if "task" in (action.get("origin"), action.get("pending_from")) \
                and any(_INSTANT_BUY_RE.search(s) for s in labels):
            # Мгновенная покупка («Buy now», «Купить в 1 клик», «Place your
            # order») для агента — оплата: её делает человек, одно «да» на
            # клик агента списало бы деньги с сохранённой карты
            return "payment"
        if any(_COMMIT_RE.search(s) or _label_submit(s) for s in labels):
            return "commit"
        for s in labels:
            # Глагол удаления/выхода — по началу, после «Да,/Yes,/Навсегда»
            # и узким словарём в любом месте подписи
            if _label_destructive(s) or _ACCOUNT_CLOSE_RE.search(s):
                return "destructive"
        return None

    # ── Гейт подтверждения (инвариант исполнения) ─────────

    @classmethod
    def confirm_reason(cls, action: Optional[dict]) -> Optional[str]:
        """Почему действие исполняется только с токеном подтверждения
        (одна из GATE_RISKS) или None. multi — причина первого такого
        пункта; nav — None: маршрут сверяет каждый найденный элемент сам
        (_nav_gate). Риск по подписи — первым: сценарий по нему решает,
        спросить или передать оплату человеку."""
        if not isinstance(action, dict):
            return None
        kind = action.get("kind")
        if kind == "multi":
            for a in action.get("items") or []:
                r = cls.confirm_reason(a)
                if r:
                    return r
            return None
        if kind == "nav":
            return None
        if "task" in (action.get("origin"), action.get("pending_from")) \
                and checkout_step_label(action):
            # Агент задач идёт к оформлению («К оформлению заказа»): не
            # коммит — «да» на переход и потом на сам заказ спрашивали дважды
            risk = None
        else:
            risk = cls.risky_label(action)
        if risk:
            return risk
        if "task" in (action.get("origin"), action.get("pending_from")) \
                and kind == "click":
            # «ОК»/«Да» в окне сайта — по тексту окна (агент кладёт его в
            # context): страховка, если вызывающий спросить забыл
            risk = dialog_risk(action.get("element"), action.get("context"),
                               in_dialog=bool(action.get("in_dialog")))
            if risk:
                return risk
        ch = action.get("choose")
        if action.get("label_unverified") or (
                isinstance(ch, dict) and ch.get("label_unverified")):
            return "label_unverified"
        if action.get("force_confirm"):
            return "force_confirm"
        if kind == "click" and action.get("point"):
            return "point"
        if kind == "type" and action.get("field_sensitive"):
            return "sensitive_field"
        if "marker" in (action.get("origin"), action.get("pending_from")):
            return "marker"
        if action.get("via_search"):
            return "via_search"
        if kind in ("key", "press") \
                and str(action.get("key") or "") in _SUBMIT_KEYS \
                and action.get("origin") not in _USER_CMD_ORIGINS \
                and not action.get("search_enter"):
            # Enter/Space/Tab могут отправить форму или нажать кнопку в
            # фокусе: не от явной команды человека — только с «да» (раньше
            # защита была лишь в агенте). search_enter — код агента: Enter
            # сразу после ввода в строго поисковое поле
            return "commit"
        if kind in ("app", "task") and action.get("origin") == "task" \
                and not str(action.get("value") or "").startswith("recipe:"):
            # Алиас приложения/команды из конфига, выбранный моделью агента
            # задач, — запуск программы на компьютере человека: только с «да»
            return "force_confirm"
        if (kind == "send" or (kind == "type" and action.get("submit")
                               and not action.get("field_safe"))) \
                and action.get("origin") not in _USER_CMD_ORIGINS:
            # Отправка (Enter/submit) необратима: сценарий, агент, маркер и
            # действие без источника — только с «да». Явная команда
            # пользователя («отправь») — как решит политика needs_confirm
            return "commit"
        return None

    @staticmethod
    def is_confirmed(action: Optional[dict]) -> bool:
        return isinstance(action, dict) and isinstance(
            action.get("confirmed"), _ConfirmToken)

    @staticmethod
    def grant_confirmation(action: Optional[dict], via: str, by=None):
        """Токен подтверждения. Звать ТОЛЬКО из потока подтверждения: «да»
        владельца на pending, «да» автора задачи агенту, «да» шагу сценария
        (или ответ человека на слот сценария для этого же поля).
        nav-продолжение (gate_label) подтверждает ровно показанную подпись
        своего первого шага."""
        if not isinstance(action, dict):
            return action
        steps = None
        if action.get("kind") == "nav" and action.get("gate_label"):
            steps = {0: _gate_label_norm(action["gate_label"])}
        action["confirmed"] = _ConfirmToken(via, by=by, steps=steps)
        return action

    def _confirm_gate(self, action: dict) -> None:
        """Единая точка инварианта (зовёт _execute_locked до _dispatch): без
        токена рискованное действие не исполняется — NeedsConfirm, а в
        action["confirm_required"] — что спросить."""
        reason = self.confirm_reason(action)
        if not reason or self.is_confirmed(action):
            return
        target = action
        if action.get("kind") == "multi":
            target = next((a for a in action.get("items") or []
                           if self.confirm_reason(a)), action)
        label = str(target.get("element") or target.get("aria")
                    or target.get("title") or "")
        info = {"reason": reason, "label": label[:80],
                "kind": target.get("kind")}
        action["confirm_required"] = info
        raise NeedsConfirm(f"нужно подтверждение ({reason})", info)

    def _nav_gate(self, action: dict, step_i: int, step: str,
                  item: Optional[dict], meta: Optional[dict],
                  ctx: dict) -> None:
        """Гейт клика шага маршрута — по ФАКТИЧЕСКИ найденному элементу:
        «Корзина → Продолжить» может привести к «Продолжить и оплатить».
        Подтверждение маршрута покрывает рискованный шаг, только если
        человек видел в вопросе шаг того же класса риска («… → Оформить
        заказ») или nav-продолжение подтвердило ровно эту подпись. Иначе —
        стоп маршрута: NeedsConfirm с пройденными шагами и тем, что спросить."""
        it = item if isinstance(item, dict) else {}
        host = ctx.get("host") or action.get("host")
        probe: Dict[str, object] = {"kind": "click", "host": host}
        for k, dst in (("text", "element"), ("aria", "aria"),
                       ("title", "title")):
            v = " ".join(str(it.get(k) or "").split())
            if v:
                probe[dst] = v[:80]
        reason = self.risky_label(probe)
        m = meta if isinstance(meta, dict) else {}
        if not reason and m.get("force_confirm"):
            reason = ("label_unverified" if m.get("label_unverified")
                      else "force_confirm")
        if not reason and m.get("point"):
            reason = "point"
        if not reason:
            return
        label = str(probe.get("element") or probe.get("aria")
                    or probe.get("title") or step)[:80]
        tok = action.get("confirmed")
        if isinstance(tok, _ConfirmToken):
            if step_i in tok.steps:
                if tok.steps[step_i] == _gate_label_norm(label):
                    return
            elif reason in ("payment", "commit", "destructive") and \
                    self.risky_label({"kind": "click", "element": str(step),
                                      "host": host}) == reason:
                return
        steps = list(action.get("steps") or [])
        info = {"reason": reason, "label": label, "kind": "nav",
                "step_i": step_i, "step": str(step),
                "done": list(ctx.get("done") or []),
                "rest": steps[step_i:], "tab_id": ctx.get("tab_id"),
                "host": host, "url": ctx.get("url") or action.get("value"),
                "idx": ctx.get("idx")}
        action["confirm_required"] = info
        from app.features.cc_privacy import redact_inline
        logger.info(f"[CompControl] Навигация: шаг «{redact_inline(step, 40)}» "
                    f"→ «{redact_inline(label, 40)}» ({reason}) — стоп "
                    "маршрута до подтверждения")
        raise NeedsConfirm(f"шаг «{step[:40]}»: нужно подтверждение "
                           f"({reason})", info)

    # Поля исполнения, которые в pending-копию не переносятся
    _GATE_VOLATILE = ("confirm_required", "confirmed", "duration_ms",
                      "_result")

    def gate_followup(self, action: Optional[dict],
                      lang: Optional[str] = None
                      ) -> Optional[Tuple[dict, str]]:
        """Отказ гейта → (действие для pending, вопрос) или None — отказа не
        было. Простое действие — оно же без служебных полей; маршрут —
        продолжение с рискованного шага на той же вкладке, в вопросе —
        подпись, которая реально нашлась. Оплата спрашивается, как у
        обычного клика (явная команда человека); агент и сценарий передают
        её человеку сами."""
        info = action.get("confirm_required") if isinstance(action, dict) \
            else None
        if not isinstance(info, dict):
            return None
        from app.features import cc_texts
        if info.get("kind") == "nav":
            rest = [str(s) for s in info.get("rest") or []]
            cont: Dict[str, object] = {
                "kind": "nav", "value": info.get("url") or action.get("value"),
                "host": info.get("host") or action.get("host"),
                "steps": rest, "resume_tab": info.get("tab_id"),
                "gate_label": info.get("label"),
                "gate_reason": info.get("reason")}
            for k in ("origin", "rest_steps", "chain_site"):
                if action.get(k) is not None:
                    cont[k] = action[k]
            done = [str(s) for s in info.get("done") or []]
            q = cc_texts.t(
                "gate_nav_question", lang,
                done=(cc_texts.t("gate_nav_done", lang,
                                 steps=" → ".join(done)) if done else ""),
                host=cont.get("host") or "", label=info.get("label") or "",
                risk=cc_texts.gate_risk(info.get("reason"), lang),
                tail=(cc_texts.t("gate_nav_tail", lang,
                                 steps=" → ".join(rest[1:]))
                      if len(rest) > 1 else ""))
            return cont, q
        pend = {k: v for k, v in action.items()
                if k not in self._GATE_VOLATILE}
        return pend, self.confirm_question(pend, lang=lang)

    # ── Pending-подтверждение ────────────────────────────
    # Pending — на чат, но «да» принимается только от того, кто просил
    # (user_id): в группе чужое «да» не должно исполнять чужое действие.
    # Кто просит в этом ходе — note_requester() в начале хода; явный
    # user_id в set_pending важнее.

    def note_requester(self, chat_id: str, user_id) -> None:
        # Автор текущего хода в чате: им подписывается новый pending
        with self._lock:
            self.__dict__.setdefault("_requesters", {})[str(chat_id)] = (
                str(user_id) if user_id is not None else None)

    def current_requester(self, chat_id) -> Optional[str]:
        # Автор текущего хода чата (note_requester) — владелец подтверждений
        # шагов сценария; None — неизвестен
        with self._lock:
            return self.__dict__.get("_requesters", {}).get(str(chat_id))

    def set_pending(self, chat_id: str, action: dict, user_id=None):
        with self._lock:
            if user_id is None:
                user_id = self.__dict__.get("_requesters", {}).get(str(chat_id))
            ttl = (TASK_START_TTL_SEC if is_goal_task(action)
                   else CHOICE_TTL_SEC if action.get("choices")
                   else PENDING_TTL_SEC)
            self._pending[str(chat_id)] = {
                "action": action,
                "expires_at": time.time() + ttl,
                "user_id": str(user_id) if user_id is not None else None}
            self.__dict__.setdefault("_pending_expired", {}).pop(
                str(chat_id), None)

    @staticmethod
    def _pending_owner_ok(entry: dict, user_id) -> bool:
        # Владелец не записан (старый вызов) или не спрашиваем — пропускаем
        owner = entry.get("user_id")
        return owner is None or user_id is None or owner == str(user_id)

    def get_pending(self, chat_id: str, user_id=None) -> Optional[dict]:
        """Живой pending чата. user_id — кто отвечает: pending другого
        участника ему не виден (None). Протухший по TTL снимается и
        запоминается для pending_expired_recently."""
        with self._lock:
            entry = self._pending.get(str(chat_id))
            if not entry:
                return None
            if time.time() > entry["expires_at"]:
                self._pending.pop(str(chat_id), None)
                self.__dict__.setdefault("_pending_expired", {})[str(chat_id)] = (
                    time.time(), entry.get("user_id"))
                return None
            if not self._pending_owner_ok(entry, user_id):
                return None
            return dict(entry["action"])

    def pending_expired_recently(self, chat_id: str, user_id=None) -> bool:
        """Pending этого пользователя протух по TTL недавно — голое «да»
        вдогонку заслуживает «подтверждение истекло». Флаг одноразовый."""
        with self._lock:
            exp = self.__dict__.setdefault("_pending_expired", {})
            rec = exp.get(str(chat_id))
            if not rec:
                return False
            ts, owner = rec
            if time.time() - ts > PENDING_EXPIRED_GRACE_SEC:
                exp.pop(str(chat_id), None)
                return False
            if not self._pending_owner_ok({"user_id": owner}, user_id):
                return False
            exp.pop(str(chat_id), None)
            return True

    def clear_pending(self, chat_id: str, user_id=None):
        """Снять pending чата. user_id — снимает только свой (реплика
        другого участника группы не сбрасывает чужое ожидание)."""
        with self._lock:
            entry = self._pending.get(str(chat_id))
            if entry and not self._pending_owner_ok(entry, user_id):
                return
            self._pending.pop(str(chat_id), None)
            self.__dict__.setdefault("_pending_expired", {}).pop(
                str(chat_id), None)

    # ── Очистка диалога (/api/chat/clear, app/api/memory_wipe.py) ────

    def forget_chat(self, chat_id) -> dict:
        """«Очистить диалог»: бот забывает, что просили и где он был в этом
        чате — pending, листание, контекст страницы (память и last_tab.json),
        записи аудита (из них сценарий «запиши, что я делал» собирает
        трассу). → срез для корзины (forget_chat_files). Браузер не
        трогаем: открытые вкладки — пользователя, не память бота."""
        ck = _chat_key(chat_id)
        if not ck:
            return {}
        try:
            self.stop_scroll_if_active(None, chat_id=ck)
        except Exception as e:
            logger.debug(f"[CompControl] листание при очистке: {e}")
        with self._lock:
            self._pending.pop(ck, None)
            for name in ("_pending_expired", "_requesters"):
                self.__dict__.get(name, {}).pop(ck, None)
        self._stop_chats().discard(ck)
        with self._state_lock():
            self._chat_states().pop(ck, None)
            if self.__dict__.get("_last_chat") == ck:
                self.__dict__.pop("_last_chat", None)
            out = forget_chat_files(self.base_dir, ck)
            if out.get("last_tab_legacy"):
                # Старый формат был умолчанием для всех чатов — шаблон тоже
                self._chat_states().pop("", None)
        return out

    def restore_chat(self, chat_id, data: dict) -> None:
        """Отмена очистки диалога: срез forget_chat — обратно (файлы и
        контекст страницы чата, если с тех пор чат не открыл новую)."""
        ck = _chat_key(chat_id)
        if not ck or not data:
            return
        with self._state_lock():
            restore_chat_files(self.base_dir, ck, data)
            rec = data.get("last_tab")
            host = str((rec or {}).get("host") or "").strip()
            cur = self._chat_states().get(ck)
            if host and (cur is None or not cur.last_host):
                self._chat_states()[ck] = ChatBrowserState(
                    last_host=host, last_url=str(rec.get("url") or "") or None,
                    vis_baseline=str(rec.get("vis") or "") or None,
                    ts=float(rec.get("ts") or 0))

    @_in_chat
    def stop_scroll_if_active(self, text: Optional[str] = None,
                              chat_id=None) -> bool:
        """«стоп»/«хватит» при живом pending — отказ И остановка листания.
        text — реплика человека: листание гасим, только если в ней есть
        стоп-фраза (голое «нет» на чужой вопрос страницу не останавливает).
        Гасится листание чата chat_id (пусто — чата хода): «стоп» другого
        чата чужую страницу не останавливает."""
        # Вежливость («stop scrolling please») — как у parse_scroll_request
        if text is not None and not any(
                _SCROLL_STOP_RE.match(_strip_polite(c))
                for c in re.split(r"[,.;!…]+", str(text))):
            return False
        try:
            if self._scroll_active():
                self._scroll_stop_now()
                return True
        except Exception as e:
            logger.debug(f"[CompControl] остановка листания не удалась: {e}")
        return False

    # «стоп» до лока хода: бот ставит флаг, долгие циклы (шаги навигации,
    # доскролл до цели) проверяют его между шагами. Флаг живёт до начала
    # следующего хода этого чата (stop_clear) — иначе «стоп», пришедший,
    # пока ход ещё думал (LLM), терялся бы до старта исполнения
    def _stop_chats(self) -> set:
        return self.__dict__.setdefault("_stop_chat_set", set())

    # Чей execute идёт и В КАКОМ ПОТОКЕ: любая запись _exec_chat (execute,
    # тест) привязывает его к писавшему потоку — ход другого чата в своём
    # потоке чужой исполняемый чат за свой «стоп» не примет
    @property
    def _exec_chat(self) -> Optional[str]:
        return self.__dict__.get("_exec_chat")

    @_exec_chat.setter
    def _exec_chat(self, value) -> None:
        self.__dict__["_exec_chat"] = value
        self.__dict__["_exec_thread"] = (threading.get_ident() if value
                                         else None)

    def _stop_key(self, chat_id=None) -> str:
        """ЕДИНСТВЕННОЕ место, где решается, чей «стоп» проверять: явный
        чат (след str(None)/пусто — не ключ, _chat_key) → чат, чей execute
        идёт В ЭТОМ потоке → ход этого потока (set_turn). Через него идут
        stop_requested/_sleep_or_stop/_stop_check/_raise_if_stopped — забытый
        путь не может взять чужой ключ или «None»."""
        k = _chat_key(chat_id)
        if k:
            return k
        d = self.__dict__
        if d.get("_exec_thread") == threading.get_ident():
            k = _chat_key(d.get("_exec_chat"))
            if k:
                return k
        return _chat_key(self.turn_key())

    def request_stop(self, chat_id) -> None:
        k = _chat_key(chat_id)
        if k:
            self._stop_chats().add(k)

    def stop_clear(self, chat_id) -> None:
        self._stop_chats().discard(_chat_key(chat_id))

    def stop_requested(self, chat_id=None) -> bool:
        chats = self._stop_chats()
        if not chats:
            return False
        key = self._stop_key(chat_id)
        return bool(key) and key in chats

    # Ход, который сейчас идёт в этом потоке: ключ режима (chat_id, у веб-чата
    # без chat_id — user_id, как в cc_turn_enter/request_stop) и язык. Бот
    # ставит его в начале каждого хода: execute/опросы без chat_id берут
    # «стоп» по этому ключу, служебные тексты — на языке хода
    def _turn_tl(self):
        return self.__dict__.setdefault("_turn_local", threading.local())

    def set_turn(self, key, lang: Optional[str] = None) -> None:
        tl = self._turn_tl()
        tl.key = str(key or "") or None
        tl.lang = lang
        # Опросы браузера этого потока (wait_dom_idle, доскролл, ожидание
        # вкладки) видят «стоп» хода и вне execute: резолв, сценарий, агент
        try:
            from app.features import browser_actions as _ba
            _ba._STOP_TL.check = (self._stop_check(tl.key) if tl.key
                                  else None)
        except Exception:
            pass

    def turn_key(self) -> Optional[str]:
        return getattr(self._turn_tl(), "key", None)

    def turn_lang(self) -> Optional[str]:
        return getattr(self._turn_tl(), "lang", None)

    def _tx(self, key: str, **values) -> str:
        """Отказ/подсказка резолвера на языке хода — единственный источник
        этих текстов (cc_texts): русский литерал в ответе английского хода —
        баг. Страж — AST-скан резолверов в test_cc_state."""
        from app.features import cc_texts
        return cc_texts.t(key, self.turn_lang(), **values)

    def _qt(self, s) -> str:
        # Цитата в перечне (вкладки, поля) — кавычками языка хода
        from app.features import cc_texts
        return f"\"{s}\"" if cc_texts.is_en(self.turn_lang()) else f"«{s}»"

    def _stop_check(self, chat_id=None):
        # Дешёвая проверка «стоп» для опросов browser_actions — по ключу,
        # зафиксированному сейчас в потоке вызова (поток воркера своего хода
        # не знает)
        key = self._stop_key(chat_id)
        return lambda: bool(key) and self.stop_requested(key)

    def executing_for(self, chat_id) -> bool:
        # Идёт ли сейчас исполнение действия этого чата (execute под локом)
        k = _chat_key(chat_id)
        return bool(k) and _chat_key(self._exec_chat) == k

    def _raise_if_stopped(self, chat_id=None) -> None:
        if self.stop_requested(chat_id):
            from app.features import cc_texts
            raise RuntimeError(cc_texts.t("stopped_by_user", self.turn_lang()))

    def _sleep_or_stop(self, sec: float, chat_id=None) -> bool:
        # Пауза опроса; True — пришёл «стоп». Флагов нет ни у кого — один
        # обычный слип (как раньше), иначе кусками по 0.1 с
        if not self._stop_chats():
            time.sleep(sec)
            return self.stop_requested(chat_id)
        end = time.time() + max(0.0, float(sec))
        while True:
            if self.stop_requested(chat_id):
                return True
            left = end - time.time()
            if left <= 0:
                return False
            time.sleep(min(0.1, left))

    # ── Исполнение ───────────────────────────────────────

    @staticmethod
    def describe(action: dict, lang: Optional[str] = None) -> str:
        # lang="en" — английская ветка (cc_texts), иначе русский шаблон
        from app.features import cc_texts
        if cc_texts.is_en(lang):
            return cc_texts.describe_en(action, "do")
        if action["kind"] == "multi":
            return " и ".join(
                ComputerControlManager.describe(a) for a in action["items"])
        if action["kind"] == "nav" and action.get("gate_label"):
            # Продолжение маршрута после «да» на рискованный шаг
            return (f"продолжить на {action.get('host', '')}: "
                    f"{' → '.join(action.get('steps', []))}")
        if action["kind"] == "nav":
            return (f"открыть {action.get('host', '')} и пройти: "
                    f"{' → '.join(action.get('steps', []))}")
        if action["kind"] == "download":
            return f"скачать «{action.get('element', '')}» с {action.get('host', '')}"
        if action["kind"] == "click":
            return f"нажать «{action.get('element', '')}» на {action.get('host', '')}"
        if action["kind"] == "hover":
            return (f"навести курсор на «{action.get('element', '')}» "
                    f"на {action.get('host', '')}")
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
            if m == "erase":
                n = int(action.get("times") or 1)
                return (f"удалить {n} {_chars_ru(n)} "
                        f"на {action.get('host', '')}")
            if m == "vol_down":
                return f"уменьшить громкость на {action.get('host', '')}"
            if m == "vol_up":
                return f"увеличить громкость на {action.get('host', '')}"
            if m == "mute":
                return f"выключить звук на {action.get('host', '')} (m)"
            if m == "unmute":
                return f"включить звук на {action.get('host', '')} (m)"
            if m == "toggle":
                return (f"поставить на паузу/продолжить "
                        f"на {action.get('host', '')}")
            return (f"нажать {_KEY_RU.get(action.get('key'), action.get('key'))}"
                    f" на {action.get('host', '')}")
        if action["kind"] == "slider":
            _su = _SLIDER_UNIT_RU.get(str(action.get("slider_unit") or ""),
                                      "")
            return (f"перетащить слайдер «{action.get('slider_label', '')}» "
                    f"на {action.get('slider_value', '')}{_su} "
                    f"на {action.get('host', '')}")
        if action["kind"] == "media_vol":
            op = str(action.get("op") or "")
            if op == "toggle":
                return f"переключить воспроизведение на {action.get('host', '')}"
            if op == "mute":
                return f"выключить звук на {action.get('host', '')}"
            if op == "unmute":
                return f"включить звук на {action.get('host', '')}"
            what = "убавить" if op.startswith("-") else "прибавить"
            return f"{what} громкость видео на {action.get('host', '')}"
        if action["kind"] == "scroll":
            _what = "страницу"
            if action.get("container"):
                _what = f"«{action['container']}»"
            elif action.get("side"):
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
        if action["kind"] == "zoom":
            what = {"in": "увеличить", "out": "уменьшить"}.get(
                action.get("dir"), "сбросить")
            return f"{what} масштаб страницы на {action.get('host', '')}"
        if action["kind"] == "tab_op":
            op = action.get("op")
            name = action.get("element") or action.get("host") or ""
            if op in ("back", "forward"):
                where = (f" на вкладке «{name}»" if name
                         else " на текущей вкладке")
                return (("вернуться назад" if op == "back"
                         else "перейти вперёд") + where)
            what = f"вкладку «{name}»" if name else "текущую вкладку"
            verb = ("обновить" if action.get("op") == "reload"
                    else "закрыть")
            return f"{verb} {what}"
        if action["kind"] == "cart":
            op_ru = {"remove": "убрать", "decrease": "убавить",
                     "increase": "прибавить", "edit": "изменить"}.get(
                action.get("op"), "изменить")
            return (f"{op_ru} «{action.get('product', '')}» "
                    f"в корзине на {action.get('host', '')}")
        if action["kind"] == "comp_edit":
            prod = action.get("product", "")
            return (f"изменить состав «{prod}» на {action.get('host', '')}"
                    if prod
                    else f"открыть редактор состава на {action.get('host', '')}")
        if action["kind"] == "url":
            return f"открыть {action['value']}"
        if action["kind"] == "app":
            return f"запустить приложение «{action.get('key', '')}»"
        return f"выполнить задачу «{action.get('key') or action['kind']}»"

    @staticmethod
    def _host(action: dict) -> str:
        p = urlparse(action["value"])
        host = (p.hostname or action["value"]).lower().removeprefix("www.")
        # Значимый сегмент пути показываем: «Открыть example.com/maps?»,
        # а не «Открыть example.com?» (последний: путь может начинаться
        # со служебных /intl/ru/…)
        seg = next((s for s in reversed(p.path.split("/")) if s), "")
        return f"{host}/{seg}" if seg else host

    @staticmethod
    def _url_params_note(action: dict, lang: Optional[str] = None) -> str:
        """Адрес с query/fragment: в вопросе — сам адрес (без токенов,
        scrub_url), иначе «Открыть evil.example/c?» прячет, что в
        ?d=+7999… уходят данные."""
        if action.get("kind") != "url" or action.get("search_query"):
            return ""
        v = str(action.get("value") or "")
        p = urlparse(v)
        if not (p.query or p.fragment):
            return ""
        from app.features import cc_texts
        from app.features.cc_privacy import scrub_url
        return "\n" + cc_texts.t("url_params_note", lang,
                                 url=cc_texts.clip(scrub_url(v), 200))

    @classmethod
    def confirm_question(cls, action: dict, lang: Optional[str] = None) -> str:
        # Шаблон вопроса на подтверждение для fast-path (без LLM).
        from app.features import cc_texts
        if action.get("kind") == "url" and len(action.get("choices") or ()) >= 2:
            return cls._choices_question(action, lang)
        if cc_texts.is_en(lang):
            return (cc_texts.describe_en(action, "ask", host_fn=cls._host)
                    + cls._url_params_note(action, lang))
        if action["kind"] == "multi":
            q = " и ".join(cls.describe(a) for a in action["items"])
            return q[0].upper() + q[1:] + "?"
        if action["kind"] == "nav" and action.get("gate_label"):
            return (f"Продолжить на {action.get('host', '')}: "
                    f"{' → '.join(action.get('steps', []))}?")
        if action["kind"] == "nav":
            return (f"Открыть {action.get('host', '')} и пройти: "
                    f"{' → '.join(action.get('steps', []))}?")
        if action["kind"] == "download":
            q = f"Скачать «{action.get('element', '')}» с {action.get('host', '')}?"
            # Целевой URL файла — часть сути риска («что именно скачивается»)
            u = str(action.get("url") or "")
            return f"{q}\n{u}" if u else q
        if action["kind"] == "click":
            return f"Нажать «{action.get('element', '')}» на {action.get('host', '')}?"
        if action["kind"] == "hover":
            return (f"Навести курсор на «{action.get('element', '')}» "
                    f"на {action.get('host', '')}?")
        if action["kind"] == "type":
            tail = " и отправить" if action.get("submit") else ""
            # Человек подтверждает, что именно уйдёт в поле, — до ~200
            # символов, длиннее — с многоточием
            _typed = cc_texts.clip(action.get("text"), cc_texts.TYPE_PREVIEW_MAX)
            return (f"Ввести «{_typed}» в поле "
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
            if m in ("vol_down", "vol_up", "mute", "unmute", "toggle",
                     "erase"):
                q = cls.describe(action)
                return q[0].upper() + q[1:] + "?"
            return (f"Нажать "
                    f"{_KEY_RU.get(action.get('key'), action.get('key'))} "
                    f"на {action.get('host', '')}?")
        if action["kind"] == "slider":
            _su = _SLIDER_UNIT_RU.get(str(action.get("slider_unit") or ""),
                                      "")
            return (f"Перетащить слайдер «{action.get('slider_label', '')}» "
                    f"на {action.get('slider_value', '')}{_su} "
                    f"на {action.get('host', '')}?")
        if action["kind"] == "media_vol":
            op = str(action.get("op") or "")
            if op == "toggle":
                return f"Переключить воспроизведение на {action.get('host', '')}?"
            if op == "mute":
                return f"Выключить звук на {action.get('host', '')}?"
            if op == "unmute":
                return f"Включить звук на {action.get('host', '')}?"
            what = "Убавить" if op.startswith("-") else "Прибавить"
            return f"{what} громкость видео на {action.get('host', '')}?"
        if action["kind"] == "scroll":
            _what = "страницу"
            if action.get("container"):
                _what = f"«{action['container']}»"
            elif action.get("side"):
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
        if action["kind"] == "tab_op":
            q = cls.describe(action)
            return q[0].upper() + q[1:] + "?"
        if action["kind"] in ("cart", "comp_edit"):
            q = cls.describe(action)
            return q[0].upper() + q[1:] + "?"
        if action["kind"] == "url" and action.get("search_query"):
            if action.get("direct"):
                return f"Открыть «{action['search_query']}» на {action['search_site']}?"
            return f"Найти «{action['search_query']}» на {action['search_site']}?"
        if action["kind"] == "url":
            return (f"Открыть {cls._host(action)}?"
                    + cls._url_params_note(action, lang))
        if action["kind"] == "app":
            return f"Запустить «{action.get('key', '')}»?"
        return f"Выполнить задачу «{action.get('key') or action['kind']}»?"

    @staticmethod
    def _choices_question(action: dict, lang: Optional[str] = None) -> str:
        """«Какой сайт открыть?» — нумерованные варианты: заголовок из
        выдачи и адрес (без схемы и секретных параметров, scrub_url)."""
        from app.features import cc_texts
        from urllib.parse import unquote
        from app.features.cc_privacy import scrub_url
        lines = []
        for i, c in enumerate(action.get("choices") or (), 1):
            addr = re.sub(r"^https?://(www\.)?", "",
                          unquote(scrub_url(c.get("url"))))
            addr = cc_texts.clip(addr.rstrip("/"), 90)
            title = cc_texts.clip(" ".join(str(c.get("title") or "").split()), 70)
            lines.append(f"{i}. {title} — {addr}" if title else f"{i}. {addr}")
        return cc_texts.t("site_choices", lang, items="\n".join(lines))

    @classmethod
    def describe_done(cls, action: dict, lang: Optional[str] = None) -> str:
        # «Готово, …» — прошедшее время для шаблонного подтверждения.
        from app.features import cc_texts
        if cc_texts.is_en(lang):
            return cc_texts.describe_en(action, "done", host_fn=cls._host)
        if action["kind"] == "multi":
            return ", ".join(cls.describe_done(a) for a in action["items"])
        if action["kind"] == "nav":
            steps = action.get("steps", [])
            if action.get("gate_label"):
                return (f"прошёл до «{steps[-1] if steps else ''}» "
                        f"на {action.get('host', '')}")
            return (f"открыл {action.get('host', '')} и прошёл до "
                    f"«{steps[-1] if steps else ''}»")
        if action["kind"] == "download":
            return f"скачал «{action.get('element', '')}» с {action.get('host', '')}"
        if action["kind"] == "click":
            return f"нажал «{action.get('element', '')}» на {action.get('host', '')}"
        if action["kind"] == "hover":
            return (f"навёл курсор на «{action.get('element', '')}» "
                    f"на {action.get('host', '')}")
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
            if m == "erase":
                n = int(action.get("times") or 1)
                return (f"удалил {n} {_chars_ru(n)} "
                        f"на {action.get('host', '')}")
            if m == "vol_down":
                return f"уменьшил громкость на {action.get('host', '')}"
            if m == "vol_up":
                return f"увеличил громкость на {action.get('host', '')}"
            if m == "mute":
                return (f"выключил звук на {action.get('host', '')} "
                        "(m — переключатель)")
            if m == "unmute":
                return (f"включил звук на {action.get('host', '')} "
                        "(m — переключатель)")
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
        if action["kind"] == "media_vol":
            got_v = str(action.get("vol_done") or "")
            if got_v.startswith("vol:"):
                return (f"выставил громкость {got_v[4:]}% "
                        f"на {action.get('host', '')}")
            if got_v == "paused":
                return f"поставил видео на паузу на {action.get('host', '')}"
            if got_v == "playing":
                return f"продолжил воспроизведение на {action.get('host', '')}"
            if got_v == "muted":
                return f"выключил звук на {action.get('host', '')}"
            if got_v == "unmuted":
                return f"включил звук на {action.get('host', '')}"
            return f"изменил громкость на {action.get('host', '')}"
        if action["kind"] == "scroll":
            _what = "страницу"
            if action.get("container"):
                _what = f"«{action['container']}»"
            elif action.get("side"):
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
            if reason == "timeout":
                return "остановил прокрутку — листание и так уже выдохлось"
            return "остановил прокрутку"
        if action["kind"] == "tab_switch":
            return (f"переключился на вкладку "
                    f"«{action.get('element') or action.get('host', '')}»")
        if action["kind"] == "zoom":
            got = int(action.get("zoom_done") or 0)
            if got:
                return f"выставил масштаб {got}% на {action.get('host', '')}"
            what = {"in": "увеличил", "out": "уменьшил"}.get(
                action.get("dir"), "сбросил")
            return f"{what} масштаб на {action.get('host', '')}"
        if action["kind"] == "tab_op":
            op = action.get("op")
            name = action.get("element") or action.get("host") or ""
            if op in ("back", "forward"):
                where = (f" на вкладке «{name}»" if name
                         else " на текущей вкладке")
                return (("вернулся назад" if op == "back"
                         else "перешёл вперёд") + where)
            what = f"вкладку «{name}»" if name else "текущую вкладку"
            verb = ("обновил" if action.get("op") == "reload"
                    else "закрыл")
            return f"{verb} {what}"
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
        if action["kind"] == "comp_edit":
            prod = action.get("product", "")
            return (f"открыл редактирование состава «{prod}»" if prod
                    else "открыл редактирование состава")
        if action["kind"] == "url" and action.get("search_query"):
            if action.get("direct"):
                return f"открыл «{action['search_query']}» на {action['search_site']}"
            return f"открыл поиск «{action['search_query']}» на {action['search_site']}"
        if action["kind"] == "url":
            return f"открыл {cls._host(action)}"
        if action["kind"] == "read":
            what = ("страницу" if action.get("mode") == "page"
                    else "последнее сообщение")
            return f"прочитал {what} на {action.get('host', '')}"
        if action["kind"] == "app":
            return f"запустил «{action.get('key', '')}»"
        return f"выполнил задачу «{action.get('key') or action['kind']}»"

    # ── Резолв для fast-path «открой X» ──────────────────

    @staticmethod
    def _lookup(table: Dict[str, object], key: str) -> Optional[str]:
        """Ключ таблицы: точное совпадение, иначе по основе слова
        («музыку» → «музыка», «на сайте» → «сайт»)."""
        if key in table:
            return key
        from app.features.web_search import _stem
        sk = _stem(key)
        if len(sk) < 4:
            return None
        return next((k for k in table if _stem(k) == sk), None)

    def resolve(self, name: str, web_search=True) -> Optional[dict]:
        """Слово места → действие: allowlist apps/tasks → алиасы sites →
        домен с точкой → история браузера → лёгкий поисковый резолв сайта.
        None — пусть разбирает LLM-путь.
        web_search: True — поисковый резолв разрешён; False — только
        алиасы/история/явный домен; "auto" (fast-path) — поиск лишь для
        бренд-подобных латинских имён («figma»): «открой душу», «включи
        свет» — обычные слова, поисковик дал бы случайный домен за 6-15 с,
        их решает LLM-ярус. Адрес от поисковика помечен via_search."""
        name = str(name or "").strip(_TARGET_EDGE_CHARS)
        if not name:
            return None
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
        # search_pick), «следующее видео» — recipe youtube_next: встроенные
        # рецепты без явного ключа в yaml
        oc = ordinal_recipe(key) or next_video_recipe(key)
        if oc:
            return {"kind": "task", "key": key, "value": f"recipe:{oc}"}
        if _looks_like_domain(key):
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
        if web_search is False or (web_search == "auto"
                                   and not _brand_like_name(key)):
            return None
        try:
            from app.features.web_search import find_site_url
            url = find_site_url(name, engine=getattr(self, "site_search", "google"))
        except Exception as e:
            logger.debug(f"[CompControl] Резолв сайта не удался: {e}")
            url = None
        if url and self._domain_allowed(url):
            # Поисковый резолв — единственный путь, где адрес не подтверждён
            # ни автором конфига (алиас), ни прошлыми визитами (история):
            # пометка для мягкой верификации title после навигации
            # via_search — адрес от поисковика: needs_confirm всегда спросит
            act = {"kind": "url", "value": url, "expect_name": name,
                   "via_search": True}
            choices = self._site_choices(name, url)
            if len(choices) >= 2:
                # Вопрос — нумерованный список: человек выбирает номером
                act["choices"] = choices
            return act
        return None

    def _site_choices(self, name: str, best: str) -> List[dict]:
        """Варианты списка «какой сайт открыть?»: выбранный резолвом адрес
        первым, дальше — остальная выдача того же поиска (без дублей по
        хост+путь и без доменов вне allow_domains), до SITE_CHOICES_MAX."""
        try:
            from app.features.web_search import site_choices
            found = site_choices(name)
        except Exception as e:
            logger.debug(f"[CompControl] Варианты резолва недоступны: {e}")
            return []

        def _key(u: str) -> Tuple[str, str]:
            p = urlparse(u)
            return ((p.hostname or "").lower().removeprefix("www."),
                    p.path.rstrip("/"))
        title = next((t for u, t in found if _key(u) == _key(best)), "")
        out, seen = [{"url": best, "title": title}], {_key(best)}
        for u, t in found:
            if len(out) >= SITE_CHOICES_MAX:
                break
            k = _key(u)
            if k in seen or not self._domain_allowed(u):
                continue
            seen.add(k)
            out.append({"url": u, "title": t})
        return out

    @staticmethod
    def pick_choice(action: dict, idx: int) -> dict:
        """Ответ на список вариантов: адрес варианта idx (с 0) становится
        адресом действия, список снимается. Номер — в аудит (choice)."""
        choices = action.pop("choices", None) or []
        if 0 <= idx < len(choices):
            action["value"] = choices[idx]["url"]
            action["choice"] = idx + 1
        return action

    def resolve_url(self, token: str) -> Optional[dict]:
        """Явный адрес из фразы («example.com/827») → url-действие.
        None — на адрес не похоже («открой config.py» — файл, а не сайт:
        _URL_TOKEN_RE считает адресом любое «слово.слово») либо адрес не
        прошёл нормализацию/whitelist доменов."""
        if not _looks_like_domain(token):
            return None
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

    def is_known_target(self, name: str) -> bool:
        """Цель открытия известна без поиска: алиас сайта/приложения/задачи,
        поисковый шаблон, имя хоста алиаса («youtube» у youtube.com) или
        явный домен. Для parse_open_many: «X and Y» делим, только если
        известны обе части («Barnes and Noble» — одно название)."""
        key = " ".join(str(name or "").strip(_TARGET_EDGE_CHARS)
                       .lower().split())
        if not key:
            return False
        for table in (self.apps, self.tasks, self.sites, self.search_urls):
            if self._lookup(table, key) is not None:
                return True
        if _looks_like_domain(key):
            return True
        for u in self.sites.values():
            host = (urlparse(u).hostname or "").lower()
            labels = host.split(".")
            if key in labels[:-1] and key not in ("www", "m"):
                return True
        return False

    def resolve_many(self, names: List[str], web_search="auto"
                     ) -> Optional[dict]:
        """«сайт А и сайт Б» → multi-действие. Резолвятся должны ВСЕ цели,
        иначе None — сообщение целиком уходит в LLM-путь. web_search — как
        у resolve; по умолчанию "auto" (fast-path regex-лесенки: обычные
        слова поисковиком не угадываем). LLM-ярус передаёт True."""
        actions = []
        for n in names:
            a = self.resolve(n, web_search=web_search)
            if a is None:
                return None
            actions.append(a)
        if not actions:
            return None
        if len(actions) == 1:
            return actions[0]
        for a in actions:
            # Вопрос о нескольких сайтах — одной строкой, без списков
            # вариантов: «да» открывает лучший адрес каждого
            a.pop("choices", None)
        multi = {"kind": "multi", "items": actions}
        if any(a.get("via_search") for a in actions):
            multi["via_search"] = True
        return multi

    @_in_chat
    def resolve_intent_llm(self, text: str, router, chat_id: str = ""
                           ) -> Tuple[Optional[dict], Optional[str]]:
        """Последний ярус разбора команды в режиме управления: ни один
        regex-парсер не сматчился — LLM классифицирует фразу в JSON-действие
        (протокол intent_prompt), дальше обычные резолверы и тот же
        confirm/allowlist. (None, None) — не команда / LLM недоступна: фраза
        уходит в обычный диалог. (None, причина) — команда распознана, но
        исполнить не вышло: честный отказ, а не «сыгранный» успех."""
        if router is None:
            return None, None
        try:
            resp = router.get_response(
                [{"role": "user", "content": intent_prompt(text)}],
                temperature=0.0, max_tokens=100, top_p=0.1,
                # Внутренний разбор — в side-чат веб-чата: служебные промпты
                # не замусоривают тред беседы с пользователем
                webchat_channel="cc", force_provider=getattr(router, "cc_provider", None))
        except Exception as e:
            logger.debug(f"[CompControl] LLM-разбор команды недоступен: {e}")
            return None, None
        act = parse_intent_action(resp)
        if not act or act["action"] == "none":
            return None, None
        self.stats["llm_intent"] = self.stats.get("llm_intent", 0) + 1
        # Текст команды ввода («введи пароль …») в лог не пишем — только
        # длину; остальное — без секретоподобных фрагментов
        from app.features.cc_privacy import redact_inline
        shown = (f"{len(text)} симв." if act["action"] == "type"
                 else redact_inline(text, 50))
        logger.info(f"[CompControl] LLM-разбор: «{shown}» → "
                    f"{act['action']}")
        # origin="intent_llm" ставит вызывающий (лесенка бота) — tag_origin
        pseudo = intent_pseudo_action(act)
        if pseudo is not None:
            return pseudo, None
        return self.intent_to_action(act, router, chat_id=chat_id)

    @_in_chat
    def intent_to_action(self, act: dict, router, chat_id: str = ""
                         ) -> Tuple[Optional[dict], Optional[str]]:
        """Разобранный LLM-ответ (parse_intent_action) → действие теми же
        резолверами, что у regex-пути (псевдо-действия бота — см.
        intent_pseudo_action)."""
        kind = act["action"]
        site = act.get("site")
        try:
            if kind == "zoom":
                return self.resolve_zoom(str(act["direction"]), site,
                                         chat_id=chat_id)
            if kind == "slider":
                return self.resolve_slider(
                    (str(act["goal"]), int(act["value"]),
                     str(act.get("unit") or "")),
                    site, router, chat_id=chat_id)
            if kind == "cart":
                return self.resolve_cart((str(act["op"]),
                                          str(act["product"])),
                                         site, router, chat_id=chat_id)
            if kind == "click":
                return self.resolve_click(str(act["goal"]), site, router,
                                          chat_id=chat_id)
            if kind == "hover":
                return self.resolve_hover(str(act["goal"]), site, router,
                                          chat_id=chat_id)
            if kind == "close":
                return self.resolve_click(f"закрой {act['goal']}", site,
                                          router, chat_id=chat_id)
            if kind == "download":
                return self.resolve_download(str(act["goal"]), site, router,
                                             chat_id=chat_id)
            if kind == "type":
                body = (f"{act['text']} в поле {act['field']}"
                        if act.get("field") else str(act["text"]))
                return self.resolve_type(body, site, router, chat_id=chat_id)
            if kind == "open":
                return self.resolve_many([str(act["target"])],
                                         web_search=True), None
            if kind == "search":
                return self.resolve_search(str(act["query"]),
                                           str(act["site"])), None
            if kind == "scroll":
                mode: object = "stop" if act.get("stop") else (
                    "start", act.get("side"), act.get("direction"))
                return self.resolve_scroll(mode, site, router, chat_id=chat_id)
            if kind == "key":
                key_goal = ((act["key"], int(act["times"]), None)
                            if act.get("times") else act["key"])
                return self.resolve_key(key_goal, site, router,
                                        chat_id=chat_id)
            if kind == "send":
                return self.resolve_send(None, site, router, chat_id=chat_id)
            if kind == "read":
                return self.resolve_read(str(act["mode"]), site,
                                         chat_id=chat_id)
            if kind == "switch_tab":
                return self.resolve_tab_switch(str(act["goal"]), True,
                                               chat_id=chat_id)
            if kind in ("reload_tab", "close_tab", "back", "forward"):
                tab_goal = act.get("goal") or site or None
                _tab_ops = {"reload_tab": "reload", "close_tab": "close",
                            "back": "back", "forward": "forward"}
                return self.resolve_tab_op(
                    str(tab_goal) if tab_goal else None,
                    _tab_ops[kind], router, chat_id=chat_id)
        except Exception as e:
            # Команда РАСПОЗНАНА, сорвалось исполнение — «не команда»
            # возвращать нельзя: фраза ушла бы в обычный диалог, и модель
            # «изобразила» бы успех. Честная причина
            logger.warning(f"[CompControl] LLM-разбор: резолвер «{kind}» "
                           f"не удался: {e}")
            return None, self._tx("rs_intent_failed", kind=kind, detail=e)
        return None, None

    def resolve_search(self, query: str, site_word: str,
                       direct: bool = True) -> Optional[dict]:
        """Поиск на сайте («фильм», «стриминге») → url по шаблону из
        `search`. Сайт-реципиент матчится по основе слова («стриминге» →
        «стриминг»). Нет шаблона — None (путь LLM).
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

    # ── Скоринг кандидатов и выбор элемента ──────

    @staticmethod
    def _score_candidates(items: List[dict], goal: str,
                          host: Optional[str] = None, op: str = "click"
                          ) -> List[Tuple[float, dict]]:
        """Скоринг вместо бинарного substring-матча: точный текст > точный
        aria-label/title > совпадение по основам слов > слова, разделённые
        текстом и контекстом > частичная подстрока > опечаточное совпадение
        слов (fuzzy) > голый контекст; штрафы за крошечный размер, позицию
        вне вьюпорта и позднее место в DOM-порядке. host — для хост-зависимых
        синонимов (_goal_synonyms: бургер — «Гид» на YouTube, «Меню» на
        остальных сайтах).
        Разрушительные контролы при цели без такого намерения в скоринг не
        попадают вовсе (_destructive_mismatch) — это вход инварианта вето:
        иначе «Очистить очередь» (70.0) обходил «Очередь просмотра» (69.5)
        лидером по баллам или номером 1 в списке для LLM.
        → [(score, item)] по убыванию, только score > 0."""
        from app.features.web_search import _stem
        g = _norm_match(goal)
        g_words = [w for w in re.findall(r"[a-z0-9а-яё]+", g) if len(w) >= 3]
        scored: List[Tuple[float, dict]] = []
        for pos, it in enumerate(items):
            if _destructive_mismatch(goal, it, op):
                continue
            text = _norm_match(it.get("text"))
            aria = _norm_match(it.get("aria"))
            title = _norm_match(it.get("title"))
            # tid (data-testid) — единственный крюк безтекстовых иконок
            # (бургер MobileHeader.BurgerButton); латиница с точками — слова
            # матчатся с их начала (_word_in), как стемы
            tid = _norm_match(it.get("tid"))
            hay = _strip_negated(f"{text} {aria} {title} {tid}", g)
            ctx = _strip_negated(_norm_match(it.get("ctx")), g)
            strong = [bool(_word_in(w, hay) or _word_in(_stem(w), hay)
                           or any(_word_in(s, hay)
                                  for s in _goal_synonyms(w, host)))
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
                                for s in _goal_synonyms(w, host))
                            for w in g_words):
                # Составная цель с иконкой-синонимом («крестик у джема» →
                # кнопка «Закрыть»): уточняющего слова в тексте кнопки нет,
                # all(strong) не сходится — но совпадение по СИНОНИМУ иконки
                # сильнее контекста (40): без него такой крестик не нашёлся
                # бы вовсе (zero-match → LLM-вето)
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
                # на карточке товара. Слабый ярус: выигрывает, лишь когда
                # текстовых совпадений нет вовсе. Скоуп-цели («выбрать на
                # Цезарь», «омлет сырный справа») сюда не пускаем — их
                # разруливает _score_scoped
                s = 40.0
            else:
                continue
            if any(_word_in(nw, hay) and not _negated_word_in(nw, hay)
                   for nw in _NEG_WORD_RE.findall(g)):
                # Цель с отрицанием («не нравится»), а у кандидата то же
                # слово без «не» — противоположное действие, пропускаем
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
                # Внешняя ссылка уводит со страницы (например, футерная
                # ссылка на внешний документ) — on-page контрол важнее
                s -= 15.0
            if it.get("cov"):
                # Центр элемента перекрыт чужим фиксированным слоем (карточка
                # каталога под открытым попапом): клик туда физически не
                # дойдёт. Основной барьер — _active_layer в выборе; штраф —
                # страховка для путей, которые скорят без фильтра слоя
                # (goal_sole, _element_on_other_pages); когда перекрыто всё —
                # равномерен и порядка не меняет
                s -= 30.0
            s -= min(pos, 20) * 0.5  # штраф за позднюю позицию в DOM
            scored.append((s, it))
        scored.sort(key=lambda x: -x[0])
        return scored

    @staticmethod
    def _score_scoped(items: List[dict], goal: str,
                      op: str = "click") -> List[Tuple[float, dict]]:
        """Фолбэк для «выбрать на Цезарь с беконом»: слова действия — в тексте
        самого элемента (кнопка «Выбрать»), слова скопа — в контексте предка
        (поле ctx: текст карточки). Шкала и штрафы — как у _score_candidates,
        +бонус за совпавший скоп, чтобы скоуп-лидер отрывался от мусора.
        Пространственный скоп «в левой/правой части (панели, разделе)» —
        фильтр по ПОЗИЦИИ элемента (центр в своей половине вьюпорта, поля
        x/vw из снапшота), а не по тексту карточки.
        Разрушительные контролы отсеиваются на входе — как в
        _score_candidates (вход инварианта вето)."""
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
            if _destructive_mismatch(goal, it, op):
                continue
            text = _norm_match(it.get("text"))
            aria = _norm_match(it.get("aria"))
            title = _norm_match(it.get("title"))
            hay = _strip_negated(f"{text} {aria} {title}", goal)
            ctx = _strip_negated(_norm_match(it.get("ctx")), goal)
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
            if any(_word_in(nw, hay) and not _negated_word_in(nw, hay)
                   for nw in _NEG_WORD_RE.findall(goal)):
                # Скоуп-цель с отрицанием — как в _score_candidates
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

    def _veto_destructive(self, goal: str, item: Optional[dict],
                          meta: Optional[dict], where: str = "",
                          op: str = "click") -> bool:
        """Единая финальная проверка инварианта: наружу (в клик) не уходит
        контрол закрытия/удаления, если в цели такого намерения нет. Копий по
        веткам каскада больше нет — ветки зовут эту точку, а фильтр скоринга
        до неё обычно и не доводит. Пишет причину в аудит-мету (meta["veto"])
        и лог. → True — клик ветирован."""
        if not item or not _destructive_mismatch(goal, item, op):
            return False
        lab = str(item.get("text") or item.get("aria")
                  or item.get("title") or "")
        if isinstance(meta, dict):
            meta["veto"] = "destructive"
            meta["path"] = "none"
        logger.info(f"[CompControl] Выбор «{self._label_for_log(lab, limit=30)}» "
                    f"ветирован (деструктивный без запроса) для «{goal[:40]}»"
                    + (f" [{where}]" if where else ""))
        return True

    def _veto_model_pick(self, goal: str, item: Optional[dict],
                         meta: Optional[dict], host: Optional[str],
                         where: str, op: str = "click",
                         label_check: bool = True) -> bool:
        """Единая проверка кандидата, выбранного ПО НОМЕРУ моделью (vision по
        скриншоту, зональный vision, широкий LLM-резолв): разрушительный
        контрол без намерения в цели ИЛИ галлюцинация номера — у кандидата
        читаемая подпись, а ни одного слова цели (стема/синонима) в ней нет.
        Безымянные иконки/зоны не трогаем: сверять не с чем, ровно для них
        визуальные ярусы и существуют.
        True — кандидата не берём; ветка обязана вернуть None и уступить
        СЛЕДУЮЩЕМУ ярусу каскада (единое поведение «следующий кандидат»), а
        не отказывать сразу.
        label_check=False — только для широкого ТЕКСТОВОГО LLM-резолва: он и
        существует ради «другого названия» («почта» при «Электронная почта»),
        там несовпадение подписи — норма, а не галлюцинация."""
        if self._veto_destructive(goal, item, meta, where, op=op):
            return True
        if not label_check or not item:
            return False
        lab = str(item.get("text") or item.get("aria")
                  or item.get("title") or "")
        chk = _label_goal_check(goal, lab, host, item.get("ctx")) \
            if lab.strip() else "match"
        if chk == "unverified":
            # Сверять нечем (номер/иконка/закрытие) или совпал только
            # контекст блока: не вето, но клик — после «да» человека
            if isinstance(meta, dict):
                meta["force_confirm"] = True
                meta["label_unverified"] = True
            logger.info(f"[CompControl] Выбор «{self._label_for_log(lab, host)}» "
                        f"для «{goal[:40]}» "
                        "подписью не подтверждён — спрошу подтверждение"
                        + (f" [{where}]" if where else ""))
            return False
        if chk == "match":
            return False
        logger.info(f"[CompControl] Выбор «{self._label_for_log(lab, host)}» "
                    f"ветирован: подпись не содержит цель «{goal[:40]}»"
                    + (f" [{where}]" if where else ""))
        if isinstance(meta, dict):
            meta["veto"] = "label_mismatch"
        return True

    def _choose_element(self, goal: str, items: List[dict],
                        router=None, host: Optional[str] = None,
                        op: str = "click",
                        page_url: Optional[str] = None
                        ) -> Tuple[Optional[int], dict]:
        """Выбор элемента: явный лидер по скору — без LLM; близкие кандидаты —
        top-5 в LLM, ответ строго одной цифрой; невалидный ответ — фолбэк на
        лучшего по скору (без «докручивания» парсинга) или честный отказ.
        host — для хост-зависимых синонимов скоринга (бургер → «Гид» на
        YouTube / «Меню» на остальных сайтах).
        Разрушительные кандидаты отсеивает сам скоринг (вето — инвариант), а
        единственный выход наружу (_out) проверяет это ещё раз.
        → (idx|None, meta) — meta (путь/кандидаты/сырой ответ LLM) идёт в аудит."""
        # Подписи кандидатов — текст страницы: с приватной — только локально.
        # page_url — полный адрес снапшота (приватность по пути: vk.com/im);
        # без него _privacy_router сверит хост с отслеживаемым URL чата
        if host or page_url:
            router = self._privacy_router(router, page_url, host)
        meta: Dict[str, object] = {"path": None, "candidates": [],
                                   "llm_response": None}
        # Активный слой поверх затемнённого фона (боковая корзина, модалка
        # с бэкдропом): элементы под бэкдропом (sc=False) или перекрытые
        # чужим фиксированным слоем (cov=True — попап без опознанного
        # бэкдропа) сейчас недоступны — клик попадает в оверлей и закрывает
        # панель. Выбираем только внутри слоя; если вне слоя оказались ВСЕ
        # кандидаты (ложный детект) — не режем в ноль, работаем с полным
        # списком (_active_layer)
        items = _active_layer(items)

        def _out(idx_val: Optional[int], path: str,
                 it: Optional[dict] = None) -> Tuple[Optional[int], dict]:
            """Единственный выход выбора: путь в мету + проверка инварианта.
            При отказе в мету идут снятые фильтром разрушительные подписи —
            иначе отказ по вето неотличим в аудите от «кандидатов не было»."""
            meta["path"] = path
            if idx_val is not None \
                    and not self._veto_destructive(goal, it, meta, "скоринг",
                                                   op=op):
                return int(idx_val), meta
            dropped = [str(x.get("text") or x.get("aria") or "")[:40]
                       for x in items if _destructive_mismatch(goal, x, op)]
            if dropped:
                meta["veto"] = "destructive"
                meta["vetoed"] = dropped[:LLM_TOP_N]
            return None, meta

        scored = self._score_candidates(items, goal, host=host, op=op)
        if not scored:
            # «выбрать на Цезарь с беконом»: плоско не матчится ни один элемент —
            # пробуем скоуп (кнопка в контексте карточки)
            scored = self._score_scoped(items, goal, op=op)
            if scored:
                meta["scoped"] = True
        meta["candidates"] = [
            {"idx": it["idx"], "text": str(it.get("text") or "")[:60],
             "score": round(s, 1)}
            for s, it in scored[:LLM_TOP_N]]
        self.stats["choices"] += 1
        if not scored:
            return _out(None, "none")
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
                return _out(top["idx"], "score", top)
        n_llm = min(LLM_TOP_N, len(scored))
        if (top_s >= LEADER_MIN_SCORE
                and (second_s is None or top_s - second_s >= LEADER_MARGIN)) \
                or (top_s >= FALLBACK_MIN_SCORE and second_s is None):
            # явный отрыв лидера — LLM не нужна
            return _out(top["idx"], "score", top)
        if router is not None:
            # Контекст слоя в строке кандидата: без него LLM выбирала между
            # «Десерт + 60 ₽» (пункт открытого попапа) и «Десерт 189 ₽»
            # (карточка каталога под ним) наугад — и часто брала карточку
            lines = "\n".join(
                f"{n}) [{it.get('tag')}/{it.get('role') or '-'}] "
                f"{str(it.get('text') or '')[:80]}{_layer_note(it)}"
                for n, (s, it) in enumerate(scored[:n_llm], 1))
            prompt = (
                f"Task: click \"{goal}\".\nCandidates:\n{lines}\n"
                f"Reply with ONLY one number (1-{n_llm}) — the number of the matching "
                "element. If nothing matches — reply \"no\".\n"
                + user_language_line(detect_language(goal)))
            try:
                resp = router.get_response([{"role": "user", "content": prompt}],
                                           temperature=0.0, max_tokens=8, top_p=0.1,
                                           webchat_channel="cc", force_provider=getattr(router, "cc_provider", None))
            except Exception as e:
                logger.debug(f"[CompControl] LLM-выбор элемента не удался: {e}")
                resp = None
            self.stats["llm_calls"] += 1
            meta["llm_response"] = (resp or "")[:200]
            m = re.fullmatch(r"\s*(\d{1,2})\s*", resp or "")
            if m and 1 <= int(m.group(1)) <= n_llm:
                # Разрушительных кандидатов в списке не было (скоринг их не
                # пустил) — отдельного вето на ответ LLM тут не нужно
                picked_it = scored[int(m.group(1)) - 1][1]
                self.stats["llm_valid"] += 1
                return _out(picked_it["idx"], "llm", picked_it)
            if _llm_said_no(resp):
                self.stats["llm_valid"] += 1  # валидный ответ: подходящего нет
                logger.info(f"[CompControl] LLM: нет подходящего элемента "
                            f"для «{goal[:40]}»")
                return _out(None, "none")
            self.stats["llm_invalid"] += 1
            logger.info(f"[CompControl] LLM-ответ невалиден: {(resp or '')[:60]!r}")
        # LLM недоступна/ошиблась — не докручиваем парсинг: фолбэк на лучшего
        # по скору, если он внятен, иначе честный отказ
        if top_s >= FALLBACK_MIN_SCORE:
            return _out(top["idx"],
                        "llm_fallback" if router is not None else "score", top)
        return _out(None, "none")

    @staticmethod
    def _element_by_idx(items: List[dict], idx: int) -> Optional[dict]:
        return next((it for it in items if it.get("idx") == idx), None)

    def _site_word_tracked(self, site_word: str) -> bool:
        """Слово места указывает на УЖЕ отслеживаемую страницу (алиас сайта
        ведёт на тот же хост, что уже открыт)? Сравниваем значимые слова с хостом/URL
        отслеживаемой вкладки и с алиасами sites, ведущими на этот же хост
        (слово места бывает на другом языке, чем хост). True — подмена
        отслеживаемой вкладкой не подмена, а то же самое место."""
        hay = _norm_match(f"{self._last_host or ''} {self._last_url or ''}")
        if not hay:
            return False
        for k, v in self.sites.items():
            h = (urlparse(v).hostname or "").lower()
            if h and self._last_host and (self._last_host == h
                                          or self._last_host.endswith("." + h)
                                          or h.endswith("." + self._last_host)):
                hay += " " + _norm_match(k)
        from app.features.web_search import _stem
        words = [w for w in re.findall(r"[a-z0-9а-яё]+", _norm_match(site_word))
                 if len(w) >= 3]
        return bool(words) and all(
            _word_in(w, hay) or _word_in(_stem(w), hay) for w in words)

    def _snapshot_state(self, site_word: Optional[str], chat_id: str,
                        auto_dismiss: bool = False):
        """Снапшот как ОДНО состояние: ((url, host, items, tab_id), None) или
        (None, причина). Четвёрка неделима — обновлять «по полям» нельзя:
        items одной вкладки с url/host/tab_id другой давали клик по метке в
        чужой странице. Каскад резолва меняет состояние только целиком."""
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id, auto_dismiss=auto_dismiss)
        if err:
            return None, err
        return (url, host, items, tab_id), None

    def take_dismissed(self, chat_id) -> Optional[str]:
        """Текст контрола, нажатого авто-закрытием оверлея в последнем
        снапшоте чата (_snapshot_for), — один раз."""
        return self.__dict__.get("_dismissed", {}).pop(_chat_key(chat_id), None)

    @_in_chat
    def _snapshot_for(self, site_word: Optional[str], chat_id: str = "",
                      auto_dismiss=False):
        """Общее для клика, скачивания и ввода: вкладка (алиас/явный домен/
        «на этой странице»/отслеживаемая) и её снапшот; отслеживаемую,
        которая ещё грузится, опрашиваем до NAV_LOAD_TIMEOUT_SEC, умершую
        забываем и падаем на последний хост.
        auto_dismiss — авто-закрытие типового оверлея (куки/подписка) перед
        снапшотом. Это КЛИК по странице, то есть побочный эффект на этапе
        резолва (до подтверждения), поэтому по умолчанию ВЫКЛЮЧЕН: его
        просит только резолв, который выбирает элемент по снапшоту (каскад
        _resolve_element_pick и подбор поля ввода) — оверлей там перекрывает
        цель и съедает бюджет. Чтение («что на странице», «прочти»,
        скриншот, масштаб, листание) и операции без выбора элемента
        (клавиша/отправка/слайдер/корзина) страницу на резолве не трогают.
        → (url, host, items, tab_id, None) или (None, None, None, None, причина)."""
        host_part = None
        tab_id = None
        if site_word == PAGE_REF:
            # «на этой/открывшейся странице» — отслеживаемая вкладка
            tab_id = self._last_tab_id
            if tab_id is None and not self._last_host:
                return None, None, None, None, self._tx("rs_no_page")
        elif site_word:
            sw = " ".join(site_word.lower().split())
            k = self._lookup(self.sites, sw)
            ks = None if k else self._lookup(self.search_urls, sw)
            if k:
                host_part = urlparse(self.sites[k]).hostname
            elif ks:
                # Слово места настроено в `search` («стриминг») — это тот же
                # сайт: хост берём из шаблона поиска, а не отказываем
                host_part = urlparse(self.search_urls[ks]).hostname
            elif _looks_like_domain(site_word):
                # Явный домен без алиаса («на example.com») — целимся напрямую
                host_part = site_word.strip().lower()
            elif sw not in _NOOP_SITE_WORDS and not self._site_word_tracked(sw):
                # Имя места не опознано (нет алиаса, не домен, не про
                # отслеживаемую страницу): молчаливый фолбэк на ПОСЛЕДНЮЮ
                # вкладку бил бы мимо адресата — честный отказ с причиной
                logger.info(f"[CompControl] Место «{site_word[:40]}» не "
                            "опознано — отказ вместо подмены вкладкой")
                return None, None, None, None, self._tx(
                    "rs_unknown_place", site=site_word)
        if host_part is None and tab_id is None:
            # Без указания места цель — отслеживаемая вкладка (последняя
            # открытая/тронутая ботом или та, на которую сказали «перейди
            # на вкладку …»): она и есть «текущий сайт», пока пользователь
            # явно не назвал другой. Видимость вкладки намеренно НЕ
            # смотрим: «тихое» фоновое открытие (окно браузера не
            # всплывает) оставляет целевую вкладку невидимой, и выбор по
            # видимости отправил бы команду в ПРЕЖНЮЮ страницу (открыли один
            # сайт, затем другой без подъёма окна — клик ушёл бы в первый);
            # тихий выбор вкладки без подъёма окна есть только на macOS, и
            # тот промахивается на редиректах SSO. Служебные вкладки (чат-UI,
            # веб-LLM) сюда не попадают никогда — их открытие контекста не
            # касается. Видимая пользовательская вкладка — только когда
            # отслеживаемого контекста нет вовсе.
            tab_id = self._last_tab_id
            if tab_id is None:
                host_part = self._last_host
            if tab_id is not None or host_part is not None:
                logger.debug(f"[CompControl] Цель — отслеживаемая вкладка "
                             f"#{tab_id} / хост {host_part}")
            else:
                try:
                    from app.features import browser_actions as _ba
                    _vis = _ba.visible_page_info()
                except Exception:
                    _vis = None
                if _vis and _vis[1] and _user_page_host(_vis[1]):
                    # Полный URL — точный матч вкладки (при дублях того же
                    # URL видимую из них выберет page_for)
                    host_part = _vis[0]
                    self._last_host = _vis[1]
                    logger.debug(f"[CompControl] Контекста нет — цель "
                                 f"видимая вкладка: {_vis[1]}")
        # Оверлей-блокер (куки-баннер, подписка, geo-попап) снимаем ДО
        # снапшота: он перекрывает контент и съедает бюджет снапшота, а его
        # контролы забирают приоритетные проходы. Просит это только резолв с
        # выбором элемента (auto_dismiss=True); цели-закрытия («закрой
        # окно») — исключение и там: крестик ищет скоринг, авто-клик мешает
        if auto_dismiss:
            # "consent" — только cookie/consent-баннер (агент задач: любой
            # другой диалог — часть наблюдения, решает модель через гейт)
            kw = {"consent_only": True} if auto_dismiss == "consent" else {}
            try:
                from app.features import browser_actions as _ba
                dismissed = _ba.dismiss_overlay(host_part, tab_id=tab_id, **kw)
            except Exception:
                dismissed = None
            if dismissed:
                self._audit(chat_id, {"kind": "overlay_dismiss",
                                      "value": dismissed,
                                      "host": host_part or self._last_host or ""},
                            True, "auto")
                if auto_dismiss == "consent":
                    # Агент задач пишет автонажатие в историю шага (своё —
                    # общего режима команды человека агент не приписывает)
                    self.__dict__.setdefault("_dismissed", {})[
                        _chat_key(chat_id)] = dismissed
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
            fb_host = (urlparse(host_part).hostname
                       if tab_id is None and host_part
                       and "://" in str(host_part) else None)
            if fb_host:
                # Записанный полный URL устарел (SSO-редирект: одноразовые
                # state/nonce в auth…?…) — та же страница по имени хоста
                try:
                    url, host, items = snapshot_elements(fb_host)
                    logger.info(f"[CompControl] Полный URL вкладки устарел — "
                                f"снапшот по хосту {fb_host}")
                    return url, host, items, None, None
                except Exception as e_fb:
                    e = e_fb
            if tab_id is None:
                logger.info(f"[CompControl] Снапшот страницы не удался: {e}")
                return None, None, None, None, self._tx(
                    "rs_snapshot_failed", detail=e)
            # Отслеживаемая вкладка может ещё грузиться (только что открыта,
            # фоновые вкладки Chrome грузятся небыстро) — опрашиваем, прежде
            # чем считать её мёртвой
            deadline = time.time() + NAV_LOAD_TIMEOUT_SEC
            while items is None and time.time() < deadline:
                if self._sleep_or_stop(NAV_POLL_SEC, chat_id):
                    # «стоп» пользователя — вкладку не забываем (она может
                    # быть просто медленной), честно говорим «остановлено»
                    from app.features import cc_texts
                    return None, None, None, None, cc_texts.t(
                        "stopped", self.turn_lang())
                try:
                    url, host, items = snapshot_elements(host_part, tab_id=tab_id)
                except Exception as e2:
                    e = e2
            if items is None:
                # Вкладка умерла (закрыли?) — забываем и пробуем по хосту
                logger.info(f"[CompControl] Отслеживаемая вкладка #{tab_id} "
                            f"недоступна: {e}")
                # Мёртвый id — у ВСЕХ чатов (_forget_tab без URL: хосты не
                # трогаем): иначе каждый другой чат ждал бы её те же ~10 с
                self._forget_tab(tab_id)
                tab_id = None
                try:
                    url, host, items = snapshot_elements(self._last_host)
                except Exception as e3:
                    logger.info(f"[CompControl] Снапшот страницы не удался: {e3}")
                    return None, None, None, None, self._tx(
                        "rs_snapshot_failed", detail=e3)
        if tab_id is not None and tab_id == self._last_tab_id and url:
            self._refresh_tracked_page(url, host)
        return url, host, items, tab_id, None

    def _refresh_tracked_page(self, url: str, host: Optional[str]):
        """Контекст следует за живым URL отслеживаемой вкладки: SSO/OAuth-
        редиректы (сайт → его домен авторизации и обратно после входа) и
        SPA-навигация меняют страницу без участия бота — без этого
        _last_host/_last_url замерли бы на адресе открытия, пока вкладка уже
        на домене авторизации. Возврат редиректом на исходный сайт после
        авторизации подхватывается сам при следующем действии — вкладка та
        же (tab_id стабилен), просто URL снова сменился. Служебные страницы
        (веб-чаты LLM, чат-UI бота) контекстом не становятся."""
        try:
            from app.features import browser_actions as _ba
            if _ba._chat_or_service_url(url):
                return
        except Exception:
            pass
        h = (host or urlparse(url or "").hostname or "")
        if not h:
            return
        if url:
            self._last_url = url
        if h != self._last_host:
            logger.info(f"[CompControl] Отслеживаемая страница переехала: "
                        f"{self._last_host} → {h}")
            self._last_host = h
            # На диск — только смена хоста: SPA-навигация меняет URL на
            # каждый чих (youtube watch?v=…), а таргетинг идёт по хосту —
            # писать last_tab.json на каждый снапшот бессмысленно
            self._save_last_page(url)

    @staticmethod
    def _resolve_fail_kind(meta: dict) -> str:
        """Почему выбор элемента не состоялся: подходило только разрушительное
        (вето) / пусто в снапшоте / кандидаты были, но LLM сказала «нет» /
        кандидаты были, но скор слабый / кончился бюджет каскада."""
        if meta.get("veto") == "destructive":
            return "destructive_veto"
        if meta.get("veto") == "label_mismatch":
            # Модель назвала номер, но подпись кандидата цели не содержит
            # (галлюцинация) — это не «низкий скор» и не «пусто в снапшоте»
            return "label_mismatch"
        if meta.get("budget_hit"):
            # Ярусы пропущены по бюджету времени — вердикта «нет» не было
            return "budget"
        if not meta.get("candidates"):
            return "not_in_snapshot"
        if _llm_said_no(meta.get("llm_response")):
            # «Нет» модели на цель, слов которой нет на странице вовсе, — это
            # «нет в снапшоте», а не вето: в список её просто не было из чего
            # включить
            if meta.get("goal_absent"):
                return "not_in_snapshot"
            return "llm_veto"
        return "low_score"

    def _scroll_hunt(self, _ba, host: str, tab_id: Optional[int],
                     search_goal: str, page_url: str = "",
                     deadline: Optional[float] = None):
        """Доскролл-поиск цели для виртуализированных списков/лент: текст цели
        появляется в DOM только после прокрутки в область. Фаза 1 — до 3
        экранов ОКНА вниз; фаза 2 — крупнейший внутренний контейнер (очередь
        YouTube #items: окно стоит, а пункты подгружаются только при
        прокрутке самого списка), до 10 шагов; перед фазой 2 окно
        возвращается на исходную позицию, иначе список уже за пределами
        вьюпорта и контейнерный шаг его не видит. Пересъёмка целевого
        снапшота после каждого шага; промах — прокрутку возвращаем, где была
        (пользователь не должен обнаружить страницу уехавшей).
        На свайп-лентах (shorts/reels) не работаем совсем: прокрутка там
        листает ролики, а не список элементов (см. _SWIPE_FEED_URL_RE).
        deadline (time.monotonic) — бюджет каскада: за ним шаги не делаем,
        прокрутку возвращаем как при промахе.
        → (url, items) последнего целевого снапшота."""
        from app.features.browser_actions import snapshot_for_goal
        if page_url and _SWIPE_FEED_URL_RE.search(page_url):
            logger.info(f"[CompControl] «{search_goal[:40]}»: свайп-лента "
                        f"({page_url[:60]}), доскролл-поиск пропущен")
            return "", []

        # «стоп» пользователя — как исчерпанный бюджет: шаги не делаем,
        # прокрутку возвращаем. Ключ — ход потока (у веб-чата без chat_id —
        # user_id), вызывающие chat_id сюда не передают
        stop = self._stop_check()

        def _late() -> bool:
            return (deadline is not None and time.monotonic() >= deadline) \
                or stop()

        y0 = _ba.scroll_position(host, tab_id)
        win_back = False
        g_url, g_items = "", []
        for _ in range(3):
            if _late():
                break
            step = _ba.scroll_step(host, tab_id)
            if not step.get("moved"):
                break
            _ba.wait_dom_idle(host, tab_id, timeout_sec=1.5, min_wait=0.2,
                              stop=stop)
            try:
                g_url, g_items = snapshot_for_goal(host, search_goal,
                                                   tab_id=tab_id)
            except Exception:
                g_items = []
            if g_items:
                logger.info(f"[CompControl] «{search_goal[:40]}» нашлось "
                            "после доскролла")
                break
        if not g_items and not _late():
            # Фаза 2: виртуализированный список ВНУТРИ страницы (очередь
            # плеера) — окно его не прокручивает. Сначала возвращаем ОКНО на
            # исходную позицию: фаза 1 проскроллила страницу вниз, и
            # внутренний контейнер (панель плейлиста YouTube, #items в
            # ytd-playlist-panel-renderer) уехал из вьюпорта — а шаг
            # контейнера видит только ВИДИМЫЕ контейнеры и крутил бы левое
            # меню вместо списка — цель не находилась за пределами уже
            # отрендеренных пунктов плейлиста
            if y0 is not None:
                _ba.scroll_restore(host, tab_id, y0)
                win_back = True   # окно уже на месте — второй раз не гоняем
            cy0 = None
            for _ in range(10):
                if _late():
                    break
                step = _ba.scroll_container_step(host, tab_id)
                if cy0 is None and step.get("y0") is not None:
                    cy0 = step["y0"]
                if not step.get("moved"):
                    break
                _ba.wait_dom_idle(host, tab_id, timeout_sec=1.5, min_wait=0.2,
                                  stop=stop)
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
        # Окно возвращаем РОВНО один раз: фаза 2 уже вернула его перед стартом,
        # контейнерный шаг окно не двигает — повторный возврат был лишним
        # вызовом в браузер на каждом промахе доскролла
        if not g_items and y0 is not None and not win_back:
            _ba.scroll_restore(host, tab_id, y0)
        return g_url, g_items

    def _clear_leader(self, goal: str, meta: Optional[dict],
                      items: List[dict]) -> bool:
        """Выбор дешёвого яруса уверенный — по тем же правилам, что у
        _choose_element (LEADER_MIN_SCORE, отрыв LEADER_MARGIN от второго),
        только без LLM (путь score). Единственный кандидат лидером считаем,
        лишь когда слова цели стоят в его подписи буквально: совпадение по
        основе («соусы» → «2 соуса 89 ₽») — повод поискать точный текст за
        бюджетом снапшота."""
        if not isinstance(meta, dict) or meta.get("path") != "score":
            return False
        cands = meta.get("candidates") or []
        if not cands:
            return False
        top = float(cands[0].get("score") or 0.0)
        if top < LEADER_MIN_SCORE:
            return False
        if len(cands) > 1:
            return top - float(cands[1].get("score") or 0.0) >= LEADER_MARGIN
        it = self._element_by_idx(items, cands[0].get("idx")) or {}
        hay = _norm_match(" ".join(str(it.get(k) or "")
                                   for k in ("text", "aria", "title")))
        words = [w for w in re.findall(r"[a-z0-9а-яё]+", _norm_match(goal))
                 if len(w) >= 3]
        return bool(words) and all(_word_in(w, hay) for w in words)

    @staticmethod
    def _goal_in_snapshot(goal: str, items: Optional[List[dict]],
                          host: Optional[str] = None) -> bool:
        """Хоть одно значимое слово цели (стем/синоним) есть где-то в
        снапшоте — в подписи или контексте любого элемента. False — цели на
        странице нет вовсе: «нет» модели тогда не вето, а «нет в снапшоте»."""
        words = [w for w in re.findall(r"[a-z0-9а-яё]+", _norm_match(goal))
                 if len(w) >= 3]
        if not words:
            return True
        from app.features.web_search import _stem
        hay = _norm_match(" ".join(
            " ".join(str(it.get(k) or "")
                     for k in ("text", "aria", "title", "tid", "ctx"))
            for it in (items or [])))
        if not hay:
            return False
        return any(_word_in(w, hay) or _word_in(_stem(w), hay)
                   or any(_word_in(s, hay) for s in _goal_synonyms(w, host))
                   for w in words)

    def _follow_visible_tab(self) -> None:
        """Команда без названного сайта: пользователь сам переключился на
        другую вкладку после прошлого резолва — цель та, что он видит.
        Сравниваем видимый хост с видимым на прошлом резолве (_vis_baseline):
        не изменился — отслеживаемая вкладка остаётся целью даже невидимой
        (тихое фоновое открытие сайта ботом окно не поднимает — видимой
        остаётся прежняя страница, это не переключение). Изменился и это
        другой сайт, чем отслеживаемый — видимая становится контекстом.
        База — своя у чата; открытие/переключение вкладки ботом ставит её
        сразу (_init_vis_baseline), и она переживает перезапуск
        (last_tab.json) — ручное переключение до первой команды заметно."""
        if self._last_tab_id is None and not self._last_host:
            return  # контекста нет — _snapshot_for и так берёт видимую
        try:
            from app.features import browser_actions as _ba
            vis = _ba.visible_page_info()
        except Exception:
            vis = None
        if not vis or not vis[1] or not _user_page_host(vis[1]):
            return
        v_url, v_host = vis
        prev = getattr(self, "_vis_baseline", None)
        self._vis_baseline = v_host

        def _same(a: Optional[str], b: Optional[str]) -> bool:
            # _last_host бывает с сегментом пути («youtube.com/watch»)
            a = str(a or "").lower().split("/")[0]
            b = str(b or "").lower().split("/")[0]
            if not a or not b:
                return False
            return a == b or a.endswith("." + b) or b.endswith("." + a) \
                or ".".join(a.split(".")[-2:]) == ".".join(b.split(".")[-2:])

        if prev is None or _same(prev, v_host) \
                or _same(self._last_host, v_host):
            if str(prev or "").lower() != v_host.lower():
                # База сменилась — на диск (без смены страницы)
                self._save_last_page()
            return
        logger.info(f"[CompControl] Пользователь переключился на вкладку "
                    f"{v_host} (была {prev}, отслеживалась "
                    f"{self._last_host}) — цель команды видимая вкладка")
        self._last_tab_id = None
        self._last_host = v_host
        self._last_url = v_url
        try:
            self._save_last_page(v_url)
        except Exception:
            pass

    def _resolve_element(self, goal: str, site_word: Optional[str], router,
                         chat_id: str = "", auto_dismiss: bool = True,
                         op: str = "click", share: Optional[dict] = None):
        """Единственная точка выхода каскада резолва наружу (клик, наведение,
        скачивание): зовёт _resolve_element_pick и проверяет инвариант вето —
        элемент (или подписанная зона vision), выбранный любой веткой, не
        может быть контролом закрытия/удаления, пока такого намерения нет в
        цели. Поэтому веткам каскада своих копий вето держать не нужно.
        op — тип операции: для неактивирующих (наведение) вето не работает
        (см. _destructive_mismatch).
        share — общее состояние нескольких попыток одного запроса («закрой
        X»: целевой крестик → общий → «свернуть»): один снапшот, один
        дедлайн бюджета, один vision-кадр (см. _resolve_element_pick)."""
        share = share if share is not None else {}
        r = self._resolve_element_pick(goal, site_word, router,
                                       chat_id=chat_id,
                                       auto_dismiss=auto_dismiss, op=op,
                                       share=share)
        url, host, items, idx, tab_id, meta, err = r
        if isinstance(meta, dict) and share.get("t0") is not None:
            meta["resolve_ms"] = int((time.monotonic() - share["t0"]) * 1000)
        if err is not None:
            return r
        item = None
        if idx is not None:
            item = self._element_by_idx(items or [], idx)
        elif (meta or {}).get("point"):
            # Зональный vision: DOM-метки нет, судить можно только по подписи
            # зоны (безымянная зона разрушительной не считается — не по чему)
            item = {"text": str((meta or {})["point"].get("label") or "")}
        if self._veto_destructive(goal, item, meta, str(host or ""), op=op):
            reason = self._tx("rs_destructive_veto", host=host, goal=goal)
            self._audit_resolve(chat_id, goal, host, reason,
                                "destructive_veto", meta=meta)
            return None, None, None, None, None, meta, reason
        return r

    def _resolve_element_pick(self, goal: str, site_word: Optional[str], router,
                              chat_id: str = "", auto_dismiss: bool = True,
                              op: str = "click", share: Optional[dict] = None):
        """Общее для клика и скачивания: вкладка и снапшот (_snapshot_for),
        скоринг, при неоднозначности — выбор номера через LLM (top-5, один
        токен). Ярус 1 скоринга — только видимые элементы (vp): цель почти
        всегда на экране пользователя, и тогда целевой снапшот/доскролл не
        запускаются — страница не уезжает; ярус 2 (промах яруса 1) — полный
        список и эскалация со скроллом. → (url, host, items, idx, tab_id,
        meta, None) или (None…, причина).
        Неудачи резолва пишутся в audit.jsonl с классом причины
        (_audit_resolve) — так отказы разбираются по логам, а не вслепую.
        Бюджет: весь каскад укладывается в resolve_budget_sec — за дедлайном
        оставшиеся ярусы не зовутся (fail_reason «budget»). share — общее
        состояние попыток одного запроса: t0/deadline, снапшот (snap),
        vision-состояние (vis), флаг сделанного повторного снапшота."""
        from app.features import browser_actions as _ba
        share = share if share is not None else {}
        if share.get("t0") is None:
            share["t0"] = time.monotonic()
            share["deadline"] = share["t0"] + float(
                getattr(self, "resolve_budget_sec", 25.0))
        deadline = float(share["deadline"])

        def _late() -> bool:
            # Бюджет каскада исчерпан: дальше только честный отказ
            if time.monotonic() >= deadline:
                share["budget_hit"] = True
                return True
            return False

        def _stamp(m: Optional[dict]) -> Optional[dict]:
            if isinstance(m, dict):
                m["resolve_ms"] = int((time.monotonic() - share["t0"]) * 1000)
                if share.get("budget_hit"):
                    m["budget_hit"] = True
            return m

        snap = share.get("snap")
        if snap is None:
            if not site_word:
                # Пользователь сам переключил вкладку после прошлого действия
                # бота — команда про то, что он видит (см. _follow_visible_tab)
                self._follow_visible_tab()
            snap, err = self._snapshot_state(site_word, chat_id, auto_dismiss)
            if err:
                # Хост — в аудит даже при сбое снапшота: иначе отказ не
                # привязать к сайту
                err_host = (site_word if site_word and site_word != PAGE_REF
                            else self._last_host)
                self._audit_resolve(
                    chat_id, goal, err_host, err,
                    "no_page" if str(err).startswith("Пока нет")
                    else "snapshot_error",
                    meta=_stamp({}))
                return None, None, None, None, None, None, err
            share["snap"] = snap
        # Состояние снапшота — одна четвёрка: url/host/items/tab_id всегда
        # меняются вместе (см. _snapshot_state)
        url, host, items, tab_id = snap
        # Приватная страница: LLM-выбор только локальной моделью, без vision
        router = self._privacy_router(router, url or host)
        # Дедлайн дешёвых ярусов (доскролл, другие вкладки, повторный
        # снапшот): хвост бюджета держим под vision — на живой ленте DOM не
        # затихает, и доскролл съедал весь бюджет, а иконку («лайк») находил
        # только гибрид, до которого очередь уже не доходила. Vision нет
        # (выключен, приватная страница) — резерв не нужен
        pre_vis_deadline = deadline
        if self._vision_ready(router):
            pre_vis_deadline -= min(
                VISION_RESERVE_SEC,
                VISION_RESERVE_SHARE * (deadline - float(share["t0"])))

        def _pre_vis_late() -> bool:
            return time.monotonic() >= pre_vis_deadline

        # Активная вкладка — наш чат: кликать там нечего, а другую вкладку
        # гадать опасно (промах по чужому сайту хуже отказа)
        p = urlparse(url)
        if p.hostname in ("localhost", "127.0.0.1") and p.port in (5173, 8000):
            return None, None, None, None, None, None, self._tx(
                "rs_click_chat_tab")
        # Ярус 1 — только видимое: «нажми X» почти всегда про то, что
        # пользователь видит на экране; опечатки покрывает fuzzy-ярус
        # скоринга и снятие диакритики («cafe» = «café»). Нашлось на видимой
        # странице — дальше клик без скролла: целевой снапшот со
        # scrollIntoView и доскролл-поиск не запускаем — иначе одноимённый
        # элемент дальше по ленте мог заменить верный видимый выбор при
        # равном скоре. Промах — ярус 2: полный список, и тогда вся
        # эскалация со скроллом ниже. Совпадает с полным списком (вся
        # страница видима) — ярус 1 не гоняем: выбор идентичен
        vp_items = [it for it in items if it.get("vp", True)]
        idx, meta = None, None
        if vp_items and len(vp_items) < len(items):
            idx, meta = self._choose_element(goal, vp_items, router,
                                             host=host, op=op)
        if idx is not None:
            meta["vp_first"] = True
            logger.debug(f"[CompControl] «{goal[:40]}» нашлось на видимой "
                         "странице — без скролла")
        else:
            idx, meta = self._choose_element(goal, items, router, host=host,
                                            op=op)
        gen_top = meta["candidates"][0]["score"] if meta["candidates"] else None
        # Скоринг (с fuzzy-ярусом: «кэшбек» ≈ «Кешбэк») нашёл кандидатов —
        # цель на странице есть, и «нет» модели дальше — вето, а не «нет в
        # снапшоте», даже если итоговую мету перепишет широкий ярус
        had_cands = gen_top is not None
        # Состояние vision на этот резолв: сбой скриншота / лежащая
        # vision-цепочка / след ярусов для аудита (см. _vision_ready). Общее
        # на попытки одного запроса: кадр один, лежащая цепочка не зовётся
        # повторно. Гибрид для иконки может отработать до доскролла
        # (hybrid_done) — второй раз его не зовём
        vis: Dict[str, object] = share.setdefault("vis", {})
        hmeta = None
        hybrid_done = False
        # Слабый лидер общего снапшота (ниже точного попадания в текст/aria)
        # — цель могла просто не влезть в его бюджет: «соусы» уехало в
        # «2 соуса 89 ₽» по основе слова, а карточка «Соусы» не влезла в
        # сотню. Даём целевому снапшоту шанс найти точный текст по всему DOM
        # и заменяем выбор, только если он увереннее. Выбор яруса 1
        # (видимая страница, vp_first) не оспариваем — иначе страница
        # уезжает из-под глаз к равносильному матчу ниже по ленте. Явный
        # лидер дешёвого яруса (LEADER_MIN_SCORE/LEADER_MARGIN) тоже не
        # оспариваем: целевой снапшот со scrollIntoView на уверенном выборе
        # только двигает страницу и тратит бюджет
        m_scope0 = _SCOPE_SPLIT_RE.match(" ".join(goal.split()))
        if idx is None or (not meta.get("vp_first")
                           and gen_top is not None and gen_top < 90.0
                           and not (not m_scope0
                                    and self._clear_leader(goal, meta, items))
                           and not _late()):
            # Элемент мог просто не влезть в снапшот: бюджет 100 на богатых
            # страницах съедают шапка и верхние разделы каталога — на
            # каталоге с сотнями кликабельных элементов нужный пункт может
            # в снапшот не попасть вовсе.
            # Целевой снапшот: по ВСЕМУ DOM элементы, чей текст содержит цель
            # (для скоуп-цели «выбрать на Цезарь» ищем скоуп — «цезарь»)
            from app.features.browser_actions import snapshot_for_goal
            m_scope = m_scope0
            search_goal = goal
            if m_scope:
                # «сырный в части слева»: искать на странице «части слева»
                # бессмысленно — для пространственного скопа ищем ДЕЙСТВИЕ
                # («сырный»); для карточного («выбрать на Цезарь») — скоп
                search_goal = m_scope.group(1) \
                    if _SPATIAL_SCOPE_RE.match(_norm_match(m_scope.group(2))) \
                    else m_scope.group(2)
            search_goal = _goal_with_synonyms(search_goal, host)
            g_url, g_items = "", []
            if not _late():
                try:
                    g_url, g_items = snapshot_for_goal(host, search_goal,
                                                       tab_id=tab_id)
                except Exception as e:
                    g_items = []
                    logger.debug(f"[CompControl] Целевой снапшот не удался: {e}")
            if not g_items and idx is None and not had_cands \
                    and self.wide_mode == "hybrid" and not _late() \
                    and (_icon_goal(goal)
                         or not self._goal_in_snapshot(goal, items, host)):
                # Ни одного текстового совпадения — ни скоринга, ни слов
                # цели (с синонимами) в снапшоте, ни по всему DOM: обычно
                # иконка без подписи («лайк»). Доскролл ищет по тексту и её
                # не найдёт — гибрид (рамки видимых кандидатов) раньше
                # доскролла, пока бюджет не съеден. Чужую подпись гибрид
                # не выберет: сверка подписи ветирует несовпадение
                hybrid_done = True
                hidx, hmeta = self._hybrid_pick(goal, items, host, tab_id,
                                                router, op=op, vis=vis)
                self._note_tier(vis, "vision_hybrid", hmeta)
                if hidx is not None:
                    return (url, host, items, hidx, tab_id,
                            _stamp(self._with_tiers(hmeta, vis)), None)
            if not g_items and idx is None and not _pre_vis_late():
                # Виртуализированный список/бесконечная лента: цель не
                # отрендерена, пока её не доскроллили — доскролл-поиск
                # (на свайп-лентах сам пропускается: там он листает ролики).
                # Только при промахе: уже найденный выбор доскроллом не
                # оспариваем — страница видимо уезжала на удачных кликах.
                # Дедлайн — без резерва под vision-ярусы
                g_url, g_items = self._scroll_hunt(_ba, host, tab_id,
                                                   search_goal, page_url=url,
                                                   deadline=pre_vis_deadline)
                if hybrid_done and g_items:
                    # Кадр гибрида снят до прокрутки, а на находке доскролл
                    # страницу назад не возвращает — кадр устарел (на промахе
                    # прокрутка возвращена, кадр годен)
                    vis.pop("shot", None)
            if g_items:
                g_idx, g_meta = self._choose_element(goal, g_items, router,
                                                     host=host, op=op)
                llm_veto = str(g_meta.get("llm_response") or "")
                if g_idx is None and not _llm_said_no(llm_veto):
                    # Кандидаты уже отфильтрованы по тексту цели на странице:
                    # единственный из них безопасен и без LLM (а вето LLM
                    # уважаем — она посмотрела и сказала «нет»)
                    sole = self._score_candidates(g_items, goal, host=host,
                                                  op=op) \
                        or self._score_scoped(g_items, goal, op=op)
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
                        and not _llm_said_no(llm_veto):
                    # Скоуп — секция страницы («сыры чеддер и пармезан В
                    # ДОБАВИТЬ ПО ВКУСУ»), а не текст карточки: её слов нет в
                    # ctx контролов секции, и скоуп-скоринг полной цели пуст.
                    # Целевой снапшот УЖЕ отфильтровал область страницы —
                    # выбираем по части-действию. Для «выбрать на Цезарь»
                    # (кнопка в карточке) не сработает — там скоуп-скоринг
                    # выше отрабатывает; для пространственного скопа часть-
                    # действие теряет сторону — его пропускаем
                    a_idx, a_meta = self._choose_element(
                        m_scope.group(1), g_items, router, host=host, op=op)
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
                    replace = idx is None or g_score >= (gen_top or 0.0)
                    if not replace and m_scope \
                            and not _SPATIAL_SCOPE_RE.match(
                                _norm_match(m_scope.group(2))):
                        # Скоуп-цель («троеточие в <комментарий>»): общий
                        # выбор без слов скоупа в тексте/контексте — ложный
                        # (меню основного видео вместо меню комментария);
                        # целевой снапшот отфильтрован по тексту скоупа —
                        # ему доверяем, даже если его скор ниже
                        cur = self._element_by_idx(items, idx)
                        sw = [w for w in re.findall(
                            r"[a-z0-9а-яё]+", _norm_match(m_scope.group(2)))
                            if len(w) >= 3]
                        hay = _norm_match(" ".join([
                            str((cur or {}).get("text") or ""),
                            str((cur or {}).get("ctx") or ""),
                            str((cur or {}).get("aria") or "")]))
                        if not cur or not any(_word_in(w, hay) for w in sw):
                            replace = True
                    if replace:
                        g_meta["via"] = "goal_snapshot"
                        url, items, idx, meta = g_url or url, g_items, g_idx, g_meta
        if idx is None:
            # Элемента нет на целевой вкладке. Ищем по остальным вкладкам
            # ТОГО ЖЕ сайта (семейство хостов: окно входа auth.… могло
            # открыться отдельно и не попасть в треки). Другой сайт — никогда:
            # «текущий сайт» липкий, и клик по чужой вкладке хуже отказа —
            # ошибка адресата опаснее честного «не нашёл».
            # Берём только ЕДИНСТВЕННЫЙ явный лидер без LLM — гадать опаснее
            alt = None if _late() or _pre_vis_late() \
                else self._element_on_other_pages(
                    goal, url, only_host=host, op=op,
                    deadline=pre_vis_deadline)
            if alt is not None:
                url, host, items, idx, tab_id, meta = alt
        if idx is None and not share.get("retry_done") and not _late() \
                and not _pre_vis_late():
            # Контент мог не дорендериться после прошлого действия (панель
            # выбора подгружается лениво): ждём стабилизации DOM
            # (не слепой слип) + ОДИН повторный снапшот с обычным выбором.
            # Второй промах — уже честный отказ. Одна на запрос: попытки
            # «закрой X» делят снапшот и повтор не множат
            share["retry_done"] = True
            logger.info(f"[CompControl] «{goal[:40]}» не нашлось — повторный "
                        "снапшот после стабилизации DOM")
            _ba.wait_dom_idle(host, tab_id, timeout_sec=3.0, min_wait=1.0)
            snap2, err2 = self._snapshot_state(site_word, chat_id, auto_dismiss)
            if not err2:
                # Состояние снапшота меняем ЦЕЛИКОМ, а не по отдельным полям:
                # url/host/tab_id из первого снапшота при items из второго
                # искали бы метки в другой вкладке (отслеживаемая могла
                # умереть и подмениться хостом), а vision получал бы
                # скриншот не той страницы
                def _sig(s):
                    return (s[0], s[3], [(it.get("idx"), it.get("text"),
                                          it.get("x"), it.get("y"))
                                         for it in s[2] or []])

                if _sig(snap2) != _sig((url, host, items, tab_id)):
                    # Кадр vision от прежнего снапшота устарел (гибрид до
                    # доскролла мог его уже снять); страница та же — кадр
                    # годен, второй скриншот не нужен
                    vis.pop("shot", None)
                url, host, items, tab_id = snap2
                share["snap"] = snap2
                idx, meta = self._choose_element(goal, items, router,
                                                 host=host, op=op)
                had_cands = had_cands or bool(meta.get("candidates"))
                if idx is not None:
                    # Дальше — только проверка сомнительного выбора в конце
                    # каскада (vision-ярусы ниже идут лишь при промахе):
                    # ничья одноимённых после повтора вслепую не кликается
                    meta["retried"] = True
        h_fail = None
        if idx is None and self.wide_mode == "hybrid" and not hybrid_done \
                and not _late():
            # Гибридный ярус (zero-match): один vision-вызов видит рамки
            # видимых кандидатов И текстовый список элементов без рамки —
            # вместо пары «широкий текстовый резолв → vision-рамки». По данным
            # аудит-журнала текстовый широкий резолв попадал лишь в ~половине
            # случаев, а уверенный промах не пускал vision дальше вовсе.
            # Иконки идут сюда же: безымянным кнопкам достаются рамки
            hidx, hmeta = self._hybrid_pick(goal, items, host, tab_id,
                                            router, op=op, vis=vis)
            self._note_tier(vis, "vision_hybrid", hmeta)
            if hidx is not None:
                return (url, host, items, hidx, tab_id,
                        _stamp(self._with_tiers(hmeta, vis)), None)
        if idx is None and hmeta is not None:
            # (и для гибрида до доскролла — hybrid_done)
            # Повод спросить текстовый широкий резолв: ответ невалиден
            # или рамка с подписью ветирована сверкой (label_mismatch) —
            # «другое название» видимого элемента решит независимая
            # текстовая модель, один вызов и только на вето
            h_fail = hmeta.get("fail") or (
                "label_mismatch"
                if hmeta.get("veto") == "label_mismatch" else None)
            if not (meta or {}).get("veto"):
                # Vision уже посмотрела страницу — её вердикт в аудит;
                # более конкретное вето прошлых ярусов не затираем
                meta = hmeta
        # Широкий LLM-резолв (zero-match): скоринг не дал ни одного
        # кандидата — слова цели не совпали с подписями на странице
        # («почта» при «Электронная почта»). LLM выбирает из компактного
        # списка элементов снапшота: дешевле скриншота vision-фолбэка и
        # закрывает кейс «другое название». В режиме hybrid — когда гибрид
        # не запускался, vision лежит (hmeta None), ответ гибрида невалиден
        # или его выбор рамки ветирован сверкой подписи: текстовый ярус идёт
        # через cc_provider, другой канал. Явное «нет» и вето разрушительного
        # — вердикт, текстовый не зовём
        if idx is None and (hmeta is None
                            or h_fail in ("invalid", "label_mismatch")) \
                and not _late():
            widx, wmeta = self._llm_wide_pick(goal, items, router, op=op,
                                              host=host)
            self._note_tier(vis, "llm_wide", wmeta)
            if widx is not None:
                return (url, host, items, widx, tab_id,
                        _stamp(self._with_tiers(wmeta, vis)), None)
            if wmeta is not None:
                meta = wmeta  # LLM посмотрела страницу — её вердикт в аудит
        if idx is None and hmeta is None and not _late():
            # Визуальный фолбэк: текстовый скоринг структурно бессилен
            # при иконочных UI (пустые accessible name) — скриншот вьюпорта
            # с пронумерованными рамками кандидатов в vision-модель. Гибрид
            # отработал (любой исход с метой) — рамки он уже показывал: он
            # надмножество этого яруса. Vision лежит — ярус молчит сам
            vidx, vmeta = self._visual_resolve(host, tab_id, items, goal,
                                               router, op=op, vis=vis)
            self._note_tier(vis, "vision", vmeta)
            if vidx is not None:
                return (url, host, items, vidx, tab_id,
                        _stamp(self._with_tiers(vmeta, vis)), None)
            if vmeta is not None and not (meta or {}).get("veto"):
                # Вердикт визуального яруса — в аудит; уже записанное вето
                # (более конкретная причина) не затираем
                meta = vmeta
        if idx is None and not _late():
            # Зональный vision-фолбэк: DOM нечитаем совсем (canvas/WebGL,
            # ARIA-скрытая разметка) — рамки вокруг всех кликабельных зон
            # вьюпорта; выбранная зона кликается по координатам (meta["point"]).
            # Vision лежит (vis["down"]) — ярус сам не зовётся
            pnt, pmeta = self._vision_zones(goal, host, tab_id, router,
                                            op=op, vis=vis)
            self._note_tier(vis, "vision_zones", pmeta)
            if pnt is not None and (pmeta or {}).get("agree_idx") is not None:
                # Зоны независимо указали на тот же элемент, что гибрид
                # (ветированный сверкой подписи): два согласных vision-ответа
                # — клик по DOM-метке, но только после «да» человека
                a_idx = int(pmeta["agree_idx"])
                pmeta.pop("point", None)
                pmeta["force_confirm"] = True
                return (url, host, items, a_idx, tab_id,
                        _stamp(self._with_tiers(pmeta, vis)), None)
            if pnt is not None:
                return (url, host, items, None, tab_id,
                        _stamp(self._with_tiers(pmeta, vis)), None)
            if pmeta is not None and not (meta or {}).get("veto"):
                meta = pmeta  # вердикт зонального яруса — в аудит
        meta = self._with_tiers(meta, vis)
        if idx is None:
            if not isinstance(meta, dict):
                meta = {"path": "none", "candidates": [],
                        "llm_response": None}
            _stamp(meta)
            # Слов цели нет на странице вовсе — «нет» модели это не вето,
            # а «нет в снапшоте» (_resolve_fail_kind)
            meta["goal_absent"] = not had_cands and not self._goal_in_snapshot(
                goal, items, host)
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
                reason = self._tx("rs_antibot", host=host, kind=antibot)
            elif fail_kind == "budget":
                reason = self._tx("rs_budget", host=host, goal=goal,
                                  sec=int(self.resolve_budget_sec))
            else:
                reason = self._tx("rs_no_element", host=host, goal=goal)
            self._audit_resolve(chat_id, goal, host, reason, fail_kind,
                                meta=meta)
            return None, None, None, None, None, meta, reason
        if idx is not None and not _late():
            # Сомнительный выбор (слабый ярус скора или ничья одноимённых
            # кандидатов) — до клика вслепую спрашиваем зональный vision:
            # общий дизамбигуатор без привязки к сайту
            g_idx, g_meta = self._vision_gate_choice(goal, host, tab_id,
                                                     idx, meta, router, op=op,
                                                     vis=vis)
            if g_idx is None and g_meta.get("point"):
                return (url, host, items, None, tab_id,
                        _stamp(self._with_tiers(g_meta, vis)), None)
            from app.features.cc_privacy import PrivateRouter
            why = self._choice_doubt(idx, meta) \
                if isinstance(router, PrivateRouter) else None
            if why and isinstance(meta, dict):
                # Приватная страница (корзина/ЛК/вход): vision-проверки нет —
                # сомнительный выбор (ничья «Изменить» в рядах) только с «да»
                logger.info(f"[CompControl] «{goal[:40]}» приватная страница, "
                            f"выбор сомнителен ({why}) — с подтверждением")
                meta["force_confirm"] = True
                meta["private_doubt"] = why
        elif idx is not None and isinstance(meta, dict):
            # За бюджетом vision-проверки нет: сомнительный выбор (те же
            # критерии) не кликаем вслепую — только после «да» человека
            why = self._choice_doubt(idx, meta)
            if why:
                logger.info(f"[CompControl] «{goal[:40]}» бюджет исчерпан, "
                            f"выбор сомнителен ({why}) — с подтверждением")
                meta["force_confirm"] = True
                meta["budget_doubt"] = why
        return (url, host, items, idx, tab_id,
                _stamp(self._with_tiers(meta, vis)), None)

    def _vision_gate_choice(self, goal: str, host: str, tab_id,
                            idx: int, meta: Dict[str, object], router,
                            op: str = "click", vis: Optional[dict] = None):
        """Проверка сомнительного текстового выбора зональным vision —
        чтобы не писать детерминированный фолбэк под каждый сайт.
        Сомнительно: слабый ярус (<55: голый контекст/подстрока/синоним —
        клики «наугад» вроде карточки комбо по упоминанию товара в её
        описании) или ничья одноимённых кандидатов с близким скором
        (три «Изменить» в корзине — выбор по списку среди одинаковых
        подписей жребий, а vision видит, какая ссылка в ряду нужного
        товара). Vision недоступен/отказался — исходный выбор без
        изменений. → (idx|None, meta): idx=None + meta["point"] —
        координатный клик по выбранной зоне."""
        if not self.vision_fallback or router is None:
            return idx, meta
        why = self._choice_doubt(idx, meta)
        if not why:
            return idx, meta
        logger.info(f"[CompControl] «{goal[:40]}» выбор сомнителен ({why}) — "
                    "проверяю зональным vision")
        pnt, pmeta = self._vision_zones(goal, host, tab_id, router, op=op,
                                        vis=vis)
        self._note_tier(vis, "vision_gate", pmeta)
        if pnt is None:
            return idx, meta
        pmeta["vision_gate"] = why
        return None, pmeta

    @staticmethod
    def _choice_doubt(idx, meta) -> Optional[str]:
        """Выбор текстового яруса сомнителен? → причина или None. Критерии
        общие для vision-проверки (_vision_gate_choice) и выбора за дедлайном
        бюджета (там проверки нет — только подтверждение): слабый скор (<55)
        или ничья одноимённых кандидатов с близким скором."""
        if not isinstance(meta, dict) \
                or meta.get("path") not in ("score", "llm", "llm_fallback"):
            return None
        cands = meta.get("candidates") or []
        top = next((float(c.get("score") or 0.0) for c in cands
                    if c.get("idx") == idx), 0.0)
        if top <= 0.0:
            return None
        chosen_txt = _norm_match(next((str(c.get("text") or "")
                                       for c in cands
                                       if c.get("idx") == idx), ""))
        tied = bool(chosen_txt) and any(
            c.get("idx") != idx
            and _norm_match(str(c.get("text") or "")) == chosen_txt
            and abs(float(c.get("score") or 0.0) - top) < 5.0
            for c in cands)
        if tied:
            return "ничья одноимённых"
        if top < 55.0:
            return f"слабый скор {top:.1f}"
        return None

    def _llm_wide_pick(self, goal: str, items: List[dict], router,
                       for_field: bool = False, op: str = "click",
                       host: Optional[str] = None
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
        if not for_field and _icon_goal(goal):
            # Цель-иконка, а скоринг с синонимами её не нашёл: подписи у
            # иконки нет, в текстовом списке её не будет — LLM лишь ткнёт
            # в чужой элемент. Сразу в vision (см. _ICON_WORD_ROOTS)
            logger.info(f"[CompControl] «{goal[:40]}» — иконка без подписи: "
                        "текстовый широкий резолв пропущен, дальше vision")
            return None, None
        # Активный слой поверх затемнённого фона — как в _choose_element:
        # под бэкдропом (sc=False) или под чужим слоем (cov) элементы
        # недоступны, в список для LLM их не берём; все вне слоя (ложный
        # детект) — не режем в ноль (_active_layer)
        items = _active_layer(items)
        # Безымянные элементы (ни текста, ни aria — иконки-SVG) текстовой
        # LLM нечем сопоставить с целью — их разбирает vision-фолбэк.
        # Служебные обрывки («0:13», «/», «•», «33 тыс. 1 г. назад») тоже
        # не берём: они съедали бюджет LLM_WIDE_MAX, и цель в список не
        # попадала вовсе (_wide_label_ok)
        named = [it for it in items
                 if (it.get("text") or it.get("aria") or it.get("title"))
                 and (for_field and it.get("ed") or _wide_label_ok(it))]
        # Порядок для промпта: для цели ввода — поля первыми, затем видимые
        # во вьюпорте; псевдокликабельные фрагменты (span имени канала с
        # унаследованным cursor:pointer) — после настоящих контролов: в
        # топ-30 они иначе вытесняют ссылку-заголовок карточки;
        # дальше в DOM-порядке; компактность важнее полноты
        pool = sorted(
            named,
            key=lambda it: (0 if (for_field and it.get("ed")) else 1,
                            0 if it.get("vp", True) else 1,
                            1 if _snap_frag(it) else 0))
        pool = pool[:LLM_WIDE_MAX]
        if not pool:
            return None, None
        lines = "\n".join(_cand_line(n, it) for n, it in enumerate(pool, 1))
        task = "select the input field" if for_field else "click"
        scope_hint = ""
        if not for_field:
            m_sc = _SCOPE_SPLIT_RE.match(" ".join(goal.split()))
            if m_sc:
                # Скоуп-форма «закрыть на корзина»: LLM ищет в списке всю
                # фразу и, не находя, отвечает «нет» — поясняем, что искомый
                # элемент может называться только действием («закрыть»)
                scope_hint = (f" (the element \"{m_sc.group(1)}\" belonging to "
                              f"\"{m_sc.group(2)}\"; it may be labelled "
                              f"just \"{m_sc.group(1)}\")")
        prompt = (
            f"Task: {task} \"{goal}\"{scope_hint}.\nPage elements:\n{lines}\n"
            f"Reply with ONLY one number (1-{len(pool)}) — the number of the matching "
            "element. If nothing matches — reply \"no\".\n"
            + user_language_line(detect_language(goal)))
        try:
            resp = router.get_response([{"role": "user", "content": prompt}],
                                       temperature=0.0, max_tokens=8, top_p=0.1,
                                       webchat_channel="cc", force_provider=getattr(router, "cc_provider", None))
        except Exception as e:
            logger.debug(f"[CompControl] Широкий LLM-резолв не удался: {e}")
            return None, None
        self.stats["llm_calls"] += 1
        meta: Dict[str, object] = {
            "path": "llm_wide",
            "candidates": [{"idx": int(it["idx"]),
                            "text": str(it.get("text") or "")[:60],
                            "score": 0.0} for it in pool[:LLM_TOP_N]],
            "llm_response": str(resp or "")[:200],
            # Сколько элементов реально ушло в промпт (после фильтра
            # служебных обрывков) и есть ли цель на странице вообще —
            # «нет» на отсутствующую цель не вето, а «нет в снапшоте»
            "n_pool": len(pool),
            "goal_absent": not self._goal_in_snapshot(goal, items, host)}
        m = re.fullmatch(r"\s*(\d{1,2})\s*", str(resp or ""))
        if m and 1 <= int(m.group(1)) <= len(pool):
            meta["picked_n"] = int(m.group(1))
            picked = pool[int(m.group(1)) - 1]
            # Zero-match промах: LLM ткнула в крестик/закрытие, хотя в цели
            # намерения закрывать нет («сырный в части слева» → «Закрыть»).
            # Общая проверка выбора по номеру, но БЕЗ сверки подписи с целью:
            # этот ярус и существует ради «другого названия» (скоринг тут
            # пуст именно потому, что подписи с целью не совпали).
            # Ветированный кандидат не отказ — каскад идёт на следующий ярус
            if self._veto_model_pick(goal, picked, meta, None,
                                     "широкий резолв", op=op,
                                     label_check=False):
                self.stats["llm_valid"] += 1
                meta["llm_response"] = f"{resp} → вето (закрытие без запроса)"
                return None, meta
            self.stats["llm_valid"] += 1
            idx = int(picked["idx"])
            logger.info(f"[CompControl] «{goal[:40]}» выбрано широким "
                        f"LLM-резолвом: кандидат {m.group(1)} (idx {idx})")
            return idx, meta
        if _llm_said_no(resp):
            self.stats["llm_valid"] += 1
            logger.info(f"[CompControl] Широкий LLM-резолв: нет подходящего "
                        f"элемента для «{goal[:40]}»")
        else:
            self.stats["llm_invalid"] += 1
            logger.info(f"[CompControl] Широкий LLM-резолв: ответ невалиден: "
                        f"{str(resp or '')[:60]!r}")
        return None, meta

    # ── Общий вход vision-ярусов (гибрид / рамки / зоны) ──
    # vis — состояние vision на ОДИН резолв (dict, заводит каскад): «скриншот
    # не снялся» (shot_failed), «vision-цепочка лежит» (down) и след ярусов
    # (tiers) для аудита. Экземпляр менеджера общий для конкурентных
    # запросов — поэтому состояние передаётся параметром, а не атрибутом

    def _vision_ready(self, router) -> bool:
        # Vision включён конфигом и есть у роутера.
        if not self.vision_fallback or router is None:
            return False
        try:
            return bool(router.supports_vision())
        except Exception:
            return False

    def is_private_page(self, host_or_url) -> bool:
        """Приватная страница (вход/оплата/банк или private_hosts конфига):
        её скриншоты и текст облачным/веб-чат моделям не отправляются."""
        from app.features.cc_privacy import is_private_page
        return is_private_page(host_or_url,
                               getattr(self, "private_hosts", ()),
                               getattr(self, "private_hosts_builtin", True))

    def _privacy_router(self, router, *where):
        """Роутер для LLM-решений по странице: на приватной — только
        локальная модель (PrivateRouter), vision выключен. where — полные
        URL и/или хосты страницы; приватен хоть один — обёртка (vk.com
        обычный, vk.com/im — переписка). Единая точка: передан только хост
        — проверяется и отслеживаемый URL чата на том же хосте, так что
        забытый на вызове полный адрес не отдаёт приватную страницу облаку.
        Отслеживаемый URL того же хоста добавляется и при полных адресах:
        адрес действия бывает адресом файла/вложения (download), а не
        страницы, на которой оно лежит."""
        from app.features.cc_privacy import (PrivateRouter, _host_of,
                                             private_router)
        if router is None or isinstance(router, PrivateRouter):
            return router
        cands = [str(w) for w in where if w]
        if cands:
            def _h(s: str) -> str:
                h = _host_of(s)[0]
                return h[4:] if h.startswith("www.") else h
            try:
                last = self._last_url
            except Exception:
                last = None
            if last and _h(str(last)) in {_h(c) for c in cands}:
                cands.append(str(last))
        return private_router(router, cands,
                              getattr(self, "private_hosts", ()),
                              getattr(self, "private_hosts_builtin", True))

    def _page_private(self, action) -> bool:
        """Страница действия приватная — по полным URL (value/url) и хосту,
        у multi — и вложенных действий (cc_privacy.page_candidates)."""
        from app.features.cc_privacy import page_candidates
        try:
            return any(self.is_private_page(c)
                       for c in page_candidates(action))
        except Exception:
            return False

    def _label_for_log(self, text, *where, idx=None, limit: int = 40) -> str:
        """Подпись элемента/вкладки для лога процесса (виден в /api/logs):
        на приватной странице (переписка, суммы, имена) — только номер или
        длина, иначе обрезанная. where — URL и/или хосты страницы; нет
        полного URL — берётся и отслеживаемый URL чата (того же хоста, а
        без where — любой), как в _privacy_router."""
        from app.features.cc_privacy import _host_of
        s = "" if text is None else str(text)
        cands = [str(w) for w in where if w]
        try:
            if not any("://" in c for c in cands):
                last = getattr(self, "_last_url", None)
                hosts = {_host_of(c)[0].removeprefix("www.") for c in cands}
                if last and (not cands or _host_of(str(last))[0]
                             .removeprefix("www.") in hosts):
                    cands.append(str(last))
            priv = any(self.is_private_page(c) for c in cands)
        except Exception:
            priv = True  # проверить не вышло — консервативно
        if priv:
            return f"#{idx}" if idx is not None else f"<{len(s)} симв.>"
        return s[:limit]

    def _vision_shot(self, host: str, tab_id, vis: dict) -> Optional[bytes]:
        """Скриншот вьюпорта для vision-яруса — один на резолв: и удачный
        кадр (vis["shot"]), и неудача запоминаются. screenshot_viewport уже
        ретраит внутри, а между ярусами одного резолва нет ни кликов, ни
        прокрутки (доскролл-поиск и повторный снапшот — ДО vision-ярусов,
        vis заводится после них) — кадр актуален. Ярус, который начнёт
        крутить страницу, обязан сбросить vis["shot"].
        Приватная страница — кадр не снимается вовсе: он ушёл бы
        vision-провайдеру (облако/веб-чат)."""
        if vis.get("shot_failed"):
            return None
        if vis.get("shot"):
            return vis["shot"]
        if host and self.is_private_page(host):
            vis["shot_failed"] = True
            vis["private"] = True
            self._note_tier(vis, "vision", {"fail": "private_page"})
            logger.info(f"[CompControl] {host} — приватная страница: "
                        "скриншот в vision не отправляю")
            return None
        from app.features import browser_actions as ba
        try:
            shot = ba.screenshot_viewport(host, tab_id)
        except Exception as e:
            logger.debug(f"[CompControl] Скриншот для vision не снят: {e}")
            shot = None
        if not shot:
            vis["shot_failed"] = True
            return None
        vis["shot"] = shot
        return shot

    def _vision_box_pool(self, host: str, tab_id, items: List[dict],
                         vis: dict) -> Tuple[Optional[bytes], List[dict], set]:
        """Кандидаты под рамки (гибрид и _visual_resolve): видимые и не
        мелкие, активный слой, центр внутри кадра скриншота, без дублей одной
        карточки. Центр проверяем по самому кадру (высота вьюпорта — из
        размера картинки и vw): у элементов iframe vp считается по окну
        фрейма, и рамка рисовалась за краем изображения.
        → (скриншот, кандидаты, idx схлопнутых дублей); скриншота нет —
        (None, [], set()): ярус не запускается."""
        eligible = [it for it in items
                    if it.get("vp") and (it.get("w") or 0) >= 10
                    and (it.get("h") or 0) >= 10]
        # Открытая панель/модалка поверх затемнённого фона (sc=0 — под
        # бэкдропом; cov — перекрыт чужим слоем): рамки тратим только на
        # активный слой; все вне слоя — не режем в ноль
        eligible = _active_layer(eligible)
        if not eligible:
            return None, [], set()
        shot = self._vision_shot(host, tab_id, vis)
        if not shot:
            return None, [], set()
        try:
            import io
            from PIL import Image
            iw, ih = Image.open(io.BytesIO(shot)).size
        except Exception:
            iw = ih = 0
        if iw and ih:
            vw0 = next((float(it.get("vw")) for it in eligible if it.get("vw")),
                       float(iw))
            vh = ih * vw0 / iw

            def _inside(it: dict) -> bool:
                cx = float(it.get("x") or 0) + float(it.get("w") or 0) / 2
                cy = float(it.get("y") or 0) + float(it.get("h") or 0) / 2
                return 0 <= cx < float(it.get("vw") or vw0) and 0 <= cy < vh

            eligible = [it for it in eligible if _inside(it)]
        pool = _dedup_same_target_cards(eligible)
        dup_ids = ({int(it["idx"]) for it in eligible}
                   - {int(it["idx"]) for it in pool})
        return shot, pool, dup_ids

    def _vision_call(self, prompt: str, boxed: bytes, shot: bytes, router,
                     vis: dict, tier: str) -> Optional[str]:
        """Vision-вызов с рамками + чистый кадр вторым изображением. None —
        vision-цепочка лежит: провайдер в карантине/недоступен отвечает
        None/пусто, а не исключением (supports_vision при vision: auto всё
        равно True). Такой резолв помечается vis["down"] — остальные
        vision-ярусы того же резолва не зовутся (та же мёртвая цепочка,
        до ~150 с каждый), а текстовый широкий резолв на cc_provider — да.
        Счётчик vision_calls — за каждый состоявшийся vision-ответ (llm_calls
        — только текстовые вызовы: их доля от выборов — метрика llm_share)."""
        try:
            resp = router.get_response_with_image(prompt, boxed,
                                                  image_mime="image/jpeg",
                                                  extra_image=shot,
                                                  force_provider=getattr(router, "vision_provider", None))
        except Exception as e:
            logger.debug(f"[CompControl] Vision-вызов ({tier}) не удался: {e}")
            resp = None
        if not str(resp or "").strip():
            vis["down"] = True
            self._note_tier(vis, tier, {"fail": "vision_down"})
            logger.info(f"[CompControl] Vision недоступен ({tier}) — "
                        "остальные vision-ярусы резолва пропускаю")
            return None
        self.stats["vision_calls"] += 1
        return str(resp)

    @staticmethod
    def _note_tier(vis: Optional[dict], path: str,
                   meta: Optional[dict]) -> None:
        """След яруса для аудита (meta["tiers"]): путь, начало ответа,
        уверенность, вето/сбой, бюджеты гибрида — по записи на каждый
        пройденный ярус, а не только на последний, иначе «нет» гибрида
        затирало бы вердикт зон."""
        if vis is None or meta is None:
            return
        rec: Dict[str, object] = {"path": path}
        if meta.get("llm_response"):
            rec["resp"] = str(meta["llm_response"])[:60]
        for k in ("conf", "veto", "fail", "n_boxes", "n_text", "picked_n",
                  "n_pool", "agree"):
            if meta.get(k) is not None:
                rec[k] = meta[k]
        vis.setdefault("tiers", []).append(rec)

    @staticmethod
    def _with_tiers(meta: Optional[dict], vis: dict) -> Optional[dict]:
        # След ярусов резолва — в итоговую мету (переживает замену meta).
        if isinstance(meta, dict) and vis.get("tiers"):
            meta["tiers"] = list(vis["tiers"])
        return meta

    def _hybrid_pick(self, goal: str, items: List[dict], host: str,
                     tab_id: Optional[int], router, op: str = "click",
                     vis: Optional[dict] = None
                     ) -> Tuple[Optional[int], Optional[dict]]:
        """Гибридный zero-match ярус (wide_mode: hybrid): ОДИН vision-вызов
        вместо пары «широкий текстовый резолв → vision-рамки». Модель видит
        скриншот вьюпорта с пронумерованными рамками видимых кандидатов
        (1..K) и текстовый список подписанных элементов без рамки — в
        основном вне экрана (K+1..N), нумерация сквозная. Выбор — DOM-метка
        (idx), а не координаты: вето и closed-loop проверка клика работают
        как у остальных ярусов; элемент вне экрана клик сам прокручивает во
        вьюпорт (до замера отпечатка — см. _click_cdp).
        → (idx, meta); (None, None) — ярус не запускался (vision выключен/
        недоступен, нет скриншота/рамок, Pillow не сработал) ИЛИ vision-
        цепочка лежит (vis["down"]) — каскад идёт раздельными ярусами
        (широкий текстовый резолв → vision-рамки);
        (None, meta) — vision ответила «нет», ответ невалиден
        (meta["fail"]="invalid") или выбор ветирован."""
        vis = vis if vis is not None else {}
        if vis.get("down") or not self._vision_ready(router):
            return None, None
        # Активный слой поверх затемнённого фона — как у остальных ярусов
        items = _active_layer(items)
        # Релевантность цели — тот же скоринг, что у выбора: в zero-match он
        # обычно пуст, но слабые (ниже порога лидера) баллы — всё равно
        # сигнал, кого ставить первым
        try:
            scored = self._score_candidates(items, goal, host=host, op=op) \
                or self._score_scoped(items, goal, op=op)
        except Exception:
            scored = []
        score = {int(it["idx"]): s for s, it in scored}
        pos = {int(it["idx"]): n for n, it in enumerate(items)}

        def _named(it: dict) -> bool:
            return bool(it.get("text") or it.get("aria") or it.get("title"))

        shot, pool, dup_ids = self._vision_box_pool(host, tab_id, items, vis)
        # Рамки: релевантность → безымянные (иконки — сопоставить их с целью
        # может только vision) → крупнее
        boxes = sorted(pool, key=lambda it: (
            -score.get(int(it["idx"]), 0.0), _named(it),
            -(float(it.get("w") or 0) * float(it.get("h") or 0))))
        boxes = boxes[:HYBRID_BOX_MAX]
        if not shot or not boxes:
            # Смотреть на скриншоте нечего — текстовый список дешевле
            # отработает широкий текстовый резолв
            return None, None
        boxed = _draw_candidate_boxes(shot, boxes)
        if boxed is None:
            return None, None
        box_ids = {int(it["idx"]) for it in boxes}
        # Текстовые строки — подписанные элементы без рамки: в основном вне
        # экрана, плюс видимые, что не влезли в бюджет рамок (дубли карточек,
        # схлопнутые в рамку, не повторяем). Порядок: релевантность →
        # видимые раньше (иначе видимая подписанная цель выпадала и из рамок,
        # и из строк) → псевдокликабельные фрагменты последними (как в
        # _llm_wide_pick) → DOM. Без строк: llm_wide_resolve=false (текстовый
        # ярус выключен) и цель-иконка — подписи у иконки нет, строки лишь
        # подсунут модели чужой подписанный элемент (как пропуск текстового
        # широкого резолва для иконок, _ICON_WORD_ROOTS)
        rows: List[dict] = []
        if self.llm_wide_resolve and not _icon_goal(goal):
            rows = [it for it in items if _named(it)
                    and int(it["idx"]) not in box_ids
                    and int(it["idx"]) not in dup_ids]
            rows.sort(key=lambda it: (
                -score.get(int(it["idx"]), 0.0),
                0 if it.get("vp", True) else 1,
                1 if _snap_frag(it) else 0,
                pos.get(int(it["idx"]), 0)))
            rows = rows[:HYBRID_TEXT_MAX]
        k = len(boxes)
        verb = "hover the cursor over" if op == "hover" else "click"
        # Цель — в пределах 200 символов: иначе бюджет промпта не жёсткий
        g_short = goal if len(goal) <= 200 else goal[:200] + "…"
        scope_hint = ""
        m_sc = _SCOPE_SPLIT_RE.match(" ".join(g_short.split()))
        if m_sc:
            # Скоуп-форма «закрыть на корзина» — как в _llm_wide_pick
            scope_hint = (f" (the element \"{m_sc.group(1)}\" belonging to "
                          f"\"{m_sc.group(2)}\"; it may be labelled "
                          f"just \"{m_sc.group(1)}\")")
        lang_line = user_language_line(detect_language(goal))

        def _prompt(lab_box: int, lab_row: int, rows_: List[dict]) -> str:
            parts = [
                f"Task: {verb} \"{g_short}\"{scope_hint}.",
                "A screenshot of the browser page. Colored boxes with numbered "
                f"badges mark elements 1..{k} (the number is in the badge of the "
                "box's color):",
                "\n".join(_cand_line(n, it, lab_box)
                          for n, it in enumerate(boxes, 1)),
                "If a second screenshot is attached, it is the same page without "
                "markup: check it for what is covered by the boxes and badges."]
            if rows_:
                parts.append("Elements without a box (mostly below the screen):")
                parts.append("\n".join(
                    _cand_line(k + n, it, lab_row)
                    # Видимый, но без рамки (не влез в бюджет рамок)
                    + (" — on screen, no box" if it.get("vp", True) else "")
                    for n, it in enumerate(rows_, 1)))
            # Уверенность C — данные для настройки (пишется в аудит), на
            # приём выбора не влияет. Формат — описанием, без числа-образца:
            # с примером «C=0.8» модели повторяли именно 0.8
            parts.append(
                f"Answer — ONLY the number of the matching element (1-{k + len(rows_)}), "
                "then, after a space, C=<confidence from 0 to 1>. "
                "If nothing matches — reply \"no\".")
            parts.append(lang_line)
            return "\n".join(parts)

        # Лимит промпта: сначала короче подписи, затем без самых слабых строк
        # (они в хвосте — нумерация оставшихся не сдвигается), в крайнем
        # случае — ещё короче подписи рамок
        prompt = _prompt(60, 120, rows)
        if len(prompt) > HYBRID_PROMPT_MAX:
            prompt = _prompt(40, 60, rows)
        while len(prompt) > HYBRID_PROMPT_MAX and rows:
            rows = rows[:-1]
            prompt = _prompt(40, 60, rows)
        if len(prompt) > HYBRID_PROMPT_MAX:
            prompt = _prompt(20, 60, rows)
        resp = self._vision_call(prompt, boxed, shot, router, vis,
                                 "vision_hybrid")
        if resp is None:
            return None, None  # vision лежит — каскад: текстовый широкий
        numbered = boxes + rows
        # Строгая грамматика — для любого номера; нестрогий разбор («3 —
        # кнопка «Оплата»», «Тариф 5 ГБ») принимаем, только если он указал на
        # РАМКУ С ПОДПИСЬЮ: её выбор ниже проходит строгую сверку подписи с
        # целью, так что эхо чужой подписи не станет кликом. Строка списка и
        # безымянная рамка сверки не имеют — для них только строгий ответ
        num, conf, said_no = _parse_pick_answer(resp, strict=True)
        loose = False
        if num is None and not said_no:
            n2, c2, _no2 = _parse_pick_answer(resp)
            if n2 is not None and 1 <= n2 <= k and _named(boxes[n2 - 1]):
                num, conf, loose = n2, c2, True
        # off — строка текстового списка (без рамки: вне экрана или не влезла)
        meta: Dict[str, object] = {
            "path": "vision_hybrid",
            "candidates": [{"idx": int(it["idx"]),
                            "text": str(it.get("text") or "")[:60],
                            "score": round(score.get(int(it["idx"]), 0.0), 1),
                            "off": n > k}
                           for n, it in enumerate(numbered[:10], 1)],
            "llm_response": resp[:200], "conf": conf,
            "wide_mode": "hybrid", "n_boxes": k, "n_text": len(rows)}
        if said_no:
            self.stats["vision_valid"] += 1
            logger.info(f"[CompControl] Гибридный ярус: нет подходящего "
                        f"элемента для «{goal[:40]}»")
            return None, meta
        if num is None or not 1 <= num <= len(numbered):
            self.stats["vision_invalid"] += 1
            # Невалидный ответ — не вердикт: каскад ещё спросит текстовый
            # широкий резолв (другой провайдер), _visual_resolve — нет
            # (гибрид — его надмножество)
            meta["fail"] = "invalid"
            logger.info(f"[CompControl] Гибридный ярус: ответ невалиден: "
                        f"{resp[:60]!r}")
            return None, meta
        self.stats["vision_valid"] += 1
        picked = numbered[num - 1]
        row = num > k
        meta["picked_n"] = num
        if loose:
            meta["loose_parse"] = True
        if row:
            meta["row"] = True
        if not picked.get("vp", True):
            meta["offscreen"] = True
        # Сверка подписи с целью — по источнику выбора:
        # • строка текстового списка — как у широкого текстового резолва: он
        #   существует ради «другого названия», сверки нет (label_check=False),
        #   зато ответ — только строгой грамматики (выше);
        # • рамка с подписью — строго ВСЕГДА, как у _visual_resolve:
        #   модель иногда указывает по картинке номер, чья подпись цели не
        #   соответствует (галлюцинация). Заявленная уверенность не
        #   освобождает: самооценка C кучкуется на 0.9–0.95 и у галлюцинаций.
        #   «Другое название» у видимого элемента не теряется: вето
        #   label_mismatch каскад отдаёт текстовому широкому резолву (независимая модель, один вызов);
        # • безымянная рамка (иконка) — сверять не с чем, как в _visual_resolve.
        # Вето разрушительного — в силе всегда. Ветированный кандидат не
        # отказ — каскад идёт на следующий ярус
        if self._veto_model_pick(goal, picked, meta, host, "гибрид", op=op,
                                 label_check=not row):
            if meta.get("veto") == "label_mismatch" and picked.get("vp", True):
                # Запоминаем ветированную рамку: если зональный vision
                # независимо укажет на неё же — это согласие двух ответов,
                # а не вторая галлюцинация (см. _vision_zones)
                vis["label_vetoed"] = picked
                # vis общий на попытки «закрой X» с разными целями —
                # согласие засчитываем только для той же цели
                vis["label_vetoed_goal"] = goal
            return None, meta
        idx = int(picked["idx"])
        logger.info(f"[CompControl] «{goal[:40]}» выбрано гибридным ярусом: "
                    f"кандидат {num}{' (строка)' if row else ''} "
                    f"(idx {idx}, C={conf})")
        return idx, meta

    def _visual_resolve(self, host: str, tab_id: Optional[int],
                        items: List[dict], goal: str, router,
                        op: str = "click", vis: Optional[dict] = None):
        """Визуальный фолбэк резолва: весь текстовый скоринг бессилен,
        когда у кандидатов пустые accessible name (иконочные тулбары). Скриншот
        вьюпорта + пронумерованные рамки кандидатов → vision-модель выбирает
        номер (тот же договор, что top-5 у текстовой LLM, но по картинке).
        Перед нарезкой топ-8 — дедуп дублей одной карточки по ссылке/тексту
        (_dedup_same_target_cards): иначе обёртка+заголовок+строка метаданных
        одной карточки конкурируют за номера и вытесняют соседние элементы.
        → (idx, meta); (None, None) — ярус не запускался (фича выключена,
        нет vision, нет скриншота/кандидатов) или vision лежит (vis["down"]);
        (None, meta) — vision ответила «нет»/невалидно или выбор ветирован."""
        vis = vis if vis is not None else {}
        if vis.get("down") or not self._vision_ready(router):
            return None, None
        shot, cands, _dups = self._vision_box_pool(host, tab_id, items, vis)
        # Безымянные впереди (именно для них фолбэк), затем по убыванию площади
        cands.sort(key=lambda it: (bool(it.get("text") or it.get("aria")),
                                   -(float(it.get("w") or 0)
                                     * float(it.get("h") or 0))))
        cands = cands[:8]
        if not shot or not cands:
            return None, None
        boxed = _draw_candidate_boxes(shot, cands)
        if boxed is None:
            return None, None
        lines = "\n".join(
            f"{n}) {str(it.get('text') or it.get('aria') or it.get('tag') or '?')[:40]}"
            for n, it in enumerate(cands, 1))
        prompt = (
            f"A screenshot of the browser page. Colored boxes with numbered "
            f"badges mark elements 1..{len(cands)} (the number is in the badge "
            f"of the box's color):\n{lines}\n"
            f"Which of them is \"{goal[:200]}\"? Reply with ONLY the number. "
            "If nothing matches — reply \"no\".\n"
            "If a second screenshot is attached, it is the same page without "
            "markup: check it for what is covered by the boxes and badges.\n"
            + user_language_line(detect_language(goal)))
        resp = self._vision_call(prompt, boxed, shot, router, vis, "vision")
        if resp is None:
            return None, None  # vision лежит — не вердикт «нет»
        meta: Dict[str, object] = {
            "path": "vision",
            "candidates": [{"idx": int(it["idx"]),
                            "text": str(it.get("text") or "")[:60],
                            "score": 0.0} for it in cands],
            "llm_response": resp[:200]}
        m = re.fullmatch(r"\s*(\d{1,2})\s*", resp)
        if not m or not (1 <= int(m.group(1)) <= len(cands)):
            # Vision посмотрела и сказала «нет» (или ответила невалидно) —
            # вердикт в аудит, иначе отказ неотличим от «ярус не запускался»
            if _llm_said_no(resp):
                self.stats["vision_valid"] += 1
            else:
                self.stats["vision_invalid"] += 1
            return None, meta
        self.stats["vision_valid"] += 1
        meta["picked_n"] = int(m.group(1))
        picked = cands[int(m.group(1)) - 1]
        # Галлюцинация номера: модель обязана ответить «нет», когда цели нет
        # среди рамок, но иногда всё равно указывает на визуально похожий,
        # но неверный кандидат. Проверка та же, что у всех выборов по номеру
        # (_veto_model_pick): ветированный кандидат
        # не отказ — каскад идёт на следующий ярус
        if self._veto_model_pick(goal, picked, meta, host, "vision", op=op):
            return None, meta
        idx = int(picked["idx"])
        logger.info(f"[CompControl] «{goal[:40]}» выбрано визуально: "
                    f"кандидат {m.group(1)} (idx {idx})")
        return idx, meta

    def _vision_zones(self, goal: str, host: str, tab_id, router,
                      op: str = "click", vis: Optional[dict] = None):
        """Зональный vision-фолбэк — последний шанс, когда DOM нечитаем
        совсем (canvas/WebGL, ARIA-скрытая разметка): рамки вокруг ВСЕХ
        визуально кликабельных зон вьюпорта (не только кандидатов скоринга),
        крупный canvas — сеткой 3×3. Клик по выбранной зоне — по КООРДИНАТАМ
        (DOM-метки у зоны нет), closed-loop внутри click_at_point.
        → ({"x","y","label"}, meta); (None, None) — ярус не запускался
        (фича выключена, нет vision/скриншота/зон, антибот) или vision лежит
        (vis["down"]); (None, meta) — ответ «нет»/невалиден или выбор
        ветирован."""
        vis = vis if vis is not None else {}
        if vis.get("down") or not self._vision_ready(router):
            return None, None
        from app.features import browser_actions as ba
        # Капчу vision-кликами не «прожимаем»: антибот прячет семантику
        # осознанно — остаётся честный отказ (каскад выше уже дал
        # fail_reason="captcha", это страховка прямого входа)
        try:
            if ba.detect_antibot(host, tab_id):
                return None, None
        except Exception:
            pass
        shot = self._vision_shot(host, tab_id, vis)
        if not shot:
            return None, None
        try:
            boxes = ba.all_clickable_boxes(host, tab_id=tab_id)
        except Exception as e:
            logger.debug(f"[CompControl] Зональный vision-фолбэк недоступен: {e}")
            return None, None
        if not boxes:
            return None, None
        boxed = _draw_candidate_boxes(shot, boxes)
        if boxed is None:
            return None, None
        lines = "\n".join(
            f"{n}) {str(b.get('text') or '').strip() or f'zone {n}'}"
            for n, b in enumerate(boxes, 1))
        prompt = (
            f"Task: click \"{goal[:200]}\".\n"
            "A screenshot of the browser page. Colored boxes with numbered "
            "badges are clickable zones "
            f"1..{len(boxes)} (the number is in the badge of the box's color):\n{lines}\n"
            "Reply with ONLY the number of the zone to click. "
            "If there is no matching zone — reply \"no\".\n"
            "If a second screenshot is attached, it is the same page without "
            "markup: check it for what is covered by the boxes and badges.\n"
            + user_language_line(detect_language(goal)))
        resp = self._vision_call(prompt, boxed, shot, router, vis,
                                 "vision_zones")
        if resp is None:
            return None, None  # vision лежит — не вердикт «нет»
        meta: Dict[str, object] = {
            "path": "vision_zones",
            "candidates": [{"idx": n,
                            "text": str(b.get("text") or "")[:60],
                            "score": 0.0}
                           for n, b in enumerate(boxes[:8], 1)],
            "llm_response": resp[:200]}
        m = re.fullmatch(r"\s*(\d{1,2})\s*", resp)
        if not m or not (1 <= int(m.group(1)) <= len(boxes)):
            if _llm_said_no(resp):
                self.stats["vision_valid"] += 1
            else:
                self.stats["vision_invalid"] += 1
            return None, meta
        self.stats["vision_valid"] += 1
        meta["picked_n"] = int(m.group(1))
        box = boxes[int(m.group(1)) - 1]
        lv = vis.get("label_vetoed")
        if isinstance(lv, dict) and lv.get("idx") is not None \
                and vis.get("label_vetoed_goal", goal) == goal \
                and _boxes_coincide(box, lv) and _zone_same_label(box, lv) \
                and not self._veto_destructive(goal, lv, meta,
                                               "vision_zones", op=op):
            # Гибрид выбрал этот же элемент, и его ветировала сверка подписи
            # (открытый бургер рисуется крестиком): два независимых
            # vision-ответа согласны — берём элемент гибрида по DOM-метке,
            # но только с подтверждением человеком
            lab = str(lv.get("text") or lv.get("aria") or lv.get("title")
                      or goal)
            meta["agree_idx"] = int(lv["idx"])
            meta["agree"] = "hybrid+zones"
            meta["force_confirm"] = True
            point = {"x": float(box.get("x") or 0) + float(box.get("w") or 0) / 2,
                     "y": float(box.get("y") or 0) + float(box.get("h") or 0) / 2,
                     "label": lab[:80], "zone": int(m.group(1))}
            if isinstance(box.get("sig"), dict):
                point["sig"] = box["sig"]  # сверка перед кликом (_dispatch)
            meta["point"] = point
            logger.info(f"[CompControl] «{goal[:40]}»: зоны и гибрид согласны "
                        f"на «{self._label_for_log(lab, host)}» — беру с "
                        "подтверждением")
            return point, meta
        # Та же проверка выбора по номеру, что у визуального фолбэка: зоны
        # DOM-метки не имеют, и клик по ним координатный — галлюцинация
        # номера тут особенно дорога. Судим по подписи зоны (безымянная —
        # законная цель: ровно для неё зональный ярус и нужен)
        if self._veto_model_pick(goal, {"text": str(box.get("text") or "")},
                                 meta, host, "vision_zones", op=op):
            return None, meta
        label = str(box.get("text") or "").strip()
        if not label:
            # Безымянная зона: «Нажать "зона 27"?» ничего не скажет
            # пользователю. Миниатюра рядом с названием видео — подписываем
            # ближайшей подписанной зоной; совсем пусто — словами самой цели
            neighbor = self._nearest_zone_label(boxes, int(m.group(1)) - 1)
            label = (f"зона рядом с «{neighbor[:50]}»" if neighbor
                     else f"«{goal[:40]}» (зона {m.group(1)})")
        point = {"x": float(box.get("x") or 0) + float(box.get("w") or 0) / 2,
                 "y": float(box.get("y") or 0) + float(box.get("h") or 0) / 2,
                 "label": label, "zone": int(m.group(1))}
        if isinstance(box.get("sig"), dict):
            # Отпечаток центра зоны со времени скриншота: после
            # подтверждения _dispatch сверяет его перед координатным кликом
            point["sig"] = box["sig"]
        meta["point"] = point
        logger.info(f"[CompControl] «{goal[:40]}» выбрано зональным vision: "
                    f"зона {m.group(1)} «{self._label_for_log(label, host)}»")
        return point, meta

    @staticmethod
    def _nearest_zone_label(boxes: List[dict], i: int) -> Optional[str]:
        """Подпись ближайшей к boxes[i] ПОДПИСАННОЙ зоны (по центрам) —
        чем подписать безымянную зону в вопросе подтверждения."""
        bx = boxes[i]
        cx = float(bx.get("x") or 0) + float(bx.get("w") or 0) / 2
        cy = float(bx.get("y") or 0) + float(bx.get("h") or 0) / 2
        best, bd = None, None
        for j, b in enumerate(boxes):
            if j == i:
                continue
            t = str(b.get("text") or "").strip()
            if not t:
                continue
            dx = float(b.get("x") or 0) + float(b.get("w") or 0) / 2 - cx
            dy = float(b.get("y") or 0) + float(b.get("h") or 0) / 2 - cy
            d = dx * dx + dy * dy
            if bd is None or d < bd:
                best, bd = t, d
        return best

    def _element_on_other_pages(self, goal: str, cur_url: str,
                                only_host: Optional[str] = None,
                                op: str = "click",
                                deadline: Optional[float] = None):
        """Кросс-страничный поиск элемента: снапшот каждой открытой страницы
        (кроме текущей, чата и пустых; only_host — только вкладки этого сайта:
        матч по СЕМЕЙСТВУ хостов — поддомены одного сайта тоже считаются им же),
        детерминированный явный лидер ровно на одной странице →
        (url, host, items, idx, tab_id, meta); иначе None.
        Снапшот целим по полному URL (host_part — подстрока URL): по голому
        хосту page_for взял бы одну и ту же вкладку для всех страниц сайта."""
        from app.features import browser_actions as ba
        try:
            pages = ba.list_pages()
        except Exception:
            return None
        site = ba._site_key(only_host) if only_host else None
        found = None
        for purl, phost in pages:
            if deadline is not None and time.monotonic() >= deadline:
                break  # бюджет каскада: снапшот каждой вкладки не бесплатен
            if not phost or purl == cur_url:
                continue
            if phost in ("localhost", "127.0.0.1"):
                continue  # чат и служебное — не кликаем
            if only_host:
                # Только свой сайт: по семейству, а при хосте без семейства
                # (голый лейбл/IP) — точное совпадение
                if site:
                    if not ba._host_matches_site(purl, site):
                        continue
                elif phost != only_host:
                    continue
            try:
                u2, h2, items2 = ba.snapshot_elements(purl)
            except Exception:
                continue
            scored = self._score_candidates(items2, goal, host=h2, op=op) \
                or self._score_scoped(items2, goal, op=op)
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
            # Клик в невидимой вкладке пользователь не видит — такую цель
            # называем в ответе и спрашиваем подтверждение (other_tab →
            # force_confirm в resolve_click); видимая вкладка — как обычно
            try:
                vis_p = ba.visible_page_info()
            except Exception:
                vis_p = None
            if not (vis_p and vis_p[0] == found[0]):
                found[5]["other_tab"] = True
                found[5]["force_confirm"] = True
        return found

    @staticmethod
    def _non_click_hint(goal: str, lang: Optional[str] = None
                        ) -> Optional[str]:
        """Цель — не элемент страницы: подсказка, как сказать правильно
        (на языке lang); None — обычная цель клика."""
        from app.features import cc_texts
        g = " ".join(str(goal or "").strip(" .,!?«»\"'").split())
        if not g:
            return None
        if _NON_CLICK_KEY_RE.match(g):
            return cc_texts.t("rs_hint_key", lang, goal=g)
        if _NON_CLICK_SCROLL_RE.match(g):
            return cc_texts.t("rs_hint_scroll", lang)
        m = _NON_CLICK_SITE_RE.match(g)
        if m:
            return cc_texts.t("rs_hint_site", lang, site=m.group(1))
        if _NON_CLICK_ONOMATOPOEIA_RE.match(g):
            return cc_texts.t("rs_hint_noise", lang, goal=g)
        return None

    @staticmethod
    def _act_from_meta(act: dict, meta: Optional[dict],
                       tab_id: Optional[int]) -> dict:
        """Флаги резолва — на уровень действия: needs_confirm смотрит
        action["force_confirm"], а не choose. Без этого непроверенная подпись,
        согласие гибрида и зон и клик в НЕвидимой вкладке (other_tab)
        исполнялись бы молча у персоны с confirm:false. Хост вкладки
        подтверждение называет само (describe)."""
        if tab_id is not None:
            act["tab_id"] = tab_id
        if isinstance(meta, dict):
            if meta.get("force_confirm") or meta.get("other_tab"):
                act["force_confirm"] = True
            if meta.get("retried"):
                act["retried"] = True
        return act

    @staticmethod
    def _with_labels(act: dict, item: Optional[dict]) -> dict:
        """aria/title элемента — в действие: у иконки без текста element
        = «#idx», и risky_label без них не видел «Оплатить»/«Удалить»."""
        for k in ("aria", "title"):
            v = " ".join(str((item or {}).get(k) or "").split())
            if v:
                act[k] = v[:80]
        return act

    @_in_chat
    def resolve_click(self, goal: str, site_word: Optional[str],
                      router, chat_id: str = ""
                      ) -> Tuple[Optional[dict], Optional[str]]:
        """«нажми „скачать“ (на сайте)» → (действие click, None) или
        (None, текст причины). «выбрать на Цезарь с беконом» — скоуп-клик:
        кнопка ищется в контексте карточки. «закрой окно» — крестик;
        «закрой соусы к бортикам» — целевое закрытие: крестик в контексте
        названного блока, промах — обычный крестик. LLM-путь клик не получает
        никогда: «сыграть» его он может."""
        # Цель — не элемент страницы (клавиша, листание, сайт, звукоподражание):
        # весь каскад (снапшоты, LLM, vision) отработал бы впустую и кончился
        # «не нашёл» — сразу честный отказ с подсказкой
        hint = self._non_click_hint(goal, self.turn_lang())
        if hint:
            self._audit_resolve(chat_id, goal, self._last_host, hint,
                                "not_a_click")
            return None, hint
        # «закрыть на на джем» — сдвоенный предлог из склейки скопа
        goal = _DUP_PREP_RE.sub(r"\1", goal)
        if site_word and site_word != PAGE_REF:
            k = self._lookup(self.sites, " ".join(site_word.lower().split()))
            if not k and not _looks_like_domain(site_word) \
                    and site_word.lower() not in _NOOP_SITE_WORDS:
                # «на маргарите» — не алиас и не домен: это скоп карточки,
                # а не сайт; возвращаем слово в цель скоупа
                goal = f"{goal} на {site_word}"
                site_word = None
        # Номерная команда («первое видео в shorts», «третий результат») —
        # рецепт по разметке полки/выдачи, «следующее видео» — рецепт
        # youtube_next: детерминированно, без скоринга и нейронки; скоуп
        # («в shorts») ordinal_recipe срезает сам
        oc = ordinal_recipe(" ".join(goal.split())) \
            or next_video_recipe(" ".join(goal.split()))
        if oc is not None:
            return {"kind": "task", "key": goal, "value": f"recipe:{oc}"}, None
        goal_n = " ".join(goal.lower().split())
        # «нажми i»: однобуквенное имя иконки не переживает фильтр слов
        # (len>=3) — подменяем на полное («информация»)
        if goal_n in _GOAL_ALIAS:
            goal = _GOAL_ALIAS[goal_n]
            goal_n = " ".join(goal.lower().split())
        close_m = _CLOSE_VERB_RE.match(goal_n)
        close_goal = bool(close_m) or bool(_CLOSE_GOAL_RE.search(goal_n))
        close_obj = close_m.group(1).strip() if close_m else ""
        # «закрыть на джем» (скоуп-форма от разбора) — объект без предлога,
        # иначе целевое закрытие строило «закрыть на на джем»
        close_obj = re.sub(r"^(?:на|в|во|у)\s+", "", close_obj,
                           flags=re.IGNORECASE)
        if close_goal:
            # «закрытие модального окна» → «закрыть»: ищем крестик, а не текст.
            # Авто-закрытие оверлея перед снапшотом тут ВЫКЛЮЧАЕМ: оно съело
            # бы крестик раньше явной команды, и «закрой окно» ответило бы
            # «не нашёл» на уже закрытом попапе. Подмену смысла команды
            # логируем: она превращает клик в разрушительный
            logger.info(f"[CompControl] Цель «{goal[:40]}» разобрана как "
                        "закрытие → ищу крестик")
            goal = "закрыть"
        # Корзинный фолбэк ДО текстового резолва: «удали чикен» /
        # «нажми удалить чикен» / «закрой чикен» / «изменить состав в
        # гавайская» / «нажми + в двойная пепперони» при открытой корзине.
        # Крестик товара и −/+ — иконки без текста и aria, «Изменить»
        # теряется среди одноимённых ссылок других рядов (а целевое
        # «закрыть на чикен» и вовсе цеплялось за крестик ВСЕЙ панели —
        # в её ctx названия товаров). cart_op находит карточку по названию
        # и жмёт контрол по позиции. Только когда товар реально виден в
        # корзине — иначе обычный путь (подписанные «Удалить …» вне
        # магазина, инвентарь бота и т.п. не задеваем)
        cart_probe = ""
        cart_op_kind = "remove"
        if close_goal and close_obj and not _CLOSE_GENERIC_RE.fullmatch(close_obj):
            cart_probe = close_obj
        elif not close_goal:
            m_rm = _CART_REMOVE_GOAL_RE.match(goal_n)
            m_ed = None if m_rm else _CART_EDIT_GOAL_RE.match(goal_n)
            m_qty = (None if (m_rm or m_ed)
                     else (_CART_INC_GOAL_RE.match(goal_n)
                           or _CART_DEC_GOAL_RE.match(goal_n)))
            if m_rm:
                cart_probe = m_rm.group(1)
            elif m_ed:
                cart_probe, cart_op_kind = m_ed.group(1), "edit"
            elif m_qty:
                cart_probe = m_qty.group(1)
                cart_op_kind = ("decrease"
                                if goal_n[:1] in ("-", "−", "–")
                                or goal_n.startswith("минус")
                                else "increase")
        if cart_probe:
            cart_act = self._cart_op_fallback(cart_probe, cart_op_kind,
                                              site_word, chat_id)
            if cart_act is not None:
                return cart_act, None
            if cart_op_kind == "edit":
                # Не корзина — страница/модалка продукта: ссылка «Изменить
                # состав» слота (комбо), товар по контексту предка
                comp_act, comp_err = self._comp_edit_fallback(
                    cart_probe, site_word, chat_id)
                if comp_act is not None:
                    return comp_act, None
                if comp_err:
                    # Неоднозначность («несколько Изменить состав») —
                    # честный вопрос лучше промаха в инфо-иконку
                    return None, comp_err
        # Попытки одного запроса («закрой X»: целевой крестик → общий →
        # «свернуть») делят снапшот, дедлайн бюджета и vision-кадр
        share: dict = {}
        if close_goal and close_obj and not _CLOSE_GENERIC_RE.fullmatch(close_obj):
            # Целевое закрытие («закрой соусы к бортикам»): крестик в контексте
            # названного блока — скоуп-форма «закрыть на X» (крестик модалки
            # соусов, а не «×» у товара в корзине). Промах — ниже обычный
            # крестик общим проходом
            r = self._resolve_element(f"закрыть на {close_obj}", site_word,
                                      router, chat_id=chat_id,
                                      auto_dismiss=False, share=share)
            if r[6] is None and r[3] is not None:
                url, host, items, idx, tab_id, meta, _ = r
                item = self._element_by_idx(items, idx) or {}
                text = str(item.get("text") or "")
                logger.info(f"[CompControl] Целевое закрытие "
                            f"«{close_obj[:40]}» → [{idx}] "
                            f"{self._label_for_log(text, url, host)} "
                            f"на {host} (путь: {meta.get('path')})")
                act = {"kind": "click", "idx": idx,
                       "element": text or f"#{idx}", "host": host,
                       "value": url, "choose": meta,
                       "goal": f"закрыть на {close_obj}"}
                self._with_labels(act, item)
                return self._act_from_meta(act, meta, tab_id), None
        url, host, items, idx, tab_id, meta, err = self._resolve_element(
            goal, site_word, router, chat_id=chat_id,
            auto_dismiss=not close_goal, share=share)
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
                        auto_dismiss=False, share=share)
                    if r[6] is None and r[3] is not None:
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
                        self._with_labels(act3, it)
                        return self._act_from_meta(act3, meta2, tab_id2), None
                # Крестика нет в снапшоте (модалки без close-контрола —
                # анкеты, шторки подтверждений, открытые меню YouTube
                # без Х внутри): если диалог/меню реально виден, закрываем
                # клавишей Escape — «нажми крестик» при открытом меню это
                # и значит
                act2 = self._escape_fallback(site_word, chat_id)
                if act2 is not None:
                    return act2, None
            return None, err
        point = (meta or {}).get("point")
        if idx is None and point:
            # Зональный vision-фолбэк: зона без DOM-метки (canvas/WebGL) —
            # клик по координатам центра зоны (диспетчер — по action["point"])
            act = {"kind": "click", "point": point,
                   "element": str(point.get("label") or goal)[:80],
                   "host": host, "value": url, "choose": meta, "goal": goal}
            return self._act_from_meta(act, meta, tab_id), None
        item = self._element_by_idx(items, idx) or {}
        text = str(item.get("text") or "")
        logger.info(f"[CompControl] Клик «{goal[:40]}» → [{idx}] "
                    f"{self._label_for_log(text, url, host)} "
                    f"на {host} (путь: {meta.get('path')})")
        act = {"kind": "click", "idx": idx, "element": text or f"#{idx}",
               "host": host, "value": url, "choose": meta, "goal": goal}
        self._with_labels(act, item)
        return self._act_from_meta(act, meta, tab_id), None

    @_in_chat
    def resolve_hover(self, goal: str, site_word: Optional[str],
                      router, chat_id: str = ""
                      ) -> Tuple[Optional[dict], Optional[str]]:
        """«наведи (курсор) на меню/видео» → (действие hover, None) или
        (None, причина). Тот же резолв элемента, что у клика (включая ярус
        видимой страницы), но исполнение — движение мыши без клика:
        раскрыть hover-меню, hover-кнопки карточки, свёрнутый слайдер.
        Спецпутей клика (корзина/закрытие/ordinal) нет: наведение ничего
        не активирует."""
        if site_word and site_word != PAGE_REF:
            k = self._lookup(self.sites, " ".join(site_word.lower().split()))
            if not k and not _looks_like_domain(site_word) \
                    and site_word.lower() not in _NOOP_SITE_WORDS:
                # «наведи на X на маргарите» — скоп, а не сайт (как у клика)
                goal = f"{goal} на {site_word}"
                site_word = None
        # op="hover": наведение ничего не активирует — вето на
        # разрушительный контрол тут не работает («наведи на очистить
        # очередь» — законная просьба, отказ был строже нужного)
        url, host, items, idx, tab_id, meta, err = self._resolve_element(
            goal, site_word, router, chat_id=chat_id, op="hover")
        if err:
            return None, err
        point = (meta or {}).get("point")
        if idx is None and point:
            # Зона зонального vision-фолбэка без DOM-метки — наведение по
            # координатам центра зоны
            act = {"kind": "hover", "point": point,
                   "element": str(point.get("label") or goal)[:80],
                   "host": host, "value": url, "choose": meta, "goal": goal}
            return self._act_from_meta(act, meta, tab_id), None
        item = self._element_by_idx(items, idx) or {}
        text = str(item.get("text") or "")
        logger.info(f"[CompControl] Наведение «{goal[:40]}» → [{idx}] "
                    f"{self._label_for_log(text, url, host)} на {host} "
                    f"(путь: {meta.get('path')})")
        act = {"kind": "hover", "idx": idx, "element": text or f"#{idx}",
               "host": host, "value": url, "choose": meta, "goal": goal}
        self._with_labels(act, item)
        return self._act_from_meta(act, meta, tab_id), None

    @_in_chat
    def resolve_download(self, goal: str, site_word: Optional[str],
                         router, chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """«скачай „методичку по sql" (на example.edu)» → (download-действие,
        None) или (None, причина). Тот же снапшот/скоринг, что у клика; у
        найденного элемента берём href по его сквозному номеру разметки —
        неважно, общий снапшот его пометил или целевой; без href (иконка
        меню, кнопка) честный отказ: скачивать нечего."""
        url, host, items, idx, tab_id, meta, err = self._resolve_element(
            goal, site_word, router, chat_id=chat_id)
        if err:
            return None, err
        if idx is None:
            # Зональный vision-фолбэк дал координатную зону — у неё нет href
            return None, self._tx("rs_not_file_link", name=goal[:60])
        item = self._element_by_idx(items, idx) or {}
        text = str(item.get("text") or "")
        from app.features.browser_actions import href_of_tagged
        href = href_of_tagged(host, idx, tab_id=tab_id)
        if not href.startswith(("http://", "https://")):
            logger.info(f"[CompControl] Скачивание «{goal[:40]}»: у [{idx}] нет href")
            return None, self._tx("rs_not_file_link", name=text or goal)
        logger.info(f"[CompControl] Скачивание «{goal[:40]}» → [{idx}] "
                    f"{self._label_for_log(text, url, host)} ({href[:60]})")
        # value — адрес СТРАНИЦЫ (url — файл): приватность flavor/аудита
        # смотрит страницу, где лежит вложение (vk.com/im), а не только файл
        act = {"kind": "download", "url": href, "value": url,
               "element": text or f"#{idx}", "host": host, "choose": meta}
        return self._act_from_meta(act, meta, tab_id), None

    @_in_chat
    def resolve_read(self, mode: str, site_word: Optional[str],
                     chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """«прочитай последнее сообщение (на почте)» → (read-действие, None)
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

    @_in_chat
    def resolve_zoom(self, direction: str, site_word: Optional[str],
                     chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """«увеличь/уменьши/сбрось масштаб (на сайте)» → (zoom-действие, None)
        или (None, причина). Та же адресация вкладки, что у чтения
        (_snapshot_for: алиас/домен/PAGE_REF/отслеживаемая/последняя)."""
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        # value — адрес страницы: по нему проверяется приватность
        act = {"kind": "zoom", "dir": direction, "host": host, "value": url}
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act, None

    @_in_chat
    def page_view_report(self, site_word: Optional[str] = None,
                         chat_id: str = "",
                         full_page: bool = False) -> Tuple[Optional[dict], Optional[str]]:
        """«что на странице?» / «пришли скриншот» → (отчёт, None) или
        (None, причина). Отчёт: url/host/tab_id/items (снапшот элементов)
        и shot — JPEG-скриншот вьюпорта (None, если бэкенд кадра не даёт
        или съёмка не удалась — текстовый список всё равно возвращается).
        full_page=True («покажи всю страницу») — вместо shot поле shots:
        нарезанные куски полностраничного захвата (скролл-стичинг), плюс
        outline (оглавление разделов по всему DOM) и truncated; захват не
        вышел — молча падаем обратно на обычный вьюпортный отчёт.
        Та же адресация вкладки, что у чтения/клика (_snapshot_for).
        Чтение без побочек — без подтверждения."""
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        if full_page:
            cap = None
            try:
                from app.features.browser_actions import full_page_capture
                cap = full_page_capture(host, tab_id=tab_id)
            except Exception as e:
                logger.debug(f"[CompControl] Полностраничный захват "
                             f"не удался: {e}")
            if cap and cap.get("shots"):
                self._audit(chat_id, {"kind": "page_view", "host": host or "",
                                      "value": url or ""}, True,
                            f"full:{len(cap['shots'])}")
                return {"url": url, "host": host, "tab_id": tab_id,
                        "items": items or [], "shot": None, "full": True,
                        "shots": cap["shots"],
                        "outline": cap.get("outline") or [],
                        "truncated": bool(cap.get("truncated"))}, None
            # Захват не вышел — ниже обычный вьюпортный отчёт
        shot = None
        try:
            from app.features.browser_actions import screenshot_viewport
            # Пользователь просил ПОКАЗАТЬ страницу — если вкладка фоновая,
            # разрешаем поднять окно: без рендера кадра не будет вовсе,
            # а всплытие тут и есть смысл команды
            shot = screenshot_viewport(host, tab_id=tab_id, allow_focus=True)
        except Exception as e:
            logger.debug(f"[CompControl] Скриншот для отчёта не удался: {e}")
        self._audit(chat_id, {"kind": "page_view", "host": host or "",
                              "value": url or ""}, True,
                    "shot+text" if shot else "text")
        return {"url": url, "host": host, "tab_id": tab_id,
                "items": items or [], "shot": shot}, None

    @_in_chat
    def scroll_to_goal(self, goal: str, site_word: Optional[str] = None,
                       chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """«пролистай до X» / «найди X на странице» / «докрути до конца» —
        ограниченный доскролл до цели (или края страницы) с фото места.
        Чтение + прокрутка, ничего не нажимается — без подтверждения.
        → ({"found", "shot", "host", "url", "edge", "goal"}, None) или
        (None, причина). При промахе прокрутка возвращается на место
        (_scroll_hunt), при находке страница остаётся у цели — пользователь
        просил «докрути до», визуальный отклик уместен."""
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        from app.features import browser_actions as _ba
        from app.features import cc_texts
        g = " ".join(goal.strip().split())
        g_norm = re.sub(r"\s*(?:страниц\w*|сайт\w*|лент\w*)\s*$", "",
                        g.lower()).strip()
        edge = None
        for k, words in _SCROLL_EDGE_WORDS.items():
            if g_norm in words:
                edge = k
                break
        found = False
        try:
            if edge == "top":
                _ba.scroll_restore(host, tab_id, 0.0)
                found = True
            elif edge == "bottom":
                for _ in range(15):
                    if self.stop_requested(chat_id):
                        break  # «стоп» от пользователя
                    step = _ba.scroll_step(host, tab_id)
                    if not step.get("moved"):
                        break
                    _ba.wait_dom_idle(host, tab_id, timeout_sec=1.5,
                                      min_wait=0.2,
                                      stop=self._stop_check(chat_id))
                    if step.get("bottom"):
                        break
                found = True
            else:
                _gu, g_items = _ba.snapshot_for_goal(host, g, tab_id=tab_id)
                if not g_items:
                    _gu, g_items = self._scroll_hunt(_ba, host, tab_id, g,
                                                     url or "")
                found = bool(g_items)
        except Exception as e:
            logger.debug(f"[CompControl] доскролл до цели не удался: {e}")
            return None, cc_texts.t("scroll_failed", self.turn_lang())
        if self.stop_requested(chat_id):
            # Доскролл прерван «стоп»: ни «докрутил до низа», ни «не вижу» —
            # честно «остановлено»
            return None, cc_texts.t("stopped", self.turn_lang())
        shot = None
        if found:
            try:
                # Место уже во вьюпорте (scrollIntoView в целевом снапшоте);
                # тихая съёмка — окно не поднимаем
                shot = _ba.screenshot_viewport(host, tab_id=tab_id,
                                               allow_focus=False)
            except Exception as e:
                logger.debug(f"[CompControl] кадр найденного места "
                             f"не удался: {e}")
        self._audit(chat_id, {"kind": "scroll_goal", "host": host or "",
                              "value": g[:60]}, True,
                    ("found" if found else "miss") + ("+shot" if shot else ""))
        return {"found": found, "shot": shot, "host": host or "",
                "url": url or "", "edge": edge, "goal": g}, None

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
            elif _looks_like_domain(site_word):
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
        # Текст секции уходит в общий LLM-поток (часто облако/веб-чат): с
        # приватной страницы (вход/оплата/банк) не отдаём — обычный диалог
        page_host = host_part or self._last_host or ""
        # Приватность — по ФАКТИЧЕСКОМУ адресу прочитанной вкладки (vk.com
        # обычный, vk.com/im — переписка; «…на vk.com» явным хостом), плюс
        # отслеживаемый адрес того же хоста; адрес не узнали — хост и он
        cands = [page_host]
        try:
            from app.features.browser_actions import tab_url
            cands.append(tab_url(host_part, tab_id) or "")
        except Exception as e:
            logger.debug(f"[CompControl] адрес вкладки секции не узнан: {e}")
        last = self._last_url
        if last:
            from app.features.cc_privacy import _host_of

            def _h(s) -> str:
                h = _host_of(str(s or ""))[0]
                return h[4:] if h.startswith("www.") else h
            if host_part is None or _h(last) == _h(page_host):
                cands.append(str(last))
        if any(self.is_private_page(c) for c in cands if c):
            logger.info(f"[CompControl] {page_host} — приватная страница: "
                        "текст секции в LLM не отдаю")
            return None
        return text, page_host, query

    @_in_chat
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

    @_in_chat
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
        # Медиа-команды («пауза», «тише») — тоже на отслеживаемую вкладку:
        # ютуб может играть фоном — это общее правило адресации, не исключение
        url, host, _items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        if mkind in ("vol_down", "vol_up", "mute", "unmute", "toggle") \
                and "/shorts/" in (url or ""):
            # На shorts клавиатурные шорткаты обычного YouTube не работают:
            # стрелки — листание видео, пробел листает ленту вперёд, «m» и
            # «k» молчат. Медиа управляем напрямую у <video>
            op = {"vol_down": "-0.2", "vol_up": "0.2", "mute": "mute",
                  "unmute": "unmute", "toggle": "toggle"}[mkind]
            act = {"kind": "media_vol", "op": op, "host": host, "value": url}
            if tab_id is not None:
                act["tab_id"] = tab_id
            return act, None
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

    @_in_chat
    def resolve_slider(self, goal, site_word: Optional[str],
                       router=None, chat_id: str = ""
                       ) -> Tuple[Optional[dict], Optional[str]]:
        """«перетащи слайдер рабочие часы на 8»: goal=(подпись, значение,
        единица ""/pct/min/sec). Адресация вкладки — как у клика; сам
        ползунок (input[type=range]/role=slider) и кламп значения — JS при
        исполнении."""
        label, value = goal[0], goal[1]
        unit = str(goal[2]) if len(goal) > 2 else ""
        url, host, _items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        act = {"kind": "slider", "host": host, "value": url,
               "slider_label": label, "slider_value": int(value),
               "slider_unit": unit, "element": label or "слайдер"}
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
        # Идёт ли фоновая прокрутка прямо сейчас (поток жив и не остановлен).
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
        движения. Один сеанс на чат: повторный старт при живом отсекается
        резолвером; сеанс и его конец пишутся в состояние СВОЕГО чата (поток
        дозорного знает ключ — контекста хода у него нет)."""
        from app.features import browser_actions as ba
        chat = self._cur_chat()
        host, tab_id = action.get("host"), action.get("tab_id")
        side = action.get("side")
        direction = action.get("dir")
        container = action.get("container")
        name_param = None
        if container:
            # Русское имя + англ. корень для DOM-матча (id/aria/class у
            # зарубежных сайтов английские: ytd-comments#comments)
            names = [str(container).lower()]
            alias = _SCROLL_CONTAINER_ALIAS.get(names[0])
            if alias:
                names.append(alias)
            name_param = "|".join(names)
        res = ba.scroll_start(host, tab_id=tab_id, side=side,
                              direction=direction, name=name_param)
        if res.get("name_missed"):
            raise ba.BrowserUnavailable(
                f"не вижу на странице прокручиваемого блока «{container}»")
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
        deadline = time.monotonic() + _SCROLL_MAX_SEC

        def _loop():
            while not stop_evt.wait(_SCROLL_POLL_SEC):
                if time.monotonic() >= deadline:
                    # Страница не сказала ни «done», ни «не active» —
                    # сеанс не должен жить дольше своего потолка
                    box["end"] = "timeout"
                    break
                try:
                    st = ba.scroll_status(host, tab_id=tab_id)
                except Exception as e:
                    box["end"] = "lost"  # вкладку закрыли/браузер ушёл
                    logger.info(f"[CompControl] Листание прервано: {e}")
                    break
                if st.get("done"):
                    box["end"] = ("timeout" if st.get("end") == "cap"
                                  else "bottom")
                    break
                if not st.get("active"):
                    # Страница ушла навигацией/перезагрузкой — анимации нет
                    box["end"] = "lost"
                    break
            self._scroll_finished(stop_evt, box, chat)

        t = threading.Thread(target=_loop, daemon=True, name="vpc-scroll")
        st = self._st(chat)
        with self._scroll_lock:
            st.scroll = {"thread": t, "stop": stop_evt, "box": box,
                         "host": host, "tab_id": tab_id, "chat": chat}
            st.scroll_ended = None
        t.start()
        logger.info(f"[CompControl] Начал листать страницу: {host}")

    def _scroll_finished(self, stop_evt, box: Dict[str, str],
                         chat: Optional[str] = None):
        """Листание кончилось САМО (конец ленты, вкладка ушла, потолок
        времени) — снимаем сеанс: иначе он числился бы живым вечно, на
        «промотай» шло бы «я уже листаю», а бытовое «стоп» спустя время
        перехватывалось бы как команда. Причину помним недолго —
        «стоп» вдогонку получит честный ответ. chat — чей сеанс (поток
        дозорного); None — текущий чат."""
        st = self._st(chat)
        with self._scroll_lock:
            s = st.scroll
            if s is None or s["stop"] is not stop_evt:
                return  # сеанс уже сняли «стопом» или сменили новым
            st.scroll = None
            st.scroll_ended = (time.monotonic(), box.get("end") or "lost")

    def _scroll_recent_end(self) -> Optional[str]:
        """Причина самозавершения, если оно было только что (иначе None).
        Вызывается под _scroll_lock."""
        ended = self._scroll_ended
        if not ended:
            return None
        if time.monotonic() - ended[0] > _SCROLL_END_GRACE_SEC:
            return None
        return ended[1]

    def _scroll_stop_now(self) -> Optional[str]:
        """Остановить листание: сначала гасим анимацию в самой странице
        (один вызов — страница замирает сразу, на месте «стоп»), затем
        дозорный поток. → причина самостоятельного завершения ('bottom' —
        долистал до конца, 'lost' — вкладка умерла/ушла, 'timeout' — упёрлось
        в потолок) или None, если остановлен пользователем."""
        with self._scroll_lock:
            s, self._scroll = self._scroll, None
            recent = self._scroll_recent_end()
            self._scroll_ended = None
        if not s:
            # Сеанс успел закончиться сам — причину ещё помним
            return recent
        from app.features import browser_actions as ba
        ba.scroll_stop(s.get("host"), tab_id=s.get("tab_id"))
        s["stop"].set()
        s["thread"].join(timeout=3)
        return s["box"].get("end")

    @_in_chat
    def resolve_scroll(self, mode, site_word: Optional[str],
                       router=None, chat_id: str = "") -> Tuple[Optional[dict], Optional[str]]:
        """«промотай страницу (на ютубе)» → (scroll-действие, None);
        «промотай раздел слева» — mode приезжает кортежем ("start", "left"):
        листается внутренняя панель, а не окно.
        «стоп»/«хватит листать» → (scroll_stop, None). «стоп» без сеанса
        листания — (None, None): бытовое слово уходит в обычный диалог
        (сеанс, кончившийся сам, держится ещё _SCROLL_END_GRACE_SEC — чтобы
        «стоп» вдогонку ответил, чем всё кончилось, а не перехватывался
        спустя полчаса)."""
        side = None
        direction = None
        container = None
        if isinstance(mode, tuple):
            if len(mode) == 4:
                mode, side, direction, container = mode
            elif len(mode) == 3:
                mode, side, direction = mode
            else:
                mode, side = mode
        if mode == "stop":
            with self._scroll_lock:
                has = (self._scroll is not None
                       or self._scroll_recent_end() is not None)
            if not has:
                return None, None
            return {"kind": "scroll_stop"}, None
        if self._scroll_active():
            return None, self._tx("rs_already_scrolling")
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id)
        if err:
            return None, err
        p = urlparse(url)
        if p.hostname in ("localhost", "127.0.0.1") and p.port in (5173, 8000):
            return None, self._tx("rs_scroll_chat_tab")
        act = {"kind": "scroll", "host": host, "value": url}
        if side:
            act["side"] = side
        if direction:
            act["dir"] = direction
        if container:
            act["container"] = container
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
        без действия и подтверждения). Текущая (отслеживаемая) вкладка —
        с пометкой «← текущая»: именно на неё направлены команды без
        названия сайта. Пометка по tab_id; не нашёлся в списке (бэкенд
        сменился, вкладка закрыта) — по хосту контекста."""
        try:
            tabs = self.list_open_tabs()
        except Exception as e:
            return self._tx("rs_no_browser", detail=e)
        if not tabs:
            return self._tx("tabs_none")
        shown = tabs[:10]
        cur_id = self._last_tab_id
        cur_idx = next((i for i, t in enumerate(shown)
                        if cur_id is not None and t["tab_id"] == cur_id), None)
        if cur_idx is None:
            cur_host = (self._last_host or "").lower().removeprefix("www.")
            if cur_host:
                cur_idx = next(
                    (i for i, t in enumerate(shown)
                     if t["host"].lower().removeprefix("www.") == cur_host),
                    None)
        mark = self._tx("tabs_current")
        parts = [f"{self._qt((t['title'] or t['host'])[:40])} ({t['host']})"
                 + (mark if i == cur_idx else "")
                 for i, t in enumerate(shown)]
        tail = ("" if len(tabs) <= 10
                else self._tx("tabs_more", n=len(tabs) - 10))
        return self._tx("tabs_list", tabs=", ".join(parts), more=tail)

    def _scored_tabs(self, goal: str, tabs: List[dict]
                     ) -> List[Tuple[float, dict]]:
        """Оценки соответствия вкладок цели («ютуб», «репозиторий проекта»): алиас
        сайта → хост, далее точное совпадение хоста/заголовка > подстрока в
        хосте > подстрока в заголовке > основы слов. Общий матч для
        «перейди на вкладку X», «закрой/обнови вкладку X».
        Слова-носители («страница/вкладка/сайт…» любого падежа — цель
        LLM-разбора их не срезает) из цели убираются; регистр и ё/е не
        важны; окончания — по основе («иванова» ~ «ИВАНОВ»)."""
        g_raw = " ".join(goal.lower().split())
        g_bare = _strip_tab_filler(g_raw) or g_raw
        # Алиас («ютуб») → хост сайта: вкладки матчатся по латинскому хосту
        alias_host = ""
        k = self._lookup(self.sites, g_raw) or (
            self._lookup(self.sites, g_bare) if g_bare != g_raw else None)
        if k:
            alias_host = (urlparse(self.sites[k]).hostname or "") \
                .lower().removeprefix("www.")
        g0 = _fold_yo(g_raw)
        g = _fold_yo(g_bare)
        gw = [w for w in _TAB_WORD_RE.findall(g) if len(w) >= 3]
        scored: List[Tuple[float, dict]] = []
        for t in tabs:
            h = t["host"].lower().removeprefix("www.")
            title = _fold_yo(" ".join(t["title"].lower().split()))
            words = _TAB_WORD_RE.findall(f"{h} {title}")
            if alias_host and (h == alias_host
                               or h.endswith("." + alias_host)
                               or alias_host.endswith("." + h)):
                s = 100.0
            elif g and (g == h or g == title or g0 == title):
                s = 95.0
            elif g and g in h:
                s = 80.0
            elif g and g in title:
                s = 70.0
            elif gw and all(any(x.startswith(w) for x in words)
                            for w in gw):
                s = 60.0
            elif gw and all(_tab_word_stem_hit(w, words) for w in gw):
                s = 50.0  # только по основам — уступает точному префиксу
            else:
                continue
            scored.append((s, t))
        scored.sort(key=lambda x: -x[0])
        return scored

    @_in_chat
    def resolve_tab_switch(self, goal: str, explicit: bool = True,
                           chat_id: str = ""
                           ) -> Tuple[Optional[dict], Optional[str]]:
        """«перейди на вкладку ютуб» → (tab_switch-действие, None) —
        переключение ничего не меняет, поэтому без подтверждения. Матч по
        живым вкладкам (_scored_tabs); явный лидер (отрыв ≥10) —
        переключаем. Несколько подходящих — честный перечень. Ни одной:
        мягкая форма («перейди на X» без слова «вкладку») — фолбэк на
        открытие сайта из алиасов/истории (без поискового резолва — гадать
        адрес на мягкую фразу не берёмся), либо (None, None) в обычный
        диалог; явная — отказ со списком открытых."""
        try:
            tabs = self.list_open_tabs()
        except Exception as e:
            return None, self._tx("rs_no_browser", detail=e)
        scored = self._scored_tabs(goal, tabs)
        if scored and (len(scored) == 1
                       or scored[0][0] - scored[1][0] >= 10.0):
            t = scored[0][1]
            label = t["title"] or t["host"]
            logger.info(f"[CompControl] Переключение на вкладку "
                        f"#{t['tab_id']} "
                        f"«{self._label_for_log(label, t.get('url'))}» "
                        f"({t['host']})")
            return {"kind": "tab_switch", "tab_id": t["tab_id"],
                    "value": t["url"], "host": t["host"],
                    "element": label[:80]}, None
        names = ", ".join(self._qt((t['title'] or t['host'])[:30])
                          for t in tabs[:5]) or "—"
        if len(scored) > 1:
            cands = ", ".join(self._qt((t['title'] or t['host'])[:30])
                              for _s, t in scored[:4])
            return None, self._tx("rs_tabs_ambiguous", goal=goal,
                                  cands=cands)
        if not explicit:
            # Мягкая форма и вкладки нет — возможно, имелось в виду «открой»:
            # алиас/история (поисковый резолв на мягкую фразу не гоняем)
            alt = self.resolve(goal, web_search=False)
            if alt and not alt.get("expect_name"):
                return alt, None
            return None, None  # не наша команда — пусть разбирает диалог
        return None, self._tx("rs_tab_not_found_open", goal=goal, names=names)

    @_in_chat
    def resolve_tab_op(self, goal: Optional[str], op: str, router=None,
                       chat_id: str = ""
                       ) -> Tuple[Optional[dict], Optional[str]]:
        """«обнови/закрой вкладку (X)» → (tab_op-действие, None). Цель —
        тот же матч вкладок, что у «перейди на вкладку X»; без цели —
        видимая пользователем вкладка (tab_id не пиним: к моменту
        подтверждения пользователь мог сменить вкладку — обновить/закрыть
        надо ту, что он видит СЕЙЧАС). С подтверждением: закрытие
        необратимо, перезагрузка теряет неотправленный ввод."""
        # Особые op парсера (parse_tab_op): пустую вкладку открывать незачем,
        # а «закрыть все» разом — слишком разрушительно для одной фразы
        if op == "new":
            return None, self._tx("rs_tab_new")
        if op == "close_all" or (op == "close" and goal and " ".join(
                goal.lower().split()) in ("все", "всё", "all")):
            return None, self._tx("rs_tab_close_all")
        tab_id: Optional[int] = None
        host = ""
        label = ""
        if goal:
            try:
                tabs = self.list_open_tabs()
            except Exception as e:
                return None, self._tx("rs_no_browser", detail=e)
            scored = self._scored_tabs(goal, tabs)
            if scored and (len(scored) == 1
                           or scored[0][0] - scored[1][0] >= 10.0):
                t = scored[0][1]
                tab_id = t["tab_id"]
                host = t["host"]
                label = t["title"] or t["host"]
            elif len(scored) > 1:
                cands = ", ".join(self._qt((t['title'] or t['host'])[:30])
                                  for _s, t in scored[:4])
                return None, self._tx("rs_tabs_ambiguous", goal=goal,
                                      cands=cands)
            else:
                names = ", ".join(self._qt((t['title'] or t['host'])[:30])
                                  for t in tabs[:5]) or "—"
                return None, self._tx("rs_tab_not_found", goal=goal,
                                      names=names)
        else:
            # Имя для вопроса-подтверждения — видимая вкладка сейчас
            # (visible_page_info отдаёт (url, host); title возьмём из
            # списка вкладок, если видимая там найдётся по точному URL)
            try:
                from app.features import browser_actions as ba
                info = ba.visible_page_info()
            except Exception:
                info = None
            if info:
                host = str(info[1] or "").lower()
                try:
                    tabs = self.list_open_tabs()
                except Exception:
                    tabs = []
                hit = next((t for t in tabs if t["url"] == info[0]), None)
                label = (hit["title"] or hit["host"]) if hit else host
        logger.info(f"[CompControl] tab_op {op}: "
                    f"{self._label_for_log(label, host) if label else (goal or '(видимая)')}")
        return {"kind": "tab_op", "op": op, "tab_id": tab_id,
                "host": host, "element": label[:80]}, None

    # ── Корзина сайта: «убери X из корзины», «убавь/прибавь X» ──

    def _cart_op_fallback(self, product_raw: str, op: str,
                          site_word: Optional[str], chat_id: str = ""):
        """Голые формы «удали X»/«закрой X»/«изменить состав в X»/
        «+ в X» при открытой корзине: корзинные контролы товара
        (×, «Изменить», −/+) текстовому резолву маловидимы — cart_op
        находит карточку по названию и жмёт контрол по позиции/тексту.
        Срабатывает, только когда товар реально виден в корзине страницы;
        иначе None — идём обычными фолбэками/честным отказом (инвентарь
        бота и «удали X» вне магазина не задеваем)."""
        product = _cart_product_name(product_raw, op)
        if len(product) < 2 or _CART_NOT_PRODUCT_RE.search(product):
            return None
        try:
            from app.features import browser_actions as _bac
            url, host, _items, tab_id, err = self._snapshot_for(
                site_word, chat_id=chat_id)
            if err or not host:
                return None
            if urlparse(url).hostname in ("localhost", "127.0.0.1"):
                return None  # вкладка чата — корзины там нет
            if not _bac.cart_item_present(host, product, tab_id=tab_id):
                return None
            op_ru = {"remove": "удаление", "edit": "изменение",
                     "increase": "увеличение", "decrease": "уменьшение"}.get(op, op)
            logger.info(f"[CompControl] «{str(product_raw)[:30]}» → {op_ru} "
                        f"«{product[:40]}» в корзине на {host}")
            act = {"kind": "cart", "op": op, "product": product,
                   "host": host, "value": url}  # value — адрес страницы
            if tab_id is not None:
                act["tab_id"] = tab_id
            return act
        except Exception as e:
            logger.debug(f"[CompControl] Корзинный фолбэк не удался: {e}")
            return None

    def _comp_edit_fallback(self, product_raw: str,
                            site_word: Optional[str], chat_id: str = ""):
        """«изменить состав (в/на X)» на странице/модалке продукта (в комбо
        у каждого слота своя ссылка «Изменить состав», а имя товара живёт
        соседним блоком — ctx снапшота его не захватывает, и скоуп-скоринг
        промахивался в инфо-иконку). Детерминированный
        поиск: контрол — по тексту, товар — по контексту предка.
        → (действие|None, None|уточняющий вопрос при неоднозначности).
        None-действие без вопроса — страница не про состав, идём дальше
        обычным резолвом."""
        product = _cart_product_name(product_raw, "edit")
        if product and _CART_NOT_PRODUCT_RE.search(product):
            return None, None
        try:
            from app.features import browser_actions as _bac
            url, host, _items, tab_id, err = self._snapshot_for(
                site_word, chat_id=chat_id)
            if err or not host:
                return None, None
            if urlparse(url).hostname in ("localhost", "127.0.0.1"):
                return None, None  # вкладка чата
            res = _bac.edit_composition_find(host, product, tab_id=tab_id)
            st = res.get("status")
            if st == "unique":
                logger.info(f"[CompControl] «{str(product_raw)[:30]}» → "
                            f"редактор состава «{product[:40]}» на {host}")
                act = {"kind": "comp_edit", "product": product,
                       "host": host, "value": url}  # value — адрес страницы
                if tab_id is not None:
                    act["tab_id"] = tab_id
                return act, None
            if st == "multi":
                vs = "; ".join(str(v) for v in (res.get("variants") or []))
                return None, self._tx("rs_comp_edit_multi",
                                      variants=f" ({vs})" if vs else "")
        except Exception as e:
            logger.debug(f"[CompControl] Поиск редактора состава не "
                         f"удался: {e}")
        return None, None

    @_in_chat
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
            return None, self._tx("rs_cart_chat_tab")
        act = {"kind": "cart", "op": op, "product": product, "host": host,
               "value": url}  # value — адрес страницы (приватность)
        if tab_id is not None:
            act["tab_id"] = tab_id
        return act, None

    # ── Ввод текста «введи X в поле Y» ─────────────────────

    def _type_fields_hint(self, msg: str, inputs: List[dict]) -> str:
        labels = [str(it.get("text") or "")[:30] for it in inputs[:5]]
        labels = [l for l in labels if l]
        return msg + (self._tx("rs_fields_seen", labels=", ".join(
            self._qt(l) for l in labels)) if labels else "")

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
                return self._tx("rs_hidden_field", label=label[:40],
                                host=host)
        return None

    @staticmethod
    def _home_city() -> Optional[str]:
        """Город пользователя из местоположения (env_location.json).
        «Город, Страна» → «Город». None — местоположение выключено."""
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
        # Хвост — от КОНЦА последнего слова подписи: по началу СЛЕДУЮЩЕГО
        # слова («в поле имя иван петров» → toks[j+k] = «иван») терялось
        # первое слово текста
        tail = body[toks[j + k - 1][0][1]:]
        text = (body[:toks[j][0][0]] + " " + tail).strip()
        text = _TYPE_PREP_EDGE_RE.sub("", text).strip()
        return it, text or None

    @_in_chat
    def resolve_type(self, body: str, site_word: Optional[str],
                     router, chat_id: str = ""
                     ) -> Tuple[Optional[dict], Optional[str]]:
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
                    if self._lookup(self.sites, cand) is not None \
                            or _looks_like_domain(cand):
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
        # Поле ввода выбирается по снапшоту — оверлей его перекрывает:
        # это единственный путь вне каскада клика, которому авто-закрытие
        # нужно (остальные операции элемент не выбирают)
        url, host, items, tab_id, err = self._snapshot_for(
            site_word, chat_id=chat_id, auto_dismiss=True)
        if err:
            if explicit:
                return None, err
            return None, None  # «в стиле …», но страницы нет — не наше
        # Форма входа/оплаты — поля выбирает только локальная модель/скоринг
        router = self._privacy_router(router, url or host)
        p = urlparse(url)
        if p.hostname in ("localhost", "127.0.0.1") and p.port in (5173, 8000):
            return None, self._tx("rs_type_chat_tab")
        inputs = [it for it in items if it.get("ed")]
        if not inputs:
            if explicit:
                hidden = self._hidden_fields_note(host, tab_id, body)
                if hidden:
                    return None, hidden
                # body — «ТЕКСТ в поле ПОЛЕ»: вводимый текст (пароль, слот
                # сценария) в аудит и лог — только маской
                from app.features.cc_privacy import redact_type_body
                no_fields = self._tx("rs_no_fields", host=host)
                self._audit_resolve(chat_id, redact_type_body(body), host,
                                    no_fields, "no_fields")
                return None, no_fields
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
                self._tx("rs_type_which_field"), inputs)
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
                # «введи кофе» / «введи мой город» + единственное поле
                item, text = inputs[0], body
            if item is None:
                if explicit:
                    return None, self._type_fields_hint(
                        self._tx("rs_type_unparsed", body=body[:60]), inputs)
                return None, None
            if not text:
                return None, self._tx(
                    "rs_type_no_text",
                    field=str(item.get('text') or '')[:40])
        if field_goal is None and item is None:
            # «текст в поле» — сепаратор есть, а названия поля после него нет
            return None, self._type_fields_hint(
                self._tx("rs_type_which_field"), inputs)
        if field_goal is not None:
            idx, meta = self._choose_element(field_goal, inputs, router,
                                             host=host)
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
                    self._tx("rs_no_field", host=host, field=field_goal),
                    inputs)
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
                return None, self._tx("rs_no_city")
            text = city
        if not text or len(text) > 200:
            return None, self._tx("rs_type_bad_text")
        label = str(item.get("text") or f"#{item['idx']}")
        logger.info(f"[CompControl] Ввод «{self._label_for_log(label, url, host, idx=item['idx'])}» "
                    f"← {len(text)} симв. на {host} (путь: {meta.get('path')})")
        act = {"kind": "type", "idx": int(item["idx"]), "text": text,
               "element": label, "host": host, "value": url, "choose": meta}
        # aria/title поля — в действие: поле «Номер карты» только в aria
        # иначе проходило мимо risky_label (ввод карты без подтверждения)
        self._with_labels(act, item)
        # Риск ввода (needs_confirm): чувствительное поле (пароль/email/tel —
        # флаг sn снапшота) подтверждается всегда; поисковое (флаг q) может
        # идти без confirm по risk_overrides.type_text_safe_fields
        if item.get("sn"):
            act["field_sensitive"] = True
        elif item.get("q"):
            act["field_safe"] = True
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
            # follow_redirects=False + _get_safe_redirects: адрес поиска и
            # каждый его редирект проходят SSRF-фильтр web_search (страницу
            # тянем МЫ, а вёрстку/редиректы диктует сторонний сайт)
            with httpx.Client(follow_redirects=False, timeout=8, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "ru,en;q=0.9",
            }) as client:
                resp, final_url = _get_safe_redirects(client, search_url)
                if resp is None:
                    logger.info(f"[CompControl] Первый результат ({site_key}) "
                                f"не загружен: {final_url}")
                    return None
                resp.raise_for_status()
                html = resp.text
            m = re.search(pattern, html)
            if not m:
                logger.info(f"[CompControl] Первый результат ({site_key}): regex не сматчился")
                return None
            # Относительную ссылку клеим к ФИНАЛЬНОМУ адресу цепочки, а не к
            # исходному: после редиректа база могла сменить хост/путь
            direct = urljoin(final_url, m.group(1) if m.groups() else m.group(0))
            logger.info(f"[CompControl] Первый результат ({site_key}): {direct[:80]}")
            return direct
        except Exception as e:
            logger.info(f"[CompControl] Извлечение первого результата ({site_key}) "
                        f"не удалось: {e}")
            return None

    @_in_chat
    def execute(self, action: dict, chat_id: str = "",
                router=None) -> Tuple[bool, str]:
        """Исполняет действие из allowlist'а. Возвращает (ok, detail).
        router — для LLM-выбора элемента в многошаговой навигации.
        Один браузер на все чаты — исполнение под _exec_lock: действия
        разных чатов не чередуются на одной странице. Контекст вкладок —
        чата chat_id (пусто — чата хода, @_in_chat)."""
        if chat_id:
            # Новое исполненное действие делает отложенное устаревшим:
            # страница уже другая, «да» потом не должно исполнять старое
            try:
                self.clear_pending(chat_id)
            except Exception:
                pass
        lock = self.__dict__.get("_exec_lock")
        if lock is None:
            lock = self.__dict__.setdefault("_exec_lock", threading.RLock())
        with lock:
            # Чей ход сейчас исполняется — для «стоп» до лока хода
            # (stop_requested/executing_for); вложенный вызов не сбрасывает.
            # Веб-чат без chat_id — ключ хода (user_id): тот же, по которому
            # cc_turn_enter ставит request_stop
            depth = self.__dict__.get("_exec_depth", 0)
            if not depth:
                # str(None) задачи/сценария веб-чата — не ключ (_chat_key)
                self._exec_chat = (_chat_key(chat_id)
                                   or _chat_key(self.turn_key()))
            self._exec_depth = depth + 1
            try:
                from app.features import browser_actions as _ba
                # Долгие опросы браузера внутри действия (снапшот, DOM,
                # ожидание вкладки) выходят по «стоп» досрочно
                with _ba.stop_scope(self._stop_check(self._exec_chat)):
                    return self._execute_locked(action, chat_id, router)
            finally:
                self._exec_depth = depth
                if not depth:
                    self._exec_chat = None

    def _execute_locked(self, action: dict, chat_id: str,
                        router) -> Tuple[bool, str]:
        error_class = None
        # LLM-решения по приватной странице (вход/оплата) — только локальной
        # моделью: снапшот/скриншот не уходят облаку и веб-чатам
        # По ПОЛНЫМ адресам (value/url, у multi — всех шагов) и хосту:
        # приватность бывает по пути (vk.com/im, */login, */cart)
        from app.features.cc_privacy import page_candidates
        router = self._privacy_router(router, *page_candidates(action))
        t0 = time.monotonic()
        action.pop("confirm_required", None)
        try:
            # Инвариант подтверждения — здесь, до любого пути исполнения:
            # рискованное действие без токена человека не исполняется
            # (шаги маршрута сверяет _nav_gate внутри _navigate)
            self._confirm_gate(action)
            self._dispatch(action, router=router)
            ok = True
            # read: detail — это прочитанный текст, он и есть ответ пользователю
            detail = (str(action.pop("_result", "") or "")
                      if action["kind"] == "read" else "")
        except Exception as e:
            # Без хвостовой точки: detail вставляется во фразы, которые
            # ставят свою («Не удалось …: {detail}.», банк «Код ошибки:
            # {detail}.») — «exceeded..» в ответе. Многоточие не трогаем
            ok, detail = False, re.sub(r"(?<!\.)\.\s*$", "",
                                       str(e)[:200].rstrip())
            # Класс ошибки для аудита: «не уверен, что сработало» —
            # отдельный класс от «элемент не найден»/«браузер недоступен»
            error_class = getattr(e, "error_class", None) or "error"
            if self.stop_requested():
                # Опрос вышел по «стоп» и шаг упал на недождавшейся странице —
                # честное «остановлено», а не ошибка сайта
                from app.features import cc_texts
                ok, detail = False, cc_texts.t("stopped_by_user",
                                               self.turn_lang())
                error_class = "stopped"
                action.pop("confirm_required", None)
            elif isinstance(action.get("confirm_required"), dict):
                # Гейт: ничего не нажато (маршрут — до рискованного шага);
                # вызывающий превращает отказ в вопрос (gate_followup).
                # Отказ распознаётся по полю, даже если исключение обернули
                from app.features import cc_texts
                error_class = "needs_confirm"
                detail = cc_texts.t(
                    "gate_blocked", self.turn_lang(),
                    risk=cc_texts.gate_risk(
                        action["confirm_required"].get("reason"),
                        self.turn_lang()))
        action["duration_ms"] = int((time.monotonic() - t0) * 1000)
        self.stats["executed" if ok else "failed"] += 1
        # Аудит гейта: чьим «да» исполнено / почему остановлено
        _gx: Dict[str, object] = {}
        if self.is_confirmed(action):
            _gx["confirmed"] = action["confirmed"].via
        if isinstance(action.get("confirm_required"), dict):
            _gx["gate"] = str(action["confirm_required"].get("reason"))
        self._audit(chat_id, action, ok, detail, error_class=error_class,
                    extra=_gx or None)
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
                # Открытая вкладка поднята — она и видна пользователю
                _opened = action.get("value") or next(
                    (a.get("value") for a in reversed(action.get("items") or ())
                     if isinstance(a, dict) and a.get("kind") == "url"), None)
                self._init_vis_baseline(_opened or self._last_host)
                self._save_last_page(action.get("value"))
            logger.info(f"[CompControl] Выполнено: {self._describe_log(action)}")
        else:
            logger.warning(f"[CompControl] Не удалось "
                           f"{self._describe_log(action)}: "
                           f"{self._label_for_log(detail, *page_candidates(action), limit=200)}")
        return ok, detail

    def _describe_log(self, action: dict) -> str:
        """_describe_safe для лога процесса (виден в /api/logs): на приватной
        странице подпись элемента (суммы, имена, переписка) — номером, как
        _label_for_log; вопрос человеку строит _describe_safe/describe."""
        from app.features.cc_privacy import page_candidates

        def _hide(a):
            if not isinstance(a, dict):
                return a
            if a.get("kind") == "multi":
                return dict(a, items=[_hide(x) for x in a.get("items") or []])
            if not any(a.get(k) for k in ("element", "aria", "title")):
                return a
            cands = page_candidates(a)
            last = getattr(self, "_last_url", None)
            if last and not any("://" in c for c in cands):
                cands.append(str(last))
            try:
                priv = any(self.is_private_page(c) for c in cands)
            except Exception:
                priv = True  # проверить не вышло — консервативно
            if not priv:
                return a
            return dict(a, element=f"#{a.get('idx')}" if a.get("idx") is not None
                        else "<hidden>", aria="", title="")
        try:
            return self._describe_safe(_hide(action))
        except Exception:
            return str(action.get("kind"))

    @staticmethod
    def _expect_label(action: dict) -> dict:
        """Подпись, которую браузер сверит с узлом в момент клика/ввода
        (click_tagged/fill_tagged expect=): для шага агента задач — номер
        из снимка, взятого десятки секунд (или минуты «да») назад, мог
        достаться другому узлу, а гейт проверял старую подпись."""
        if "task" in (action.get("origin"), action.get("pending_from")) \
                and action.get("element"):
            return {"expect": str(action["element"])}
        return {}

    def _describe_safe(self, action: dict, lang: Optional[str] = None) -> str:
        # describe() для логов: введённый текст — ТОЛЬКО длиной, в любое
        # поле (пароль в безымянное «#1» по подписи не распознать), URL —
        # без токенов. Для чувствительного поля вопрос task_agent'а видит
        # ту же маску, что и раньше давал redact_typed
        from app.features.cc_privacy import mask, redact_inline

        def _safe(a: dict) -> dict:
            if a.get("kind") == "type":
                return dict(a, text=mask(a.get("text")))
            return a
        try:
            if action.get("kind") == "multi":
                action = dict(action, items=[_safe(a) for a in action["items"]])
            return redact_inline(self.describe(_safe(action), lang=lang))
        except Exception:
            return str(action.get("kind"))

    def _dispatch(self, action: dict, router=None):
        # Системный вызов (в тестах подменяется).
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
            # Активированная вкладка видна — база ручного переключения
            self._init_vis_baseline(url or action.get("host"))
            self._save_last_page()
            if title and not action.get("element"):
                action["element"] = title[:80]
            return
        if action["kind"] == "tab_op":
            # Управление самой вкладкой: перезагрузка / закрытие / история
            # назад-вперёд. tab_id None — видимая пользователем на момент
            # ИСПОЛНЕНИЯ (после подтверждения): действие над той вкладкой,
            # что он видит сейчас
            from app.features import browser_actions as ba
            tid = action.get("tab_id")
            tid = int(tid) if tid is not None else None
            op = action.get("op")
            if op == "close":
                url, title = ba.close_tab(tid)
                # Вкладка закрыта — контекст инвалидируем ОДНИМ местом
                # (_forget_tab): без этого «закрой вкладку» без цели
                # (tid=None) оставлял бы отслеживаемым id мёртвой вкладки
                self._forget_tab(tid, url=url, host=action.get("host"))
            elif op in ("back", "forward"):
                url, title = ba.history_nav_tab(tid, op)
                # Вкладка теперь на другой странице (возможно, и сайта) —
                # обновляем цель команд по умолчанию
                self._last_url = url
                _h = urlparse(url).hostname
                if _h:
                    self._last_host = _h
            else:
                url, title = ba.reload_tab(tid)
            if title and not action.get("element"):
                action["element"] = title[:80]
            return
        if action["kind"] == "zoom":
            # Зум вкладки (Chrome per-host): меню «Вид» через System Events —
            # CDP-акселераторы на macOS не доходят. Фактический
            # % после — в zoom_done для ответа; хост резолвится по вкладке
            from app.features import browser_actions as ba
            res = ba.zoom_page(str(action.get("dir") or "reset"),
                               action.get("host") or None,
                               action.get("tab_id"))
            action["zoom_done"] = int(res.get("zoom") or 100)
            if res.get("host"):
                action["host"] = res["host"]
            return
        if action["kind"] == "click":
            from app.features import browser_actions as ba
            pre = ba.page_urls()
            point = action.get("point")
            if point is not None:
                # Зона зонального vision-фолбэка без DOM-метки (canvas и
                # т.п.) — клик по координатам; closed-loop в click_at_point.
                # Точка снята со скриншота ДО подтверждения: отпечаток
                # (scrollY + элемент под точкой) сверяется перед кликом —
                # страница прокрутилась/перерисовалась → честный отказ
                _pkw = {"expect": point["sig"]} if point.get("sig") else {}
                ba.click_at_point(action.get("host"), float(point["x"]),
                                  float(point["y"]),
                                  tab_id=action.get("tab_id"), **_pkw)
            else:
                # Номер элемента сквозной (блоки _mark_base): он сам
                # определяет, какой снапшот и какой фрейм его пометил, а
                # метка прошлой разметки под ним не найдётся — клик по
                # чужому элементу после смены разметки невозможен
                try:
                    ba.click_tagged(action.get("host"), int(action["idx"]),
                                    tab_id=action.get("tab_id"),
                                    **self._expect_label(action))
                except ba.ClickUncertain:
                    # Клик ДОСТАВЛЕН, эффекта не видно: повтор перещёлкнул
                    # бы тоггл (бургер/аккордеон открылся и тут же закрылся)
                    # — честное «не уверен, что сработало», без второго клика
                    raise
                except Exception as e:
                    if "элемент потерян" not in str(e) \
                            or not action.get("goal"):
                        raise
                    # Между резолвом и кликом лежит подтверждение
                    # пользователя, живые страницы (карусель баннеров) за эти
                    # секунды перерисовываются и метка протухает — клик НЕ
                    # ушёл. Перенаходим ТОТ ЖЕ подтверждённый элемент и
                    # кликаем ОДИН раз
                    idx2 = self._refind_confirmed(action, router)
                    action["retried"] = True
                    ba.click_tagged(action.get("host"), int(idx2),
                                    tab_id=action.get("tab_id"))
                    action["idx"] = int(idx2)
            pop = ba.follow_popup(pre)
            if pop is not None:
                # Клик открыл новое окно (вход в аккаунт через сторонний сервис и т.п.) —
                # следующие «введи …»/«нажми …» работают уже в нём
                tid, host, url = pop
                self._last_tab_id, self._last_host, self._last_url = tid, host, url
                self._init_vis_baseline(url or host)
                self._save_last_page(url)
                logger.info(f"[CompControl] Отслеживаю попап: {host}")
            else:
                self._remember_tab(action)
            return
        if action["kind"] == "hover":
            # «наведи на X» — движение мыши без клика (раскрыть hover-меню /
            # кнопки карточки / слайдер). Попапов и навигации не ждём
            from app.features import browser_actions as ba
            point = action.get("point")
            if point is not None:
                _pkw = {"expect": point["sig"]} if point.get("sig") else {}
                ba.hover_at_point(action.get("host"), float(point["x"]),
                                  float(point["y"]),
                                  tab_id=action.get("tab_id"), **_pkw)
            else:
                try:
                    ba.hover_tagged(action.get("host"), int(action["idx"]),
                                    tab_id=action.get("tab_id"))
                except Exception as e:
                    # Как у клика: метка могла протухнуть за секунды
                    # подтверждения — перенаходим тот же элемент и ОДИН
                    # повтор. Ретраим только потерю элемента: «не уверен, что
                    # сработало» повтором не лечится
                    if "элемент потерян" not in str(e) \
                            or not action.get("goal"):
                        raise
                    idx2 = self._refind_confirmed(action, router)
                    action["retried"] = True
                    ba.hover_tagged(action.get("host"), int(idx2),
                                    tab_id=action.get("tab_id"))
                    action["idx"] = int(idx2)
            self._remember_tab(action)
            return
        if action["kind"] == "type":
            from app.features import browser_actions as ba
            ba.fill_tagged(action.get("host"), int(action["idx"]),
                           action["text"], tab_id=action.get("tab_id"),
                           submit=bool(action.get("submit")),
                           **self._expect_label(action))
            self._remember_tab(action)
            return
        if action["kind"] == "read":
            # Чтение текста со страницы: результат уезжает в ответ через
            # action["_result"] (execute заберёт в detail)
            from app.features import browser_actions as ba
            action["_result"] = ba.read_text(
                action.get("host"), tab_id=action.get("tab_id"),
                mode=str(action.get("mode") or "last"),
                **({"task": True} if action.get("read_scope") == "task"
                   else {}))
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
            # и выставляет значение (unit: pct — доля шкалы, min/sec —
            # медиа-прогресс); фактическое — в отчёт ответа
            from app.features import browser_actions as ba
            _lab = str(action.get("slider_label") or "").lower()
            _unit = str(action.get("slider_unit") or "")
            _val = int(action.get("slider_value") or 0)
            if re.search(r"звук|громк|volume", _lab) \
                    and _unit in ("", "pct") and 0 <= _val <= 100:
                # Громкость при живом <video> — напрямую у медиа-элемента:
                # ползунок плеера (YouTube) свёрнут до наведения и часто
                # «не принимает» значение, а громкость медиа — надёжна.
                # Видео нет — обычный слайдер
                try:
                    got = ba.media_volume_op(action.get("host"),
                                             f"={_val / 100:g}",
                                             tab_id=action.get("tab_id"))
                except ba.BrowserUnavailable as e:
                    if "нет видео" not in str(e):
                        raise
                else:
                    action["slider_done"] = (got[4:] + "%"
                                             if got.startswith("vol:")
                                             else got)
                    action["slider_path"] = "media_vol"
                    self._remember_tab(action)
                    return
            action["slider_done"] = ba.set_slider(
                action.get("host"), action.get("slider_label") or "",
                int(action.get("slider_value") or 0),
                tab_id=action.get("tab_id"),
                unit=str(action.get("slider_unit") or ""))
            self._remember_tab(action)
            return
        if action["kind"] == "media_vol":
            # Громкость <video> напрямую (shorts) — фактическая громкость/
            # состояние мьюта — в отчёт ответа
            from app.features import browser_actions as ba
            action["vol_done"] = ba.media_volume_op(
                action.get("host"), str(action.get("op") or ""),
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
        if action["kind"] == "comp_edit":
            # Редактор состава слота на странице/модалке продукта:
            # детерминированный клик по ссылке «Изменить состав» (товар —
            # по контексту предка; неоднозначность отсеял резолвер)
            from app.features import browser_actions as ba
            ba.edit_composition_op(action.get("host"),
                                   action.get("product", ""),
                                   tab_id=action.get("tab_id"))
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
                # Открытие по просьбе пользователя: переключаемся на страницу
                # (вкладка + окно на передний план)
                self._last_tab_id = ba.open_new_tab(value, focus=True)
                self._verify_opened_site(action)
                return
            try:
                self._last_tab_id = ba.open_new_tab(value, focus=True)
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
        # shell, а браузерные рецепты из реестра browser_actions
        if value.startswith("recipe:"):
            from app.features.browser_actions import run_recipe
            run_recipe(value.removeprefix("recipe:").strip())
            return
        subprocess.Popen(value, shell=True)  # noqa: S602

    def _refind_confirmed(self, action: dict, router) -> int:
        """Перенайти подтверждённый пользователем элемент после «элемент
        потерян» (метка протухла, клик НЕ ушёл). Пользователь подтвердил
        конкретный элемент Y — молча нажать Z нельзя: вкладка ушла с
        подтверждённой страницы → отказ; сначала ищем элемент с ТОЧНО той же
        подписью, иначе обычный выбор по цели, но только если его подпись
        совпала с подтверждённой. → номер элемента свежей разметки."""
        from app.features import browser_actions as ba
        goal = str(action.get("goal") or "")
        url2, host2, items2 = ba.snapshot_elements(
            action.get("host"), tab_id=action.get("tab_id"))
        was = str(action.get("value") or "")
        if was.startswith("http") and str(url2 or "").startswith("http") \
                and _page_key(was) != _page_key(url2):
            raise RuntimeError(
                "элемент потерян — страница уже сменилась, повторно не "
                "нажимаю; повтори команду")

        def _lab(s) -> str:
            return " ".join(str(s or "").lower().split())[:80]

        want = str(action.get("element") or "")
        # «#12» — подписи у элемента не было: сверять не с чем
        want_l = "" if re.fullmatch(r"#\d+", want.strip()) else _lab(want)
        if want_l:
            same = [it for it in items2 if _lab(it.get("text")) == want_l]
            if len(same) == 1:
                return int(same[0]["idx"])
        # Свежий снапшот: подписи кандидатов — текст страницы; приватность
        # по его ПОЛНОМУ адресу (vk.com/im) и подтверждённому, не по хосту
        router = self._privacy_router(router, url2, was if was.startswith(
            "http") else None, host2 or action.get("host"))
        private = self.is_private_page(url2 or host2 or "") or (
            was.startswith("http") and self.is_private_page(was))
        idx2, _meta2 = self._choose_element(goal, items2, router, host=host2,
                                            page_url=url2 or None)
        if idx2 is None:
            raise RuntimeError(
                "элемент потерян — страница изменилась, "
                f"и «{goal[:40]}» заново не нашёлся")
        found = self._element_by_idx(items2, int(idx2)) or {}
        if _lab(found.get("text")) != want_l:
            # Подпись найденного на приватной странице (переписка, счёт) —
            # в ответ/историю/flavor не несём
            now = ("другой элемент" if private else
                   f"«{str(found.get('text') or '?')[:40]}»")
            raise RuntimeError(
                "элемент потерян — страница изменилась, а на месте "
                f"«{(want or goal)[:40]}» теперь {now}; не нажимаю, "
                "повтори команду")
        return int(idx2)

    def _verify_opened_site(self, action: dict):
        """Мягкая верификация после навигации: если сайт резолвился
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
            # title ещё не поднялся — страница грузится; «стоп» — не ждём
            if self._sleep_or_stop(0.5):
                break
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
            logger.warning(
                f"[CompControl] Открытая страница не похожа на «{name}»: "
                + self._label_for_log(ident, action.get("value"), limit=60))

    def _forget_tab(self, tab_id: Optional[int], url: str = "",
                    host: Optional[str] = None):
        """Единая инвалидация контекста при ЗАКРЫТИИ вкладки — обратная
        сторона _remember_tab. tab_id=None («закрой вкладку» без цели —
        закрывается видимая, её id неизвестен) тоже сбрасывает отслеживаемый
        id: иначе он указывал на мёртвую вкладку и КАЖДАЯ следующая команда
        ждала её до NAV_LOAD_TIMEOUT_SEC (~10 с), прежде чем упасть на хост.
        Хост/URL забываем только если закрыли именно их страницу: по хосту
        снапшот ещё может найти другую живую вкладку того же сайта.
        Закрытая вкладка мертва для всех чатов — её id снимаем у каждого, кто
        её отслеживал; хост/URL — только у текущего. Без id (закрыта видимая)
        другие чаты сверяются по странице: чья отслеживаемая — закрытый URL
        или его сайт, тот id теряет. Промах в сторону «забыть» дёшев (цель по
        хосту найдёт живую вкладку сайта сразу), мёртвый id стоил бы ~10 с."""
        # Хост закрытой — по её фактическому URL (у tab_id=None хост действия
        # — видимая на РЕЗОЛВЕ, а закрыта видимая на исполнении)
        closed = (urlparse(url or "").hostname or host or "").lower()

        def _same_site(h: Optional[str]) -> bool:
            h = (h or "").lower()
            return bool(closed and h) and (h == closed
                                           or h.endswith("." + closed)
                                           or closed.endswith("." + h))
        with self._state_lock():
            for _s in self._chat_states().values():
                if _s.last_tab_id is None:
                    continue
                if tab_id is not None:
                    dead = _s.last_tab_id == tab_id
                else:
                    dead = bool(url and _s.last_url == url) \
                        or _same_site(_s.last_host)
                if dead:
                    _s.last_tab_id = None
        if tab_id is None or self._last_tab_id == tab_id:
            self._last_tab_id = None
        if _same_site(self._last_host):
            self._last_host = None
            self._last_url = None

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
        """Многошаговая навигация: структура шагов в коде, каждый шаг —
        снапшот → выбор элемента (скоринг → при неоднозначности LLM) → клик →
        проверка эффекта → следующий шаг. Шаг, не нашедшийся на общем
        снапшоте, эскалирует: целевой снапшот/доскролл → широкий LLM-резолв →
        vision-фолбэк → LLM-восстановление (что нажать, чтобы приблизиться,
        или «пропустить» — шаг устарел). Устаревший шаг пропускается и без
        LLM, когда более поздний уже явный лидер на странице. Таймаут и
        честная ошибка, если застряли.
        Вкладка открывается отслеживаемой (стабильный id) — ни старые вкладки
        того же сайта, ни порядок окон навигации не мешают.
        Осечка — RuntimeError с честным текстом: что прошли и где встали.
        Пути выбора шагов пишутся в action["choose"] и при осечке — по
        аудиту видно, на каком шаге и каким путём встали."""
        trace: Dict[str, object] = {"paths": [], "tiers": [], "done": [],
                                    "meta": {}}
        try:
            self._navigate_steps(action, router, trace)
        finally:
            self._nav_record(action, trace)

    @staticmethod
    def _nav_record(action: dict, trace: Dict[str, object]):
        """Пути выбора шагов nav → action["choose"] (аудит): на успехе и
        на осечке одинаково."""
        step_paths = trace["paths"]
        step_tiers = trace["tiers"]
        done = trace["done"]
        meta = trace["meta"] or {}
        if not step_paths:
            return
        # Пути выбора шагов — в аудит: видно, где понадобилась LLM
        action["choose"] = {"path": ",".join(step_paths),
                            "candidates": meta.get("candidates") if done else [],
                            "llm_response": meta.get("llm_response") if done else None}
        # Поля выбора последнего шага (гибрид: уверенность, номер, бюджеты)
        # и след ярусов всех шагов — пересборка их не теряет
        if done:
            for k in ("conf", "wide_mode", "offscreen", "picked_n",
                      "n_boxes", "n_text"):
                if meta.get(k) is not None:
                    action["choose"][k] = meta[k]
        if step_tiers:
            action["choose"]["tiers"] = step_tiers

    def _navigate_steps(self, action: dict, router,
                        trace: Dict[str, object]):
        # Тело _navigate; trace — общий с _nav_record след шагов
        from app.features import browser_actions as ba
        tab_id = None
        tab_host = ""
        if action.get("gate_label"):
            # Продолжение маршрута после «да» на рискованный шаг: та же
            # вкладка, где маршрут остановился, — новую не открываем
            tab_id = action.get("resume_tab")
            if tab_id is None:
                tab_host = (urlparse(str(action.get("value") or "")).hostname
                            or str(action.get("host") or ""))
        elif sys.platform == "darwin" or ba.backend_forced():
            # Открытие по просьбе пользователя: переключаемся на страницу
            tab_id = ba.open_new_tab(action["value"], focus=True)
        else:
            try:
                tab_id = ba.open_new_tab(action["value"], focus=True)
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
        done: List[str] = trace["done"]
        step_paths: List[str] = trace["paths"]
        # След vision/широких ярусов эскалации по всем шагам — в аудит
        step_tiers: List[dict] = trace["tiers"]
        steps = list(action["steps"])
        # Первый шаг сразу после открытия: страница ещё грузится (скелет без
        # ссылок → «нет кликабельных элементов»). Ждём готовности документа
        # и стабилизации DOM до первого снапшота; best effort
        try:
            ba.wait_dom_idle(tab_host or action.get("host"), tab_id,
                             timeout_sec=NAV_SETTLE_SEC + 2.0, min_wait=0.8)
        except Exception:
            pass

        def _step_click(step_idx: int):
            """Клик шага + детект новой вкладки (target=_blank, window.open):
            открывшаяся страница становится вкладкой маршрута — следующие
            шаги целятся уже в неё («мы перешли на новую»), а старая вкладка
            остаётся открытой и нетронутой. На не-CDP бэкендах follow_popup
            молча None — маршрут остаётся на той же вкладке.
            Единственный клик шага маршрута — и единственная точка гейта
            подтверждения по фактически найденному элементу (подпись/aria/
            title, флаги выбора шага): все три пути выбора шага идут сюда."""
            nonlocal tab_id, host, url
            _m = meta if isinstance(meta, dict) else {}
            self._nav_gate(
                action, step_i, step,
                _m.get("picked_item")
                or self._element_by_idx(items or [], int(step_idx)),
                _m, {"host": host, "url": url, "tab_id": tab_id,
                     "done": done, "idx": int(step_idx)})
            pre_urls = ba.page_urls()
            ba.click_tagged(host, int(step_idx), tab_id=tab_id)
            try:
                pop = ba.follow_popup(pre_urls)
            except Exception:
                pop = None  # детект попапа — best effort, клик уже свершился
            if pop is not None:
                tab_id, host, url = pop
                logger.info(f"[CompControl] Шаг открыл новую вкладку "
                            f"#{tab_id} ({host}) — маршрут переходит на неё")

        for step_i, step in enumerate(steps):
            # «стоп» от пользователя — между шагами маршрута
            self._raise_if_stopped()
            # Ждём страницу с кликабельными элементами: первая загрузка и
            # переходы между страницами занимают секунды — опрашиваем снапшот
            url = host = items = None
            last_err: Optional[Exception] = None
            deadline = time.time() + NAV_LOAD_TIMEOUT_SEC
            while time.time() < deadline:
                try:
                    url, host, items = ba.snapshot_elements(tab_host, tab_id=tab_id)
                    break
                except Exception as e:
                    last_err = e
                    if self._sleep_or_stop(NAV_POLL_SEC):
                        self._raise_if_stopped()
            if items is None:
                where = f"после «{' → '.join(done)}»" if done else "после открытия"
                step_paths.append("no_items")
                if "нет кликабельных" in str(last_err):
                    raise RuntimeError(
                        f"страница {where} за {NAV_LOAD_TIMEOUT_SEC:g} с так "
                        "и не показала кликабельных элементов (пустая, "
                        "ошибка сайта или ещё грузится) — шаг "
                        f"«{step}» не с чего начать")
                raise RuntimeError(f"не читается страница {where}: {last_err}")
            # Маршрут мог привести на страницу входа/оплаты (в т.ч. по пути:
            # site.com/login) — дальше LLM-решения только локальной моделью;
            # обёртка «липкая»: вернуться к облаку в том же маршруте нельзя
            router = self._privacy_router(router, url or host)
            # Оверлей-блокер (куки/подписка/geo-попап) снимаем до выбора
            # элемента: он перекрывает цель шага. Сняли — снапшот протух,
            # переснимаем (индексы разметки относились к удалённым узлам)
            try:
                dismissed = ba.dismiss_overlay(tab_host or host, tab_id=tab_id)
            except Exception:
                dismissed = None
            if dismissed:
                action.setdefault("overlays", []).append(dismissed)
                url, host, items = ba.snapshot_elements(tab_host, tab_id=tab_id)
            so_far = f" (прошёл: {' → '.join(done)})" if done else ""
            # Шаг уже не нужен (страница сама ушла вперёд по плану): текущий
            # на странице не находится совсем, а более поздний — явный лидер
            if not (self._score_candidates(items, step, host=host)
                    or self._score_scoped(items, step)):
                later = self._later_step_leader(steps[step_i + 1:], items,
                                                host=host)
                if later is not None:
                    logger.info(f"[CompControl] Навигация: шаг «{step[:40]}» "
                                f"устарел — на странице уже «{later[:40]}», "
                                "пропускаю")
                    step_paths.append(f"skip_ahead:{step[:20]}")
                    continue
            # Клик по шагу. DOM живых сайтов перерисовывается между снапшотом
            # и кликом (меню с таймерами) — «элемент потерян» (клик НЕ ушёл)
            # лечим одним повтором: свежий снапшот → свежий выбор (тот же
            # пункт по подписи) → повторный клик. «Клик без эффекта»
            # (ClickUncertain — клик ДОСТАВЛЕН) не повторяем никогда:
            # тоггл-меню открылось бы и тут же закрылось
            meta: Dict[str, object] = {}
            last_lab = ""
            for attempt in (1, 2):
                idx, meta = self._choose_element(step, items, router,
                                                 host=host)
                if attempt == 2 and idx is not None and last_lab:
                    # Повтор целится в ТОТ ЖЕ пункт: при нескольких кандидатах
                    # предпочитаем элемент с прежней подписью
                    _same = [it for it in items
                             if " ".join(str(it.get("text") or "").lower()
                                         .split())[:80] == last_lab]
                    if len(_same) == 1:
                        idx = int(_same[0]["idx"])
                step_paths.append(str(meta.get("path") or "?")
                                  + (":retry" if attempt == 2 else ""))
                if idx is None:
                    break  # выбора нет — ниже эскалация резолва шага
                _it = self._element_by_idx(items, int(idx)) or {}
                last_lab = " ".join(str(_it.get("text") or "").lower()
                                    .split())[:80]
                try:
                    self._nav_click_honest(_step_click, idx, step, so_far,
                                           step_i == len(steps) - 1,
                                           step_paths)
                    break
                except (ba.ClickUncertain, NeedsConfirm):
                    # последний шаг: доставлен, эффекта не видно; гейт —
                    # стоп маршрута до «да», без повтора и обёртки
                    raise
                except Exception as e:
                    if attempt == 2 or "элемент потерян" not in str(e):
                        raise RuntimeError(f"на шаге «{step}»{so_far}: {e}")
                    action["retried"] = True
                    if self._sleep_or_stop(NAV_POLL_SEC):
                        self._raise_if_stopped()
                    url, host, items = ba.snapshot_elements(tab_host, tab_id=tab_id)
            if idx is None:
                # Эскалация резолва шага — тот же каскад, что у агентного
                # клика: целевой снапшот/доскролл → широкий LLM → vision
                idx, meta = self._resolve_nav_step(step, host, items,
                                                   tab_id, router,
                                                   page_url=url or "")
                step_paths.append(str(meta.get("path") or "?"))
                step_tiers.extend(meta.get("tiers") or [])
                if idx is not None:
                    try:
                        self._nav_click_honest(_step_click, idx, step, so_far,
                                               step_i == len(steps) - 1,
                                               step_paths)
                    except (ba.ClickUncertain, NeedsConfirm):
                        raise
                    except Exception as e:
                        raise RuntimeError(f"на шаге «{step}»{so_far}: {e}")
            if idx is None:
                # Последняя эскалация: LLM смотрит живой снапшот и решает —
                # что нажать, чтобы приблизиться к цели шага (открыть меню,
                # закрыть попап), «пропустить» (шаг устарел) или сдаться
                rec = self._nav_step_recover(step, steps, step_i, done,
                                             host, items, tab_id, router)
                if rec == "skip":
                    step_paths.append(f"llm_skip:{step[:20]}")
                    continue
                if rec:
                    ba.wait_dom_idle(tab_host or host, tab_id,
                                     timeout_sec=NAV_SETTLE_SEC + 2.0,
                                     min_wait=0.5)
                    _, host, items = ba.snapshot_elements(tab_host,
                                                          tab_id=tab_id)
                    idx, meta = self._choose_element(step, items, router,
                                                     host=host)
                    step_paths.append(str(meta.get("path") or "?")
                                      + ":after_recover")
                    if idx is not None:
                        try:
                            self._nav_click_honest(
                                _step_click, idx, step, so_far,
                                step_i == len(steps) - 1, step_paths)
                        except (ba.ClickUncertain, NeedsConfirm):
                            raise
                        except Exception as e:
                            raise RuntimeError(f"на шаге «{step}»{so_far}: {e}")
            if idx is None:
                raise RuntimeError(
                    f"не нашёл на странице пункт «{step}»{so_far}")
            done.append(step)
            trace["meta"] = meta
            # Переход + загрузка следующей страницы: ждём стабилизации DOM,
            # а не слепой слип — статичная страница отпускает раньше, живая
            # (дорендер SPA) — держит дольше в пределах бюджета
            ba.wait_dom_idle(tab_host or host, tab_id,
                             timeout_sec=NAV_SETTLE_SEC + 2.0, min_wait=0.5)
        if tab_id is not None:
            # Финальная страница пути — «открывшаяся страница» для следующих
            # команд («скачай на открывшейся странице …»)
            self._last_tab_id = tab_id

    @staticmethod
    def _nav_click_honest(click, idx: int, step: str, so_far: str,
                          last: bool, step_paths: List[str]):
        """Клик шага nav без повтора доставленного клика. ClickUncertain
        (клик ушёл, видимого эффекта нет — меню на CSS, тоггл) на
        промежуточном шаге — идём дальше: проверкой служит следующий шаг
        (его пункт найдётся, только если меню раскрылось); на последнем —
        честное «не уверен» с именем шага (класс ошибки uncertain).
        Остальные ошибки — наружу как есть (решает вызывающий)."""
        from app.features import browser_actions as ba
        try:
            click(idx)
        except ba.ClickUncertain as e:
            step_paths.append("uncertain")
            if last:
                raise ba.ClickUncertain(f"на шаге «{step}»{so_far}: {e}")
            logger.info(f"[CompControl] Навигация: шаг «{step[:40]}» — клик "
                        "без видимого эффекта, не повторяю; проверит "
                        "следующий шаг")

    def _later_step_leader(self, later: List[str], items: List[dict],
                           host: Optional[str] = None) -> Optional[str]:
        """Пропуск устаревшего шага плана без LLM: текущий шаг на странице
        не находится (проверяет вызывающий), а более поздний — уже явный
        лидер (сайт пропустил промежуточный экран: A/B-вёрстка, автопереход
        после логина). Возвращает текст такого шага или None."""
        for s in later:
            scored = self._score_candidates(items, s, host=host) \
                or self._score_scoped(items, s)
            if not scored:
                continue
            top_s = scored[0][0]
            second_s = scored[1][0] if len(scored) > 1 else None
            if top_s >= LEADER_MIN_SCORE and (
                    second_s is None or top_s - second_s >= LEADER_MARGIN):
                return s
        return None

    def _resolve_nav_step(self, step: str, host: str, items: List[dict],
                          tab_id: Optional[int], router,
                          page_url: str = ""):
        """Эскалация резолва шага навигации, когда общий снапшот шага не дал
        кандидата: целевой снапшот по всему DOM (пункт мог не влезть в бюджет
        общего) → доскролл-поиск → гибридный ярус (wide_mode: hybrid) либо
        широкий LLM-резолв → vision-фолбэк. Тот же
        каскад, что у агентного клика в _resolve_element, но на отслеживаемой
        вкладке навигации; выход один, и на нём — тот же инвариант вето.
        → (idx|None, meta); номер сквозной, и клик по нему одинаков, каким
        бы снапшотом элемент ни был помечен (meta["via"] — только для аудита).
        На свайп-лентах (page_url с /shorts/|/reels/) доскролл-поиск внутри
        _scroll_hunt сам пропускается: там он листает ролики."""
        from app.features import browser_actions as _ba
        router = self._privacy_router(router, page_url or host)
        search_goal = _goal_with_synonyms(step, host)
        pick: Optional[int] = None
        pick_meta: Dict[str, object] = {}
        pick_items = items
        g_items: List[dict] = []
        try:
            _gu, g_items = _ba.snapshot_for_goal(host, search_goal,
                                                 tab_id=tab_id)
        except Exception as e:
            logger.debug(f"[CompControl] Навигация: целевой снапшот шага "
                         f"«{step[:40]}» не удался: {e}")
        if not g_items:
            try:
                _gu, g_items = self._scroll_hunt(_ba, host, tab_id, search_goal,
                                                 page_url=page_url)
            except Exception as e:
                logger.debug(f"[CompControl] Навигация: доскролл-поиск шага "
                             f"«{step[:40]}» не удался: {e}")
                g_items = []
        if g_items:
            g_idx, g_meta = self._choose_element(step, g_items, router,
                                                 host=host)
            if g_idx is None:
                # Единственный отфильтрованный целевым снапшотом кандидат
                # безопасен и без LLM (как goal_sole в _resolve_element)
                sole = self._score_candidates(g_items, step, host=host) \
                    or self._score_scoped(g_items, step)
                if len(sole) == 1:
                    g_idx = int(sole[0][1]["idx"])
                    g_meta = {"path": "goal_sole",
                              "candidates": [{
                                  "idx": g_idx,
                                  "text": str(sole[0][1].get("text") or "")[:60],
                                  "score": round(sole[0][0], 1)}],
                              "llm_response": None}
            if g_idx is not None:
                g_meta["via"] = "goal_snapshot"
                pick, pick_meta, pick_items = g_idx, g_meta, g_items
        vis: Dict[str, object] = {}  # состояние vision на шаг (_vision_ready)
        hmeta = None
        if pick is None and self.wide_mode == "hybrid":
            # Гибридный ярус на общем снапшоте шага — как у агентного клика:
            # один vision-вызов вместо пары «широкий резолв → vision-рамки»
            hidx, hmeta = self._hybrid_pick(step, items, host, tab_id, router,
                                            vis=vis)
            self._note_tier(vis, "vision_hybrid", hmeta)
            if hidx is not None:
                pick, pick_meta = hidx, hmeta
            elif hmeta is not None:
                pick_meta = hmeta  # вердикт vision — в путь шага для аудита
        if pick is None and (hmeta is None
                             or hmeta.get("fail") == "invalid"
                             or hmeta.get("veto") == "label_mismatch"):
            # Широкий LLM-резолв — на общем снапшоте шага: режим text, гибрид
            # не запускался / vision лежит, ответ гибрида невалиден или его
            # выбор рамки ветирован сверкой подписи (как в каскаде клика)
            widx, wmeta = self._llm_wide_pick(step, items, router)
            self._note_tier(vis, "llm_wide", wmeta)
            if widx is not None:
                pick, pick_meta = widx, wmeta
        if pick is None and hmeta is None:
            # Рамки — только если гибрид не смотрел страницу (он их
            # надмножество); лежащий vision ярус пропускает сам
            vidx, vmeta = self._visual_resolve(host, tab_id, items, step,
                                               router, vis=vis)
            self._note_tier(vis, "vision", vmeta)
            if vidx is not None:
                pick, pick_meta = vidx, vmeta
        pick_meta = self._with_tiers(pick_meta, vis)
        # Единственный выход эскалации шага — инвариант вето здесь же
        if pick is not None and self._veto_destructive(
                step, self._element_by_idx(pick_items, pick), pick_meta, host):
            return None, pick_meta
        if pick is not None and isinstance(pick_meta, dict):
            # Подписи выбранного элемента — гейту клика маршрута (_nav_gate):
            # целевой снапшот нумерует элементы вне общего списка шага
            _pi = self._element_by_idx(pick_items, pick) or {}
            pick_meta["picked_item"] = {k: _pi.get(k) for k in
                                        ("text", "aria", "title")
                                        if _pi.get(k)}
        return pick, pick_meta

    def _nav_step_recover(self, step: str, steps: List[str], step_i: int,
                          done: List[str], host: str, items: List[dict],
                          tab_id: Optional[int], router):
        """Сбойный шаг навигации (план разошёлся с реальностью): LLM по
        живому снапшоту решает, что нажать, чтобы приблизиться к цели шага
        (открыть меню, закрыть незапланированный попап), — или «пропустить»
        (шаг устарел, страница сама ушла дальше), или «нет». → True — клик
        восстановления выполнен (шаг повторяем снаружи); "skip" — шаг
        пропускаем; False — честный отказ. Тот же приём, что
        LLM-восстановление шагов сценария (ScenarioManager._llm_recover):
        модель выбирает из реальных элементов, клик исполняет система."""
        router = self._privacy_router(router, host)
        if router is None or not items:
            return False
        try:
            scored = self._score_candidates(items, step, host=host)
            ranked = [it for _s, it in scored]
        except Exception:
            ranked = list(items)
        top = ranked[:15]
        ctl = [it for it in items
               if _NAV_CTL_RE.search(str(it.get("text") or ""))
               and all(it.get("idx") != t.get("idx") for t in top)]
        shown = (top + ctl)[:25]
        if not shown:
            return False
        lines = "\n".join(
            f"{n}) [{it.get('tag')}/{it.get('role') or '-'}] "
            f"{str(it.get('text') or it.get('aria') or '')[:60]}"
            for n, it in enumerate(shown, 1))
        roadmap = []
        for n, s in enumerate(steps):
            mark = "✓" if n < step_i else ("✗" if n == step_i else "·")
            roadmap.append(f"{mark} {n + 1}. {s}")
        prompt = (
            "I am opening a page step by step (✓ — already done, ✗ — broke "
            "here, · — next):\n" + "\n".join(roadmap) + "\n\n"
            f"At step ✗ I need to click \"{step}\", but there is no such element among "
            f"the visible ones on the page {host}.\n"
            f"Visible page elements:\n{lines}\n"
            "Maybe a menu has to be opened first, a popup closed, "
            "or the element has a different name. Reply with ONLY the number of the element "
            "worth clicking to get closer to the goal of step ✗. "
            "If step ✗ is no longer needed (the page already moved on along the plan) — "
            "reply \"skip\". If nothing will help — reply \"no\".\n"
            + user_language_line(detect_language(step)))
        try:
            resp = router.get_response([{"role": "user", "content": prompt}],
                                       temperature=0.0, max_tokens=8, top_p=0.1,
                                       webchat_channel="cc", force_provider=getattr(router, "cc_provider", None))
        except Exception as e:
            logger.debug(f"[CompControl] LLM-восстановление шага недоступно: {e}")
            return False
        self.stats["llm_calls"] += 1
        if _llm_said_skip(resp):
            logger.info(f"[CompControl] Навигация: шаг «{step[:40]}» устарел "
                        "по мнению LLM — пропускаю")
            return "skip"
        m = re.fullmatch(r"\s*(\d{1,2})\s*", resp or "")
        if not m or not (1 <= int(m.group(1)) <= len(shown)):
            logger.info(f"[CompControl] LLM-восстановление шага: нет "
                        f"кандидата ({(resp or '')[:40]!r})")
            return False
        item = shown[int(m.group(1)) - 1]
        # Клик тут исполняется на месте, без общего резолва — инвариант вето
        # проверяем перед ним (ранжирования нет, уступать некому: отказ)
        if self._veto_destructive(step, item, None, "восстановление шага"):
            return False
        # Вспомогательный клик выбрала модель по недоверенным подписям:
        # оплату/оформление/отправку/удаление им не жмём никогда (гейт
        # маршрута на нём не спросит — это не шаг, а обходной клик). Шаг
        # встанет честным «не нашёл», человек нажмёт сам или скажет явно
        _probe = {"kind": "click", "host": host}
        for _k, _dst in (("text", "element"), ("aria", "aria"),
                         ("title", "title")):
            if item.get(_k):
                _probe[_dst] = str(item[_k])[:80]
        if self.risky_label(_probe):
            logger.info(f"[CompControl] LLM-восстановление шага: выбор "
                        f"рискованный ({self.risky_label(_probe)}) — не жму")
            return False
        from app.features import browser_actions as ba
        from app.features.browser_actions import ClickUncertain
        logger.info(f"[CompControl] LLM-восстановление шага «{step[:40]}»: "
                    f"жму «{self._label_for_log(item.get('text') or '', host, idx=item.get('idx'))}»")
        try:
            ba.click_tagged(host, int(item["idx"]), tab_id=tab_id)
        except ClickUncertain:
            # «Нет видимого эффекта» — годится: JS-меню открывается без
            # изменения DOM-отпечатка; повтор шага снаружи покажет, помогло ли
            return True
        except Exception as e:
            logger.info(f"[CompControl] LLM-восстановление: клик не удался: {e}")
            return False
        return True

    def _audit(self, chat_id: str, action: dict, ok: bool, detail: str,
               error_class: Optional[str] = None,
               extra: Optional[Dict[str, object]] = None):
        try:
            from app.features.cc_privacy import (
                audit_append, contains_value, known_secret_values, mask,
                redact_audit_record, redact_typed)
            # Единая точка маски ввода, какой бы ни была подпись поля и путь
            # исполнения (агент, сценарий, маркер, «да»): приватная страница
            # (по полному URL и хосту, у multi — любого шага) и известные
            # секреты чатов (пароль, данный ответом, — в поле «Поиск»)
            known = known_secret_values()
            private = self._page_private(action)

            def _typed(a: dict) -> str:
                t = a.get("text")
                if private or contains_value(t, known):
                    return mask(t)
                return redact_typed(t, a.get("element"),
                                    bool(a.get("field_sensitive")))
            if action.get("kind") == "multi":
                # describe() печатает введённый текст — для лога описываем
                # копии с уже замаскированным вводом
                value = " ; ".join(self.describe(
                    dict(a, text=_typed(a))
                    if a.get("kind") == "type" else a)
                    for a in action["items"])
            else:
                value = action.get("value")
            record = {
                "ts": time.time(), "chat_id": str(chat_id), "ok": ok,
                "kind": action.get("kind"), "key": action.get("key"),
                "value": value,
                "detail": detail,
            }
            # Семантика действия для записи сценариев (ScenarioManager строит
            # шаги по тексту элемента, а не по idx — он между сессиями нестабилен)
            if action.get("element"):
                el = str(action["element"])[:80]
                if private and ("task" in (action.get("origin"),
                                           action.get("pending_from"))
                                or re.search(r"\d", el) or len(el) > 30):
                    # Приватная страница: подпись с суммой/временем/номером
                    # или длинная (имя, текст переписки) и любой шаг агента —
                    # длиной. Короткие «Войти»/«Пароль»/«Далее» остаются:
                    # по ним записанный сценарий входа находит шаг
                    el = mask(el)
                record["element"] = el
            if action.get("host"):
                record["host"] = action["host"]
            if action.get("field_sensitive"):
                # Флаг поля (пароль/email/tel) — сценарий сделает из ввода
                # слот «спросить каждый раз», аудит — маску
                record["field_sensitive"] = True
            if action["kind"] == "type" and action.get("text"):
                t = str(action["text"])
                record["text"] = (mask(t) if private or contains_value(t, known)
                                  else t[:80])
            # Наблюдаемость без утечек: откуда пришло действие, был ли
            # повтор, длительность исполнения и каскада резолва
            # from_search — адрес из выдачи собственного поиска агента задач
            # (только для аудита, гейт его не видит — в отличие от via_search)
            for k in ("origin", "pending_from", "retried", "via_search",
                      "from_search", "choice", "duration_ms", "task_run"):
                if action.get(k) is not None:
                    record[k] = action[k]
            # Корзина/состав: value у них пуст — товар и операция, иначе по
            # аудиту не понять, что именно не удалилось; op — и у
            # tab_op/media_vol; slider_path — громкость ушла в <video>
            if action.get("kind") in ("cart", "comp_edit") \
                    and action.get("product"):
                record["product"] = str(action["product"])[:80]
            if action.get("op") is not None:
                record["op"] = str(action["op"])[:20]
            if action.get("slider_path"):
                record["slider_path"] = action["slider_path"]
            # Наблюдаемость: какой путь сработал (score/LLM/fallback),
            # какие кандидаты рассматривались и с каким скором, сырой ответ
            # LLM, результат closed-loop проверки и класс ошибки
            choose = action.get("choose") or {}
            if choose.get("path"):
                record["path"] = choose["path"]
            if choose.get("candidates"):
                record["candidates"] = choose["candidates"]
            if choose.get("llm_response"):
                record["llm_response"] = choose["llm_response"]
            # Сравнение режимов zero-match яруса (wide_mode hybrid/text):
            # уверенность модели, номер ответа, бюджеты гибрида, выбор
            # строкой вне экрана и след всех ярусов резолва
            for k in ("conf", "offscreen", "picked_n", "n_boxes", "n_text",
                      "tiers", "resolve_ms", "n_pool", "label_unverified",
                      "other_tab"):
                if choose.get(k) is not None:
                    record[k] = choose[k]
            if action.get("kind") in _RESOLVE_KINDS:
                # Режим — из конфига, на каждой записи резолва/клика (не
                # только когда ярус его пометил): сравнивать режимы по аудиту
                record["wide_mode"] = self.wide_mode
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
                # Мягкая верификация «тот ли сайт открыли»
                record["name_check"] = action["name_check"]
            if extra:
                record.update(extra)
            # Секреты в лог не пишем: маска ввода в чувствительные поля и
            # секретоподобных значений, URL без токенов/фрагментов, тексты
            # страницы обрезаны (cc_privacy — те же правила, что у
            # scripts/scrub_cc_audit.py). Ротация 10 МБ × 3 файла
            record, _ = redact_audit_record(
                record, getattr(self, "private_hosts", ()),
                getattr(self, "private_hosts_builtin", True), known)
            audit_append(self.base_dir / "audit.jsonl", record)
        except Exception as e:
            logger.debug(f"[CompControl] Аудит-лог не записан: {e}")
        # Каждый исполненный ввод (любой путь: сценарий, агент, маркер,
        # «да») — хуку приватности истории бота (маска ввода в STM и
        # KnownSecrets), до записи ответа хода
        hook = getattr(self, "on_typed", None)
        if callable(hook) and action.get("kind") in ("type", "multi"):
            try:
                hook(action)
            except Exception as e:
                logger.debug(f"[CompControl] хук ввода упал: {e}")

    def _audit_resolve(self, chat_id: str, goal: str, host: Optional[str],
                       detail: str, fail_reason: str,
                       meta: Optional[dict] = None):
        """Неудача РЕЗОЛВА элемента (до построения действия) — в тот же
        audit.jsonl, чтобы причины отказов были видны, а не выяснялись
        вслепую по жалобам. fail_reason: no_page | snapshot_error |
        not_in_snapshot | low_score | llm_veto | destructive_veto | no_fields |
        captcha | label_mismatch | budget (кончился бюджет каскада) |
        not_a_click (цель — клавиша/листание/сайт, а не элемент) — база для
        авто-классификации причин отказов."""
        extra: Dict[str, object] = {"fail_reason": fail_reason}
        if meta:
            if meta.get("path"):
                extra["path"] = meta["path"]
            if meta.get("candidates"):
                extra["candidates"] = meta["candidates"]
            if meta.get("llm_response"):
                extra["llm_response"] = meta["llm_response"]
            for k in ("conf", "offscreen", "picked_n", "n_boxes", "n_text",
                      "tiers", "resolve_ms", "origin", "n_pool"):
                if meta.get(k) is not None:
                    extra[k] = meta[k]  # как в _audit — сравнение режимов
            if meta.get("veto"):
                extra["veto"] = meta["veto"]
                if meta.get("vetoed"):
                    extra["vetoed"] = meta["vetoed"]
        self._audit(chat_id, {"kind": "resolve_fail",
                              "value": str(goal)[:80],
                              "host": host or ""},
                    False, detail, extra=extra)
        from app.features.cc_privacy import redact_inline
        logger.info(f"[CompControl] Резолв «{redact_inline(str(goal)[:40])}» "
                    f"не удался ({fail_reason}): "
                    f"{redact_inline(str(detail)[:80])}")
