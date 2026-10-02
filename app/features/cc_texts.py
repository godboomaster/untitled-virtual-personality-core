"""Служебные тексты режима управления на языке пользователя.

Фиксированные реплики (отказ, остановка, переключение режима, простой) и
английские описания действий для describe/confirm_question/describe_done.
Языков два: en — английский, всё остальное (ru, None, неизвестный) —
русский шаблон, как было до локализации. Русские описания действий живут
в самом ComputerControlManager; здесь — только английская ветка."""

from typing import Optional

# Длина вводимого текста в вопросе на подтверждение: человек должен видеть,
# что именно уйдёт в поле, а не первые 40 символов
TYPE_PREVIEW_MAX = 200


def is_en(lang: Optional[str]) -> bool:
    return str(lang or "").lower().startswith("en")


def clip(text, limit: int) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


_T = {
    # Отказ на pending-действие
    "declined": {"ru": "Хорошо, не выполняю.",
                 "en": "Okay, I won't do it."},
    "declined_scroll": {"ru": "Хорошо, не выполняю. Листание остановил.",
                        "en": "Okay, I won't do it. Scrolling stopped."},
    "done": {"ru": "Готово, {what}.", "en": "Done: {what}."},
    # Поисковый резолв «открой X»: несколько подходящих ссылок в выдаче
    "site_choices": {
        "ru": "Какой сайт открыть? Нашёл:\n{items}\n"
              "Ответь номером («да» — первый) или «нет».",
        "en": "Which site should I open? Found:\n{items}\n"
              "Reply with a number (\"yes\" means the first) or \"no\"."},
    "site_choice_range": {
        "ru": "Варианта {n} нет — выбери от 1 до {max} или скажи «нет».",
        "en": "There's no option {n} — pick 1 to {max} or say \"no\"."},
    "failed": {"ru": "Не удалось {what}: {detail}.",
               "en": "Couldn't {what}: {detail}."},
    "read_failed": {"ru": "Не удалось прочитать: {detail}.",
                    "en": "Couldn't read it: {detail}."},
    "chain_tail": {"ru": "После «да» дальше: {steps}.",
                   "en": "After \"yes\" I'll continue with: {steps}."},
    "cc_confirm_expired": {"ru": "Подтверждение истекло — повтори команду.",
                        "en": "The confirmation has expired — repeat the command."},
    # Остановка долгого действия (до лока хода)
    "stopping": {"ru": "Останавливаю…", "en": "Stopping…"},
    "already_running": {"ru": "Уже выполняю — дождись результата.",
                        "en": "Already on it — wait for the result."},
    # Секрет в команде, которую regex не разобрал: в облачный LLM-разбор
    # такую фразу не отдаём
    "secret_rephrase": {
        "ru": "В команде есть пароль или код — такую фразу я не отдаю "
              "модели на разбор. Скажи строго так: «введи <значение> в поле "
              "<название поля>».",
        "en": "Your command contains a password or code, so I won't send it "
              "to the model for parsing. Say it exactly like this: \"type "
              "<value> into field <field name>\"."},
    "task_busy": {"ru": "Ещё работаю над задачей — подожди или скажи «отмена».",
                  "en": "Still working on the task — wait or say \"cancel\"."},
    "stopped_by_user": {"ru": "остановлено по просьбе пользователя",
                        "en": "stopped at the user's request"},
    # Самостоятельная реплика: опрос страницы/доскролл прерван «стоп»
    "stopped": {"ru": "Остановлено по твоей просьбе.",
                "en": "Stopped, as you asked."},
    # Маркеры модели (process_markers)
    "marker_not_allowed": {
        "ru": "⚠️ Не могу выполнить «{target}» — нет в списке разрешённых.",
        "en": "⚠️ I can't do \"{target}\" — it's not on the allowed list."},
    # Доскролл до цели («пролистай до X»)
    "scroll_failed": {"ru": "Не удалось пролистать страницу.",
                      "en": "Couldn't scroll the page."},
    "scroll_goal_bottom": {"ru": "Докрутил до самого низа страницы.",
                           "en": "Scrolled to the very bottom of the page."},
    "scroll_goal_top": {"ru": "Вернулся на самый верх страницы.",
                        "en": "Back at the very top of the page."},
    "scroll_goal_found": {"ru": "Нашёл «{goal}» — вот это место.",
                          "en": "Found \"{goal}\" — here's the spot."},
    "scroll_goal_caption": {"ru": "«{goal}» ({host})",
                            "en": "\"{goal}\" ({host})"},
    "scroll_goal_edge_caption": {"ru": "Край страницы ({host})",
                                 "en": "Edge of the page ({host})"},
    # Команды «покажи всю страницу»/«ещё» разбираются только по-русски —
    # английская подсказка называет рабочую русскую команду
    "scroll_goal_miss": {
        "ru": "Пролистал страницу — «{goal}» не вижу. Могу показать её "
              "целиком: скажи «покажи всю страницу».",
        "en": "Scrolled the page — I don't see \"{goal}\". I can show the "
              "whole page: say \"покажи всю страницу\" (show the whole page)."},
    # Отчёт о странице («что на странице?», «покажи всю страницу»)
    "page_view_failed": {"ru": "Не удалось посмотреть страницу.",
                         "en": "Couldn't look at the page."},
    "page_full_line": {"ru": "Снял всю страницу — держи целиком, по кускам.",
                       "en": "Captured the whole page — here it is, piece "
                             "by piece."},
    "page_full_more": {
        "ru": "Прислал первые {n} из {total} кадров — скажи «ещё», пришлю "
              "остальные.",
        "en": "Sent the first {n} of {total} shots — say \"ещё\" (more) and "
              "I'll send the rest."},
    "page_more_next": {"ru": "Держи, следующие {n} кадров — «ещё» пришлёт "
                             "дальше.",
                       "en": "Here are the next {n} shots — \"ещё\" (more) "
                             "sends the rest."},
    "page_more_last": {"ru": "Держи, последние {n} кадров.",
                       "en": "Here are the last {n} shots."},
    "page_shot_caption": {"ru": "Так выглядит страница ({host})",
                          "en": "This is what the page looks like ({host})"},
    "page_shot_failed": {
        "ru": "Скриншот снять не вышел — браузер не дал кадр, держи список "
              "элементов.",
        "en": "Couldn't take a screenshot — the browser gave no frame; "
              "here's the list of elements."},
    # page_view_text / page_view_full_text
    "pv_page": {"ru": "Страница:", "en": "Page:"},
    "pv_no_items": {"ru": "Кликабельных элементов не вижу.",
                    "en": "I don't see any clickable elements."},
    "pv_group_modal": {"ru": "Открытое окно", "en": "Open dialog"},
    "pv_group_fields": {"ru": "Поля ввода", "en": "Input fields"},
    "pv_group_buttons": {"ru": "Кнопки", "en": "Buttons"},
    "pv_group_links": {"ru": "Ссылки", "en": "Links"},
    "pv_group_other": {"ru": "Прочее", "en": "Other"},
    "pv_more": {"ru": "…и ещё {n}", "en": "…and {n} more"},
    "pv_top_down": {"ru": "Сверху вниз:", "en": "Top to bottom:"},
    "pv_no_outline": {"ru": "Структуру страницы текстом не вижу — держи кадры.",
                      "en": "I can't read the page structure as text — "
                            "here are the shots."},
    "pv_truncated": {"ru": "Страница длинная — показал верхнюю часть.",
                     "en": "The page is long — I showed the top part."},
    # Сценарии: реплики бота вокруг ScenarioManager
    "scenario_text_only": {
        "ru": "Жду ответ текстом — содержимое фото или файла в поле сайта "
              "не впишу.",
        "en": "I need the answer as text — I won't type the contents of a "
              "photo or file into the site."},
    "scenario_confirm_start": {"ru": "Запустить сценарий «{name}»?",
                               "en": "Run the \"{name}\" scenario?"},
    "scenario_broken": {"ru": "Сценарий сломался ({err}) — отменил его.",
                        "en": "The scenario broke ({err}) — I cancelled it."},
    # Цепочка шагов (_cc_run_steps)
    "chain_not_command": {
        "ru": "«{step}» не понял как команду — на этом остановился.",
        "en": "I didn't understand \"{step}\" as a command — stopped there."},
    "tasks_disabled": {
        "ru": "Многошаговые задачи у меня выключены — веди меня по шагам: "
              "«открой …», «нажми …».",
        "en": "Multi-step tasks are turned off for me — guide me step by "
              "step: \"open …\", \"click …\"."},
    "task_goal_confirm": {"ru": "Берусь за задачу «{goal}»?",
                          "en": "Shall I take on the task \"{goal}\"?"},
    # В группе голое «да» без триггера бот не видит (гейт Telegram)
    "group_reply_hint": {
        "ru": "(В группе ответь на это сообщение reply-ем: «да» или «нет».)",
        "en": "(In a group, answer with a reply to this message: \"yes\" or \"no\".)"},
    # Переключатель режима
    "cc_mode_disabled": {
        "ru": "Управление компьютером у меня выключено в настройках — "
              "включи его в досье («Инструменты»).",
        "en": "Computer control is turned off in my settings — enable it "
              "in the dossier (\"Tools\")."},
    "cc_mode_already_on": {
        "ru": "Я уже в режиме управления. Обратно — «выйди из режима управления».",
        "en": "I'm already in control mode. To leave — \"exit control mode\"."},
    "cc_mode_on": {
        "ru": "Режим управления включён: «открой …», «нажми …», «введи …», "
              "сценарии — всё работает. На время режима молчат: напоминания, "
              "список дел, инвентарь, обучение. Закончить — «выйди из режима "
              "управления».",
        "en": "Control mode is on: \"open …\", \"click …\", \"type …\", "
              "scenarios — all work. While it's on, reminders, the todo list, "
              "the inventory and lessons are paused. To finish — \"exit "
              "control mode\"."},
    "cc_mode_already_off": {"ru": "Режим управления и так выключен.",
                            "en": "Control mode is already off."},
    "cc_mode_off": {
        "ru": "Вышел из режима управления — браузером не управляю. "
              "Напоминания, список дел, инвентарь и обучение снова работают.",
        "en": "Left control mode — I'm not controlling the browser anymore. "
              "Reminders, the todo list, the inventory and lessons work again."},
    "cc_mode_idle_off": {
        "ru": "Режим управления выключен после простоя ({minutes} мин без "
              "команд). Нужен снова — «перейди в режим управления».",
        "en": "Control mode was turned off after {minutes} min of inactivity. "
              "Need it again — \"enter control mode\"."},
    "rescue_ok": {
        "ru": "Открыл браузер бота — пройди в его окне проверку «я не робот» "
              "или войди в аккаунт, если чат разлогинен (если просят "
              "несколько чатов — в каждом). Как закончишь, просто напиши мне: "
              "карантин снимется сам, и браузер уйдёт обратно в невидимый режим.",
        "en": "I've opened the bot's browser — pass the \"I'm not a robot\" "
              "check in its window or sign in if the chat was logged out (in "
              "each chat that asks). When you're done, just message me: the "
              "quarantine lifts by itself and the browser goes back to "
              "hidden mode."},
    "rescue_fail": {
        "ru": "Не смог перезапустить браузер бота в видимом режиме — "
              "подробности в логе.",
        "en": "Couldn't restart the bot's browser in visible mode — see "
              "the log for details."},
    # ── Сценарии (ScenarioManager): фиксированные реплики без банка ──
    # Команды сценариев разбираются по-русски («сохрани сценарий», «отмена»),
    # английский текст называет рабочую русскую команду
    "scenario_saved_asks": {"ru": " По ходу спрошу: {questions}",
                            "en": " Along the way I'll ask: {questions}"},
    "scenario_record_goes_on": {
        "ru": " Запись продолжается — добавь действий и скажи «сохрани "
              "сценарий» ещё раз.",
        "en": " Recording continues — do a few more actions and say "
              "\"сохрани сценарий\" (save the scenario) again."},
    "scenario_trace_short_since": {
        "ru": "Пока нечего записывать: с начала записи было всего {n} "
              "действий на страницах. Проведи меня по сюжету — и запишем.",
        "en": "Nothing to save yet: since the recording started there were "
              "only {n} actions on pages. Walk me through the flow and "
              "we'll save it."},
    "scenario_trace_short_recent": {
        "ru": "Пока нечего записывать: за последние полчаса было всего {n} "
              "действий на страницах. Проведи меня по сюжету — и запишем.",
        "en": "Nothing to save yet: in the last half hour there were only "
              "{n} actions on pages. Walk me through the flow and we'll "
              "save it."},
    "scenario_trace_unlabeled": {
        "ru": "Из этой трассы сценарий не собрать: в ней есть ввод текста в "
              "поле без подписи — при воспроизведении такое поле не найти. "
              "Пройди путь ещё раз, называя поля.",
        "en": "I can't build a scenario from this trace: it has text typed "
              "into a field without a label, and such a field can't be found "
              "on replay. Go through it again, naming the fields."},
    "scenario_trace_empty": {
        "ru": "В трассе слишком мало осмысленных шагов — сценарий не собрался.",
        "en": "The trace has too few meaningful steps — the scenario didn't "
              "come together."},
    "scenario_continue": {"ru": "Продолжаю.", "en": "Continuing."},
    "scenario_step_skipped": {
        "ru": "Этот шаг уже не нужен — страница ушла вперёд, пропускаю.",
        "en": "This step isn't needed anymore — the page has moved on, "
              "skipping it."},
    "scenario_step_unsure": {"ru": "{done} (вроде; если нет — скажи).",
                             "en": "{done} (I think; if not, tell me)."},
    "scenario_step_ok": {"ru": "Готово.", "en": "Done."},
    "scenario_err_no_target": {"ru": "не нашёл «{target}» на странице",
                               "en": "couldn't find \"{target}\" on the page"},
    "scenario_err_no_field": {"ru": "не нашёл поле «{field}»",
                              "en": "couldn't find the \"{field}\" field"},
    "scenario_err_unknown_step": {"ru": "неизвестный шаг «{op}»",
                                  "en": "unknown step \"{op}\""},
    "scenario_err_failed": {"ru": "не вышло ({detail})",
                            "en": "it didn't work ({detail})"},
    "scenario_finished": {"ru": "Сценарий «{name}» завершён.",
                          "en": "The \"{name}\" scenario is finished."},
    "scenario_step_failed": {
        "ru": "Стоп: {err}. Скажи «повтори», «дальше» (пропустить) или "
              "«отмена».",
        "en": "Stopped: {err}. Say \"retry\", \"skip\" or \"отмена\" "
              "(cancel)."},
    # ── Агент задач (TaskAgent): фиксированные реплики без банка ──
    "task_crashed": {"ru": "Задача сорвалась: {err}.",
                     "en": "The task broke down: {err}."},
    "task_done_unverified": {
        "ru": "Состав корзины я проверить не смог — посмотри её перед "
              "оплатой.",
        "en": "I couldn't verify the cart contents — check it before "
              "paying."},
    "task_cart_fact": {"ru": "Корзина на сайте: {cart}.",
                       "en": "The cart on the site: {cart}."},
    "task_fact_site": {"ru": "сайт {site}", "en": "site {site}"},
    "task_fact_total": {"ru": "итого {total}", "en": "total {total}"},
    "task_fact_address": {"ru": "адрес: {value}", "en": "address: {value}"},
    "task_fact_time": {"ru": "время: {value}", "en": "time: {value}"},
    "task_fact_payment": {"ru": "оплата: {value}", "en": "payment: {value}"},
    "task_fact_unknown": {"ru": "состав и сумму со страницы прочитать не "
                                "удалось — проверь на экране",
                          "en": "couldn't read the items and total from the "
                                "page — check the screen"},
    "task_total_changed": {"ru": "Сумма на странице изменилась — "
                                 "подтверди заново.",
                           "en": "The total on the page changed — please "
                                 "confirm again."},
    # Сайт из прошлой задачи — вопрос до любого действия
    "task_site_offer": {"ru": "Как в прошлый раз — на {site}? (да / другой "
                              "сайт)",
                        "en": "Same as last time — on {site}? (yes / another "
                              "site)"},
    # Разделы магазина (что заказать, не сказано) и выбранный раздел
    "task_sections": {"ru": "На {site} есть разделы:\n{options}\nЧто "
                            "посмотрим? Можно и сразу назвать, что хочешь.",
                      "en": "{site} has these sections:\n{options}\nWhich "
                            "one shall we look at? Or just name what you "
                            "want."},
    "task_sections_past": {"ru": "\nВ прошлый раз: {what}.",
                           "en": "\nLast time: {what}."},
    "task_section_all": {"ru": "В разделе «{section}»:\n{items}\nЧто "
                               "заказываем?",
                         "en": "In \"{section}\":\n{items}\nWhat shall I "
                               "order?"},
    "task_page_all": {"ru": "На {site}:\n{items}\nЧто заказываем?",
                      "en": "On {site}:\n{items}\nWhat shall I order?"},
    "task_section_rest": {"ru": "- и ещё {n} — скажи «покажи все»",
                          "en": "- and {n} more — say \"show all\""},
    # Магазин открыт кликом, сайт не выбран — до первого действия на нём
    "task_site_here": {"ru": "Заказываем здесь — на {site}? (да / другой "
                             "сайт)",
                       "en": "Order here — on {site}? (yes / another site)"},
    # Сайт выбран названием, а сверить его с адресом нечем
    "task_site_is": {"ru": "«{name}» — это {site}? Заказываем здесь? (да / "
                           "нет)",
                     "en": "Is \"{name}\" {site}? Order here? (yes / no)"},
    # Выбран один сайт, а действие — на другом (редирект, ссылка)
    "task_site_switch": {"ru": "Сейчас открыт {site}, а выбран {chosen}. "
                               "Заказываем здесь? (да / нет)",
                         "en": "{site} is open now, but {chosen} was chosen. "
                               "Order here? (yes / no)"},
    # Заказ, сайт не выбран — до открытия магазина, варианты из выдачи
    "task_site_which": {"ru": "На каком сайте это сделать? Нашёл:\n{options}"
                              "\nМожно назвать и другой.",
                        "en": "Which site should I use? Found:\n{options}\n"
                              "Or name another one."},
    # Вопросы о товаре одним сообщением — до «В корзину»
    "task_item_qs": {"ru": "Перед тем как положить «{item}» в корзину:\n"
                           "{questions}\nОтветь одним сообщением; «как есть» "
                           "— положу как выбрано.",
                     "en": "Before I put \"{item}\" in the cart:\n"
                           "{questions}\nAnswer in one message; \"as is\" — "
                           "I'll add it as selected."},
    "task_q_size": {"ru": "Какой размер: {sizes}?",
                    "en": "Which size: {sizes}?"},
    "task_q_size_sel": {"ru": " Сейчас выбран {sel}.",
                        "en": " Selected now: {sel}."},
    "task_q_opts": {"ru": "Ещё варианты: {opts}; сейчас выбрано: {sel}. "
                          "Поменять что-то?",
                    "en": "Other choices: {opts}; selected now: {sel}. "
                          "Change anything?"},
    "task_q_addons": {"ru": "Добавить к «{item}» что-нибудь из этого?",
                      "en": "Add any of these to \"{item}\"?"},
    "task_addons_rest": {"ru": "- и ещё {n}", "en": "- and {n} more"},
    # Просимое в корзине (план) и перед оформлением: дозаказ и то, что лежало
    # в корзине до задачи
    "task_more": {"ru": "В корзине: {what}.{extra} Добавить что-нибудь ещё? "
                        "Если нет — «нет», открою корзину и перейду к "
                        "оформлению.",
                  "en": "In the cart: {what}.{extra} Anything else? If not, "
                        "say \"no\" and I'll open the cart and go to "
                        "checkout."},
    "task_more_pre": {"ru": " Ещё до этой задачи в корзине что-то лежало "
                            "({pre}), сейчас на сайте {cart} — скажи, если "
                            "это убрать.",
                      "en": " There was already something in the cart before "
                            "this task ({pre}); the site shows {cart} now — "
                            "tell me if it should go."},
    "task_more_missing": {"ru": " Ещё не в корзине: {items}.",
                          "en": " Not in the cart yet: {items}."},
    "task_fact_pre": {"ru": "в корзине есть и то, что лежало там до задачи",
                      "en": "the cart also has what was there before this "
                            "task"},
    "task_fact_cart": {"ru": "в корзине на сайте: {cart}",
                       "en": "the cart on the site: {cart}"},
    "task_items": {"ru": "шт.", "en": "item(s)"},
    "task_in_block": {"ru": "в блоке «{block}»", "en": "in the block \"{block}\""},
    "task_private_group_confirm": {
        "ru": "Следующий шаг на приватной странице нужно подтвердить, а в "
              "группе я его не показываю — подтвердить вслепую нельзя. Сделай "
              "этот шаг в браузере сам; когда пройдёшь, скажи «да» — продолжу "
              "(или «стоп»).",
        "en": "The next step on a private page needs a confirmation, but I "
              "don't show it in a group — confirming blind isn't allowed. Do "
              "this step in the browser yourself; when you're past it, say "
              "\"yes\" and I'll continue (or \"stop\")."},
    "task_private_group_finish": {
        "ru": "Задача дошла до приватной страницы (оплата, вход или личный "
              "кабинет) — дальше сам, браузер открыт на этом шаге. "
              "Подробности в группе не показываю.",
        "en": "The task reached a private page (payment, sign-in or an "
              "account) — take it from here, the browser is open at this "
              "step. I don't show the details in a group."},
    "task_private_group": {
        "ru": "Шаг на приватной странице (вход, оплата, личный кабинет) — в "
              "группе подробности не показываю. Ответ жду от того, кто "
              "поставил задачу; «стоп» — прервать.",
        "en": "A step on a private page (sign-in, payment, account) — I don't "
              "show the details in a group. Waiting for the person who set "
              "the task; \"stop\" to cancel."},
    "task_llm_down": {
        "ru": "Модель сейчас не отвечает — продолжить попозже? («да» / "
              "«отмена»)",
        "en": "The model isn't answering right now — continue later? "
              "(\"yes\" / \"cancel\")"},
    "task_llm_invalid": {
        "ru": "Модель так и не выдала понятного действия — останавливаю "
              "задачу.",
        "en": "The model never gave a clear action — stopping the task."},
    "task_loop": {
        "ru": "Хожу по кругу — одно и то же действие не помогает. "
              "Останавливаюсь, браузер оставил как есть.",
        "en": "I'm going in circles — the same action doesn't help. "
              "Stopping; I left the browser as it is."},
    "task_done_default": {"ru": "Готово.", "en": "Done."},
    "task_failed_msg": {"ru": "Не получилось: {reason}.",
                        "en": "It didn't work out: {reason}."},
    "task_fail_no_reason": {"ru": "причину модель не назвала",
                            "en": "the model gave no reason"},
    "task_scrolling": {"ru": "Листаю страницу.", "en": "Scrolling the page."},
    "task_finding": {"ru": "Ищу «{text}» на странице.",
                     "en": "Looking for \"{text}\" on the page."},
    "task_reading": {"ru": "Читаю страницу.", "en": "Reading the page."},
    "task_searching": {"ru": "Ищу в интернете: «{query}».",
                       "en": "Searching the web: \"{query}\"."},
    # Ход агента в боте (_task_agent_turn)
    "task_text_only": {
        "ru": "Жду ответ текстом — содержимое фото или файла агенту не "
              "передаю.",
        "en": "I need the answer as text — I don't pass the contents of a "
              "photo or file to the agent."},
    "task_turn_crashed": {"ru": "Задача сорвалась ({err}) — бросил её.",
                          "en": "The task broke down ({err}) — I dropped it."},
    "task_already_closed": {
        "ru": "Задача уже закрыта — поставь её заново, если нужно.",
        "en": "The task is already closed — set it again if you need to."},
    # ── Гейт подтверждения (execute отказал: нет «да» человека) ──
    "gate_blocked": {
        "ru": "не выполняю без подтверждения ({risk})",
        "en": "not doing it without a confirmation ({risk})"},
    "gate_nav_question": {
        "ru": "{done}Дальше на {host} — «{label}»: {risk}. Нажать?{tail}",
        "en": "{done}Next on {host}: \"{label}\" — {risk}. Click it?{tail}"},
    "gate_nav_done": {"ru": "Прошёл: {steps}. ",
                      "en": "Went through: {steps}. "},
    "gate_nav_tail": {"ru": " Потом: {steps}.", "en": " Then: {steps}."},
    "url_params_note": {
        "ru": "В адресе есть параметры: {url}",
        "en": "The address has parameters: {url}"},
    "marker_url_params": {
        "ru": "Ссылку с параметрами, которую предложила модель, сам не "
              "открываю — если нужно, скажи «открой …» и адрес.",
        "en": "I won't open a link with parameters that the model suggested "
              "— if you need it, say \"open …\" with the address."},
    # Сценарий: шаг ждёт «да»/«нет», передача оплаты, «стоп»
    "scenario_step_confirm": {
        "ru": "Шаг сценария — {what} ({risk}). Делаю? (да/нет)",
        "en": "Scenario step — {what} ({risk}). Shall I? (yes/no)"},
    "scenario_confirm_wait": {
        "ru": "Жду ответа на шаг сценария: «да» — сделать, «нет» — "
              "остановить сценарий.",
        "en": "Waiting for your answer on the scenario step: \"yes\" — do "
              "it, \"no\" — stop the scenario."},
    "scenario_confirm_foreign": {
        "ru": "Этот шаг сценария должен подтвердить тот, кто его запустил.",
        "en": "This scenario step has to be confirmed by the person who "
              "started it."},
    "scenario_confirm_expired": {
        "ru": "Подтверждение шага истекло — проверяю страницу заново.",
        "en": "The step confirmation expired — checking the page again."},
    "scenario_declined": {
        "ru": "Хорошо, шаг не делаю — сценарий «{name}» остановлен.",
        "en": "Okay, I won't do that step — the \"{name}\" scenario is "
              "stopped."},
    "scenario_payment_handoff": {
        "ru": "Дальше оплата («{label}») — это уже за тобой, я к деньгам не "
              "прикасаюсь.",
        "en": "Next comes the payment (\"{label}\") — that's on you, I don't "
              "touch money."},
    "scenario_stopped": {
        "ru": "Остановлено по твоей просьбе — сценарий «{name}» прерван.",
        "en": "Stopped, as you asked — the \"{name}\" scenario is "
              "interrupted."},
    # ── Отказы резолверов (ComputerControlManager._tx) ──
    # Русские шаблоны — ровно прежние тексты (тесты и аудит их ищут)
    "rs_no_page": {
        "ru": "Пока нет открытой мной страницы — сначала «открой …», "
              "потом уточняй «на этой странице …».",
        "en": "I haven't opened a page yet — say \"open …\" first, then "
              "\"on this page …\"."},
    "rs_unknown_place": {
        "ru": "Не знаю, где «{site}»: такого алиаса в sites нет и на домен "
              "не похоже. Открой сайт («открой …»), назови домен («на "
              "example.edu») или скажи «на этой странице».",
        "en": "I don't know where \"{site}\" is: it's not a known site "
              "and doesn't look like a domain. Open the site (\"open …\"), "
              "name the domain (\"on example.com\") or say \"on this "
              "page\"."},
    "rs_snapshot_failed": {"ru": "Не удалось: {detail}",
                           "en": "Couldn't read the page: {detail}"},
    "rs_destructive_veto": {
        "ru": "На странице {host} для «{goal}» подходит только кнопка "
              "закрытия/удаления — не жму её без явной просьбы. Скажи "
              "«закрой …» или «удали …», если это то, что нужно.",
        "en": "On {host}, the only match for \"{goal}\" is a close/delete "
              "button — I won't press it unless you ask explicitly. Say "
              "\"close …\" or \"delete …\" if that's what you want."},
    "rs_antibot": {
        "ru": "Похоже, {host} показывает проверку «я не робот» ({kind}) — "
              "пройди её в браузере вручную и повтори.",
        "en": "Looks like {host} is showing an \"I'm not a robot\" check "
              "({kind}) — pass it in the browser yourself and try again."},
    "rs_budget": {
        "ru": "На странице {host} не успел найти элемент для «{goal}» за "
              "{sec} с — остановил поиск. Назови элемент так, как он "
              "подписан на странице.",
        "en": "Couldn't find an element for \"{goal}\" on {host} within "
              "{sec} s — stopped searching. Name the element the way it's "
              "labeled on the page."},
    "rs_no_element": {
        "ru": "На странице {host} не нашёл элемента для «{goal}».",
        "en": "Couldn't find an element for \"{goal}\" on {host}."},
    "rs_already_scrolling": {
        "ru": "Я уже листаю страницу — скажи «стоп», и остановлюсь.",
        "en": "I'm already scrolling the page — say \"stop\" and I'll "
              "stop."},
    "rs_scroll_chat_tab": {
        "ru": "Сейчас активна вкладка чата — листать там нечего. Назови "
              "сайт («промотай страницу на ютубе») или переключись на неё.",
        "en": "The chat tab is active right now — there's nothing to "
              "scroll there. Name the site (\"scroll the page on "
              "youtube\") or switch to it."},
    "rs_no_browser": {"ru": "Не вижу браузер: {detail}",
                      "en": "I can't see the browser: {detail}"},
    "rs_tabs_ambiguous": {
        "ru": "Под «{goal}» подходят несколько вкладок: {cands}. Уточни, "
              "какую.",
        "en": "Several tabs match \"{goal}\": {cands}. Which one?"},
    "rs_tab_not_found": {
        "ru": "Вкладка «{goal}» не найдена. Открыты: {names}.",
        "en": "No tab \"{goal}\" found. Open tabs: {names}."},
    "rs_tab_not_found_open": {
        "ru": "Вкладка «{goal}» не найдена. Открыты: {names}. Скажи "
              "«открой …», если нужна новая.",
        "en": "No tab \"{goal}\" found. Open tabs: {names}. Say \"open …\" "
              "if you need a new one."},
    "rs_tab_new": {
        "ru": "Пустую вкладку открывать не буду — скажи, что открыть: "
              "например, «открой ютуб».",
        "en": "I won't open an empty tab — tell me what to open, e.g. "
              "\"open youtube\"."},
    "rs_tab_close_all": {
        "ru": "Все вкладки разом не закрываю. Называй по одной: «закрой "
              "вкладку ютуба», или спроси «какие вкладки открыты».",
        "en": "I don't close all tabs at once. Name them one by one "
              "(\"close the youtube tab\"), or ask \"which tabs are "
              "open\"."},
    "tabs_none": {"ru": "В браузере бота нет открытых вкладок.",
                  "en": "There are no open tabs in the bot's browser."},
    "tabs_list": {"ru": "Открытые вкладки: {tabs}{more}.",
                  "en": "Open tabs: {tabs}{more}."},
    "tabs_current": {"ru": " ← текущая", "en": " ← current"},
    "tabs_more": {"ru": " и ещё {n}", "en": " and {n} more"},
    "rs_click_chat_tab": {
        "ru": "Сейчас активна вкладка чата — там кликать нечего. Назови "
              "сайт («нажми X на ютубе») или переключись на страницу.",
        "en": "The chat tab is active right now — there's nothing to click "
              "there. Name the site (\"click X on youtube\") or switch to "
              "the page."},
    "rs_type_chat_tab": {
        "ru": "Сейчас активна вкладка чата — туда вводить нечего. Назови "
              "сайт («введи X в поле Y на ютубе») или переключись на "
              "страницу.",
        "en": "The chat tab is active right now — there's nothing to type "
              "into there. Name the site (\"type X into field Y on "
              "youtube\") or switch to the page."},
    "rs_no_fields": {"ru": "На странице {host} нет полей ввода.",
                     "en": "There are no input fields on {host}."},
    "rs_type_which_field": {
        "ru": "Не понял, в какое поле ввести. Скажи так: «введи ТЕКСТ в "
              "поле НАЗВАНИЕ».",
        "en": "I didn't get which field to type into. Say it like this: "
              "\"type TEXT into field NAME\"."},
    "rs_type_unparsed": {
        "ru": "Не разобрал, что и куда ввести из «{body}». Скажи так: "
              "«введи ТЕКСТ в поле НАЗВАНИЕ».",
        "en": "I couldn't tell what to type and where from \"{body}\". Say "
              "it like this: \"type TEXT into field NAME\"."},
    "rs_type_no_text": {
        "ru": "Не понял, какой текст ввести в «{field}» — добавь текст "
              "после названия поля.",
        "en": "I didn't get what text to type into \"{field}\" — add the "
              "text after the field name."},
    "rs_type_bad_text": {"ru": "Не понял, какой текст ввести.",
                         "en": "I didn't get what text to type."},
    "rs_no_field": {"ru": "На странице {host} не нашёл поля «{field}».",
                    "en": "Couldn't find the field \"{field}\" on {host}."},
    "rs_fields_seen": {"ru": " Вижу поля: {labels}.",
                       "en": " I can see these fields: {labels}."},
    "rs_hidden_field": {
        "ru": "Поле «{label}» на странице {host} есть, но сейчас скрыто "
              "(свёрнутое меню или закрытый попап). Открой его и повтори — "
              "тогда введу.",
        "en": "The field \"{label}\" on {host} exists but is hidden right "
              "now (a collapsed menu or a closed popup). Open it and try "
              "again — then I'll type."},
    "rs_no_city": {
        "ru": "Не знаю твой город: местоположение выключено (досье → "
              "«Настройки» → местоположение). Назови город текстом.",
        "en": "I don't know your city: location is turned off (dossier → "
              "\"Settings\" → location). Tell me the city as text."},
    "rs_not_file_link": {
        "ru": "Элемент «{name}» — не ссылка на файл, скачивать нечего.",
        "en": "The element \"{name}\" isn't a link to a file — nothing to "
              "download."},
    "rs_cart_chat_tab": {
        "ru": "Сейчас активна вкладка чата — корзины там нет. Переключись "
              "на страницу магазина.",
        "en": "The chat tab is active right now — there's no cart there. "
              "Switch to the shop's page."},
    "rs_comp_edit_multi": {
        "ru": "На странице несколько «Изменить состав»{variants} — уточни, "
              "для какого товара.",
        "en": "There are several \"Edit ingredients\" buttons on the "
              "page{variants} — which item do you mean?"},
    "rs_intent_failed": {
        "ru": "Понял команду как «{kind}», но выполнить не получилось: "
              "{detail}",
        "en": "I understood the command as \"{kind}\", but couldn't carry "
              "it out: {detail}"},
    "rs_hint_key": {
        "ru": "«{goal}» — это клавиша, а не кнопка на странице: скажи "
              "«нажми клавишу {goal}»",
        "en": "\"{goal}\" is a key, not a button on the page: say \"press "
              "the {goal} key\""},
    "rs_hint_scroll": {
        "ru": "Это листание, а не кнопка: скажи «прокрути вниз/вверх»",
        "en": "That's scrolling, not a button: say \"scroll down/up\""},
    "rs_hint_site": {
        "ru": "«{site}» — это сайт, а не кнопка: скажи «открой {site}»",
        "en": "\"{site}\" is a site, not a button: say \"open {site}\""},
    "rs_hint_noise": {
        "ru": "«{goal}» — не похоже на кнопку на странице: назови, что "
              "нажать, как оно подписано",
        "en": "\"{goal}\" doesn't look like a button on the page: name "
              "what to press the way it's labeled"},
    # ── Фолбэки лесенки бота (_cc_ladder) при исключении резолвера ──
    "ladder_action_failed": {
        "ru": "Не удалось выполнить действие на странице.",
        "en": "Couldn't perform the action on the page."},
    "ladder_tabs_failed": {"ru": "Не удалось получить список вкладок.",
                           "en": "Couldn't get the list of tabs."},
    "ladder_tab_switch_failed": {"ru": "Не удалось переключить вкладку.",
                                 "en": "Couldn't switch the tab."},
    "ladder_read_failed": {"ru": "Не удалось прочитать страницу.",
                           "en": "Couldn't read the page."},
    "ladder_zoom_failed": {"ru": "Не удалось изменить масштаб.",
                           "en": "Couldn't change the zoom."},
    "ladder_not_found": {"ru": "Не нашёл это на странице.",
                         "en": "Couldn't find that on the page."},
}

