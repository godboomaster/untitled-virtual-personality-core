//! Клавиши и кнопки мыши по всей системе → анимация портрета на рабочем
//! столе (anim.keys, app/api/portrait_anim.py; запись сочетаний — вкладка
//! «Анимации» в вебе, web/src/portrait/animKeys.ts).
//!
//! Окна портретов говорят, какие сочетания у их персоны (portrait_keys);
//! пока хоть у одного окна они есть, панель слушает ввод: на macOS —
//! прослушивающий перехват событий (CGEventTap, только чтение: события идут
//! дальше как шли, нужен доступ «Мониторинг ввода»), на Windows —
//! низкоуровневые хуки клавиатуры и мыши. Привязок нет — слушать нечего,
//! перехват снимается.
//!
//! Нажатие сразу сравнивается со списками окон и забывается: ни лога, ни
//! истории, ни счётчиков. Совпало — окну с этим сочетанием уходит
//! portrait-key (само сочетание), остальное не покидает этот модуль.
//!
//! Сочетание — как в вебе: модификаторы в порядке Ctrl, Alt, Shift, Meta и
//! одна клавиша по KeyboardEvent.code (физическая: KeyA, Digit1, F5,
//! ArrowLeft…) или кнопка мыши (MouseLeft, MouseRight, MouseMiddle,
//! MouseBack, MouseForward). Модификатор сам по себе срабатывает, когда его
//! отпустили, ничего больше не нажав (Shift + буква не будит привязку
//! «Shift»); в приставке — остальные зажатые модификаторы.

use std::collections::{HashMap, HashSet};
use std::sync::mpsc::{channel, Sender};
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager};

/// Сочетаний на окно — с запасом: у анимации до 8, анимаций до 40
const KEYS_MAX: usize = 400;
/// Без доступа к клавиатуре (macOS) — проверять, не дали ли, так часто
const ACCESS_POLL: Duration = Duration::from_secs(3);

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Mods {
    pub ctrl: bool,
    pub alt: bool,
    pub shift: bool,
    pub meta: bool,
}

