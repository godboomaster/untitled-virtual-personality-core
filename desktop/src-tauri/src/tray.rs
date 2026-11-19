//! Белый логотип с чёрными деталями; на macOS — 24 pt. Состояние — в подсказке
//! и панели. Меню — основные действия,
//! «Слушать имя» (голосовая активация микрофоном панели, voice.rs) и
//! «Портрет на рабочем столе» (portrait.rs).

use crate::panel;
use crate::portrait;
use crate::robot::{Action, PowerState, Service, View as RobotView};
use crate::state::AppState;
use crate::supervisor::{Phase, Summary};
use crate::texts::{t, Lang};
use crate::voice::VoiceSummary;
use std::sync::Mutex;
use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, Wry};

pub const TRAY_ID: &str = "main";
const SIZE: u32 = 48;
const LOGO_RGBA: &[u8; (SIZE * SIZE * 4) as usize] = include_bytes!("../icons/tray-logo-24@2x.rgba");
#[cfg(target_os = "macos")]
const LOGICAL_SIZE: f64 = 24.0;

pub struct Tray {
    icon: TrayIcon<Wry>,
    toggle: MenuItem<Wry>,
    restart: MenuItem<Wry>,
    wake: CheckMenuItem<Wry>,
    /// Последнее от монитора бота и от слушателя — подсказка значка из обоих
    last: Mutex<(Option<Summary>, Option<VoiceSummary>)>,
    lang: Lang,
    robot: Option<RobotItems>,
}

struct RobotItems {
    status: MenuItem<Wry>,
    enable: MenuItem<Wry>,
    project: MenuItem<Wry>,
    stop: MenuItem<Wry>,
    poweroff: MenuItem<Wry>,
}

fn is_up(phase: Phase) -> bool {
    matches!(phase, Phase::Running | Phase::Starting | Phase::External)
}

pub fn build(app: &AppHandle, lang: Lang) -> tauri::Result<Tray> {
    let item = |id: &str, key: &str| MenuItem::with_id(app, id, t(lang, key), true, None::<&str>);
    let open = item("open_panel", "open_panel")?;
    let web = item("open_web", "open_web")?;
    let toggle = item("toggle", "start")?;
    let restart = item("restart", "restart")?;
    let wake = CheckMenuItem::with_id(app, "wake", t(lang, "wake"), true, false, None::<&str>)?;
    let face = CheckMenuItem::with_id(app, "portrait", t(lang, "portrait"), true, false, None::<&str>)?;
    app.state::<AppState>().portrait.set_tray_item(face.clone());
    // Портрет к курсору (нет на столе — показать); то же — горячая клавиша
    let summon = item("portrait_summon", "portrait_summon")?;
    let quit = item("quit", "quit")?;
    let robot = if app.state::<AppState>().robot.is_some() {
        Some(RobotItems {
            status: MenuItem::with_id(app, "robot_status", t(lang, "robot_unknown"), false, None::<&str>)?,
            enable: item("robot_enable", "robot_enable")?,
            project: item("robot_project", "robot_project")?,
            stop: item("robot_stop", "robot_stop")?,
            poweroff: item("robot_poweroff", "robot_poweroff")?,
        })
    } else {
        None
    };
    let menu = if let Some(robot) = &robot {
        Menu::with_items(app, &[
            &robot.status,
            &PredefinedMenuItem::separator(app)?,
            &robot.enable,
            &robot.project,
            &robot.stop,
            &robot.poweroff,
            &PredefinedMenuItem::separator(app)?,
            &open,
            &web,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ])?
    } else {
        Menu::with_items(
            app,
            &[
                &open,
                &web,
                &PredefinedMenuItem::separator(app)?,
                &toggle,
                &restart,
                &PredefinedMenuItem::separator(app)?,
                &wake,
                &face,
                &summon,
                &PredefinedMenuItem::separator(app)?,
                &quit,
            ],
        )?
    };
    let icon = TrayIconBuilder::with_id(TRAY_ID)
        .icon(logo_icon())
        // Шаблон macOS перекрасил бы также глаза и заливку в один цвет.
        .icon_as_template(false)
        .tooltip("Virtual Persona")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, ev| match ev.id().as_ref() {
            "open_panel" => panel::show(app, None),
            "open_web" => crate::commands::open_web_url(app),
            "toggle" => {
                let st = app.state::<AppState>();
                if is_up(st.sup.summary().phase) {
                    st.sup.stop();
                } else {
                    st.sup.start();
                }
            }
            "restart" => app.state::<AppState>().sup.restart(),
            "wake" => {
                let st = app.state::<AppState>();
                let _ = st.voice.set_enabled(!st.sup.wake_enabled());
            }
            "portrait" => portrait::toggle(app),
            "portrait_summon" => portrait::summon(app),
            "robot_enable" | "robot_project" | "robot_stop" | "robot_poweroff" => {
                let st = app.state::<AppState>();
                if let Some(robot) = &st.robot {
                    let action = match ev.id().as_ref() {
                        "robot_enable" => Action::Enable,
                        "robot_project" => Action::Project,
                        "robot_stop" => Action::Stop,
                        _ => Action::Poweroff,
                    };
                    let _ = robot.run(action, st.sup.clone());
                    // Прогресс и ошибки видны в панели, даже при запуске из меню.
                    panel::show(app, None);
                }
            }
            "quit" => crate::commands::quit_app(app.clone()),
            _ => {}
        })
        .on_tray_icon_event(|tray, ev| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                rect,
                ..
            } = ev
            {
                panel::toggle(tray.app_handle(), Some(rect));
            }
        })
        .build(app)?;
    configure_size(&icon, robot.is_some())?;
    if robot.is_some() {
        let _ = icon.set_title(Some("Pi"));
        let _ = icon.set_tooltip(Some("Virtual Persona · Robot"));
    }
    Ok(Tray { icon, toggle, restart, wake, last: Mutex::new((None, None)), lang, robot })
}