# Английские шаблоны фраз с голосом персоны (flavor-банк, секция phrases):
# русский шаблон остаётся в месте вызова (сценарии, агент задач) и уходит
# в банк как раньше; английскому ходу банк не годится — он сгенерирован на
# языке персоны (см. phrase)
_EN_PHRASES = {
    "scenario_record_already": (
        "Already recording (since {since}). When you're done, say \"сохрани "
        "сценарий\" (save the scenario); changed your mind — \"отмени "
        "запись\" (cancel recording)."),
    "scenario_record_start": (
        "Recording a scenario. Do things as usual — \"open …\", \"click …\", "
        "\"type …\" — it all goes into the recording. To finish: \"сохрани "
        "сценарий\" (save the scenario), optionally with a name. To cancel: "
        "\"отмени запись\"."),
    "scenario_record_cancel_none": "Nothing was being recorded — nothing "
                                   "to cancel.",
    "scenario_record_cancel": "Recording cancelled — nothing saved.",
    "scenario_save_ask_name": (
        "What should I call the scenario? Say it like this: \"сохрани "
        "сценарий заказ пиццы\" (save the scenario pizza order)."),
    "scenario_saved": ("Saved the scenario \"{name}\" — {steps} steps. Now "
                       "just say \"{name}\"."),
    "scenario_not_found": "I don't have a scenario called \"{name}\".",
    "scenario_started": ("Here we go — \"{name}\" ({steps} steps). Say "
                         "\"отмена\" (cancel) if you change your mind."),
    "scenario_stuck": ("I'm stuck on a failed step. Say \"retry\", \"skip\" "
                       "or \"отмена\" (cancel)."),
    "scenario_run_cancel": "The \"{name}\" scenario is cancelled.",
    "scenario_run_cancel_none": "Nothing to cancel — no scenario is running.",
    "scenario_offer": (
        "By the way, that was a whole flow — I can remember it as a scenario "
        "and do it myself next time. Say \"запомни сценарий …\" (remember "
        "the scenario) with a name."),
    "task_cancelled": "Okay, dropping the task.",
    "task_started": "On it: {goal}. \"stop\" to cancel.",
    "task_continue_foreign": "Only the person who set this task can "
                             "continue it.",
    "task_confirm_foreign": "This step has to be confirmed by the person who "
                            "set the task.",
    "task_owner_foreign": "This task is run by the person who set it.",
    "task_continue_expired": ("The pause dragged on. Continue the task? "
                              "(\"yes\" / \"cancel\")"),
    "task_too_long": ("I've done {steps} steps and the task still isn't "
                      "solved — stopping. I left the browser as it is."),
    "task_continue": ("I've done {steps} steps already and the task isn't "
                      "finished. Continue? (\"yes\" / \"cancel\")"),
    "task_private_handoff": (
        "This is a private page (cart, checkout, sign-in, payment or an "
        "account) — I don't send its contents to an external model, and "
        "there's no local one. Take it from here; when you're past this "
        "step, say \"yes\" and I'll continue (or \"stop\")."),
    "task_search_pii": (
        "I want to search the web for \"{query}\", but the query has personal "
        "data (email, phone, card number or a code) — it would go to the "
        "search engine. Search like this? (yes/no)"),
    "task_payment_handoff": (
        "I've reached the payment (\"{label}\") — take it from here: I won't "
        "pay for you. The browser is open at this step."),
    "task_confirm": "Next step — {what}. Shall I? (yes/no)",
    "task_commit_confirm": ("Placing the order: {what}. Press \"{label}\"? "
                            "(yes/no)"),
    "task_confirm_dialog": ("Next step — \"{label}\" in a site dialog: "
                            "\"{ctx}\". Shall I? (yes/no)"),
    "task_switch": ("A task is running now: \"{goal}\". Drop it and do "
                    "\"{cmd}\"? (yes/no)"),
    "task_switch_no": "OK, I'm going on with the task.",
    "task_confirm_expired": ("That question was more than 10 minutes ago — "
                             "the page may have changed, so I'm not doing "
                             "the step; looking at the page again."),
    "task_confirm_stale": ("While I was waiting for your answer the page "
                           "changed — that element is gone or its label is "
                           "different. Not doing the step; looking again."),
    "task_confirm_submit": ("Next step — \"{label}\": this button submits the "
                            "form to the site. Shall I? (yes/no)"),
    "task_confirm_unlabeled": ("Next step — click \"{label}\": the button has "
                               "no clear label, so I can't tell what it will "
                               "do. Shall I? (yes/no)"),
    "task_unclear_yes_no": ("I didn't get \"{msg}\". Please answer \"yes\" or "
                            "\"no\" (or \"cancel\")."),
    "task_cart_stuck": ("\"{label}\" didn't work twice already — the item "
                        "isn't in the cart. Press it again? (yes/no)"),
    "task_cart_again": ("Already in the cart: {added}. Add more — press "
                        "\"{label}\"? (yes/no)"),
    "task_repeat_toggle": ("\"{label}\" is already pressed — pressing it "
                           "again will most likely undo the choice. Press it "
                           "anyway? (yes/no)"),
    "task_option_unchosen": ("Press \"{label}\"? It wasn't in your answer. "
                             "(yes/no)"),
}