/// Событие ввода от системы — уже в именах KeyboardEvent.code
#[derive(Debug, PartialEq, Eq)]
pub enum Input {
    Down { code: &'static str, mods: Mods, repeat: bool },
    Up { code: &'static str },
    Mouse { button: &'static str, mods: Mods },
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Family {
    Ctrl,
    Alt,
    Shift,
    Meta,
}

fn family(code: &str) -> Option<Family> {
    match code {
        "ControlLeft" | "ControlRight" => Some(Family::Ctrl),
        "AltLeft" | "AltRight" => Some(Family::Alt),
        "ShiftLeft" | "ShiftRight" => Some(Family::Shift),
        "MetaLeft" | "MetaRight" => Some(Family::Meta),
        _ => None,
    }
}

/// «Ctrl+Shift+KeyK»: зажатые модификаторы, кроме семейства самой клавиши
pub fn combo(mods: Mods, code: &str) -> String {
    let own = family(code);
    let mut out = String::new();
    for (on, fam, name) in [
        (mods.ctrl, Family::Ctrl, "Ctrl+"),
        (mods.alt, Family::Alt, "Alt+"),
        (mods.shift, Family::Shift, "Shift+"),
        (mods.meta, Family::Meta, "Meta+"),
    ] {
        if on && own != Some(fam) {
            out.push_str(name);
        }
    }
    out.push_str(code);
    out
}

/// Похоже ли на сочетание из веба (portrait_anim._KEY_RE): чужое отбросить
pub fn valid(combo: &str) -> bool {
    let mut rest = combo;
    for m in ["Ctrl+", "Alt+", "Shift+", "Meta+"] {
        rest = rest.strip_prefix(m).unwrap_or(rest);
    }
    let mut ch = rest.chars();
    matches!(ch.next(), Some(c) if c.is_ascii_uppercase()) && rest.len() <= 24 && ch.all(|c| c.is_ascii_alphanumeric())
}

/// Что сработало из потока событий: повторы зажатой клавиши — нет;
/// модификатор — только отпущенный без других нажатий
#[derive(Default)]
pub struct Tracker {
    pending: Option<(&'static str, String)>,
}

impl Tracker {
    pub fn feed(&mut self, input: Input) -> Option<String> {
        match input {
            Input::Down { repeat: true, .. } => None,
            Input::Down { code, mods, .. } => {
                if family(code).is_some() {
                    self.pending = Some((code, combo(mods, code)));
                    None
                } else {
                    self.pending = None;
                    Some(combo(mods, code))
                }
            }
            Input::Up { code } => match self.pending.take() {
                Some((c, combo)) if c == code => Some(combo),
                other => {
                    self.pending = other;
                    None
                }
            },
            Input::Mouse { button, mods } => {
                self.pending = None;
                Some(combo(mods, button))
            }
        }
    }
}

/// Слушает ли панель ввод: off — привязок нет; on — клавиатура и мышь;
/// mouse — только мышь (macOS: нет доступа «Мониторинг ввода»);
/// unsupported — на этой системе не умеем или перехват не поставился
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Status {
    Off,
    On,
    Mouse,
    Unsupported,
}

impl Status {
    pub fn as_str(self) -> &'static str {
        match self {
            Status::Off => "off",
            Status::On => "on",
            Status::Mouse => "mouse",
            Status::Unsupported => "unsupported",
        }
    }
}

type Stopper = Box<dyn FnOnce() + Send>;

struct Watch {
    app: Option<AppHandle>,
    /// Метка окна портрета → его сочетания
    binds: HashMap<String, HashSet<String>>,
    /// Перехват идёт: как его снять и слушает ли он клавиатуру
    running: Option<(Stopper, bool)>,
    status: Status,
    /// Ждём доступ к клавиатуре (поток проверки идёт)
    polling: bool,
}

fn watch() -> MutexGuard<'static, Watch> {
    static W: OnceLock<Mutex<Watch>> = OnceLock::new();
    W.get_or_init(|| Mutex::new(Watch { app: None, binds: HashMap::new(), running: None, status: Status::Off, polling: false }))
        .lock()
        .unwrap_or_else(|e| e.into_inner())
}

/// События от перехвата — в разборщик (свой поток): обратные вызовы
/// системы должны отдавать управление сразу
fn sink() -> &'static Sender<Input> {
    static S: OnceLock<Sender<Input>> = OnceLock::new();
    S.get_or_init(|| {
        let (tx, rx) = channel::<Input>();
        thread::Builder::new()
            .name("input-watch".into())
            .spawn(move || {
                let mut tracker = Tracker::default();
                for input in rx {
                    if let Some(c) = tracker.feed(input) {
                        fire(&c);
                    }
                }
            })
            .ok();
        tx
    })
}

pub(crate) fn push(input: Input) {
    let _ = sink().send(input);
}

/// Сработало сочетание — окнам, у которых оно есть
fn fire(combo: &str) {
    let mut w = watch();
    let Some(app) = w.app.clone() else { return };
    let mut gone = Vec::new();
    for (label, keys) in &w.binds {
        if !keys.contains(combo) {
            continue;
        }
        if app.get_webview_window(label).is_none() {
            gone.push(label.clone());
            continue;
        }
        let _ = app.emit_to(label.as_str(), "portrait-key", combo);
    }
    if !gone.is_empty() {
        for l in gone {
            w.binds.remove(&l);
        }
        sync(&mut w);
    }
}

/// Сочетания окна портрета (пусто — ему ничего не нужно)
pub fn set(app: &AppHandle, label: &str, keys: Vec<String>) -> Status {
    let keys: HashSet<String> = keys.into_iter().filter(|k| valid(k)).take(KEYS_MAX).collect();
    let mut w = watch();
    w.app.get_or_insert_with(|| app.clone());
    if keys.is_empty() {
        w.binds.remove(label);
    } else {
        w.binds.insert(label.to_string(), keys);
    }
    sync(&mut w);
    w.status
}

/// Окно портрета закрыли — его сочетания больше не нужны
pub fn forget(label: &str) {
    let mut w = watch();
    if w.binds.remove(label).is_some() {
        sync(&mut w);
    }
}

pub fn status() -> Status {
    watch().status
}

/// Перехват — по привязкам: есть — поставить (или переставить, если дали
/// доступ к клавиатуре), нет — снять
fn sync(w: &mut Watch) {
    let need = w.binds.values().any(|k| !k.is_empty());
    if !need {
        if let Some((stop, _)) = w.running.take() {
            stop();
        }
        w.status = Status::Off;
        return;
    }
    let keyboard = sys::keyboard_allowed();
    if let Some((_, had)) = &w.running {
        if *had == keyboard {
            return;
        }
        if let Some((stop, _)) = w.running.take() {
            stop();
        }
    }
    if !keyboard {
        sys::ask_keyboard();
    }
    match sys::start(keyboard) {
        Some(stop) => {
            w.running = Some((stop, keyboard));
            w.status = if keyboard { Status::On } else { Status::Mouse };
        }
        None => w.status = Status::Unsupported,
    }
    if w.status == Status::Mouse && !w.polling {
        w.polling = true;
        thread::spawn(|| loop {
            thread::sleep(ACCESS_POLL);
            let mut w = watch();
            if w.status != Status::Mouse {
                w.polling = false;
                return;
            }
            if sys::keyboard_allowed() {
                sync(&mut w);
            }
        });
    }
}

/// Открыть настройку доступа к вводу (macOS: «Мониторинг ввода»)
pub fn open_settings() {
    sys::open_settings();
}

// ── Клавиши систем → KeyboardEvent.code ──

/// macOS: виртуальный код клавиши (kVK_*, Carbon/HIToolbox Events.h)
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub fn mac_key(code: u16) -> Option<&'static str> {
    Some(match code {
        0x00 => "KeyA",
        0x01 => "KeyS",
        0x02 => "KeyD",
        0x03 => "KeyF",
        0x04 => "KeyH",
        0x05 => "KeyG",
        0x06 => "KeyZ",
        0x07 => "KeyX",
        0x08 => "KeyC",
        0x09 => "KeyV",
        0x0A => "IntlBackslash",
        0x0B => "KeyB",
        0x0C => "KeyQ",
        0x0D => "KeyW",
        0x0E => "KeyE",
        0x0F => "KeyR",
        0x10 => "KeyY",
        0x11 => "KeyT",
        0x12 => "Digit1",
        0x13 => "Digit2",
        0x14 => "Digit3",
        0x15 => "Digit4",
        0x16 => "Digit6",
        0x17 => "Digit5",
        0x18 => "Equal",
        0x19 => "Digit9",
        0x1A => "Digit7",
        0x1B => "Minus",
        0x1C => "Digit8",
        0x1D => "Digit0",
        0x1E => "BracketRight",
        0x1F => "KeyO",
        0x20 => "KeyU",
        0x21 => "BracketLeft",
        0x22 => "KeyI",
        0x23 => "KeyP",
        0x24 => "Enter",
        0x25 => "KeyL",
        0x26 => "KeyJ",
        0x27 => "Quote",
        0x28 => "KeyK",
        0x29 => "Semicolon",
        0x2A => "Backslash",
        0x2B => "Comma",
        0x2C => "Slash",
        0x2D => "KeyN",
        0x2E => "KeyM",
        0x2F => "Period",
        0x30 => "Tab",
        0x31 => "Space",
        0x32 => "Backquote",
        0x33 => "Backspace",
        0x35 => "Escape",
        0x36 => "MetaRight",
        0x37 => "MetaLeft",
        0x38 => "ShiftLeft",
        0x39 => "CapsLock",
        0x3A => "AltLeft",
        0x3B => "ControlLeft",
        0x3C => "ShiftRight",
        0x3D => "AltRight",
        0x3E => "ControlRight",
        0x40 => "F17",
        0x41 => "NumpadDecimal",
        0x43 => "NumpadMultiply",
        0x45 => "NumpadAdd",
        0x47 => "NumLock",
        0x48 => "AudioVolumeUp",
        0x49 => "AudioVolumeDown",
        0x4A => "AudioVolumeMute",
        0x4B => "NumpadDivide",
        0x4C => "NumpadEnter",
        0x4E => "NumpadSubtract",
        0x4F => "F18",
        0x50 => "F19",
        0x51 => "NumpadEqual",
        0x52 => "Numpad0",
        0x53 => "Numpad1",
        0x54 => "Numpad2",
        0x55 => "Numpad3",
        0x56 => "Numpad4",
        0x57 => "Numpad5",
        0x58 => "Numpad6",
        0x59 => "Numpad7",
        0x5A => "F20",
        0x5B => "Numpad8",
        0x5C => "Numpad9",
        0x60 => "F5",
        0x61 => "F6",
        0x62 => "F7",
        0x63 => "F3",
        0x64 => "F8",
        0x65 => "F9",
        0x67 => "F11",
        0x69 => "F13",
        0x6A => "F16",
        0x6B => "F14",
        0x6D => "F10",
        0x6E => "ContextMenu",
        0x6F => "F12",
        0x71 => "F15",
        0x72 => "Insert",
        0x73 => "Home",
        0x74 => "PageUp",
        0x75 => "Delete",
        0x76 => "F4",
        0x77 => "End",
        0x78 => "F2",
        0x79 => "PageDown",
        0x7A => "F1",
        0x7B => "ArrowLeft",
        0x7C => "ArrowRight",
        0x7D => "ArrowDown",
        0x7E => "ArrowUp",
        _ => return None,
    })
}

/// Windows: скан-код (набор 1) и флаг «расширенная» — физическая клавиша,
/// как KeyboardEvent.code; клавиши без своего скан-кода (громкость,
/// мультимедиа, Pause) — по виртуальному коду
#[cfg_attr(not(windows), allow(dead_code))]
pub fn win_key(scan: u32, extended: bool, vk: u32) -> Option<&'static str> {
    let by_vk = match vk {
        0x13 => Some("Pause"),
        0x90 => Some("NumLock"),
        0x2C => Some("PrintScreen"),
        0xAD => Some("AudioVolumeMute"),
        0xAE => Some("AudioVolumeDown"),
        0xAF => Some("AudioVolumeUp"),
        0xB0 => Some("MediaTrackNext"),
        0xB1 => Some("MediaTrackPrevious"),
        0xB2 => Some("MediaStop"),
        0xB3 => Some("MediaPlayPause"),
        _ => None,
    };
    if by_vk.is_some() {
        return by_vk;
    }
    if extended {
        return Some(match scan {
            0x1C => "NumpadEnter",
            0x1D => "ControlRight",
            0x35 => "NumpadDivide",
            0x38 => "AltRight",
            0x47 => "Home",
            0x48 => "ArrowUp",
            0x49 => "PageUp",
            0x4B => "ArrowLeft",
            0x4D => "ArrowRight",
            0x4F => "End",
            0x50 => "ArrowDown",
            0x51 => "PageDown",
            0x52 => "Insert",
            0x53 => "Delete",
            0x5B => "MetaLeft",
            0x5C => "MetaRight",
            0x5D => "ContextMenu",
            _ => return None,
        });
    }
    const ROW: [&str; 0x59] = [
        "", "Escape", "Digit1", "Digit2", "Digit3", "Digit4", "Digit5", "Digit6", "Digit7", "Digit8", "Digit9", "Digit0", "Minus", "Equal",
        "Backspace", "Tab", "KeyQ", "KeyW", "KeyE", "KeyR", "KeyT", "KeyY", "KeyU", "KeyI", "KeyO", "KeyP", "BracketLeft", "BracketRight",
        "Enter", "ControlLeft", "KeyA", "KeyS", "KeyD", "KeyF", "KeyG", "KeyH", "KeyJ", "KeyK", "KeyL", "Semicolon", "Quote", "Backquote",
        "ShiftLeft", "Backslash", "KeyZ", "KeyX", "KeyC", "KeyV", "KeyB", "KeyN", "KeyM", "Comma", "Period", "Slash", "ShiftRight",
        "NumpadMultiply", "AltLeft", "Space", "CapsLock", "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "NumLock",
        "ScrollLock", "Numpad7", "Numpad8", "Numpad9", "NumpadSubtract", "Numpad4", "Numpad5", "Numpad6", "NumpadAdd", "Numpad1",
        "Numpad2", "Numpad3", "Numpad0", "NumpadDecimal", "", "", "IntlBackslash", "F11", "F12",
    ];
    if let Some(&code) = ROW.get(scan as usize) {
        return (!code.is_empty()).then_some(code);
    }
    Some(match scan {
        0x64 => "F13",
        0x65 => "F14",
        0x66 => "F15",
        0x67 => "F16",
        0x68 => "F17",
        0x69 => "F18",
        0x6A => "F19",
        0x6B => "F20",
        0x6C => "F21",
        0x6D => "F22",
        0x6E => "F23",
        0x76 => "F24",
        _ => return None,
    })
}

