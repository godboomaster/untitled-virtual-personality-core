//! Портрет на рабочем столе: живой портрет персоны (тот же движок, что в
//! голосовом режиме веба, web/src/portrait/LivePortrait.tsx) в прозрачном
//! окне без рамки поверх всех окон. Страница окна — src/portrait/.
//!
//! Клики проходят сквозь окно к тому, что под ним, кроме самой фигуры: окно
//! «прозрачно для мыши», пока курсор не над ней. Панель сообщает странице,
//! где курсор (заодно портрет смотрит на него по всему экрану), страница по
//! прозрачности кадра отвечает, над фигурой ли он (portrait_hit). Над
//! фигурой: клик — погладить (ядру — сигнал pet: персона узнает, если
//! включены «тычки»), перетаскивание — переставить, колёсико — размер,
//! правая кнопка — меню (чей портрет, облачка, убрать с рабочего стола).
//!
//! Реплики персоны — облачком над головой: окно выше головы на BUBBLE_H (это
//! место тоже пропускает клики), новые сообщения — долгим опросом
//! /api/inbox со своим client_id (watch_inbox). Клик по облачку — чат в вебе.
//!
//! Энергия: курсор сверяется часто, только пока он у окна (POLL_NEAR), вдали —
//! реже (POLL_FAR), а когда вас нет (система давно без мыши и клавиатуры) —
//! совсем редко (POLL_AWAY); тогда же портрет засыпает (portrait-away).
//! Атласы качаются заново, только когда портрет правили (updated в
//! /api/portraits) — проверка по таймеру лёгкая.
//!
//! Бот не запущен — портрет всё равно на столе: атласы и имена персон панель
//! читает прямо из папки данных ядра (data/api_<p>/portrait, data/personas),
//! а включают его и выбирают персону и в трее, и в окне панели (portrait_set).
//!
//! Чей портрет, где голова и какого она размера — portrait.json в папке
//! настроек, отдельно от config.json: место меняется перетаскиванием, а
//! настройки окно панели сохраняет целиком. Атласы последней загрузки — в
//! кэше приложения: портрет остаётся на столе, даже если папка данных не та.

use crate::config::ApiEnv;
use base64::Engine;
use crate::platform;
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
    WebviewWindow, WebviewWindowBuilder, WindowEvent, Wry,
};
use tauri_plugin_opener::OpenerExt;

pub const LABEL: &str = "portrait";
/// Курсор у окна (ближе NEAR_PX): сверять часто — над фигурой ли он, ловить ли клик
const POLL_NEAR: Duration = Duration::from_millis(33);
/// Курсор далеко: нужен только взгляд, хватает и реже
const POLL_FAR: Duration = Duration::from_millis(100);
/// Вас нет: курсор стоит, ждём только, когда вернётесь
const POLL_AWAY: Duration = Duration::from_millis(250);
const NEAR_PX: f64 = 120.0;
/// Столько секунд без мыши и клавиатуры — вас нет у компьютера: портрет засыпает
const AWAY_AFTER: f64 = 5.0 * 60.0;
const IDLE_EVERY: Duration = Duration::from_secs(1);
/// Место над головой под облачко с репликой, логические px: окно выше головы
/// на BUBBLE_H и не уже BUBBLE_W
const BUBBLE_W: f64 = 280.0;
const BUBBLE_H: f64 = 112.0;
/// Свой курсор входящих: сообщения, забранные портретом, веб и телефон получат как обычно
const INBOX_CLIENT: &str = "desktop-portrait";
const INBOX_WAIT_SECS: u64 = 300;
const INBOX_RETRY_SECS: [u64; 3] = [15, 30, 60];
/// Гладят подряд часто — ядру не чаще (у него свой лимит, раз в 15 мин)
const PET_POKE_EVERY: Duration = Duration::from_secs(60);
const SIZE_DEFAULT: f64 = 240.0;
const SIZE_MIN: f64 = 120.0;
const SIZE_MAX: f64 = 640.0;
/// Место по умолчанию — у правого нижнего угла рабочей области, с отступом справа
const MARGIN: f64 = 24.0;
/// Место сохраняется, когда окно столько не двигалось
const SAVE_AFTER: Duration = Duration::from_millis(700);
const MENU_PERSONA: &str = "portrait_persona:";
const MENU_BUBBLES: &str = "portrait_bubbles";
const MENU_HIDE: &str = "portrait_hide";

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default, rename_all = "camelCase")]
pub struct Placement {
    pub shown: bool,
    /// id персоны; пусто — первая с портретом
    pub persona: String,
    /// Сторона головы, логические px
    pub size: f64,
    /// Левый верхний угол головы, логические px; нет — у правого нижнего угла экрана
    pub x: Option<f64>,
    pub y: Option<f64>,
    /// Реплики персоны облачком над головой
    pub bubbles: bool,
}

