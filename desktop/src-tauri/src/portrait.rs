//! Портрет на рабочем столе: живой портрет персоны (тот же движок, что в
//! голосовом режиме веба, web/src/portrait/LivePortrait.tsx) в прозрачном
//! окне без рамки поверх всех окон. Страница окна — src/portrait/. Своих
//! листов портрета у персоны нет — живая голова её фигуры из «Комнаты»
//! (web/src/portrait/LiveHead.tsx): облик — из раскладки комнаты
//! (avatar_preset), не задан — по умолчанию.
//!
//! Клики проходят сквозь окно к тому, что под ним, кроме самой фигуры: окно
//! «прозрачно для мыши», пока курсор не над ней. Панель сообщает странице,
//! где курсор (заодно портрет смотрит на него по всему экрану), страница по
//! прозрачности кадра отвечает, над фигурой ли он (portrait_hit). Над
//! фигурой: клик — погладить (ядру — сигнал pet: персона узнает, если
//! включены «тычки»), перетаскивание — переставить (своё, не системное:
//! системное не пускает окно выше строки меню), колёсико — размер,
//! правая кнопка — меню (чей портрет, облачка, убрать с рабочего стола).
//!
//! Реплики персоны — облачком над головой: окно выше головы на BUBBLE_H (это
//! место тоже пропускает клики). Новые реплики — долгим опросом ленты реплик
//! ядра /api/said (watch_said): и ответы в чате, откуда бы ни писали (веб,
//! телефон, голос, поле у портрета), и фоновые сообщения. Длинная реплика
//! идёт по частям — страница режет её под размер облачка. Клик по облачку —
//! следующая часть, по последней — чат в вебе.
//!
//! Поле для сообщений — сбоку от головы (окно шире головы на FIELD_W), с той
//! стороны, где на экране больше места: голова справа — поле слева, и
//! наоборот (side_for). Реплика уходит персоне портрета как из веба
//! (portrait_send). Окно не берёт фокус,
//! пока в поле не щёлкнули (portrait_focus): клик по голове не уводит
//! клавиатуру из окна, где вы работаете; ушли в другое окно — снова так.
//!
//! Портрет ходит (portrait_motion.rs): гуляет и прыгает на окна по просьбе
//! страницы (ротация), «Иди сюда» (меню, трей, Ctrl+Option+P), «Спрячься» за
//! край; с «Ходит по окнам» — падает, когда его бросили, стоит на нижнем крае
//! экрана или на верхнем крае окон и едет вместе с окном. Странице —
//! portrait-motion. Файл, брошенный на фигуру, уходит персоне (картинка — в
//! чат, документ — в её файлы и просьба прочитать). «Посиди со мной» — таймер
//! страницы (portrait-focus). Анимации персоны (portrait_anim ядра) — с
//! портретом в portrait_load; какую выбрала сама персона — act в ленте реплик.
//!
//! Аватар режима (app/api/avatar_modes.py): у режима персоны (свой YAML)
//! бывают свои листы и анимации. Портрет показывает аватар режима, что
//! включён в основном чате (/portrait?live=1, /portrait/anims?live=1; метки
//! и режим — в /api/portraits). Сменился режим — событие mode в ленте
//! реплик: перечитать; переход из прежнего режима в новый (анимация с
//! поводом mode) играет страница.
//!
//! Энергия: курсор сверяется часто, только пока он у окна (POLL_NEAR), вдали —
//! реже (POLL_FAR), а когда вас нет (система давно без мыши и клавиатуры) —
//! совсем редко (POLL_AWAY); тогда же портрет засыпает (portrait-away).
//! Атласы качаются заново, только когда портрет правили (updated в
//! /api/portraits) — проверка по таймеру лёгкая.
//!
//! Бот не запущен — портрет всё равно на столе: атласы, облик фигуры и имена
//! персон панель читает прямо из папки данных ядра (data/api_<p>/portrait,
//! data/api_<p>/room/layout.json, data/personas),
//! а включают его и выбирают персону и в трее, и в окне панели (portrait_set).
//!
//! Разговор голосом (voice.rs, слушатель имени с --portrait): прозвучало
//! имя, а чат этой персоны в вебе на этом компьютере не открыт — портрет
//! этой персоны встаёт на стол (on_voice), и дальше с ней говорят без имени;
//! что вы говорите, страница рисует облачком сбоку по ходу речи
//! (portrait-voice). Назвали в разговоре другую — встаёт и её портрет,
//! рядом: у каждой персоны разговора своё окно (гость, метка
//! portrait-<id>), портрет на столе — главное окно (LABEL), если разговор с
//! его персоной. Голос (portrait-voice) — только окну той, что слушает;
//! перебили её — её окну portrait-cut (оборвать реплику). Разговор кончился
//! — гости уходят; «на столе» в настройках разговор не трогает. Убрали
//! портрет персоны — разговор дальше без неё (leave_talk).
//!
//! Кнопка микрофона у головы (portrait_mic_toggle) — то же, что «Слушать
//! имя» в трее: выключили — слушатель закрывает микрофон, разговор кончается;
//! включили — слушает снова. Состояние слушателя — всем окнам (on_mic,
//! portrait-mic). Закончить разговор, не выключая микрофон, — пункт меню.
//!
//! Чей портрет, где голова и какого она размера — portrait.json в папке
//! настроек, отдельно от config.json: место меняется перетаскиванием, а
//! настройки окно панели сохраняет целиком. Атласы последней загрузки — в
//! кэше приложения: портрет остаётся на столе, даже если папка данных не та.

#[path = "portrait_motion.rs"]
mod motion;

use crate::config::ApiEnv;
use base64::Engine;
use crate::platform;
use crate::probe;
use crate::state::AppState;
use crate::texts::{t, tf};
use crate::voice::{VoiceView, WakeTalk};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{
    AppHandle, DragDropEvent, Emitter, LogicalPosition, LogicalSize, Manager, PhysicalPosition, State, WebviewUrl,
    WebviewWindow, WebviewWindowBuilder, WindowEvent, Wry,
};
use tauri_plugin_opener::OpenerExt;

pub const LABEL: &str = "portrait";
/// Гости — портреты персон разговора голосом: метка LABEL-<id персоны>
const GUEST_PREFIX: &str = "portrait-";
/// Между окнами портретов, логические px
const GUEST_GAP: f64 = 16.0;
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
/// Место сбоку от головы под поле для сообщений
const FIELD_W: f64 = 220.0;
/// Id портрета как устройства (client_id реплик): пока ответа нет, свою
/// реплику веб и телефон покажут, а портрету её показывать незачем
const CLIENT_ID: &str = "desktop-portrait";
const SAID_WAIT_SECS: u64 = 300;
const SAID_RETRY_SECS: [u64; 3] = [15, 30, 60];
/// Ответ персоны бывает долгим: веб-чат LLM, агент задач
const SEND_TIMEOUT_MS: u64 = 10 * 60_000;
/// История чата — простая выдача, но ядро может быть занято ходом
const HISTORY_TIMEOUT_MS: u64 = 15_000;
/// Сколько последних реплик показать, если пришло сразу много (вас не было)
const SAID_KEEP: usize = 3;
/// Гладят подряд часто — ядру не чаще (у него свой лимит, раз в 15 мин)
const PET_POKE_EVERY: Duration = Duration::from_secs(60);
const SIZE_DEFAULT: f64 = 240.0;
const SIZE_MIN: f64 = 120.0;
const SIZE_MAX: f64 = 640.0;
/// Место по умолчанию — у правого нижнего угла рабочей области, с отступом справа
const MARGIN: f64 = 24.0;
/// Место сохраняется, когда окно столько не двигалось
const SAVE_AFTER: Duration = Duration::from_millis(700);
/// Голову утащили за край экрана дальше, чем можно, — вернуть, когда окно
/// столько не двигалось
const FIT_AFTER: Duration = Duration::from_millis(1200);
/// За краем экрана голова может быть, но столько её (доля, не меньше KEEP_MIN
/// px) по каждой оси остаётся на экране
const KEEP_FRAC: f64 = 0.3;
const KEEP_MIN: f64 = 48.0;
/// Перетаскивание: как часто окно догоняет курсор
const DRAG_EVERY: Duration = Duration::from_millis(8);
const MENU_PERSONA: &str = "portrait_persona:";
const MENU_BUBBLES: &str = "portrait_bubbles";
const MENU_INPUT: &str = "portrait_input";
const MENU_HIDE: &str = "portrait_hide";
const MENU_COME: &str = "portrait_come";
const MENU_HIDEAWAY: &str = "portrait_hideaway";
const MENU_FOCUS: &str = "portrait_focus:";
const MENU_PHYSICS: &str = "portrait_physics";
const MENU_ANIMS: &str = "portrait_anims";
const MENU_SOUNDS: &str = "portrait_sounds";
const MENU_TALK_END: &str = "portrait_talk_end";
/// «Посиди со мной»: сколько минут на выбор
const FOCUS_MINUTES: [u32; 2] = [25, 50];
/// Файл, брошенный на голову: не больше (картинка — как у чата, документ — с запасом)
const DROP_IMAGE_MAX: u64 = 10 * 1024 * 1024;
const DROP_DOC_MAX: u64 = 20 * 1024 * 1024;
const IMAGE_TYPES: [(&str, &str); 6] = [
    ("png", "image/png"),
    ("jpg", "image/jpeg"),
    ("jpeg", "image/jpeg"),
    ("webp", "image/webp"),
    ("gif", "image/gif"),
    ("bmp", "image/bmp"),
];

/// С какой стороны головы поле для сообщений
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Side {
    Left,
    Right,
}

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
    /// Поле для сообщений сбоку от головы
    pub input: bool,
    /// С какой стороны оно сейчас — по месту головы на экране (side_for)
    pub side: Side,
    /// «Ходит по окнам»: гравитация, стоит на нижнем крае экрана или на
    /// верхнем крае окон (portrait_motion.rs)
    pub physics: bool,
    /// «Анимации»: ротация и анимации на события (решает страница)
    pub anims: bool,
    /// «Звуки анимаций»: свой звук анимации (anim.sound_url) играет вместе с ней
    pub sounds: bool,
    /// Запас окна под анимации крупнее головы — по анимациям персоны
    /// (room_of), не сохраняется
    #[serde(skip)]
    pub room: Room,
    /// Transient space for the music panel; never restore an empty panel at boot.
    #[serde(skip)]
    player: PlayerSpace,
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
struct PlayerSpace {
    width: f64,
    height: f64,
}

/// Место в окне под анимации персоны крупнее головы, в сторонах головы:
/// сколько кадр самой большой из них уходит от середины пола влево (kl) и
/// вправо (kr; не меньше половины), вверх (ky, не меньше 1) и под пол (kb).
/// Середина пола кадра стоит на середине низа головы (страница,
/// portrait-anim; web animTypes.animAnchor и animSink)
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Room {
    pub kl: f64,
    pub kr: f64,
    pub ky: f64,
    pub kb: f64,
}

impl Default for Room {
    fn default() -> Self {
        Room { kl: 0.5, kr: 0.5, ky: 1.0, kb: 0.0 }
    }
}

/// Запас под анимации из того, что получила страница (portrait_load):
/// включённые, с атласом; «Анимации» в меню выключены — запаса нет.
/// Длинная сторона кадра — голова × scale (web/src/portrait/animTypes.animBox)
fn room_of(v: &Value) -> Room {
    let mut r = Room::default();
    if v["animsOn"] == json!(false) {
        return r;
    }
    for a in v["anims"]["anims"].as_array().into_iter().flatten() {
        let on = a["enabled"] != json!(false) && a["atlas"].as_str().is_some_and(|s| !s.is_empty()) && a["frames"].as_f64().unwrap_or(0.0) > 0.0;
        if !on {
            continue;
        }
        let (fw, fh) = (a["fw"].as_f64().unwrap_or(1.0).max(1.0), a["fh"].as_f64().unwrap_or(1.0).max(1.0));
        let sink = a["sink"].as_f64().unwrap_or(0.0).clamp(0.0, fh);
        let ax = a["ax"].as_f64().unwrap_or(fw / 2.0).clamp(0.0, fw);
        let k = a["scale"].as_f64().unwrap_or(1.0).clamp(0.3, 3.0) / fw.max(fh);
        r.kl = r.kl.max(ax * k);
        r.kr = r.kr.max((fw - ax) * k);
        r.ky = r.ky.max((fh - sink) * k);
        r.kb = r.kb.max(sink * k);
    }
    r
}

impl Placement {
    /// Поле для сообщений и его сторона; выключено — None
    fn field(&self) -> Option<Side> {
        self.input.then_some(self.side)
    }

    fn window_room(&self) -> Room {
        self.window_room_for(self.size)
    }

    fn window_room_for(&self, size: f64) -> Room {
        let mut room = self.room;
        if self.player.width > 0.0 {
            room.kr = room.kr.max(0.5 + (self.player.width + 20.0) / size);
            room.ky = room.ky.max((self.player.height + 8.0) / size);
        }
        room
    }
}

impl Default for Placement {
    fn default() -> Self {
        Placement {
            shown: false,
            persona: String::new(),
            size: SIZE_DEFAULT,
            x: None,
            y: None,
            bubbles: true,
            input: true,
            side: Side::Left,
            physics: true,
            anims: true,
            sounds: true,
            room: Room::default(),
            player: PlayerSpace::default(),
        }
    }
}

/// Окно вокруг головы стороной size: (ширина, высота, отступ головы слева,
/// сверху). С облачками — место над головой, с полем для сообщений — сбоку
/// от неё (над полем облачко тоже помещается: оно шире головы). Анимации
/// крупнее головы (room) — ещё запас по бокам и сверху, сколько не хватает
/// (pads): иначе края окна обрезают кадр
fn frame(size: f64, bubbles: bool, field: Option<Side>, room: Room) -> (f64, f64, f64, f64) {
    let (w, h, dx, dy) = bare_frame(size, bubbles, field);
    let (l, t, r, b) = pads(size, bubbles, field, room);
    (w + l + r, h + t + b, dx + l, dy + t)
}

fn bare_frame(size: f64, bubbles: bool, field: Option<Side>) -> (f64, f64, f64, f64) {
    let top = if bubbles { BUBBLE_H } else { 0.0 };
    match field {
        Some(side) => (size + FIELD_W, top + size, if side == Side::Left { FIELD_W } else { 0.0 }, top),
        None => {
            let w = if bubbles { size.max(BUBBLE_W) } else { size };
            (w, top + size, (w - size) / 2.0, top)
        }
    }
}