// ── macOS: CGEventTap только на чтение ──

#[cfg(target_os = "macos")]
mod sys {
    use super::{mac_key, push, Input, Mods, Stopper};
    use std::ffi::c_void;
    use std::ptr;
    use std::sync::atomic::{AtomicBool, AtomicPtr, Ordering};
    use std::sync::mpsc::channel;
    use std::sync::Arc;
    use std::time::Duration;

    type Ref = *mut c_void;
    type TapCallback = extern "C" fn(proxy: Ref, kind: u32, event: Ref, info: *mut c_void) -> Ref;

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventTapCreate(tap: u32, place: u32, options: u32, mask: u64, callback: TapCallback, info: *mut c_void) -> Ref;
        fn CGEventTapEnable(tap: Ref, enable: bool);
        fn CGEventGetIntegerValueField(event: Ref, field: u32) -> i64;
        fn CGEventGetFlags(event: Ref) -> u64;
        fn CGPreflightListenEventAccess() -> bool;
        fn CGRequestListenEventAccess() -> bool;
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFMachPortCreateRunLoopSource(alloc: *const c_void, port: Ref, order: isize) -> Ref;
        fn CFMachPortInvalidate(port: Ref);
        fn CFRunLoopGetCurrent() -> Ref;
        fn CFRunLoopAddSource(rl: Ref, source: Ref, mode: *const c_void);
        fn CFRunLoopRunInMode(mode: *const c_void, seconds: f64, return_after_source: bool) -> i32;
        fn CFRelease(cf: *const c_void);
        static kCFRunLoopCommonModes: *const c_void;
        static kCFRunLoopDefaultMode: *const c_void;
    }

    // kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionListenOnly
    const SESSION_TAP: u32 = 1;
    const HEAD_INSERT: u32 = 0;
    const LISTEN_ONLY: u32 = 1;
    // Типы событий (CGEventType)
    const LEFT_DOWN: u32 = 1;
    const RIGHT_DOWN: u32 = 3;
    const KEY_DOWN: u32 = 10;
    const KEY_UP: u32 = 11;
    const FLAGS_CHANGED: u32 = 12;
    const OTHER_DOWN: u32 = 25;
    const TAP_OFF_TIMEOUT: u32 = 0xFFFF_FFFE;
    const TAP_OFF_USER: u32 = 0xFFFF_FFFF;
    // Поля (CGEventField)
    const MOUSE_BUTTON: u32 = 3;
    const KEY_AUTOREPEAT: u32 = 8;
    const KEY_CODE: u32 = 9;
    // Флаги (CGEventFlags) и левые/правые модификаторы (NX_DEVICE*KEYMASK)
    const SHIFT: u64 = 0x0002_0000;
    const CONTROL: u64 = 0x0004_0000;
    const ALT: u64 = 0x0008_0000;
    const COMMAND: u64 = 0x0010_0000;

    /// Перехват этого потока: снова включить, если система его выключила
    static TAP: AtomicPtr<c_void> = AtomicPtr::new(ptr::null_mut());

    fn mods(flags: u64) -> Mods {
        Mods { ctrl: flags & CONTROL != 0, alt: flags & ALT != 0, shift: flags & SHIFT != 0, meta: flags & COMMAND != 0 }
    }

    /// Модификатор нажат или отпущен: его бит «левый/правый» во флагах
    fn modifier_bit(code: u16) -> Option<u64> {
        Some(match code {
            0x3B => 0x0001,
            0x38 => 0x0002,
            0x3C => 0x0004,
            0x37 => 0x0008,
            0x36 => 0x0010,
            0x3A => 0x0020,
            0x3D => 0x0040,
            0x3E => 0x2000,
            _ => return None,
        })
    }

    extern "C" fn on_event(_proxy: Ref, kind: u32, event: Ref, _info: *mut c_void) -> Ref {
        if kind == TAP_OFF_TIMEOUT || kind == TAP_OFF_USER {
            let tap = TAP.load(Ordering::SeqCst);
            if !tap.is_null() {
                unsafe { CGEventTapEnable(tap, true) };
            }
            return event;
        }
        let field = |f: u32| unsafe { CGEventGetIntegerValueField(event, f) };
        let flags = unsafe { CGEventGetFlags(event) };
        match kind {
            KEY_DOWN | KEY_UP => {
                if let Some(code) = mac_key(field(KEY_CODE) as u16) {
                    push(if kind == KEY_DOWN {
                        Input::Down { code, mods: mods(flags), repeat: field(KEY_AUTOREPEAT) != 0 }
                    } else {
                        Input::Up { code }
                    });
                }
            }
            FLAGS_CHANGED => {
                let vk = field(KEY_CODE) as u16;
                if let Some(code) = mac_key(vk) {
                    match modifier_bit(vk) {
                        Some(bit) if flags & bit != 0 => push(Input::Down { code, mods: mods(flags), repeat: false }),
                        Some(_) => push(Input::Up { code }),
                        // Caps Lock: событие на каждое нажатие
                        None => push(Input::Down { code, mods: mods(flags), repeat: false }),
                    }
                }
            }
            LEFT_DOWN | RIGHT_DOWN | OTHER_DOWN => {
                let button = match (kind, field(MOUSE_BUTTON)) {
                    (LEFT_DOWN, _) => Some("MouseLeft"),
                    (RIGHT_DOWN, _) => Some("MouseRight"),
                    (_, 2) => Some("MouseMiddle"),
                    (_, 3) => Some("MouseBack"),
                    (_, 4) => Some("MouseForward"),
                    _ => None,
                };
                if let Some(button) = button {
                    push(Input::Mouse { button, mods: mods(flags) });
                }
            }
            _ => {}
        }
        event
    }

    pub fn keyboard_allowed() -> bool {
        unsafe { CGPreflightListenEventAccess() }
    }

    /// Спросить доступ «Мониторинг ввода» — один раз за запуск: система
    /// покажет окно и внесёт панель в список настроек
    pub fn ask_keyboard() {
        static ASKED: AtomicBool = AtomicBool::new(false);
        if !ASKED.swap(true, Ordering::SeqCst) {
            unsafe { CGRequestListenEventAccess() };
        }
    }

    pub fn open_settings() {
        let _ = std::process::Command::new("open")
            .arg("x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent")
            .spawn();
    }

    pub fn start(keyboard: bool) -> Option<Stopper> {
        let mouse_mask = (1u64 << LEFT_DOWN) | (1 << RIGHT_DOWN) | (1 << OTHER_DOWN);
        let mask = if keyboard { mouse_mask | (1 << KEY_DOWN) | (1 << KEY_UP) | (1 << FLAGS_CHANGED) } else { mouse_mask };
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let (ready_tx, ready_rx) = channel::<bool>();
        std::thread::Builder::new()
            .name("input-tap".into())
            .spawn(move || unsafe {
                let tap = CGEventTapCreate(SESSION_TAP, HEAD_INSERT, LISTEN_ONLY, mask, on_event, ptr::null_mut());
                if tap.is_null() {
                    let _ = ready_tx.send(false);
                    return;
                }
                let source = CFMachPortCreateRunLoopSource(ptr::null(), tap, 0);
                CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes);
                CGEventTapEnable(tap, true);
                TAP.store(tap, Ordering::SeqCst);
                let _ = ready_tx.send(true);
                // Кусками по полсекунды: сняли — выйти без чужих потоков
                while !flag.load(Ordering::SeqCst) {
                    CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.5, false);
                }
                let _ = TAP.compare_exchange(tap, ptr::null_mut(), Ordering::SeqCst, Ordering::SeqCst);
                CFMachPortInvalidate(tap);
                CFRelease(source);
                CFRelease(tap);
            })
            .ok()?;
        if !ready_rx.recv_timeout(Duration::from_secs(3)).unwrap_or(false) {
            stop.store(true, Ordering::SeqCst);
            return None;
        }
        Some(Box::new(move || stop.store(true, Ordering::SeqCst)))
    }
}

