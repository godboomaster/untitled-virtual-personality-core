//! Окно-панель: открывается у значка в трее (сверху на macOS, у панели задач
//! на Windows), прячется при потере фокуса — как панель Wi-Fi.

use crate::state::AppState;
use crate::tray::TRAY_ID;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, Rect, WebviewWindow};

pub const LABEL: &str = "panel";

pub fn toggle(app: &AppHandle, anchor: Option<Rect>) {
    let Some(win) = app.get_webview_window(LABEL) else { return };
    if win.is_visible().unwrap_or(false) {
        return hide(app);
    }
    // Клик по значку при открытой панели: окно сперва теряет фокус и
    // прячется, затем приходит сам клик — он не должен открыть панель снова
    let st = app.state::<AppState>();
    let just_hidden = st
        .hidden_at
        .lock()
        .ok()
        .and_then(|t| *t)
        .map(|t| t.elapsed() < Duration::from_millis(400))
        .unwrap_or(false);
    if !just_hidden {
        show(app, anchor);
    }
}

pub fn show(app: &AppHandle, anchor: Option<Rect>) {
    let Some(win) = app.get_webview_window(LABEL) else { return };
    let anchor = anchor.or_else(|| app.tray_by_id(TRAY_ID).and_then(|t| t.rect().ok().flatten()));
    place(&win, anchor);
    let _ = win.show();
    let _ = win.set_focus();
    let _ = win.emit("panel-visible", true);
}

pub fn hide(app: &AppHandle) {
    let Some(win) = app.get_webview_window(LABEL) else { return };
    if !win.is_visible().unwrap_or(false) {
        return;
    }
    let _ = win.hide();
    if let Ok(mut t) = app.state::<AppState>().hidden_at.lock() {
        *t = Some(Instant::now());
    }
    let _ = win.emit("panel-visible", false);
}

fn clamp(v: f64, lo: f64, hi: f64) -> f64 {
    if hi < lo {
        lo
    } else {
        v.max(lo).min(hi)
    }
}

/// Под значком, если он у верхнего края экрана, иначе над ним; без значка —
/// в углу экрана, где обычно трей. Всегда в пределах рабочей области
fn place(win: &WebviewWindow, anchor: Option<Rect>) {
    let Ok(size) = win.outer_size() else { return };
    let (w, h) = (size.width as f64, size.height as f64);
    let scale = win.scale_factor().unwrap_or(1.0);
    let anchor = anchor.map(|r| {
        let p = r.position.to_physical::<f64>(scale);
        let s = r.size.to_physical::<f64>(scale);
        (p.x, p.y, s.width, s.height)
    });
    let monitor = match anchor {
        Some((x, y, aw, ah)) => win.monitor_from_point(x + aw / 2.0, y + ah / 2.0).ok().flatten(),
        None => None,
    }
    .or_else(|| win.primary_monitor().ok().flatten());
    let Some(m) = monitor else { return };
    let wa = m.work_area();
    let (wx, wy) = (wa.position.x as f64, wa.position.y as f64);
    let (ww, wh) = (wa.size.width as f64, wa.size.height as f64);
    let gap = 8.0 * m.scale_factor();
    let (ax, ay, aw, ah) = anchor.unwrap_or_else(|| {
        let right = wx + ww - w / 2.0 - gap;
        if cfg!(target_os = "macos") {
            (right, wy, 0.0, 0.0)
        } else {
            (right, wy + wh, 0.0, 0.0)
        }
    });
    let x = clamp(ax + aw / 2.0 - w / 2.0, wx + gap, wx + ww - w - gap);
    let below = ay + ah / 2.0 < wy + wh / 2.0;
    let y = if below { ay + ah + gap } else { ay - h - gap };
    let y = clamp(y, wy + gap, wy + wh - h - gap);
    let _ = win.set_position(PhysicalPosition::new(x.round() as i32, y.round() as i32));
}
