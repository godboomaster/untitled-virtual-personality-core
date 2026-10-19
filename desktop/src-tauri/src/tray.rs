//! Значок в трее: цвет — состояние бота, меню — основные действия и
//! «Слушать имя» (голосовая активация микрофоном панели, voice.rs).

use crate::panel;
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
const SIZE: u32 = 44;

pub struct Tray {
    icon: TrayIcon<Wry>,
    toggle: MenuItem<Wry>,
    restart: MenuItem<Wry>,
    wake: CheckMenuItem<Wry>,
    /// Последнее от монитора бота и от слушателя — подсказка значка из обоих
    last: Mutex<(Option<Summary>, Option<VoiceSummary>)>,
    lang: Lang,
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
    let quit = item("quit", "quit")?;
    let menu = Menu::with_items(
        app,
        &[
            &open,
            &web,
            &PredefinedMenuItem::separator(app)?,
            &toggle,
            &restart,
            &PredefinedMenuItem::separator(app)?,
            &wake,
            &PredefinedMenuItem::separator(app)?,
            &quit,
        ],
    )?;
    let icon = TrayIconBuilder::with_id(TRAY_ID)
        .icon(status_icon(Phase::Stopped, false))
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
    Ok(Tray { icon, toggle, restart, wake, last: Mutex::new((None, None)), lang })
}

impl Tray {
    pub fn refresh(&self, s: Summary) {
        let _ = self.icon.set_icon(Some(status_icon(s.phase, s.responding)));
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
}

/// Кольцо с точкой цвета состояния; остановлен — пустое серое кольцо.
/// Рисуется кодом: цветной значок не зависит от темы строки меню
fn status_icon(phase: Phase, responding: bool) -> Image<'static> {
    let (rgb, filled) = match phase {
        Phase::Running | Phase::External if responding => ([52, 199, 89], true),
        Phase::Running | Phase::External | Phase::Starting | Phase::Stopping => ([255, 159, 10], true),
        Phase::Crashed => ([255, 69, 58], true),
        Phase::Stopped => ([142, 142, 147], false),
    };
    let n = SIZE as usize;
    let c = (SIZE as f32 - 1.0) / 2.0;
    let outer = SIZE as f32 * 0.45;
    let inner = outer - SIZE as f32 * 0.11;
    let dot = SIZE as f32 * 0.19;
    let mut px = vec![0u8; n * n * 4];
    for y in 0..n {
        for x in 0..n {
            let d = ((x as f32 - c).powi(2) + (y as f32 - c).powi(2)).sqrt();
            let ring = (outer + 0.5 - d).clamp(0.0, 1.0) * (d - inner + 0.5).clamp(0.0, 1.0);
            let core = if filled { (dot + 0.5 - d).clamp(0.0, 1.0) } else { 0.0 };
            let a = ring.max(core);
            let i = (y * n + x) * 4;
            px[i..i + 4].copy_from_slice(&[rgb[0], rgb[1], rgb[2], (a * 255.0).round() as u8]);
        }
    }
    Image::new_owned(px, SIZE, SIZE)
}