// ── Windows: низкоуровневые хуки (WH_KEYBOARD_LL, WH_MOUSE_LL) ──

#[cfg(windows)]
mod sys {
    use super::{push, win_key, Input, Mods, Stopper};
    use std::cell::RefCell;
    use std::collections::HashSet;
    use std::ffi::c_void;
    use std::ptr;
    use std::sync::mpsc::channel;
    use std::time::Duration;

    type Hook = *mut c_void;
    type HookProc = unsafe extern "system" fn(code: i32, wparam: usize, lparam: isize) -> isize;

    #[repr(C)]
    #[allow(dead_code)]
    struct KbdLl {
        vk: u32,
        scan: u32,
        flags: u32,
        time: u32,
        extra: usize,
    }

    #[repr(C)]
    #[allow(dead_code)]
    struct MouseLl {
        x: i32,
        y: i32,
        data: u32,
        flags: u32,
        time: u32,
        extra: usize,
    }

    #[repr(C)]
    #[allow(dead_code)]
    struct Msg {
        hwnd: isize,
        message: u32,
        wparam: usize,
        lparam: isize,
        time: u32,
        x: i32,
        y: i32,
        private: u32,
    }

    #[link(name = "user32")]
    extern "system" {
        fn SetWindowsHookExW(id: i32, proc_: HookProc, module: *mut c_void, thread: u32) -> Hook;
        fn UnhookWindowsHookEx(hook: Hook) -> i32;
        fn CallNextHookEx(hook: Hook, code: i32, wparam: usize, lparam: isize) -> isize;
        fn GetMessageW(msg: *mut Msg, hwnd: isize, min: u32, max: u32) -> i32;
        fn PostThreadMessageW(thread: u32, msg: u32, wparam: usize, lparam: isize) -> i32;
        fn GetAsyncKeyState(vk: i32) -> i16;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetModuleHandleW(name: *const u16) -> *mut c_void;
        fn GetCurrentThreadId() -> u32;
    }