def phrase(context: str, key: str, template: str,
           lang: Optional[str] = None, **values) -> str:
    """Служебная фраза сценариев/агента задач на языке хода. Не en —
    голос персоны из flavor-банка с русским шаблоном template, как раньше;
    en — английский шаблон (_EN_PHRASES, затем _T): банк сгенерирован на
    языке персоны. Английского шаблона нет — template."""
    if is_en(lang):
        en = _EN_PHRASES.get(key) or (_T.get(key) or {}).get("en")
        if not en:
            return template
        try:
            return en.format_map(_Keep(_no_double_dot(
                en, {k: str(v) for k, v in values.items()})))
        except (ValueError, IndexError):
            return en
    try:
        from app.features import flavor_text
        return flavor_text.phrase(context, key, template,
                                  **_no_double_dot(template, values))
    except Exception:
        return template


def t(key: str, lang: Optional[str] = None, **values) -> str:
    """Фиксированная реплика на языке пользователя; ключа/языка нет —
    русский шаблон. Плейсхолдеры, которых не передали, остаются как есть."""
    row = _T.get(key) or {}
    text = row.get("en") if is_en(lang) else None
    text = text or row.get("ru") or key
    try:
        return text.format_map(_Keep(_no_double_dot(text, values)))
    except (ValueError, IndexError):
        return text


