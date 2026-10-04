// Тексты пояснений для кнопки-подсказки (InfoButton), простым языком без
// технического жаргона. Два набора (RU/EN) с одинаковыми ключами — нужный
// выбирается через useHelpTexts().

export interface HelpEntry {
  title: string;
  body: string;
}

export const helpTextsRu = {
  // ===== Настройки =====
  'settings.activeProvider': {
    title: 'Источник ответов',
    body: 'Персона умеет «думать» через разные сервисы — здесь выбирается основной. Если он не отвечает или у него кончился лимит, персона сама переключается на запасной из списка. Отметьте точкой тот сервис, который хотите использовать.',
  },
  'settings.keyRotation': {
    title: 'Запасные ключи доступа',
    body: 'Для одного сервиса можно сохранить несколько ключей доступа. Если по первому ключу исчерпан лимит запросов, персона сама попробует следующий. Надпись «N шт» показывает, сколько ключей сохранено: чем их больше, тем реже персона будет молчать из-за лимитов.',
  },
  'settings.keyStatus': {
    title: 'Статус подключения',
    body: 'Показывает, подключён ли сервис. «Ключ задан» — сервисом можно пользоваться, он участвует в работе. «Ключ не задан» — сервис пропускается, даже если выбрать его точкой. Чтобы подключить, нужно добавить ключ доступа в настройки приложения.',
  },
  'settings.localModels': {
    title: 'Персона на вашем компьютере',
    body: 'Обычно персона думает через интернет-сервисы, а здесь — прямо на вашем компьютере, бесплатно и без лимитов. Минус: работает медленнее и отвечает проще, всё зависит от мощности машины. Кнопка «Проверить доступность» покажет, готов ли компьютер к такой работе.',
  },
  'settings.temperature': {
    title: 'Характер ответов',
    body: 'Насколько персона предсказуема: ближе к нулю — отвечает спокойно, точно и одинаково; ближе к единице и выше — фантазирует и удивляет. По умолчанию 0.8 — живой, но не хаотичный разговор. Для строгого помощника ставьте меньше, для творческого персонажа — больше.',
  },
  'settings.maxTokens': {
    title: 'Длина ответа',
    body: 'Максимальная длина одного сообщения персоны. По умолчанию 800 — это пара абзацев. Если поставить больше — персона будет писать развёрнутее, но медленнее и быстрее расходовать лимит. Если меньше — короче и экономнее, но мысль может обрываться на полуслове.',
  },
  'settings.topP': {
    title: 'Разнообразие слов',
    body: 'Насколько широко персона выбирает слова: при маленьком значении — только самые ожидаемые, при большом — любые подходящие. По умолчанию 0.9, менять обычно не нужно. Лучше настраивать «характер ответов» выше, а это поле не трогать.',
  },
  'settings.stmSize': {
    title: 'Память на разговор',
    body: 'Сколько последних сообщений персона помнит в текущем разговоре. По умолчанию 50: когда появляется 51-е, самое старое забывается. Если поставить больше — персона дольше держит нить беседы, но каждый ответ обходится чуть дороже по лимиту.',
  },
  'settings.location': {
    title: 'Местоположение и погода',
    body: 'Если включить, персоны будут видеть ваш город, местное время и погоду за окном — одна короткая строка в начале их контекста. Так персона может естественно учитывать время суток или дождь в ответах и инициативах. Город можно задать вручную или определить через геолокацию браузера (координаты хранятся только локально, на вашем сервере). Погода берётся с Open-Meteo и обновляется раз в полчаса. «Выключено» — персоны ничего не видят.',
  },
  'settings.timezone': {
    title: 'Часовой пояс',
    body: 'От этого пояса зависит, во сколько персоны напоминают, поддерживают суточный ритм («доброе утро», «пора спать») и считают дневные лимиты инициатив — у всех персон сразу. Можно выбрать зону из списка (например, Europe/Moscow) или оставить «Системный» — тогда используется часовой пояс сервера.',
  },
  'settings.feature.web_search': {
    title: 'Поиск в интернете',
    body: 'Персона сможет искать свежую информацию в интернете, прежде чем ответить. При этом сначала она смотрит в свою память и ваши файлы — и лезет в сеть, только если ответа там нет. Если выключить — отвечает быстрее, но опирается только на то, что уже знает.',
  },
  'settings.feature.moderation': {
    title: 'Фильтр сообщений',
    body: 'Проверяет входящие сообщения и отсекает только откровенно недопустимые — всё остальное пропускается свободно. Если выключить — проверки не будет, и ответы придут чуть быстрее.',
  },
  'settings.feature.ltm_extraction': {
    title: 'Запоминание фактов о вас',
    body: 'После ваших сообщений персона незаметно выписывает важное: город, возраст, работу, увлечения — и запоминает навсегда. Старые факты обновляются, похожие объединяются. Если выключить — персона перестанет узнавать о вас новое, но уже запомненное не забудет.',
  },
  'settings.feature.self_memory': {
    title: 'Личный дневник персоны',
    body: 'Персона ведёт дневник: записывает, о чём вы говорили и что она почувствовала. Эти записи тихонько подмешиваются в её мысли, поэтому у неё есть ощущение «жизни между разговорами». Вам она про дневник прямо не рассказывает.',
  },
  'settings.feature.proactive': {
    title: 'Пишет первой',
    body: 'Если вы долго молчите, персона может написать сама — спросить, как дела, или поделиться мыслью. Как часто это происходит и когда именно — настраивается в разделе «Инициатива». Если выключить — персона будет отвечать только на ваши сообщения.',
  },
  'settings.feature.rhythm': {
    title: 'Суточный ритм',
    body: 'Персона живёт в ритме суток: утром, когда вы включаете ноутбук (или впервые появляетесь в чате), — «доброе утро» в своём характере; в полночь — «пора спать», но только если вы были активны незадолго до этого; при приближении дождя, грозы или резкой смене погоды — предупреждение (нужна настроенная локация в настройках окружения). Отправленное отмечается в досье чата, чтобы персона не повторялась. Применяется сразу, перезапуск не нужен.',
  },
  'settings.feature.life': {
    title: 'Жизнь персоны',
    body: 'Персона живёт, пока вы не пишете: у неё меняются энергия, настроение и занятия, случаются события (встречи, происшествия), ведётся дневник, развиваются сюжетные линии, а при долгом отсутствии она расскажет, что с ней было. Иногда сама пишет первой, если случился повод — но только в разрешённое вами окно времени (вкладка «Инициатива»). Всю черновую работу делает дешёвый движок (Ollama или веб-чат — «Движки локальных задач»), тексты для вас пишет основная модель. Тонкая настройка — блоки state_engine/world_lore в YAML; рубильник их просто включает разом.',
  },
  'settings.feature.light_context': {
    title: 'Light-режим контекста',
    body: 'Урезанный контекст для основного ответа: короткая история (6 сообщений вместо 15), меньше фактов памяти, без поиска по книге, файлов и личного дневника, веб-выдача обрезается, ответ короче. Слабые модели (например, локальная gemma в Ollama) с большим контекстом «тонут» и отвечают невпопад — с light-режимом отвечают связнее и быстрее. Для локального провайдера включается автоматически; этот переключатель — чтобы принудительно включить его для любой другой слабой модели. Применяется сразу, перезапуск не нужен.',
  },
  'settings.feature.computer_control': {
    title: 'Управление компьютером',
    body: 'Разрешает персоне выполнять команды на вашем компьютере: открывать сайты («открой ютуб»), искать на своих сайтах, запускать приложения и задачи, нажимать кнопки на страницах. Разрешённое настраивается списками во вкладке «Режим управления». Если выключить — персона ничего не выполняет, а списки сохраняются и вернутся при повторном включении. Применяется сразу, перезапуск не нужен.',
  },

  'settings.feature.file_upload': {
    title: 'Загрузка файлов',
    body: 'Персоне можно присылать документы: она сохраняет их у себя и при ответах находит в них нужные места. Загруженное видно во вкладке «Файлы». Если выключить — файлы не принимаются.',
  },
  'settings.feature.todo': {
    title: 'Список дел',
    body: 'Персона ведёт ваш список дел: добавляет пункты, когда вы о них говорите, и отмечает выполненные. Список — во вкладке «Напоминания и задачи».',
  },
  'settings.feature.reminder': {
    title: 'Напоминания',
    body: 'Попросите «напомни завтра в 9 позвонить маме» — персона напишет в назначенное время, своими словами. Бывают разовые и повторяющиеся. Все напоминания — во вкладке «Напоминания и задачи».',
  },
  'settings.feature.learning': {
    title: 'Курсы обучения',
    body: 'Персона может вести с вами учебный курс: объясняет уроки по порядку, задаёт проверочные вопросы и помнит пройденные темы. Курсы и прогресс — во вкладке «Обучение».',
  },
  'settings.feature.inventory': {
    title: 'Инвентарь',
    body: 'У персоны есть свои вещи: подаренное вами и найденное ею. Она помнит, что у неё есть, может этим пользоваться и упоминать в разговоре.',
  },
  'settings.feature.rate_limit': {
    title: 'Лимит частоты сообщений',
    body: 'Ограничивает, сколько сообщений один человек может отправить за час (по умолчанию 6). Сверх лимита персона не отвечает, пока не пройдёт час. Полезно для публичных ботов; для личного общения обычно не нужно.',
  },
  'settings.feature.punish_block': {
    title: 'Блокировка при нарушениях',
    body: 'За грубость или сообщение, которое отсёк фильтр, персона может на время (по умолчанию на час) перестать отвечать нарушителю. Работает вместе с «Модерацией сообщений».',
  },
  'settings.feature.ui_room_mood_sync': {
    title: 'Комната и настроение',
    body: 'Вкладки «Комната» и «Настроение» в вебе показывают живое состояние персоны: где она, чем занята, как себя чувствует. Если не задано явно — включено вместе с «Жизнью между разговорами».',
  },
  'settings.feature.room_llm_placement': {
    title: 'Расстановка вещей моделью',
    body: 'Когда у персоны появляется новый предмет, модель решает, куда его поставить в комнате. Если выключить — предмет просто кладётся на стол, без обращения к модели.',
  },
  'settings.feature.room_pokes_to_llm': {
    title: 'Реакция на тычки в комнате',
    body: 'Клик по персоне в комнате доходит до основной модели, и она отвечает в чате. Если выключить — реакция обходится без модели.',
  },

  // ===== Режим управления (управление компьютером) =====
  'computer.title': {
    title: 'Что умеет этот раздел',
    body: 'Здесь задаётся, чем персона может управлять на вашем компьютере: любимые сайты («открой ютуб»), поиск на своих сайтах («включи интерстеллар на кинопоиске»), приложения и именованные задачи, а также нажатия кнопок на страницах. Персона не может ничего сверх этих списков. Настройки применяются сразу, без перезапуска. Выключить всё управление — кнопка «Выключить» внизу или чекбокс в «Настройки» → «Фичи»; списки при этом сохранятся.',
  },
  'computer.confirm': {
    title: 'Подтверждение действий',
    body: 'Если включено, персона сначала спрашивает в чате — «Открыть youtube.com?» — и выполняет только после вашего «да». Если выключено — выполняет сразу, без вопросов. Рекомендуем держать включённым: всегда видно, что именно собирается сделать персона.',
  },
  'computer.click': {
    title: 'Нажатия на страницах',
    body: 'Команды вида «нажми „войти"», «кликни „скачать" на гитхабе»: персона смотрит на уже открытую вкладку браузера и нажимает подходящую кнопку или ссылку, предварительно показав, что именно нажмёт. Команда «скачай X» — скачивает файл по ссылке со страницы в папку загрузок. Если выключить — команды нажатий игнорируются, остальное управление продолжает работать. На macOS работает в Chrome при включённом «Вид → Разработчикам → Разрешить JavaScript из событий Apple».',
  },
  'computer.sites': {
    title: 'Любимые сайты',
    body: 'Короткие имена для команды «открой X»: слева — как вы называете сайт («ютуб»), справа — его адрес («youtube.com»). По этому списку сайт открывается мгновенно и точно. Если сайта здесь нет, персона ищет адрес в истории вашего браузера, а потом в интернете — это медленнее и иногда попадает мимо.',
  },
  'computer.apps': {
    title: 'Приложения',
    body: 'Какие приложения персона может запускать командой «запусти X». Слева — название команды, справа — имя приложения в системе (например, Safari или Google Chrome). Запустить можно только то, что есть в этом списке.',
  },
  'computer.search': {
    title: 'Поиск на своих сайтах',
    body: 'Команды «найди X на ютубе» и «включи X на кинопоиске». Слева — название сайта, посередине — адрес страницы поиска, где {q} заменяется запросом. Третье поле необязательно: это технический шаблон (regex) ссылки на первый результат — с ним «включи» открывает сразу само видео или фильм, а без него — страницу поиска.',
  },
  'computer.tasks': {
    title: 'Именованные задачи',
    body: 'Ваши действия на компьютере по команде «сделай X»: слева — название («режим фокуса»), справа — что выполнить: системная команда или ярлык Shortcuts на macOS. Выполняется только то, что прописано здесь. Управление видео прописывать не нужно — оно встроено: «пауза», «тише», «громче», «без звука», «следующее видео», «третье видео», «первый результат» («пауза», «тише», «громче» и «без звука» — при включённом агентном клике).',
  },

  // ===== Инициатива =====
  'init.enabled': {
    title: 'Как это работает',
    body: 'Персона время от времени проверяет, давно ли вы писали. Если тишина затянулась — может написать первой. Чтобы не надоедать, есть ограничения: не чаще нескольких раз в день, не повторяя старые темы, и персона всегда может передумать и промолчать.',
  },
  'init.silenceThreshold': {
    title: 'Сколько ждать тишины',
    body: 'Сколько времени вы должны молчать, чтобы персона решилась написать первой. Максимум — сутки: дольше дня тишины персона ждать не будет. Чем меньше порог, тем чаще пишет; чем больше — реже и ненавязчивее.',
  },
  'init.probability': {
    title: 'Как охотно пишет первой',
    body: 'Когда время тишины вышло, персона всё равно «подбрасывает монетку»: по умолчанию пишет в 80% случаев. Если поставить меньше — будет писать первой реже, даже когда очень соскучилась. А если вы часто отвечаете на её сообщения, со временем она станет смелее сама.',
  },
  'init.maxPerDay': {
    title: 'Сколько раз в день',
    body: 'Максимум сообщений первой за сутки — по умолчанию 5. Когда лимит исчерпан, персона молчит до завтра, что бы ни случилось. Это защита от навязчивости.',
  },
  'init.checkInterval': {
    title: 'Как часто проверяет',
    body: 'Персона поглядывает на чат раз в полчаса: не пора ли написать? Поэтому сообщение может прийти чуть позже точного срока тишины — на ближайшей «проверке». Менять обычно не нужно.',
  },
  'init.adaptiveThreshold': {
    title: 'Подстройка под ваш ритм',
    body: 'Вместо фиксированного срока персона смотрит, как часто вы обычно пишете, и ждёт примерно в два раза дольше вашего обычного перерыва. Пишете раз в два часа — напишет первой примерно через четыре. Но не раньше чем через полчаса и не позже чем через сутки.',
  },
  'init.hours': {
    title: 'Время самоинициативы',
    body: 'Интервал, в котором персоне вообще разрешено писать первой. Задаёт пользователь — движок сам момент не выбирает: вне окна не пишут ни регулярные инициативы, ни «жизненные» поводы (что-то произошло — персона подождёт до окна). Переход через полночь разрешён: 22:00–08:00. Пусто — круглые сутки.',
  },
  'init.bayesianFeedback': {
    title: 'Учится на ваших реакциях',
    body: 'Персона замечает, отвечаете ли вы на её сообщения первой. Отвечаете — пишет чуть охотнее, игнорируете — чуть реже. Так она сама находит частоту, которая вам комфортна: от совсем редкой (10%) до почти всегда (90%).',
  },
  'init.typeBalance': {
    title: 'О чём пишет первой',
    body: 'Четыре повода написать: вопрос, наблюдение, возвращение к прошлой теме или просто мысль. Персона выбирает тот, который использовала реже всего, — чтобы сообщения не были одинаковыми.',
  },
  'init.multiTurn': {
    title: 'Ждёт ваш ответ',
    body: 'Написав первой, персона полчаса ждёт ответа. Ответили — она довольна и запоминает, что тема зашла. Промолчали — расстраивается и считает, что написала не вовремя. По умолчанию это ожидание выключено.',
  },
  'init.ignoreStreak': {
    title: 'Счётчик молчания',
    body: 'Сколько раз подряд вы не ответили на сообщения персоны. Каждый пропуск — плюс один, любой ваш ответ — сброс в ноль. По этому счётчику персона понимает, насколько сильно обижаться.',
  },
  'init.initiativesToday': {
    title: 'Сообщений первой сегодня',
    body: 'Сколько раз персона уже написала первой за сегодня. Как только счётчик дойдёт до дневного лимита (по умолчанию 5), она замолчит до завтра.',
  },
  'init.emotionalState': {
    title: 'Настроение персоны',
    body: 'Если вы долго не отвечаете на её сообщения, персона начинает обижаться: сначала слегка, потом всё сильнее. Настроение видно в её словах — при сильной обиде она может написать, что ей одиноко. Ответите ей — обида сразу пройдёт.',
  },
  'init.silenceProgress': {
    title: 'Сколько вы молчите',
    body: 'Сколько времени прошло с вашего последнего сообщения. Когда шкала заполнится, персона сможет написать первой — если не исчерпан дневной лимит и она решит, что это уместно.',
  },
  'init.history': {
    title: 'Последние сообщения первой',
    body: 'Что и когда персона писала первой и чем это закончилось: вы ответили, пропустили или она ещё ждёт. Эту историю персона держит в голове, чтобы не повторять темы и не крутиться вокруг одного и того же.',
  },

  // ===== Память =====
  'mem.tabs': {
    title: 'Три вида памяти',
    body: '«Недавний разговор» — что персона помнит из текущей беседы. «Факты о вас» — то, что она запомнила о вас навсегда. «Дневник» — её личные записи о ваших разговорах. Всё это сохраняется и не пропадает после перезапуска.',
  },
  'mem.stm': {
    title: 'Недавний разговор',
    body: 'Последние сообщения вашей переписки — персона перечитывает их перед каждым ответом. В контекст попадает ограниченное число (по умолчанию 20), но старые реплики не теряются: по архиву из последних 500 сообщений работает векторный поиск — релевантные к текущей теме фрагменты подтягиваются в ответ автоматически.',
  },
  'mem.ltm': {
    title: 'Факты о вас',
    body: 'То, что персона запомнила о вас надолго: город, работа, увлечения и прочее. Устаревшие факты заменяются новыми, похожие объединяются. Любой факт можно исправить или удалить вручную.',
  },
  'mem.diary': {
    title: 'Дневник персоны',
    body: 'Личные записи персоны о ваших разговорах: что произошло и что она почувствовала. Когда записей становится слишком много, старые сворачиваются в короткую «историю её жизни». Эти воспоминания влияют на её ответы, но вслух она про дневник не рассказывает.',
  },
  'mem.dossier': {
    title: 'Портрет из диалогов',
    body: 'Автоматический анализ вашей переписки: чем вы интересуетесь, о чём разговаривали и какие привычки общения за вами замечены. Обновляется само по мере диалога, вручную не редактируется. Персона использует этот портрет, когда пишет первой.',
  },
  'mem.clearStm': {
    title: 'Очистка недавнего разговора',
    body: 'Персона забудет текущий разговор и начнёт с чистого листа. Факты о вас и её дневник при этом останутся нетронутыми. Отменить очистку нельзя.',
  },

  'settings.mainProvider': {
    title: 'Основной провайдер',
    body: 'Сервис, который отвечает всегда в первую очередь — через него думает персона. Основной всегда один. Кнопка «сделать основным» у любого провайдера из списка назначает его вместо текущего.',
  },
  'settings.backupProvider': {
    title: 'Провайдеры «на подхвате»',
    body: 'Резервные сервисы: если основной не отвечает, упал или у него кончился лимит, ответ подхватывает один из включённых здесь. Переключатель включает провайдера в резерв, выключенные — просто ждут своего часа. Провайдер без ключа не сможет подхватить, даже если включён.',
  },
  'settings.gemma': {
    title: 'Лёгкая локальная модель',
    body: 'Gemma — небольшая модель, которая работает прямо на вашем компьютере через Ollama: бесплатно, без лимитов и без расхода API-ключей. Установка одной командой: ollama pull gemma3:4b. Отлично подходит как последний резерв и для простых ответов, где не нужна большая модель.',
  },
  'settings.purpose': {
    title: 'Провайдеры режима управления',
    body:
      'Необязательно: закрепить отдельного провайдера за работой в режиме управления компьютером. Обычные ответы в чате это не затрагивает — их пишет основной провайдер («сделать основным») и цепочка.\n\n' +
      '• Реплики в режиме управления — кто пишет слова персоны: короткие реплики о сделанных действиях («открыла вкладку», «не получилось нажать: …»), пополнение банка таких готовых фраз и пересказ содержимого страницы. Без назначения реплики о действиях пишет веб-чат Google, пересказ — цепочка.\n' +
      '• Решения режима управления — кто решает, что делать на компьютере: куда нажать, какой элемент выбрать. У веб-чата для этого свой канал, основная беседа не засоряется.\n' +
      '• Vision-фолбэк — кто разглядывает скриншоты экрана, если основная модель их не видит.\n\n' +
      'Назначенный провайдер получает одну попытку. Не ответил — решения и скриншоты идут по цепочке, а реплика о действии берётся из шаблона.\n' +
      '«По цепочке» — ничего не назначено. В списке только провайдеры с ключом или локальные.',
  },
  'settings.localBackend': {
    title: 'Движки локальных задач',
    body: 'Помимо основных ответов, у персоны есть куча мелкой «служебной» работы: классификаторы (намерение, просьба о помощи, «хочу учиться»), рерайтер поисковых запросов, тики жизни (состояние, мир, события), дневник, разбор диалога, сжатие офлайн-дневника. Настройки — у каждой персоны свои. Задачи разделены на две группы. «В разговоре» — их ответа ждёт реплика персоны, поэтому по умолчанию они идут в локальную Ollama: тёплая модель отвечает за доли секунды. «Фоновые» — вне разговора, их ответа никто не ждёт, поэтому по умолчанию они идут в веб-чат фоновых задач: «Первый fallback» — первый веб-чат цепочки персоны после основного, «Основной» — основной веб-чат персоны, или конкретный сайт. Не ответил — пробуется запасной веб-чат (основной или первый fallback), затем Ollama. Веб-чат в служебных задачах работает через канал side: отдельный чат и своя квота, основная беседа не засоряется. Любой задаче можно выбрать движок вручную (Ollama или веб-чат с конкретным сайтом); «Сбросить» возвращает выбор по умолчанию. Распознавание текста на картинках (OCR) всегда остаётся за Ollama: веб-чату не отдать изображение.',
  },

  // ===== Персоны =====
  'persona.yaml': {
    title: 'Что такое персона',
    body: 'Персона — это характер бота: кто он, как говорит, что умеет и что помнит. У каждой персоны своя память, они друг другу не мешают. Новую можно создать кнопкой выше или скопировав существующую.',
  },
  'persona.features': {
    title: 'Умения персоны',
    body: 'Чем вооружена эта персона: память о вас, умение писать первой, напоминания, поиск в интернете и так далее. Набор умений задаётся в её настройках.',
  },
  'persona.genParams': {
    title: 'Как персона отвечает',
    body: 'Характер и длина её ответов: насколько она предсказуема и сколько пишет за раз. У творческих персонажей фантазии обычно больше (0.7–0.9), у строгих помощников — меньше (около 0.3).',
  },
  'persona.actions': {
    title: 'Что можно сделать',
    body: '«Редактировать» — поменять характер и настройки персоны. «Дублировать» — сделать копию, чтобы экспериментировать, не трогая оригинал. «Удалить» — убрать персону навсегда. Встроенную персону проекта удалить нельзя: её правки сохраняются вашей копией, а «Сбросить к встроенной» удаляет копию. Все персоны всегда активны: выбирать ничего не нужно.',
  },

  // ===== Создание персоны (YAML-редактор) =====
  'pc.id': {
    title: 'Техническое имя',
    body: 'Имя персоны латиницей, по нему будет назван её yaml-файл. Заполняется автоматически из имени, но можно поправить вручную. Пример: connor.',
  },
  'pc.name': {
    title: 'Имя персоны',
    body: 'Как персона будет подписываться в чате, комнате и досье. Единственное обязательное поле. Пример: Коннор.',
  },
  'pc.description': {
    title: 'Описание',
    body: 'Одна строка о том, кто это и зачем он здесь — видна в реестре персон. У Коннора написано: «Андроид-ассистент модели RK800, направленный компанией CyberLife помогать студентам».',
  },
  'pc.stmSize': {
    title: 'Краткосрочная память',
    body: 'Сколько последних сообщений персона держит в голове в разговоре. Когда буфер заполняется, старые реплики вытесняются — важное персона успевает записать в долгосрочную память. У Коннора — 500, в шаблоне — 50.',
  },
  'pc.version': {
    title: 'Версия конфига',
    body: 'Номер версии yaml-файла персоны — просто пометка для вас, чтобы отличать правки. Для новой персоны оставьте «1.0».',
  },
  'pc.intellect': {
    title: 'Уровень интеллекта',
    body: 'Отдельное измерение поверх характера: определяет, как персона отвечает на просьбы о помощи. «Нечеловеческое мышление» (в yaml — primitive) — существо с нечеловеческим типом мышления (животное, дух, примитивный робот): простая речь, помощь действием, дневник из инстинктивных впечатлений. «Человек» (normal): помогает по-бытовому коротко, без ассистентских уточнений. «Высокий интеллект» (bot): право на полный разбор просьбы (расчёты, код, уточняющие вопросы), подача всё равно в стиле персоны. Уровень ограничивает поведение, а характер (system_prompt) наполняет его внутри ограничения. Вместе с уровнем в поля temperature / max_tokens / top_p подставляются подходящие дефолты (высокий интеллект — 0.7 / 4000 / 0.9; человек — как у живых персон: 0.85 / 3000 / 0.92; нечеловеческое мышление — 0.5 / 800 / 0.85), их можно править вручную. «Не задан» — уровневые механики полностью выключены, блок intellect в yaml не пишется (режим старых персон).',
  },
  'pc.features': {
    title: 'Умения персоны',
    body: 'Чем вооружена персона: поиск в интернете, загрузка файлов, дневник, напоминания и так далее. У Коннора включено: web_search, file_upload, self_memory, todo, reminder, inventory, learning и proactive.',
  },
  'pc.learning': {
    title: 'Обучение «Научи меня»',
    body: 'Персона сможет вести курсы: вы просите «научи меня X», она регулярно присылает уроки и тесты. quiz_every — каждый какой урок будет тестом; silence_threshold — после скольких молчаний подряд спросит «продолжаем?»; интервалы — минимальная и максимальная пауза между уроками в секундах.',
  },
  'pc.proactive': {
    title: 'Самоинициатива',
    body: 'Персона пишет первой, если вы долго молчите. check_interval_minutes — как часто проверять молчание; silence_threshold_minutes — сколько молчания считается «долгим»; initiative_probability — шанс написать при проверке; max_daily_initiatives — лимит таких сообщений в день. Частота также зависит от того, как давно вы отвечали персоне: чем свежее диалог, тем она разговорчивее.',
  },
  'pc.triggerWords': {
    title: 'Слова-вызовы',
    body: 'Слова, на которые персона отзывается в групповом чате, даже когда её не звали напрямую. Через запятую. Пример: «коннор», «connor».',
  },
  'pc.systemPrompt': {
    title: 'Душа персоны',
    body: 'Главная инструкция, по которой персона говорит и ведёт себя: характер, голос, запреты. У Коннора промпт начинается так: «Ты — Коннор, андроид модели RK800, серийный номер 313 248 317 - 52. Произведён CyberLife…».',
  },

  // ===== Напоминания и задачи =====
  'tasks.reminders': {
    title: 'Напоминания',
    body: 'Персона сама напишет вам в нужный момент — один раз или регулярно. Крестик удаляет напоминание навсегда, а переключатель просто усыпляет его на время.',
  },
  'tasks.reminderSwitch': {
    title: 'Пауза напоминания',
    body: 'Выключает напоминание, не удаляя его: в назначенное время оно не сработает, но останется в списке. Включите обратно — и оно снова заработает.',
  },
  'tasks.reminderRepeat': {
    title: 'Повтор',
    body: '«Разовое» — напомнит один раз и завершится. «Каждый день», «по будням», «по выходным» и «по дням недели» — повторяет в указанное время в эти дни; первое срабатывание — не раньше выбранной даты.',
  },
  'tasks.todo': {
    title: 'Список дел',
    body: 'Обычный список задач: галочка отмечает выполненное (текст зачёркивается), крестик удаляет дело из списка. Внизу видно, сколько уже сделано.',
  },
  'tasks.todoProgress': {
    title: 'Сколько уже сделано',
    body: 'Счётчик выполненных дел из общего числа. Отмечайте задачи галочкой — прогресс обновится сам.',
  },
} satisfies Record<string, HelpEntry>;