    const WH_KEYBOARD_LL: i32 = 13;
    const WH_MOUSE_LL: i32 = 14;
    const WM_QUIT: u32 = 0x0012;
    const WM_KEYDOWN: usize = 0x0100;
    const WM_KEYUP: usize = 0x0101;
    const WM_SYSKEYDOWN: usize = 0x0104;
    const WM_SYSKEYUP: usize = 0x0105;
    const WM_LBUTTONDOWN: usize = 0x0201;
    const WM_RBUTTONDOWN: usize = 0x0204;
    const WM_MBUTTONDOWN: usize = 0x0207;
    const WM_XBUTTONDOWN: usize = 0x020B;
    const LLKHF_EXTENDED: u32 = 0x01;

    thread_local! {
        /// Зажатые клавиши: повтор зажатой — не новое нажатие
        static HELD: RefCell<HashSet<&'static str>> = RefCell::new(HashSet::new());
    }

    fn down(vk: i32) -> bool {
        unsafe { GetAsyncKeyState(vk) as u16 & 0x8000 != 0 }
    }

    /// Модификаторы, зажатые до этого события (внутри хука состояние
    /// клавиши самого события ещё старое — для них это и нужно)
    fn mods() -> Mods {
        Mods { ctrl: down(0x11), alt: down(0x12), shift: down(0x10), meta: down(0x5B) || down(0x5C) }
    }