def _no_double_dot(template: str, values: dict) -> dict:
    """Значение, которое шаблон сам закрывает точкой («Стоп: {err}.»), —
    без своей хвостовой точки: отказ резолва уже кончается ею, и в ответ
    шло «…для «X».. Скажи…». Многоточие («..», «…») не трогаем."""
    out = dict(values)
    for k, v in values.items():
        if isinstance(v, str) and v.endswith(".") and not v.endswith("..") \
                and ("{" + k + "}.") in template:
            out[k] = v[:-1]
    return out


class _Keep(dict):
    def __missing__(self, k):
        return "{" + k + "}"


# Пояснение причины гейта подтверждения в вопросе (computer_control.GATE_RISKS)
_GATE_RISK = {
    "payment": {"ru": "это оплата", "en": "this is a payment"},
    "commit": {"ru": "это отправка или оформление — необратимо",
               "en": "this submits or places an order — irreversible"},
    "destructive": {"ru": "это удаление или выход",
                    "en": "this deletes something or signs out"},
    "force_confirm": {"ru": "не уверен, что это нужный элемент",
                      "en": "I'm not sure it's the right element"},
    "label_unverified": {"ru": "подпись элемента не совпала с запросом",
                         "en": "the element's label didn't match the request"},
    "point": {"ru": "клик по точке на скриншоте",
              "en": "a click on a point of the screenshot"},
    "sensitive_field": {"ru": "чувствительное поле",
                        "en": "a sensitive field"},
    "marker": {"ru": "действие предложила модель",
               "en": "the model suggested this action"},
    "via_search": {"ru": "адрес нашёл поисковик",
                   "en": "the address came from a web search"},
}