impl Tray {
    pub fn refresh(&self, s: Summary) {
        // RGBA-логотип сохраняет белую заливку и чёрные детали при смене
        // состояния. Не пересоздаём изображение и не сбрасываем его размер.
        let _ = self.toggle.set_text(t(self.lang, if is_up(s.phase) { "stop" } else { "start" }));
        let _ = self.toggle.set_enabled(!s.busy);
        let _ = self.restart.set_enabled(!s.busy);
        self.last.lock().unwrap_or_else(|e| e.into_inner()).0 = Some(s);
        self.update_tooltip();
    }

    pub fn refresh_voice(&self, v: VoiceSummary) {
        let _ = self.wake.set_checked(v.enabled);
        self.last.lock().unwrap_or_else(|e| e.into_inner()).1 = Some(v);
        self.update_tooltip();
    }

    fn update_tooltip(&self) {
        if self.robot.is_some() {
            return;
        }
        let (s, v) = *self.last.lock().unwrap_or_else(|e| e.into_inner());
        let key = match s {
            Some(s) => match s.phase {
                Phase::Running | Phase::External if !s.responding => "tip_no_answer",
                Phase::Running => "tip_running",
                Phase::External => "tip_external",
                Phase::Starting => "tip_starting",
                Phase::Stopping => "tip_stopping",
                Phase::Stopped => "tip_stopped",
                Phase::Crashed => "tip_crashed",
            },
            None => "tip_stopped",
        };
        let mut tip = format!("Virtual Persona — {}", t(self.lang, key));
        if v.is_some_and(|v| v.listening) {
            tip = format!("{tip} · {}", t(self.lang, "tip_wake"));
        }
        let _ = self.icon.set_tooltip(Some(tip));
    }

