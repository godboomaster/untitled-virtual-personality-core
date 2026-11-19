//! Строки, которые собирает сама панель (меню трея, подсказка значка,
//! ошибки запуска). Интерфейс окна переводит себя сам (src/i18n.ts).

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Lang {
    Ru,
    En,
}

impl Lang {
    pub fn detect() -> Lang {
        match sys_locale::get_locale() {
            Some(l) if l.to_lowercase().starts_with("ru") => Lang::Ru,
            _ => Lang::En,
        }
    }
}

// (ключ, ru, en)
const TEXTS: &[(&str, &str, &str)] = &[
    ("robot_enable", "Запустить службу робота", "Start robot service"),
    ("robot_project", "Запустить в проекте", "Run with project"),
    ("robot_stop", "Остановить службу робота", "Stop robot service"),
    ("robot_poweroff", "Выключить Raspberry Pi", "Shut down Raspberry Pi"),
    ("robot_unknown", "Робот: состояние неизвестно", "Robot: status unknown"),
    ("robot_busy", "Робот: выполняю команду…", "Robot: working…"),
    ("robot_error", "Робот: ошибка — откройте панель", "Robot: error — open panel"),
    ("robot_connected", "Робот: подключён к проекту", "Robot: connected to project"),
    ("robot_connected_no_ssh", "Робот на связи; настройки по SSH недоступны", "Robot connected; SSH settings unavailable"),
    ("robot_active", "Робот: служба работает", "Robot: service is running"),
    ("robot_inactive", "Робот: служба остановлена", "Robot: service is stopped"),
    ("robot_failed", "Робот: служба упала", "Robot: service failed"),
    ("robot_unreachable", "Робот: нет связи по SSH", "Robot: SSH unavailable"),
    ("robot_shutdown_requested", "Pi приняла команду выключения", "Pi accepted the shutdown command"),
    ("robot_power_off", "Pi выключена — можно отключить питание", "Pi is off — safe to disconnect power"),
    ("robot_power_shutting", "Pi выключается…", "Pi is shutting down…"),
    ("robot_title_off", "Pi выкл.", "Pi off"),
    ("robot_err_power_off", "Pi выключена или выключается. Подключите питание и дождитесь появления Pi в сети.", "Pi is off or shutting down. Power it on and wait for it to reconnect."),
    ("robot_err_mode", "Управление роботом доступно только с --robot", "Robot controls require --robot"),
    ("robot_err_busy", "Дождитесь завершения команды робота", "Wait for the current robot operation"),
    ("robot_err_config", "Проверьте хост, пользователя SSH, порт и ID устройства", "Check the SSH host, user, port and device ID"),
    ("robot_err_idle_config", "Интервал — от 1 до 1440 минут, вероятность — от 0 до 100%", "Interval must be 1–1440 minutes and probability 0–100%"),
    ("robot_err_idle_reply", "Pi не подтвердила настройки автоповоротов", "The Pi did not confirm the idle head settings"),
    ("robot_err_ssh", "SSH не запустился или не ответил за 30 с. Проверьте питание Pi, сеть и доступ по ключу.", "SSH failed to start or timed out after 30 s. Check Pi power, network and key access."),
    ("robot_err_remote", "Робот: {}", "Robot: {}"),
    ("robot_err_shutdown_ack", "Pi не подтвердила планирование выключения. Она могла начать выключение; автоматического повтора не будет.", "The Pi did not confirm scheduling shutdown. It may be shutting down; this request will not be retried automatically."),
    ("robot_err_start", "Служба робота не запустилась. Проверьте journalctl --user -u vpc-robot.service на Pi.", "Robot service did not start. Check journalctl --user -u vpc-robot.service on the Pi."),
    ("robot_err_core", "Ядро проекта не запустилось — проверьте его состояние и лог в панели", "Project core did not start — check its status and log in the panel"),
    ("robot_err_connect", "Служба запущена, но робот не появился в ядре за 45 с. Проверьте ID устройства, VPC_CORE_URL и VPC_TOKEN в device.env на Pi.", "Service started, but the robot did not connect within 45 s. Check device ID, VPC_CORE_URL and VPC_TOKEN in device.env on the Pi."),
    ("open_panel", "Открыть панель", "Open panel"),
    ("open_web", "Веб-интерфейс", "Web interface"),
    ("start", "Запустить бота", "Start bot"),
    ("stop", "Остановить бота", "Stop bot"),
    ("restart", "Перезапустить", "Restart"),
    ("wake", "Слушать имя", "Listen for name"),
    ("tip_wake", "слушает имя", "listening for a name"),
    ("portrait", "Портрет на рабочем столе", "Portrait on desktop"),
    ("portrait_hide", "Убрать с рабочего стола", "Remove from desktop"),
    ("portrait_leave", "Убрать из разговора", "Remove from the conversation"),
    ("portrait_talk_end", "Закончить разговор", "End the conversation"),
    ("portrait_bubbles", "Реплики облачком", "Speech bubbles"),
    ("portrait_input", "Поле для сообщений", "Message box"),
    ("portrait_come", "Иди сюда", "Come here"),
    ("portrait_hideaway", "Спрячься", "Hide"),
    ("portrait_peek", "Выгляни", "Come out"),
    ("portrait_focus", "Посиди со мной", "Keep me company"),
    ("portrait_focus_min", "{} минут", "{} minutes"),
    ("portrait_focus_stop", "Хватит", "Stop"),
    ("portrait_physics", "Ходит по окнам", "Walks on windows"),
    ("portrait_anims", "Анимации", "Animations"),
    ("portrait_sounds", "Звуки анимаций", "Animation sounds"),
    ("portrait_summon", "Позвать портрет", "Call the portrait"),
    (
        "portrait_file_ask",
        "Вот файл «{}» — проанализируй его и скажи, что думаешь.",
        "Here is the file «{}» — read the full text and tell me what you think.",
    ),
    ("quit", "Выход", "Quit"),
    ("tip_running", "бот работает", "bot is running"),
    ("tip_external", "бот работает (запущен вне панели)", "bot is running (started outside the panel)"),
    ("tip_starting", "бот запускается", "bot is starting"),
    ("tip_stopping", "бот останавливается", "bot is stopping"),
    ("tip_stopped", "бот остановлен", "bot is stopped"),
    ("tip_crashed", "бот упал", "bot has crashed"),
    ("tip_no_answer", "бот не отвечает", "bot is not responding"),
    ("err_repo", "Папка проекта не найдена: {}", "Project folder not found: {}"),
    (
        "err_python",
        "Не найден Python с fastapi и uvicorn — укажите его в настройках",
        "No Python with fastapi and uvicorn found — set it in settings",
    ),
    ("err_python_path", "Python не найден: {}", "Python not found: {}"),
    (
        "err_node",
        "Не найден Node.js — укажите его в настройках",
        "Node.js not found — set it in settings",
    ),
    (
        "err_vite",
        "В web/ нет node_modules — выполните npm install в web/",
        "web/ has no node_modules — run npm install in web/",
    ),
    ("err_port", "Порт {} занят другой программой", "Port {} is taken by another program"),
    ("err_spawn", "Не удалось запустить: {}", "Failed to start: {}"),
    ("err_exit", "Процесс завершился: {}", "Process exited: {}"),
    ("err_code", "код {}", "code {}"),
    ("err_signal", "сигнал {}", "signal {}"),
    (
        "err_no_answer",
        "Запущен, но не отвечает уже {} с",
        "Started but not responding for {} s",
    ),
    (
        "err_restart_limit",
        "Падает снова и снова — автоперезапуск остановлен",
        "Keeps crashing — auto-restart stopped",
    ),
    ("err_stop", "Не удалось остановить процесс", "Failed to stop the process"),
    ("err_api_down", "Бот не запущен", "The bot is not running"),
    ("log_start", "запуск панелью", "started by the panel"),
    ("log_exit", "процесс завершился сам", "process exited on its own"),
    ("log_stop", "остановлен панелью", "stopped by the panel"),
];

pub fn t(lang: Lang, key: &str) -> String {
    TEXTS
        .iter()
        .find(|(k, _, _)| *k == key)
        .map(|(_, ru, en)| if lang == Lang::Ru { *ru } else { *en })
        .unwrap_or(key)
        .to_string()
}

pub fn tf(lang: Lang, key: &str, arg: impl std::fmt::Display) -> String {
    t(lang, key).replacen("{}", &arg.to_string(), 1)
}
