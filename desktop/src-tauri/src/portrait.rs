//! Портрет на рабочем столе: живой портрет персоны (тот же движок, что в
//! голосовом режиме веба, web/src/portrait/LivePortrait.tsx) в прозрачном
//! окне без рамки поверх всех окон. Страница окна — src/portrait/.
//!
//! Клики проходят сквозь окно к тому, что под ним, кроме самой фигуры: окно
//! «прозрачно для мыши», пока курсор не над ней. Панель раз в POLL сообщает
//! странице, где курсор (заодно портрет смотрит на него по всему экрану),
//! страница по прозрачности кадра отвечает, над фигурой ли он (portrait_hit).
//! Над фигурой: клик — погладить, перетаскивание — переставить, колёсико —
//! размер, правая кнопка — меню (чей портрет, убрать с рабочего стола).
//!
//! Чей портрет, где окно и какого размера — portrait.json в папке настроек,
//! отдельно от config.json: место меняется перетаскиванием, а настройки окно
//! панели сохраняет целиком. Атласы последней загрузки — в кэше приложения:
//! без бота портрет остаётся на столе.

use crate::config::ApiEnv;
use crate::probe;
use crate::state::AppState;
use crate::texts::t;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem};
use tauri::{
    AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, PhysicalPosition, State, WebviewUrl,
    WebviewWindowBuilder, WindowEvent, Wry,
};

pub const LABEL: &str = "portrait";
/// Как часто сверять курсор: и взгляд по экрану, и «над фигурой ли»
const POLL: Duration = Duration::from_millis(33);
const SIZE_DEFAULT: f64 = 240.0;
const SIZE_MIN: f64 = 120.0;
const SIZE_MAX: f64 = 640.0;
/// Место по умолчанию — у правого нижнего угла рабочей области, с отступом справа
const MARGIN: f64 = 24.0;
/// Место сохраняется, когда окно столько не двигалось
const SAVE_AFTER: Duration = Duration::from_millis(700);
const MENU_PERSONA: &str = "portrait_persona:";
const MENU_HIDE: &str = "portrait_hide";

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default, rename_all = "camelCase")]
pub struct Placement {
    pub shown: bool,
    /// id персоны; пусто — первая с портретом
    pub persona: String,
    /// Сторона окна, логические px
    pub size: f64,
    /// Левый верхний угол, логические px; нет — у правого нижнего угла экрана
    pub x: Option<f64>,
    pub y: Option<f64>,
}

impl Default for Placement {
    fn default() -> Self {
        Placement { shown: false, persona: String::new(), size: SIZE_DEFAULT, x: None, y: None }
    }
}

#[derive(Clone)]
pub struct Portrait {
    place: Arc<Mutex<Placement>>,
    path: PathBuf,
    cache_dir: PathBuf,
    /// Номер окна: поток слежения за курсором закрытого окна видит, что устарел
    gen: Arc<AtomicU64>,
    moved_at: Arc<Mutex<Option<Instant>>>,
    saving: Arc<AtomicBool>,
    /// Галочка «Портрет на рабочем столе» в меню трея
    tray_item: Arc<Mutex<Option<CheckMenuItem<Wry>>>>,
}

fn lk<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// id персоны в пути запроса — как SAFE_ID_RE ядра (app/api/security.py)
fn safe_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
}

impl Portrait {
    pub fn new(path: PathBuf, cache_dir: PathBuf) -> Self {
        let place = fs::read_to_string(&path)
            .ok()
            .and_then(|s| serde_json::from_str::<Placement>(&s).ok())
            .unwrap_or_default();
        Portrait {
            place: Arc::new(Mutex::new(place)),
            path,
            cache_dir,
            gen: Arc::new(AtomicU64::new(0)),
            moved_at: Arc::new(Mutex::new(None)),
            saving: Arc::new(AtomicBool::new(false)),
            tray_item: Arc::new(Mutex::new(None)),
        }
    }

    pub fn placement(&self) -> Placement {
        lk(&self.place).clone()
    }

    fn update(&self, f: impl FnOnce(&mut Placement)) {
        f(&mut lk(&self.place));
        self.save();
    }

    fn save(&self) {
        let pl = self.placement();
        let Some(dir) = self.path.parent() else { return };
        let _ = fs::create_dir_all(dir);
        let tmp = self.path.with_extension("json.tmp");
        if let Ok(body) = serde_json::to_string_pretty(&pl) {
            if fs::write(&tmp, body).is_ok() {
                let _ = fs::rename(&tmp, &self.path);
            }
        }
    }