impl Default for Placement {
    fn default() -> Self {
        Placement { shown: false, persona: String::new(), size: SIZE_DEFAULT, x: None, y: None, bubbles: true }
    }
}

/// Окно вокруг головы стороной size: (ширина, высота, отступ головы слева,
/// сверху). С облачками над головой — место под них
fn frame(size: f64, bubbles: bool) -> (f64, f64, f64, f64) {
    if !bubbles {
        return (size, size, 0.0, 0.0);
    }
    let w = size.max(BUBBLE_W);
    (w, size + BUBBLE_H, (w - size) / 2.0, BUBBLE_H)
}

#[derive(Clone, Serialize)]
pub struct Brief {
    shown: bool,
    persona: String,
}

#[derive(Clone)]
pub struct Portrait {
    place: Arc<Mutex<Placement>>,
    path: PathBuf,
    cache_dir: PathBuf,
    /// Номер окна: потоки слежения закрытого окна видят, что устарели
    gen: Arc<AtomicU64>,
    /// Левый верхний угол окна (физические px) и масштаб — для слежения за
    /// курсором без вопросов окну: те идут через главный поток
    origin: Arc<Mutex<Option<(f64, f64, f64)>>>,
    /// Когда ядру последний раз сказали, что погладили
    pet_at: Arc<Mutex<Option<Instant>>>,
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
            origin: Arc::new(Mutex::new(None)),
            pet_at: Arc::new(Mutex::new(None)),
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
        *lk(&self.origin) = Some((pos.x as f64, pos.y as f64, scale));
        {
            let mut pl = lk(&self.place);
            let (_, _, dx, dy) = frame(pl.size, pl.bubbles);
            pl.x = Some(pos.x as f64 / scale + dx);
            pl.y = Some(pos.y as f64 / scale + dy);
        }
        self.save_soon();
    }

    pub fn set_tray_item(&self, item: CheckMenuItem<Wry>) {
        let _ = item.set_checked(self.placement().shown);
        *lk(&self.tray_item) = Some(item);
    }

    /// Для окна панели: на столе ли и чей
    pub fn brief(&self) -> Brief {
        let pl = self.placement();
        Brief { shown: pl.shown, persona: pl.persona }
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

/// Пункт меню портрета (правая кнопка по голове): чей портрет, облачка, убрать
pub fn on_menu(app: &AppHandle, id: &str) {
    if id == MENU_HIDE {
        return hide(app);
    }
    if id == MENU_BUBBLES {
        let p = app.state::<AppState>().portrait.clone();
        let pl = p.placement();
        // Голова остаётся на месте: меняется только место над ней
        if let Some(w) = app.get_webview_window(LABEL) {
            if let Some((hx, hy, s)) = head_pos(&w, &pl) {
                p.update(|pl| pl.bubbles = !pl.bubbles);
                apply_frame(&p, &w, pl.size, hx, hy, !pl.bubbles, s);
            }
        } else {
            p.update(|pl| pl.bubbles = !pl.bubbles);
        }
        let _ = app.emit_to(LABEL, "portrait-reload", ());
        return;
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
    let (w, h, dx, dy) = frame(size, pl.bubbles);
    let gen = p.gen.fetch_add(1, Ordering::SeqCst) + 1;
    *lk(&p.origin) = None;
    let built = WebviewWindowBuilder::new(app, LABEL, WebviewUrl::App("portrait.html".into()))
        .title("Virtual Persona")
        .inner_size(w, h)
        .position(x - dx, y - dy)
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
    if let (Ok(o), Ok(s)) = (win.inner_position(), win.scale_factor()) {
        *lk(&p.origin) = Some((o.x as f64, o.y as f64, s));
    }
    let w2 = win.clone();
    let p2 = p.clone();
    win.on_window_event(move |e| {
        if let WindowEvent::Moved(pos) = e {
            p2.moved(*pos, w2.scale_factor().unwrap_or(1.0));
        }
    });
    watch_cursor(app.clone(), gen);
    watch_inbox(app.clone(), gen);
}

/// Где сейчас голова (логические px) и масштаб окна — по положению окна
fn head_pos(w: &WebviewWindow, pl: &Placement) -> Option<(f64, f64, f64)> {
    let (s, pos) = (w.scale_factor().ok()?, w.outer_position().ok()?);
    let (_, _, dx, dy) = frame(pl.size, pl.bubbles);
    Some((pos.x as f64 / s + dx, pos.y as f64 / s + dy, s))
}

/// Окно — вокруг головы size в (hx, hy); место головы и окна — в настройки
fn apply_frame(p: &Portrait, w: &WebviewWindow, size: f64, hx: f64, hy: f64, bubbles: bool, scale: f64) {
    let (fw, fh, dx, dy) = frame(size, bubbles);
    {
        let mut pl = lk(&p.place);
        pl.size = size;
        pl.x = Some(hx);
        pl.y = Some(hy);
    }
    *lk(&p.origin) = Some(((hx - dx) * scale, (hy - dy) * scale, scale));
    let _ = w.set_size(LogicalSize::new(fw, fh));
    let _ = w.set_position(LogicalPosition::new(hx - dx, hy - dy));
    p.save_soon();
}

#[derive(Clone, Serialize)]
struct Cursor {
    x: f64,
    y: f64,
}

#[derive(Clone, Serialize)]
struct Away {
    away: bool,
}

/// Вас нет у компьютера: система давно без мыши и клавиатуры
fn away_now() -> bool {
    platform::idle_secs().map(|s| s >= AWAY_AFTER).unwrap_or(false)
}

/// Курсор в координатах страницы окна (логические px, может быть за окном) —
/// при каждом сдвиге курсора или окна; заодно — ушли вы или вернулись
/// (portrait-away). Чем дальше курсор от окна, тем реже сверка
fn watch_cursor(app: AppHandle, gen: u64) {
    std::thread::spawn(move || {
        let p = app.state::<AppState>().portrait.clone();
        let mut last: Option<(i64, i64)> = None;
        let mut moved_at = Instant::now();
        let mut idle_at = Instant::now();
        let mut away = false;
        let mut wait = POLL_NEAR;
        loop {
            std::thread::sleep(wait);
            if p.gen.load(Ordering::SeqCst) != gen || app.get_webview_window(LABEL).is_none() {
                return;
            }
            let origin = *lk(&p.origin);
            let (Ok(c), Some((ox, oy, s))) = (app.cursor_position(), origin) else {
                continue;
            };
            let (x, y) = ((c.x - ox) / s, (c.y - oy) / s);
            let key = (x.round() as i64, y.round() as i64);
            if last != Some(key) {
                last = Some(key);
                moved_at = Instant::now();
                let _ = app.emit_to(LABEL, "portrait-cursor", Cursor { x, y });
            }
            if idle_at.elapsed() >= IDLE_EVERY {
                idle_at = Instant::now();
                // Системе не узнать — по одной мыши
                let idle = platform::idle_secs().unwrap_or_else(|| moved_at.elapsed().as_secs_f64());
                if (idle >= AWAY_AFTER) != away {
                    away = !away;
                    let _ = app.emit_to(LABEL, "portrait-away", Away { away });
                }
            }
            let pl = p.placement();
            let (w, h, _, _) = frame(pl.size, pl.bubbles);
            let near = x > -NEAR_PX && y > -NEAR_PX && x < w + NEAR_PX && y < h + NEAR_PX;
            wait = if away {
                POLL_AWAY
            } else if near {
                POLL_NEAR
            } else {
                POLL_FAR
            };
        }
    });
}

#[derive(Clone, Serialize)]
struct Said {
    text: String,
    kind: String,
}

/// Новые сообщения персоны портрета — облачком (portrait-say). Долгий опрос
/// /api/inbox: запрос висит до INBOX_WAIT_SECS, пока не придёт сообщение, —
/// без частого опроса. Первый ответ только даёт курсор: то, что пришло до
/// портрета, уже не новость
fn watch_inbox(app: AppHandle, gen: u64) {
    std::thread::spawn(move || {
        let p = app.state::<AppState>().portrait.clone();
        let mut cursor: Option<(u64, String)> = None;
        let mut fails = 0usize;
        loop {
            if p.gen.load(Ordering::SeqCst) != gen {
                return;
            }
            if !p.placement().bubbles {
                cursor = None;
                std::thread::sleep(Duration::from_secs(5));
                continue;
            }
            let api = app.state::<AppState>().sup.api();
            let path = match &cursor {
                None => format!("/api/inbox?client_id={INBOX_CLIENT}&wait=0&epoch="),
                Some((since, epoch)) => {
                    format!("/api/inbox?client_id={INBOX_CLIENT}&wait={INBOX_WAIT_SECS}&since={since}&epoch={epoch}")
                }
            };
            let v = match probe::api_get(&api, &path, (INBOX_WAIT_SECS + 20) * 1000) {
                Ok(v) => v,
                Err(_) => {
                    // Бот не запущен или перезапускается
                    fails += 1;
                    let secs = INBOX_RETRY_SECS[(fails - 1).min(INBOX_RETRY_SECS.len() - 1)];
                    std::thread::sleep(Duration::from_secs(secs));
                    continue;
                }
            };
            fails = 0;
            let (Some(next), Some(epoch)) = (v["cursor"].as_u64(), v["epoch"].as_str()) else {
                // Ядро старее долгого опроса — облачков не будет; спросить позже
                std::thread::sleep(Duration::from_secs(INBOX_RETRY_SECS[2]));
                continue;
            };
            let first = cursor.is_none();
            cursor = Some((next, epoch.to_string()));
            if first || p.gen.load(Ordering::SeqCst) != gen {
                continue;
            }
            let persona = p.placement().persona;
            let last = v["messages"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|m| m["persona"].as_str() == Some(persona.as_str()))
                .filter_map(|m| {
                    let text = m["text"].as_str()?.trim();
                    (!text.is_empty()).then(|| Said {
                        text: text.to_string(),
                        kind: m["kind"].as_str().unwrap_or("message").to_string(),
                    })
                })
                .last();
            if let Some(said) = last {
                let _ = app.emit_to(LABEL, "portrait-say", said);
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
/// offline — бот не ответил (data — из кэша, если был), gone — персоны нет,
/// asleep — спит ли персона (null — ядро не знает), away — вас нет у
/// компьютера, bubbles и bubbleH — облачка и место над головой под них}
fn load(st: &AppState) -> Value {
    let api = st.sup.api();
    let p = &st.portrait;
    let list = probe::api_get(&api, "/api/portraits", 4000).ok();
    let core_up = list.is_some() || probe::health(&api);
    let mut persona = p.placement().persona;
    if persona.is_empty() {
        let first = if core_up {
            list.as_ref().and_then(|v| v["personas"].as_array()?.first()?.as_str().map(str::to_string))
        } else {
            disk_ids(&api).into_iter().next()
        };
        if let Some(first) = first {
            persona = first.clone();
            p.update(|pl| pl.persona = first);
        }
    }
    let pl = p.placement();
    let asleep = list.as_ref().map(|v| v["asleep"][&persona].clone()).unwrap_or(Value::Null);
    let with_state = |mut v: Value| {
        v["asleep"] = asleep.clone();
        v["away"] = json!(away_now());
        v["bubbles"] = json!(pl.bubbles);
        v["bubbleH"] = json!(frame(pl.size, pl.bubbles).3);
        v
    };
    if !safe_id(&persona) {
        return with_state(json!({ "persona": null, "data": null, "offline": !core_up }));
    }
    if !core_up {
        // Бот не запущен — атласы прямо из папки данных ядра
        if let Some(data) = disk_portrait(&api, &persona) {
            let name = disk_name(&api, &persona);
            return with_state(json!({ "persona": persona, "name": name, "data": data, "offline": true }));
        }
    }
    // Портрет не правили с прошлой загрузки — атласы из кэша, не качая
    let updated = list.as_ref().map(|v| v["updated"][&persona].clone()).unwrap_or(Value::Null);
    if !updated.is_null() {
        if let Some(mut cached) = p.read_cache(&persona) {
            if cached["data"]["updated_at"] == updated {
                cached["offline"] = json!(false);
                return with_state(cached);
            }
        }
    }
    with_state(fetch(st, &api, &persona))
}

/// Портрет с ядра целиком (атласы) → кэш; без связи — из кэша
fn fetch(st: &AppState, api: &ApiEnv, persona: &str) -> Value {
    let p = &st.portrait;
    match probe::api_get(api, &format!("/api/personas/{persona}/portrait"), 20_000) {
        Ok(data) => {
            let name = persona_names(api)
                .and_then(|list| list.into_iter().find(|(id, _)| id == persona).map(|(_, n)| n))
                .unwrap_or_else(|| persona.to_string());
            let out = json!({ "persona": persona, "name": name, "data": data, "offline": false });
            p.write_cache(persona, &out);
            out
        }
        Err(e) if e.starts_with("HTTP 404") => {
            p.drop_cache(persona);
            json!({ "persona": persona, "name": persona, "data": null, "gone": true, "offline": false })
        }
        Err(_) => match p.read_cache(persona) {
            Some(mut v) => {
                v["offline"] = json!(true);
                v
            }
            None => json!({ "persona": persona, "name": persona, "data": null, "offline": true }),
        },
    }
}

/// Кого предложить: персоны с портретом и откуда список (true — бот не
/// отвечает, из папки данных; там пусто — текущая из кэша)
fn personas_with_portrait(st: &AppState) -> (Vec<(String, String)>, bool) {
    let api = st.sup.api();
    let current = st.portrait.placement().persona;
    match (persona_names(&api), with_portrait(&api)) {
        (Some(list), Some(ids)) => (list.into_iter().filter(|(id, _)| ids.contains(id)).collect(), false),
        (Some(list), None) => (list, false),
        (None, _) => {
            let disk: Vec<(String, String)> =
                disk_ids(&api).into_iter().map(|id| (id.clone(), disk_name(&api, &id))).collect();
            if !disk.is_empty() {
                return (disk, true);
            }
            let cached = st
                .portrait
                .read_cache(&current)
                .and_then(|v| v["name"].as_str().map(|n| vec![(current.clone(), n.to_string())]))
                .unwrap_or_default();
            (cached, true)
        }
    }
}

fn menu_personas(st: &AppState) -> Vec<(String, String)> {
    personas_with_portrait(st).0
}

// ── Портрет прямо из папки данных ядра: бот не запущен ──

fn disk_dir(api: &ApiEnv, persona: &str) -> PathBuf {
    api.data.join(format!("api_{persona}")).join("portrait")
}

/// portrait.json персоны (app/api/portrait_api.py), если в нём есть лист A
fn disk_meta(api: &ApiEnv, persona: &str) -> Option<Value> {
    let dir = disk_dir(api, persona);
    let meta: Value = serde_json::from_str(&fs::read_to_string(dir.join("portrait.json")).ok()?).ok()?;
    (meta["sheets"]["A"].is_object() && dir.join("A.webp").is_file()).then_some(meta)
}

/// Персоны с живым портретом в папке данных, по id
fn disk_ids(api: &ApiEnv) -> Vec<String> {
    let Ok(dirs) = fs::read_dir(&api.data) else { return Vec::new() };
    let mut ids: Vec<String> = dirs
        .filter_map(|e| {
            let name = e.ok()?.file_name().into_string().ok()?;
            let id = name.strip_prefix("api_")?.to_string();
            (safe_id(&id) && disk_meta(api, &id).is_some()).then_some(id)
        })
        .collect();
    ids.sort();
    ids
}

/// Имя персоны из её YAML (name: в начале строки): сначала персоны
/// пользователя (data/personas), потом встроенные (app/personas); нет — id
fn disk_name(api: &ApiEnv, persona: &str) -> String {
    for dir in [api.data.join("personas"), api.repo.join("app").join("personas")] {
        let Ok(text) = fs::read_to_string(dir.join(format!("{persona}.yaml"))) else { continue };
        let name = text.lines().find_map(|l| l.strip_prefix("name:")).map(|n| n.trim().trim_matches(['"', '\'']).trim());
        if let Some(n) = name.filter(|n| !n.is_empty()) {
            return n.to_string();
        }
    }
    persona.to_string()
}

/// Портрет в том же виде, что GET /api/personas/{p}/portrait, — с диска
fn disk_portrait(api: &ApiEnv, persona: &str) -> Option<Value> {
    let meta = disk_meta(api, persona)?;
    let dir = disk_dir(api, persona);
    let (mut sheets, mut settings) = (serde_json::Map::new(), serde_json::Map::new());
    for key in ["A", "B", "C", "D", "E", "F"] {
        let Some(s) = meta["sheets"][key].as_object() else { continue };
        let Ok(bytes) = fs::read(dir.join(format!("{key}.webp"))) else { continue };
        let url = format!("data:image/webp;base64,{}", base64::engine::general_purpose::STANDARD.encode(bytes));
        sheets.insert(key.into(), json!(url));
        settings.insert(key.into(), Value::Object(s.clone()));
    }
    sheets.contains_key("A").then(|| {
        json!({
            "rows": meta["rows"].as_u64().unwrap_or(5),
            "cols": meta["cols"].as_u64().unwrap_or(5),
            "cell": 256,
            "sheets": sheets,
            "settings": settings,
            "updated_at": meta["updated_at"],
        })
    })
}

#[derive(Serialize)]
struct PersonaItem {
    id: String,
    name: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PortraitInfo {
    shown: bool,
    persona: String,
    personas: Vec<PersonaItem>,
    /// Бот не отвечает: список и атласы — из папки данных ядра
    from_disk: bool,
}

/// Для окна панели: на столе ли, чей и из кого выбрать
#[tauri::command]
pub async fn portrait_info(state: State<'_, AppState>) -> Result<PortraitInfo, String> {
    let st = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let (list, from_disk) = personas_with_portrait(&st);
        let pl = st.portrait.placement();
        let persona = if pl.persona.is_empty() { list.first().map(|(id, _)| id.clone()).unwrap_or_default() } else { pl.persona };
        PortraitInfo {
            shown: pl.shown,
            persona,
            personas: list.into_iter().map(|(id, name)| PersonaItem { id, name }).collect(),
            from_disk,
        }
    })
    .await
    .map_err(|e| e.to_string())
}

/// Окно панели: показать или убрать портрет, сменить персону — и без бота
#[tauri::command]
pub fn portrait_set(app: AppHandle, shown: bool, persona: Option<String>) {
    let st = app.state::<AppState>();
    if let Some(id) = persona.filter(|id| safe_id(id)) {
        if st.portrait.placement().persona != id {
            st.portrait.update(|pl| pl.persona = id);
            let _ = app.emit_to(LABEL, "portrait-reload", ());
        }
    }
    let open = app.get_webview_window(LABEL).is_some();
    if shown && !open {
        show(&app);
    } else if !shown {
        hide(&app);
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

/// Колёсико над головой: размер в factor раз, низ и середина головы — на месте
#[tauri::command]
pub fn portrait_resize(app: AppHandle, state: State<'_, AppState>, factor: f64) {
    let Some(w) = app.get_webview_window(LABEL) else { return };
    if !factor.is_finite() || factor <= 0.0 {
        return;
    }
    let p = &state.portrait;
    let pl = p.placement();
    let Some((hx, hy, s)) = head_pos(&w, &pl) else { return };
    let old = pl.size;
    let new = (old * factor).clamp(SIZE_MIN, SIZE_MAX).round();
    if (new - old).abs() < 1.0 {
        return;
    }
    apply_frame(p, &w, new, hx + (old - new) / 2.0, hy + (old - new), pl.bubbles, s);
}

/// Погладили: ядру — сигнал pet (до персоны дойдёт, если включены «тычки»)
#[tauri::command]
pub fn portrait_pet(state: State<'_, AppState>) {
    let p = &state.portrait;
    {
        let mut at = lk(&p.pet_at);
        if at.map(|t| t.elapsed() < PET_POKE_EVERY).unwrap_or(false) {
            return;
        }
        *at = Some(Instant::now());
    }
    let persona = p.placement().persona;
    if !safe_id(&persona) {
        return;
    }
    let api = state.sup.api();
    std::thread::spawn(move || {
        let body = json!({ "chat_id": "auto", "kind": "pet" });
        let _ = probe::api_post_json(&api, &format!("/api/personas/{persona}/room/poke"), &body, 8000);
    });
}

/// Клик по облачку: чат с персоной в вебе
#[tauri::command]
pub fn portrait_open_chat(app: AppHandle, state: State<'_, AppState>) {
    let persona = state.portrait.placement().persona;
    let base = crate::commands::web_url(&state.sup.config());
    let url = if safe_id(&persona) { format!("{base}/?chat={persona}") } else { base };
    let _ = app.opener().open_url(url, None::<&str>);
}

/// Меню правой кнопки: чей портрет, облачка и «Убрать с рабочего стола»
#[tauri::command]
pub async fn portrait_menu(app: AppHandle) -> Result<(), String> {
    let st = app.state::<AppState>().inner().clone();
    let st2 = st.clone();
    let list = tauri::async_runtime::spawn_blocking(move || menu_personas(&st2)).await.map_err(|e| e.to_string())?;
    let pl = st.portrait.placement();
    let current = pl.persona.clone();
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
    let bubbles =
        CheckMenuItem::with_id(&app, MENU_BUBBLES, t(st.lang, "portrait_bubbles"), true, pl.bubbles, None::<&str>)
            .map_err(err)?;
    menu.append(&bubbles).map_err(err)?;
    let hide = MenuItem::with_id(&app, MENU_HIDE, t(st.lang, "portrait_hide"), true, None::<&str>).map_err(err)?;
    menu.append(&hide).map_err(err)?;
    let win = app.get_webview_window(LABEL).ok_or("нет окна портрета")?;
    win.popup_menu(&menu).map_err(err)
}

#[cfg(test)]
mod tests {
    use super::{disk_ids, disk_name, disk_portrait, frame, safe_id, Placement, BUBBLE_H, BUBBLE_W, SIZE_DEFAULT};
    use crate::config::ApiEnv;
    use std::fs;

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
        assert_eq!((p.shown, p.persona.as_str(), p.size, p.x, p.bubbles), (true, "don", SIZE_DEFAULT, None, true));
    }

    #[test]
    fn window_frame() {
        // Без облачков окно — сама голова
        assert_eq!(frame(240.0, false), (240.0, 240.0, 0.0, 0.0));
        // С облачками: место над головой, голова по середине низа
        let (w, h, dx, dy) = frame(240.0, true);
        assert_eq!((w, h, dy), (BUBBLE_W, 240.0 + BUBBLE_H, BUBBLE_H));
        assert_eq!(dx * 2.0 + 240.0, w);
        // Голова шире облачка — окно по голове
        assert_eq!(frame(400.0, true).0, 400.0);
    }

    #[test]
    fn portrait_from_disk() {
        // Бот не запущен: портреты и имена — из папки данных
        let root = std::env::temp_dir().join(format!("vpc-portrait-disk-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let data = root.join("data");
        let don = data.join("api_don").join("portrait");
        fs::create_dir_all(&don).unwrap();
        fs::write(don.join("portrait.json"), r#"{"rows":5,"cols":5,"sheets":{"A":{"bg":"auto"},"D":{"bg":"auto"}},"updated_at":12.5}"#).unwrap();
        fs::write(don.join("A.webp"), b"RIFFa").unwrap();
        fs::write(don.join("D.webp"), b"RIFFd").unwrap();
        // Без листа A — не портрет; не api_ — не персона
        let kara = data.join("api_kara").join("portrait");
        fs::create_dir_all(&kara).unwrap();
        fs::write(kara.join("portrait.json"), r#"{"sheets":{"D":{}}}"#).unwrap();
        fs::create_dir_all(data.join("logs")).unwrap();
        fs::create_dir_all(data.join("personas")).unwrap();
        fs::write(data.join("personas").join("don.yaml"), "id: don\nname: \"Don Quixote\"\nfeatures:\n  name: nested\n").unwrap();
        let api = ApiEnv::read(&root);
        assert_eq!(api.data, data);
        assert_eq!(disk_ids(&api), vec!["don".to_string()]);
        assert_eq!(disk_name(&api, "don"), "Don Quixote");
        assert_eq!(disk_name(&api, "kara"), "kara");
        let p = disk_portrait(&api, "don").unwrap();
        assert_eq!(p["sheets"]["A"], "data:image/webp;base64,UklGRmE=");
        assert!(p["sheets"]["D"].is_string() && p["sheets"]["B"].is_null());
        assert_eq!((p["rows"].as_u64(), p["updated_at"].as_f64()), (Some(5), Some(12.5)));
        assert!(disk_portrait(&api, "kara").is_none());
        let _ = fs::remove_dir_all(&root);
    }
}
