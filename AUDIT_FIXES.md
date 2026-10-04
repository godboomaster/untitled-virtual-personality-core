# Аудит и исправления — ветка `fix/audit-control-mode`

Дата: 2026-09-21…22. Ветка создана от `web_extended` вместе с её незакоммиченными
изменениями. **Ничего не закоммичено и не запушено** — все правки лежат в рабочем дереве.

Интерпретатор с зависимостями: `/usr/local/bin/python3` (системный `python3` из
homebrew — без зависимостей). Запуск тестов: `PYTHONPATH=. /usr/local/bin/python3 scripts/<test>.py`.
Скрипты тестов возвращают код 0 даже при FAIL — смотреть `grep FAIL`.

## Итоговый прогон (общее дерево после всех задач, 2026-09-22)

| Тест | Проверок | FAIL |
|---|---|---|
| test_computer_control | 936 | 0 (2 средовых FAIL `visible …` при открытом браузере бота) |
| test_browser_actions (новый) | 134 | 0 |
| test_web_llm | 105 | 0 |
| test_api_security | 103 | 0 |
| test_misc_features (новый) | 60 | 0 |
| test_state_io | 22 | 0 |
| test_memory_core (новый) | 73 | 0 |
| test_memory_wipe | ALL OK | 0 |
| test_bg_isolation | 13 | 0 |
| test_reminder_parse | 63 | 0 |
| test_calendar | 20 | 0 |
| test_flavor_text | 30 | 0 |
| test_rhythm | 64 | 0 |
| test_timeutil (новый) | 32 | 0 |
| test_scenario (новый) | 51 | 0 |
| test_presence (новый) | 52 | 0 |
| test_wave2_sweep (новый) | 55 | 0 |
| test_retention (новый) | 37 | 0 |
| test_settings_tz (новый) | 36 | 0 |
| test_living_persona | 30 | 0 (флак `mood_impact` — зависит от Gemma) |
| test_intellect | 38 | 0 |
| test_search, test_conversation_style, test_split_messages | OK | 0 |

`compileall app scripts migrate_stm.py migrate_stm_500.py` — без ошибок. `test_photo_reply` не
стартует в `/usr/local/bin/python3` — там нет модуля `telegram` (не дефект кода).
Счётчик `check()` в test_computer_control при FAIL вычитает 100, поэтому «677 проверок / 2 FAIL»
предыдущего прогона = 877 успешных.

**Важно: всё проверено на моках/фейках и в node; вживую в браузере НЕ проверялось ничего.**
Перед слиянием — ручной прогон режима управления (см. «Проверить вживую»).

test_computer_control и test_web_llm мокают `internet_available()` (TCP-пробы 1.1.1.1:443 /
8.8.8.8:53 в песочнице молчат, а роутер честно уходит в «офлайн» — были ложные FAIL).

Второй проход (2026-09-22) сделан двенадцатью агентами по непересекающимся файлам; принцип —
корень проблемы и одно общее решение, применённое во всех местах, а не заплатка на симптом.

---

## Сделано

