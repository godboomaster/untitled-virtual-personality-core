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
    ("open_panel", "Открыть панель", "Open panel"),
    ("open_web", "Веб-интерфейс", "Web interface"),
    ("start", "Запустить бота", "Start bot"),
    ("stop", "Остановить бота", "Stop bot"),
    ("restart", "Перезапустить", "Restart"),
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