    /// Окно двигали или меняли размер — сохранить, когда успокоится
    fn save_soon(&self) {
        *lk(&self.moved_at) = Some(Instant::now());
        if self.saving.swap(true, Ordering::SeqCst) {
            return;
        }
        let me = self.clone();
        std::thread::spawn(move || {
            loop {
                std::thread::sleep(SAVE_AFTER);
                if lk(&me.moved_at).map(|t| t.elapsed() >= SAVE_AFTER).unwrap_or(true) {
                    break;
                }
            }
            me.saving.store(false, Ordering::SeqCst);
            me.save();
        });
    }

    fn moved(&self, pos: PhysicalPosition<i32>, scale: f64) {
        {
            let mut pl = lk(&self.place);
            pl.x = Some(pos.x as f64 / scale);
            pl.y = Some(pos.y as f64 / scale);
        }
        self.save_soon();
    }

    pub fn set_tray_item(&self, item: CheckMenuItem<Wry>) {
        let _ = item.set_checked(self.placement().shown);
        *lk(&self.tray_item) = Some(item);
    }

    fn sync_tray(&self, shown: bool) {
        if let Some(item) = lk(&self.tray_item).as_ref() {
            let _ = item.set_checked(shown);
        }
    }

    fn cache_file(&self, persona: &str) -> PathBuf {
        self.cache_dir.join(format!("portrait-{persona}.json"))
    }

    fn read_cache(&self, persona: &str) -> Option<Value> {
        fs::read_to_string(self.cache_file(persona)).ok().and_then(|s| serde_json::from_str(&s).ok())
    }

    fn write_cache(&self, persona: &str, v: &Value) {
        let _ = fs::create_dir_all(&self.cache_dir);
        let file = self.cache_file(persona);
        let tmp = file.with_extension("json.tmp");
        if fs::write(&tmp, v.to_string()).is_ok() {
            let _ = fs::rename(&tmp, &file);
        }
    }

    fn drop_cache(&self, persona: &str) {
        let _ = fs::remove_file(self.cache_file(persona));
    }
}

/// Портрет был на столе при выходе — вернуть
pub fn restore(app: &AppHandle) {
    if app.state::<AppState>().portrait.placement().shown {
        show(app);
    }
}

pub fn toggle(app: &AppHandle) {
    if app.get_webview_window(LABEL).is_some() {
        hide(app);
    } else {
        show(app);
    }
}

pub fn show(app: &AppHandle) {
    let st = app.state::<AppState>();
    st.portrait.update(|pl| pl.shown = true);
    st.portrait.sync_tray(true);
    // Не из обработчика меню: на Windows создание окна там виснет (WebView2)
    let app = app.clone();
    std::thread::spawn(move || open(&app));
}

pub fn hide(app: &AppHandle) {
    let st = app.state::<AppState>();
    st.portrait.update(|pl| pl.shown = false);
    st.portrait.gen.fetch_add(1, Ordering::SeqCst);
    st.portrait.sync_tray(false);
    if let Some(w) = app.get_webview_window(LABEL) {
        let _ = w.destroy();
    }
}

/// Пункт меню портрета (правая кнопка по голове): чей портрет, убрать
pub fn on_menu(app: &AppHandle, id: &str) {
    if id == MENU_HIDE {
        return hide(app);
    }
    if let Some(persona) = id.strip_prefix(MENU_PERSONA) {
        let persona = persona.to_string();
        app.state::<AppState>().portrait.update(|pl| pl.persona = persona);
        let _ = app.emit_to(LABEL, "portrait-reload", ());
    }
}

fn open(app: &AppHandle) {
    if let Some(w) = app.get_webview_window(LABEL) {
        let _ = w.show();
        return;
    }
    let p = app.state::<AppState>().portrait.clone();
    let pl = p.placement();
    let size = pl.size.clamp(SIZE_MIN, SIZE_MAX);
    let (x, y) = match (pl.x, pl.y) {
        (Some(x), Some(y)) if on_screen(app, x, y, size) => (x, y),
        _ => corner(app, size),
    };
    let gen = p.gen.fetch_add(1, Ordering::SeqCst) + 1;
    let built = WebviewWindowBuilder::new(app, LABEL, WebviewUrl::App("portrait.html".into()))
        .title("Virtual Persona")
        .inner_size(size, size)
        .position(x, y)
        .decorations(false)
        .transparent(true)
        .shadow(false)
        .resizable(false)
        .always_on_top(true)
        .visible_on_all_workspaces(true)
        .skip_taskbar(true)
        // Клик по голове не уводит фокус из окна, где вы работаете
        .focused(false)
        .focusable(false)
        .accept_first_mouse(true)
        .build();
    let win = match built {
        Ok(w) => w,
        Err(e) => {
            eprintln!("портрет на рабочем столе: окно не открылось: {e}");
            return;
        }
    };
    // Сквозь окно, пока страница не скажет, что курсор над фигурой
    let _ = win.set_ignore_cursor_events(true);
    let w2 = win.clone();
    win.on_window_event(move |e| {
        if let WindowEvent::Moved(pos) = e {
            p.moved(*pos, w2.scale_factor().unwrap_or(1.0));
        }
    });
    watch_cursor(app.clone(), gen);
}