### №1. Подтверждение действия пробивалось отказом (CRITICAL)
«не открывай» / «давай не будем» → YES → исполнялось pending-действие (в т.ч. RUN_TASK).
- Корень: два независимых `re.search` по всему тексту любой длины и происхождения.
- `computer_control.py` `classify_confirmation`: клаузы по пунктуации + токены; отрицание
  (не/not/don't/никогда) рядом с да-словом или словом желания → NO; NO любой клаузы
  побеждает; непонятное «не» → UNKNOWN; YES только для реплики ≤10 слов без «?».
- Источник текста: `process_message(raw_user_text=…)` — подтверждение классифицируется
  только по тому, что человек написал (текст/подпись), не по OCR/тексту документа
  (Telegram и оба эндпоинта веб-API).
- `learning_manager.classify_continue_answer` переведён на тот же классификатор (см. №18).

### №2. Авторизация режима управления + pre_check для фото/документов (CRITICAL)
- `BotInstance._cc_allowed()` — единая точка: владелец или `features.computer_control.allowed_users`.
  Ею закрыты: переключатель режима, «почини браузер», fast-path, подтверждение pending,
  исполнение маркеров LLM (неавторизованному — вырезаются), автопредложение сценария,
  CC-нота в system prompt (URL открытой страницы).
- Неавторизованному режим «не существует» (обычный диалог). Владелец не задан → в Telegram
  fail-closed + warning при старте; веб-API не затронут (`web_single_user`).
- `telegram_bot._gate_update()` — общий pre_check для текста, фото, документов.
- Документация: `persona_template.yaml`, README («Авторизация»).

### №3. Вето на разрушительный клик — инвариант (HIGH)
- Корень: вето жило в отдельных ветках; фолбэк по скору возвращал ветированный элемент;
  `_destructive_mismatch` — список корней по одному полю; открытые корни `закр\w+`/`скро\w*`
  подменяли намерение («нажми закрепить» → крестик).
- Одно определение «разрушительного» (RU/EN формы, иконки, text+aria+title); фильтр на входе
  скоринга (решение само переходит к следующему кандидату); единый `_veto_destructive` на
  выходе; `_resolve_element` — обёртка-инвариант над `_resolve_element_pick`; дубли удалены;
  аудит: `destructive_veto`, `meta["veto"]/["vetoed"]`.
- `_CLOSE_VERB_RE`, `_CLOSE_GOAL_RE`, намерение — явные словоформы.
- Флапающий тест «дедуп карт … вето» — был незамоканный `all_clickable_boxes` (ходил в живой Chrome).

### №4. Адресация размеченных элементов (HIGH)
- Корень: «номер элемента» не был однозначным (две разметки idx/gidx с пересекающейся
  нумерацией, общий диапазон у iframe, протухшие метки).
- Сквозная нумерация: `_mark_base()` выдаёт непересекающиеся блоки (снапшот 100, iframe 25,
  целевой 25), номера не переиспользуются, счётчик засеян от часов. Флаг `gidx`/параметр
  `mark` удалены; поиск через общий `_mark_sel`/`_mark_find_js`. Исправлены: скачивание
  чужого файла, коллизия main/iframe, клик по протухшей метке (теперь «элемент потерян» →
  существующий перерезолв).

### №5. raw-CDP транспорт пула H и таймауты воркера (HIGH)
- `_eval_in_tab()` — единый диспетчер JS по транспортам; 11 мостов переведены. Невозможное
  для raw-вкладки → `RawTabUnsupported`.
- `detect_antibot(strict=True)`; в web_llm «сбой замера ≠ чисто», карантин снимается только
  по положительному подтверждению.
- Монотонные id вкладок под локом; reattach после обрыва сокета; «session not found» → один
  reattach+retry; `close_background_tab()` (`Target.closeTarget`), web_llm закрывает бросаемые вкладки.
- Таймаут ответа CDP — per-call от бюджета операции; таймаут вызова не рвёт соединение.
- `_CdpWorker.submit(timeout)` (дефолт 45 с + явные бюджеты), «отравленный» воркер
  пересоздаётся; restart/shutdown/выход не виснут.
- **Правка оркестратора:** авто-уборка сирот `_sweep_orphan_tabs` выключена по умолчанию
  (`browser.sweep_orphan_tabs: true` для включения) — Chrome пула H общий для раздельно
  запущенных процессов персон, второй процесс закрыл бы живые чаты первого.

### №6. Ввод API → пути ФС и .env (CRITICAL)
- Найдено больше, чем в аудите: `/api/personas/{persona}/config|initiative` давали ЗАПИСЬ в
  произвольный существующий файл.
- Новый `app/api/security.py`: единый формат id, `safe_join()` (resolve-проверка),
  `PersonaId/SafeId` (pydantic), `PersonaIdPath/Query` (422 на входе), `persist_env/remove_env`
  (отказ на \r \n NUL, атомарно, под локом, round-trip), `atomic_write_text` + `yaml_write_lock`.
- Применено: schemas.py, 37 параметров в server.py, runtime, settings_api, clear_backup, drafts_api.
- SSE не отдаёт `str(e)`; `hmac.compare_digest`; warning при не-loopback без `API_TOKEN`.

### №7. web_llm: сигнал сайта vs текст ответа (HIGH)
- `_classify_site_signal()`: rate-limit / too-many-images ищутся только в баннерах/error-скоупе;
  тело ответа — только для deepseek/qwen/google и только если короткое (≤200).
- Попутно: после первого совпадения баннер-пробник не форсировался → 2 замера подряд не
  набирались; готовый ответ не выбрасывается при сомнительном сигнале.
- `web_llm_state.json`: общий на процесс лок по пути файла + атомарная запись + инкремент
  квоты одним проходом (в процессе несколько независимых ModelRouter на один файл).

### №8. Closed-loop проверка эффекта действий (HIGH)
- Трёхзначная модель `EFFECT_CHANGED/SAME/FAILED`; навигация определяется без JS; сбой замера
  никогда не «успех»; общий `_wait_effect` для клика/наведения/ввода/координат и мостов.
- Отпечаток без `innerHTML`: aria-счётчики, диалоги, фокус, прокрутка, 5 hit-тестов,
  ограниченный обход DOM (≤1500 узлов, глубина ≤20).
- `_HOVER_CHECK_JS` — функция; `_eval_arg` + линтер на «IIFE + arg».
- Прокрутка: шаг с `scroll-behavior:auto`; rAF-листание завершается по застою + потолок;
  дозорный поток с дедлайном, самозавершение очищает `self._scroll` (15 с памяти причины);
  `scroll_stop` не требует подтверждения (поведение без подтверждения не менялось).
- Старый FAIL «доскролл: промах» — настоящий дефект (`_scroll_hunt` возвращал окно дважды), исправлен.
- Зоны vision клампятся к вьюпорту; координаты вне вьюпорта — отказ без клика.

### №9. Состояние менеджеров и время (HIGH/MEDIUM)
- Новый `app/core/atomic_io.py`: атомарная запись + `load_json_safe` (битый файл → warning +
  `.corrupt-<ts>` + дефолт). Переведены: reminder, calendar, rhythm, scenario, inventory,
  learning, flavor_text, chat_dossier, env_context, proactive_messaging (все 5 сохранений).
- Локи: proactive (RLock, все RMW), scenario, inventory, todo (`get_list`/`clear`),
  chat_dossier (8 методов), env_context. Todo: экранирование \n (старый формат читается).
- `learning_manager.start()` идемпотентен; setup-состояние по (chat_id, user_id);
  `flavor_text`: атомарный check-and-add, `_pick` перебирает всех кандидатов.
- Время: одна таблица-источник слов времени (опечатка «полуночь», рассинхрон RU 24 / EN 0),
  `_normalize_hour`; recurring на 24:00; «мне» в задаче; перенос <10 с; `calendar.update_entry`
  валидирует title/time; ночное окно rhythm в секундах.
- `BotInstance.on_user_message()` — `note_presence` до `record_activity` (утреннее приветствие
  в Telegram не срабатывало никогда).

### №10. Подстановка текста в JS и общая нормализация (MEDIUM)
- `_js_fill/_js_value` — единственный путь подстановки (плейсхолдер = значение, JSON-литерал,
  U+2028/2029, `</`); вырезание кавычек убрано (`L'Oréal`, `Papa John's` находятся).
- `_as_lit` — единый экранировщик AppleScript; закрыта неэкранированная подстановка host/origin.
- `_VPC_NORM_CORE_JS` — одна JS-нормализация, зеркалящая `_norm_match` (ё→е, диакритика
  латиницы без порчи «й», апострофы, тире); посимвольное совпадение с питоном проверено в node.
- `_HIDDEN_EDITABLES_JS` в IIFE; cart-варианты через JSON; `download_in_tab` через `_js_fill`.
- Линтеры: нет `.replace("__X__")` вне helper'а, нет плейсхолдеров внутри кавычек.

---


### №11. Разбор команд режима управления (`computer_control.py`)
- Тест «cc-нота перед conv-нотой» устарел (после conv-ноты намеренно идёт нота языка) — переписан
  на порядок CC → CS → язык. Второй FAIL «оверлей: цель-закрытие» — незамоканные
  `modal_visible`/`open_list_visible` ходили в живой Chrome; добавлены дефолт-моки.
- Корни и общие решения: слова направления листания — одни наборы `_SCROLL_UP/DOWN/SIDE_WORDS`
  для regex и срезки контейнера («пролистай комментарии вниз» → «комментарии»);
  `_PAGE_VIEW_WHAT_RE` — `\s+` снаружи группы; `_snapshot_for` — нераспознанное место даёт
  причину («Не знаю, где «X»»), а не последнюю вкладку (`_site_word_tracked()`; у click/hover
  слово по-прежнему становится скопом карточки — решение №3); `_match_field_anywhere` —
  off-by-one; `process_markers` — несколько маркеров = одно действие `multi`; `_forget_tab()` —
  обратная сторона `_remember_tab`, закрытие видимой вкладки сбрасывает `_last_tab_id`;
  `_snapshot_state()` — состояние снапшота одной четвёркой (url, host, items, tab_id);
  `submit` отменяет послабление `type_text_safe_fields`; `auto_dismiss` у `_snapshot_for`
  по умолчанию False — резолв без побочек, оверлей снимается только у выбора элемента по
  снапшоту (клик/наведение/скачивание/подбор поля).
- LOW: `_ORDINAL_MAX=20` («0 результат» → None); `_RISK_SAFE_KEYS` + «k»; «включи звук» —
  направленный `unmute`; `_looks_like_domain()` — одно определение «это домен, а не файл» в
  6 местах + `resolve_url`; «назад в будущее» — хвост у back/forward только «на <слово>»;
  cart-regex вынесены в константы с общим IGNORECASE; `resolve_intent_llm` отличает сбой LLM
  от «не команда».
- `_veto_model_pick()` — единая проверка любого выбора «по номеру от модели» (разрушительный
  контрол без намерения + галлюцинация номера по подписи): `_visual_resolve`, `_vision_zones`
  (проверки не было — галлюцинация уходила в координатный клик), `_llm_wide_pick`
  (`label_check=False`, ярус ради «другого названия»). Причина в аудите — `label_mismatch`.
- Тип операции сквозь каскад: `_destructive_mismatch(goal, it, op)`,
  `_NON_ACTIVATING_OPS={"hover"}` — «наведи на очистить очередь» работает, клик ветируется;
  ветированный выбор vision/LLM уступает следующему ярусу (единое поведение).
- SSRF: `_first_result_url` идёт через общий `web_search.get_with_safe_redirects()`
  (проверка исходного URL и каждого хопа, `follow_redirects=False`).

### №12. Браузерный слой (`browser_actions.py`, `chrome_debug.sh`)
- `_CdpWorker.current_user_page()` — одно понятие «текущая пользовательская вкладка»
  (`scan_search`, reload/close/history); `_abort_launch()` добивает Chrome при любом сбое
  запуска (пулы V и H), на основном профиле — только свой процесс.
- AppleScript: имя приложения зашито было в 8 скриптах. Одна точка формирования `tell`:
  `_as_app_name/_as_tell/_as_tell_to/_as_proc_ref(pid)/_as_browser_pids/_as_single_target/
  _as_run`; guard «приложение запущено» (Apple Events больше не запускают браузер). Chrome-suite
  по pid не адресуется (проверено: -1728), поэтому при >1 экземпляре мосты по имени
  отказываются работать (раньше переднее окно личного Chrome выдавалось за видимую страницу).
- Preferences: `_load_prefs/_save_prefs/_prefs_editable` — нечитаемый файл не перезаписывается,
  на Windows живой профиль не правится; `_pid_alive` платформенный (ctypes на win32,
  `PermissionError` → жив).
- `is_service_host` — одно определение (list_tabs_detailed, list_pages, follow_popup).
  `_all_pages/_purge_pages/_register_page` — единый учёт страниц с очисткой; `_hist_owner`
  (weakref) не даёт унаследовать чужую историю при переиспользовании `id(page)`.
- `full_page_capture` сохраняет исходный inline `display`; `_dedup_snapshot_items` — правила
  href и текста оба, общий `_texts_dup`. `set_slider`: `_slider_accepted()`, `_slider_unmark()`
  в `finally`. `_gateway_status(budget_sec)`, `SUBMIT_NAV_TIMEOUT_SEC` вычисляется из шагов;
  `set_tab_frozen` удалён (мёртвый).
- Отравленный воркер: `_poison()` кладёт `(pw, browser)` в `_abandoned[gen]`, зависший поток
  после возврата закрывает своё соединение (`pw.stop()`), не Chrome.
- `BackendUnsupported(BrowserUnavailable)` + `_no_backend()` — 18 точек «только CDP»;
  `chat_wait_uploaded` ловит тип, не текст.
- Лимиты JS ↔ константы: `var M=<SNAPSHOT_MAX>`/`<GOAL_SNAPSHOT_MAX>` собираются в шаблон,
  этапные бюджеты — доли M. Стемминг: `_GOAL_SNAPSHOT_JS` на `_VPC_NORM_JS` (`__vpcWIn`),
  своя копия удалена. `press_key` считает сделанные нажатия и добивает остаток;
  `PRESS_TIMES_MAX=100` — единственный потолок (`_ERASE_MAX` ссылается на него).
  `_MEDIA_VOLUME_JS`: явные `mute`/`unmute`/`toggle_mute`; `media_volume_op` бросает
  `BrowserUnavailable` на причину из JS (строка-отказ шла в отчёт как «изменил громкость»).
- `chrome_debug.sh` берёт флаги/профиль/порт/бинарь у самого модуля (`--mute-audio` ушёл,
  anti-throttling флаги совпадают с кодом).

### №13. Ядро памяти (`self_memory`, `memory`, `file_vector_db`, миграции)
- Новый `app/core/chroma_space.py`: `open_collection()` — единственная точка открытия коллекций
  с явной метрикой cosine; у существующих l2-коллекций перенос вместе с эмбеддингами (страницы
  по 500, временная `<имя>.mig-cosine`, сверка количества, доигрывание после обрыва). Замер:
  факт vs перефразировка — l2 d=14.26, cosine d=0.363 → `forget`/`update_fact` при l2 не
  находили факт никогда. `FORGET_MAX_DISTANCE` 1.0 → 0.7: на cosine 1.0 — почти вся шкала,
  посторонний запрос (d≈0.9–0.98) удалял бы первый попавшийся факт; перефразировка — d≈0.36.
  Переведены: memory, restore_memory, migrate_stm*, migrate_embeddings,
  load_book, book_search (там же `rerank_score = 1 - distance` предполагал cosine).
- `self_memory`: LLM вне лока — `_append_episode()`/`_commit_summary(taken, epoch)`,
  `_epoch` (очистка во время LLM не наполняется старым), `_summarize_inflight`;
  `clear_all/import_state/get_context_block` под локом; atomic_io.
- Новый `app/core/bounded_cache.py` (LRU + TTL, dict-API): STM-буферы (200, с ленивой
  подгрузкой чата из БД — `_get_buffer/_load_chat_from_db/_entry_from_row`), счётчики (500),
  `FileVectorDB._loaded_docs`, `rate_limiter`, `chat_dossier._facts_seen/_facts_watermark`,
  `living_persona._chat_user_lang/_harvest_*`, presence. Персистентные структуры
  (`proactive._chat_topics`, `state_engine._states`, `relationship._chats`) намеренно не тронуты.
- `FileVectorDB`: `@_locked` (RLock) на всех методах. `migrate_stm.py`/`migrate_stm_500.py`:
  общий код `load_import_file → backup_collection → replace_collection` с откатом, бэкап в
  `memory_export/` в формате `restore_memory`. `rich_message_formatter`: `_escape_html/
  _stash_code/_restore_code` — код-блоки экранируются в обоих методах.
  `restore_memory.RESTORE_MAP` удалён — `find_latest_export()` по именам файлов.

### №14. Часовой пояс пользователя (`app/core/timeutil.py`)
- Корень: у каждого менеджера свой `datetime.now()` = пояс процесса; настройки не было.
  `timeutil.now()/today()/from_ts()/to_ts()/tz()`; источник — `TIMEZONE` (IANA), фолбэк `TZ`,
  иначе системный пояс (прежнее поведение). Договор: naive «стенные часы пользователя»,
  epoch↔datetime только через `from_ts/to_ts` (у naive `.timestamp()` — системный пояс).
- Переведены: reminder (парсинг «завтра в 9», `_next_occurrence`), rhythm (гейты утра/ночи),
  env_context, proactive (`_get_today`, `_in_initiative_hours`), scenario, state_engine
  (`_now_iso` + чтение через `to_ts`), world_engine, offline_summarizer, inventory, todo,
  chat_dossier, settings_api, persona `_format_msg_ts` (memory `get_last_display` — вторая
  копия формата — теперь зовёт его), bot_instance, living_persona, web_llm `_parse_reset_ttl`.
  Персистентные строки-даты `%Y-%m-%d` читаются как раньше. Документация —
  `persona_template.yaml`, `.env.example`.

### №15. Напоминания: стабильный id
- Корень: единственным именем был номер в списке, пересчитываемый на каждый показ. Формат
  `r`+5hex, `_new_rid()` под локом, `parse_reminder_ref()` — единый разбор (id/номер),
  `cancel_by_ref()` — единая точка отмены (возвращает удалённую запись), `_ensure_ids()` —
  миграция при чтении и после restore. Telegram `/reminders`, `/cancel_reminder` и веб-API
  (`_reminders()` отдаёт id, DELETE через `cancel_by_ref`, календарный id `rem:{persona}:{id}`)
  переведены; ответ называет задачу и id. `cancel_reminder(index)` — легаси-обёртка (фронт
  шлёт `?index=`).

### №16. Сценарии и локи менеджеров
- `scenario_manager`: `_is_payment()` — единственная точка «платёжный шаг» (явные словоформы,
  `_MAP_SENSE_RE` для «карта сайта/метро»; «карточка», «мир» больше не платёж);
  `_validate_steps()` — единый валидатор шага (LLM-вывод, rule-based фолбэк, чтение
  `scenarios.json`): `type` с пустым полем не проходит. RLock на `_runs/_recording/_offered`
  (не держится на время `_advance`).
- `proactive_messaging`: locked-методы `_get_last_initiative_time/_mark_initiative_sent/
  _multi_turn_snapshot/_clear_multi_turn_wait`, `record_user_response` и счётчик досье под
  локом; метка «инициатива ушла» ставится до `self_memory.tick` (ответ в окне LLM не терялся).

### №17. Presence по (персона, чат) (`app/core/presence.py`)
- Корень: один глобальный флаг без персоны и чата — одна вкладка морозила фон всех персон и
  Telegram-чатов. `note(context, chat_id, active)/is_active(context, chat_id)/any_active(context)`,
  ключ (контекст бота, chat_id), TTL на ключ, `web_context(persona)` — единственное соответствие
  id персоны ↔ контекст. Гейт по чату в 7 местах; living `_tick_all` пропускает чат с открытой
  вкладкой внутри цикла, `any_active` — только для стимулов мира и сценариста.
- API: `PresenceRequest {active, persona: PersonaId, chat_id}`, heartbeat `focused=1` — по ключу.
  Фронт: `usePresenceReporting` в `Chat.tsx`, `api.setPresence(active, persona)`, `inboxStore`
  ставит `focused` только для персоны открытого чата; при смене — `active=false` по старому
  ключу. `web/dist` пересобран. `INITIATIVE.md` §1.4 обновлён.

### №18. `bot_instance.process_message` и классификатор «продолжаем?»
- `except Exception` к основному try: traceback в лог, `_pipeline_failure_reply()` на языке
  пользователя пишется в STM (если последняя реплика не assistant) и возвращается как ответ —
  история больше не остаётся с вопросом без ответа; веб отдаёт 200 с текстом вместо 500.
- `learning_manager.classify_continue_answer` → `computer_control.classify_confirmation` через
  подстановки `_LEARN_SYNONYMS` (второй копии клаузной логики нет): «не, давай не будем
  продолжать» = NO (было YES). `LearningManager.clear_chat(chat_id)` — публичная очистка
  сессий и всех setup чата под локом (`memory_wipe._wipe_learning` зовёт его);
  `clear_setup(all_users=)`, `_clear_setup_locked`.

### №19. Веб-API и мелочи
- `atomic_io.atomic_write_text` — одна реализация (сохраняет права файла), `security.py`
  реэкспортирует; `atomic_io.file_lock(path)` — межпроцессный лок (fcntl/msvcrt/no-op с warning).
- `clear_backup` привязан к chat_id (`clear_backups/{safe_segment(chat_id)}/{ts}.json`, легаси
  плоские бэкапы матчатся по полям внутри JSON); `security.safe_segment()` — общий helper.
- SSE-«печать» — async-генератор `_typed_chunks` в event loop (поток пула не держится);
  `persona_yaml`, `inbox` (`last_message.json`), `initiative` (`ignore_streak.json`) — через
  `asyncio.to_thread`.
- `web_search.is_safe_public_url()` (схема, резолв всех адресов, private/loopback/link-local/
  IPv6-mapped) + `get_with_safe_redirects()` — единственный обход редиректов
  (`fetch_page_text`, `computer_control._first_result_url`).
- `query_rewriter._tokenize_for_markers` (местоимение перед пунктуацией);
  `intent_router` берёт `OLLAMA_URL` из env с дефолтом `local_router.DEFAULT_OLLAMA_URL`;
  `get_local_router()` под локом; `file_sender` — код-блок без языка → `.txt`, tmp-файлы
  чистятся при сбое; `state_engine` на atomic_io; legacy `commit_session` без user_id.

### №20. web_llm: state-файл между процессами
- `_update_state(path, mutate)` — единственный RMW `web_llm_state.json`: `file_lock` снаружи
  (процессы) + `_state_file_lock` внутри (потоки), `load_json_safe`, `atomic_write_json`,
  запись только при изменении. `_mutate_state`, `clear_chat_urls`, `restore_chat_urls` — через
  него; локальный `_atomic_write_json` удалён. Тест на двух процессах.

### №21. `requirements.txt`, `.env.example`
- +10 пакетов (psutil, beautifulsoup4, ebooklib, pydantic, pillow, huggingface_hub, numpy,
  rank-bm25, requests, websocket-client); +~40 переменных с описанием по месту использования
  (API_*, провайдеры, Ollama, TIMEZONE, WEBCHAT_SITES…). Описания сверены с кодом.

---

### №22. Браузер: несколько экземпляров, Windows, поколения, единый стем
- Мосты AppleScript при >1 экземпляре: `_as_foreign_instance(url)` — чужой экземпляр = URL
  переднего окна не встречается среди вкладок пулов (`_bot_page_urls()` через `/json/list`,
  не через воркер — иначе deadlock на `_op_lock`; `_page_key` без query/#/www). Нет отладочного
  порта — не мешаем. Побочно: пул H + пул V — уже два «Google Chrome», мосты были выключены.
- Windows: `_win_profile_closed(udd)` берёт `<User Data>\lockfile` неблокирующим `msvcrt.locking`
  → Memory Saver включается при закрытом профиле; `_proc_terminate/_proc_kill` — платформенные
  (`taskkill /PID` → `/F /T`), `os.kill` в win32-ветках нет. Проверено только на моках.
- `_pages_by_gen/_hist_by_gen/_owner_by_gen` + `threading.local` поколения потока: страницы
  зависшего потока брошенного поколения новому не видны; `_pages/_url_hist` — свойства.
- Стем: «основа слова» была определена трижды (`web_search._stem`, JS `__vpcStem` усечением,
  копия в `_READ_SECTION_JS`). JS-стем собирается из той же таблицы `_WORD_ENDINGS` (читается
  теперь из stdlib-модуля `app/core/word_stem.py` — `WORD_ENDINGS`/`stem()`, `web_search._stem`
  — алиас; browser_actions по-прежнему поднимается без httpx для `chrome_debug.sh`), алгоритм
  один в один; парити в node на 49 словах. Таблица строже усечения → меньше ложных.
- Инцидент: патч стема вырезал 1327 строк (снапшот/скролл-шаблоны); восстановлено из
  file-history, сверено построчно (68 отличий = намеренные правки), тесты на подстроки шаблонов
  зелёные.

### №23. DNS rebinding (`web_search`)
- Корень: проверка и соединение резолвили имя по отдельности. `_resolve_checked(host)` — один
  `getaddrinfo`, проверка всех адресов; `_pinned_get()` — запрос на `scheme://IP:port/path`
  с `Host` и `extensions={"sni_hostname": host}` (httpx 0.28: и SNI, и проверка сертификата
  по имени). `is_safe_public_url` — обёртка (policy-хук для тестов сохранён), фейки без
  `headers/extensions` поддерживаются.

### №24. Ретенция персистентных словарей, перенос по id, presence без persona
- `app/core/retention.py`: `prune_stale(records, last_seen, max_age_days, keep_min)`,
  `CHAT_RETENTION_DAYS` (env, 180). При загрузке: proactive `known_chats.json` (три поля
  согласованно, маркер `_last_activity`), `state_engine._states` (`updated_at`),
  `relationship._chats` (`last_message_at`). Легаси без метки не удаляется. На живом процессе —
  `RetentionTimer` (`RETENTION_TICK_HOURS`, 6 ч) из периодических путей: `StateEngine.tick`,
  `RelationshipMemory.add_extracted`, цикл инициатив proactive; не из per-message путей.
- Перенос напоминания — один путь в `reminder_manager`: `begin_pending_postpone_choice(chat_id,
  ids, seconds, abs_time, …)` хранит сдвиг и id кандидатов в показанном порядке,
  `resolve_postpone_choice(chat_id, reply)` разбирает ответ (`parse_reminder_ref`, порядковые
  слова, слова задачи) и переносит через `postpone_by_id`; дубль логики в bot_instance удалён.
  Список показывает id.
- `PresenceRequest.persona` — Optional: старый фронт получает 200 без отметки, warning один раз.

### №25. Часовой пояс из веб-настроек
- `settings_api.get_timezone/set_timezone` (валидация `zoneinfo`, `.env` + `os.environ`, пустое
  = сброс), `GET/PUT /api/settings/timezone` (422 на невалидное), фронт: карточка в
  `ApiKeys.tsx` рядом с местоположением (селект `Intl.supportedValuesOf`), i18n, helpText,
  `web/dist` пересобран. **`web/` целиком в `.gitignore`** — правки фронта в git не попадают.

### №26. Иконки без семантики в DOM и ложный «офлайн» (кейс 22.09)
Симптом: «нажми крестик» → клик «More», «нажми закрыть» → «Log out», «колокольчик» → пункт
меню «Notifications»; в логе «Нет интернета — ответ локальной модели gemma4:e2b».
Корни и решения:
- **Крестик/колокольчик шторки — `<button class="jss151"><svg><path d=…>`**: ни текста, ни
  aria, ни testid — снапшот видел их безымянными, и текстовому LLM-резолву (`_llm_wide_pick`)
  их в списке не было → модель тыкала в чужой пункт. Теперь цель-иконка (`_ICON_WORD_ROOTS`:
  крестик, колокол…, лупа, шестерёнка, троеточие, аватар, бургер/полоски), которую скоринг с
  синонимами не нашёл, **минует текстовый ярус и идёт сразу в vision** (рамки вокруг
  безымянных кнопок — ярус для иконок и существует).
- **Классификатор формы SVG** в проходе 3б снапшота (`vpcIcoShape`, `browser_actions.py`):
  точки вдоль контуров (`getPointAtLength`), нормировка в bbox — крестик ✕ (две диагонали) →
  «закрыть», бургер ≡ (три полосы) → «бургер-меню», троеточие ⋮/⋯ → «ещё»; svg `<title>` —
  как подпись. Словарь классов расширен подсказками из data-testid и вложенных
  svg/img/i/use (MUI/FontAwesome вешают семантику на иконку, не на кнопку). Проверено живьём:
  крестик шторки → «закрыть» (скоринг 100, без LLM и vision), колокольчик/лупа — безымянные
  (vision), шевроны — без ложных подписей.
- **Латинские синонимы иконок** в `_GOAL_SYNONYMS` (close/dismiss, notif/bell, search,
  setting, avatar/profile/account): англоязычные интерфейсы с aria «Close»/«Notifications».
- **Вето по классу разрушительности** (`_DESTRUCTIVE_CLASS_RES`: close / delete / leave):
  намерение в цели снимает вето только со своего класса — «закрыть» больше не разрешает
  «Log out» и «Удалить»; × — и close, и delete («убери сыр» у товара в корзине).
- **Проба интернета** (`router.internet_available`): таймаут 1.5→3 с, «офлайн» — только после
  двух пустых серий подряд (гистерезис), кэш «офлайн» 10 с вместо 30, успешный ответ облака/
  веб-чата подтверждает онлайн без пробы (`note_internet_ok`). Слабый Wi-Fi с потерей SYN
  больше не переводит все решения режима управления на локальную модель на полминуты.
- Тесты: `test_computer_control` +5 (929), `test_web_llm` +4 (109).

### №27. Клик уходит под открытый попап (кейс 22.09, dodopizza «Десерт и напиток» → «Заменить» → «яблочный крамбл»)
Симптом: в комбо открыт попап «Заменить десерт» со списком замен; «нажми яблочный крамбл» →
«Яблочный крамбл 189 ₽ — готово», но нажата карточка каталога ПОД попапом, а не пункт списка.
По `audit.jsonl` (ts 1790088183): все кандидаты `vp`, детект бэкдропа (`sc`) не сработал, и
карточка каталога «Яблочный крамбл» (точный текст: 100 − 10 штраф позиции = 90) обошла пункт
попапа «Яблочный крамбл + 60 ₽» (70 + 10 `md` − 2.5 = 77.5); отрыв 12.5 < `LEADER_MARGIN` →
LLM получила голый список без контекста и выбрала «Яблочный крамбл 189 ₽».
Корни и решения:
- **Слой определялся только через бэкдроп** (`bdEl` в снапшоте: fixed/absolute ≥60% вьюпорта,
  заливка rgba с 0<α<0.98). Попап товара dodo эту цветовую эвристику не прошёл — `sc` у всех 1,
  и каталог под попапом конкурировал на равных. Теперь у каждого элемента во вьюпорте есть
  **независимый признак перекрытия `cov`** (`vpcCov`, `browser_actions.py`): `elementFromPoint`
  в центре элемента; перекрывающий не сам элемент/потомок/предок И принадлежит чужому слою —
  fixed-предок ≥30% вьюпорта или absolute ≥60%, не содержащий элемент (липкая шапка мала,
  бейдж/обёртка карточки содержит её — не считаются). Вне вьюпорта — `cov=0`; элемент из shadow root сравнивается через внешний хост (ретаргетинг `elementFromPoint`). В целевом снапшоте ключ сортировки остался мягким (любой перекрыватель), строгий `vpcCov` — только в поле `cov`.
- **Единый фильтр активного слоя `_active_layer`** (`computer_control.py`) вместо трёх
  inline-копий правила `sc`: сначала `sc`, затем `cov`; в `_choose_element`, `_llm_wide_pick`
  и `_visual_resolve`. Правило «не режем в ноль» сохранено для обоих сигналов (всё перекрыто /
  ложный детект — полный список).
- **Штраф −30 за `cov` в `_score_candidates`** — страховка для путей, скорящих без фильтра
  слоя (`goal_sole`, `_element_on_other_pages`); при равномерном перекрытии порядка не меняет.
- **Контекст слоя в промпте LLM** (`_layer_note`): к строке кандидата дописывается «— в
  открытом списке» (`dd`) / «— в открытом окне» (`md`) / «— под затемнением» (`cov`) — выбор
  между одноимёнными пунктом попапа и карточкой страницы перестал быть жребием.
- Тесты: `test_computer_control` +7 (936): аудит-кейс детерминированно → 86811762 без LLM,
  фолбэк «всё перекрыто», штраф, широкий LLM-резолв и vision-рамки без перекрытых, пометки в
  промпте; фикстура `snapshot_fixtures/modal_row_vs_covered_catalog.json` (9/9 в
  `eval_snapshot_scoring`); `test_browser_actions` — проверки `vpcCov` в шаблонах снапшотов.

---

### №28. Антибот-детект не работал нигде: невалидный селектор (кейс 22.09, deepseek «JS во вкладке упал»)
- Симптом: `[WebChat] deepseek: антибот-проверку выполнить не удалось (… JS во вкладке упал) —
  состояние страницы неизвестно, карантин не трогаю` на каждом обращении, хотя в окне браузера
  бота никакой капчи нет; сам ответ дальше приходит (проверка не блокирует отправку).
- Корень: в `_ANTIBOT_JS` и `_CHALLENGE_BOX_JS` селектор `iframe[src*=challenges.cloudflare]` —
  точка в незакавыченном значении атрибута не CSS-идентификатор, `querySelectorAll` кидает
  `SyntaxError` на ЛЮБОЙ странице (проверено в headless Chrome). Список селекторов перебирается
  по порядку, до cloudflare — recaptcha/hcaptcha, поэтому падало всегда. Следствия: strict-замер
  (`web_llm._challenge_check`) всегда «неизвестно» → карантин ни разу не снимался автоматически и
  реальная капча не детектилась; best-effort замер в `computer_control` (два вызова) молча отдавал
  «чисто»; автоклик по чекбоксу (`try_challenge_autoclick`) тоже никогда не находил виджет.
- Вторая причина долгой жизни бага: `_raw_eval` поднимал голое «JS во вкладке упал» без текста
  исключения. Теперь в ошибку идёт первая строка `exceptionDetails.exception.description`
  (`_cdp_exception_text`), обрезки причины в `detect_antibot`/`_challenge_check` расширены.
- Селектор закавычен в обоих скриптах (`iframe[src*="challenges.cloudflare"]`); оба скрипта
  прогнаны в headless Chrome на чистой странице — возвращают `''`, не бросают.
- Тесты (`test_browser_actions`, разд. 17): статический детектор незакавыченных значений
  атрибутов по обоим JS; `_raw_eval` отдаёт текст исключения без стека и не выбрасывает вкладку;
  `detect_antibot strict` показывает причину.

## Осталось сделать

Из списков аудита и из всплывшего в ходе работы — ничего. Осознанные ограничения (см. разделы):
- DNS rebinding закрыт пиннингом IP; ретенция на живом процессе — не чаще `RETENTION_TICK_HOURS`.
- Windows-ветки (`lockfile`, `taskkill`) и все браузерные правки проверены только на моках.
- Фронт (`web/`) не под git — изменения `ApiKeys.tsx`/`Settings.tsx`/`Chat.tsx`/`presence.ts`/
  `api.ts`/`inboxStore.ts`/`helpTexts.ts`/i18n и `dist` живут только в рабочем каталоге.

### Проверить вживую (ничего из этого не гонялось в реальном браузере)
- **Попап dodopizza (№27) живьём не воспроизведён**: на момент правки российские сайты
  (dodopizza.ru, ya.ru) с машины разработки не открывались (ERR_TIMED_OUT). Проверить: комбо
  «Десерт и напиток» → «Заменить» → «нажми яблочный крамбл» жмёт пункт списка замен, а не
  карточку каталога (в аудите путь `score`, кандидатов каталога нет); обычная страница с
  липкой шапкой (ютуб, dodo без попапа) — клики без изменений, `cov` у карточек 0.
- Снапшот/клик/скачивание с новой сквозной нумерацией (`scripts/smoke_browser_live.py`), в т.ч. iframe.
- Новый отпечаток страницы: нет ли новых «не уверен, что сработало» на ютубе/додо; отправка форм.
- Наведение (проверка `:hover` теперь реально работает — возможны честные «не уверен»);
  «наведи на очистить очередь» — наведение проходит, клик ветируется.
- Листание: smooth-scroll сайты, самозавершение, «стоп» в обоих режимах подтверждения;
  «пролистай комментарии вниз».
- Оверлеи: куки-баннер больше не снимается при чтении/листании/клавишах — убедиться, что
  клик/ввод по-прежнему его снимают, а закрытый цикл честно сообщает, если баннер мешает клавише.
- Неизвестное имя места («нажми пробел на додо» без алиаса) → отказ, а не действие в последней
  вкладке; при жалобах — добавить алиас в `sites` персоны.
- Пул H: reattach после обрыва, закрытие вкладок, капча/карантин, долгий аплоад картинки (>20 с);
  два процесса персон на общий `data/` — квота/chat_url не теряются.
- Зависшая вкладка (alert) → таймаут submit → пересоздание воркера (старое соединение закрыто);
  выход бота.
- AppleScript-мосты при одном и при двух запущенных Chrome (личный + бота: команды идут в
  браузер бота, личный не трогается); Safari-мосты; `chrome_debug.sh`.
- Windows: запущенный профиль → Memory Saver не правится, закрытый → правится; `taskkill`
  гасит пул (обе ветки проверены только на моках).
- Ретенция: на старом `data/` при первом запуске в логе «удалено N записей» только для
  чатов без активности дольше `CHAT_RETENTION_DAYS`; форум-топики живых чатов на месте.
- Настройка часового пояса в веб-интерфейсе: смена → «завтра в 9» считается по новому поясу
  без перезапуска.
- Авторизация: не-владелец в группе; фото с подписью от заблокированного; «не открывай» на pending.
- Веб-интерфейс: presence по персоне/чату (открытая вкладка одной персоны не глушит инициативу
  другой и Telegram), `PersonaIdPath` (422/404), сохранение ключей/моделей в `.env`,
  `/reminders` с id, `DELETE /reminders?index=`.
- Память: первый запуск на существующем `data/` — перенос коллекций l2→cosine (лог WARNING при
  неудаче), `forget`/`update_fact` по перефразировке; `restore_memory` подхватывает `api_*` дампы.
- `TIMEZONE` ≠ системному поясу: «завтра в 9», утреннее приветствие, окно инициатив 22–9,
  «resets at HH:MM» у веб-чатов.

---

## Волна 3 (2026-09-28): приватность и режим управления

Семь зон (privacy, confirm, execution, resolve, parsers, dispatch, ux) параллельными агентами,
затем интеграция. Состояние до волны — `refs/backup/pre-cc-fix`, снимки — `refs/backup/cc-fix-wip-*`,
журнал — `.git/cc-fix-wip/HANDOFF.md`. **Ничего не закоммичено.** Всё проверено на моках/node,
вживую — ничего.

Общий контракт полей действия (все зоны): `origin` (fast / intent_llm / marker / pending /
scenario / task), `pending_from` (исходный origin подтверждённого), `via_search`, `retried`,
`force_confirm`, `choose.resolve_ms`; всё это пишется в `audit.jsonl`.

### Приватность (`cc_privacy.py`, новый)
- Аудит: маска ввода в чувствительные поля и секретоподобных значений, URL без токенов/
  фрагментов, кандидаты ≤10, обрезка llm_response/detail/tiers; ротация 10 МБ × 3.
  `scripts/scrub_cc_audit.py` — чистка старых логов (по умолчанию dry-run).
- Приватные страницы (`private_hosts` + встроенные признаки входа/оплаты): без скриншотов,
  выбор элемента — только локальной моделью, текст страницы в диалог не идёт, в system prompt —
  только хост. Роутер перепроверяется после каждого шага nav-пути (sticky до конца маршрута).
- Снапшоты (`_SNAPSHOT_JS`/`_FRAME_SNAPSHOT_JS`/`_GOAL_SNAPSHOT_JS`) не читают `value` полей
  ввода — только подписи кнопок. `last_tab.json` — URL без сессии/фрагмента.
- Сценарии: секреты в трассе → `<SECRET>`, секретный ввод → слот «спросить каждый раз».
- `execute` под `_exec_lock` (RLock), `duration_ms` в аудите.

### Подтверждения
- `needs_confirm`: всегда «да» для `force_confirm`, `origin=marker`, `via_search`, оплаты/
  коммита/удаления/выхода (`risky_label`, overrides не отменяют); `tab_switch` — без вопроса.
- Маркеры LLM — всегда pending с вопросом-шаблоном; при недоверенном тексте (страница, веб,
  OCR, файл, reply) без глагола от человека — отбрасываются.
- Pending: TTL 60 с, «подтверждение истекло», владелец в группе, сброс любым новым действием.
- «хватит»/«stop» → NO; `stop_scroll_if_active(text)` гасит листание только стоп-фразой, а не
  любым «нет».
- Сценарий по частичному совпадению имени — только после «Запустить сценарий «X»?».
- `_COMMIT_RE`/`_PAYMENT_RE`/`_destructive_label` — один источник в computer_control.

### Исполнение
- Нет повторного клика после `ClickUncertain`; `_refind_confirmed` — только при «элемент
  потерян», по точной подписи, отказ при смене URL/подписи, `retried=True`.
- label → checkbox: второго клика нет, состояние меряется до клика (`window.__vpcChk`).
- Координатные клики/hover сверяют подпись точки (`sig` рамки, `expect=`).
- Корзина: опрос до 2.5 с; слайдер: синонимы, повтор при непринятом значении, громкость —
  напрямую в `<video>` (`media_volume_op '=0.NN'`).

### Резолв
- Бюджет каскада `resolve_budget_sec` (25 с): `fail_reason=budget`, `choose.resolve_ms`;
  «закрой X» — три попытки на одном снапшоте/дедлайне.
- Классификация: цели нет в снапшоте вовсе → `not_in_snapshot`, иначе «нет» модели → `llm_veto`;
  новый класс `not_a_click` (клавиша/листание/сайт, звукоподражание).
- `_act_from_meta`: `force_confirm`/`other_tab`/`retried` из меты доходят до действия —
  непроверенная подпись, согласие гибрида и зон, клик в скрытой вкладке теперь спрашивают «да».
- Порядковые («третье видео») не ветируют подпись; согласие зон требует совместимой подписи.

### Парсеры
- `resolve(name, web_search='auto')` — поиск только для брендоподобных латинских имён;
  результат поиска помечен `via_search`.
- Вкладки («зайди во вкладку с почтой», «новая вкладка», «закрой все вкладки» — отказ),
  стоп-лист UI-слов для поиска на сайте, «напиши» только с полем, общий `_strip_polite`,
  закрытие по белому списку объектов, английские формы команд.
- `split_compound_command` экспортирован и используется диспетчером; `parse_open_with_url(...,
  with_rest=True)` возвращает хвост команд (диспетчер его пока не читает — режет раньше).

### Диспетчер
- Всё — из `raw_user_text` (не из OCR/файла): лесенка, сценарии, запуск агента «задача: …».
- Составная команда — шагами, хвост едет в pending (`rest_steps`/`chain_site`).
- Гейт `looks_like_command`: `detect_language` знает только ru/en — прочие языки (é/ö/і/CJK,
  частые es/de/fr/it/pt слова) теперь пропускаются в LLM-ярус, а не молча блокируются.
- LLM-ярус: scroll/scroll_to/zoom/slider/cart/key×N/вкладки/page_view/read.

### UX
- «стоп» во время долгого действия: `cc_turn_enter/exit` до лока чата (Telegram, `/api/chat`,
  `/api/chat/stream`), флаг `request_stop` проверяется между шагами nav/цепочки/доскролла,
  отмена агента. Дубли одной команды в очереди — «Уже выполняю…».
- Typing в Telegram каждые 4 с; автовыход из режима после простоя `idle_exit_min` (30 мин,
  0 — никогда) с уведомлением; режим переживает перезапуск (`control_mode.json`).
- Напоминания/todo в режиме управления — честное «не сохранено, выйди из режима».
- Живое включение фичи в настройках пересобирает scenario/task_agent.
- `cc_texts.py`: ru/en фиксированных ответов, `describe/confirm_question/describe_done(lang=)`;
  превью ввода 200 символов; в группе — подсказка «ответь реплаем».

### Интеграция
- `pending_from` добавлен в аудит (контракт был, запись — нет).
- `scroll … on/in <site>`: предлог не уходит в имя контейнера («on youtube»), «way» — мера.
- Живой прогон агента получает только написанное человеком (`feed_text=raw_user_text`);
  фото/файл без подписи — «Жду ответ текстом…», как у сценариев.
- `TaskAgent._do_open` помечает действие `origin=task`.
- `test_hybrid_pick`: 3 ожидания `llm_veto` → `not_in_snapshot` (цель отсутствует в фикстуре
  zero-match — новая классификация зоны resolve).
- README: `idle_exit_min`, `resolve_budget_sec`.
- Тест стыков: `scripts/test_cc_integration.py`.

### Прогон (2026-09-28, после интеграции)

| Тест | Проверок | FAIL |
|---|---|---|
| test_computer_control | 952 | 0 |
| test_cc_privacy / confirm_policy / dispatcher | 123 / 88 / 102 | 0 |
| test_cc_execution / resolve / parsers / ux | 42 / 69 / 101 / 70 | 0 |
| test_cc_integration (новый) | 19 | 0 |
| test_hybrid_pick | 233 | 0 |
| test_task_agent / test_scenario | 56 / 59 | 0 |
| test_browser_actions / test_web_llm / test_api_security | 134 / 138 / 103 | 0 |
| остальные 40 наборов `scripts/test_*.py` | — | 0 |

`compileall app scripts addons` — чисто; ruff F821/F811 по затронутым файлам — чисто.
Не гонялись: `test_telegram_live`, `test_photo_reply` (нужен модуль `telegram`).
`test_computer_control` идёт ~6 мин: часть разделов (например, `_remember_tab` →
`find_tab_id`, `reveal_player_controls`) зовёт настоящий `osascript` (System Events/Chrome) —
так было и до волны, моки неполные.

### Не сделано
(Статус на 2026-09-28, вечер — см. «Волна 3, остатки».)
- ~~`scrub_cc_audit.py --apply` на `data/`~~ — закрыто: выполнено с согласия, 142 из 2792 записей.
- ~~Секрет в самой команде уходит в облачный intent-LLM и в историю; текст read с приватной
  страницы — в историю~~ — закрыто: LLM-ярус пропускается (`secret_rephrase`), в историю маска
  и заглушка «[прочитано N символов с приватной страницы …]».
- «отправь»/Enter при `confirm:false` не спрашивают (осознанно: явная команда человека).
- ~~`parse_slider_request`: «слайдер громкости на 70», «сделай звук 20%», «перемотай ползунок
  на 2 минуты»~~ — закрыто, все три разбираются.
- ~~`_vis_baseline` ставится только первым резолвом~~ — закрыто (остатки: база при открытии
  вкладки ботом, хранится в `last_tab.json`).
- ~~`task_agent._do_open` без отдельного «да»~~ — закрыто частично: чужой домен/query/fragment
  от модели — только после «да» (раунд ревью).
- «стоп» и один идущий вызов: ~~браузер~~ закрыт (остатки), ~~веб-чат без chat_id~~ закрыт,
  ~~английские тексты сценариев, агента, маркеров~~ закрыты. Остался одиночный вызов LLM.
- ~~Встроенный детект приватных хостов по подстроке~~ — закрыто в раунде ревью (по сегментам).
- ~~Состояние вкладок/листания общее на персону~~ — закрыто: `ChatBrowserState` по chat_id.

### Проверить вживую
- Приватность: логин/оплата (`*/login`, банк) — нет скриншотов в vision, выбор элемента
  локальный; в `audit.jsonl` ввод пароля замаскирован; `last_tab.json` без токенов.
- Подтверждения (connor, `confirm:false`): «открой душу» (поиск) → вопрос; маркер модели →
  вопрос; «оформить заказ»/«оплатить» → вопрос; «да» через 2 мин → «истекло»; «нет» не гасит
  листание, «хватит» гасит.
- Клик: чекбокс по подписи (не снимается вторым кликом); «не уверен» без повторного клика;
  корзина +/−/удалить на додо; «громкость 30» на ютубе.
- Резолв: «третье видео», «закрой шторку» на тяжёлой странице (≤25 с), «нажми сайт X» → подсказка.
- Парсеры/диспетчер: «зайди во вкладку с почтой», «открой dodo.ru и нажми пепперони»,
  «scroll down on youtube», команда на испанском/украинском доходит до LLM-яруса.
- UX: «стоп» посреди nav-маршрута и задачи агента (Telegram и веб), дубль команды, typing
  при долгом действии, автовыход после простоя и восстановление режима после рестарта,
  английские ответы, подсказка реплая в группе.

### Волна 3, раунд ревью

Ревью нашло 25 проблем (`.git/cc-fix-wip/review-wave3.txt`). Пять зон их исправили, затем
каждую проблему воспроизвёл заново на текущем коде (моки, без Chrome и сети).

Исправлено, сценарий ревью больше не воспроизводится:
- Агент задач (`task_agent.py`):
  - Не нажимает без «да» «Отправить», Send, Submit, «Подтвердить», «Опубликовать», Place your order
    и «Оформить». Тот же вопрос перед type+submit и перед Enter/Tab, кроме Enter сразу после ввода
    в поисковое поле. Кнопка оплаты с подписью только в aria передаёт шаг человеку.
  - С приватной страницы промпт уходит только локальной модели; если её нет, пауза и передача
    человеку. URL в промпте и в истории чистится `scrub_url`.
  - Адрес, выбранный моделью (чужой домен, query или fragment), открывается только после «да».
  - Подтверждение живёт `PENDING_TTL_SEC` и принимается только от автора задачи.
  - В лог шага вводимый текст попадает только длиной.
- Клик и наведение на иконку: aria/title копируются в действие (`_with_labels`), поэтому
  `risky_label` видит оплату и удаление.
- Аудит:
  - `resolve_type` без полей пишет `redact_type_body`.
  - Для read в detail пишется `read:N chars`.
  - `redact_audit_record` чистит и старые записи.
- Логи: fast-path, «LLM-разбор» и `_audit_resolve` без секретов.
- Словарь `risky_label`: «Оформить», «Купить в 1 клик», «Заказать», Place your order, Order now,
  переводы, пополнение и вывод денег, Donate, отмена подписки, Deactivate.
- `_llm_recover` в сценариях не жмёт оплату, коммит, отправку и удаление.
- `page_view_text` и `page_view_full_text` выводят URL через `scrub_url`.
- `scrub_url`: PHPSESSID/sid, hmac, verification, magic, reset, `p` с нечисловым значением,
  токены в пути.
- Приватные страницы: почта, мессенджеры, банки, /cart, /basket, /lk, /settings/tokens.
  Ложные срабатывания на author, espresso, lasso и upay убраны.
- `scrub_cc_audit --apply`: берёт flock вместе с `audit_append`, читает файл фиксированного размера,
  при изменении файла отказывается (`FileChanged`, код 2).
- Диспетчер:
  - Тело ввода не режется: «напиши в чат ок, открой ссылку» — один шаг.
  - Короткие команды без глагола («погромче», «на главную») доходят до LLM-яруса.
  - Повтор шаговых команд («громче» ×3) не считается дублем.
  - `normalize_command` снимает вежливые слова только с краёв фразы.
  - «click Accept and close» — один клик; «Barnes and Noble» и «Rock and Roll» — одна цель.
  - «нажми на красное платье» больше не принимает прилагательное за сайт.
  - Режим управления без `computer_control` гаснет и чистится на диске.
- Резолв:
  - За дедлайном бюджета сомнительный выбор (ничья одноимённых кнопок, слабый скор) ставит
    `force_confirm`.
  - Под vision держится резерв: 9 с или 40 % бюджета. Для цели без текстовых совпадений
    («лайк») гибрид идёт до доскролла.

Исправлено частично (сценарий ревью закрыт, соседние случаи остались):
- №11 (иконка): `resolve_type` не копирует aria/title. Ввод в поле, подписанное только в aria
  («Номер карты»), при `confirm:false` идёт без вопроса.
- №23 (логи):
  - `process_markers` («Маркеры отброшены», «Маркер отклонён») пишет маркер как есть.
  - «Выполнено: …» идёт через `_describe_safe`, но пароль в поле без подписи (эвристика
    `redact_typed`) виден.
- №26/29 (словарь): «Купить сейчас», «Закрыть аккаунт» и голое Transfer дают None. Их нажмут
  агент без «да» и `_llm_recover`.
- №38 (`scrub_url`): `?s=` и `?lk=` не маскируются.
- №44 (чистка): битая строка чистится только `redact_inline`, пароль без метки остаётся.
- №54 (дубли): повтор перемотки («перемотай на 10 секунд вперёд») в течение 1,5 с отбрасывается.

Проблемы, которые принесли сами фиксы:
- Взаимодействие фиксов, исправлено здесь же. /cart, /basket и /lk стали приватными, а на
  приватной странице vision-проверка сомнительного выбора не работает. Поэтому ничья «Изменить»
  в корзине кликалась по первому кандидату без вопроса. Теперь на приватной странице
  сомнительный выбор ставит `force_confirm` (`meta.private_doubt`). Добавлена проверка в
  `test_cc_resolve`.
- Любая фраза из 1–3 слов, кроме явной болтовни, в режиме управления стоит одного
  intent-LLM-вызова (например, «люблю тебя»).
- Агент задач:
  - Нет «да» перед Space по кнопке.
  - Поисковый запрос агента уходит в DDG без проверки на ПДн.
  - Текст страницы после read с неприватной страницы уходит в облачный промпт.
  - Ожидание `continue` после передачи человеку снимает любой участник группы.
- Во время `scrub --apply` каждая запись аудита ждёт flock до 10 с под общим тред-локом:
  бот может подвиснуть на время прохода.

Прогон (после раунда):

| Тест | Проверок | FAIL |
|---|---|---|
| test_computer_control | 952 | 0 |
| test_cc_privacy / confirm_policy / dispatcher | 203 / 106 / 168 | 0 |
| test_cc_resolve / parsers / execution / ux | 79 / 128 / 42 / 70 | 0 |
| test_cc_integration / history_privacy (новый) | 19 / 31 | 0 |
| test_hybrid_pick / test_task_agent / test_scenario | 233 / 81 / 59 | 0 |
| test_browser_actions / test_web_llm / test_api_security | 134 / 138 / 103 | 0 |
| остальные 41 набор `scripts/test_*.py` и 2 теста аддона arrodes | — | 0 |

`compileall app scripts addons` проходит чисто. Не запускались `test_telegram_live` и
`test_photo_reply`: для них нужен модуль `telegram`.

### Волна 3, финальный раунд

Две зоны закрыли 10 оставшихся дыр. Каждую я перепроверил своим сценарием: моки, без Chrome и
сети.

Закрыто:
- `resolve_type` копирует aria/title (`_with_labels`). Поле «Номер карты» (aria), «CVC» (title) и
  Card number при `confirm:false` ставятся на вопрос, в том числе при вводе через «в поле …».
- Словарь `risky_label`:
  - «Купить сейчас» — commit.
  - «Закрыть аккаунт / учётную запись / профиль / счёт» и Close (my) account — destructive.
  - Transfer, «Перевод 500 ₽» и «Перевод на карту» — payment. Голое «Перевод» считается оплатой
    только на банковском хосте.
  - «Закрыть», «Закрыть окно», «Перевод страницы» и Transfer files по-прежнему дают None.
- `scrub_url`:
  - `s` и `lk` маскируются, если значение не число (`?s=20` остаётся).
  - Любой параметр с `sid` удаляется (gdsid/ssid/usid); sidebar и side остаются.
  - Результат идемпотентен.
- Логи: `process_markers` пишет маркер через `scrub_url`/`redact_inline`, а «Выполнено: …» и
  «Ожидаю подтверждения» показывают ввод маской в любом поле. Проверено: пароль, токены и email
  в лог не попадают.
- Дубли: «перемотай на N сек вперёд/назад», «перемотай вперёд», skip/rewind — это шаг, и повтор
  исполняется. «Перемотай видео про котиков» шагом не считается.
- Агент задач:
  - Space и Enter/Tab ждут «да».
  - Поисковый запрос с email, телефоном или картой уходит только после «да» автора. Чужое «да»
    и протухшее подтверждение не срабатывают. В промпт и лог запрос попадает маской.
  - `continue`: чужое «да» отклоняется. Протухшее «да» переспрашивается и прогон не продолжает.
- История:
  - Ответ на вопрос агента о пароле, коде или логине (на русском и английском) записывается маской.
  - Пароль в самой цели («задача: …пароль X») и ввод агента в чувствительное поле тоже маскируются.
  - На маркерном пути маска ставится до записи реплики.
  - `_rewrite_image_stm` пишет через `stm_add_message`, то есть через маски хода.
- Регрессия прежних critical/high не найдена:
  - Агент не жмёт без «да» рискованные кнопки (aria «Удалить аккаунт», title «Оплатить» —
    передача человеку).
  - URL, выбранный моделью, открывается только после «да».
  - `PrivateRouter` на банке и /cart в облако не ходит.
  - `resolve_type` no_fields пишет в аудит маску.
  - `split_compound` не режет тело ввода.

Закрыто после проверки (оркестратор):
- Агент задач, группа: владелец прогона — тот, кто поставил задачу (`turn_user` задаётся при
  старте и больше не переходит). Чужая реплика не отвечает на `ask`, не подтверждает, не
  продолжает и не отменяет задачу через `feed()`, а получает «Этой задачей управляет тот, кто её
  поставил». Ранний «стоп» до лока хода (`cc_turn_enter`) по-прежнему доступен любому
  допущенному участнику — остановка в сторону безопасности. test_task_agent: 99 / 0.
- Повторная чистка аудита (`scrub_cc_audit --apply`, бот выключен): 2 записи с `gdsid`; итого за
  волну очищено 142 записи из 2792, повторный dry-run пуст.

Осталось (все семь пунктов закрыты в «Волна 3, остатки»):
- ~~История: пароль из ответа, введённый агентом в следующем ходе, открыт; маски живут один
  ход~~ — закрыто (`KnownSecrets`).
- ~~Маркерный путь: логин без слова «логин/пароль» в реплике открыт~~ — закрыто, кроме пакета
  LTM-извлечения этого же хода.
- ~~Поисковый запрос агента: `Kotik2019!` уходит в DDG без вопроса~~ — закрыто.
- ~~Лог: `task_agent._do_open` пишет `target` как есть~~ — закрыто.
- ~~Словарь: «Закрыть вклад» даёт None~~ — закрыто (payment).
- ~~Пережим: email/телефоны из обычных реплик под маской~~ — закрыто (`contacts=False`).
- ~~Текст read с неприватной страницы уходит в облачный промпт~~ — закрыто (`redact_inline`).

Прогон (финальный; интерпретатор 3.11, `grep -a "[FAIL]"`):

| Тест | Проверок | FAIL |
|---|---|---|
| test_computer_control | 952 | 0 |
| test_cc_privacy / confirm_policy / dispatcher | 237 / 106 / 179 | 0 |
| test_cc_resolve / parsers / execution / ux | 79 / 128 / 42 / 70 | 0 |
| test_cc_integration / history_privacy | 19 / 42 | 0 |
| test_hybrid_pick / test_task_agent / test_scenario | 233 / 96 / 59 | 0 |
| test_browser_actions / test_web_llm / test_api_security | 134 / 138 / 103 | 0 |
| test_telegram_live / test_photo_reply (ptb из scratch в PYTHONPATH) | 41 / 8 | 0 |
| остальные 46 наборов `scripts/test_*.py` | — | 0 |
| аддон arrodes: 6 наборов (addon 44, fact_markers 19, volume 35, остальные без меток) | — | 0 |

`compileall app scripts addons` проходит чисто. Чинить интеграцию не понадобилось.

### Волна 3, остатки (2026-09-28)

Три зоны (privacy, ux, perchat) закрыли «Осталось» финального раунда и три пункта «Не сделано».
Каждый пункт сначала воспроизведён скретч-скриптом, после фикса тот же скрипт перепрогнан (моки,
без Chrome и сети). Снимок до зон: `refs/backup/cc-fix-wip-20260928-2035`. Не закоммичено.

Закрыто — приватность:
- `cc_privacy.KnownSecrets`: известные секреты чата живут 30 мин (`KNOWN_SECRET_TTL`), каждая
  запись истории проходит через них; сброс по концу прогона агента. Ответы да/нет и «отмена» не
  копятся. «Ввёл «Kotik2019!»» ходом позже в истории — `***(10)`, пользователю — как было.
- Маркерный путь: `_cc_hist_after_markers` после `process_markers` маскирует значение ввода в
  логин/email/пароль и переписывает реплики только этого хода (`_cc_hist_rewrite_tail`).
- Поиск агента: `TaskAgent._secrets` (хук `known_secrets` + ответы на секретные вопросы) →
  `mask_values`, затем `redact_inline`; при изменении — «да/нет» с маской.
- `_do_open` и лог сбоя DDG — через `redact_inline`/`scrub_url`.
- `_PAYMENT_RE`: «Закрыть/досрочно закрыть/расторгнуть вклад/депозит», Close deposit → payment;
  «Закрыть вкладку», «Открыть вклад», «Вклады» — None.
- Пережим снят: на маркерном пути email/телефон (`looks_contact`) маскируются только при вводе в
  поле или если уже известны; пароли/карты/токены — как раньше. Агентский путь без изменений.
- Текст read с неприватной страницы в облачный промпт — после `redact_inline`, затем обрезка.

Закрыто — «стоп» и английский (ux):
- «стоп» прерывает один долгий вызов браузера: `stop_scope`/`sleep_or_stop` в `browser_actions`
  (`wait_dom_idle`, `_gateway_status`, `open_new_tab`), опрос вкладки в `_snapshot_for`,
  доскролл `_scroll_hunt`. Ответ — «Остановлено по твоей просьбе.», вкладка не забывается;
  сбой шага после «стоп» — `error_class=stopped`.
- Веб-чат без chat_id: ключ хода — user_id, «стоп» до лока хода виден исполнению.
- Английские тексты в `cc_texts`: маркеры, отчёт о странице, доскролл, сценарии, агент задач,
  цепочка шагов; `cc_texts.phrase` для английского хода не берёт русский flavor-банк.

Закрыто — состояние по чатам (perchat):
- `ChatBrowserState` на chat_id (или user_id): хост, вкладка, URL, кэш вкладок, листание,
  базовая видимая вкладка. Старые атрибуты — дескрипторы `_ChatAttr`; чат хода —
  contextvar `chat_scope` (весь `process_message`) → `set_turn` → последний активный.
- Листание одно на чат: «стоп» в B не гасит листание A.
- `_vis_baseline` ставится при открытии вкладки ботом (всем чатам), переживает рестарт.
- `last_tab.json` по чатам (≤50, атомарная запись, URL через `scrub_url`); старый формат —
  шаблон для всех чатов.
- Вкладка, закрытая ботом, снимается со слежения во всех чатах.

Осталось:
- Пакет LTM-извлечения этого же хода собран до переписывания хвоста — маркерная маска в него не
  попадает (нужна правка `memory.py`).
- Два чата листают одну вкладку: «стоп» одного завершает сессию другого с причиной `lost`.
- Вызов вне хода без чата берёт последний активный чат (прод-входы все в scope).
- `task_agent`/`scenario_manager`: `_runs` с ключом `'None'` у веб-чата без chat_id.
- «стоп» не прерывает одиночный вызов LLM.
- «отправь»/Enter при `confirm:false` — без вопроса (осознанно).
- `test_cc_confirm_policy.py:468` — повторный импорт `SimpleNamespace` (pyflakes, безвредно).

Прогон (верификация; интерпретатор 3.11, `grep -a "[FAIL]"`):

| Тест | Проверок | FAIL |
|---|---|---|
| test_computer_control | 952 | 0 |
| test_cc_privacy / confirm_policy / dispatcher | 256 / 106 / 179 | 0 |
| test_cc_resolve / parsers / execution / ux | 79 / 128 / 42 / 113 | 0 |
| test_cc_integration / history_privacy / perchat (новый) | 19 / 58 / 57 | 0 |
| test_hybrid_pick / test_task_agent / test_scenario | 233 / 108 / 59 | 0 |
| test_browser_actions / test_web_llm / test_api_security | 134 / 138 / 103 | 0 |
| test_telegram_live / test_photo_reply (ptb из scratch в PYTHONPATH) | 41 / 8 | 0 |
| остальные 41 набор `scripts/test_*.py` | — | 0 |
| аддон arrodes: 6 наборов (addon 44, fact_markers 19, volume 35, остальные без меток) | — | 0 |

`compileall app scripts addons` чисто; pyflakes (undefined/redefined) по затронутым файлам —
только пункт выше. FAIL `wait_dom_idle: фолбэк-слип` из отчёта privacy к моменту прогона
не воспроизводится (фолбэк без «стоп» снова обычный слип). Чинить интеграцию не понадобилось.

### Волна 3, узкие места (2026-09-29)

Раньше каждый путь исполнения чинили отдельно, и ревью каждый раз находило незалатанный (nav,
сценарий, flavor, логи). В этом раунде правила стоят в единых узких местах, через которые
проходят все пути. На вызовах UX остался прежним (задаётся вопрос), но узкое место откажет, если
вызов забыл спросить. 18 пунктов `review-leftovers.txt` проверены повторным прогоном скриптов
ревьюера (`scratchpad/rv/h*.py`, `probe*.py`; моки, без Chrome и сети). Не закоммичено.

Инварианты и где они живут:
- **Подтверждение** (`computer_control.py`). Токен `_ConfirmToken`: строку, dict или `True` из
  JSON, маркера или LLM за токен не принимаем. Выдаёт его только `grant_confirmation`, из
  четырёх мест: «да» владельца на pending, «да» автора задачи агента, «да» на шаг сценария,
  ответ-слот сценария. `_confirm_gate` стоит в `_execute_locked` перед `_dispatch`, а
  `_execute_locked` — единственный вызывающий `_dispatch`. Токен нужен, если выполняется
  risky_label (оплата, отправка, удаление), force_confirm, label_unverified, клик по точке,
  ввод в чувствительное поле, маркер или via_search. Без токена действие не выполняется, в аудит
  пишется `needs_confirm`. В nav `_nav_gate` сидит в `_step_click` — единственном клике маршрута;
  проверяется подпись найденного элемента. `_nav_step_recover` risky не нажимает. Отказ
  узкого места вызывающий превращает в вопрос: `_cc_gate_ask` (продолжение на той же вкладке),
  `_gate_pause` у сценария, `_ask_confirm` или handoff оплаты у агента.
- **Приватность облака.** `_privacy_router(router, *where)` принимает полный URL и хост. Если
  передан только хост, проверяет ещё и отслеживаемый URL чата. `cc_privacy.page_candidates`
  собирает адреса действия, записи или multi. Тем же набором пользуются `_audit`,
  `flavor_text._from_live` (на приватной странице возвращает None, дальше шаблон) и
  `scenario_manager._trace` (приватная трасса уходит только в PrivateRouter или rule-based).
- **Известные секреты.** Реестр `cc_privacy.known_secret_values()` общий для всех
  `KnownSecrets` процесса. Маску по нему накладывают `_audit` (ввод), flavor, трасса
  сценариев, `task_agent` (`_remember`, `_prompt` qa, `_do_element`) и фильтр логов.
- **Логи процесса.** `app/core/log_privacy.py`: фабрика LogRecord и `LogPrivacyFilter`,
  `install()` в `app/main.py` и `app/api/server.py`. Входные строки (START, [MSG], «Обработка
  от») пишутся через `input_for_log`: в режиме управления только длина.
- **«Стоп».** `_stop_key` — единственное место, где выбирается ключ. Порядок: явный чат (`"None"`
  и пустой ключом не считаются), затем `_exec_chat`, но только своего потока (сеттер запоминает
  поток), затем ход потока. Сценарий проверяет `_stop_hit()` до и после каждого шага, а
  `cc_turn_enter` вызывает `scenario_manager.request_stop`.
- **Язык отказов резолвера.** Все такие тексты идут через `_tx` в `cc_texts` на языке хода.
  Сторож — AST-скан в `test_cc_state`.

Статус 18 пунктов (повторный прогон):

| # | Пункт | Статус | Доказательство |
|---|---|---|---|
| 1 | nav жмёт risky-шаги (critical) | fixed | h1: вопрос, dispatched=[]; h3: кликнута только «Корзина», «не выполняю без подтверждения (это оплата)»; h2: recover=False ×4 |
| 2 | сценарий без risky/force_confirm | fixed | h4: пауза «Шаг сценария — нажать «Оформить заказ»… Делаю? (да/нет)», исполнены только url и «Корзина» |
| 3 | URL и алиасы от LLM в сценарии | fixed | h13: сохранён `https://dodopizza.ru/` (адреса нет в трассе, поэтому rule-based); h13b: «давай», «да», «открой ютуб» отброшены, «закажи пиццу» оставлен. Список шагов при сохранении не показывается |
| 4 | пароль в task_memory и облачном qa | fixed | h8: пароля нет ни в промпте текущего прогона, ни на диске, ни в промпте следующей задачи |
| 5 | секретный слот сценария в STM | fixed | h10: STM `user \| ***(10)`, `Typed "***(10)"…` |
| 6 | агент вводит известный секрет без вопроса | fixed | h9: для обоих полей outcome=pause, awaiting, dispatched=[] |
| 7 | query скрыт в вопросе, маркер с query | fixed | h12: маркер отброшен, pending=None; вопрос показывает `?d=***(12)`. Fragment показывается пустым (scrub_url его режет) |
| 8 | приватность по хосту, а не по URL | fixed | h7: облачный роутер 0 вызовов (vk.com/im) |
| 9 | flavor на приватных страницах | fixed | h11: провайдеру не ушло ничего (×3); h11b: ввод `***(10)` |
| 10 | `_llm_generalize` отдаёт приватную трассу облаку | fixed | h5: облачный роутер не вызван, rule-based |
| 11 | ввод открытым текстом в audit.jsonl | fixed | h5: `***(21)`/`***(35)`/`***(10)`; h9b: известный секрет на shop.ru виден как `***(10)`, «пепперони» остаётся открытым |
| 12 | сырой ввод в логах процесса | fixed | h6: в логе нет строки с секретом; `test_cc_privacy_choke` |
| 13 | «стоп» не останавливает сценарий | fixed | probe_sc: исполнены [url, Меню], дальше «Остановлено по твоей просьбе — сценарий «пицца» прерван.» |
| 14 | `_forget_tab` per-chat | fixed | probe_close2: у B id=None, resolve_click 0.0 с (было 10.6 с) |
| 15 | утечка `stop_requested` через `_exec_chat` | fixed | probe_leak / probe_leak_b: веб-чат без «стоп» докрутил, steps=10. probe1 P1 ставит `_exec_chat` в своём же потоке и новую семантику не проверяет |
| 16 | ключ `"None"` у веб-чата | fixed | probe1 P2: `chat_id='None'` → 0.0 с «Stopped, as you asked.» |
| 17 | английский ход: русские отказы резолвера | fixed | probe_disp (en): «Couldn't find the field "search field"…»; probe_sc_en: отказ на английском с одной точкой; русских литералов в `_cc_ladder` нет |
| 18 | «stop scrolling» не распознаётся | fixed | `parse_scroll_request`, `_SCROLL_STOP_RE` и `_CC_STOP_RE` принимают stop scrolling / stop the scroll(ing) / enough scrolling |

Замечено при проверке, не входит в 18:
- `parse_open_with_url` оставляет у первого шага ведущее «- » («- Корзина»). Шаг всё равно
  резолвится, но в вопросе видно «- Корзина».
- Ответ «Kotik2019!» на слот сценария переключает язык хода на английский: итог «Typed … The
  "вход в магазин" scenario is finished.» приходит по-английски в русском диалоге.
- На приватной странице подпись нажатого элемента (номер счёта, сумма) по-прежнему лежит в
  audit.jsonl открытым текстом. В облако она не уходит: трасса приватная.
- Fragment адреса в вопросе подтверждения не виден: есть только пометка «есть параметры».

Прогон (верификация; интерпретатор 3.11, `grep -a "[FAIL]"`). `compileall app scripts addons`
чистый. pyflakes по затронутым файлам даёт только известный `test_cc_confirm_policy.py:468`.
Чинить интеграцию не понадобилось.

| Тест | Проверок | FAIL |
|---|---|---|
| test_computer_control | 953 | 0 |
| test_cc_gate (новый) / privacy_choke (новый) / state | 89 / 75 / 81 | 0 |
| test_cc_privacy / confirm_policy / dispatcher | 256 / 106 / 179 | 0 |
| test_cc_resolve / parsers / execution / ux | 79 / 128 / 42 / 113 | 0 |
| test_cc_integration / history_privacy / perchat | 19 / 59 / 57 | 0 |
| test_hybrid_pick / task_agent / scenario / flavor_text | 233 / 108 / 59 / 35 | 0 |
| test_browser_actions / web_llm / api_security / click_delivery | 134 / 138 / 103 / 22 | 0 |
| test_telegram_live / photo_reply (ptb из scratch) | 41 / 8 | 0 |
| остальные 39 наборов `scripts/test_*.py` | — | 0 |
| аддон arrodes: 6 наборов (addon 44, fact_markers 19, volume 35, остальные без меток, rc 0) | — | 0 |

### Волна 3, ред-тим (2026-09-29)

Перепроверка 15 находок `.git/cc-fix-wip/redteam-1.txt` на текущем коде: все repro-скрипты
(scratchpad `rtq/`, `rt/`) прогнаны заново, интерпретатор 3.11.

| # | Находка | Статус | Доказательство / что осталось |
|---|---|---|---|
| 1 | [confirm][crit] risky_label не узнаёт покупку, финальное удаление, выход и блокировку | частично | q5: все 11 подписей дают dispatched=False (payment/destructive/commit). q7: агент «Buy for $4.99» не жмёт. q8: после strip шаги [open, handoff]. q9: connor переспрашивает. q10: на «Да, удалить» бот спрашивает снова. q11: пойманы все, кроме голого «Купить» (так задумано). **Остаток того же класса:** ZWSP (U+200B) вместо пробела склеивает слова, и `Оформить​заказ`, `Buy​now`, `Leave​server` дают risky_label=None (_risk_text удаляет Cf-символ, а пробел не ставит). Без токена проходят также «Get Premium» без цены, «Заказ подтверждаю» и голое «Перевести». |
| 2 | [confirm][high] шаг сценария send / type+submit без паузы | исправлено | q2: «Шаг сценария — отправить сообщение… Делаю?», dispatched без send. Явная команда «отправляй» (origin=fast, confirm:false) по-прежнему исполняется сразу, как и задумано. |
| 3 | [confirm][med] LLM-ярус open открывает адрес, выбранный моделью, без «да» | исправлено | q3 и q12: pending via_search=True, origin=intent_llm, dispatched=[] при confirm:false и при confirm:true. |
| 4 | [privacy][high] после неудачного резолва ввода пароль лежит в STM и уходит во flavor | исправлено | r10b: в STM `введи ***(10) в поле пароль`, known=True. r10_type_unparsed: во flavor уходит `***(10)`, секрет зарегистрирован. |
| 5 | [privacy][high] _llm_recover сценария подставляет значение слота | исправлено | r6: в облако уходит `type "{секрет1}"`, секрета в промпте нет. |
| 6 | [privacy][high] агент переносит приватную страницу в облако и в task_memory | частично | r3: в облачном промпте нет ни email, ни телефона, ни «ВИЧ», в history стоит заглушка. r3b: task_memory хранит «details not kept», следующий промпт чистый. **Не подключено:** `TaskAgent.on_private_text` бот не выставляет (в `app/` нет присваивания), поэтому ответ хода с приватной страницы (при локальной модели) ложится в STM открытым. |
| 7 | [privacy][high] read_page_section с явно названным сайтом проверяет приватность только по хосту | исправлено | r1: для «на vk.com», «на вк» и shop.ru/checkout результат None. r1c: private text in cloud prompt=False. |
| 8 | [privacy][med] ответ на вопрос агента без ключевого слова — секрет в облаке, STM и task_memory | частично | r7 (3 варианта вопроса): в облако и в task_memory.json не уходит. **В STM лежит открытым (True):** `TaskAgent.answer_is_secret` есть, но бот его не вызывает. `_cc_hist_note_user_text` (bot_instance) по-прежнему проверяет только `is_sensitive_label(question)`, и секрет не попадает в KnownSecrets. |
| 9 | [privacy][med] команда с секретом без ключевого слова уходит в облако, STM и лог | исправлено | r4_kw (4 фразы): command_has_secret=True, в облаке, логе и STM 0. |
| 10 | [privacy][med] is_private_page без маршрута во фрагменте/query и без /account, /profile | исправлено | r11: /#/checkout, ?route=checkout/checkout и /my-account/ дают private=True и 0 облачных вызовов. Также ловятся api-keys, apikeys, gocheckout, #!/signin и ?r=login. Ложное срабатывание в безопасную сторону: `/blog/how-to-checkout-faster` тоже приватная (подстрока checkout). |
| 11 | [privacy][med] flavor: download или действие только с хостом обходят приватность | исправлено | r2: download, click без value и cart на приватной вкладке дают flavor=None и 0 вызовов провайдера. |
| 12 | [privacy][med] scrub_url: вложенные URL и JWT/токены в «безобидных» параметрах | исправлено | r9: токена нет в system prompt, last_tab.json, audit и логе. u1: continue, next, redirect_uri, data=JWT, k, c, h, t.me/+ и email в пути замаскированы. Остаток вне находки: публичные ссылки-«ключи» `yadi.sk/d/…` и `disk.yandex.ru/i/…` не маскируются. |
| 13 | [privacy][low] name_check.title в аудите мимо redact_audit_record | исправлено | r13: `title: '<70 chars>'`. |
| 14 | [privacy][low] лог-фильтр: «пароль от почты X», «пароль — X», пасс, cvv, ghp_ | исправлено | r5: все строки замаскированы (ghp_, sk-proj-, Bearer). r12: 0 строк с секретом в /api/logs. Карта `4276…5678` в r5 не маскируется только потому, что не проходит проверку Луна; `4111…` маскируется. |
| 15 | [privacy][low] подписи приватной страницы в логе | частично | r8: в /api/logs нет строк с подписью («Клик», «Выбор», «Наведение», «Скачивание» идут через `_label_for_log`). **Осталось:** `scenario_manager.py` `[Scenarios] LLM-восстановление: жму «{act['element']}»` и строка «рискованный» пишут подпись как есть. На приватной странице с локальной моделью _llm_recover выполняется, и подпись попадает в лог. |

Итого: 11 исправлено, 4 частично (1, 6, 8, 15), не исправленных нет.

Что осталось:
1. `_risk_text`: невидимые разделители (U+200B/200C/200D/2060/FEFF) заменять пробелом там, где они разделяют две буквы, а не удалять.
2. bot_instance: выставить `task_agent.on_private_text = self._cc_hist_note_private`. В `_cc_hist_note_user_text` для ответа на вопрос агента использовать `task_agent.answer_is_secret(chat_id, text)` вместо `is_sensitive_label(question)`.
3. scenario_manager `_llm_recover`: подписи в лог-строках пропускать через `cc._label_for_log(label, url, host)`.

Наборы после перепроверки: test_cc_gate 122/0, test_cc_privacy_choke 99/0, test_cc_privacy 300/0,
test_task_agent 130/0, test_scenario 59/0 (все rc=0).

### Волна 3, доводка ред-тима и живой смоук (2026-09-29, оркестратор)
- Закрыты 4 частичных пункта ред-тима: `_risk_text` — zero-width (U+200B/200C/200D/2060/FEFF) → пробел
  (`Оформить​заказ` → commit), мягкий перенос по-прежнему убирается; бот подключает
  `task_agent.on_private_text` (отчёт агента с приватной страницы — заглушкой в историю) и
  `answer_is_secret` (ответ-секрет на вопрос агента — маской в STM, r7: in STM False);
  `_llm_recover` сценария пишет подписи и цель через `_label_for_log`.
- test_cc_ux: мок `_prompt` принимает `**kw` (сигнатура `local=`).
- Полный параллельный прогон: 69 наборов, 0 FAIL (трейсбек test_telegram_live — ожидаемый «LLM down»).
- Аудит дочищен новыми правилами (вложенные URL и пр.): +24 записи; повторный dry-run пуст;
  `task_memory.json` без секретов.
- Живой смоук (`scripts/smoke_browser_live.py`, профиль бота, бот выключен): 17/20.
  Вся браузерная механика OK (клик, куки-баннер, попап, модалка, iframe, shadow DOM, доскролл,
  ввод, antibot, youtube-снапшот, wikipedia-ввод, вкладки). Смоук обновлён под инвариант:
  неуверенный выбор → вопрос → после «да» клик.
  FAIL: vision-иконка и wide-LLM — у провайдеров нет рабочих ключей (KIMI 403 подписка, ZAI не
  принимает картинки, GROQ 401/404, HF model_not_supported); аккордеон с опечаткой — без роутера
  находится текстом (`goal_sole`) и без вопроса, с vision-роутером слабый текстовый выбор уходит на
  vision-проверку и результат зависит от провайдера (вопрос или клик по координатам «не уверен»).
- Осталось: vision-гейт не должен заменять верный текстовый выбор кликом по координатам (принимать
  текстовый выбор, если vision указал на ту же область, иначе спрашивать); починить ключи провайдеров.

### Агент задач: зацикливание в магазине после «В корзину» (2026-09-29)
Разбор по промптам/ответам канала cc (история чатов deepseek) и audit.jsonl. Цепочка причин:
- Модель не видела кнопку корзины: снапшот (100) на 70% занят кликабельными div карточек каталога
  (3–4 на карточку), агент резал список до 80 по порядку снапшота — липкая шапка (корзина, вход)
  выпадала; к тому же кнопка корзины подписана ценой «4 0 8 ₽», «Корзина» — только в aria-label.
  После успешного «В корзину» идти было некуда: модель снова открывала тот же товар, собирала его,
  упиралась в защиту второго добавления, закрывала окно — и так по кругу.
- Цепочка «20 см → халапеньо 79 ₽» жала опцию по номеру старого снимка, а выбор размера уже сменил
  её цену на 49 ₽: модель не понимала, применилась ли опция, и перепроверяла товар.
- Вопрос с вариантами-товарами, придуманными моделью («Апельсиновый сок 0,3 л — ~129 ₽», стоя в
  окне пиццы); ответ «Добрый Кола» совпадал и с «Добрый Кола без сахара» — переспрос тем же списком.
- «Продолжать?» после бюджета хода протухал за PENDING_TTL_SEC (60 с): «да» через 2–3 минуты давало
  второе «Пауза затянулась. Продолжать?».

Исправлено в `task_agent.py` (общие правила, не под сайт):
- `_pick_shown`: дубли (подпись + блок) схлопываются; сверх ELEMENTS_MAX — сначала видимые на экране,
  порядок страницы сохраняется. `_item_label`: подпись без слов (цена, счётчик, глиф) — с aria/title
  в скобках («4 0 8 ₽ (Корзина - 408 ₽)»); она же в сверке подписи модели и в строке истории.
- `_reground`: каждое следующее звено цепочки (click/type) — по свежему снимку, элемент ищется по той
  же подписи; подпись пропала/сменилась — звено не исполняется, модель решает заново. Заодно у каждого
  звена своя заметка «after it».
- `_options_bounce`: вариант вопроса с ценой, слов которого не было на страницах прогона (и в цели,
  ответах человека, прошлых задачах), — назад модели один раз («открой раздел и спроси с настоящими
  названиями и ценами»); повтор уходит человеку. В промпт — правила «варианты только со страниц» и
  «открытое окно скрывает страницу — закрой его, если следующий шаг вне окна».
- «Already done in this task» в промпте: добавления в корзину не пропадают из вида, когда шаг уходит
  за окно истории (HISTORY_SHOWN).
- `_chosen_option`: несколько вариантов содержат ответ — точный выбор того, у кого нет лишних слов.
- `CONTINUE_TTL_SEC = RESUME_SEC` (10 мин) для «продолжать?»; владелец по-прежнему проверяется.

Тесты: test_task_agent 181/0 (+11), test_cc_confirm_policy 106/0 и test_cc_resolve 79/0 — починены
заглушки, устаревшие после доводки (нет `obs["shown"]`, `_is_cart_add`, `run["history"]`);
остальные наборы режима управления — 0 FAIL.

### Резолв «открой X»: веб-Google и список вариантов (2026-09-29)

Проблема: поисковый резолв сайта шёл через DDG, у которого на узких запросах (страница преподавателя,
«госуслуги», «почта россии») первыми идут Википедия, агрегаторы и карточки приложений; найденный адрес
предлагался одним вопросом «Открыть X?» — выбрать другую ссылку из выдачи было нельзя.

Сделано:
- `web_search.google_web_links`: выдача веб-Google во вкладке уже поднятого пула H
  (`browser_actions.open_headless_tab` — без запуска Chrome и без деградации в видимый пул V),
  `search?udm=14` (фильтр «Веб», без ИИ-обзора), селектор `#rso a:has(h3)`. Капча не обходится:
  `/sorry/` или антибот-виджет → карантин `google` (общий с AI Mode — тот же профиль и IP);
  нет `#rso` без капчи → warning «разметка изменилась?». Любой отказ → DDG (`search_links`).
- `computer_control.site_search: google | ddg`, по умолчанию google (`persona_template.yaml`).
- Замер `scripts/eval_site_search.py` (30 запросов, эталон — префикс адреса): правильная ссылка
  первой — Google 30/30, DDG 9/30; в топ-5 — 30/30 и 21/30; `find_site_url` верно — 20 и 13;
  медиана 0.7 с и 8.1 с.
- Список вариантов: `find_site_url` запоминает отфильтрованную выдачу (`site_choices`, TTL 120 с),
  `resolve` кладёт в via_search-действие до 5 вариантов (лучший первым, без дублей хост+путь и без
  доменов вне allow_domains). Вопрос — нумерованный список «заголовок — адрес» (адрес через
  `scrub_url`). Ответ: номер/порядковое («2», «второй», «открой 2-й») — этот вариант и согласие;
  «да» — первый; «нет» — отказ; номер вне списка — переспрос, pending жив; остальное — как раньше.
  `parse_choice` строгий, как `classify_confirmation`: вопрос, отрицание, два номера, посторонние
  слова — не выбор. Pending со списком живёт `CHOICE_TTL_SEC` (180 с). Номер выбора — в аудит
  (`choice`). У составной команды (multi) списков нет. Резолв с отказом по-прежнему уходит в
  LLM-ярус: список не подсовывает непроверенный адрес агенту задач.

Попутно найдено, не исправлено: `_google_translate` (deep_translator, прямой HTTP) получает с этого IP
`/sorry` 429 — перевод «озон → ozon» не работает, и кириллические бренды не матчатся по домену
(у обоих движков; отсюда часть промахов `find_site_url`).

Тесты: test_computer_control 979/0 (+24; тесты матча изолированы от живого Google),
test_cc_confirm_policy 114/0 (+8, сквозной «список → номер»), test_cc_parsers 128/0; остальные
наборы режима управления, test_task_agent, test_scenario — 0 FAIL.

### «Очистить диалог» стирает и память режима управления (2026-09-29)

Проблема: /api/chat/clear стирал STM/LTM/дневник и прочие хранилища, но режим управления сознательно
пропускал («состояние браузера — транзиент»). После очистки бот помнил, что его просили делать и куда
он заходил: записи аудита чата (из них «запиши, что я делал» собирает сценарий), страницу чата в
last_tab.json (подхватывалась после перезапуска), память агента задач (task_memory.json — цели,
ответы, сайты; уходит в промпт будущих задач), живые pending/прогоны.

Сделано (`memory_wipe._collect/_wipe/_restore_control`, всё — только для очищаемого чата):
- `ComputerControlManager.forget_chat`: pending, флаг «стоп», листание, контекст страницы в памяти,
  запись чата в last_tab.json (справочная «последняя» наверху — следующему по свежести), строки аудита
  чата в audit.jsonl и ротациях .1/.2 (`cc_privacy.audit_pop_chat` — под локами audit_append,
  атомарная замена; межпроцессный лок не взят — файл не трогаем). Файловый вариант
  (`forget_chat_files`) — когда режим управления выключен, а файлы прошлых запусков остались.
- `TaskAgent.forget_chat`: идущий прогон — флаги cancel+forget (остановится перед следующим шагом и
  не допишет стёртую память: `_remember` пропускает forget), законченный «продолжай»-прогон, запись
  чата в task_memory.json.
- Сценарии: прогон, идущая запись и метка автопредложения чата; сохранённые сценарии не трогаем.
- Бот: известные секреты чата (KnownSecrets.purge), отложенные скриншоты страниц.
- Корзина: строки аудита, запись страницы и память задач кладутся в снапшот; restore возвращает их
  (аудит — слиянием по ts, хвост для сценариев остаётся хронологическим).
- Не трогаем: включённость режима управления (настройка), вкладки браузера (пользователя).

Тесты: test_memory_wipe — живые менеджеры, идущий прогон, восстановление, файловый фолбэк (ALL OK;
тест теперь убирает остатки упавшего прошлого прогона); наборы режима управления, test_task_agent,
test_scenario, test_misc_features, test_wave2_sweep, test_presence — 0 FAIL.

### Агент задач: сайты по памяти модели («Додо Пицца — dodo.ru») (2026-09-29)

Проблема: «закажи пиццу» → агент спросил «На каком сайте?» с вариантами-адресами, придуманными
моделью (dodo.ru вместо dodopizza.ru); на ответ «додо пицца» открыл https://dodo.ru (аудит:
origin task). До очистки диалога подсказку давала память задач (sites: dodopizza.ru), после — нет.
`_options_bounce` проверял только варианты-товары с ценой; адрес в open модели не сверялся ни с чем.

Сделано (`task_agent.py`):
- `_site_grounded`: адрес «виден», если хост (или его поддомен/родитель — dodopizza.ru ↔
  его городской поддомен, но не dodo.ru) был в выдаче поиска прогона (`run["search_hosts"]` —
  копится, следующий поиск не затирает), на страницах прогона (`sites`), в цели/ответах человека,
  в прошлых задачах чата, в алиасах/allow_domains.
- `_sites_bounce` (из `_options_bounce`): вопрос с невиданными адресами — назад модели «поищи и
  предложи сайты из результатов»; один раз подряд, повтор — человеку.
- `_do_open`: набранный моделью адрес (не имя — имя резолвится поисковиком) с невиданным хостом не
  открывается — «NOT opened … search first / open by name»; повтор — обычная политика (вопрос «да/нет»).
  Адрес из инъекции страницы теперь сначала отбивается модели, а не выносится вопросом человеку.
- Промпт: «сайт одного очевидного — открой по имени; если подходят несколько — сначала поиск; адреса
  сайтов из памяти не писать».
- Поиск агента (`web_search_links`) — через `web_search.search_links` с движком
  `computer_control.site_search` (по умолчанию веб-Google, фолбэк DDG); у выдачи Google появились
  сниппеты (`[data-sncf]`/`.VwiC3b`, нет — пусто). Живая проверка «заказать пиццу доставка»:
  первые — городские страницы dodopizza.ru и papajohns.ru.

Тесты: test_task_agent 188/0 (+7: догадки в вопросе, отбой open, dodopizza из выдачи, домен из слов
человека, разбор адресов), test_cc_confirm_policy 116/0 (открытие чужого домена: сначала отбой
модели, повтор — вопрос); остальные наборы режима управления — 0 FAIL.

### Агент задач: починка по аудиту 29.09 (2026-09-30)

Разбор 12 прогонов «закажи пиццу» (dodopizza.ru, 27–29.09): ни одного проверяемо верного заказа,
дубли в корзине (816 ₽ = 2×408), ложные итоги, размер выбран за человека, 10–17 минут на заказ. Тесты
при этом были зелёные: `FakeCC` не моделировал гейт, `ScriptedRouter` отдавал только чистый JSON.
Каждый пункт аудита перепроверен по коду (субагенты: `.git/task-agent-fix/verify-{A,D,E}.md`), затем
исправлен с тестом по настоящему пути. После пакетов A+B — ред-тим (`redteam-AB.md`, 13 находок,
все закрыты), в конце — независимая проверка диффа (`final-review.md`). Чек-лист:
`.git/task-agent-fix/PROGRESS.md`. Бэкапы: `refs/backup/pre-task-agent-fix` (stash create — только
отслеживаемые) и `refs/backup/pre-task-agent-fix-full` (всё дерево: task_agent.py, cc_privacy.py и
тесты не отслеживаются). Не закоммичено.

Инвариант «ничего необратимого без да» и приватность:
- Авто-закрытие оверлея (`_DISMISS_OVERLAY_JS`): агент — только cookie/consent/gdpr-баннер и только
  на своей вкладке (после своего open), нажатое пишется в историю шага; окно с деньгами/заказом/
  подпиской («автоплатёж 299 ₽ [Согласен]») не соглашается ни в каком режиме; «ОК» в диалоге заказа/
  удаления не жмётся и для команд человека.
- «ОК/Да/Продолжить» внутри окна сайта — риск по тексту окна (`dialog_risk`): «Списать … с карты» —
  передача человеку, «Подтвердите заказ / Удалить аккаунт» — «да» с текстом окна в вопросе; тем же
  правилом страхует гейт для origin=task.
- Словари коммита/оплаты: de/fr/es/it/pt/pl/uk/tr, PayPal/SberPay/ЮMoney/«долями»/«Сплит»; запись/
  регистрация/ответ — отправка. Существительные («Métodos de pago», «Zahlungsart») — не оплата.
- Мгновенная покупка (Buy now, «Купить в 1 клик», Place your order) для агента — оплата.
- Снимок: `on` (выбранность — раньше терялась в `_parse_snapshot`), `sub`/`fm` (submit формы), `dis`
  (неактивен), `qs` (строго поисковое поле), `sn` по autocomplete/inputmode tel/email. На корзине/
  оформлении submit, иконка без подписи и подпись-цена — только с «да».
- Гейт: Enter/Space/Tab не от команды человека — «да» (кроме Enter сразу после ввода в строго
  поисковое поле, метка кода `search_enter`); открытие алиаса приложения агентом — «да».
- Подпись сверяется в момент клика/ввода (`expect`, `_LABEL_MATCH_FN`, CDP и AppleScript): узел
  «Далее» → «Подтвердить заказ»/«Далее — оплатить» не нажимается. Force-клик сквозь чужой слой — нет.
- «да» на шаг живёт 10 минут (клавиша — минуту): перед исполнением элемент ищется на свежем снимке
  (тот же адрес, подпись, блок; иконка — тег+блок; найденное find — пересъёмка места).
- Коммит заказа — одно «да» с фактами: состав, итог со страницы, адрес/время/оплата из брифа; итог
  перечитывается перед кликом, изменился — вопрос заново.
- `open <имя>` агента не ходит в поисковик (поиск — только действием search с проверкой ПДн).
- Приватные страницы: строка шага после «да», логи «Выполнено/Не удалось», подписи в аудите (шаги
  агента, суммы/время/длинные), лог шага агента — маской; в группе — заглушки, а «да» вслепую на
  скрытый шаг не принимается (шаг делает человек).

Разбор ответа, отмена, подтверждения:
- JSON ответа модели — `raw_decode` с каждой «{»: `{{secret1}}` вводится, `<think>` не исполняется,
  `n` — только целое.
- Одно правило отмены (`STOP_CMD_RE`) для бота и агента; «не надо»/«хватит» на вопрос или «да/нет» —
  «нет», а не отмена задачи. «стоп» во время подтверждённого шага останавливает прогон (busy ставится
  до шага, проверка после пересъёмки). «Коннор, да» — согласие, «да?» — нет.
- Секреты: целиком прячется ответ на вопрос о пароле/коде/логине/паспорте/карте/кодовом слове;
  телефон/почта в обычном ответе — каждый своим плейсхолдером; «Пепперони» — не секрет.
- Ссылка из выдачи своего поиска — без двойного «да» (`from_search` — только аудит).

Корзина и итог (общие правила, без правил под сайт):
- Добавление засчитывается только по счётчику/сумме корзины в шапке до и после клика; не выросли —
  «не добавлено», повтор вслепую — только с «да»; корзины не видно — «открой корзину и проверь».
- Ключ добавления — товар (название из блока), не текст кнопки: «пепперони и маргарита» — два товара.
- Перед «В корзину» опции сверяются со слотами: размер не назван — спросить с размерами со страницы.
- `done` по заказу принимается, только если корзина сходится с брифом или видна страница «заказ
  принят»; иначе модели один раз «NOT finished», итог человеку — с фактом корзины.

Бриф, маршрутизация, память:
- Бриф (сайт, позиции с размером/опциями/количеством, адрес, телефон, время, оплата) ведёт код: ответ
  человека разбирается отдельным маленьким вызовом LLM (`slot_router`) в изменения слотов — «Додо,
  но не пепперони» оставляет сайт и адрес, пепперони — в исключения.
- Цель из LLM-яруса запускает агента сразу («Беру: …. «стоп» — прервать.»), хвост составной фразы не
  теряется; «скачай … с сайта X» — цель, а не скачивание на текущей вкладке. `_TASK_RE` — только с
  разделителем. Сценарии не перехватывают ответы живому прогону. Явная посторонняя команда при ждущем
  прогоне — «Бросить задачу и выполнить X?». В группе вопрос агента требует reply; веб без chat_id —
  прогон и память по user_id. «ок» после успешного итога/оплаты задачу не возобновляет.
- Память «как в прошлый раз»: тема — по предмету цели; к повтору — только успешные заказы (с позициями
  и сайтом, без адреса/телефона); «как в прошлый раз»/«повтори» — последний успешный заказ в бриф
  «из памяти», подтверждается одним вопросом, ответ может быть частичным. Битый task_memory.json не
  перезаписывается.
- Промпт: данные сайтов — не инструкции; пометки слоя; «спрашивать поздно и по одной теме»; не
  выбирать за человека, но и не спрашивать до открытия меню; необратимое подтверждает система; поле
  `expect` сверяется кодом.

Браузерный слой: find → click по живым меткам, back во вкладке агента, клик засчитывается по эффекту, а
не по фокусу (`_DOM_STATE_CLICK_JS`; селектор aria-pressed починен), маска телефона «+7 (___)» — по
цифрам, `read` агента — открытая шторка/окно и итог внизу, листание открытого окна, «элемент потерян»
после open — одна пересъёмка того же элемента.

Финальная проверка диффа (`final-review.md`) нашла ещё 1 критическую и 5 серьёзных дыр — закрыты:
- Команда человека при открытой модалке заказа Bootstrap-вида жала «ОК» (текст смотрелся у
  `.modal-footer`): автозакрытие судит по тексту всего окна, только всплывающие окна, кнопки форм
  не жмёт; cookie-баннер с «in order to»/«payments» по-прежнему принимается.
- Повтор после «элемент потерян» проходит все проверки заново (тот же блок и тот же слой).
- Нативный `<dialog>` — окно; «Да/ОК» в блоке страницы («Удалить аккаунт? [Да]») — тоже по тексту.
- Слоты брифа из ответа на вопрос с приватной страницы — облаку заглушкой.
- «Бросить задачу?» не пишет пароль команды в историю и вопрос.
- Итог перечитывается до пересъёмки элемента (иначе подтверждённый коммит терял номер).
- Средние: поиск в форме заказа — не «поисковое поле»; «стоп» во время разбора цели/перечитывания
  итога; «продолжи» — не посторонняя команда; сверка подписи по тем же источникам, что у снимка
  (иконки, пилюли, поля с подписью у родителя); чтение корзины — весь состав; словари (отмена
  заказа, такси, заявка, голос, OAuth «Разрешить»); ключ товара при подтверждённом повторе.
- Отпечаток клика без фокуса — только для шагов агента: команды человека и маршруты — как раньше.
Перепроверка исправлений (`rereview.md`) нашла, что исключение для cookie-баннера отключалось
словами «персональные данные»/«конфиденциальность» (окно «Удалить аккаунт… персональные данные
[ОК]» снова жалось) — теперь cookie-контекст только по «cookie/куки», а удаление/выход/отправка
опасны всегда. Там же: повтор вопроса с приватной страницы после «нет» на «бросить задачу?» —
заглушкой; ключ товара у подтверждённого «В корзину»; сбой разбора цели не оставляет прогон
«занятым»; «Продолжить» рядом со «Списать … с карты» — передача человеку (только глагол списания рядом с
суммой или картой: «Список товаров», «Списать бонусы», «free of charge» — не оплата).
Целевая перепроверка этих правок (`recheck2.md`): cookie-окно распознаётся по классу/id
(cookie/gdpr/consent), а не по словам в тексте; окна без `role=dialog` (absolute) целиком
считаются окном, и «Удалить аккаунт?/Подтвердите заказ» в них не жмутся; баннеры со словами
«удалить cookie», «отправки уведомлений» и кнопкой «Accept All Cookies» снова принимаются.
Оставлены: текст вопроса в аудите `task_ask` (после `redact_inline`), порядок строк E1 в STM,
проверка телефона на AppleScript-вводе.

Решения, принятые по ходу (не из аудита):
- Строгий `qs` добавлен отдельно от `q`: `q` по подписи нужен командам человека («введи X в поиск»).
- Подписи элементов маскируются только секретоподобными значениями (телефон, почта, пароль формы
  пароля), слово-ответ («Маргарита» как пароль) пункт каталога не прячет.
- Цель в хвосте цепочки ПОСЛЕ подтверждённого шага по-прежнему идёт через «Берусь за задачу?»
  (прямой запуск задвоил бы STM) — редкий путь.

Тесты: новый `scripts/test_ba_sandbox.py` — JS снимка/клика/ввода/оверлея/чтения на собственном
headless-Chromium Playwright (локальные страницы, без сети и браузеров пользователя; нет Playwright —
SKIP). В `test_task_agent` — обвязка `real_cc` (настоящий гейт, заглушены только браузер и снимок) и
глобальная заглушка `PrivateRouter` (тесты не зовут живую Ollama). Переписаны тесты, закреплявшие
неверное поведение: «клик без эффекта — повтор сразу, без вопроса» (дубли), «скорее всего добавлено»,
«ссылка из выдачи — via_search» на FakeCC без гейта, «Place your order → вопрос», «task → Берусь за
задачу?». test_cc_ux больше не оставляет `data/ux_*` (data_dir() читает VPC_DATA_DIR, а тест ставил
DATA_DIR).

### Агент задач: живой прогон dodopizza 30.09 — порядок сообщений, сайт из памяти, добавки, «что-нибудь ещё?»

Живой прогон «закажи пиццу» (веб-версия). Поправлено:

- **Порядок сообщений в веб-версии.** Промежуточные «Нажал …» (13:58) стояли под ответом (13:59). Лента
  сортируется по `ts`, а пузырь ответа получал `ts` момента отправки вопроса (+1 мс). Поэтому ответ вставал
  сразу под вопросом, выше всех сообщений хода из inbox.
  - Сервер отдаёт в стриме `reply_ts` — метку записи ответа в STM (`server._reply_stm_ts` через
    `_turn_stm_tail`, в кадре хода). Фронт ставит пузырь по ней; сверка с историей — с допуском `TS_EPS`.
  - Пока свой запрос идёт в режиме управления, inbox опрашивается раз в 3 с: «Нажал …» появляются по
    ходу, а не пачкой раз в 15 с.
  - После перезагрузки порядок был верным и раньше (история STM).
- **Сайт из памяти открывался до ответа.** Модель открыла dodopizza.ru дважды и только потом спросила
  «там же?» (правило промпта проигнорировала).
  - Теперь код до любого действия (`open`/`search`/`click`/`type`/`key`/`back`) спрашивает «Как в прошлый
    раз — на X? (да / другой сайт)». Это делается, если у прошлой задачи той же темы есть сайт, а человек
    в этой сайт не выбрал. Не спрашивает, если цель — «как в прошлый раз» или модель уже спросила сама.
  - «да» записывает сайт в бриф от человека; открытие этого сайта больше не переспрашивает «открыть?».
    Адреса с query/fragment — по-прежнему с «да».
- **Добавки.**
  - Что считается добавкой: в окне товара с кнопкой «В корзину» — пункты с ценой («Моцарелла 69 ₽»);
    на странице товара — переключатели с ценой.
  - Если человек о добавках не говорил, перед «В корзину» задаётся один вопрос на товар, с названиями и
    ценами со страницы (до 6 и «ещё N»).
  - Не спрашивает, если добавки названы, сказано «без добавок/как есть», позиция взята из памяти или
    окно — шторка корзины («Итого», «К оформлению»).
- **«Что-нибудь ещё?»** Перед уходом из покупок (переход к оформлению, оплата, коммит заказа) задаётся
  один раз за прогон, если задача что-то добавила. В вопросе — состав.
  - Если корзина была не пуста до первого добавления (на живом прогоне — 849 ₽ с прошлых раз, итог
    1 208 ₽ при пицце за 359 ₽), вопрос это называет и предлагает убрать.
  - То же — в вопросе о самом заказе («в корзине есть и то, что лежало там до задачи»).
  - Модель сама спросила «что-нибудь ещё?» — код не повторяет.
- **«К оформлению заказа» — не коммит для агента.** Раньше «да» спрашивали дважды: на переход и ещё
  раз после окна входа.
  - `computer_control.checkout_step_label`: подпись целиком — «(перейти) к оформлению (заказа)»,
    «proceed/go/continue to checkout», «zur Kasse».
  - Для `origin: task` гейт такой клик не считает риском, «да» — только на сам заказ. Команда человека —
    как раньше. С суммой в подписи или «Оформить заказ» — как раньше.
- **Суммы человеку — с валютой и разрядами** («Корзина на сайте: 1 208 ₽», а не «1208»).

Тесты:
- `test_task_agent` (357, «живой 30.09», «добавки», «ещё», переход через настоящий гейт);
- `test_api_security` (`_reply_stm_ts`);
- `tsc` веба.

Независимая проверка этой волны (`review-live-3009.md`) нашла, что без «да» ничего необратимого не
происходит, но было три существенных дефекта. Исправлено:
- **Предлагался не тот сайт.** «Как в прошлый раз» брал хост из `sites` — туда попадала вкладка
  человека (youtube, почта), открытая до задачи.
  - Теперь сайт задачи записывается только после успешного шага в браузере.
  - В память кладётся адрес, где выросла корзина (иначе — где кончилась задача). Название вида «додо»
    заменяется адресом, без `www.`.
  - Код предлагает только сайт из брифа записи и не предлагает приватные хосты.
- **Открытие без «да» было слишком широким.** Его снимал любой ответ «да» на вопрос модели о сайте.
  Теперь без «да» открывается только главная сайта, который предложил код; адрес с путём — с «да».
- **«Что-нибудь ещё?» перечислял заказанное, а не добавленное.** Теперь в вопросе то, что реально в
  корзине, а недобавленное — отдельно («Ещё не в корзине: …»).

Мелочи:
- «лежало до задачи» фиксируется при первой попытке добавления;
- вопрос модели «что-нибудь ещё» распознаётся и без «ё», и голым, когда что-то уже добавлено;
- позиция с приватной страницы — вопрос приватный;
- добавки ищутся только в окне товара, без нулевых цен и «от/до N»;
- длинная подпись (текст, aria или title) и кнопка отправки формы — не «переход к оформлению»;
- в вебе ответ не встаёт раньше вопроса при расхождении часов, части ответа и досылаемые списки —
  следом за ним;
- суммы с копейками.

Итог: `test_task_agent` — 368 проверок, 25 наборов без провалов.

### Агент задач: живой прогон dodopizza 30.09, 15:43 — вопрос о сайте до открытия, вопросы о товаре от модели, корзина под окном товара

Прогон «закажи пиццу» в веб-версии, прошлого сайта в памяти задач не было. Поправлено:

- **Сайт открывался до вопроса.** Модель нашла «заказать пиццу доставка», открыла первый результат
  (страницу dodopizza.ru) и только потом спросила «На каком сайте заказать пиццу?».
  - Вопрос «как в прошлый раз — на X?» (прошлый раунд) срабатывает только при сайте в памяти. Ссылка из
    выдачи своего поиска открывается без «да». Правило промпта «один сайт очевидно подходит — открой»
    позволяло открыть сайт сразу.
  - Теперь (`_site_which`, в `_act` перед `_do_open`): цель — заказ/покупка, человек сайт не выбирал
    (нет в брифе, не принят «как в прошлый раз», адреса нет в цели/ответах, вопроса «где?» не было или
    ответ на него — голое «нет»). В этом случае `open` не исполняется:
    - есть выдача поиска — пауза с вопросом «на каком сайте?» и вариантами из выдачи;
    - выдачи нет — назад модели: «сначала поиск».
  - Спрашивает один раз за прогон. Правило промпта для заказов: искать, потом открывать — система
    спросит сама.
- **Что спросить перед шагом — решает модель, когда — код** (`_pre_questions`, предложение
  пользователя). Отдельный короткий вызов LLM (роутер разбора ответов). В нём цель, бриф, прошлые
  вопросы и ответы, страница и вопрос «что нужно спросить человека перед этим шагом? каждый вопрос с
  новой строки, NONE — если нечего».
  - Ответ разбирается в `_question_lines`: вступление и разметка — мимо, варианты «- …» — под своим
    вопросом, не больше 6 вопросов.
  - Страница с приватной — только локальной модели (`_llm_for`, то же решение, что у шагов).
    Секреты — `{{secretN}}`.
  - Используется в двух местах:
    - **Вопрос о сайте.** Варианты из выдачи. Если модели нет, ответ пуст или в вопросе адрес не из
      выдачи — варианты собирает код.
    - **Вопросы о товаре** (`_item_questions`, первым в `_cart_add_gate`). Перед первым «В корзину»
      товара — всё о нём одним сообщением («Перед тем как положить «X» в корзину: …; «как есть» —
      положу как выбрано»). Раньше вопросы шли по одному: модель спрашивала размер, потом код —
      добавки. Модели передаются подписи окна товара с отметкой выбранного.
      - Если модель промолчала о размере (размеров несколько, человек не назвал) или о платных
        добавках (`_addon_options`, прежний `_addons_question`), код добавляет эти вопросы сам.
      - Ответ без размера («как есть») оставляет выбранный размер: `sizes_asked` → `_options_mismatch`
        не переспрашивает.
      - Позиция из памяти («как в прошлый раз») — без вопросов.
  - О других товарах, цене и заказе не спрашивает: «что-нибудь ещё?» — перед оформлением, «да» на
    заказ — с фактами.
- **Добавление под окном товара «не проверено» → чтение по кругу → стоп.**
  - Окно товара вытеснило из снимка шапку со счётчиком. «В корзину» (1 208 → 1 536 ₽) выходило «NOT
    verified»: браузер не увидел изменения за 3 с, так как модалка в портале вне бюджета обхода DOM.
  - Модель открывала корзину и читала страницу, но чтение шторку Додо не берёт. После шести одинаковых
    чтений срабатывало «Хожу по кругу».
  - Теперь база «до» для корзины — последний снимок, где корзина была видна (`cart_seen`). Туда же
    ложится «лежало до задачи».
  - Повтор того же текста чтения — модели: «тот же текст, ищи в списке элементов / find».

Тесты: `test_task_agent`. Сценарии:
- «живой 15:43» — вопрос о сайте с выдачей до открытия; вопрос от модели; адрес не из выдачи; без
  поиска; «другой сайт»; не заказ;
- «вопросы о товаре» — от модели одним сообщением; страховка кода; всё названо; разбор;
- «живой 15:49» — корзина прошлого снимка; «тот же текст».

Поправлены старые тесты:
- C4a — размер теперь спрашивает код, а не модель по отбою;
- c1/m1 — сайт в цели: тесты о другом;
- M2 — у чата не было записи памяти, и проверялось обычное «Делаю?», а не «принятый сайт».

Независимая проверка (`review-live-3009b.md`) обхода «да» не нашла. Найденное исправлено:
- **Вопрос о сайте обходился.**
  - Выбором сайта считался любой прошлый вопрос со словом «магазин»/«сайт» или с адресом. Теперь —
    только вопрос «где?» (`_is_where_q`: «на каком сайте / где заказать / which site» или варианты —
    сайты); вопросы-предложения кода — только по «да».
  - Выбор привязан к адресу. Ответ на «где?» → `site_pick`; «да» на «как в прошлый раз» → `site_ok`.
    Другой магазин модель молча не открывает («NOT opened — the user chose …»).
  - Клик по ссылке с адресом (`href`) на другой сайт проверяется тем же правилом до клика. Магазин,
    открытый кликом без адреса, — «Заказываем здесь — на X?» до первого действия на нём
    (`_site_here`; вкладка, где задача началась, — выбор человека).
  - Правило только для покупки (`_BUY_GOAL_RE`: не «найди в почте письмо о заказе», не «мой заказ»).
    Алиас конфига и приватный хост — не выбор магазина. Поиск не работает — «спроси, где заказать».
- **Ответ без размера принимал выбранный.** Выбранный размер остаётся только на «как есть» /
  «любой» / «да» без чисел. «35, тонкое», «XL» — модели «спроси размер».
- **Вопрос о товаре «сгорал».** «В корзину» на карточке каталога (спрашивать нечего) помечал товар
  спрошенным, и окно товара потом не спрашивало добавки. Теперь помечается, когда вопрос задан или
  модель оценила окно. Без модели — прочие переключатели окна (тесто) строкой кода.
- **База корзины.** Только того же сайта. Сбрасывается после добавления или правки корзины
  («−»/«Удалить»), если корзины не видно.
- **Вопрос о сайте** строится по выдаче, как облачный промпт, без приватного брифа.
- **Вопросы модели о паролях/кодах, контактах, оплате** выбрасываются: их спрашивает сама система, а
  текст страницы мог подсунуть такой вопрос.

Тесты: блок «review: …».

Перепроверка нашла ещё семь мест, исправлено:
- «да» на вопрос модели «заказать на papajohns.ru?» меняет выбор;
- ответ «нет / другой сайт» на «здесь?» / «как в прошлый раз?» — на этом сайте агент не действует
  (`site_no`), пока человек его снова не выберет;
- магазин, открытый кликом при выбранном другом сайте, — вопрос «Сейчас открыт X, а выбран Y.
  Заказываем здесь?»; сайт, названный в цели, — тоже выбор;
- вопрос о товаре помечается отдельно для карточки каталога и окна товара;
- «оставь как есть» принимается и в ответ на вопрос модели о размерах; числа, не совпадающие с
  размерами («2 штуки»), не мешают;
- алиас конфига и вкладка, где задача началась, — выбор человека; сайт этой вкладки — не «адрес
  наугад»;
- регулярки: «хочу пиццу с доставкой» — покупка; «Where do you live?», «Каким сервисом оплатишь?» —
  не вопрос «где заказать».

Тесты: блок «N1–N7».

Вторая перепроверка:
- алиас конфига и вкладка, где задача началась, перекрывали выбор и отказ человека. Теперь они —
  выбор, только пока человек сам не выбирал. На вкладке старта при выбранном другом сайте переходы
  (клик по выдаче) разрешены, а «В корзину» — только после вопроса «Сейчас открыт X, а выбран Y»;
- «да» на уточнение модели («35 см?») засчитывалось как «оставь как выбрано». Теперь «да» — только на
  вопрос кода о товаре; числа размеров сравниваются целыми;
- «где моя доставка», «условия доставки» — не покупка.

Тесты: блок «Перепроверка 2».

Третья перепроверка:
- на вкладке старта при выбранном другом сайте без вопроса проходят только ссылки-переходы. Кнопки
  («Добавить», «+», «Купить», «В корзину»), ввод и клавиши — после «Сейчас открыт X, а выбран Y.
  Заказываем здесь?»;
- вопрос о самом заказе называет сайт («Оформляю заказ: сайт shop.test; …»).

Тесты: блок «Перепроверка 3». Итог: `test_task_agent` — 414 проверок, полный прогон 25 наборов —
0 FAIL.

Не сделано:
- браузерный слой: отпечаток клика не видит модалку-портал за бюджетом 1500 узлов, чтение не берёт
  шторку корзины Додо;
- проактивное сообщение (погода) пришло посреди задачи в режиме управления.

### Агент задач: живой прогон 30.09, 18:11 — алиас и оставшаяся вкладка не выбор сайта; опции до вопроса о товаре

«Закажи пиццу» — агент нашёл пиццерии поиском и открыл dodopizza.ru без вопроса. Потом сам открыл
«Пепперони фреш» и закрыл, а после ответа «чикен» сам перещёлкал размеры 20 → 25 → 35 см и тесто
«Тонкое». Вопрос о товаре вышел с «Сейчас выбран 35 см» — выбором модели.

- **Сайт без вопроса.** Прошлый раунд (находка ревьюера N6) считал выбором человека:
  - сайт из алиасов конфига — в `connor.yaml` есть `пицца: dodopizza.ru` и `додо пицца: dodopizza.ru`
    для «открой пиццу»;
  - сайт вкладки, на которой задача началась, — а она осталась на Додо от прошлой задачи.

  Обе поблажки убраны из `_site_choice`:
  - пока в этой задаче сайт не выбран, магазин не открывается без вопроса с выдачей;
  - на уже открытом магазине — «Заказываем здесь — на X?» до первого действия (при открытии без
    поиска — тоже он, а не «сначала поиск»);
  - без вопроса на вкладке старта — только переход по ссылке с выдачи поисковика или к выбранному
    сайту.
- **Опции до вопроса о товаре** (`_early_option`, теперь `_option_unchosen`). Переключатель в окне товара (размер, тесто,
  добавка) до вопроса о товаре в этом окне не нажимается, если человек его не называл: модели —
  «нажми В корзину, система спросит». Число размера сверяется со словами человека, прочее — по
  значимому слову подписи без цены. Шторка корзины («Приборы») — не окно товара.
- **Промпт:** окно товара не открывать, пока человек не выбрал товар; размеры и опции ради цен не
  щёлкать.

Тесты:
- «живой 18:11» — алиас и оставшаяся вкладка, размер и тесто до вопроса;
- тесты, где магазин уже открыт, а цель его не называет, получили сайт в цели (они о другом);
- два теста N6 — по новому правилу;
- `test_cc_gate` (сайт в цели), `test_cc_resolve` (подстановка `_early_option`).

Итог: `test_task_agent` — 417 проверок, 25 наборов — 0 FAIL.

### Агент задач: разделы магазина — «что посмотрим?» и показ раздела (2026-09-30)

Запрос пользователя: когда бот открыл сайт, а что заказать, человек не сказал, — сначала перечислить
разделы сайта (у Додо: пиццы, комбо, закуски, напитки…). Человек выбирает раздел, бот показывает его:
весь список или короткий пересказ.

Решено по размеру раздела:
- до 10 позиций — список целиком («название — цена»);
- больше — коротко: сколько позиций, разброс цен, 3–5 видов с 2–3 примерами и подсказка «назови,
  покажи все или отфильтруй»;
- «покажи все» — список со страницы, до 40 позиций.

Длинный список в чате плохо читается, а один пересказ заставлял бы переспрашивать.

- **`_browse_step`** (в `_step`, до хода модели шагов). Срабатывает, когда цель — покупка, сайт
  выбран, товар не назван и ничего не добавлено. Когда — решает код, что — модель по странице
  (`_pre_llm`, тот же вызов, что у вопросов перед шагом).
  - **Разделы** (`_browse_sections`). Модель называет разделы каталога/меню из подписей страницы. Код
    оставляет только точные подписи страницы, без цен, корзины и входа; меньше двух — не спрашивает.
    Если есть прошлый заказ той же темы — строкой «В прошлый раз: …». Открытое окно (город, cookie)
    сначала решает модель шагов.
  - **Ответ** (`_browse_answer`):
    - раздел — по номеру или названию; «пиццы», записанное разбором в позиции, — всё равно раздел;
    - назван товар — дальше модель шагов;
    - «покажи все» — весь список;
    - прочее — как раньше.
  - **Открыть раздел** (`_browse_open`) — клик кодом по подписи раздела через `_do_element`, со всеми
    проверками клика.
  - **Показ** (`_browse_show`). Позиции — подписи с ценой на странице и вокруг раздела. Если модель
    назвала то, чего на странице нет, или её нет — список кодом.
- **Разбор ответа по слотам:** общий вид («пицца», «что-нибудь поесть») — не товар; иначе разделы не
  показывались бы при «закажи пиццу».
- **Промпт модели шагов:** разделы и показ раздела делает система; когда человек назвал товар —
  открыть его.

Тесты: блок «Разделы магазина»:
- разделы со страницы;
- «пиццы» → клик и список;
- номер раздела и выдуманная позиция → список кодом;
- «покажи все»;
- большой раздел — коротко;
- назван товар в ответе и в цели.

`test_task_agent` — 425, 25 наборов — 0 FAIL.

Независимая проверка (`review-browse.md`) нашла 7 дефектов, исправлено:
- **Разделы чужой вкладки.** Сайт назван словом («в Додо», без адреса) — разделы спрашивались про
  вкладку новостей человека. Теперь только на явно выбранном сайте или там, куда пришёл агент (не
  вкладка старта, не поисковик).
- **Отказ открывал раздел** («без комбо», «кроме пиццы»). Раздел — номером или ответом из одного его
  названия (со словами «давай/покажи»); отрицание — не выбор (`_picked_section`).
- **Названный товар стирался** («Ролл Филадельфия» ~ «Роллы»). Позиция очищается, только если она —
  само название раздела (`_words_in`, в одну сторону).
- **Вопрос посреди выбора товара.** Одна попытка при первом приходе на сайт.
- **Показ раздела без фильтра.** Просьбы о данных/контактах/оплате, чужие адреса или цены не со
  страницы (числа от 50) — список кодом (`_section_text_ok`). Акции и доставка — не позиции.
- **Неудавшийся клик по разделу** выдавался за раздел. Раздел открыт, только если клик прошёл и
  страница изменилась; видимая подпись — первой.
- **Цель о собранной корзине** («оформи, корзина уже собрана») — без разделов.
- **Правило промпта о разделах** — только пока они в ходу.

Тесты: «review-browse 1–8». `test_task_agent` — 435, 25 наборов — 0 FAIL.

Ограничения:
- «покажи все» показывает то, что есть в снимке страницы; на длинной ленте может быть не весь раздел;
- «закажи две пиццы» — количество без товара записать некуда.

### Агент задач: живой прогон 30.09, 19:28 — раздел на одностраничном меню, карточка товара до выбора

Разделы спросились верно. После «пиццы» код нажал «Пиццы», но раздел не показал: модель сама нажала
«Пепперони фреш» и спросила про размер и тесто.

- **Раздел не показан.** Ссылка «Пиццы» на Додо лишь прокручивает к разделу: адрес и подписи снимка
  те же. Проверка «страница изменилась» (добавлена по review-browse п.6) решала, что раздел не открылся,
  и отдавала ход модели. Теперь раздел открыт, раз браузер подтвердил клик («→ ok»). Клик без эффекта
  браузер помечает «не уверен» — такой случай по-прежнему уходит модели. Позиции берутся сначала вокруг
  раздела (`snapshot_for_goal`), затем со всей страницы.
- **Карточка товара до выбора** (`_early_item`). Покупка, а товар человек не назвал (бриф пуст, в
  корзине ничего): клик по карточке (подпись с названием и ценой) не исполняется, модели — «спроси,
  какой, со списком со страницы». Товар, названный человеком (слово подписи в его словах), — можно.
  Правило промпта модель нарушала второй раз подряд, теперь это проверка кода.

Тесты:
- «живой 19:28» — прокрутка к разделу, «на твой вкус», товар назван;
- «review-browse 6» — клик «не уверен»;
- `test_cc_resolve` — `_early_item` в подстановке.

`test_task_agent` — 438, 25 наборов — 0 FAIL.

### Агент задач: живой прогон 30.09, 20:18 — сайт, выбранный названием; страница без чётких разделов

После «Додо пицца» агент открыл dodopizza.ru, а вопроса о разделах не было: модель сама перечислила 14
пицц.
- **Причина.** Сайт выбран названием («Додо Пицца» — в брифе без адреса), а вкладка агента осталась на
  Додо от прошлой задачи. По правилу review-browse п.1 разделы для сайта, выбранного только названием,
  не спрашиваются на вкладке старта (чтобы не спрашивать про вкладку новостей). Вызов модели «какие тут
  разделы» даже не выполнялся.
- **Название → адрес** (`_name_fits_host`, в `_site_choice`). Название из брифа — это хост, если:
  - алиас конфига с тем же названием («додо пицца: dodopizza.ru»);
  - или отличительное слово названия транслитом есть в имени хоста («додо» → dodo ⊂ dodopizza,
    «Папа Джонс» → papajohns, «Pizza Hut» → pizzahut).

  Общие слова («пицца», «суши», «доставка») не считаются. Такой сайт — выбранный: разделы, вопросы
  «здесь?» и открытие — как для сайта с адресом.
- **Доработка идеи пользователя «спросить модель, есть ли тут чёткие разделы».** Модель по странице
  называет разделы или отвечает NONE (так было и раньше). Теперь при NONE, если на странице есть товары
  с ценой, код показывает саму страницу тем же правилом («На X: …» — до 10 списком, больше коротко,
  «покажи все»), а не отдаёт ход модели шагов.

Тесты:
- «живой 20:18»;
- «название → адрес»;
- «разделов нет — страница показана» (от модели и списком кодом).

`test_task_agent` — 442, 25 наборов — 0 FAIL.

### Агент задач: названия сайтов человека — «додо пицца» = открытый dodopizza.ru (2026-09-30)

Предложение пользователя: запоминать, как человек назвал сайт, и приравнивать к тому, что открыли.
- **Словарь названий** (`run["site_names"]`, в памяти задач — поле `site_names` последней записи чата;
  при старте собирается из записей чата). Ключ — название без служебных слов («давай додо пиццу» →
  «додо пиццу»). Длинные фразы, адреса и приватные хосты не пишутся. «Очистить диалог» стирает его
  вместе с записями.
- **Когда название связывается с адресом:**
  - сверилось само — алиас конфига, транслит («додо» → dodopizza), заголовок выдачи поиска с этим
    названием;
  - человек ответил названием на вопрос с адресами («додо» → «- dodopizza.ru — …»);
  - «да» на новый вопрос «„Якитория“ — это yakitoria.ru? Заказываем здесь?».

  Этот вопрос задаётся, когда сайт выбран названием, а сверить его с адресом нечем: перед открытием,
  перед первым действием на сайте, куда агент пришёл сам. После «да» спрошенный адрес открывается
  сразу, без круга к модели (все проверки открытия — `_do_open`).
- **Названный сайт из словаря** — выбранный: открывается без вопросов, разделы спрашиваются, другой
  магазин — «Сейчас открыт X, а выбран Y».
- Без сверки название не привязывается: модель, открывшая не тот магазин, не должна записать ошибку в
  память навсегда.

Тесты:
- «названия»: «Якитория» → «это yakitoria.test?» → «да» → открыт и запомнен → следующая задача без
  вопроса;
- служебные слова.

`test_task_agent` — 446, 25 наборов — 0 FAIL.

### Агент задач: опция не из ответа человека — «Тонкое» на «25, традиционное, бекон» (2026-09-30)

Живой прогон 21:05. На вопрос о товаре человек ответил «25, традиционное, бекон». Модель шагов (DeepSeek)
одной цепочкой нажала «25 см» и «Тонкое» (audit.jsonl: клики с разницей 2,4 с), затем «Бекон», а
«Традиционное» вернула только следующим шагом. Код подписи не подменял: звено цепочки переснимается по
той же подписи (`_reground`), клик — по метке снимка. «Тонкое» выбрала сама модель, её сырой ответ
не сохраняется.
Правило промпта «не выбирать опцию, которую человек не выбирал» было, но код держал его только до
вопроса о товаре (`_early_option`). После ответа любой переключатель окна нажимался.
- **`_option_unchosen`** (бывший `_early_option`) → `"early"` — до вопроса о товаре, как раньше;
  `"after"` — после вопроса, опция не названа ни в брифе, ни в словах человека (число размера, значимое
  слово подписи без цены). Выбор, отданный агенту в ответах с вопроса о товаре («любое», «на твой
  вкус», «выбери сам» — `_ANY_RE`, `_YOUR_PICK_RE`), — не за человека.
- **`_bounce_option`** — `"after"`. Первый раз клик не исполняется, модели — «человек это не выбирал:
  только названное, остальное оставь как выбрано»; цепочка на этом обрывается. Модель настаивает (та же
  опция, цена не в счёт: «Бекон 99 ₽» → «Бекон 89 ₽») — вопрос человеку «Нажать «Тонкое»? В твоём
  ответе этого не было. (да/нет)». Так ответ другими словами («обычное» — это «Традиционное») стоит
  одного вопроса, а не тупика.
- **Промпт:** выбирать в окне то, что выбрал человек; опция, уже отмеченная выбранной, клика не
  требует.

Тесты: «живой 21:05» — цепочка «25 см → Тонкое → Бекон» (нажаты 25 см и бекон, «Тонкое» — возврат
модели, повтор — вопрос, «нет» — не нажато); «тесто на твой вкус» — нажато без вопроса. Без ветки
`"after"` оба первых теста падают. `test_cc_resolve` — подстановка переименованного метода.


`test_task_agent` — 449, 25 наборов — 0 FAIL.

### Агент задач: план заказа — подзадачи ведёт код (2026-09-30)

Живой прогон 21:02–21:16. Пицца легла в корзину. Дальше модель сама пошла в корзину, потом
«Показать дополнительную информацию». Только на шаге к оформлению код спросил «что-нибудь ещё?».
На «добавь какой-нибудь напиток» модель выбрала колу за человека и по кругу жала «Добрый Кола» и
«Сохранить». Затем ушла в «Напитки», снова открыла уже положенную «Чикен бомбони» и на 40-м шаге
остановилась. Своего плана у агента не было: что сделано, а что осталось, модель восстанавливала по
истории шагов.

Предложение пользователя: разбить задачу на подзадачи и отмечать выполненные, а новая просьба заводит
новые подзадачи. Решение общее, не под Додо:
- **План из фактов** (`_plan_lines`, в промпт каждого шага). Шаги: магазин; выбор, что заказать
  (разделы), пока ничего не названо; по пункту на каждый вид без позиции («напиток»); по пункту на
  каждую позицию брифа; «что-нибудь ещё?»; корзина; оформление. ✓ ставит код: позиция — по
  корзине (`cart_adds`, сверенные счётчиком), «ещё?» — по ответу «нет», корзина — по открытой
  шторке или адресу корзины. ▶ — первый несделанный шаг. Модели: работать только над ним,
  сделанное не повторять.
- **«Что-нибудь ещё?» — сразу, когда всё просимое в корзине** (`_plan_step` до хода модели,
  `_more_due`). Раньше вопрос звучал только на переходе к оформлению. Добавление должно быть сверено
  счётчиком, иначе сначала модель проверяет корзину. Снова спрашивается после новых добавлений.
  После «нет» больше не спрашивается. Перед оформлением вопрос задаётся по-прежнему и называет
  недобавленное.
- **Ответ на «ещё?»** (`_more_answer`, флаг `more` у вопроса кода и у такого же вопроса модели):
  - новые позиции — план их показывает, дальше модель;
  - вид без позиции (новый слот разбора `kinds`: «какой-нибудь напиток» → «напиток») — снова раздел и
    выбор (`_browse_again`): раздел, совпавший с видом («Напитки»), код открывает сам и показывает,
    иначе вопрос о разделах;
  - «да» без подробностей — вопрос о разделах;
  - «нет», «всё», «хватит», «оформляй» (`_MORE_NO_RE`) — дальше корзина.
  Виды идут очередью; позиции брифа, названные до просьбы, ответом на неё не считаются (`items0`).
  Раздел под открытым окном ждёт, пока модель его закроет.
- **Выбор за человека.** Пока идёт выбор вида, карточку товара, который человек не называл, модель
  не открывает (`_early_item`: «колу за человека»). Карточку товара, уже положенного столько раз,
  сколько просили, тоже не открывает (`_done_item`). В корзине править позицию можно.
- **Товар кликом по своей карточке** («Морс от 99 ₽» в шторке кладёт сразу, без «В корзину»).
  Если после клика по карточке с ценой корзина выросла, это добавление (`cart_watch` →
  `_note_cart`), и план идёт дальше. Корзина та же — это было открытие окна товара.
- «Больше ничего» не считается новым словом человека для повтора «В корзину»: повтор после него
  сначала возвращается модели.
- Тексты: «…Если нет — «нет», открою корзину и перейду к оформлению». Правила промпта: «ещё?» —
  когда позиции плана в корзине; после «нет» — корзина и оформление.

Тесты:
- «план»: сквозной путь. Пицца → сразу «что-нибудь ещё?» (модель не звалась) → «добавь
  какой-нибудь напиток» → «Напитки» открыты кодом без вопроса о разделах, показаны → «колу» → в
  промпте ✓ пицца и ▶ кола; повторное открытие пиццы не выполнено → «что-нибудь ещё?» с обеими →
  «нет» → ▶ корзина → шторка корзины → ▶ оформление.
- Карточка колы, пока выбирают напиток; «да» → снова разделы; «всё, оформляй» → больше не спрашивать;
  вторая просьба после законченного выбора; карточка, положившая товар (корзина выросла) и открывшая
  окно (не выросла).
- Старые тесты: после добавления теперь сначала «что-нибудь ещё?», потом «нет» (эффект клика,
  повтор «В корзину», C3/C4, 15:49). Смысл проверок тот же, порядок — новый.
- `test_cc_resolve` — подстановка `_done_item`.
Без `_plan_step` и `_done_item` новые проверки падают.


`test_task_agent` — 464, 25 наборов — 0 FAIL.

### Агент задач: раздел на одностраничном меню — не показывать чужие позиции (2026-09-30)

Живой прогон 22:49. На «добавь напиток» код открыл «Напитки» и показал «В разделе «Напитки»:
Айс-ти, Чипси-пицца, Чикен бомбони…».
- На Додо раздел — место на одной странице: ссылка лишь прокручивает к нему. Выше по странице стоят все
  прошлые разделы.
- Подписи для модели (до 60) и список кодом (до 10) брались в порядке страницы, поэтому первыми шли
  пиццы. Текст модели не прошёл проверку, и код выдал этот список за раздел.
- Были ли напитки в снимке вообще, по логам не установить: снимки не сохраняются, а браузер
  пользователя по правилу задачи не трогаю.

Исправлено в `_browse_show` для раздела, открытого без смены адреса:
- видимые после прокрутки элементы идут первыми и помечены «(on screen)»;
- модели: только позиции, которые по названию относятся к разделу, иначе NONE;
- список кодом (текст модели не прошёл проверку или модели нет) — только из видимого;
- NONE или видимых позиций меньше двух — `_browse_handoff`. Показ отдаётся модели шагов: докрутить
  или найти, спросить со списком. Вид («напиток») остаётся в плане (`keep_kind`), поэтому карточку
  за человека модель не откроет (`_early_item`). Когда человек называет позицию, вид снимается, и
  следующий вид снова идёт через раздел.
- Раздел на отдельной странице (адрес сменился) и снимок без флагов видимости — как раньше.

Тесты «живой 22:49»:
- показ не со страницы и отсутствие модели — список только из видимого (напитки, без пицц и «Айс-ти»
  сверху страницы);
- порядок и пометка видимого в запросе к модели;
- NONE — модели «докрути и спроси», карточка колы не открыта; «колу» — вид выбран, карточка открыта.

На версии из `refs/backup/task-agent-plan` первые два теста падают.


`test_task_agent` — 469, 25 наборов — 0 FAIL.

#### Дополнение: раздел по разметке страницы (живая вкладка Додо, только чтение)

С разрешения пользователя вкладку Додо (CDP :9222) прочитали два раза через `Runtime.evaluate` кодом
только на чтение: без меток, кликов и прокрутки. Остальные вкладки не открывались, из них взят только
домен. Что оказалось в DOM:
- **Всё меню на одной странице:** 165 карточек-`article` с ценой. Разделы — отдельные блоки со своим
  заголовком `h2`: «Пиццы», «Комбо», «Римские пиццы», «Закуски», «Кофе и чай», «Напитки», «Десерты»,
  «Соусы».
- **Ссылки разделов** — закреплённая панель `<a>` без `href`.
- **В общий снимок агента раздел не влезает:** бюджет 100, сначала видимое.
- **«Видимое после прокрутки» тоже неверно.** Вкладка стояла так, что заголовок «Напитки» был в центре
  экрана (434 из 860 px). Так его ставит `snapshot_for_goal` (`scrollIntoView({block:'center'})`).
  На экране при этом 6 позиций «Кофе и чай» и один «Айс-ти», и кофе показался бы напитками.

Добавлено:
- **`browser_actions.section_items` / `_SECTION_ITEMS_JS`** — позиции раздела по разметке, только чтение.
  Заголовок с текстом раздела ищется вне ссылок, навигации и закреплённых панелей. Блок раздела — самый
  широкий предок заголовка без другого заголовка того же уровня. Позиции — кликабельные элементы блока
  с ценой и словом: внешний из вложенных, без дублей.
  На живой вкладке нашлось: «Напитки» — 21 позиция (Айс-ти, коктейли, Добрый Кола… морс), «Пиццы» —
  35, «Кофе и чай» — 10, «Соусы» — 6.
- **`_browse_show`:** позиции по разметке (≥2) — единственный источник показа: модели передаются только
  они, и список кодом строится из них. `snapshot_for_goal`, который прокручивает страницу, в этом
  случае не вызывается. Без разметки работает прежняя логика: видимое первым, NONE, передача модели.

Тесты:
- `test_task_agent`: раздел по разметке — показаны кола и морс, не кофе на экране и не пиццы.
  `section_items` в тестах заглушена глобально, браузер не трогается.
- `test_ba_sandbox` (свой headless Chromium): только карточки «Напитков» без дублей; заголовок в обёртке;
  нет заголовка — found=false; страница не прокручена и не размечена.


`test_task_agent` — 470, `test_ba_sandbox` — 34, 25 наборов — 0 FAIL.

### Агент задач: ответ на «где заказать?» — вариант только по отличительному слову (2026-09-30)

Живой прогон 23:35. На вопрос «Где заказать пиццу? Вот результаты поиска: - Dodo Pizza — dodopizza.ru …
- Пицца Синица — pizzasinizza.ru …» человек
ответил «додо пицца», а открылся pizzasinizza.ru.

`_site_pick` брал вариант из `_chosen_option`, в том числе неточный: «единственный вариант с наибольшим
числом общих слов». С «Dodo Pizza» (латиница) общих слов не было, с «Пицца Синица» — одно общее
«пицца». Выбранной стала Синица. Модели, которая пошла на Додо, код ответил «NOT opened — выбран
pizzasinizza.ru», и она открыла Синицу. Пара «додо пицца = pizzasinizza.ru» попала в словарь названий.
В память задач она ещё не записалась: файл пуст, прогон жив. Запишется, если прогон кончится, в том
числе по «стоп». Поэтому пользователю: перезапустить бэкенд, а не отменять задачу.

Исправлено:
- **`_site_pick`:**
  - неточный выбор по общим словам не засчитывается;
  - точный выбор по названию требует отличительного слова: ответ «пицца» ⊂ «Пицца Синица» — не выбор;
  - номер и «да» — как раньше;
  - иначе — единственный вариант, который ответ называет отличительным словом (`_site_option_fits`):
    как есть, транслитом («додо» — «Dodo») или в имени хоста (`_name_fits_host`), с допуском одной
    опечатки в слове от 4 букв (`_one_edit`: «доодо» — «dodo»);
  - общие слова названий (`_NAME_GENERIC`: «пицца») и служебные слова ответа не считаются.
- **Словарь названий** пополняется только из такого выбора (`_bind_name` на тот же `pick`).

Тесты «живой 23:35»:
- «додо пицца», «Доодо пицца», «додо» — dodopizza.ru; «синица», «папа джонс», «2» — свои;
- «пицца», название города, «888 пицца» — не выбор;
- путь «поиск → где? → додо пицца»: Додо открыт, в словаре «додо пицца» = dodopizza.ru; модель,
  открывающая Синицу, получает отказ.

На прошлой версии все четыре проверки падают.


`test_task_agent` — 474, 25 наборов — 0 FAIL.

### Агент задач: открытие сайта названием — адрес резолвом, не название (2026-10-01)

Живой прогон 23:52. Модель спросила «где заказать?» без вариантов, человек ответил «додо пицца», и модель
открыла сайт названием: open «додо пицца», а не адресом. В `_site_which` хостом считалось то, что
`urlsplit` вернул из «https://додо пицца», то есть само название. Отсюда три симптома:
- вопрос «„Додо Пицца“ — это додо пицца? Заказываем здесь?»;
- «да» записало название выбранным сайтом (`site_ok`), и на открытом dodopizza.ru выбор разошёлся
  с адресом — разделы не спрашивались, меню перечисляла модель;
- на первой кнопке — «Сейчас открыт dodopizza.ru, а выбран додо пицца. Заказываем здесь?».

Правка 23:35 (`_site_pick`) тут ни при чём: на вопросе без вариантов она ничего не выбирает, и до неё было
так же. Ошибка — в проверке «это X?» из прогона 20:18. Она проявилась, когда модель открыла сайт не по адресу.

Исправлено: в `_site_which` у заказа название без точки превращается в адрес тем же резолвом, которым его
откроет `_do_open` (`cc.resolve(target, web_search=False)`: алиас конфига, история браузера). Дальше —
обычные проверки выбора по адресу. «додо пицца» → dodopizza.ru сверяется с названием из ответа
(`_name_fits_host`), запоминается и открывается без вопроса. Название, которое резолв ведёт на другой адрес,
вызывает вопрос «это X?» уже с настоящим адресом. Неизвестное название пропускается в `_do_open`, а тот
отвечает модели «сначала поиск».

Тесты «живой 23:52» ×3:
- открытие названием: без «это додо пицца?», открыт dodopizza.ru, сразу разделы;
- dodopizza.ru — выбранный сайт, в словаре «додо пицца» = dodopizza.ru;
- название, ведущее на другой адрес: «„Якитория“ — это rolls.test?», не открыт.

На прошлой версии все три падают.

### Агент задач: разделы магазина — запас по разметке, когда модель их не назвала (2026-10-01)

Живой прогон 00:16: после открытия dodopizza.ru бот не спросил о разделах, а сразу показал обзор всей
страницы («больше 10 позиций — всего около 25 пицц…»).

Разбор с разрешения пользователя — разовое чтение вкладки Додо на :9222. Снимок бота запускался в копии, из
которой вырезаны все записи в DOM (`set/removeAttribute` меток `data-vpc-*`); без кликов и прокрутки.
- Код шага «разделы» (`_browse_sections`) не менялся с 22:49, когда он сработал на Додо (сверено по
  бэкапам).
- В списке, который видит модель разделов, ссылки «Пиццы, Комбо, Римские пиццы, Закуски, Кофе и чай,
  Напитки» стоят на местах 1–8 из 60.
- Значит, в этот раз не набралось двух разделов из ответа модели: NONE или ответ не списком. Сам ответ
  нигде не записывался. По правилу 20:18 («разделов нет — показать страницу») код показал обзор.

Исправлено:
- **`browser_actions.section_names` / `_SECTION_NAMES_JS`** (только чтение): заголовки страницы, в блоке
  которых (как в `_SECTION_ITEMS_JS`) не меньше двух кликабельных позиций с ценой. Ссылки, навигация и
  закреплённые панели — не заголовки. На вкладке Додо — 7 мс: «Пиццы, Комбо, Римские пиццы, Закуски,
  Кофе и чай, Напитки, Завтраки, Десерты, …».
- **`_browse_sections`:** если из ответа модели меньше двух разделов, берутся такие заголовки, у которых на
  странице есть та же подпись: её и нажмёт `_browse_open`. Обзор страницы остаётся на случай, когда нет и
  их.
- Ответ модели, из которого разделов не набралось, пишется в лог (INFO, до 200 символов, через
  `redact_inline`).

На данных вкладки (снимок и заголовки), при ответе модели NONE: «На dodopizza.ru есть разделы: Пиццы,
Комбо, Римские пиццы, Закуски, Кофе и чай, Напитки. Что посмотрим?».

Тесты:
- «живой 00:16» ×2 (ответ NONE; ответ не списком) — разделы по разметке, только те, что есть на странице.
  На прошлой версии оба падают.
- `test_ba_sandbox`: названия разделов на синтетической странице — «Десерты» с одной позицией и панель
  ссылок не попадают, страница не прокручена и не размечена.

### Веб-чаты: один постоянный чат на канал вместо свежего на каждый вызов (2026-10-01)

Аккаунт deepseek пользователя заблокировали на 3 дня. Причина: каналы без памяти (`_STATELESS_CHANNELS`:
cc, cc_gen, burst, probe, search, inline) открывали свежий чат на каждый вызов. Агент задач и разбор
команд ходят через «cc», так что каждый шаг агента был новым чатом. Счётчик `deepseek#cc` в
`web_llm_state.json`: 60 вызовов за ~50 минут, у main и side при этом по одному постоянному чату.

Исправлено (`web_llm.py`): свежий тред на каждый вызов остался только там, где это обычное поведение, —
на поисковике (`_FRESH_THREAD_SITES = {"google"}`: AI Mode, новый вопрос — новый поиск). На чат-сайтах
(deepseek, qwen, claude, zai, chatgpt, kimi) у каждого канала один постоянный чат, как у main:
- адрес запоминается под ключом `сайт#канал`;
- новый чат открывается, только если сохранённый недоступен: удалён, переполнен («length limit»), не
  принял ввод.

Сброс адресов при очистке истории персоны (`clear_chat_urls`) покрывает и эти ключи.

Компромисс: в треде канала копятся прошлые промпты агента. Промпты самодостаточны, но модель видит
историю; раньше её избегали намеренно.

Тесты:
- `test_web_llm` 11a: cc на чат-сайте не stateless, на google — stateless; cc на deepseek: второй вызов
  идёт в сохранённый чат без перехода на home, адрес запомнен. Без правки обе проверки падают.
- `test_bg_isolation` 1: burst на deepseek — постоянный чат, на google — stateless. Прежняя проверка
  «burst stateless» описывала отменённое требование.

### Агент задач: находки теста локальных моделей gemma (2026-10-01)

Сценарии агента с gemma3:4b и gemma4:e2b на подделке браузера из снимка вкладки Додо (без браузеров
пользователя и бота) выявили две ошибки кода:

- **`_picked_section`:** ответ «Пиццы» при разделах «Пиццы» и «Римские пиццы» (у Додо оба) не выбирал
  раздел, потому что подходили оба. Раздел не открывался, дальше шла модель шагов, а на одностраничном
  меню она кликала «Пиццы» по кругу. От модели это не зависит, с DeepSeek было бы так же. Теперь из
  нескольких подходящих берётся раздел, названный целиком.
- **`_update_brief`:** gemma3 записала позицией слово цели («закажи пиццу» → позиция «пицца»). Бриф
  считал товар названным, и вопрос о разделах пропадал. Правило промпта «позиция — конкретный товар»
  теперь дублирует код: позиция из одних родовых слов (`_NAME_GENERIC`: «пицца», «суши», «роллы»…)
  переносится в виды (`kinds`).

Тесты «gemma 01.10» ×3; на прошлой версии все три падают.

Итоги теста моделей. Вызов — как у бота: num_ctx 8192, think false, keep_alive 2m. Модели по одной;
предохранитель памяти — стоп ниже 15% свободной.
- **gemma3:4b:** разбор ответов — поля верные, но с шумом: позиции копируются из текста вопроса, размер
  «...». После правок вопрос о разделах и показ раздела работают, их ведёт код. Модель шагов не
  справляется: на каждый ход сначала эхо текста вместо JSON, потом снова «Что заказываем?» при уже
  данном ответе; агент останавливается «хожу по кругу». Память: свободно 70% → 18%.
- **gemma4:e2b:** разбор ответов чище (без лишних полей, 5 из 6), разделы называет сама верно. Но в
  памяти помещается на грани: из 4 запусков в 3 первый же вызов опускал свободную память до 11–13%, и
  предохранитель останавливал прогон. В единственном полном прогоне (до правок) упёрлась в ошибку
  `_picked_section` (см. выше), после правки сценарий заказа целиком пройти не удалось.

### Веб-чаты: duck.ai как провайдер, очередь мелких задач duck.ai/Google (2026-10-01)

Запрос пользователя после бана deepseek:
- duck.ai — вариантом веб-LLM;
- мелкие задачи бота — по очереди с Google AI Mode;
- агент режима управления (task) — через duck.ai, если промпт не длиннее 16 тыс. символов.

Сделано:
- **Адаптер `duckai`** (`web_llm.ADAPTERS`):
  - модель Gemma 4 31B (`_DUCKAI_MODE_JS`: строка меню `model-picker-row-<id>`, заодно выключает
    веб-поиск);
  - ответ — тело сообщения `div[id*='-assistant-message-']:not([id^='heading-']) > div:has(.space-y-4)`;
  - маркер завершения — кнопка копирования;
  - `max_input` 16000 с `max_input_strict`: длиннее — вызов сразу `None`, роутер идёт к следующему
    провайдеру;
  - «Сервис Duck.ai временно недоступен» (анти-бот 418) — в `_CHAT_ERROR_RES` и `_OVERLOAD_RE`, то есть
    карантин сайта, а не сброс чата и повтор;
  - адрес у чата один (duck.ai/) — постоянный чат держит вкладка.
- **Enter фоновой вкладки.** `_raw_enter` слал `rawKeyDown` без символа, а duck.ai отправляет только по
  `keyDown` с «\r». Бот уходил в кнопку после неудачного Enter, и на втором вызове пул H завис. Флаг
  адаптера `enter_text`, через `chat_fill_send(..., enter_text=True)`, включён только у duck.ai; прочим
  сайтам вызов прежний. Безопасный замер в своём headless (запросы чата заблокированы route→abort):
  rawKeyDown отправку не запускает, keyDown с «\r» — запускает.
- **Очередь `bg_site: rotate`** (`local_router`): `llm.local_tasks.rotate: {сайт: сколько подряд}` —
  сайты по очереди, следующий в очереди — запасной. Очередь сдвигает только настоящий вызов, не снимок
  для настроек. Без списка — как по умолчанию.
- **Коннор:**
  - `cc_provider: webchat:duckai`;
  - `local_tasks: bg_site: rotate, rotate: {duckai: 1, google: 1}`;
  - очередь — только для фоновых задач (дневник, состояние, мир, досье, отношения…). Задачи на пути
    ответа (intent_router, help_detect, query_rewrite и т.п.) по решению пользователя остались на
    локальной Ollama: веб-чат отвечает 15–25 с против ~1 с.
  - Ollama — и запасной для фоновых (оба сайта молчат), и OCR, и `primary: local` Коннора.
- **`_pid_alive`: зомби — не жив.** Завершённый Chrome пула H, которого бэкенд ещё не забрал, держал
  `SingletonLock` «живым» pid. Профиль считался занятым, и браузер не поднимался до перезапуска бота.
  Проявилось после ручного завершения зависшего пула H; протухший лок снят вручную.

Живая проверка через код бота (пул H, своя вкладка, 3 сообщения): классификатор «YES» за 24 с и «NO»
за 17 с в том же чате; настоящий промпт агента (разделы) разобран верно за 21 с; пул H отвечает.

Тесты:
- `test_web_llm` +4: duck.ai — постоянный чат и модель; промпт > 16 тыс. → `None` без вкладки;
  «временно недоступен» → карантин; `enter_text` только у duck.ai.
- `test_local_tasks` +6: очередь 2:1, снимок не сдвигает, запасной, мусор, rotate без списка.
- `test_ba_sandbox` +3: JS модели (идемпотентно), селектор ответа и hasDone, Enter с символом на keypress.
- `test_browser_actions` +1: зомби.


### Скины: запись от имени владельца — только по жесту, режим управления из скина закрыт (2026-10-04)

Находка разбора готовности к релизу. С 911443c скин стал писать в чат. Скрипт скина мог сам, без
человека, вызвать `vpc.send(...)`, а собеседник в вебе всегда владелец (`web_single_user`). Отсюда
команды режима управления и «да» на подтверждение из скина, у connor ещё и без вопроса
(`confirm: false`). Через `toggle-feature` скин мог включить `computer_control`, а действия записи
(стереть факты, урезать историю, сменить модель) шли без участия человека.

- **Бэкенд.** `ChatRequest.from_skin`. Оба эндпоинта чата передают его в `process_message`, а
  `cc_turn_enter` («стоп», «ещё работаю») для скина не вызывается. В `process_message` выставляется
  флаг потока хода `_SKIN_TURN`, его читает `_cc_allowed` — единая точка авторизации режима управления.
  В таком ходе нет ни команд, ни подтверждений, ни маркеров LLM. Вложенный вызов флаг не снимает,
  фоновые потоки его не видят. Права самого пользователя — `_cc_user_allowed`.
- **Подсказка.** Если режим управления включён или пришло «перейди в режим управления», владелец
  получает `cc_texts.skin_no_control`: «нажми „Обычный вид“ над скином». Без подсказки персона
  «открывала» бы сайт на словах.
- **Хост (`SkinFrame`).** `send`, `clear`, `action`, `set-setting` принимаются только при свежей
  активации окна (`navigator.userActivation.isActive`) и фокусе на iframe скина. Клик внутри iframe
  активирует и родителя, а клик по самому приложению фокус с iframe снимает. Поверх стоит общий бакет
  записей: 5 подряд, дальше одна в секунду. Отказ `send` — `send-result` с reason `gesture` или `rate`.
- **`toggle-feature computer_control`** из скина — отказ (`skin.actNoControl`).
- **Отправка из скина** помечена: `submitMessage(..., fromSkin)` → `streamChat({fromSkin})` →
  `from_skin: true`.
- **Контракт.** В правилах песочницы — запись только по жесту, лимит и что команды режима управления
  из скина не исполняются.
- **Скины удалены по просьбе пользователя.** Встроенный «Лесная роща» (`presets/sylvan-grove.html`,
  `PRESETS` пуст, механизм встроенных остался) и оба скина библиотеки в `data/skins`. Скрытые
  встроенные, которых в приложении больше нет, в счётчик «Восстановить встроенные» не входят.

Проверка в headless Chromium на стенде с настоящим `SkinFrame`, скин с автоотправкой на загрузке и
по таймеру:
- без действий человека и после клика по приложению — 0 записей;
- клик в скине — отправка проходит;
- пачка из 20 удалений по одному клику — принято 5;
- Enter в поле ввода скина — отправка проходит.

Не закрыто: за несколько секунд после настоящего клика в скине тот же скин может приложить к нему
свои записи (не больше бакета). Safari и Firefox вживую не проверялись.

Тесты: `scripts/test_skin_turn.py` (25); `test_skin_gen_api` — `strip_base` вида «skin» на шаблоне
чата вместо удалённого пресета.

### Этап 3 плана проверок: дела и инвентарь, персоны, сроки, мелочи, промпты (2026-10-04)

Починка открытых пунктов этапа 3 `docs/test-plan.md` (№5, 10, 13, 15, 16, 17, 19, 20) и трёх дефектов
промптов. Четыре агента в отдельных worktree, затем два ревью и доработки по их находкам.

- **№10 «да» на переспрос бота.** `app/features/list_offers.py`: отложенный вопрос чата («Записать /
  Добавить / Отметить / Убрать?») привязан к спросившему, срок 600 с. Действует только на его следующий
  ход: забирается в начале `_process_message_impl`, до ранних возвратов. «Да» выполняет действие тем же
  API, что маркеры, без LLM. «Нет» — короткий ответ. Посторонняя реплика или отказ с хвостом снимают
  вопрос молча и идут обычным путём. Не перехватывается reply на другое сообщение бота и «да» после
  вопроса обучения или инициативы. Вопрос задаётся только на явную просьбу: без локальной модели
  эвристика раньше спрашивала на «как сделать…». Тексты ru/en.
- **№15 маркеры.** Разбираются все вхождения всех типов. TODO_DONE — только чистый список номеров или
  диапазон, иначе поиск пункта по тексту; удаление по убыванию со сверкой текста. Номера сверяются со
  списком, который видела модель (`todo_seen` при сборке промпта): пункт, удалённый или добавленный за
  время генерации, не сдвигает вычёркивание на чужой. Пустой инвентарь тоже даёт инструкцию маркеров —
  там же, где маркеры разбираются.
- **№19 «запомни сценарий» вне режима управления** правилом `Rule:` не становится. Допущенному к режиму
  управления — подсказка `cc_texts.scenario_outside_mode`. Обычные «запиши сценарий ролика…» идут как
  раньше.
- **№13 YAML на живую.** `save_persona_yaml` применяет то же, что форма: computer_control с allowed_users,
  proactive, rhythm, life, system_prompt. `restart_required` — для ключей, которые бот читает только при
  создании. Для computer_control и owner при Telegram-токене персоны рестарт тоже нужен: Telegram — отдельный
  процесс.
- **№16 память удалённой персоны.** Создание, смена id и копия в id с оставшейся памятью отвечают 409
  `memory_exists`. Выбор `keep` подхватывает память. Выбор `fresh` переносит её в
  `<папка>.archived-<время>`, ничего не удаляя. Перед переносом закрываются базы Chroma этих папок
  (`chroma_space.release_clients_under`), иначе процесс читал старую память из кеша. Служебные id
  (tg, default, skins…) и префикс `api_` запрещены: Telegram-папка персоны `api_X` — это веб-память X. В вебе — `choiceDialog`, при «с чистого листа» забываются локальные
  данные браузера. Flavor-банк при создании — в контексте `api_<id>`.
- **№5 срок «да».** Подтверждение режима управления и список сайтов — 300 с, как обещает гайд. Клавиши
  (Enter, Tab, «отправь» — `KEY_ACTION_KINDS`) — 60 с: клавиша уходит в то, что в фокусе, а страница за
  это время могла смениться.
- **№17** `/erase` признаёт владельца из YAML (`_is_owner`). **№20:** `/files` в вебе. Telegram-форматтер
  разбирает выделение одним проходом (`__x__` → `<u>`, `==x==` → `<b>`, вложенность тегов всегда верная,
  только теги, которые принимает Telegram). Переспрос «Записать…?» досылается и после фото/документа
  (общий `_send_list_messages`), а не остаётся взведённым невидимым. Молчание в
  самоинициативе и в досье скина — по настоящим данным (`silenceProgress`), иначе «—».
- **Промпты.** Блок WRONG в извлечении фактов без висящих кавычек. Удалены мёртвые
  `relationship.extract_moments` и `world_engine.detect_from_dialogue`: те же данные пишет
  `_harvest_dialogue` одним вызовом. Защита primitive перенесена в `add_detected`. Снята мёртвая
  локальная задача `relationship`.

Тесты: `test_bot_markers` (175), `test_persona_lifecycle` (94), `test_prompt_defects` (39), дополнены
test_cc_confirm_policy, test_cc_gate, test_computer_control, test_memory_core (форматтер, 9380 смесей),
test_misc_features, test_local_tasks, test_intellect.

Не проверено вживую: Telegram (модуля `telegram` в окружении тестов нет), веб в браузере, бот с настоящей
LLM.
