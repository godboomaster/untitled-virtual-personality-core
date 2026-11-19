//! Различия macOS/Linux и Windows: как запускать и останавливать дочерние
//! процессы, кто слушает порт, где искать Python и Node.

use std::collections::HashSet;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
#[cfg(windows)]
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;

/// Бэкенд и vite: без окна консоли (Windows) и в своей группе процессов
/// (unix) — группу можно остановить целиком вместе с детьми
pub fn detach(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
}

/// Служебные утилиты (taskkill, lsof, проверка python) — без мелькающей консоли
pub fn quiet(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    let _ = cmd;
}

/// Мягкая остановка. unix — SIGTERM (группе, если group). На Windows
/// процессу без консоли мягко не сказать «выйди» — сразу дерево целиком
/// (бэкенд мягко останавливается раньше, через /api/system/shutdown)
pub fn terminate(pid: u32, group: bool) {
    #[cfg(unix)]
    unsafe {
        let target = if group { -(pid as i32) } else { pid as i32 };
        libc::kill(target, libc::SIGTERM);
    }
    #[cfg(windows)]
    {
        let _ = group;
        kill(pid, true);
    }
}

/// Жёсткая остановка: unix — SIGKILL (группе, если group), Windows —
/// taskkill /T /F: всегда с детьми (Chrome бота, esbuild у vite)
pub fn kill(pid: u32, group: bool) {
    #[cfg(unix)]
    unsafe {
        let target = if group { -(pid as i32) } else { pid as i32 };
        libc::kill(target, libc::SIGKILL);
    }
    #[cfg(windows)]
    {
        let _ = group;
        let mut cmd = Command::new("taskkill");
        cmd.args(["/PID", &pid.to_string(), "/T", "/F"]);
        quiet(&mut cmd);
        let _ = run_with_timeout(&mut cmd, Duration::from_secs(10));
    }
}

pub fn pid_alive(pid: u32) -> bool {
    #[cfg(unix)]
    unsafe {
        libc::kill(pid as i32, 0) == 0
            || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
    }
    #[cfg(windows)]
    {
        let mut cmd = Command::new("tasklist");
        cmd.args(["/FI", &format!("PID eq {pid}"), "/NH", "/FO", "CSV"]);
        quiet(&mut cmd);
        run_with_timeout(&mut cmd, Duration::from_secs(5))
            .map(|o| String::from_utf8_lossy(&o.stdout).contains(&format!("\"{pid}\"")))
            .unwrap_or(false)
    }
}

/// PID процессов, слушающих TCP-порт (бот или vite, запущенные вне панели)
pub fn pids_listening(port: u16) -> Vec<u32> {
    #[cfg(unix)]
    {
        let mut cmd = Command::new("lsof");
        cmd.args(["-nP", &format!("-iTCP:{port}"), "-sTCP:LISTEN", "-t"]);
        run_with_timeout(&mut cmd, Duration::from_secs(5))
            .map(|o| {
                String::from_utf8_lossy(&o.stdout)
                    .lines()
                    .filter_map(|l| l.trim().parse().ok())
                    .collect()
            })
            .unwrap_or_default()
    }
    #[cfg(windows)]
    {
        // Строка netstat: «TCP 127.0.0.1:8000 0.0.0.0:0 LISTENING 1234».
        // Слово состояния локализовано (на русской Windows — не LISTENING),
        // поэтому слушающий сокет узнаём по нулевому удалённому адресу
        let mut out: Vec<u32> = Vec::new();
        for proto in ["TCP", "TCPv6"] {
            let mut cmd = Command::new("netstat");
            cmd.args(["-ano", "-p", proto]);
            quiet(&mut cmd);
            let Some(o) = run_with_timeout(&mut cmd, Duration::from_secs(10)) else { continue };
            for line in String::from_utf8_lossy(&o.stdout).lines() {
                let cols: Vec<&str> = line.split_whitespace().collect();
                if cols.len() < 5 || !cols[0].starts_with("TCP") {
                    continue;
                }
                let local_port = cols[1].rsplit(':').next().unwrap_or("");
                let remote_any = matches!(cols[2], "0.0.0.0:0" | "[::]:0" | "*:*");
                if local_port == port.to_string() && remote_any {
                    if let Ok(pid) = cols[cols.len() - 1].parse::<u32>() {
                        if pid != 0 && !out.contains(&pid) {
                            out.push(pid);
                        }
                    }
                }
            }
        }
        out
    }
}

