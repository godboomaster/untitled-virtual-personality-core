//! Панель Virtual Persona: значок в трее и окно-панель. Запускает бэкенд
//! бота (python -m app.main api) и веб-интерфейс (vite из web/), следит за
//! ними и показывает состояние браузеров бота, Ollama и памяти.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;
mod config;
mod logs;
mod panel;
mod platform;
mod probe;
mod state;
mod supervisor;
mod texts;
mod tray;

use state::AppState;
use std::sync::{Arc, Mutex};
use tauri::{Manager, RunEvent, WindowEvent};
use tauri_plugin_autostart::MacosLauncher;

/// Флаг запуска при входе в систему: тогда панель не открывается сама
const AUTOSTART_ARG: &str = "--autostart";

fn main() {
    let lang = texts::Lang::detect();
    let app = tauri::Builder::default()
        // Второй запуск (ярлык, Finder) — открыть панель уже работающей копии
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| panel::show(app, None)))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec![AUTOSTART_ARG])))
        .setup(move |app| {
            // Без значка в Dock: панель живёт в строке меню
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            let config_path = app.path().app_config_dir()?.join("config.json");
            let fallback_logs = app.path().app_log_dir()?;
            let cfg = config::load(&config_path);
            let start_now = cfg.start_on_launch;
            let sup = supervisor::Supervisor::new(cfg, config_path, fallback_logs, lang);
            app.manage(AppState {
                sup: sup.clone(),
                sys: Arc::new(Mutex::new(sysinfo::System::new())),
                hidden_at: Arc::new(Mutex::new(None)),
                lang,
            });

            let tray = tray::build(app.handle(), lang)?;
            sup.spawn_monitor(move |summary| tray.refresh(summary));
            if start_now {
                sup.start();
            }

            if let Some(win) = app.get_webview_window(panel::LABEL) {
                let handle = app.handle().clone();
                win.on_window_event(move |e| {
                    if let WindowEvent::Focused(false) = e {
                        panel::hide(&handle);
                    }
                });
            }
            if !std::env::args().any(|a| a == AUTOSTART_ARG) {
                panel::show(app.handle(), None);
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::snapshot,
            commands::start,
            commands::stop,
            commands::restart,
            commands::rescue,
            commands::open_web,
            commands::open_logs,
            commands::get_settings,
            commands::save_settings,
            commands::detect_python,
            commands::set_autostart,
            commands::hide_panel,
            commands::quit,
        ])
        .build(tauri::generate_context!())
        .expect("не удалось запустить панель");

    app.run(|_app, event| {
        // Окно панели только прячется; приложение живёт в трее до «Выход»
        if let RunEvent::ExitRequested { api, code, .. } = event {
            if code.is_none() {
                api.prevent_exit();
            }
        }
    });
}