export type HelpKey = keyof typeof helpTextsRu;

// English translations of the same help entries (same keys).
export const helpTextsEn: Record<HelpKey, HelpEntry> = {
  // ===== Settings =====
  'settings.activeProvider': {
    title: 'Answer source',
    body: 'A persona can "think" through different services — the main one is chosen here. If it stops responding or runs out of quota, the persona switches to a backup from the list on its own. Mark with the dot the service you want to use.',
  },
  'settings.keyRotation': {
    title: 'Spare access keys',
    body: 'You can save several access keys for one service. If the first key runs out of request quota, the persona tries the next one itself. The "N pcs" label shows how many keys are saved: the more there are, the less often the persona goes silent because of limits.',
  },
  'settings.keyStatus': {
    title: 'Connection status',
    body: 'Shows whether the service is connected. "Key set" — the service is usable and takes part in the work. "Key not set" — the service is skipped, even if you mark it with the dot. To connect, add an access key in the app settings.',
  },
  'settings.localModels': {
    title: 'Persona on your computer',
    body: 'Usually a persona thinks through online services, but here — right on your computer, free and without limits. The downside: it works slower and answers more simply, depending on your machine. The "Check availability" button shows whether your computer is ready for this.',
  },
  'settings.temperature': {
    title: 'Reply character',
    body: 'How predictable the persona is: closer to zero — calm, precise and consistent answers; closer to one and above — more imagination and surprises. Default is 0.8 — lively but not chaotic conversation. Set lower for a strict helper, higher for a creative character.',
  },
  'settings.maxTokens': {
    title: 'Reply length',
    body: 'Maximum length of a single persona message. Default is 800 — a couple of paragraphs. Higher — the persona writes in more detail, but slower and burns through quota faster. Lower — shorter and cheaper, but a thought may get cut off mid-sentence.',
  },
  'settings.topP': {
    title: 'Word diversity',
    body: 'How broadly the persona chooses words: small value — only the most expected ones, large value — any suitable ones. Default is 0.9, usually no need to change. Better tune "reply character" above and leave this field alone.',
  },
  'settings.stmSize': {
    title: 'Conversation memory',
    body: 'How many recent messages the persona remembers in the current conversation. Default is 50: when the 51st appears, the oldest is forgotten. Set higher — the persona holds the thread of the conversation longer, but each answer costs a bit more quota.',
  },
  'settings.location': {
    title: 'Location & weather',
    body: 'When enabled, personas see your city, local time and current weather — one short line at the start of their context. This lets a persona naturally account for the time of day or rain in replies and initiatives. Set the city manually or detect it via browser geolocation (coordinates are stored locally on your server only). Weather comes from Open-Meteo and refreshes every 30 minutes. "Off" — personas see nothing.',
  },
  'settings.timezone': {
    title: 'Time zone',
    body: 'This zone determines when personas send reminders, keep the daily rhythm ("good morning", "time to sleep") and count daily initiative limits — for every persona at once. Pick a zone from the list (e.g. Europe/Moscow) or leave "System" to use the server\'s time zone.',
  },
  'settings.feature.web_search': {
    title: 'Internet search',
    body: 'The persona can search the internet for fresh information before answering. It checks its memory and your files first — and goes online only if the answer is not there. Turn off — answers come faster but rely only on what it already knows.',
  },
  'settings.feature.moderation': {
    title: 'Message filter',
    body: 'Checks incoming messages and cuts only the clearly unacceptable — everything else passes freely. Turn off — no checks, and answers arrive slightly faster.',
  },
  'settings.feature.ltm_extraction': {
    title: 'Remembering facts about you',
    body: 'After your messages the persona quietly writes down what matters: city, age, job, hobbies — and remembers forever. Old facts get updated, similar ones merged. Turn off — the persona stops learning new things about you, but keeps what it already memorized.',
  },
  'settings.feature.self_memory': {
    title: "Persona's personal diary",
    body: 'The persona keeps a diary: it writes down what you talked about and how it felt. These notes quietly blend into its thoughts, giving it a sense of "life between conversations". It won\'t tell you about the diary directly.',
  },
  'settings.feature.proactive': {
    title: 'Writes first',
    body: 'If you stay silent for a long time, the persona may write first — ask how you are or share a thought. How often and when exactly is configured in the "Initiative" section. Turn off — the persona will only reply to your messages.',
  },
  'settings.feature.rhythm': {
    title: 'Daily rhythm',
    body: 'The persona lives in the daily rhythm: in the morning, when you turn on your laptop (or first show up in the chat), — a "good morning" in its own character; at midnight — a "time to sleep" nudge, but only if you were active shortly before; when rain, a thunderstorm or a sharp weather change approaches — a warning (requires a configured location in the environment settings). What was sent is noted in the chat dossier so the persona does not repeat itself. Applies instantly, no restart needed.',
  },
  'settings.feature.life': {
    title: 'Persona life',
    body: 'The persona lives while you are away: energy, mood and pastime shift, events happen (encounters, incidents), a diary is kept, storylines develop — and after a long absence they will tell you what they have been through. Sometimes they write first when something happens — but only inside the time window you set (the "Initiative" tab). All the rough work is done by a cheap engine (Ollama or a web chat — "Local task engines"); texts for you come from the main model. Fine tuning lives in the state_engine/world_lore YAML blocks; this switch simply turns them all on.',
  },
  'settings.feature.light_context': {
    title: 'Light context mode',
    body: 'A trimmed context for the main reply: short history (6 messages instead of 15), fewer memory facts, no book search, files or personal diary, truncated web results, shorter answers. Weak models (e.g. a local gemma in Ollama) "drown" in a large context and reply incoherently — light mode makes them faster and more consistent. It turns on automatically for the local provider; this toggle lets you force it for any other weak model. Applies instantly, no restart needed.',
  },
  'settings.feature.computer_control': {
    title: 'Computer control',
    body: 'Lets the persona run commands on your computer: open sites ("open youtube"), search on your sites, launch apps and named tasks, click buttons on pages. What is allowed is configured via the lists in the "Control mode" tab. Turn off — the persona runs nothing, while the lists are kept and come back when re-enabled. Applies instantly, no restart needed.',
  },

  'settings.feature.file_upload': {
    title: 'File upload',
    body: 'You can send documents to the persona: it keeps them and finds the relevant passages when answering. Uploaded files are in the "Files" tab. When off, files are not accepted.',
  },
  'settings.feature.todo': {
    title: 'To-do list',
    body: 'The persona keeps your to-do list: adds items when you mention them and checks off completed ones. The list is in the "Reminders & tasks" tab.',
  },
  'settings.feature.reminder': {
    title: 'Reminders',
    body: 'Ask "remind me tomorrow at 9 to call mom" — the persona will write at that time, in its own words. One-off and repeating reminders are supported. All of them are in the "Reminders & tasks" tab.',
  },
  'settings.feature.learning': {
    title: 'Learning courses',
    body: 'The persona can run a course with you: explains lessons in order, asks check questions and remembers covered topics. Courses and progress are in the "Learning" tab.',
  },
  'settings.feature.inventory': {
    title: 'Inventory',
    body: 'The persona has its own things: gifts from you and what it found. It remembers what it has, can use it and mention it in conversation.',
  },
  'settings.feature.rate_limit': {
    title: 'Message rate limit',
    body: 'Limits how many messages one person can send per hour (6 by default). Over the limit the persona stays silent until the hour passes. Useful for public bots; usually unnecessary for personal chats.',
  },
  'settings.feature.punish_block': {
    title: 'Block on violations',
    body: 'For rudeness or a message cut by the filter, the persona may stop answering the offender for a while (an hour by default). Works together with "Message moderation".',
  },
  'settings.feature.ui_room_mood_sync': {
    title: 'Room & mood',
    body: 'The "Room" and "Mood" tabs in the web UI show the persona\'s live state: where it is, what it\'s doing, how it feels. If not set explicitly, it follows "Life between chats".',
  },
  'settings.feature.room_llm_placement': {
    title: 'Model places items',
    body: 'When the persona gets a new item, the model decides where to put it in the room. When off, the item simply goes on the table without a model call.',
  },
  'settings.feature.room_pokes_to_llm': {
    title: 'Reacting to room pokes',
    body: 'Clicking the persona in the room reaches the main model, which replies in the chat. When off, the reaction happens without the model.',
  },

  // ===== Control mode (computer control) =====
  'computer.title': {
    title: 'What this section does',
    body: 'This is where you define what the persona may control on your computer: favorite sites ("open youtube"), search on your sites ("play Interstellar on kinopoisk"), apps and named tasks, plus clicking buttons on pages. The persona cannot do anything beyond these lists. Settings apply instantly, no restart. To turn all control off — the "Disable" button below or the checkbox in "Settings" → "Features"; the lists are preserved.',
  },
  'computer.confirm': {
    title: 'Action confirmation',
    body: 'When on, the persona asks in chat first — "Open youtube.com?" — and acts only after your "yes". When off — it acts immediately, no questions. Keeping it on is recommended: you always see what the persona is about to do.',
  },
  'computer.click': {
    title: 'Clicking on pages',
    body: 'Commands like "click „log in“", "press „download“ on github": the persona looks at an already open browser tab and clicks the matching button or link, showing beforehand what exactly it will click. The "download X" command saves a file from a page link to your downloads folder. When off, click commands are ignored while the rest of control keeps working. On macOS it works in Chrome with "View → Developer → Allow JavaScript from Apple Events" enabled.',
  },
  'computer.sites': {
    title: 'Favorite sites',
    body: 'Short names for the "open X" command: left — how you call the site ("youtube"), right — its address ("youtube.com"). Listed sites open instantly and precisely. If a site is not listed, the persona looks the address up in your browser history, then on the internet — slower and sometimes off target.',
  },
  'computer.apps': {
    title: 'Applications',
    body: 'Which apps the persona may launch with "launch X". Left — the command name, right — the app name on the system (e.g. Safari or Google Chrome). Only what is listed here can be launched.',
  },
  'computer.search': {
    title: 'Search on your sites',
    body: 'Commands "find X on youtube" and "play X on kinopoisk". Left — the site name, middle — the search page address where {q} is replaced with the query. The third field is optional: a technical pattern (regex) of the first-result link — with it, "play" opens the video or film itself instead of the search page.',
  },
  'computer.tasks': {
    title: 'Named tasks',
    body: 'Your own computer actions for "do X" commands: left — the name ("focus mode"), right — what to run: a system command or a macOS Shortcuts shortcut. Only what is written here gets executed. Video control needs no entries — it is built in (phrases in Russian): «пауза», «тише», «громче», «без звука», «следующее видео», «третье видео», «первый результат» («пауза», «тише», «громче» and «без звука» need agent click enabled).',
  },

  // ===== Initiative =====
  'init.enabled': {
    title: 'How it works',
    body: 'The persona periodically checks how long ago you wrote. If the silence drags on — it may write first. To avoid being annoying there are limits: no more than a few times a day, no repeating old topics, and the persona can always change its mind and stay silent.',
  },
  'init.silenceThreshold': {
    title: 'How long to wait for silence',
    body: 'How long you must be silent before the persona dares to write first. Maximum — one day: it will never wait longer than a day of silence. Lower — it writes more often; higher — rarer and less intrusive.',
  },
  'init.probability': {
    title: 'How eagerly it writes first',
    body: 'When the silence time is up, the persona still "flips a coin": by default it writes in 80% of cases. Lower — it will write first more rarely, even when it really misses you. And if you often reply to its messages, it will gradually grow bolder on its own.',
  },
  'init.maxPerDay': {
    title: 'How many times a day',
    body: 'Maximum first messages per day — 5 by default. Once the limit is exhausted, the persona stays silent until tomorrow, no matter what. This is protection against intrusiveness.',
  },
  'init.checkInterval': {
    title: 'How often it checks',
    body: 'The persona glances at the chat every half hour: is it time to write? So a message may arrive slightly later than the exact silence deadline — on the nearest "check". Usually no need to change.',
  },
  'init.adaptiveThreshold': {
    title: 'Adapting to your rhythm',
    body: 'Instead of a fixed deadline the persona watches how often you usually write and waits roughly twice your usual break. Write once every two hours — it will write first after about four. But no sooner than half an hour and no later than a day.',
  },
  'init.hours': {
    title: 'Self-initiative hours',
    body: 'The window in which the persona is allowed to write first at all. The user sets it — the engine does not pick the moment: outside the window neither regular initiatives nor "life" triggers fire (something happened — the persona waits for the window). Crossing midnight is allowed: 22:00–08:00. Empty — around the clock.',
  },
  'init.bayesianFeedback': {
    title: 'Learns from your reactions',
    body: 'The persona notices whether you reply to its first messages. Reply — it writes a bit more eagerly, ignore — a bit more rarely. This way it finds the frequency comfortable for you: from very rare (10%) to almost always (90%).',
  },
  'init.typeBalance': {
    title: 'What it writes first about',
    body: 'Four reasons to write: a question, an observation, returning to a past topic, or just a thought. The persona picks the one it used least recently — so the messages are not all the same.',
  },
  'init.multiTurn': {
    title: 'Waits for your answer',
    body: 'After writing first, the persona waits half an hour for a reply. You replied — it is glad and remembers the topic landed. You stayed silent — it gets upset and considers the message ill-timed. By default this waiting is off.',
  },
  'init.ignoreStreak': {
    title: 'Silence counter',
    body: 'How many times in a row you haven\'t replied to the persona\'s messages. Each miss — plus one, any reply — reset to zero. By this counter the persona knows how offended to be.',
  },
  'init.initiativesToday': {
    title: 'First messages today',
    body: 'How many times the persona has already written first today. As soon as the counter hits the daily limit (5 by default), it goes quiet until tomorrow.',
  },
  'init.emotionalState': {
    title: "Persona's mood",
    body: 'If you don\'t reply to its messages for a long time, the persona starts taking offense: slightly at first, then more and more. The mood shows in its words — deeply hurt, it may write that it feels lonely. Reply to it — the offense goes away at once.',
  },
  'init.silenceProgress': {
    title: 'How long you\'ve been silent',
    body: 'How much time has passed since your last message. When the bar fills up, the persona can write first — if the daily limit isn\'t exhausted and it decides it\'s appropriate.',
  },
  'init.history': {
    title: 'Recent first messages',
    body: 'What the persona wrote first and when, and how it ended: you replied, skipped it, or it is still waiting. The persona keeps this history in mind to avoid repeating topics and circling the same thing.',
  },

  // ===== Memory =====
  'mem.tabs': {
    title: 'Three kinds of memory',
    body: '"Recent conversation" — what the persona remembers from the current chat. "Facts about you" — what it has memorized about you forever. "Diary" — its personal notes about your conversations. All of this persists and survives restarts.',
  },
  'mem.stm': {
    title: 'Recent conversation',
    body: 'The latest messages of your conversation — the persona re-reads them before every answer. A limited number fits the context (20 by default), but old lines are not lost: vector search runs over the archive of the last 500 messages — fragments relevant to the current topic are pulled into the answer automatically.',
  },
  'mem.ltm': {
    title: 'Facts about you',
    body: 'What the persona has remembered about you for the long term: city, job, hobbies and so on. Outdated facts are replaced with new ones, similar ones merged. Any fact can be edited or deleted manually.',
  },
  'mem.diary': {
    title: "Persona's diary",
    body: 'The persona\'s personal notes about your conversations: what happened and how it felt. When there are too many entries, old ones are condensed into a short "story of its life". These memories influence its answers, but it never mentions the diary out loud.',
  },
  'mem.dossier': {
    title: 'Portrait from conversations',
    body: 'Automatic analysis of your conversation: what you are into, what you talked about and which communication habits were noticed. It refreshes itself as the dialogue goes on and cannot be edited manually. The persona uses this portrait when it messages you first.',
  },
  'mem.clearStm': {
    title: 'Clearing the recent conversation',
    body: 'The persona will forget the current conversation and start with a clean slate. Facts about you and its diary remain untouched. Clearing cannot be undone.',
  },

  'settings.mainProvider': {
    title: 'Main provider',
    body: 'The service that always answers first — the persona thinks through it. There is always exactly one main. The "make main" button on any provider in the list appoints it instead of the current one.',
  },
  'settings.backupProvider': {
    title: 'Backup providers',
    body: 'Reserve services: if the main one doesn\'t respond, is down or out of quota, the answer is picked up by one of those enabled here. The toggle adds a provider to the reserve; disabled ones simply wait their turn. A provider without a key cannot pick up, even if enabled.',
  },
  'settings.gemma': {
    title: 'Lightweight local model',
    body: 'Gemma is a small model that runs right on your computer via Ollama: free, no limits and no API key spending. Install with one command: ollama pull gemma3:4b. Great as a last resort and for simple answers that don\'t need a big model.',
  },
  'settings.purpose': {
    title: 'Control mode providers',
    body:
      'Optional: pin a specific provider to a job in computer-control mode. Regular chat replies are not affected — they are written by the primary provider ("make main") and the chain.\n\n' +
      '• Persona lines in control mode — who writes the persona\'s words: short lines about completed actions ("opened the tab", "couldn\'t click: …"), refilling the bank of such ready-made phrases, and retelling page content. When unassigned, action lines are written by the Google web chat and retelling follows the chain.\n' +
      '• Computer-control decisions — who decides what to do on the computer: where to click, which element to pick. A web chat uses its own channel for this, so the main conversation stays clean.\n' +
      '• Vision fallback — who looks at screen screenshots when the main model can\'t see them.\n\n' +
      'The assigned provider gets one attempt. If it fails, decisions and screenshots follow the chain, and the action line falls back to a template.\n' +
      '"Follow chain" means nothing is assigned. Only providers with a key or local ones are listed.',
  },
  'settings.localBackend': {
    title: 'Local task engines',
    body: 'Besides the main replies, a persona has plenty of small "household" work: classifiers (intent, help request, "teach me"), the search query rewriter, life ticks (state, world, events), the diary, dialogue harvest, offline diary compression. The settings are per persona. Tasks are split into two groups. "In conversation" — the persona\'s reply waits for them, so by default they go to the local Ollama: a warm model answers in a fraction of a second. "Background" — outside the conversation, nobody waits for them, so by default they go to the background web chat: "First fallback" is the first web chat in the persona\'s chain after the primary, "Primary" is the persona\'s primary web chat, or pick a specific site. If it fails, the backup web chat is tried (the primary or the first fallback), then Ollama. Household tasks use the web chat\'s side channel: a separate chat with its own quota, so the main conversation stays clean. Any task can get its engine chosen by hand (Ollama or a web chat with a specific site); "Reset" restores the default. Image OCR always stays on Ollama: a web chat cannot take a picture.',
  },

  // ===== Personas =====
  'persona.yaml': {
    title: 'What a persona is',
    body: 'A persona is the bot\'s personality: who it is, how it speaks, what it can do and what it remembers. Each persona has its own memory; they don\'t interfere with each other. Create a new one with the button above or by copying an existing one.',
  },
  'persona.features': {
    title: "Persona's abilities",
    body: 'What this persona is equipped with: memory about you, writing first, reminders, internet search and so on. The ability set is configured in its settings.',
  },
  'persona.genParams': {
    title: 'How the persona answers',
    body: 'The character and length of its answers: how predictable it is and how much it writes at once. Creative characters usually get more imagination (0.7–0.9), strict helpers — less (around 0.3).',
  },
  'persona.actions': {
    title: 'What you can do',
    body: '"Edit" — change the persona\'s personality and settings. "Duplicate" — make a copy to experiment without touching the original. "Delete" — remove the persona forever. A built-in project persona cannot be deleted: your edits are kept as your own copy, and "Reset to built-in" deletes that copy. All personas are always active: there is nothing to select.',
  },

  // ===== Persona creation (YAML editor) =====
  'pc.id': {
    title: 'Technical name',
    body: 'The persona\'s name in Latin letters; its yaml file will be named after it. Filled automatically from the name, but can be adjusted manually. Example: connor.',
  },
  'pc.name': {
    title: 'Persona name',
    body: 'How the persona will be labeled in chat, room and dossier. The only required field. Example: Connor.',
  },
  'pc.description': {
    title: 'Description',
    body: 'One line about who this is and why they are here — visible in the persona registry. Connor\'s says: "RK800 android assistant, sent by CyberLife to help students".',
  },
  'pc.stmSize': {
    title: 'Short-term memory',
    body: 'How many recent messages the persona keeps in mind during a conversation. When the buffer fills up, old lines are pushed out — the persona saves the important ones to long-term memory in time. Connor has 500, the template has 50.',
  },
  'pc.version': {
    title: 'Config version',
    body: 'The version number of the persona\'s yaml file — just a marker for you to tell edits apart. Leave "1.0" for a new persona.',
  },
  'pc.intellect': {
    title: 'Intellect tier',
    body: 'A separate dimension on top of personality: it defines how the persona responds to requests for help. "Non-human mind" (primitive in yaml) — a creature with a non-human mind (animal, spirit, simple robot): simple speech, helps by doing, an instinct-driven diary. "Human" (normal): helps briefly and casually, no assistant-style clarifications. "High intelligence" (bot): entitled to a full task breakdown (calculations, code, follow-up questions), still delivered in the persona\'s voice. The tier constrains behavior; the character (system_prompt) fills it in within the constraint. Picking a tier also fills temperature / max_tokens / top_p with fitting defaults (high intelligence — 0.7 / 4000 / 0.9; human — like living personas: 0.85 / 3000 / 0.92; non-human mind — 0.5 / 800 / 0.85), which you can then edit manually. "Not set" disables tier mechanics entirely and omits the intellect block from the yaml (legacy mode of old personas).',
  },
  'pc.features': {
    title: "Persona's abilities",
    body: 'What the persona is equipped with: internet search, file upload, diary, reminders and so on. Connor has enabled: web_search, file_upload, self_memory, todo, reminder, inventory, learning and proactive.',
  },
  'pc.learning': {
    title: '"Teach me" learning',
    body: 'The persona can run courses: you ask "teach me X", it regularly sends lessons and quizzes. quiz_every — every Nth lesson is a quiz; silence_threshold — after how many consecutive silences it asks "shall we continue?"; intervals — minimum and maximum pause between lessons in seconds.',
  },
  'pc.proactive': {
    title: 'Proactivity',
    body: 'The persona writes first if you stay silent for a long time. check_interval_minutes — how often to check the silence; silence_threshold_minutes — how much silence counts as "long"; initiative_probability — chance of writing on a check; max_daily_initiatives — daily limit of such messages. Frequency also depends on how long ago you replied: the fresher the dialog, the more talkative it is.',
  },
  'pc.triggerWords': {
    title: 'Trigger words',
    body: 'Words the persona responds to in a group chat even when not addressed directly. Comma-separated. Example: "connor", "connor".',
  },
  'pc.systemPrompt': {
    title: "Persona's soul",
    body: 'The main instruction the persona speaks and behaves by: character, voice, taboos. Connor\'s prompt starts like this: "You are Connor, an RK800 android, serial number 313 248 317 - 52. Manufactured by CyberLife…".',
  },

  // ===== Reminders and tasks =====
  'tasks.reminders': {
    title: 'Reminders',
    body: 'The persona will write to you at the right moment — once or regularly. The cross deletes a reminder forever, while the toggle simply puts it to sleep for a while.',
  },
  'tasks.reminderSwitch': {
    title: 'Reminder pause',
    body: 'Turns a reminder off without deleting it: it won\'t fire at the set time but stays in the list. Turn it back on — and it works again.',
  },
  'tasks.reminderRepeat': {
    title: 'Repeat',
    body: '"One-time" — reminds once and completes. "Daily", "on weekdays", "on weekends" and "custom days" — repeats at the set time on those days; the first one fires no earlier than the chosen date.',
  },
  'tasks.todo': {
    title: 'To-do list',
    body: 'A plain task list: the checkbox marks done (text is struck through), the cross removes a task from the list. At the bottom you can see how much is already done.',
  },
  'tasks.todoProgress': {
    title: 'How much is done',
    body: 'Counter of completed tasks out of the total. Mark tasks with the checkbox — progress updates itself.',
  },
};
