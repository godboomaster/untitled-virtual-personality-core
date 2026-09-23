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

### №26. Иконки без семантики в DOM и ложный «офлайн» (кейс 22.09, school.example.com)
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