    pub fn refresh_robot(&self, view: RobotView) {
        let Some(items) = &self.robot else { return };
        let key = if view.busy {
            "robot_busy"
        } else if view.error.is_some() {
            "robot_error"
        } else if view.power_state == PowerState::Off {
            "robot_power_off"
        } else if view.power_state == PowerState::ShuttingDown {
            "robot_power_shutting"
        } else if view.shutdown_requested {
            "robot_shutdown_requested"
        } else if view.connected == Some(true) && view.ssh_error.is_some() {
            "robot_connected_no_ssh"
        } else if view.ssh_error.is_some() {
            "robot_error"
        } else if view.connected == Some(true) && view.service == Service::Active {
            "robot_connected"
        } else {
            match view.service {
                Service::Active => "robot_active",
                Service::Inactive => "robot_inactive",
                Service::Failed => "robot_failed",
                Service::Unreachable => "robot_unreachable",
                Service::Unknown => "robot_unknown",
            }
        };
        let status = t(self.lang, key);
        let _ = items.status.set_text(&status);
        let enabled = !view.busy && !view.power_state.blocks_actions();
        let _ = items.enable.set_enabled(enabled);
        let _ = items.project.set_enabled(enabled);
        let _ = items.stop.set_enabled(enabled);
        let _ = items.poweroff.set_enabled(enabled);
        let title = match view.power_state {
            PowerState::Off => t(self.lang, "robot_title_off"),
            PowerState::ShuttingDown => "Pi…".into(),
            PowerState::Unknown => "Pi ?".into(),
            PowerState::On => "Pi".into(),
        };
        let _ = self.icon.set_title(Some(title));
        #[cfg(target_os = "macos")]
        {
            let off = view.power_state == PowerState::Off;
            let _ = self.icon.with_inner_tray_icon(move |inner| {
                let Some(mtm) = objc2::MainThreadMarker::new() else { return };
                let Some(item) = inner.ns_status_item() else { return };
                let _ = mtm;
                item.setLength(LOGICAL_SIZE + if off { 72.0 } else { 32.0 });
            });
        }
        let detail = view.error.as_deref().or(view.ssh_error.as_deref()).unwrap_or(&status);
        let _ = self.icon.set_tooltip(Some(format!("Virtual Persona · Robot — {detail}")));
    }
}

/// Версия №118: белые призрак и скорлупа, чёрные глаза, обводка и трещины.
/// Сохраняем исходные цвета и прозрачность PNG; 48 px для Retina 24 pt.
fn logo_icon() -> Image<'static> {
    Image::new_owned(LOGO_RGBA.to_vec(), SIZE, SIZE)
}

fn configure_size(icon: &TrayIcon<Wry>, robot_mode: bool) -> tauri::Result<()> {
    #[cfg(target_os = "macos")]
    return icon.with_inner_tray_icon(move |inner| {
        let Some(mtm) = objc2::MainThreadMarker::new() else { return };
        let Some(item) = inner.ns_status_item() else { return };
        let Some(button) = item.button(mtm) else { return };
        let Some(image) = button.image() else { return };
        image.setSize(objc2_foundation::NSSize::new(LOGICAL_SIZE, LOGICAL_SIZE));
        button.setImage(Some(&image));
        // В режиме робота оставляем место для подписи Pi возле логотипа.
        item.setLength(LOGICAL_SIZE + if robot_mode { 32.0 } else { 8.0 });
    });
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (icon, robot_mode);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn logo_has_retina_resolution_and_transparent_background() {
        let icon = logo_icon();
        assert_eq!((icon.width(), icon.height()), (48, 48));
        assert_eq!(icon.rgba()[3], 0, "outside background must be transparent");
        assert!(icon.rgba().chunks_exact(4).any(|p| p[3] == 255), "ink must remain visible");
        assert!(icon.rgba().chunks_exact(4).any(|p| p[3] > 0 && p[3] < 255), "retain antialiasing");
    }

    #[test]
    fn logo_preserves_white_fill_and_black_eyes() {
        let icon = logo_icon();
        let pixel = |x: usize, y: usize| &icon.rgba()[(y * SIZE as usize + x) * 4..][..4];
        for (x, y) in [(24, 12), (24, 40)] {
            let p = pixel(x, y);
            assert!(p[..3].iter().all(|c| *c > 240) && p[3] > 240, "face and shell must stay white");
        }
        let eye = pixel(21, 12);
        assert!(eye[..3].iter().all(|c| *c < 32) && eye[3] > 240, "eyes must stay opaque black");
    }
}