/// Запас под анимации: (слева, сверху, справа, снизу), логические px. Кадр
/// — от середины низа головы на size × kl влево и size × kr вправо, над
/// низом головы высотой size × ky и под ним — size × kb (фигура в кадрах
/// уходит под пол)
fn pads(size: f64, bubbles: bool, field: Option<Side>, room: Room) -> (f64, f64, f64, f64) {
    let (w, h, dx, _) = bare_frame(size, bubbles, field);
    let cx = dx + size / 2.0;
    let l = (size * room.kl - cx).max(0.0).ceil();
    let r = (size * room.kr - (w - cx)).max(0.0).ceil();
    let t = (size * room.ky - h).max(0.0).ceil();
    let b = (size * room.kb).max(0.0).ceil();
    (l, t, r, b)
}

#[derive(Clone, Serialize)]
pub struct Brief {
    shown: bool,
    persona: String,
    /// Клавиши анимаций: off, on, mouse (нет доступа к клавиатуре), unsupported
    keys: &'static str,
}

/// Общее для всех окон портретов: разговор голосом и окна его персон
#[derive(Default)]
struct Shared {
    /// Разговор голосом сейчас (от слушателя, voice.rs)
    talk: Mutex<Option<WakeTalk>>,
    /// Гости: окна персон разговора, кроме главного
    guests: Mutex<Vec<Portrait>>,
}

/// Окно портрета: главное (портрет на столе, место и настройки — в
/// portrait.json) или гость (персона разговора голосом, только в памяти)
#[derive(Clone)]
pub struct Portrait {
    /// Метка окна: LABEL — главное, GUEST_PREFIX + id — гость
    label: String,
    guest: bool,
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
    fitting: Arc<AtomicBool>,
    /// Голову тащат (portrait_drag): место не поправлять, пока не отпустят
    dragging: Arc<AtomicBool>,
    /// Видимая часть окна, о которой странице уже сказали (portrait-visible)
    visible: Arc<Mutex<Option<Visible>>>,
    /// Галочка «Портрет на рабочем столе» в меню трея
    tray_item: Arc<Mutex<Option<CheckMenuItem<Wry>>>>,
    /// Ходит, падает, стоит на окне (portrait_motion.rs)
    motion: Arc<motion::Motion>,
    /// «Посиди со мной» идёт до этого времени — в меню тогда «Хватит»
    focus_until: Arc<Mutex<Option<Instant>>>,
    /// Разговор голосом для этого окна: его персона слушает — разговор, иначе None
    voice: Arc<Mutex<Option<WakeTalk>>>,
    shared: Arc<Shared>,
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
        Portrait::with(LABEL.into(), false, place, path, cache_dir, Arc::new(Shared::default()))
    }

    fn with(label: String, guest: bool, place: Placement, path: PathBuf, cache_dir: PathBuf, shared: Arc<Shared>) -> Self {
        Portrait {
            label,
            guest,
            place: Arc::new(Mutex::new(place)),
            path,
            cache_dir,
            gen: Arc::new(AtomicU64::new(0)),
            origin: Arc::new(Mutex::new(None)),
            pet_at: Arc::new(Mutex::new(None)),
            moved_at: Arc::new(Mutex::new(None)),
            saving: Arc::new(AtomicBool::new(false)),
            fitting: Arc::new(AtomicBool::new(false)),
            dragging: Arc::new(AtomicBool::new(false)),
            visible: Arc::new(Mutex::new(None)),
            tray_item: Arc::new(Mutex::new(None)),
            motion: Arc::new(motion::Motion::default()),
            focus_until: Arc::new(Mutex::new(None)),
            voice: Arc::new(Mutex::new(None)),
            shared,
        }
    }

    /// Гость — портрет персоны разговора голосом: место и настройки — в
    /// памяти (place), кэш атласов и разговор — общие с главным
    fn guest(&self, place: Placement) -> Self {
        let label = format!("{GUEST_PREFIX}{}", place.persona);
        Portrait::with(label, true, place, PathBuf::new(), self.cache_dir.clone(), self.shared.clone())
    }

    pub fn placement(&self) -> Placement {
        lk(&self.place).clone()
    }

    /// Чей это портрет
    pub fn persona(&self) -> String {
        self.placement().persona
    }

    fn window(&self, app: &AppHandle) -> Option<WebviewWindow> {
        app.get_webview_window(&self.label)
    }

    /// Событие странице этого окна
    fn emit<S: Serialize + Clone>(&self, app: &AppHandle, event: &str, payload: S) {
        let _ = app.emit_to(self.label.as_str(), event, payload);
    }

    /// С какой стороны головы поле: у главного — где на экране больше места,
    /// у гостя — как поставили (в сторону от соседа)
    fn side_at(&self, app: &AppHandle, hx: f64, hy: f64, size: f64) -> Side {
        if self.placement().player.width > 0.0 {
            Side::Left // Chat and music get separate sides of the avatar.
        } else if self.guest {
            self.placement().side
        } else {
            side_for(app, hx, hy, size)
        }
    }

    fn update(&self, f: impl FnOnce(&mut Placement)) {
        f(&mut lk(&self.place));
        self.save();
    }

    fn save(&self) {
        if self.guest {
            return;
        }
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
            let (_, _, dx, dy) = frame(pl.size, pl.bubbles, pl.field(), pl.window_room());
            pl.x = Some(pos.x as f64 / scale + dx);
            pl.y = Some(pos.y as f64 / scale + dy);
        }
        self.save_soon();
    }

    /// Окно перетащили: когда успокоится, поле — на сторону середины экрана, а
    /// голову, задвинутую за край дальше, чем можно, — вернуть
    fn fit_soon(&self, w: &WebviewWindow) {
        if self.fitting.swap(true, Ordering::SeqCst) {
            return;
        }
        let (me, w) = (self.clone(), w.clone());
        std::thread::spawn(move || {
            loop {
                std::thread::sleep(FIT_AFTER);
                let still = lk(&me.moved_at).map(|t| t.elapsed() >= FIT_AFTER).unwrap_or(true);
                if still && !me.dragging.load(Ordering::SeqCst) && !me.motion.busy() {
                    break;
                }
            }
            me.fitting.store(false, Ordering::SeqCst);
            let pl = me.placement();
            let Some((hx, hy, s)) = head_pos(&w, &pl) else { return };
            // Голову перенесли на другую половину экрана — поле на другую сторону;
            // задвинули за край дальше, чем можно, — вернуть
            let field = pl.input.then(|| me.side_at(w.app_handle(), hx, hy, pl.size));
            let (fx, fy) = fit_on_screen(w.app_handle(), hx, hy, pl.size);
            if (fx - hx).abs() > 0.5 || (fy - hy).abs() > 0.5 || field != pl.field() {
                apply_frame(&me, &w, pl.size, fx, fy, pl.bubbles, pl.input, s);
            }
        });
    }

    /// Какая часть окна на экране — странице (portrait-visible), если
    /// изменилась: голову задвинули за край — поле и облачко она держит на
    /// видимой части
    fn send_visible(&self, w: &WebviewWindow) {
        let Some(v) = visible_now(w, &self.placement()) else { return };
        {
            let mut last = lk(&self.visible);
            if *last == Some(v) {
                return;
            }
            *last = Some(v);
        }
        let _ = w.emit_to(self.label.as_str(), "portrait-visible", v);
    }

    pub fn set_tray_item(&self, item: CheckMenuItem<Wry>) {
        let _ = item.set_checked(self.placement().shown);
        *lk(&self.tray_item) = Some(item);
    }

    /// Для окна панели: на столе ли и чей
    pub fn brief(&self) -> Brief {
        let pl = self.placement();
        Brief { shown: pl.shown, persona: pl.persona, keys: crate::input_watch::status().as_str() }
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

    /// Анимации персоны (GET .../portrait/anims с атласами) — свой файл кэша
    fn anims_file(&self, persona: &str) -> PathBuf {
        self.cache_dir.join(format!("portrait-anims-{persona}.json"))
    }

    fn read_anims_cache(&self, persona: &str) -> Option<Value> {
        fs::read_to_string(self.anims_file(persona)).ok().and_then(|s| serde_json::from_str(&s).ok())
    }

    fn write_anims_cache(&self, persona: &str, v: &Value) {
        let _ = fs::create_dir_all(&self.cache_dir);
        let file = self.anims_file(persona);
        let tmp = file.with_extension("json.tmp");
        if fs::write(&tmp, v.to_string()).is_ok() {
            let _ = fs::rename(&tmp, &file);
        }
    }
}

fn main_of(app: &AppHandle) -> Portrait {
    app.state::<AppState>().portrait.clone()
}

/// Окно портрета по метке: главное или гость
fn of(app: &AppHandle, label: &str) -> Option<Portrait> {
    let main = main_of(app);
    if label == LABEL {
        return Some(main);
    }
    let found = lk(&main.shared.guests).iter().find(|g| g.label == label).cloned();
    found
}

/// Все окна портретов: главное и гости
fn all(app: &AppHandle) -> Vec<Portrait> {
    let main = main_of(app);
    let mut v = vec![main.clone()];
    v.extend(lk(&main.shared.guests).iter().cloned());
    v
}

/// Портрет был на столе при выходе — вернуть
pub fn restore(app: &AppHandle) {
    if main_of(app).placement().shown {
        show(app);
    }
}

/// Кнопка в сайдбаре веб-чата управляет тем же нативным окном, что и трей.
/// Опрос живёт на уровне панели: работает и когда портрет убран со стола.
pub fn watch_web(app: AppHandle) {
    std::thread::spawn(move || {
        let applied = Arc::new(Mutex::new(String::new()));
        loop {
            let p = main_of(&app);
            let pl = p.placement();
            let api = app.state::<AppState>().sup.api();
            let body = json!({
                "persona": pl.persona,
                "shown": pl.shown && p.window(&app).is_some(),
                "applied": lk(&applied).clone(),
            });
            if let Ok(v) = probe::api_post_json(&api, "/api/desktop/portrait/report", &body, 1500) {
                let cmd = &v["pending"];
                if let (Some(id), Some(persona), Some(shown)) =
                    (cmd["id"].as_str(), cmd["persona"].as_str(), cmd["shown"].as_bool())
                {
                    if id != lk(&applied).as_str() && safe_id(persona) {
                        let (id, persona) = (id.to_string(), persona.to_string());
                        let (handle, done) = (app.clone(), applied.clone());
                        let _ = app.run_on_main_thread(move || {
                            portrait_set(handle, shown, Some(persona));
                            *lk(&done) = id;
                        });
                    }
                }
            }
            std::thread::sleep(Duration::from_secs(1));
        }
    });
}

pub fn toggle(app: &AppHandle) {
    if main_of(app).window(app).is_some() {
        hide(app);
    } else {
        show(app);
    }
}

pub fn show(app: &AppHandle) {
    let p = main_of(app);
    p.update(|pl| pl.shown = true);
    p.sync_tray(true);
    // Не из обработчика меню: на Windows создание окна там виснет (WebView2)
    let app = app.clone();
    std::thread::spawn(move || {
        open(&app, &p);
        // Его персона уже в разговоре голосом у гостя — теперь он
        let talk = lk(&p.shared.talk).clone();
        on_voice(&app, talk);
    });
}

/// Убрать портрет с рабочего стола
pub fn hide(app: &AppHandle) {
    let p = main_of(app);
    p.update(|pl| pl.shown = false);
    p.sync_tray(false);
    remove(app, &p);
}

/// Окно убрали (меню «Убрать», трей): главное — и «на столе» в настройках;
/// его персона была в разговоре голосом — разговор дальше без неё
fn remove(app: &AppHandle, p: &Portrait) {
    let persona = p.persona();
    let talking = lk(&p.shared.talk).as_ref().is_some_and(|t| t.personas.contains(&persona));
    close(app, p);
    if talking {
        app.state::<AppState>().voice.leave_talk(&persona);
    }
}

/// Закрыть окно портрета, не трогая «на столе» в настройках; гость уходит
/// и из списка гостей
fn close(app: &AppHandle, p: &Portrait) {
    p.gen.fetch_add(1, Ordering::SeqCst);
    // Окна нет — движение и слежение за окном под ногами гаснут
    p.motion.cancel();
    if p.guest {
        lk(&p.shared.guests).retain(|g| g.label != p.label);
    }
    crate::input_watch::forget(&p.label);
    if let Some(w) = p.window(app) {
        let _ = w.destroy();
    }
}

/// Разговор голосом начался, изменился (что вы говорите, кто слушает, кого
/// назвали, кого перебили) или кончился (None) — из voice.rs. Каждой
/// персоне разговора — окно: главное, если оно на столе с ней, иначе гость
/// (встаёт рядом с последним портретом). Ушла из разговора — гость уходит.
/// Голос — окну той, что слушает; перебили — её окну portrait-cut
pub fn on_voice(app: &AppHandle, talk: Option<WakeTalk>) {
    let main = main_of(app);
    let talk = talk.filter(|t| safe_id(&t.persona)).map(|mut t| {
        t.personas.retain(|p| safe_id(p));
        if !t.personas.contains(&t.persona) {
            t.personas.push(t.persona.clone());
        }
        t
    });
    let prev = std::mem::replace(&mut *lk(&main.shared.talk), talk.clone());
    let personas = talk.as_ref().map(|t| t.personas.clone()).unwrap_or_default();
    let main_persona = main.persona();
    let main_open = main.window(app).is_some();
    let in_main = |p: &str| main_open && p == main_persona;
    // Ушли из разговора (или их взяло главное окно) — гости уходят
    let gone: Vec<Portrait> = lk(&main.shared.guests)
        .iter()
        .filter(|g| {
            let p = g.persona();
            !personas.contains(&p) || in_main(&p)
        })
        .cloned()
        .collect();
    for g in gone {
        close(app, &g);
    }
    // Новые — рядом с теми, что уже стоят
    for persona in &personas {
        let has = lk(&main.shared.guests).iter().any(|g| &g.persona() == persona);
        if in_main(persona) || has {
            continue;
        }
        let g = main.guest(guest_place(app, &main, persona));
        lk(&main.shared.guests).push(g.clone());
        // Не из потока вывода слушателя: окно строится долго
        let app = app.clone();
        std::thread::spawn(move || {
            open(&app, &g);
            // Ушла из разговора, пока окно строилось
            let alive = lk(&g.shared.guests).iter().any(|x| x.label == g.label);
            if !alive {
                close(&app, &g);
            }
        });
    }
    // Голос — окну той, что слушает; остальным — ничего (сказать, если изменилось)
    let windows = all(app);
    for w in &windows {
        let mine = talk.clone().filter(|t| t.persona == w.persona() && (w.guest || main_open));
        let changed = {
            let mut cur = lk(&w.voice);
            let changed = *cur != mine;
            *cur = mine.clone();
            changed
        };
        if changed {
            w.emit(app, "portrait-voice", mine);
        }
    }
    // Перебили — её реплику на портрете оборвать
    if let Some(t) = &talk {
        if t.cut > prev.as_ref().map(|p| p.cut).unwrap_or(0) {
            let who = &t.cut_persona;
            let target = windows.iter().find(|w| &w.persona() == who && (w.guest || main_open));
            if let Some(w) = target {
                w.emit(app, "portrait-cut", ());
            }
        }
    }
}