#[derive(Clone, Serialize)]
struct Cursor {
    x: f64,
    y: f64,
}

/// Курсор в координатах страницы окна (логические px, может быть за окном) —
/// при каждом сдвиге курсора или окна
fn watch_cursor(app: AppHandle, gen: u64) {
    std::thread::spawn(move || {
        let mut last: Option<(i64, i64)> = None;
        loop {
            std::thread::sleep(POLL);
            if app.state::<AppState>().portrait.gen.load(Ordering::SeqCst) != gen {
                return;
            }
            let Some(win) = app.get_webview_window(LABEL) else { return };
            let (Ok(c), Ok(o), Ok(s)) = (app.cursor_position(), win.inner_position(), win.scale_factor()) else {
                continue;
            };
            let (x, y) = ((c.x - o.x as f64) / s, (c.y - o.y as f64) / s);
            let key = (x.round() as i64, y.round() as i64);
            if last != Some(key) {
                last = Some(key);
                let _ = app.emit_to(LABEL, "portrait-cursor", Cursor { x, y });
            }
        }
    });
}

/// Центр окна на каком-нибудь мониторе (монитор могли отключить)
fn on_screen(app: &AppHandle, x: f64, y: f64, size: f64) -> bool {
    let (cx, cy) = (x + size / 2.0, y + size / 2.0);
    app.available_monitors()
        .map(|ms| {
            ms.iter().any(|m| {
                let s = m.scale_factor();
                let (mx, my) = (m.position().x as f64 / s, m.position().y as f64 / s);
                let (mw, mh) = (m.size().width as f64 / s, m.size().height as f64 / s);
                cx >= mx && cx < mx + mw && cy >= my && cy < my + mh
            })
        })
        .unwrap_or(true)
}

/// Правый нижний угол рабочей области основного монитора: голова «стоит» на
/// панели задач или над Dock
fn corner(app: &AppHandle, size: f64) -> (f64, f64) {
    let Some(m) = app.primary_monitor().ok().flatten() else { return (MARGIN, MARGIN) };
    let s = m.scale_factor();
    let wa = m.work_area();
    let (x, y) = (wa.position.x as f64 / s, wa.position.y as f64 / s);
    let (w, h) = (wa.size.width as f64 / s, wa.size.height as f64 / s);
    (x + w - size - MARGIN, y + h - size)
}

/// Персоны ядра (id, имя) в порядке списка; бот не отвечает — None
fn persona_names(api: &ApiEnv) -> Option<Vec<(String, String)>> {
    let v = probe::api_get(api, "/api/personas", 4000).ok()?;
    Some(
        v.as_array()?
            .iter()
            .filter_map(|p| {
                let id = p["id"].as_str()?.to_string();
                let name = p["name"].as_str().filter(|n| !n.is_empty()).unwrap_or(&id).to_string();
                Some((id, name))
            })
            .collect(),
    )
}

/// id персон с живым портретом; ядро старее панели (нет /api/portraits) — None
fn with_portrait(api: &ApiEnv) -> Option<Vec<String>> {
    let v = probe::api_get(api, "/api/portraits", 4000).ok()?;
    Some(v["personas"].as_array()?.iter().filter_map(|x| x.as_str().map(str::to_string)).collect())
}

/// Ответ странице: {persona, name, data (GET /api/personas/{p}/portrait),
/// offline — бот не ответил (data — из кэша, если был), gone — персоны нет}
fn load(st: &AppState) -> Value {
    let api = st.sup.api();
    let p = &st.portrait;
    let mut persona = p.placement().persona;
    if persona.is_empty() {
        if let Some(first) = with_portrait(&api).and_then(|ids| ids.into_iter().next()) {
            persona = first.clone();
            p.update(|pl| pl.persona = first);
        }
    }
    if !safe_id(&persona) {
        return json!({ "persona": null, "data": null, "offline": !probe::health(&api) });
    }
    match probe::api_get(&api, &format!("/api/personas/{persona}/portrait"), 20_000) {
        Ok(data) => {
            let name = persona_names(&api)
                .and_then(|list| list.into_iter().find(|(id, _)| *id == persona).map(|(_, n)| n))
                .unwrap_or_else(|| persona.clone());
            let out = json!({ "persona": persona, "name": name, "data": data, "offline": false });
            p.write_cache(&persona, &out);
            out
        }
        Err(e) if e.starts_with("HTTP 404") => {
            p.drop_cache(&persona);
            json!({ "persona": persona, "name": persona, "data": null, "gone": true, "offline": false })
        }
        Err(_) => match p.read_cache(&persona) {
            Some(mut v) => {
                v["offline"] = json!(true);
                v
            }
            None => json!({ "persona": persona, "name": persona, "data": null, "offline": true }),
        },
    }
}

