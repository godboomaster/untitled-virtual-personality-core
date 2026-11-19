//! Панель Virtual Persona: значок в трее и окно-панель. Запускает бэкенд
//! бота (python -m app.main api) и веб-интерфейс (vite из web/), следит за
//! ними и показывает состояние браузеров бота, Ollama и памяти. Слушает
//! имя персоны своим микрофоном (голосовая активация, voice.rs). Держит
//! живой портрет персоны на рабочем столе (portrait.rs).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;
mod config;
mod input_watch;
mod logs;
mod panel;
mod platform;
mod portrait;
mod probe;
#[path = "../../../../vpc-robot/desktop/robot.rs"]
mod robot;
mod state;
mod supervisor;
mod texts;
mod tray;
mod voice;

use state::AppState;
use std::sync::{Arc, Mutex};
use tauri::{Manager, RunEvent, WindowEvent};
use tauri_plugin_autostart::MacosLauncher;
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

/// Флаг запуска при входе в систему: тогда панель не открывается сама
const AUTOSTART_ARG: &str = "--autostart";

/// «Позвать портрет»: Ctrl+Option+P (macOS), Ctrl+Alt+P (Windows)
fn summon_shortcut() -> Shortcut {
    Shortcut::new(Some(Modifiers::CONTROL | Modifiers::ALT), Code::KeyP)
}

fn main() {
    let lang = texts::Lang::detect();
    let robot_mode = robot::requested(std::env::args());
    let mut context = tauri::generate_context!();
    if robot_mode {
        // Отдельная single-instance блокировка, настройки и автозапуск:
        // обычная панель и трей робота могут работать одновременно.
        context.config_mut().identifier = robot::APP_ID.into();
        context.config_mut().product_name = Some(robot::APP_NAME.into());
        context.package_info_mut().name = robot::APP_NAME.into();
    }
    let autostart_args = if robot_mode { vec![AUTOSTART_ARG, robot::ARG] } else { vec![AUTOSTART_ARG] };
    let app = tauri::Builder::default()
        // Второй запуск (ярлык, Finder) — открыть панель уже работающей копии
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| panel::show(app, None)))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(autostart_args)))
        // «Позвать портрет» с клавиатуры: к курсору, нет на столе — показать
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, ev| {
                    if ev.state() == ShortcutState::Pressed && *shortcut == summon_shortcut() {
                        portrait::summon(app);
                    }
                })
                .build(),
        )
        .setup(move |app| {
            // Без значка в Dock: панель живёт в строке меню
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            let config_path = app.path().app_config_dir()?.join("config.json");
            let fallback_logs = app.path().app_log_dir()?;
            let cfg = if robot_mode && !config_path.exists() {
                let normal_path = app.path().config_dir()?.join("dev.virtualpersona.panel/config.json");
                let mut cfg = config::load(&normal_path);
                // Сама по себе иконка робота не запускает ядро и микрофон.
                cfg.start_on_launch = false;
                cfg.wake_listen = Some(false);
                cfg
            } else {
                config::load(&config_path)
            };
            let start_now = cfg.start_on_launch;
            let sup = supervisor::Supervisor::new(cfg, config_path, fallback_logs, lang);
            let voice = voice::Voice::new(sup.clone(), lang);
            let portrait = portrait::Portrait::new(
                app.path().app_config_dir()?.join("portrait.json"),
                app.path().app_cache_dir()?,
            );
            app.manage(AppState {
                sup: sup.clone(),
                voice: voice.clone(),
                portrait,
                robot: if robot_mode {
                    Some(robot::Robot::new(app.path().app_config_dir()?.join("robot.json"), lang))
                } else {
                    None
                },
                sys: Arc::new(Mutex::new(sysinfo::System::new())),
                hidden_at: Arc::new(Mutex::new(None)),
                lang,
            });

            let tray = Arc::new(tray::build(app.handle(), lang)?);
            let t2 = tray.clone();
            let t3 = tray.clone();
            sup.spawn_monitor(move |summary| tray.refresh(summary));
            if let Some(robot) = &app.state::<AppState>().robot {
                robot.spawn_monitor(sup.clone(), move |view| t3.refresh_robot(view));
            }
            // Слушание имени — с запуском панели, если включено (не ждёт бота:
            // имена слушатель возьмёт, когда ядро ответит)
            voice.spawn_monitor(move |v| t2.refresh_voice(v));
            // Разговор голосом: имя прозвучало, чат персоны в вебе не открыт —
            // её портрет на стол, пока разговор идёт
            let handle = app.handle().clone();
            voice.set_talk_hook(Arc::new(move |talk| portrait::on_voice(&handle, talk)));
            // Кнопке микрофона у портретов — состояние слушателя
            let handle = app.handle().clone();
            voice.set_view_hook(Arc::new(move |view| portrait::on_mic(&handle, view)));
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
            // Меню правой кнопки по портрету (пункты трея — в tray.rs)
            app.on_menu_event(|app, ev| portrait::on_menu(app, ev.id().as_ref()));
            if !robot_mode {
                portrait::restore(app.handle());
                portrait::watch_web(app.handle().clone());
            }
            // Клавиша занята другой программой — без неё: есть меню и трей
            if !robot_mode {
                if let Err(e) = app.global_shortcut().register(summon_shortcut()) {
                    eprintln!("портрет: горячая клавиша не зарегистрирована: {e}");
                }
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
            commands::set_wake,
            commands::set_wake_lang,
            commands::wake_calibrate,
            commands::wake_forget_voice,
            commands::hide_panel,
            commands::quit,
            robot::robot_save,
            robot::robot_idle_save,
            robot::robot_action,
            portrait::portrait_load,
            portrait::portrait_mood,
            portrait::portrait_hit,
            portrait::portrait_drag,
            portrait::portrait_resize,
            portrait::portrait_menu,
            portrait::portrait_pet,
            portrait::portrait_open_chat,
            portrait::portrait_send,
            portrait::portrait_history,
            portrait::portrait_player_status,
            portrait::portrait_player_layout,
            portrait::portrait_player_control,
            portrait::portrait_focus,
            portrait::portrait_visible,
            portrait::portrait_info,
            portrait::portrait_set,
            portrait::portrait_walker,
            portrait::portrait_keys,
            portrait::portrait_keys_settings,
            portrait::portrait_gait,
            portrait::portrait_stroll,
            portrait::portrait_hop,
            portrait::portrait_come,
            portrait::portrait_hide,
            portrait::portrait_peek,
            portrait::portrait_peek_end,
            portrait::portrait_stop,
            portrait::portrait_voice,
            portrait::portrait_mic,
            portrait::portrait_mic_toggle,
        ])
        .build(context)
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