    unsafe extern "system" fn on_key(code: i32, wparam: usize, lparam: isize) -> isize {
        if code >= 0 && lparam != 0 {
            let k = &*(lparam as *const KbdLl);
            if let Some(name) = win_key(k.scan, k.flags & LLKHF_EXTENDED != 0, k.vk) {
                match wparam {
                    WM_KEYDOWN | WM_SYSKEYDOWN => {
                        let repeat = !HELD.with(|h| h.borrow_mut().insert(name));
                        push(Input::Down { code: name, mods: mods(), repeat });
                    }
                    WM_KEYUP | WM_SYSKEYUP => {
                        HELD.with(|h| h.borrow_mut().remove(name));
                        push(Input::Up { code: name });
                    }
                    _ => {}
                }
            }
        }
        CallNextHookEx(ptr::null_mut(), code, wparam, lparam)
    }

    unsafe extern "system" fn on_mouse(code: i32, wparam: usize, lparam: isize) -> isize {
        if code >= 0 && lparam != 0 {
            let m = &*(lparam as *const MouseLl);
            let button = match wparam {
                WM_LBUTTONDOWN => Some("MouseLeft"),
                WM_RBUTTONDOWN => Some("MouseRight"),
                WM_MBUTTONDOWN => Some("MouseMiddle"),
                WM_XBUTTONDOWN => match m.data >> 16 {
                    1 => Some("MouseBack"),
                    2 => Some("MouseForward"),
                    _ => None,
                },
                _ => None,
            };
            if let Some(button) = button {
                push(Input::Mouse { button, mods: mods() });
            }
        }
        CallNextHookEx(ptr::null_mut(), code, wparam, lparam)
    }