/// Состояние слушателя изменилось (voice.rs, монитор) — кнопке микрофона
/// во всех окнах портретов
pub fn on_mic(app: &AppHandle, view: VoiceView) {
    for w in all(app) {
        w.emit(app, "portrait-mic", view.clone());
    }
}

/// Место гостя persona: рядом с последним портретом на столе — в сторону
/// середины экрана, ноги на одной линии (окна не заходят одно на другое:
/// поле гостя — со стороны от соседа); на столе никого — на привычное место
/// портрета. Размер и настройки — как у главного
fn guest_place(app: &AppHandle, main: &Portrait, persona: &str) -> Placement {
    let pl = main.placement();
    let size = pl.size.clamp(SIZE_MIN, SIZE_MAX);
    let mut last: Option<(f64, f64, Placement)> = None;
    if let Some(w) = main.window(app) {
        if let Some((hx, hy, _)) = head_pos(&w, &pl) {
            last = Some((hx, hy, pl.clone()));
        }
    }
    for g in lk(&main.shared.guests).iter() {
        let gp = g.placement();
        if let (Some(x), Some(y)) = (gp.x, gp.y) {
            last = Some((x, y, gp));
        }
    }
    let mut place = Placement { shown: true, persona: persona.to_string(), size, x: None, y: None, ..pl.clone() };
    let (x, y, side) = match last {
        None => {
            let (x, y) = match (pl.x, pl.y) {
                (Some(x), Some(y)) if on_screen(app, x, y, size) => (x, y),
                _ => corner(app, size),
            };
            let (x, y) = fit_on_screen(app, x, y, size);
            (x, y, side_for(app, x, y, size))
        }
        Some((hx, hy, lp)) => {
            // В сторону середины экрана: голова справа — гость слева от неё
            let leftward = side_for(app, hx, hy, lp.size) == Side::Left;
            let (x, y, side) = beside(hx, hy, &lp, size, place.bubbles, place.input, leftward);
            let (x, y) = fit_on_screen(app, x, y, size);
            (x, y, side)
        }
    };
    (place.x, place.y, place.side) = (Some(x), Some(y), side);
    place
}

/// Голова гостя стороной size рядом с соседом (его голова в (hx, hy), его
/// окно — по lp): окна через GUEST_GAP, ноги на одной линии, поле гостя —
/// с дальней от соседа стороны. → (x, y, сторона поля)
fn beside(hx: f64, hy: f64, lp: &Placement, size: f64, bubbles: bool, input: bool, leftward: bool) -> (f64, f64, Side) {
    let side = if leftward { Side::Left } else { Side::Right };
    let (lw, _, ldx, _) = frame(lp.size, lp.bubbles, lp.field(), lp.window_room());
    let (fw, _, dx, _) = frame(size, bubbles, input.then_some(side), Room::default());
    let left = if leftward { hx - ldx - GUEST_GAP - fw } else { hx - ldx + lw + GUEST_GAP };
    (left + dx, hy + lp.size - size, side)
}

/// «Позвать портрет» (трей, горячая клавиша): нет на столе — показать, и к курсору
pub fn summon(app: &AppHandle) {
    let p = main_of(app);
    let open = p.window(app).is_some();
    if !open {
        show(app);
    }
    let app = app.clone();
    std::thread::spawn(move || {
        if !open {
            // Окно и страница поднимаются; страница загрузит портрет и встанет на опору
            std::thread::sleep(Duration::from_millis(1500));
        }
        motion::come(&app, &p);
    });
}

#[derive(Clone, Serialize)]
struct Focus {
    minutes: u32,
}

/// Пункт меню портрета (правая кнопка по голове): чей портрет, облачка,
/// движение, «Посиди со мной», убрать. id — пункт@метка окна
pub fn on_menu(app: &AppHandle, id: &str) {
    let (id, label) = id.split_once('@').unwrap_or((id, LABEL));
    let Some(p) = of(app, label) else { return };
    if id == MENU_HIDE {
        return if p.guest { remove(app, &p) } else { hide(app) };
    }
    if id == MENU_TALK_END {
        return app.state::<AppState>().voice.end_talk();
    }
    if id == MENU_COME || id == MENU_HIDEAWAY {
        let (app, come) = (app.clone(), id == MENU_COME);
        std::thread::spawn(move || if come { motion::come(&app, &p) } else { motion::hide_toggle(&app, &p) });
        return;
    }
    if let Some(min) = id.strip_prefix(MENU_FOCUS).and_then(|m| m.parse::<u32>().ok()) {
        *lk(&p.focus_until) = (min > 0).then(|| Instant::now() + Duration::from_secs(min as u64 * 60));
        p.emit(app, "portrait-focus", Focus { minutes: min });
        return;
    }
    if id == MENU_PHYSICS || id == MENU_ANIMS || id == MENU_SOUNDS {
        let physics = id == MENU_PHYSICS;
        let mut on = false;
        p.update(|pl| {
            if physics {
                pl.physics = !pl.physics;
                on = pl.physics;
            } else if id == MENU_SOUNDS {
                pl.sounds = !pl.sounds;
            } else {
                pl.anims = !pl.anims;
            }
        });
        if physics {
            let (app, p) = (app.clone(), p.clone());
            std::thread::spawn(move || motion::physics_changed(&app, &p, on));
        }
        p.emit(app, "portrait-reload", ());
        return;
    }
    if id == MENU_BUBBLES || id == MENU_INPUT {
        let pl = p.placement();
        let (bubbles, input) =
            if id == MENU_BUBBLES { (!pl.bubbles, pl.input) } else { (pl.bubbles, !pl.input) };
        // Голова остаётся на месте: меняется только место над ней или под ней
        match p.window(app).and_then(|w| head_pos(&w, &pl).map(|h| (w, h))) {
            Some((w, (hx, hy, s))) => {
                p.update(|pl| (pl.bubbles, pl.input) = (bubbles, input));
                apply_frame(&p, &w, pl.size, hx, hy, bubbles, input, s);
            }
            None => p.update(|pl| (pl.bubbles, pl.input) = (bubbles, input)),
        }
        p.emit(app, "portrait-reload", ());
        return;
    }
    if let Some(persona) = id.strip_prefix(MENU_PERSONA) {
        let persona = persona.to_string();
        p.update(|pl| pl.persona = persona);
        p.emit(app, "portrait-reload", ());
        // Портрет на столе сменил персону — кому теперь голос разговора
        let talk = lk(&p.shared.talk).clone();
        on_voice(app, talk);
    }
}

fn open(app: &AppHandle, p: &Portrait) {
    if let Some(w) = p.window(app) {
        let _ = w.show();
        return;
    }
    let pl = p.placement();
    let size = pl.size.clamp(SIZE_MIN, SIZE_MAX);
    let (x, y) = match (pl.x, pl.y) {
        (Some(x), Some(y)) if p.guest || on_screen(app, x, y, size) => (x, y),
        _ => corner(app, size),
    };
    // Поле — с той стороны, где больше места; голову, задвинутую за край
    // дальше, чем можно, — вернуть (fit). Гость — где поставили
    let side = p.side_at(app, x, y, size);
    let field = pl.input.then_some(side);
    let (x, y) = if p.guest { (x, y) } else { fit_on_screen(app, x, y, size) };
    if pl.side != side || (pl.x.is_some() && (pl.x, pl.y) != (Some(x), Some(y))) {
        let placed = pl.x.is_some();
        p.update(|pl| {
            pl.side = side;
            if placed {
                (pl.x, pl.y) = (Some(x), Some(y));
            }
        });
    }
    let (w, h, dx, dy) = frame(size, pl.bubbles, field, pl.window_room_for(size));
    let gen = p.gen.fetch_add(1, Ordering::SeqCst) + 1;
    *lk(&p.origin) = None;
    let built = WebviewWindowBuilder::new(app, p.label.as_str(), WebviewUrl::App("portrait.html".into()))
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
    // Голову можно задвинуть и за верх экрана: macOS прижала окно под строку
    // меню, когда показывала его, — поставить на место ещё раз
    platform::allow_offscreen(&win);
    let _ = win.set_position(LogicalPosition::new(x - dx, y - dy));
    // Сквозь окно, пока страница не скажет, что курсор над фигурой
    let _ = win.set_ignore_cursor_events(true);
    if let (Ok(o), Ok(s)) = (win.inner_position(), win.scale_factor()) {
        *lk(&p.origin) = Some((o.x as f64, o.y as f64, s));
    }
    let w2 = win.clone();
    let p2 = p.clone();
    win.on_window_event(move |e| match e {
        WindowEvent::Moved(pos) => {
            p2.moved(*pos, w2.scale_factor().unwrap_or(1.0));
            p2.send_visible(&w2);
            p2.fit_soon(&w2);
        }
        WindowEvent::Resized(_) => p2.send_visible(&w2),
        // Ушли в другое окно — клавиатура снова не наша, поле свернуть
        WindowEvent::Focused(false) => {
            let _ = w2.set_focusable(false);
            p2.emit(w2.app_handle(), "portrait-blur", ());
        }
        // Файл тащат на фигуру (мышь окно ловит только над ней) и бросили
        WindowEvent::DragDrop(ev) => match ev {
            DragDropEvent::Enter { .. } => p2.emit(w2.app_handle(), "portrait-file-over", FileOver { over: true }),
            DragDropEvent::Leave => p2.emit(w2.app_handle(), "portrait-file-over", FileOver { over: false }),
            DragDropEvent::Drop { paths, .. } => {
                p2.emit(w2.app_handle(), "portrait-file-over", FileOver { over: false });
                if let Some(path) = paths.first() {
                    drop_file(w2.app_handle().clone(), p2.clone(), path.clone());
                }
            }
            _ => {}
        },
        _ => {}
    });
    watch_cursor(app.clone(), p.clone(), gen);
    watch_said(app.clone(), p.clone(), gen);
}

/// Запас окна под анимации сменился — окно шире или выше, голова на месте
fn set_room(p: &Portrait, w: &WebviewWindow, room: Room) {
    let pl = p.placement();
    if pl.room == room {
        return;
    }
    let pos = head_pos(w, &pl);
    lk(&p.place).room = room;
    if let Some((hx, hy, s)) = pos {
        apply_frame(p, w, pl.size, hx, hy, pl.bubbles, pl.input, s);
    }
}

/// Где сейчас голова (логические px) и масштаб окна — по положению окна
fn head_pos(w: &WebviewWindow, pl: &Placement) -> Option<(f64, f64, f64)> {
    let (s, pos) = (w.scale_factor().ok()?, w.outer_position().ok()?);
    let (_, _, dx, dy) = frame(pl.size, pl.bubbles, pl.field(), pl.window_room());
    Some((pos.x as f64 / s + dx, pos.y as f64 / s + dy, s))
}