def gate_risk(reason, lang: Optional[str] = None) -> str:
    row = _GATE_RISK.get(str(reason or "")) or _GATE_RISK["force_confirm"]
    return row["en"] if is_en(lang) else row["ru"]


# ── Английские описания действий ─────────────────────────

_KEY_EN = {"Space": "Space", "Enter": "Enter", "Escape": "Escape",
           "Tab": "Tab", "Backspace": "Backspace"}
_SLIDER_UNIT_EN = {"pct": "%", "min": " min", "sec": " sec"}


def _q(s: str) -> str:
    return f"\"{s}\""


def _scroll_what(action: dict) -> str:
    what = "the page"
    if action.get("container"):
        what = _q(action["container"])
    elif action.get("side"):
        what = ("the left section" if action.get("side") == "left"
                else "the right section")
    if action.get("dir") == "up":
        what += " up"
    return what


def describe_en(action: dict, form: str = "do", host_fn=None) -> str:
    """Английское описание действия. form: "do" — инфинитив со строчной
    («open example.com»), "ask" — вопрос на подтверждение, "done" —
    прошедшее время для «Done: …». host_fn — _host менеджера для url."""
    kind = action.get("kind")
    host = action.get("host", "")
    on = f" on {host}" if host else ""
    done = form == "done"

    def V(inf: str, past: str) -> str:
        return past if done else inf

    if kind == "multi":
        sep = ", " if done else " and "
        s = sep.join(describe_en(a, "done" if done else "do", host_fn)
                     for a in action.get("items") or [])
    elif kind == "nav":
        steps = list(action.get("steps") or [])
        if action.get("gate_label"):
            # Продолжение маршрута после «да» на рискованный шаг
            s = (f"went through to {_q(steps[-1] if steps else '')}{on}"
                 if done else f"continue{on}: {' → '.join(steps)}")
        elif done:
            s = (f"opened {host} and went through to "
                 f"{_q(steps[-1] if steps else '')}")
        else:
            s = f"open {host} and go through: {' → '.join(steps)}"
    elif kind == "download":
        s = f"{V('download', 'downloaded')} {_q(action.get('element', ''))} from {host}"
        if form == "ask" and action.get("url"):
            return s[0].upper() + s[1:] + "?\n" + str(action["url"])
    elif kind == "click":
        s = f"{V('click', 'clicked')} {_q(action.get('element', ''))}{on}"
    elif kind == "hover":
        s = f"{V('hover over', 'hovered over')} {_q(action.get('element', ''))}{on}"
    elif kind == "type":
        limit = TYPE_PREVIEW_MAX if form == "ask" else 40
        tail = V(" and submit", " and submitted") if action.get("submit") else ""
        s = (f"{V('type', 'typed')} {_q(clip(action.get('text'), limit))} into "
             f"the {_q(action.get('element', ''))} field{on}{tail}")
    elif kind == "read":
        what = "the page" if action.get("mode") == "page" else "the last message"
        s = f"{V('read', 'read')} {what}{on}"
    elif kind == "send":
        s = f"{V('send', 'sent')} the message{on}" + ("" if done else " (Enter)")
    elif kind == "press":
        s = (f"pressed Escape{on} — the window is closed" if done
             else f"press Escape{on} to close the window")
    elif kind == "key":
        m = action.get("media")
        if m == "erase":
            n = int(action.get("times") or 1)
            s = f"{V('delete', 'deleted')} {n} character{'s' if n != 1 else ''}{on}"
        elif m == "vol_down":
            s = f"{V('turn the volume down', 'turned the volume down')}{on}"
        elif m == "vol_up":
            s = f"{V('turn the volume up', 'turned the volume up')}{on}"
        elif m == "mute":
            s = f"{V('mute', 'muted')} the sound{on} (m)"
        elif m == "unmute":
            s = f"{V('unmute', 'unmuted')} the sound{on} (m)"
        elif m == "toggle":
            s = (f"pressed Space{on} — pause/resume" if done
                 else f"pause/resume{on}")
        else:
            key = _KEY_EN.get(action.get("key"), action.get("key"))
            s = f"{V('press', 'pressed')} {key}{on}"
    elif kind == "slider":
        unit = _SLIDER_UNIT_EN.get(str(action.get("slider_unit") or ""), "")
        if done:
            got = action.get("slider_done") or action.get("slider_value", "")
            s = f"set the {_q(action.get('slider_label', ''))} slider to {got}{on}"
        else:
            s = (f"drag the {_q(action.get('slider_label', ''))} slider to "
                 f"{action.get('slider_value', '')}{unit}{on}")
    elif kind == "media_vol":
        op = str(action.get("op") or "")
        got_v = str(action.get("vol_done") or "") if done else ""
        if got_v.startswith("vol:"):
            s = f"set the volume to {got_v[4:]}%{on}"
        elif got_v == "paused":
            s = f"paused the video{on}"
        elif got_v == "playing":
            s = f"resumed playback{on}"
        elif op == "toggle":
            s = f"{V('toggle', 'toggled')} playback{on}"
        elif op == "mute":
            s = f"{V('mute', 'muted')} the sound{on}"
        elif op == "unmute":
            s = f"{V('unmute', 'unmuted')} the sound{on}"
        elif done:
            s = f"changed the volume{on}"
        else:
            s = f"{'lower' if op.startswith('-') else 'raise'} the video volume{on}"
    elif kind == "scroll":
        if done:
            s = (f"started scrolling {_scroll_what(action)}{on} — say "
                 "\"stop\" and I'll stop")
        elif form == "ask":
            return (f"Start scrolling {_scroll_what(action)}{on}? "
                    "Say \"stop\" to stop.")
        else:
            s = f"scroll {_scroll_what(action)}{on}"
    elif kind == "scroll_stop":
        if done:
            s = {"bottom": "stopped scrolling — the page was already at the end",
                 "lost": "stopped scrolling — the tab was already closed",
                 "timeout": "stopped scrolling — it had already run out",
                 }.get(action.get("end_reason"), "stopped scrolling")
        else:
            s = "stop scrolling the page"
    elif kind == "tab_switch":
        s = (f"{V('switch to', 'switched to')} the "
             f"{_q(action.get('element') or host)} tab")
    elif kind == "zoom":
        got = int(action.get("zoom_done") or 0) if done else 0
        if got:
            s = f"set the zoom to {got}%{on}"
        else:
            verb = {"in": V("zoom in", "zoomed in"),
                    "out": V("zoom out", "zoomed out")}.get(
                action.get("dir"), V("reset the zoom", "reset the zoom"))
            s = f"{verb}{on}"
    elif kind == "tab_op":
        op = action.get("op")
        name = action.get("element") or host or ""
        if op in ("back", "forward"):
            where = f" in the {_q(name)} tab" if name else " in the current tab"
            s = ((V("go back", "went back") if op == "back"
                  else V("go forward", "went forward")) + where)
        else:
            what = f"the {_q(name)} tab" if name else "the current tab"
            verb = (V("reload", "reloaded") if op == "reload"
                    else V("close", "closed"))
            s = f"{verb} {what}"
    elif kind == "cart":
        prod = _q(action.get("product", ""))
        op = action.get("op")
        if done and op == "decrease" and action.get("qty_new") == 0:
            s = f"removed {prod} from the cart (it was the last one)"
        else:
            verb = {"remove": V("remove", "removed"),
                    "decrease": V("decrease", "decreased"),
                    "increase": V("increase", "increased"),
                    }.get(op, V("edit", "opened editing of"))
            s = f"{verb} {prod} in the cart{on}"
            if done and op in ("decrease", "increase") \
                    and action.get("qty_new") is not None:
                s += f" — now {action['qty_new']} in the cart"
    elif kind == "comp_edit":
        prod = action.get("product", "")
        if prod:
            s = (f"opened the composition editor for {_q(prod)}" if done
                 else f"change the composition of {_q(prod)}{on}")
        else:
            s = V(f"open the composition editor{on}",
                  "opened the composition editor")
    elif kind == "url" and action.get("search_query"):
        sq, site = _q(action["search_query"]), action.get("search_site", "")
        if action.get("direct"):
            s = f"{V('open', 'opened')} {sq} on {site}"
        else:
            s = V(f"search for {sq} on {site}", f"opened the search for {sq} on {site}")
    elif kind == "url":
        where = action.get("value", "")
        if form != "do" and host_fn is not None:
            try:
                where = host_fn(action)
            except Exception:
                pass
        s = f"{V('open', 'opened')} {where}"
    elif kind == "app":
        s = f"{V('launch', 'launched')} {_q(action.get('key', ''))}"
    else:
        s = f"{V('run', 'ran')} the task {_q(action.get('key') or kind or '')}"
    if form == "ask":
        return (s[0].upper() + s[1:] + "?") if s else "?"
    return s