/// Кого предложить в меню: персоны с портретом; бот не отвечает — текущая
fn menu_personas(st: &AppState) -> Vec<(String, String)> {
    let api = st.sup.api();
    let current = st.portrait.placement().persona;
    match (persona_names(&api), with_portrait(&api)) {
        (Some(list), Some(ids)) => list.into_iter().filter(|(id, _)| ids.contains(id)).collect(),
        (Some(list), None) => list,
        (None, _) => st
            .portrait
            .read_cache(&current)
            .and_then(|v| v["name"].as_str().map(|n| vec![(current.clone(), n.to_string())]))
            .unwrap_or_default(),
    }
}

#[tauri::command]
pub async fn portrait_load(state: State<'_, AppState>) -> Result<Value, String> {
    let st = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || load(&st)).await.map_err(|e| e.to_string())
}

/// Курсор над фигурой — окно ловит мышь; мимо — клики проходят насквозь
#[tauri::command]
pub fn portrait_hit(app: AppHandle, hit: bool) {
    if let Some(w) = app.get_webview_window(LABEL) {
        let _ = w.set_ignore_cursor_events(!hit);
    }
}

/// Перетаскивание за голову (кнопка зажата и мышь сдвинулась)
#[tauri::command]
pub fn portrait_drag(app: AppHandle) {
    if let Some(w) = app.get_webview_window(LABEL) {
        let _ = w.start_dragging();
    }
}

/// Колёсико над головой: размер в factor раз, низ и середина — на месте
#[tauri::command]
pub fn portrait_resize(app: AppHandle, state: State<'_, AppState>, factor: f64) {
    let Some(w) = app.get_webview_window(LABEL) else { return };
    let (Ok(s), Ok(pos), Ok(size)) = (w.scale_factor(), w.outer_position(), w.inner_size()) else { return };
    if !factor.is_finite() || factor <= 0.0 {
        return;
    }
    let old = size.width as f64 / s;
    let new = (old * factor).clamp(SIZE_MIN, SIZE_MAX).round();
    if (new - old).abs() < 1.0 {
        return;
    }
    let x = pos.x as f64 / s + (old - new) / 2.0;
    let y = pos.y as f64 / s + (old - new);
    let _ = w.set_size(LogicalSize::new(new, new));
    let _ = w.set_position(LogicalPosition::new(x, y));
    {
        let mut pl = lk(&state.portrait.place);
        pl.size = new;
        pl.x = Some(x);
        pl.y = Some(y);
    }
    state.portrait.save_soon();
}

/// Меню правой кнопки: чей портрет и «Убрать с рабочего стола»
#[tauri::command]
pub async fn portrait_menu(app: AppHandle) -> Result<(), String> {
    let st = app.state::<AppState>().inner().clone();
    let st2 = st.clone();
    let list = tauri::async_runtime::spawn_blocking(move || menu_personas(&st2)).await.map_err(|e| e.to_string())?;
    let current = st.portrait.placement().persona;
    let err = |e: tauri::Error| e.to_string();
    let menu = Menu::new(&app).map_err(err)?;
    for (id, name) in &list {
        let item = CheckMenuItem::with_id(&app, format!("{MENU_PERSONA}{id}"), name, true, *id == current, None::<&str>)
            .map_err(err)?;
        menu.append(&item).map_err(err)?;
    }
    if !list.is_empty() {
        menu.append(&PredefinedMenuItem::separator(&app).map_err(err)?).map_err(err)?;
    }
    let hide = MenuItem::with_id(&app, MENU_HIDE, t(st.lang, "portrait_hide"), true, None::<&str>).map_err(err)?;
    menu.append(&hide).map_err(err)?;
    let win = app.get_webview_window(LABEL).ok_or("нет окна портрета")?;
    win.popup_menu(&menu).map_err(err)
}

#[cfg(test)]
mod tests {
    use super::{safe_id, Placement, SIZE_DEFAULT};

    #[test]
    fn persona_ids() {
        assert!(safe_id("don"));
        assert!(safe_id("api-test_2"));
        assert!(!safe_id(""));
        assert!(!safe_id("../x"));
        assert!(!safe_id("a b"));
    }

    #[test]
    fn placement_defaults() {
        // Старый или неполный portrait.json — недостающее по умолчанию
        let p: Placement = serde_json::from_str(r#"{"shown":true,"persona":"don"}"#).unwrap();
        assert_eq!((p.shown, p.persona.as_str(), p.size, p.x), (true, "don", SIZE_DEFAULT, None));
    }
}