/// Окно — вокруг головы size в (hx, hy); место головы и окна — в настройки
#[allow(clippy::too_many_arguments)]
fn apply_frame(p: &Portrait, w: &WebviewWindow, size: f64, hx: f64, hy: f64, bubbles: bool, input: bool, scale: f64) {
    let side = p.side_at(w.app_handle(), hx, hy, size);
    let field = input.then_some(side);
    let pl = p.placement();
    let (hx, hy) = match area_for(w.app_handle(), hx, hy, size) {
        Some(area) if pl.player.width > 0.0 => fit_player(area, hx, hy, size, pl.player),
        _ => fit_on_screen(w.app_handle(), hx, hy, size),
    };
    let room = pl.window_room_for(size);
    let (fw, fh, dx, dy) = frame(size, bubbles, field, room);
    let flipped = {
        let mut pl = lk(&p.place);
        pl.size = size;
        pl.x = Some(hx);
        pl.y = Some(hy);
        std::mem::replace(&mut pl.side, side) != side && input
    };
    *lk(&p.origin) = Some(((hx - dx) * scale, (hy - dy) * scale, scale));
    let _ = w.set_size(LogicalSize::new(fw, fh));
    let _ = w.set_position(LogicalPosition::new(hx - dx, hy - dy));
    p.send_visible(w);
    p.save_soon();
    // Поле перешло на другую сторону — странице разложиться заново
    if flipped {
        p.emit(w.app_handle(), "portrait-reload", ());
    }
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
fn watch_cursor(app: AppHandle, p: Portrait, gen: u64) {
    std::thread::spawn(move || {
        let mut last: Option<(i64, i64)> = None;
        let mut moved_at = Instant::now();
        let mut idle_at = Instant::now();
        let mut away = false;
        let mut wait = POLL_NEAR;
        loop {
            std::thread::sleep(wait);
            if p.gen.load(Ordering::SeqCst) != gen || p.window(&app).is_none() {
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
                p.emit(&app, "portrait-cursor", Cursor { x, y });
            }
            if idle_at.elapsed() >= IDLE_EVERY {
                idle_at = Instant::now();
                // Системе не узнать — по одной мыши
                let idle = platform::idle_secs().unwrap_or_else(|| moved_at.elapsed().as_secs_f64());
                if (idle >= AWAY_AFTER) != away {
                    away = !away;
                    p.emit(&app, "portrait-away", Away { away });
                }
            }
            let pl = p.placement();
            let (w, h, _, _) = frame(pl.size, pl.bubbles, pl.field(), pl.window_room());
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
    /// Кнопки ответа на вопрос (ядро: BotInstance.cc_answer_options) — как
    /// есть, рисует страница
    #[serde(skip_serializing_if = "Option::is_none")]
    options: Option<serde_json::Value>,
}

/// Новые реплики персоны портрета — облачком (portrait-say). Долгий опрос
/// ленты реплик ядра /api/said: запрос висит до SAID_WAIT_SECS, пока персона
/// что-нибудь не скажет, — без частого опроса. Первый ответ только даёт
/// номер: сказанное до портрета уже не новость. Ядро перезапустили — оно
/// само отдаст сказанное за последние минуты (номер прошлого запуска)
fn watch_said(app: AppHandle, p: Portrait, gen: u64) {
    std::thread::spawn(move || {
        let mut cursor: Option<(u64, String)> = None;
        let mut fails = 0usize;
        loop {
            if p.gen.load(Ordering::SeqCst) != gen {
                return;
            }
            // acts=1 — и анимации, которые выбрала персона (kind act), emo=1 —
            // и выражения лица (kind emo): старые клиенты ленты их не получают
            let api = app.state::<AppState>().sup.api();
            let path = match &cursor {
                None => "/api/said?wait=0&acts=1&emo=1".to_string(),
                Some((since, epoch)) => format!("/api/said?wait={SAID_WAIT_SECS}&since={since}&epoch={epoch}&acts=1&emo=1"),
            };
            let v = match probe::api_get(&api, &path, (SAID_WAIT_SECS + 20) * 1000) {
                Ok(v) => v,
                Err(e) => {
                    // Ядро старее ленты реплик (404) — облачков не будет, спросить
                    // позже; не запущено или перезапускается — чаще
                    fails += 1;
                    let i = if e.starts_with("HTTP 404") { 2 } else { (fails - 1).min(2) };
                    std::thread::sleep(Duration::from_secs(SAID_RETRY_SECS[i]));
                    continue;
                }
            };
            fails = 0;
            let (Some(next), Some(epoch)) = (v["cursor"].as_u64(), v["epoch"].as_str()) else {
                std::thread::sleep(Duration::from_secs(SAID_RETRY_SECS[2]));
                continue;
            };
            cursor = Some((next, epoch.to_string()));
            if p.gen.load(Ordering::SeqCst) != gen {
                return;
            }
            // Скорость текста в облачке (настройка «Общие» в вебе) — до реплик:
            // им уже по ней
            if v["text"].is_object() {
                p.emit(&app, "portrait-text", v["text"].clone());
            }
            // В вебе сменили тему оформления — перечитать портрет: голова
            // рисуется скином (look приходит с портретом). Сохранили портрет
            // или анимации этой персоны — тоже сразу, а не к опросу раз в
            // 10 минут
            if has_look(&v) || has_portrait_change(&v, &p.persona()) {
                p.emit(&app, "portrait-reload", ());
            }
            // Облачка выключены — из ленты только анимации
            let pl = p.placement();
            for one in said_events(&v, &p.persona(), !pl.bubbles) {
                p.emit(&app, "portrait-say", one);
            }
        }
    });
}

/// Есть ли в ответе ленты событие look — в вебе сменили тему оформления
/// (app/core/ui_look); оно без персоны и для всех портретов
fn has_look(v: &serde_json::Value) -> bool {
    v["messages"].as_array().is_some_and(|a| a.iter().any(|m| m["kind"].as_str() == Some("look")))
}

/// Есть ли в ответе ленты событие portrait этой персоны — в вебе сохранили
/// её портрет или анимации (app/api/said.py, portrait_changed), — или mode:
/// в чате сменился её режим, а с ним аватар (app/api/avatar_modes)
fn has_portrait_change(v: &serde_json::Value, persona: &str) -> bool {
    !persona.is_empty()
        && v["messages"].as_array().is_some_and(|a| {
            a.iter().any(|m| matches!(m["kind"].as_str(), Some("portrait") | Some("mode")) && m["persona"].as_str() == Some(persona))
        })
}

/// События ленты для портрета персоны persona, по порядку: реплики (из
/// многих сразу — последние SAID_KEEP), пауза до ответа — heard (вы
/// написали: текст — ваше сообщение для облачка сбоку, у картинки без
/// подписи пусто) и settled без текста (ход кончился) — и act (персона
/// выбрала анимацию: текст — её id), emo (выражение лица: текст — id
/// эмоции). События паузы, act и emo проходят все; only_acts (облачка
/// выключены) — только act и emo
fn said_events(v: &serde_json::Value, persona: &str, only_acts: bool) -> Vec<Said> {
    let all: Vec<Said> = v["messages"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|m| m["persona"].as_str() == Some(persona))
        .filter_map(|m| {
            let text = m["text"].as_str().unwrap_or("").trim();
            let kind = m["kind"].as_str().unwrap_or("reply");
            (!text.is_empty() || kind == "heard" || kind == "settled").then(|| Said {
                text: text.to_string(),
                kind: kind.to_string(),
                options: m["options"]["options"].as_array().filter(|a| !a.is_empty()).map(|_| m["options"].clone()),
            })
        })
        .collect();
    let act = |s: &Said| s.kind == "act" || s.kind == "emo";
    if only_acts {
        return all.into_iter().filter(act).collect();
    }
    let pass = |s: &Said| s.kind == "heard" || s.kind == "settled" || act(s);
    let mut skip = all.iter().filter(|s| !pass(s)).count().saturating_sub(SAID_KEEP);
    all.into_iter()
        .filter(|s| {
            if pass(s) || skip == 0 {
                return true;
            }
            skip -= 1;
            false
        })
        .collect()
}

type Area = (f64, f64, f64, f64);

/// Рабочие области мониторов (без строки меню, Dock, панели задач) —
/// логические px: (x, y, ширина, высота)
fn work_areas(app: &AppHandle) -> Vec<Area> {
    let area = |m: &tauri::Monitor| {
        let (s, wa) = (m.scale_factor(), m.work_area());
        (wa.position.x as f64 / s, wa.position.y as f64 / s, wa.size.width as f64 / s, wa.size.height as f64 / s)
    };
    app.available_monitors().map(|ms| ms.iter().map(area).collect()).unwrap_or_default()
}

/// Сколько квадрата головы (x, y, size) в области a
fn overlap(a: Area, x: f64, y: f64, size: f64) -> f64 {
    let w = (a.0 + a.2).min(x + size) - a.0.max(x);
    let h = (a.1 + a.3).min(y + size) - a.1.max(y);
    w.max(0.0) * h.max(0.0)
}

/// Рабочая область основного монитора
fn primary_area(app: &AppHandle) -> Option<Area> {
    let m = app.primary_monitor().ok().flatten()?;
    let (s, wa) = (m.scale_factor(), m.work_area());
    Some((wa.position.x as f64 / s, wa.position.y as f64 / s, wa.size.width as f64 / s, wa.size.height as f64 / s))
}

/// Из областей — та, где больше всего головы (её середина может быть за
/// краем экрана); голова ни в одной — fallback
fn area_among(areas: &[Area], fallback: Option<Area>, hx: f64, hy: f64, size: f64) -> Option<Area> {
    areas
        .iter()
        .map(|a| (overlap(*a, hx, hy, size), *a))
        .filter(|(o, _)| *o > 0.0)
        .max_by(|a, b| a.0.total_cmp(&b.0))
        .map(|(_, a)| a)
        .or(fallback)
}

/// Рабочая область монитора, где больше всего головы; ни на одном — основного
fn area_for(app: &AppHandle, hx: f64, hy: f64, size: f64) -> Option<Area> {
    area_among(&work_areas(app), primary_area(app), hx, hy, size)
}

/// Голова хоть немного на каком-нибудь мониторе (монитор могли отключить)
fn on_screen(app: &AppHandle, x: f64, y: f64, size: f64) -> bool {
    let areas = work_areas(app);
    areas.is_empty() || areas.into_iter().any(|a| overlap(a, x, y, size) > 0.0)
}

/// Сколько головы остаётся на экране по каждой оси, когда её задвинули за край
fn keep(size: f64) -> f64 {
    (size * KEEP_FRAC).max(KEEP_MIN).min(size)
}

/// Голову можно задвинуть за край экрана, но на экране остаётся хотя бы
/// keep(size) по каждой оси: не потерять и есть за что взять. Поле и облачко
/// держит на видимой части окна страница (portrait-visible)
fn fit(area: Area, hx: f64, hy: f64, size: f64) -> (f64, f64) {
    let (ax, ay, aw, ah) = area;
    let k = keep(size);
    (hx.min(ax + aw - k).max(ax + k - size), hy.min(ay + ah - k).max(ay + k - size))
}

fn fit_on_screen(app: &AppHandle, hx: f64, hy: f64, size: f64) -> (f64, f64) {
    match area_for(app, hx, hy, size) {
        Some(area) => fit(area, hx, hy, size),
        None => (hx, hy),
    }
}

/// Keep the panel on the same monitor, moving the head only at a screen edge.
fn fit_player(area: Area, hx: f64, hy: f64, size: f64, player: PlayerSpace) -> (f64, f64) {
    let (ax, ay, aw, ah) = area;
    let right = (ax + aw - size - player.width - 20.0).max(ax);
    let bottom = (ay + ah - size).max(ay);
    let top = (ay + (player.height + 8.0 - size).max(0.0)).min(bottom);
    (hx.clamp(ax, right), hy.clamp(top, bottom))
}

/// Видимая часть окна (ox, oy — левый верхний угол, fw × fh) в области area —
/// в координатах страницы: [слева, сверху, справа, снизу]
fn visible_in(area: Area, ox: f64, oy: f64, fw: f64, fh: f64) -> [f64; 4] {
    let (ax, ay, aw, ah) = area;
    let l = (ax - ox).clamp(0.0, fw);
    let t = (ay - oy).clamp(0.0, fh);
    [l, t, (ax + aw - ox).clamp(l, fw), (ay + ah - oy).clamp(t, fh)]
}

/// Видимая часть окна портрета сейчас (по его месту и рамке)
/// Видимая часть окна, отступы и геометрия головы одним снимком.
fn visible_now(w: &WebviewWindow, pl: &Placement) -> Option<Visible> {
    let (s, pos) = (w.scale_factor().ok()?, w.outer_position().ok()?);
    let (ox, oy) = (pos.x as f64 / s, pos.y as f64 / s);
    let (_, _, dx, dy) = frame(pl.size, pl.bubbles, pl.field(), pl.window_room());
    let area = area_for(w.app_handle(), ox + dx, oy + dy, pl.size)?;
    Some(visible_frame(area, ox, oy, pl))
}

fn visible_frame(area: Area, ox: f64, oy: f64, pl: &Placement) -> Visible {
    let (fw, fh, dx, dy) = frame(pl.size, pl.bubbles, pl.field(), pl.window_room());
    let [l, t, r, b] = visible_in(area, ox, oy, fw, fh);
    let (pl_, pt, pr, pb) = pads(pl.size, pl.bubbles, pl.field(), pl.window_room());
    Visible {
        left: l, top: t, right: r, bottom: b, pad_l: pl_, pad_t: pt, pad_r: pr, pad_b: pb,
        layout: PortraitLayout {
            head_x: dx, head_y: dy, head_size: pl.size,
            bubble_h: if pl.bubbles { BUBBLE_H } else { 0.0 },
            input_w: if pl.input { FIELD_W } else { 0.0 }, input_side: pl.side,
        },
    }
}

/// Странице: видимая часть окна и запас под анимации — голова и всё у неё
/// (облачко, поле) стоят внутри окна с отступом padL, padT, padR, padB
#[derive(Clone, Copy, Debug, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Visible {
    left: f64,
    top: f64,
    right: f64,
    bottom: f64,
    pad_l: f64,
    pad_t: f64,
    pad_r: f64,
    pad_b: f64,
    layout: PortraitLayout,
}

/// Геометрия головы и колонок одним событием: поздний portrait_load или
/// resize не должен переставлять голову, когда прячется кнопка чата.
#[derive(Clone, Copy, Debug, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
struct PortraitLayout {
    head_x: f64,
    head_y: f64,
    head_size: f64,
    bubble_h: f64,
    input_w: f64,
    input_side: Side,
}

/// Поле — со стороны середины экрана: там больше места
fn side_in(area: Area, hx: f64, size: f64) -> Side {
    if hx + size / 2.0 > area.0 + area.2 / 2.0 {
        Side::Left
    } else {
        Side::Right
    }
}

/// С какой стороны головы в (hx, hy) ставить поле — по её монитору
fn side_for(app: &AppHandle, hx: f64, hy: f64, size: f64) -> Side {
    area_for(app, hx, hy, size).map(|a| side_in(a, hx, size)).unwrap_or(Side::Left)
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

/// Ответ странице: {persona, name, data (GET /api/personas/{p}/portrait),
/// avatar — облик фигуры, если листов портрета нет (null — по умолчанию),
/// offline — бот не ответил (data — из кэша, если был), gone — персоны нет,
/// asleep — спит ли персона (null — ядро не знает), away — вас нет у
/// компьютера, bubbles и bubbleH — облачка и место над головой под них,
/// text — скорость текста в облачке (null — ядро не знает), look — тема
/// оформления веба (app/core/ui_look; null — обычный вид), physics — «Ходит
/// по окнам», animsOn — «Анимации», anims — анимации персоны с атласами
/// (GET .../portrait/anims; нет — null)}
fn load(st: &AppState, p: &Portrait) -> Value {
    let api = st.sup.api();
    let list = probe::api_get(&api, "/api/portraits", 4000).ok();
    let core_up = list.is_some() || probe::health(&api);
    let mut persona = p.persona();
    if persona.is_empty() && !p.guest {
        // Сначала — с портретом; нет таких — любая (живая голова фигуры)
        let first = if core_up {
            list.as_ref()
                .and_then(|v| v["personas"].as_array()?.first()?.as_str().map(str::to_string))
                .or_else(|| persona_names(&api)?.into_iter().next().map(|(id, _)| id))
        } else {
            disk_ids(&api).into_iter().next().or_else(|| disk_persona_ids(&api).into_iter().next())
        };
        if let Some(first) = first {
            persona = first.clone();
            p.update(|pl| pl.persona = first);
        }
    }
    let pl = p.placement();
    let asleep = list.as_ref().map(|v| v["asleep"][&persona].clone()).unwrap_or(Value::Null);
    // Режим персоны в чате ("" — обычный; null — ядро не знает): страница
    // по смене играет переход
    let mode = list.as_ref().map(|v| v["mode"][&persona].clone()).filter(Value::is_string).unwrap_or(Value::Null);
    let text = list.as_ref().map(|v| v["text"].clone()).unwrap_or(Value::Null);
    let initiative_badge = if core_up && safe_id(&persona) {
        probe::api_get(&api, &format!("/api/personas/{persona}/portrait/initiative-badge"), 4000)
            .unwrap_or(Value::Null)
    } else {
        Value::Null
    };
    // Тема оформления веба: голова — рисовкой скина; от ядра, иначе с диска
    let look = list.as_ref().map(|v| v["look"].clone()).filter(Value::is_object).unwrap_or_else(|| disk_look(&api));
    let anims = if safe_id(&persona) { anims_for(p, &api, &persona, core_up, list.as_ref()) } else { Value::Null };
    let with_state = |mut v: Value| {
        v["asleep"] = asleep.clone();
        v["mode"] = mode.clone();
        v["text"] = text.clone();
        v["initiativeBadge"] = initiative_badge.clone();
        v["look"] = look.clone();
        v["physics"] = json!(pl.physics);
        v["animsOn"] = json!(pl.anims);
        v["soundsOn"] = json!(pl.sounds);
        v["anims"] = anims.clone();
        v["away"] = json!(away_now());
        v["bubbles"] = json!(pl.bubbles);
        v["bubbleH"] = json!(bare_frame(pl.size, pl.bubbles, pl.field()).3);
        v["input"] = json!(pl.input);
        v["inputW"] = json!(if pl.input { FIELD_W } else { 0.0 });
        v["inputSide"] = json!(pl.side);
        // Своих листов нет — голова фигуры: какой у неё облик
        if v["persona"].is_string() && v["gone"] != json!(true) && v["data"]["sheets"]["A"].is_null() {
            v["avatar"] = avatar_preset(&api, &persona, core_up);
        }
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
    with_state(fetch(p, &api, &persona))
}

/// Портрет с ядра целиком (атласы) → кэш; без связи — из кэша. live=1 —
/// аватар режима, что включён в чате (старое ядро параметр не знает и
/// отдаёт обычный)
fn fetch(p: &Portrait, api: &ApiEnv, persona: &str) -> Value {
    match probe::api_get(api, &format!("/api/personas/{persona}/portrait?live=1"), 20_000) {
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
            None => json!({ "persona": persona, "name": disk_name(api, persona), "data": null, "offline": true }),
        },
    }
}

/// Анимации персоны (GET .../portrait/anims с атласами): ядро отвечает — из
/// кэша, если метка в /api/portraits (anims) та же, иначе с ядра; ядро не
/// запущено — с диска, нет там — из кэша. Анимаций нет — null
fn anims_for(p: &Portrait, api: &ApiEnv, persona: &str, core_up: bool, list: Option<&Value>) -> Value {
    if !core_up {
        return disk_anims(api, persona).or_else(|| p.read_anims_cache(persona)).unwrap_or(Value::Null);
    }
    // Ядро старее анимаций (или список не ответил) — что было
    let Some(labels) = list.and_then(|v| v["anims"].as_object()) else {
        return p.read_anims_cache(persona).unwrap_or(Value::Null);
    };
    let Some(label) = labels.get(persona) else {
        let _ = fs::remove_file(p.anims_file(persona));
        return Value::Null;
    };
    if let Some(cached) = p.read_anims_cache(persona) {
        if !label.is_null() && cached["updated_at"] == *label {
            return cached;
        }
    }
    match probe::api_get(api, &format!("/api/personas/{persona}/portrait/anims?live=1"), 20_000) {
        Ok(v) => {
            p.write_anims_cache(persona, &v);
            v
        }
        Err(_) => p.read_anims_cache(persona).unwrap_or(Value::Null),
    }
}

/// Анимации в том же виде, что GET /api/personas/{p}/portrait/anims, — с
/// диска (app/api/portrait_anim.py: portrait/anims/anims.json и <id>.webp)
fn disk_anims(api: &ApiEnv, persona: &str) -> Option<Value> {
    let dir = disk_dir(api, persona).join("anims");
    let meta: Value = serde_json::from_str(&fs::read_to_string(dir.join("anims.json")).ok()?).ok()?;
    let anims: Vec<Value> = meta["anims"]
        .as_array()?
        .iter()
        .filter_map(|a| {
            let id = a["id"].as_str().filter(|id| safe_id(id))?;
            let bytes = fs::read(dir.join(format!("{id}.webp"))).ok()?;
            let mut a = a.clone();
            a["atlas"] = json!(format!("data:image/webp;base64,{}", base64::engine::general_purpose::STANDARD.encode(bytes)));
            // Свой звук анимации (<id>.sound, MIME — в записи)
            let mime = a["sound"].as_str().filter(|m| m.starts_with("audio/") && m.len() < 40).map(str::to_string);
            match mime.and_then(|m| Some((m, fs::read(dir.join(format!("{id}.sound"))).ok()?))) {
                Some((m, sound)) => {
                    a["sound_url"] = json!(format!("data:{m};base64,{}", base64::engine::general_purpose::STANDARD.encode(sound)));
                }
                None => a["sound"] = Value::Null,
            }
            Some(a)
        })
        .collect();
    let settings = if meta["settings"].is_object() { meta["settings"].clone() } else { json!({ "every_min": 4, "walk": true }) };
    Some(json!({ "anims": anims, "settings": settings, "updated_at": meta["updated_at"] }))
}

/// Кого предложить: все персоны (без своего портрета — живая голова фигуры)
/// и откуда список (true — бот не отвечает, из папки данных; там пусто —
/// текущая из кэша)
fn portrait_personas(st: &AppState) -> (Vec<(String, String)>, bool) {
    let api = st.sup.api();
    if let Some(list) = persona_names(&api) {
        return (list, false);
    }
    let mut ids = disk_persona_ids(&api);
    ids.extend(disk_ids(&api));
    ids.sort();
    ids.dedup();
    if !ids.is_empty() {
        return (ids.into_iter().map(|id| (id.clone(), disk_name(&api, &id))).collect(), true);
    }
    let current = st.portrait.placement().persona;
    let cached = st
        .portrait
        .read_cache(&current)
        .and_then(|v| v["name"].as_str().map(|n| vec![(current.clone(), n.to_string())]))
        .unwrap_or_default();
    (cached, true)
}

fn menu_personas(st: &AppState) -> Vec<(String, String)> {
    portrait_personas(st).0
}

/// Облик фигуры персоны в «Комнате» (пресет аватара в layout.json): с ядра,
/// а бот не запущен — из папки данных; не задан — null (страница возьмёт
/// облик по умолчанию)
fn avatar_preset(api: &ApiEnv, persona: &str, core_up: bool) -> Value {
    let layout = if core_up {
        probe::api_get(api, &format!("/api/personas/{persona}/room/layout"), 4000).ok()
    } else {
        let path = api.data.join(format!("api_{persona}")).join("room").join("layout.json");
        fs::read_to_string(path).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok())
    };
    layout.map(|v| v["avatar"].clone()).filter(Value::is_object).unwrap_or(Value::Null)
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

/// Все персоны в папке данных по их YAML: персоны пользователя
/// (data/personas) и встроенные (app/personas), по id
fn disk_persona_ids(api: &ApiEnv) -> Vec<String> {
    let mut ids: Vec<String> = [api.data.join("personas"), api.repo.join("app").join("personas")]
        .iter()
        .filter_map(|dir| fs::read_dir(dir).ok())
        .flatten()
        .filter_map(|e| {
            let name = e.ok()?.file_name().into_string().ok()?;
            let id = name.strip_suffix(".yaml")?.to_string();
            safe_id(&id).then_some(id)
        })
        .collect();
    ids.sort();
    ids.dedup();
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

/// Тема оформления веба (app/core/ui_look) с диска — бот не отвечает или
/// старее: data/ui_look.json; нет файла или битый — null (голова по умолчанию)
fn disk_look(api: &ApiEnv) -> Value {
    fs::read_to_string(api.data.join("ui_look.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .filter(Value::is_object)
        .unwrap_or(Value::Null)
}

/// Портрет в том же виде, что GET /api/personas/{p}/portrait, — с диска
fn disk_portrait(api: &ApiEnv, persona: &str) -> Option<Value> {
    let meta = disk_meta(api, persona)?;
    let dir = disk_dir(api, persona);
    let (mut sheets, mut settings) = (serde_json::Map::new(), serde_json::Map::new());
    // Гласные (VA…VO) — тоже: рот по гласным и без бота; эмоции — нет, их
    // выбирает персона, а бота нет
    for key in ["A", "B", "C", "D", "E", "F", "VA", "VI", "VU", "VE", "VO"] {
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
        let (list, from_disk) = portrait_personas(&st);
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
    let p = main_of(&app);
    if let Some(id) = persona.filter(|id| safe_id(id)) {
        if p.placement().persona != id {
            p.update(|pl| pl.persona = id);
            p.emit(&app, "portrait-reload", ());
            let talk = lk(&p.shared.talk).clone();
            on_voice(&app, talk);
        }
    }
    let open = p.window(&app).is_some();
    if shown && !open {
        show(&app);
    } else if !shown {
        hide(&app);
    }
}

/// Окно, из которого позвали команду: главное или гость
fn caller(app: &AppHandle, window: &WebviewWindow) -> Result<Portrait, String> {
    of(app, window.label()).ok_or_else(|| "нет такого окна портрета".to_string())
}

#[tauri::command]
pub async fn portrait_load(app: AppHandle, window: WebviewWindow, state: State<'_, AppState>) -> Result<Value, String> {
    let st = state.inner().clone();
    let p = caller(&app, &window)?;
    tauri::async_runtime::spawn_blocking(move || {
        let v = load(&st, &p);
        // Анимации крупнее головы — окну запас под них
        set_room(&p, &window, room_of(&v));
        // С физикой — висит в воздухе: упасть на опору; стоит на окне — следить
        motion::settle(&app, &p);
        v
    })
    .await
    .map_err(|e| e.to_string())
}

/// Настроение персоны портрета сейчас ({valence, arousal, tag}): лицо само
/// встаёт в эмоцию «по настроению». Ядро не ответило, живого состояния нет —
/// null: лицо обычное
#[tauri::command]
pub async fn portrait_mood(app: AppHandle, window: WebviewWindow, state: State<'_, AppState>) -> Result<Value, String> {
    let st = state.inner().clone();
    let p = caller(&app, &window)?;
    tauri::async_runtime::spawn_blocking(move || {
        let persona = p.persona();
        if !safe_id(&persona) {
            return Value::Null;
        }
        probe::api_get(&st.sup.api(), &format!("/api/personas/{persona}/portrait/mood"), 3000)
            .map(|v| v["mood"].clone())
            .unwrap_or(Value::Null)
    })
    .await
    .map_err(|e| e.to_string())
}

/// Курсор над фигурой — окно ловит мышь; мимо — клики проходят насквозь
#[tauri::command]
pub fn portrait_hit(window: WebviewWindow, hit: bool) {
    let _ = window.set_ignore_cursor_events(!hit);
}

/// Перетаскивание за голову (кнопка зажата и мышь сдвинулась). Своё, не
/// системное: системное не пускает окно выше строки меню, а голову можно
/// задвинуть и за верх экрана. Окно идёт за курсором, пока зажата кнопка
/// мыши, и голова упирается в край, за которым её не видно (fit). Кнопку не
/// узнать (не macOS и не Windows) — системное
#[tauri::command]
pub fn portrait_drag(app: AppHandle, window: WebviewWindow) {
    let Ok(p) = caller(&app, &window) else { return };
    let w = window;
    let down = platform::primary_button_down();
    let (Some(true), Ok(c0), Ok(o0), Ok(s)) = (down, app.cursor_position(), w.outer_position(), w.scale_factor()) else {
        if down.is_none() {
            let _ = w.start_dragging();
        }
        return;
    };
    if p.dragging.swap(true, Ordering::SeqCst) {
        return;
    }
    // Схватили: ходьба и падение гаснут, странице — held
    motion::grabbed(&app, &p);
    let pl = p.placement();
    let (_, _, dx, dy) = frame(pl.size, pl.bubbles, pl.field(), pl.window_room());
    // Мониторы за время перетаскивания не меняются — спросить один раз
    let (areas, fallback) = (work_areas(&app), primary_area(&app));
    std::thread::spawn(move || {
        let mut last = (o0.x, o0.y);
        // Где была голова последние мгновения — скорость броска
        let mut throw = motion::Throw::new();
        while platform::primary_button_down() == Some(true) && p.window(&app).is_some() {
            std::thread::sleep(DRAG_EVERY);
            let Ok(c) = app.cursor_position() else { continue };
            let hx = (o0.x as f64 + c.x - c0.x) / s + dx;
            let hy = (o0.y as f64 + c.y - c0.y) / s + dy;
            let (hx, hy) = match area_among(&areas, fallback, hx, hy, pl.size) {
                Some(area) => fit(area, hx, hy, pl.size),
                None => (hx, hy),
            };
            throw.push(hx, hy);
            let pos = (((hx - dx) * s).round() as i32, ((hy - dy) * s).round() as i32);
            if pos != last {
                last = pos;
                let _ = w.set_position(PhysicalPosition::new(pos.0, pos.1));
            }
        }
        p.dragging.store(false, Ordering::SeqCst);
        // Отпустили: с физикой — бросок и падение на опору, без — стоит, где отпустили
        motion::released(&app, &p, &throw);
        // Поле на сторону середины экрана, если голову перенесли (после падения)
        p.fit_soon(&w);
    });
}

/// Движение (portrait_motion.rs) — не в главном потоке: окна, мониторы, курсор
async fn motion_call<T: Send + 'static>(
    app: AppHandle,
    window: WebviewWindow,
    f: fn(&AppHandle, &Portrait) -> T,
) -> Result<T, String> {
    let p = caller(&app, &window)?;
    tauri::async_runtime::spawn_blocking(move || f(&app, &p)).await.map_err(|e| e.to_string())
}

/// Страница: клавиши и кнопки мыши анимаций её персоны (anim.keys) —
/// панель слушает их по всей системе (input_watch.rs) и шлёт окну
/// portrait-key. Пусто — окну ничего не нужно. Ответ — слушает ли панель:
/// off, on, mouse (без доступа к клавиатуре), unsupported
#[tauri::command]
pub async fn portrait_keys(app: AppHandle, window: WebviewWindow, keys: Vec<String>) -> Result<&'static str, String> {
    let label = window.label().to_string();
    tauri::async_runtime::spawn_blocking(move || crate::input_watch::set(&app, &label, keys).as_str())
        .await
        .map_err(|e| e.to_string())
}

/// Открыть настройку доступа к вводу (macOS: «Мониторинг ввода»)
#[tauri::command]
pub fn portrait_keys_settings() {
    crate::input_watch::open_settings();
}

/// Страница: есть ли у персоны своя анимация шагов. Без неё сама не гуляет,
/// а «Иди сюда» и «Спрячься» переезжают быстро, без шагов
#[tauri::command]
pub fn portrait_walker(app: AppHandle, window: WebviewWindow, on: bool) {
    if let Ok(p) = caller(&app, &window) {
        p.motion.set_walker(on);
    }
}

/// Шаги фигуры из кадров ходьбы (motion::Motion::gait): step — px, на
/// сколько окну пройти на этой смене кадра (0 — шаги будут); null — их нет,
/// окно идёт ровно
#[tauri::command]
pub fn portrait_gait(app: AppHandle, window: WebviewWindow, step: Option<f64>) {
    if let Ok(p) = caller(&app, &window) {
        p.motion.gait(step);
    }
}

/// Ротация страницы: прогулка по текущей опоре. false — своих шагов нет,
/// некуда или занята
#[tauri::command]
pub async fn portrait_stroll(app: AppHandle, window: WebviewWindow) -> Result<bool, String> {
    motion_call(app, window, motion::stroll).await
}

/// Ротация страницы: прыжок на край окна поблизости. false — физика
/// выключена, окон рядом нет или занята
#[tauri::command]
pub async fn portrait_hop(app: AppHandle, window: WebviewWindow) -> Result<bool, String> {
    motion_call(app, window, motion::hop).await
}

/// «Иди сюда»: к курсору
#[tauri::command]
pub async fn portrait_come(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    motion_call(app, window, motion::come).await
}

/// Анимация подглядывает из-за края экрана (anim.edge): голову — так, чтобы
/// линия из кадра стояла на краю (motion::peek). Ответ — у какого края и
/// отражена ли; null — сейчас нельзя (в воздухе, в руке), не играть
#[tauri::command]
pub async fn portrait_peek(
    app: AppHandle,
    window: WebviewWindow,
    side: String,
    off: f64,
    off_mirror: f64,
) -> Result<Option<motion::Peeked>, String> {
    let p = caller(&app, &window)?;
    tauri::async_runtime::spawn_blocking(move || motion::peek(&app, &p, &side, off, off_mirror))
        .await
        .map_err(|e| e.to_string())
}

/// Подглядывание кончилось — назад, где стояла
#[tauri::command]
pub async fn portrait_peek_end(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    motion_call(app, window, motion::peek_end).await
}

/// «Спрячься» за край экрана; уже спрятана — «Выгляни»
#[tauri::command]
pub async fn portrait_hide(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    motion_call(app, window, motion::hide_toggle).await
}

/// Остановить ходьбу (падение и прыжок — нет)
#[tauri::command]
pub async fn portrait_stop(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    motion_call(app, window, motion::stop).await
}

/// Колёсико над головой: размер в factor раз, низ и середина головы — на месте
#[tauri::command]
pub fn portrait_resize(app: AppHandle, window: WebviewWindow, factor: f64) {
    let Ok(p) = caller(&app, &window) else { return };
    let w = window;
    if !factor.is_finite() || factor <= 0.0 {
        return;
    }
    let p = &p;
    let pl = p.placement();
    let Some((hx, hy, s)) = head_pos(&w, &pl) else { return };
    let old = pl.size;
    let new = (old * factor).clamp(SIZE_MIN, SIZE_MAX).round();
    if (new - old).abs() < 1.0 {
        return;
    }
    apply_frame(p, &w, new, hx + (old - new) / 2.0, hy + (old - new), pl.bubbles, pl.input, s);
}

/// Погладили: ядру — сигнал pet (до персоны дойдёт, если включены «тычки»)
#[tauri::command]
pub fn portrait_pet(app: AppHandle, window: WebviewWindow, state: State<'_, AppState>) {
    let Ok(p) = caller(&app, &window) else { return };
    {
        let mut at = lk(&p.pet_at);
        if at.map(|t| t.elapsed() < PET_POKE_EVERY).unwrap_or(false) {
            return;
        }
        *at = Some(Instant::now());
    }
    let persona = p.persona();
    if !safe_id(&persona) {
        return;
    }
    let api = state.sup.api();
    std::thread::spawn(move || {
        let body = json!({ "chat_id": "auto", "kind": "pet" });
        let _ = probe::api_post_json(&api, &format!("/api/personas/{persona}/room/poke"), &body, 8000);
    });
}

/// Поле для сообщений: реплика персоне портрета, как из веба. Ответ придёт
/// облачком из ленты реплик; здесь он тоже возвращается (reply и досланные
/// части) — на случай ответа, который в историю не ложится (слэш-команды):
/// повтор страница отсеет. Ошибка: "offline" — бот не отвечает, иначе текст
#[tauri::command]
pub async fn portrait_send(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, AppState>,
    text: String,
) -> Result<Vec<String>, String> {
    let st = state.inner().clone();
    let persona = caller(&app, &window)?.persona();
    tauri::async_runtime::spawn_blocking(move || {
        let text = text.trim().to_string();
        if text.is_empty() || !safe_id(&persona) {
            return Ok(Vec::new());
        }
        let body = json!({ "persona": persona, "message": text, "client_id": CLIENT_ID });
        chat(&st.sup.api(), &body)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Ошибка запроса к ядру — странице: нет связи — "offline", иначе detail ядра
fn api_error(e: String) -> String {
    match e.strip_prefix("HTTP ") {
        Some(rest) => rest.split_once(": ").map(|(_, d)| d.to_string()).unwrap_or_else(|| e.clone()),
        None => "offline".into(),
    }
}

/// Реплика персоне (POST /api/chat): ответ и досланные части
fn chat(api: &ApiEnv, body: &Value) -> Result<Vec<String>, String> {
    let v = probe::api_post_json(api, "/api/chat", body, SEND_TIMEOUT_MS).map_err(api_error)?;
    Ok(chat_replies(&v))
}

/// История переписки с персоной портрета (GET /api/chat/history, тот же чат,
/// что у веба, — portrait_send шлёт без user_id): мини-чату у поля, вся,
/// что есть у ядра. Ядро не отвечает — пусто, страница покажет то, что
/// собрала сама
#[tauri::command]
pub async fn portrait_history(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, AppState>,
) -> Result<Value, String> {
    let st = state.inner().clone();
    let persona = caller(&app, &window)?.persona();
    tauri::async_runtime::spawn_blocking(move || {
        if !safe_id(&persona) {
            return Ok(Value::Array(Vec::new()));
        }
        let api = st.sup.api();
        let path = format!("/api/chat/history?persona={persona}&user_id=web_user");
        let rows = probe::api_get(&api, &path, HISTORY_TIMEOUT_MS)
            .map_err(api_error)?
            .as_array()
            .cloned()
            .unwrap_or_default();
        Ok(Value::Array(rows))
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub async fn portrait_player_status(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, AppState>,
) -> Result<Value, String> {
    caller(&app, &window)?;
    let st = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        probe::api_get(&st.sup.api(), "/api/player/browser", 8000).map_err(api_error)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
pub fn portrait_player_layout(app: AppHandle, window: WebviewWindow, width: f64, height: f64) -> Result<(), String> {
    let p = caller(&app, &window)?;
    if !width.is_finite() || !height.is_finite() || !(0.0..=320.0).contains(&width)
        || !(0.0..=600.0).contains(&height) || (width == 0.0) != (height == 0.0) {
        return Err("Invalid player layout".into());
    }
    let player = PlayerSpace { width, height };
    let pl = p.placement();
    if pl.player == player { return Ok(()); }
    let pos = head_pos(&window, &pl);
    lk(&p.place).player = player;
    if let Some((hx, hy, scale)) = pos {
        apply_frame(&p, &window, pl.size, hx, hy, pl.bubbles, pl.input, scale);
        motion::settle(&app, &p);
    }
    Ok(())
}

fn player_control_body(command: &str, value: Option<f64>) -> Result<Value, String> {
    match command {
        "pause" | "resume" | "stop" | "next" | "previous" | "volume_up" | "volume_down" => Ok(json!({"command": command})),
        "volume" | "seek" => {
            let n = value.filter(|v| v.is_finite() && *v >= 0.0 && *v <= if command == "volume" { 1.0 } else { 864000.0 })
                .ok_or_else(|| "Invalid player value".to_string())?;
            Ok(json!({"command": command, "value": n}))
        }
        _ => Err("Unknown player command".into()),
    }
}

#[tauri::command]
pub async fn portrait_player_control(
    app: AppHandle,
    window: WebviewWindow,
    state: State<'_, AppState>,
    command: String,
    value: Option<f64>,
) -> Result<Value, String> {
    caller(&app, &window)?;
    let body = player_control_body(&command, value)?;
    let st = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        probe::api_post_json(&st.sup.api(), "/api/player/browser/control",
            &body, 45000).map_err(api_error)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Что из ответа /api/chat говорить облачком. Ответ хода агента задач
/// (режим управления) начинается строками хода («Нажал …», «Листаю
/// страницу.») — они в журнале карточки задачи (task.log), веб прячет их из
/// пузыря, лента реплик ядра — из облачка; здесь так же
fn chat_replies(v: &Value) -> Vec<String> {
    let log: std::collections::HashSet<&str> = v["task"]["log"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|x| x["text"].as_str())
        .collect();
    let said = |m: &str| -> String {
        if log.is_empty() {
            return m.to_string();
        }
        m.split('\n').filter(|ln| !log.contains(ln.trim())).collect::<Vec<_>>().join("\n").trim().to_string()
    };
    let reply = v["reply"].as_str().map(said);
    let extra = v["extra_messages"].as_array().into_iter().flatten().filter_map(|m| m.as_str().map(said));
    reply.into_iter().chain(extra).filter(|m| !m.trim().is_empty()).collect()
}

#[derive(Clone, Serialize)]
struct FileOver {
    over: bool,
}

#[derive(Clone, Serialize)]
struct FileSent {
    name: String,
    kind: &'static str,
}

#[derive(Clone, Serialize)]
struct FileDone {
    ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
    /// Ответы персоны (как у portrait_send): обычно они уже пришли лентой
    replies: Vec<String>,
}

/// Картинка ли это — по расширению: тип для data-URL
fn image_mime(path: &std::path::Path) -> Option<&'static str> {
    let ext = path.extension()?.to_string_lossy().to_lowercase();
    IMAGE_TYPES.iter().find(|(e, _)| *e == ext).map(|(_, m)| *m)
}

/// Файл бросили на фигуру: странице — portrait-file (имя, картинка или
/// документ), персоне — файл, потом portrait-file-done
fn drop_file(app: AppHandle, p: Portrait, path: PathBuf) {
    std::thread::spawn(move || {
        let st = app.state::<AppState>().inner().clone();
        let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let mime = image_mime(&path);
        let kind = if mime.is_some() { "image" } else { "doc" };
        p.emit(&app, "portrait-file", FileSent { name: name.clone(), kind });
        let done = match send_file(&st, &p.persona(), &path, &name, mime) {
            Ok(replies) => FileDone { ok: true, error: None, replies },
            Err(e) => FileDone { ok: false, error: Some(e), replies: Vec::new() },
        };
        p.emit(&app, "portrait-file-done", done);
    });
}

/// Файл персоне портрета: картинка — в чат вложением (как из веба), документ
/// — в её файлы (POST .../files), потом просьба прочитать его целиком
/// («проанализируй» — BotInstance._is_full_doc_request). Ok — ответы; Err —
/// "not_file" (папка, не читается), "too_big", "no_files" (у персоны
/// выключены файлы), "offline" или detail ядра
fn send_file(
    st: &AppState,
    persona: &str,
    path: &std::path::Path,
    name: &str,
    mime: Option<&str>,
) -> Result<Vec<String>, String> {
    if !safe_id(persona) {
        return Err("offline".into());
    }
    let meta = fs::metadata(path).map_err(|_| "not_file".to_string())?;
    if !meta.is_file() {
        return Err("not_file".into());
    }
    if meta.len() > if mime.is_some() { DROP_IMAGE_MAX } else { DROP_DOC_MAX } {
        return Err("too_big".into());
    }
    let bytes = fs::read(path).map_err(|_| "not_file".to_string())?;
    let api = st.sup.api();
    let body = match mime {
        Some(m) => {
            let url = format!("data:{m};base64,{}", base64::engine::general_purpose::STANDARD.encode(&bytes));
            json!({ "persona": persona, "message": "", "image": url, "client_id": CLIENT_ID })
        }
        None => {
            probe::api_post_file(&api, &format!("/api/personas/{persona}/files"), "file", name, &bytes, 120_000).map_err(|e| {
                if e.contains("file_upload") {
                    "no_files".to_string()
                } else {
                    api_error(e)
                }
            })?;
            json!({ "persona": persona, "message": tf(st.lang, "portrait_file_ask", name), "client_id": CLIENT_ID })
        }
    };
    chat(&api, &body)
}

/// Щёлкнули в поле для сообщений — окно берёт клавиатуру (on), вышли из
/// поля — отдаёт: клик по голове фокус из вашего окна не уводит
#[tauri::command]
pub fn portrait_focus(window: WebviewWindow, on: bool) {
    let _ = window.set_focusable(on);
    if on {
        let _ = window.set_focus();
    }
}

/// Какая часть окна на экране — для страницы при загрузке (дальше —
/// событием portrait-visible)
#[tauri::command]
pub fn portrait_visible(app: AppHandle, window: WebviewWindow) -> Option<Visible> {
    let p = caller(&app, &window).ok()?;
    let v = visible_now(&window, &p.placement())?;
    *lk(&p.visible) = Some(v);
    Some(v)
}

/// Разговор голосом для этого окна сейчас — для страницы при загрузке
/// (дальше — событием portrait-voice): окно открылось на имени, а вы уже
/// говорите. Слушает не его персона — None
#[tauri::command]
pub fn portrait_voice(app: AppHandle, window: WebviewWindow) -> Option<WakeTalk> {
    let p = caller(&app, &window).ok()?;
    let v = lk(&p.voice).clone();
    v
}

/// Состояние слушателя для кнопки микрофона — при загрузке страницы
/// (дальше — событием portrait-mic)
#[tauri::command]
pub fn portrait_mic(state: State<'_, AppState>) -> VoiceView {
    state.voice.view()
}

/// Кнопка микрофона у головы: выключить или включить слушание — та же
/// настройка, что «Слушать имя» в трее. Выключили посреди разговора —
/// разговор кончился (voice.rs). Остальным окнам — сразу, не ждать монитора
#[tauri::command]
pub fn portrait_mic_toggle(app: AppHandle, state: State<'_, AppState>) -> Result<VoiceView, String> {
    state.voice.set_enabled(!state.sup.wake_enabled())?;
    let view = state.voice.view();
    on_mic(&app, view.clone());
    Ok(view)
}

/// Клик по облачку: чат с персоной в вебе
#[tauri::command]
pub fn portrait_open_chat(app: AppHandle, window: WebviewWindow, state: State<'_, AppState>) {
    let Ok(p) = caller(&app, &window) else { return };
    let persona = p.persona();
    let base = crate::commands::web_url(&state.sup.config());
    let url = if safe_id(&persona) { format!("{base}/?chat={persona}") } else { base };
    let _ = app.opener().open_url(url, None::<&str>);
}

/// Меню правой кнопки: чей портрет, облачка и «Убрать с рабочего стола».
/// У гостя (портрет персоны разговора) — только движение и «Убрать»: чей
/// он, решает разговор, настройки — у портрета на столе. id пунктов —
/// пункт@метка окна (on_menu)
#[tauri::command]
pub async fn portrait_menu(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    let p = caller(&app, &window)?;
    let st = app.state::<AppState>().inner().clone();
    let st2 = st.clone();
    let list = if p.guest {
        Vec::new()
    } else {
        tauri::async_runtime::spawn_blocking(move || menu_personas(&st2)).await.map_err(|e| e.to_string())?
    };
    let pl = p.placement();
    let current = p.persona();
    let id = |item: &str| format!("{item}@{}", p.label);
    let err = |e: tauri::Error| e.to_string();
    let menu = Menu::new(&app).map_err(err)?;
    for (pid, name) in &list {
        let item = CheckMenuItem::with_id(&app, id(&format!("{MENU_PERSONA}{pid}")), name, true, *pid == current, None::<&str>)
            .map_err(err)?;
        menu.append(&item).map_err(err)?;
    }
    if !list.is_empty() {
        menu.append(&PredefinedMenuItem::separator(&app).map_err(err)?).map_err(err)?;
    }
    // Движение: к курсору, спрятаться за край или выглянуть
    let come = MenuItem::with_id(&app, id(MENU_COME), t(st.lang, "portrait_come"), true, None::<&str>).map_err(err)?;
    menu.append(&come).map_err(err)?;
    let hidden = p.motion.hidden();
    let away = t(st.lang, if hidden { "portrait_peek" } else { "portrait_hideaway" });
    let hideaway = MenuItem::with_id(&app, id(MENU_HIDEAWAY), away, true, None::<&str>).map_err(err)?;
    menu.append(&hideaway).map_err(err)?;
    // Разговор голосом с этой персоной — закончить, микрофон слушает дальше
    if lk(&p.voice).is_some() {
        let end = MenuItem::with_id(&app, id(MENU_TALK_END), t(st.lang, "portrait_talk_end"), true, None::<&str>)
            .map_err(err)?;
        menu.append(&end).map_err(err)?;
    }
    if !p.guest {
        // «Посиди со мной»: 25 / 50 минут, «Хватит» — пока идёт
        let focusing = lk(&p.focus_until).map(|u| u > Instant::now()).unwrap_or(false);
        let focus = Submenu::new(&app, t(st.lang, "portrait_focus"), true).map_err(err)?;
        for min in FOCUS_MINUTES {
            let label = tf(st.lang, "portrait_focus_min", min);
            let item = MenuItem::with_id(&app, id(&format!("{MENU_FOCUS}{min}")), label, true, None::<&str>).map_err(err)?;
            focus.append(&item).map_err(err)?;
        }
        let enough =
            MenuItem::with_id(&app, id(&format!("{MENU_FOCUS}0")), t(st.lang, "portrait_focus_stop"), focusing, None::<&str>)
                .map_err(err)?;
        focus.append(&enough).map_err(err)?;
        menu.append(&focus).map_err(err)?;
    }
    menu.append(&PredefinedMenuItem::separator(&app).map_err(err)?).map_err(err)?;
    if !p.guest {
        let bubbles =
            CheckMenuItem::with_id(&app, id(MENU_BUBBLES), t(st.lang, "portrait_bubbles"), true, pl.bubbles, None::<&str>)
                .map_err(err)?;
        menu.append(&bubbles).map_err(err)?;
        let input = CheckMenuItem::with_id(&app, id(MENU_INPUT), t(st.lang, "portrait_input"), true, pl.input, None::<&str>)
            .map_err(err)?;
        menu.append(&input).map_err(err)?;
        let physics =
            CheckMenuItem::with_id(&app, id(MENU_PHYSICS), t(st.lang, "portrait_physics"), true, pl.physics, None::<&str>)
                .map_err(err)?;
        menu.append(&physics).map_err(err)?;
        let anims = CheckMenuItem::with_id(&app, id(MENU_ANIMS), t(st.lang, "portrait_anims"), true, pl.anims, None::<&str>)
            .map_err(err)?;
        menu.append(&anims).map_err(err)?;
        let sounds =
            CheckMenuItem::with_id(&app, id(MENU_SOUNDS), t(st.lang, "portrait_sounds"), pl.anims, pl.sounds, None::<&str>)
                .map_err(err)?;
        menu.append(&sounds).map_err(err)?;
    }
    let hide_text = t(st.lang, if p.guest { "portrait_leave" } else { "portrait_hide" });
    let hide = MenuItem::with_id(&app, id(MENU_HIDE), hide_text, true, None::<&str>).map_err(err)?;
    menu.append(&hide).map_err(err)?;
    window.popup_menu(&menu).map_err(err)
}

#[cfg(test)]
mod tests {
    use super::{fit_player, player_control_body, PlayerSpace};
    use serde_json::json;
    use super::{
        avatar_preset, beside, chat_replies, disk_anims, disk_ids, disk_look, disk_name, disk_persona_ids, disk_portrait, fit, frame,
        has_look, has_portrait_change, image_mime, keep, pads, room_of, safe_id, said_events, side_in, visible_in, visible_frame, Placement, Room, Side, BUBBLE_H, BUBBLE_W, FIELD_W, GUEST_GAP, KEEP_MIN, SIZE_DEFAULT,
    };
    use crate::config::ApiEnv;
    use std::fs;

    #[test]
    fn player_space_preserves_head_and_animation_room() {
        let mut pl = Placement { size: 240.0, bubbles: false, input: false, ..Placement::default() };
        pl.player = PlayerSpace { width: 60.0, height: 334.0 };
        let (w, h, dx, dy) = frame(pl.size, pl.bubbles, pl.field(), pl.window_room());
        assert_eq!((w, h, dx, dy), (320.0, 342.0, 0.0, 102.0));
        pl.player = PlayerSpace { width: 292.0, height: 286.0 };
        let (w, h, dx, dy) = frame(pl.size, pl.bubbles, pl.field(), pl.window_room());
        assert_eq!((w, h, dx, dy), (552.0, 294.0, 0.0, 54.0));
        // Resizing the head does not scale the player's reserved pixel width.
        let (w, h, _, _) = frame(360.0, false, None, pl.window_room_for(360.0));
        assert_eq!((w, h), (672.0, 360.0));
        pl.room.kr = 2.0;
        assert_eq!(pl.window_room().kr, 2.0);
        let saved = serde_json::to_value(&pl).unwrap();
        assert!(saved.get("player").is_none());
        pl.player = PlayerSpace::default();
        assert_eq!(pl.window_room(), pl.room);
    }

    #[test]
    fn visible_geometry_tracks_side_changes_without_changing_head_position() {
        let area = (0.0, 0.0, 1600.0, 1000.0);
        let mut pl = Placement { size: 240.0, side: Side::Left, ..Placement::default() };
        let (hx, hy) = (600.0, 200.0);
        let (_, _, dx, dy) = frame(pl.size, pl.bubbles, pl.field(), pl.window_room());
        let left = visible_frame(area, hx - dx, hy - dy, &pl);
        assert_eq!((left.layout.head_x + hx - dx, left.layout.head_y + hy - dy), (hx, hy));
        pl.side = Side::Right;
        let (_, _, dx, dy) = frame(pl.size, pl.bubbles, pl.field(), pl.window_room());
        let right = visible_frame(area, hx - dx, hy - dy, &pl);
        assert_eq!((right.layout.head_x + hx - dx, right.layout.head_y + hy - dy), (hx, hy));
        // Раньше эти значения были всем ключом кэша: смена стороны терялась,
        // пока запоздалый portrait_load не переставлял голову на ширину чата.
        assert_eq!((left.left, left.top, left.right, left.bottom, left.pad_l, left.pad_t, left.pad_r, left.pad_b),
                   (right.left, right.top, right.right, right.bottom, right.pad_l, right.pad_t, right.pad_r, right.pad_b));
        assert_ne!(left, right);
        let v = serde_json::to_value(right).unwrap();
        assert_eq!(v["layout"], json!({"headX": 0.0, "headY": BUBBLE_H, "headSize": 240.0,
            "bubbleH": BUBBLE_H, "inputW": FIELD_W, "inputSide": "right"}));
    }

    #[test]
    fn player_fits_to_right_without_moving_an_interior_head() {
        let area = (0.0, 0.0, 1000.0, 800.0);
        let panel = PlayerSpace { width: 292.0, height: 286.0 };
        assert_eq!(fit_player(area, 300.0, 350.0, 240.0, panel), (300.0, 350.0));
        assert_eq!(fit_player(area, 900.0, 20.0, 240.0, panel), (448.0, 54.0));
    }

    #[test]
    fn player_proxy_checks_sliders_and_supports_full_close() {
        assert_eq!(player_control_body("stop", None).unwrap(), json!({"command": "stop"}));
        assert_eq!(player_control_body("volume", Some(0.4)).unwrap(), json!({"command": "volume", "value": 0.4}));
        assert!(player_control_body("volume", Some(1.1)).is_err());
        assert!(player_control_body("volume", Some(f64::NAN)).is_err());
        assert!(player_control_body("seek", Some(-1.0)).is_err());
        assert!(player_control_body("seek", None).is_err());
        assert!(player_control_body("open", None).is_err());
    }

    #[test]
    fn guest_beside() {
        // Сосед — голова 240 в (1200, 700), поле слева (стоит справа на экране)
        let lp = Placement { size: 240.0, side: Side::Left, ..Placement::default() };
        let lx0 = 1200.0 - FIELD_W;
        let (x, y, side) = beside(1200.0, 700.0, &lp, 240.0, true, true, true);
        // Гость слева: его окно — [x − поле, x + 240], до соседа — GUEST_GAP
        assert_eq!(side, Side::Left);
        assert!((x + 240.0 + GUEST_GAP - lx0).abs() < 1e-9, "x={x}");
        assert_eq!(y, 700.0);
        // Вправо: поле гостя справа, окна не заходят одно на другое
        let lp = Placement { size: 240.0, side: Side::Right, ..Placement::default() };
        let (x, _, side) = beside(300.0, 700.0, &lp, 240.0, true, true, false);
        assert_eq!(side, Side::Right);
        assert!((x - (300.0 + 240.0 + FIELD_W + GUEST_GAP)).abs() < 1e-9, "x={x}");
        // Гость меньше соседа — ноги на одной линии
        let lp = Placement { size: 320.0, ..Placement::default() };
        let (_, y, _) = beside(800.0, 500.0, &lp, 240.0, true, false, true);
        assert_eq!(y + 240.0, 500.0 + 320.0);
        // Без поля и облачков — вплотную через зазор
        let lp = Placement { size: 240.0, bubbles: false, input: false, ..Placement::default() };
        let (x, _, _) = beside(800.0, 500.0, &lp, 240.0, false, false, true);
        assert!((x - (800.0 - GUEST_GAP - 240.0)).abs() < 1e-9);
    }

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
        assert!(p.input && p.side == Side::Left && p.field() == Some(Side::Left));
        let p: Placement = serde_json::from_str(r#"{"input":false,"side":"right"}"#).unwrap();
        assert_eq!((p.side, p.field()), (Side::Right, None));
        // Старый файл без физики и анимаций — обе включены; выключенные — так и есть
        assert!(p.physics && p.anims);
        let p: Placement = serde_json::from_str(r#"{"physics":false,"anims":false}"#).unwrap();
        assert!(!p.physics && !p.anims);
    }

    #[test]
    fn dropped_file_kind() {
        let kind = |name: &str| image_mime(std::path::Path::new(name));
        assert_eq!(kind("/tmp/Фото.JPG"), Some("image/jpeg"));
        assert_eq!(kind("a.webp"), Some("image/webp"));
        assert_eq!(kind("отчёт.pdf"), None);
        assert_eq!(kind("README"), None);
    }

    #[test]
    fn anims_from_disk() {
        // Бот не запущен: анимации — из data/api_<p>/portrait/anims
        let root = std::env::temp_dir().join(format!("vpc-portrait-anims-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let dir = root.join("data").join("api_don").join("portrait").join("anims");
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            dir.join("anims.json"),
            r#"{"anims":[{"id":"a1b2c3d4","name":"Ест","frames":4,"sound":"audio/wav"},{"id":"b1b2c3d4","name":"Звук потерян","sound":"audio/mpeg"},{"id":"deadbeef","name":"без атласа"},{"id":"../x","name":"плохой id"}],"updated_at":7.5}"#,
        )
        .unwrap();
        fs::write(dir.join("a1b2c3d4.webp"), b"RIFFa").unwrap();
        fs::write(dir.join("a1b2c3d4.sound"), b"RIFFb").unwrap();
        fs::write(dir.join("b1b2c3d4.webp"), b"RIFFa").unwrap();
        let api = ApiEnv::read(&root);
        let v = disk_anims(&api, "don").unwrap();
        let list = v["anims"].as_array().unwrap();
        assert_eq!(list.len(), 2);
        assert_eq!((list[0]["name"].as_str(), list[0]["frames"].as_u64()), (Some("Ест"), Some(4)));
        assert_eq!(list[0]["atlas"], "data:image/webp;base64,UklGRmE=");
        // Звук — data-URL с MIME из записи; файла нет — без звука
        assert_eq!(list[0]["sound_url"], "data:audio/wav;base64,UklGRmI=");
        assert!(list[1]["sound"].is_null() && list[1].get("sound_url").is_none());
        // Настроек в файле нет — по умолчанию; метка — как в файле
        assert_eq!((v["settings"]["every_min"].as_u64(), v["settings"]["walk"].as_bool()), (Some(4), Some(true)));
        assert_eq!(v["updated_at"].as_f64(), Some(7.5));
        // Анимаций нет — None
        assert!(disk_anims(&api, "kara").is_none());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn said_acts() {
        // act — анимация, которую выбрала персона: проходит всегда и не
        // вытесняет реплики из SAID_KEEP; облачка выключены — только act
        let one = |kind: &str, text: &str| serde_json::json!({"persona": "don", "kind": kind, "text": text});
        let v = serde_json::json!({"messages": [
            one("reply", "1"), one("act", "a1b2c3d4"), one("reply", "2"), one("reply", "3"),
            one("reply", "4"), one("act", ""), one("heard", "x"),
            {"persona": "kara", "kind": "act", "text": "ffff0000"},
        ]});
        let got: Vec<String> = said_events(&v, "don", false).into_iter().map(|s| format!("{}:{}", s.kind, s.text)).collect();
        assert_eq!(got, ["act:a1b2c3d4", "reply:2", "reply:3", "reply:4", "heard:x"]);
        let got: Vec<String> = said_events(&v, "don", true).into_iter().map(|s| format!("{}:{}", s.kind, s.text)).collect();
        assert_eq!(got, ["act:a1b2c3d4"]);
    }

    #[test]
    fn said_emotions() {
        // emo — выражение лица к ответу: как act, проходит всегда, и при
        // выключенных облачках тоже
        let one = |kind: &str, text: &str| serde_json::json!({"persona": "don", "kind": kind, "text": text});
        let v = serde_json::json!({"messages": [
            one("reply", "1"), one("emo", "m1a2b3c"), one("reply", "2"), one("reply", "3"), one("reply", "4"),
            {"persona": "kara", "kind": "emo", "text": "m0000ff"},
        ]});
        let got: Vec<String> = said_events(&v, "don", false).into_iter().map(|s| format!("{}:{}", s.kind, s.text)).collect();
        assert_eq!(got, ["emo:m1a2b3c", "reply:2", "reply:3", "reply:4"]);
        let got: Vec<String> = said_events(&v, "don", true).into_iter().map(|s| format!("{}:{}", s.kind, s.text)).collect();
        assert_eq!(got, ["emo:m1a2b3c"]);
    }

    #[test]
    fn said_look() {
        // look — в вебе сменили тему: без персоны, будит любой портрет
        let v = serde_json::json!({"messages": [
            {"persona": "don", "kind": "reply", "text": "1"},
            {"persona": "", "kind": "look", "text": ""},
        ]});
        assert!(has_look(&v));
        // В облачка не идёт: у события нет ни персоны, ни текста
        assert!(said_events(&v, "don", false).iter().all(|s| s.kind != "look"));
        assert!(!has_look(&serde_json::json!({"messages": [{"persona": "don", "kind": "act", "text": "a"}]})));
        assert!(!has_look(&serde_json::json!({"messages": []})));
    }

    #[test]
    fn said_portrait_change() {
        // portrait — сохранили портрет или анимации: будит портрет только этой персоны
        let v = serde_json::json!({"messages": [
            {"persona": "don", "kind": "reply", "text": "1"},
            {"persona": "don", "kind": "portrait", "text": ""},
        ]});
        assert!(has_portrait_change(&v, "don"));
        assert!(!has_portrait_change(&v, "kara"));
        assert!(!has_portrait_change(&v, ""));
        assert!(!has_look(&v));
        // В облачка не идёт: текста нет
        assert!(said_events(&v, "don", false).iter().all(|s| s.kind != "portrait"));
        assert!(!has_portrait_change(&serde_json::json!({"messages": [{"persona": "don", "kind": "act", "text": "a"}]}), "don"));
    }

    #[test]
    fn said_mode_change() {
        // mode — в чате сменился режим персоны: перечитать её аватар; облачком не идёт
        let v = serde_json::json!({"messages": [{"persona": "don", "kind": "mode", "text": ""}]});
        assert!(has_portrait_change(&v, "don"));
        assert!(!has_portrait_change(&v, "kara"));
        assert!(said_events(&v, "don", false).is_empty());
        assert!(said_events(&v, "don", true).is_empty());
    }

    #[test]
    fn look_from_disk() {
        // Бот не запущен: тема оформления — из data/ui_look.json
        let root = std::env::temp_dir().join(format!("vpc-portrait-look-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("data")).unwrap();
        let api = ApiEnv::read(&root);
        assert!(disk_look(&api).is_null());
        fs::write(root.join("data").join("ui_look.json"), r#"{"skin":"clean-neutral","personas":{"kara":"default"},"theme":"dark"}"#).unwrap();
        let v = disk_look(&api);
        assert_eq!((v["skin"].as_str(), v["personas"]["kara"].as_str(), v["theme"].as_str()), (Some("clean-neutral"), Some("default"), Some("dark")));
        // Битый файл — как без файла
        fs::write(root.join("data").join("ui_look.json"), "{битый").unwrap();
        assert!(disk_look(&api).is_null());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn anim_room() {
        use serde_json::json;
        // «Падает» у Дон Кихота: кадр 432×275, размер ×1.75
        let v = json!({ "animsOn": true, "anims": { "anims": [
            { "enabled": true, "atlas": "data:x", "frames": 8, "fw": 432, "fh": 275, "scale": 1.75 },
            { "enabled": true, "atlas": "data:x", "frames": 8, "fw": 256, "fh": 308, "scale": 1.0 },
            { "enabled": false, "atlas": "data:x", "frames": 8, "fw": 100, "fh": 900, "scale": 3.0 },
            { "atlas": "", "frames": 8, "fw": 100, "fh": 900, "scale": 3.0 }
        ] } });
        let r = room_of(&v);
        assert!((r.kl - 0.875).abs() < 1e-9 && (r.kr - 0.875).abs() < 1e-9 && (r.ky - 1.75 * 275.0 / 432.0).abs() < 1e-9, "{r:?}");
        let mut off = v.clone();
        off["animsOn"] = json!(false);
        assert_eq!(room_of(&off), Room::default());
        assert_eq!(room_of(&json!({})), Room::default());
        // Без облачков и поля: окно = голова, кадру не хватает по 90 px с боков и 28 сверху
        assert_eq!(pads(240.0, false, None, r), (90.0, 28.0, 90.0, 0.0));
        let (w, h, dx, dy) = frame(240.0, false, None, r);
        assert_eq!((w, h, dx, dy), (420.0, 268.0, 90.0, 28.0));
        // С облачком: сверху места хватает, по бокам окно и так 280
        assert_eq!(pads(240.0, true, None, r), (70.0, 0.0, 70.0, 0.0));
        // Поле слева: слева место уже есть, справа — нет
        assert_eq!(pads(240.0, true, Some(Side::Left), r), (0.0, 0.0, 90.0, 0.0));
        // Анимации не крупнее головы — запаса нет, окно как было
        assert_eq!(pads(240.0, true, Some(Side::Right), Room::default()), (0.0, 0.0, 0.0, 0.0));
        // Фигура под полом: кадр 432×300, из них 60 под полом — окно ниже головы на 60 × 1.75 / 432 × 240
        let v = json!({ "anims": { "anims": [
            { "atlas": "data:x", "frames": 8, "fw": 432, "fh": 300, "sink": 60, "scale": 1.75 }
        ] } });
        let r = room_of(&v);
        // Над полом 240 из 432 × 1.75 — ниже головы: сверху запаса не надо
        assert!(r.ky == 1.0 && (r.kb - 1.75 * 60.0 / 432.0).abs() < 1e-9, "{r:?}");
        let (_, _, _, b) = pads(240.0, true, None, r);
        assert_eq!(b, (240.0 * 1.75 * 60.0 / 432.0_f64).ceil());
        // Низ окна — ниже низа головы на этот запас, голова на месте
        let (_, h, _, dy) = frame(240.0, true, None, r);
        assert_eq!((h - dy - 240.0, dy), (b, BUBBLE_H));
        // Своя рамка, несимметричная: середина пола в 100 px от левого края кадра 400
        let v = json!({ "anims": { "anims": [
            { "atlas": "data:x", "frames": 8, "fw": 400, "fh": 200, "ax": 100, "scale": 2.0 }
        ] } });
        let r = room_of(&v);
        assert!((r.kl - 0.5).abs() < 1e-9 && (r.kr - 1.5).abs() < 1e-9, "{r:?}");
        // Голова 240 без облачков и поля: слева кадр в голову влез, справа — на 240 дальше
        assert_eq!(pads(240.0, false, None, r), (0.0, 0.0, 240.0, 0.0));
    }

    #[test]
    fn window_frame() {
        // Без облачков и поля окно — сама голова
        assert_eq!(frame(240.0, false, None, Room::default()), (240.0, 240.0, 0.0, 0.0));
        // С облачками: место над головой, голова по середине
        let (w, h, dx, dy) = frame(240.0, true, None, Room::default());
        assert_eq!((w, h, dy), (BUBBLE_W, 240.0 + BUBBLE_H, BUBBLE_H));
        assert_eq!(dx * 2.0 + 240.0, w);
        // Голова шире облачка — окно по голове
        assert_eq!(frame(400.0, true, None, Room::default()).0, 400.0);
        // Поле сбоку: окно шире на поле, голова у другого края, высота — без поля
        assert_eq!(frame(240.0, true, Some(Side::Left), Room::default()), (240.0 + FIELD_W, BUBBLE_H + 240.0, FIELD_W, BUBBLE_H));
        assert_eq!(frame(240.0, true, Some(Side::Right), Room::default()), (240.0 + FIELD_W, BUBBLE_H + 240.0, 0.0, BUBBLE_H));
        assert_eq!(frame(140.0, false, Some(Side::Right), Room::default()), (140.0 + FIELD_W, 140.0, 0.0, 0.0));
    }

    #[test]
    fn head_may_leave_screen() {
        // Экран 1512 × 982 без строки меню (33). Голову можно задвинуть за
        // край, но на экране остаётся keep(240) = 72 px по каждой оси
        let area = (0.0, 33.0, 1512.0, 949.0);
        let k = keep(240.0);
        assert_eq!(k, 72.0);
        // На экране — на месте; наполовину за низом — тоже
        assert_eq!(fit(area, 600.0, 400.0, 240.0), (600.0, 400.0));
        assert_eq!(fit(area, 1236.0, 862.0, 240.0), (1236.0, 862.0));
        // Дальше — вернуть до keep: снизу, справа, слева, сверху
        assert_eq!(fit(area, 1236.0, 975.0, 240.0), (1236.0, 982.0 - k));
        assert_eq!(fit(area, 1500.0, 400.0, 240.0), (1512.0 - k, 400.0));
        assert_eq!(fit(area, -230.0, 400.0, 240.0), (k - 240.0, 400.0));
        assert_eq!(fit(area, 600.0, -300.0, 240.0), (600.0, 33.0 + k - 240.0));
        // Маленькая голова — не меньше KEEP_MIN
        assert_eq!(keep(120.0), KEEP_MIN);
    }

    #[test]
    fn visible_part() {
        let area = (0.0, 33.0, 1512.0, 949.0);
        // Окно целиком на экране — видно всё
        assert_eq!(visible_in(area, 100.0, 100.0, 460.0, 352.0), [0.0, 0.0, 460.0, 352.0]);
        // Голова на 100 px за низом: видно окно до низа экрана
        assert_eq!(visible_in(area, 1016.0, 730.0, 460.0, 352.0), [0.0, 0.0, 460.0, 982.0 - 730.0]);
        // За правым краем и под строкой меню
        assert_eq!(visible_in(area, 1200.0, 0.0, 460.0, 352.0), [0.0, 33.0, 312.0, 352.0]);
        // За левым краем
        assert_eq!(visible_in(area, -150.0, 300.0, 460.0, 352.0), [150.0, 0.0, 460.0, 352.0]);
        // Совсем за краем — пустая полоса, без отрицательных размеров
        let [l, t, r, b] = visible_in(area, 2000.0, 300.0, 460.0, 352.0);
        assert!(l <= r && t <= b);
    }

    #[test]
    fn field_side() {
        // Поле — со стороны середины экрана
        let area = (0.0, 33.0, 1512.0, 949.0);
        assert_eq!(side_in(area, 1236.0, 240.0), Side::Left);
        assert_eq!(side_in(area, 40.0, 240.0), Side::Right);
        // Второй монитор правее основного
        assert_eq!(side_in((1512.0, 0.0, 1920.0, 1080.0), 1600.0, 240.0), Side::Right);
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
        // Персоны без портрета тоже в списке; облик фигуры — из раскладки комнаты
        fs::create_dir_all(root.join("app").join("personas")).unwrap();
        fs::write(root.join("app").join("personas").join("connor.yaml"), "name: Коннор\n").unwrap();
        fs::write(data.join("personas").join("notes.txt"), "").unwrap();
        assert_eq!(disk_persona_ids(&api), vec!["connor".to_string(), "don".to_string()]);
        let room = data.join("api_kara").join("room");
        fs::create_dir_all(&room).unwrap();
        fs::write(room.join("layout.json"), r#"{"items":{},"avatar":{"head":"circle","eyes":3,"accessory":2,"shade":0}}"#).unwrap();
        assert_eq!(avatar_preset(&api, "kara", false)["head"], "circle");
        assert!(avatar_preset(&api, "don", false).is_null());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn said_pause_events() {
        // Пауза до ответа: heard с текстом вашего сообщения и без него
        // (картинка), settled без текста проходят; чужая персона и пустая
        // реплика — нет; из многих реплик — последние SAID_KEEP, события
        // паузы при этом не теряются и идут по порядку
        let one = |persona: &str, kind: &str, text: &str| serde_json::json!({"persona": persona, "kind": kind, "text": text});
        let v = serde_json::json!({"messages": [
            one("don", "heard", "Почему небо голубое?"),
            one("kara", "heard", "Привет"),
            one("don", "reply", "  "),
            one("don", "reply", "Ответ"),
            one("don", "settled", ""),
            one("don", "heard", ""),
        ]});
        let got: Vec<(String, String)> = said_events(&v, "don", false).into_iter().map(|s| (s.kind, s.text)).collect();
        let want = [("heard", "Почему небо голубое?"), ("reply", "Ответ"), ("settled", ""), ("heard", "")];
        assert_eq!(got, want.iter().map(|(k, t)| (k.to_string(), t.to_string())).collect::<Vec<_>>());
        let many = serde_json::json!({"messages": [
            one("don", "reply", "1"), one("don", "heard", "а"), one("don", "reply", "2"),
            one("don", "reply", "3"), one("don", "heard", "б"), one("don", "reply", "4"), one("don", "settled", ""),
        ]});
        let got: Vec<String> = said_events(&many, "don", false).into_iter().map(|s| format!("{}:{}", s.kind, s.text)).collect();
        assert_eq!(got, ["heard:а", "reply:2", "reply:3", "heard:б", "reply:4", "settled:"]);
    }

    #[test]
    fn chat_replies_without_task_lines() {
        let v = serde_json::json!({
            "reply": "Нажал «Меню» на pizza.test.\nЛистаю страницу.\nКакую пиццу: Пепперони или Маргариту?",
            "extra_messages": ["  ", "1. Пепперони"],
            "task": {"log": [{"text": "Нажал «Меню» на pizza.test.", "ok": true}, {"text": "Листаю страницу.", "ok": true}]}
        });
        assert_eq!(chat_replies(&v), vec!["Какую пиццу: Пепперони или Маргариту?".to_string(), "1. Пепперони".to_string()]);
        // Ответ из одних строк хода — облачка нет
        let only = serde_json::json!({"reply": "Листаю страницу.", "task": {"log": [{"text": "Листаю страницу."}]}});
        assert!(chat_replies(&only).is_empty());
        // Без карточки — как есть (переносы строк сохраняются)
        let plain = serde_json::json!({"reply": "Привет.\nКак дела?", "task": null});
        assert_eq!(chat_replies(&plain), vec!["Привет.\nКак дела?".to_string()]);
    }

    #[test]
    fn said_events_keep_answer_options() {
        let v = serde_json::json!({"messages": [
            {"persona": "don", "kind": "reply", "text": "Делаю? (да/нет)",
             "options": {"kind": "yesno", "options": [{"role": "yes"}, {"role": "no"}]}},
            {"persona": "don", "kind": "reply", "text": "Привет", "options": {"kind": "yesno", "options": []}},
            {"persona": "don", "kind": "reply", "text": "Пока"},
        ]});
        let got = said_events(&v, "don", false);
        assert_eq!(got[0].options.as_ref().map(|o| o["kind"].clone()), Some(serde_json::json!("yesno")));
        assert!(got[1].options.is_none(), "пустой список — без кнопок");
        assert!(got[2].options.is_none());
        // Странице без кнопок поля нет вовсе
        assert!(!serde_json::to_string(&got[2]).unwrap().contains("options"));
    }
}