/// Запуск утилиты с потолком по времени (зависшая — убивается)
pub fn run_with_timeout(cmd: &mut Command, timeout: Duration) -> Option<Output> {
    cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = cmd.spawn().ok()?;
    let mut so = child.stdout.take()?;
    let mut se = child.stderr.take()?;
    let ro = thread::spawn(move || {
        let mut b = Vec::new();
        let _ = so.read_to_end(&mut b);
        b
    });
    let re = thread::spawn(move || {
        let mut b = Vec::new();
        let _ = se.read_to_end(&mut b);
        b
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(50)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    Some(Output { status, stdout: ro.join().unwrap_or_default(), stderr: re.join().unwrap_or_default() })
}

/// PATH для бэкенда. Приложение, запущенное из Finder/при входе, получает
/// урезанный PATH (/usr/bin:/bin:…) — берём PATH из login-shell пользователя,
/// как в терминале. На Windows PATH пользователя и так у приложения
pub fn user_path() -> String {
    let current = std::env::var("PATH").unwrap_or_default();
    #[cfg(unix)]
    {
        const MARK: &str = "__VPC_PATH__";
        let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
        let mut cmd = Command::new(shell);
        cmd.args(["-ilc", &format!("printf '{MARK}%s{MARK}' \"$PATH\"")]);
        let from_shell = run_with_timeout(&mut cmd, Duration::from_secs(8)).and_then(|o| {
            let s = String::from_utf8_lossy(&o.stdout).into_owned();
            let start = s.find(MARK)? + MARK.len();
            let end = s[start..].find(MARK)? + start;
            Some(s[start..end].to_string())
        });
        let extra = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";
        let mut parts: Vec<String> = Vec::new();
        for chunk in [from_shell.unwrap_or_default(), current, extra.to_string()] {
            for p in chunk.split(':').filter(|p| !p.is_empty()) {
                if !parts.iter().any(|x| x == p) {
                    parts.push(p.to_string());
                }
            }
        }
        parts.join(":")
    }
    #[cfg(windows)]
    {
        current
    }
}

#[cfg(windows)]
const PY_NAMES: &[&str] = &["python.exe", "python3.exe"];
#[cfg(not(windows))]
const PY_NAMES: &[&str] = &["python3", "python"];

fn venv_python(repo: &Path, venv: &str) -> PathBuf {
    if cfg!(windows) {
        repo.join(venv).join("Scripts").join("python.exe")
    } else {
        repo.join(venv).join("bin").join("python3")
    }
}

fn well_known_pythons() -> Vec<PathBuf> {
    let mut out = Vec::new();
    #[cfg(target_os = "macos")]
    {
        let base = Path::new("/Library/Frameworks/Python.framework/Versions");
        if let Ok(rd) = std::fs::read_dir(base) {
            let mut vers: Vec<PathBuf> =
                rd.flatten().map(|e| e.path()).filter(|p| !p.ends_with("Current")).collect();
            vers.sort();
            vers.reverse();
            out.extend(vers.into_iter().map(|v| v.join("bin").join("python3")));
        }
        out.push("/opt/homebrew/bin/python3".into());
        out.push("/usr/local/bin/python3".into());
    }
    #[cfg(windows)]
    {
        for root in [std::env::var("LOCALAPPDATA").map(|d| format!("{d}\\Programs\\Python")), std::env::var("ProgramFiles")]
            .into_iter()
            .flatten()
        {
            if let Ok(rd) = std::fs::read_dir(&root) {
                let mut vers: Vec<PathBuf> = rd
                    .flatten()
                    .map(|e| e.path())
                    .filter(|p| p.file_name().map(|n| n.to_string_lossy().starts_with("Python3")).unwrap_or(false))
                    .collect();
                vers.sort();
                vers.reverse();
                out.extend(vers.into_iter().map(|v| v.join("python.exe")));
            }
        }
    }
    out
}

/// Python, в котором есть зависимости бэкенда: venv проекта, затем PATH,
/// затем обычные места установки. Homebrew-python без fastapi не подходит —
/// поэтому каждый кандидат проверяется импортом
pub fn find_python(repo: &Path, path: &str) -> Option<PathBuf> {
    let mut cands: Vec<PathBuf> = vec![venv_python(repo, ".venv"), venv_python(repo, "venv")];
    for dir in std::env::split_paths(path) {
        cands.extend(PY_NAMES.iter().map(|n| dir.join(n)));
    }
    cands.extend(well_known_pythons());
    let mut seen = HashSet::new();
    for c in cands {
        // Заглушка Microsoft Store вместо Python открывает магазин
        if !c.is_file() || c.to_string_lossy().contains("WindowsApps") {
            continue;
        }
        if !seen.insert(c.canonicalize().unwrap_or_else(|_| c.clone())) {
            continue;
        }
        if python_has_deps(&c, repo) {
            return Some(c);
        }
    }
    None
}

pub fn python_has_deps(py: &Path, repo: &Path) -> bool {
    let mut cmd = Command::new(py);
    cmd.args(["-c", "import fastapi, uvicorn, dotenv"]).current_dir(repo);
    quiet(&mut cmd);
    run_with_timeout(&mut cmd, Duration::from_secs(30)).map(|o| o.status.success()).unwrap_or(false)
}

pub fn find_node(path: &str) -> Option<PathBuf> {
    let name = if cfg!(windows) { "node.exe" } else { "node" };
    let mut cands: Vec<PathBuf> = std::env::split_paths(path).map(|d| d.join(name)).collect();
    if cfg!(windows) {
        if let Ok(pf) = std::env::var("ProgramFiles") {
            cands.push(Path::new(&pf).join("nodejs").join("node.exe"));
        }
    } else {
        cands.push("/opt/homebrew/bin/node".into());
        cands.push("/usr/local/bin/node".into());
    }
    cands.into_iter().find(|c| c.is_file())
}

/// Сколько секунд не было ни мыши, ни клавиатуры (по всей системе): портрет
/// на рабочем столе засыпает, когда вас нет. Узнать нельзя — None
pub fn idle_secs() -> Option<f64> {
    idle::secs()
}

#[cfg(target_os = "macos")]
mod idle {
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventSourceSecondsSinceLastEventType(state_id: i32, event_type: u32) -> f64;
    }

    // kCGEventSourceStateCombinedSessionState, kCGAnyInputEventType
    const COMBINED_SESSION: i32 = 0;
    const ANY_INPUT: u32 = u32::MAX;

    pub fn secs() -> Option<f64> {
        let s = unsafe { CGEventSourceSecondsSinceLastEventType(COMBINED_SESSION, ANY_INPUT) };
        (s.is_finite() && s >= 0.0).then_some(s)
    }
}

#[cfg(windows)]
mod idle {
    #[repr(C)]
    struct LastInputInfo {
        cb_size: u32,
        dw_time: u32,
    }

    #[link(name = "user32")]
    extern "system" {
        fn GetLastInputInfo(plii: *mut LastInputInfo) -> i32;
    }

    #[link(name = "kernel32")]
    extern "system" {
        fn GetTickCount() -> u32;
    }

    pub fn secs() -> Option<f64> {
        let mut info = LastInputInfo { cb_size: std::mem::size_of::<LastInputInfo>() as u32, dw_time: 0 };
        if unsafe { GetLastInputInfo(&mut info) } == 0 {
            return None;
        }
        // Оба счётчика — миллисекунды с загрузки, по кругу раз в 49,7 дня
        Some(unsafe { GetTickCount() }.wrapping_sub(info.dw_time) as f64 / 1000.0)
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
mod idle {
    pub fn secs() -> Option<f64> {
        None
    }
}

/// Окну можно стоять и выше строки меню: macOS сама прижимает окна под неё
/// (NSWindow constrainFrameRect:toScreen:), а голову портрета можно задвинуть
/// за верх экрана. Метод переопределяется у класса окна tao (TaoWindow) —
/// значит, у всех окон панели: окно панели и так ставится под значком в трее
pub fn allow_offscreen(window: &tauri::WebviewWindow) {
    #[cfg(target_os = "macos")]
    if let Ok(ns) = window.ns_window() {
        offscreen::allow(ns);
    }
    #[cfg(not(target_os = "macos"))]
    let _ = window;
}

#[cfg(target_os = "macos")]
mod offscreen {
    use std::ffi::{c_char, c_void, CStr};

    #[repr(C)]
    #[derive(Clone, Copy)]
    pub struct Rect {
        x: f64,
        y: f64,
        w: f64,
        h: f64,
    }

    #[link(name = "objc")]
    extern "C" {
        fn object_getClass(obj: *mut c_void) -> *mut c_void;
        fn sel_registerName(name: *const c_char) -> *mut c_void;
        fn class_addMethod(cls: *mut c_void, sel: *mut c_void, imp: *const c_void, types: *const c_char) -> bool;
    }

    extern "C" fn keep_frame(_this: *mut c_void, _sel: *mut c_void, rect: Rect, _screen: *mut c_void) -> Rect {
        rect
    }

    pub fn allow(ns_window: *mut c_void) {
        let sel = CStr::from_bytes_with_nul(b"constrainFrameRect:toScreen:\0").unwrap();
        let types =
            CStr::from_bytes_with_nul(b"{CGRect={CGPoint=dd}{CGSize=dd}}@:{CGRect={CGPoint=dd}{CGSize=dd}}@\0").unwrap();
        // Второй раз class_addMethod ничего не делает: метод у класса уже свой
        unsafe {
            let cls = object_getClass(ns_window);
            class_addMethod(cls, sel_registerName(sel.as_ptr()), keep_frame as *const c_void, types.as_ptr());
        }
    }
}

/// Зажата ли основная кнопка мыши (по всей системе): портрет на рабочем столе
/// идёт за курсором, пока она зажата. Узнать нельзя — None
pub fn primary_button_down() -> Option<bool> {
    mouse::primary_down()
}

#[cfg(target_os = "macos")]
mod mouse {
    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventSourceButtonState(state_id: i32, button: u32) -> bool;
    }

    // kCGEventSourceStateCombinedSessionState, kCGMouseButtonLeft
    const COMBINED_SESSION: i32 = 0;
    const BUTTON_LEFT: u32 = 0;

    pub fn primary_down() -> Option<bool> {
        Some(unsafe { CGEventSourceButtonState(COMBINED_SESSION, BUTTON_LEFT) })
    }
}

#[cfg(windows)]
mod mouse {
    #[link(name = "user32")]
    extern "system" {
        fn GetAsyncKeyState(vkey: i32) -> i16;
        fn GetSystemMetrics(index: i32) -> i32;
    }

    const VK_LBUTTON: i32 = 0x01;
    const VK_RBUTTON: i32 = 0x02;
    const SM_SWAPBUTTON: i32 = 23;

    pub fn primary_down() -> Option<bool> {
        // GetAsyncKeyState видит кнопки физически: поменяли местами (левша) —
        // основная правая
        let vk = if unsafe { GetSystemMetrics(SM_SWAPBUTTON) } != 0 { VK_RBUTTON } else { VK_LBUTTON };
        Some(unsafe { GetAsyncKeyState(vk) } as u16 & 0x8000 != 0)
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
mod mouse {
    pub fn primary_down() -> Option<bool> {
        None
    }
}

/// Окно другой программы на экране: id и рамка, логические px (как у окон
/// панели: верхний левый угол основного монитора — 0, y вниз)
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct WinRect {
    pub id: u64,
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

/// Видимые обычные окна других программ — спереди назад (по z-порядку): по
/// ним портрет ходит и на них сидит. Свои окна, строка меню, Dock, панель
/// задач, рабочий стол, скрытые и свёрнутые — не в списке. scale — масштаб
/// окна портрета: на Windows рамки в физических px. Узнать нельзя — пусто
pub fn windows(scale: f64) -> Vec<WinRect> {
    wins::list(scale)
}

#[cfg(target_os = "macos")]
mod wins {
    use super::WinRect;
    use std::ffi::c_void;

    type CFRef = *const c_void;

    #[repr(C)]
    #[derive(Clone, Copy, Default)]
    struct CGRect {
        x: f64,
        y: f64,
        w: f64,
        h: f64,
    }

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGWindowListCopyWindowInfo(option: u32, relative_to: u32) -> CFRef;
        fn CGRectMakeWithDictionaryRepresentation(dict: CFRef, rect: *mut CGRect) -> bool;
        static kCGWindowLayer: CFRef;
        static kCGWindowBounds: CFRef;
        static kCGWindowOwnerPID: CFRef;
        static kCGWindowNumber: CFRef;
        static kCGWindowAlpha: CFRef;
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFArrayGetCount(array: CFRef) -> isize;
        fn CFArrayGetValueAtIndex(array: CFRef, index: isize) -> CFRef;
        fn CFDictionaryGetValue(dict: CFRef, key: CFRef) -> CFRef;
        fn CFNumberGetValue(number: CFRef, kind: isize, out: *mut c_void) -> u8;
        fn CFRelease(obj: CFRef);
    }

    // kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
    const ON_SCREEN: u32 = 1 | 16;
    // kCFNumberSInt64Type, kCFNumberFloat64Type
    const SINT64: isize = 4;
    const FLOAT64: isize = 6;

    unsafe fn int(dict: CFRef, key: CFRef) -> Option<i64> {
        let n = CFDictionaryGetValue(dict, key);
        let mut v: i64 = 0;
        (!n.is_null() && CFNumberGetValue(n, SINT64, &mut v as *mut i64 as *mut c_void) != 0).then_some(v)
    }

    unsafe fn float(dict: CFRef, key: CFRef) -> Option<f64> {
        let n = CFDictionaryGetValue(dict, key);
        let mut v: f64 = 0.0;
        (!n.is_null() && CFNumberGetValue(n, FLOAT64, &mut v as *mut f64 as *mut c_void) != 0).then_some(v)
    }

    pub fn list(_scale: f64) -> Vec<WinRect> {
        let me = std::process::id() as i64;
        let mut out = Vec::new();
        unsafe {
            let all = CGWindowListCopyWindowInfo(ON_SCREEN, 0);
            if all.is_null() {
                return out;
            }
            for i in 0..CFArrayGetCount(all) {
                let d = CFArrayGetValueAtIndex(all, i);
                // Слой 0 — обычные окна (строка меню, Dock, всплывающие — выше)
                if d.is_null() || int(d, kCGWindowLayer) != Some(0) || int(d, kCGWindowOwnerPID) == Some(me) {
                    continue;
                }
                if float(d, kCGWindowAlpha).map(|a| a <= 0.01).unwrap_or(false) {
                    continue;
                }
                let b = CFDictionaryGetValue(d, kCGWindowBounds);
                let mut r = CGRect::default();
                if b.is_null() || !CGRectMakeWithDictionaryRepresentation(b, &mut r) || r.w < 1.0 || r.h < 1.0 {
                    continue;
                }
                let id = int(d, kCGWindowNumber).unwrap_or(0) as u64;
                out.push(WinRect { id, x: r.x, y: r.y, w: r.w, h: r.h });
            }
            CFRelease(all);
        }
        out
    }
}

#[cfg(windows)]
mod wins {
    use super::WinRect;
    use std::ffi::c_void;

    type Hwnd = *mut c_void;

    #[repr(C)]
    #[derive(Default)]
    struct Rect {
        left: i32,
        top: i32,
        right: i32,
        bottom: i32,
    }

    #[link(name = "user32")]
    extern "system" {
        fn EnumWindows(cb: extern "system" fn(Hwnd, isize) -> i32, lparam: isize) -> i32;
        fn IsWindowVisible(h: Hwnd) -> i32;
        fn IsIconic(h: Hwnd) -> i32;
        fn GetWindowRect(h: Hwnd, r: *mut Rect) -> i32;
        fn GetWindowLongPtrW(h: Hwnd, index: i32) -> isize;
        fn GetWindowThreadProcessId(h: Hwnd, pid: *mut u32) -> u32;
        fn GetClassNameW(h: Hwnd, buf: *mut u16, max: i32) -> i32;
    }

    #[link(name = "dwmapi")]
    extern "system" {
        fn DwmGetWindowAttribute(h: Hwnd, attr: u32, out: *mut c_void, size: u32) -> i32;
    }

    const GWL_EXSTYLE: i32 = -20;
    const WS_EX_TOOLWINDOW: isize = 0x80;
    const WS_EX_NOACTIVATE: isize = 0x0800_0000;
    const DWMWA_EXTENDED_FRAME_BOUNDS: u32 = 9;
    const DWMWA_CLOAKED: u32 = 14;
    // Рабочий стол и панель задач — не окна, на которых стоят
    const SKIP_CLASSES: [&str; 4] = ["Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"];

    extern "system" fn collect(h: Hwnd, lparam: isize) -> i32 {
        // lparam — &mut Vec<Hwnd> из list(); EnumWindows зовёт синхронно
        let all = unsafe { &mut *(lparam as *mut Vec<Hwnd>) };
        all.push(h);
        1
    }

    fn class(h: Hwnd) -> String {
        let mut buf = [0u16; 64];
        let n = unsafe { GetClassNameW(h, buf.as_mut_ptr(), buf.len() as i32) };
        String::from_utf16_lossy(&buf[..n.max(0) as usize])
    }

    pub fn list(scale: f64) -> Vec<WinRect> {
        let s = if scale.is_finite() && scale > 0.0 { scale } else { 1.0 };
        let me = std::process::id();
        let mut all: Vec<Hwnd> = Vec::new();
        // По z-порядку: первое — самое переднее
        unsafe { EnumWindows(collect, &mut all as *mut Vec<Hwnd> as isize) };
        let mut out = Vec::new();
        for h in all {
            unsafe {
                if IsWindowVisible(h) == 0 || IsIconic(h) != 0 {
                    continue;
                }
                let ex = GetWindowLongPtrW(h, GWL_EXSTYLE);
                if ex & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) != 0 {
                    continue;
                }
                let mut pid = 0u32;
                GetWindowThreadProcessId(h, &mut pid);
                if pid == me {
                    continue;
                }
                // Окна «спрятанные» системой (другие рабочие столы, приложения UWP в фоне)
                let mut cloaked = 0u32;
                let hr = DwmGetWindowAttribute(h, DWMWA_CLOAKED, &mut cloaked as *mut u32 as *mut c_void, 4);
                if hr == 0 && cloaked != 0 {
                    continue;
                }
                if SKIP_CLASSES.contains(&class(h).as_str()) {
                    continue;
                }
                // Рамка без невидимых полей под тень (Windows 10/11)
                let mut r = Rect::default();
                let size = std::mem::size_of::<Rect>() as u32;
                if DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS, &mut r as *mut Rect as *mut c_void, size) != 0
                    && GetWindowRect(h, &mut r) == 0
                {
                    continue;
                }
                let (w, hh) = ((r.right - r.left) as f64, (r.bottom - r.top) as f64);
                if w < 1.0 || hh < 1.0 {
                    continue;
                }
                out.push(WinRect { id: h as usize as u64, x: r.left as f64 / s, y: r.top as f64 / s, w: w / s, h: hh / s });
            }
        }
        out
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
mod wins {
    use super::WinRect;

    pub fn list(_scale: f64) -> Vec<WinRect> {
        Vec::new()
    }
}