    pub fn keyboard_allowed() -> bool {
        true
    }

    pub fn ask_keyboard() {}

    pub fn open_settings() {}

    pub fn start(_keyboard: bool) -> Option<Stopper> {
        let (ready_tx, ready_rx) = channel::<Option<u32>>();
        std::thread::Builder::new()
            .name("input-hook".into())
            .spawn(move || unsafe {
                let module = GetModuleHandleW(ptr::null());
                let kb = SetWindowsHookExW(WH_KEYBOARD_LL, on_key, module, 0);
                let ms = SetWindowsHookExW(WH_MOUSE_LL, on_mouse, module, 0);
                if kb.is_null() && ms.is_null() {
                    let _ = ready_tx.send(None);
                    return;
                }
                let _ = ready_tx.send(Some(GetCurrentThreadId()));
                // Хуки зовутся из очереди сообщений этого потока; WM_QUIT — снять
                let mut msg: Msg = std::mem::zeroed();
                while GetMessageW(&mut msg, 0, 0, 0) > 0 {}
                if !kb.is_null() {
                    UnhookWindowsHookEx(kb);
                }
                if !ms.is_null() {
                    UnhookWindowsHookEx(ms);
                }
            })
            .ok()?;
        let tid = ready_rx.recv_timeout(Duration::from_secs(3)).ok().flatten()?;
        Some(Box::new(move || unsafe {
            PostThreadMessageW(tid, WM_QUIT, 0, 0);
        }))
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
mod sys {
    use super::Stopper;

    pub fn keyboard_allowed() -> bool {
        false
    }

    pub fn ask_keyboard() {}

    pub fn open_settings() {}

    pub fn start(_keyboard: bool) -> Option<Stopper> {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const NONE: Mods = Mods { ctrl: false, alt: false, shift: false, meta: false };
    const CTRL: Mods = Mods { ctrl: true, alt: false, shift: false, meta: false };
    const CTRL_SHIFT: Mods = Mods { ctrl: true, alt: false, shift: true, meta: false };

    #[test]
    fn combo_order_and_own_family() {
        assert_eq!(combo(CTRL_SHIFT, "KeyK"), "Ctrl+Shift+KeyK");
        assert_eq!(combo(Mods { ctrl: true, alt: true, shift: true, meta: true }, "MouseLeft"), "Ctrl+Alt+Shift+Meta+MouseLeft");
        // Shift при зажатом Ctrl: свой Shift в приставку не идёт
        assert_eq!(combo(CTRL_SHIFT, "ShiftLeft"), "Ctrl+ShiftLeft");
        assert_eq!(combo(NONE, "Space"), "Space");
    }

    #[test]
    fn valid_like_core() {
        for ok in ["KeyA", "Ctrl+Shift+KeyK", "Meta+MouseLeft", "F12", "Ctrl+Alt+Shift+Meta+Digit1"] {
            assert!(valid(ok), "{ok}");
        }
        for bad in ["", "Ctrl+", "keyA", "Shift+Ctrl+KeyA", "Key A", "Ctrl+Key-A", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"] {
            assert!(!valid(bad), "{bad}");
        }
    }

    #[test]
    fn tracker_keys_repeats_and_lone_modifiers() {
        let mut t = Tracker::default();
        assert_eq!(t.feed(Input::Down { code: "KeyK", mods: CTRL, repeat: false }), Some("Ctrl+KeyK".into()));
        assert_eq!(t.feed(Input::Down { code: "KeyK", mods: CTRL, repeat: true }), None);
        // Shift один: срабатывает, когда отпустили
        assert_eq!(t.feed(Input::Down { code: "ShiftLeft", mods: Mods { shift: true, ..NONE }, repeat: false }), None);
        assert_eq!(t.feed(Input::Up { code: "ShiftLeft" }), Some("ShiftLeft".into()));
        // Shift + буква: буква — да, Shift при отпускании — нет
        t.feed(Input::Down { code: "ShiftLeft", mods: Mods { shift: true, ..NONE }, repeat: false });
        assert_eq!(t.feed(Input::Down { code: "KeyA", mods: Mods { shift: true, ..NONE }, repeat: false }), Some("Shift+KeyA".into()));
        assert_eq!(t.feed(Input::Up { code: "KeyA" }), None);
        assert_eq!(t.feed(Input::Up { code: "ShiftLeft" }), None);
        // Ctrl зажат, нажали и отпустили Shift — «Ctrl+ShiftLeft»
        t.feed(Input::Down { code: "ControlLeft", mods: CTRL, repeat: false });
        t.feed(Input::Down { code: "ShiftLeft", mods: CTRL_SHIFT, repeat: false });
        assert_eq!(t.feed(Input::Up { code: "ShiftLeft" }), Some("Ctrl+ShiftLeft".into()));
        // Клик мыши отменяет ожидающий модификатор
        t.feed(Input::Down { code: "AltLeft", mods: Mods { alt: true, ..NONE }, repeat: false });
        assert_eq!(t.feed(Input::Mouse { button: "MouseLeft", mods: Mods { alt: true, ..NONE } }), Some("Alt+MouseLeft".into()));
        assert_eq!(t.feed(Input::Up { code: "AltLeft" }), None);
    }

    #[test]
    fn mac_table_is_valid_and_unique() {
        let mut seen = HashSet::new();
        for k in 0u16..=0x7F {
            if let Some(code) = mac_key(k) {
                assert!(valid(code), "{code}");
                assert!(seen.insert(code), "дважды {code}");
            }
        }
        assert_eq!(mac_key(0x28), Some("KeyK"));
        assert_eq!(mac_key(0x31), Some("Space"));
        assert_eq!(mac_key(0x34), None);
    }

    #[test]
    fn win_table() {
        assert_eq!(win_key(0x25, false, 0x4B), Some("KeyK"));
        assert_eq!(win_key(0x1C, false, 0x0D), Some("Enter"));
        assert_eq!(win_key(0x1C, true, 0x0D), Some("NumpadEnter"));
        assert_eq!(win_key(0x4B, true, 0x25), Some("ArrowLeft"));
        assert_eq!(win_key(0x4B, false, 0x64), Some("Numpad4"));
        assert_eq!(win_key(0x58, false, 0x7B), Some("F12"));
        assert_eq!(win_key(0x2E, false, 0xAE), Some("AudioVolumeDown"));
        assert_eq!(win_key(0x5B, true, 0x5B), Some("MetaLeft"));
        let mut seen = HashSet::new();
        for scan in 0u32..0x80 {
            if let Some(code) = win_key(scan, false, 0) {
                assert!(valid(code), "{code}");
                assert!(seen.insert(code), "дважды {code}");
            }
        }
    }
}
