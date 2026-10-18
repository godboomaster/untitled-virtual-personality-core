//! Голосовая активация («Коннор, …») — в панели, а не в ядре: слушатель
//! имени — дочерний процесс панели (python -m app.voice.wake_listener
//! --status). Микрофон принадлежит панели — macOS спрашивает доступ у
//! «Virtual Persona»; слушание включается в трее и живёт, пока живёт панель,
//! ядро нужно только для ответа. Состояние слушатель печатает строками
//! «WAKE_STATUS {json}»; ядру, запущенному панелью, — WAKE_LISTEN=0
//! (supervisor.rs), а ядро из терминала само уступает микрофон, пока
//! слушатель панели сообщает ему о себе.

use crate::config::{self, display_path};
use crate::logs::{self, LogSink};
use crate::platform;
use crate::supervisor::Supervisor;
use crate::texts::{t, tf, Lang};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

const STATUS_PREFIX: &str = "WAKE_STATUS ";
/// Слушатель вышел с «нет модулей распознавания» — перезапуск не поможет
const EXIT_SETUP: i32 = 2;
/// Слушатель закрывает микрофон штатно до 5 с (STOP_WAIT_SEC)
const STOP_GRACE: Duration = Duration::from_secs(7);
const RESTART_DELAY: Duration = Duration::from_secs(5);
/// Автоперезапуск: не больше CRASH_MAX падений за CRASH_WINDOW
const CRASH_WINDOW: Duration = Duration::from_secs(600);
const CRASH_MAX: usize = 3;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum VoicePhase {
    Off,
    /// Процесс запущен, состояния от него ещё не было
    Starting,
    Running,
    Stopping,
    /// Упал или не запустился; error — почему
    Failed,
}

/// Состояние слушателя — WakeListener.status() в app/voice/wake_listener.py
#[derive(Clone, Debug, Default, PartialEq, Deserialize, Serialize)]
#[serde(default, rename_all = "camelCase")]
pub struct WakeStatus {
    pub running: bool,
    /// idle — ждёт имя, await — ждёт просьбу, busy — отвечает
    pub state: String,
    pub persona: Option<String>,
    /// Пауза: открыт голосовой чат веба
    pub held: bool,
    /// model — грузит распознавание, names — ждёт имена от ядра, listening,
    /// mic — микрофон недоступен (повтор)
    pub stage: String,
    pub names: Vec<String>,
    pub error: String,
    /// mic_access, mic, core, modules, failed — панель переводит сама
    pub issue: String,
    #[serde(alias = "last_heard")]
    pub last_heard: String,
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct VoiceView {
    pub enabled: bool,
    pub phase: VoicePhase,
    pub pid: Option<u32>,
    pub status: Option<WakeStatus>,
    pub error: Option<String>,
    pub log_file: String,
}

/// То, что показывает трей: галочка «Слушать имя» и подсказка значка
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct VoiceSummary {
    pub enabled: bool,
    pub listening: bool,
}

struct Inner {
    phase: VoicePhase,
    child: Option<Child>,
    pid: Option<u32>,
    /// Номер запуска: строки состояния прежнего процесса — мимо
    run: u64,
    status: Option<WakeStatus>,
    error: Option<String>,
    stop_requested: bool,
    /// Не запускать снова, пока слушание не включат заново (нет модулей
    /// или Python, падает раз за разом)
    blocked: bool,
    restart_at: Option<Instant>,
    crashes: Vec<Instant>,
    /// Панель закрывается: больше не запускать
    closing: bool,
    log: Arc<Mutex<LogSink>>,
}

fn lk<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

#[derive(Clone)]
pub struct Voice {
    inner: Arc<Mutex<Inner>>,
    /// Запуск и остановка — по одной за раз
    op: Arc<Mutex<()>>,
    sup: Supervisor,
    lang: Lang,
}

impl Voice {
    pub fn new(sup: Supervisor, lang: Lang) -> Self {
        let inner = Inner {
            phase: VoicePhase::Off,
            child: None,
            pid: None,
            run: 0,
            status: None,
            error: None,
            stop_requested: false,
            blocked: false,
            restart_at: None,
            crashes: Vec::new(),
            closing: false,
            log: Arc::new(Mutex::new(LogSink::new(sup.logs_dir(), "voice"))),
        };
        Voice { inner: Arc::new(Mutex::new(inner)), op: Arc::new(Mutex::new(())), sup, lang }
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        lk(&self.inner)
    }

    pub fn view(&self) -> VoiceView {
        let enabled = self.sup.wake_enabled();
        let g = self.lock();
        let log_file = display_path(&lk(&g.log).current_path());
        VoiceView { enabled, phase: g.phase, pid: g.pid, status: g.status.clone(), error: g.error.clone(), log_file }
    }

    pub fn summary(&self) -> VoiceSummary {
        let enabled = self.sup.wake_enabled();
        let g = self.lock();
        let listening = g.phase == VoicePhase::Running
            && g.status.as_ref().is_some_and(|s| s.running && s.stage == "listening" && s.issue != "mic_access");
        VoiceSummary { enabled, listening }
    }

    /// Включить или выключить слушание (трей, кнопка в панели). Включение
    /// снимает запрет на запуск: пользователь, скорее всего, поправил причину
    pub fn set_enabled(&self, on: bool) -> Result<(), String> {
        self.sup.set_wake(on)?;
        if on {
            let mut g = self.lock();
            g.blocked = false;
            g.crashes.clear();
            g.restart_at = None;
            g.error = None;
        }
        let me = self.clone();
        thread::spawn(move || me.reconcile());
        Ok(())
    }

    /// Раз в секунду: подобрать вышедший процесс, привести к включено/выключено
    pub fn spawn_monitor<F: Fn(VoiceSummary) + Send + 'static>(&self, notify: F) {
        let me = self.clone();
        thread::spawn(move || {
            let mut last: Option<VoiceSummary> = None;
            loop {
                me.reconcile();
                let sum = me.summary();
                if last != Some(sum) {
                    notify(sum);
                    last = Some(sum);
                }
                thread::sleep(Duration::from_secs(1));
            }
        });
    }

    fn reconcile(&self) {
        let _op = lk(&self.op);
        if self.lock().closing {
            return;
        }
        let dir = self.sup.logs_dir();
        let log = self.lock().log.clone();
        lk(&log).set_dir(dir);
        self.reap();
        let on = self.sup.wake_enabled();
        let (has_child, may_start) = {
            let g = self.lock();
            let due = g.restart_at.map_or(true, |t| Instant::now() >= t);
            (g.child.is_some(), !g.blocked && due)
        };
        if on && !has_child && may_start {
            self.start();
        } else if !on && has_child {
            self.stop_child();
        } else if !on {
            let mut g = self.lock();
            g.phase = VoicePhase::Off;
            g.status = None;
            g.error = None;
            g.blocked = false;
            g.restart_at = None;
        }
    }

    fn reap(&self) {
        let lang = self.lang;
        let mut g = self.lock();
        let Some(child) = g.child.as_mut() else { return };
        let Ok(Some(status)) = child.try_wait() else { return };
        g.child = None;
        g.pid = None;
        if g.stop_requested {
            g.phase = VoicePhase::Off;
            return;
        }
        let code = status.code();
        // Причина: последняя ошибка из состояния слушателя, иначе — его вывод
        let reason = g
            .status
            .as_ref()
            .map(|s| s.error.clone())
            .filter(|e| !e.is_empty())
            .or_else(|| lk(&g.log).tail.back().cloned());
        let how = match code {
            Some(c) => tf(lang, "err_code", c),
            None => status.to_string(),
        };
        lk(&g.log).marker(&format!("{}: {how}", t(lang, "log_exit")));
        let mut err = tf(lang, "err_exit", &how);
        if let Some(r) = reason {
            err = format!("{err} — {}", shorten(&r, 200));
        }
        if code == Some(EXIT_SETUP) {
            g.blocked = true;
        } else {
            g.crashes.retain(|t| t.elapsed() < CRASH_WINDOW);
            if g.crashes.len() < CRASH_MAX {
                g.crashes.push(Instant::now());
                g.restart_at = Some(Instant::now() + RESTART_DELAY);
            } else {
                g.blocked = true;
                err = format!("{} {err}", t(lang, "err_restart_limit"));
            }
        }
        g.phase = VoicePhase::Failed;
        g.error = Some(err);
    }

    fn fail(&self, msg: String) {
        let mut g = self.lock();
        g.phase = VoicePhase::Failed;
        g.blocked = true;
        g.error = Some(msg);
    }

    fn start(&self) {
        let lang = self.lang;
        let cfg = self.sup.config();
        let repo = PathBuf::from(&cfg.repo_dir);
        if !config::repo_ok(&repo) {
            return self.fail(tf(lang, "err_repo", &cfg.repo_dir));
        }
        let python = match self.sup.resolve_python(&repo) {
            Ok(p) => p,
            Err(e) => return self.fail(e),
        };
        let api = self.sup.api();
        let mut cmd = listener_command(&python, &repo, &api.base);
        cmd.env("PATH", self.sup.user_path());
        platform::detach(&mut cmd);
        let (log, run) = {
            let mut g = self.lock();
            g.run += 1;
            (g.log.clone(), g.run)
        };
        match cmd.spawn() {
            Ok(mut child) => {
                let pid = child.id();
                let label = format!("{} -m app.voice.wake_listener", display_path(&python));
                lk(&log).marker(&format!("{} (pid {pid}): {label}", t(lang, "log_start")));
                let inner = self.inner.clone();
                logs::pump_with(child.stdout.take(), move |line| on_stdout(&inner, run, line));
                logs::pump(child.stderr.take(), log);
                let mut g = self.lock();
                g.child = Some(child);
                g.pid = Some(pid);
                g.phase = VoicePhase::Starting;
                g.status = None;
                g.error = None;
                g.stop_requested = false;
                g.restart_at = None;
            }
            Err(e) => self.fail(tf(lang, "err_spawn", e)),
        }
    }

    /// SIGTERM слушателю — он сам закроет микрофон и оборвёт озвучку;
    /// не вышел — вся его группа (say, afplay) жёстко
    fn stop_child(&self) {
        let pid = {
            let mut g = self.lock();
            g.stop_requested = true;
            g.phase = VoicePhase::Stopping;
            g.pid
        };
        let Some(pid) = pid else { return };
        platform::terminate(pid, false);
        if !self.wait_exit(STOP_GRACE) {
            platform::kill(pid, true);
            self.wait_exit(Duration::from_secs(3));
        }
        let mut g = self.lock();
        if let Some(mut c) = g.child.take() {
            let _ = c.wait();
        }
        g.pid = None;
        g.phase = VoicePhase::Off;
        g.status = None;
        lk(&g.log).marker(&t(self.lang, "log_stop"));
    }

    fn wait_exit(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        loop {
            let exited = {
                let mut g = self.lock();
                match g.child.as_mut() {
                    Some(c) => matches!(c.try_wait(), Ok(Some(_))),
                    None => true,
                }
            };
            if exited {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            thread::sleep(Duration::from_millis(100));
        }
    }

    /// Выход из панели: закрыть микрофон, дождаться слушателя и больше не
    /// запускать (панель ещё ждёт остановки бота)
    pub fn stop_blocking(&self) {
        let _op = lk(&self.op);
        self.lock().closing = true;
        if self.lock().child.is_some() {
            self.stop_child();
        }
    }
}

fn listener_command(python: &Path, repo: &Path, core: &str) -> Command {
    let mut cmd = Command::new(python);
    cmd.args(["-m", "app.voice.wake_listener", "--status", "--core", core])
        .current_dir(repo)
        .env("PYTHONUNBUFFERED", "1")
        // Вывод в трубу на Windows иначе в cp1251 — русские строки падали бы
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    cmd
}

/// Строка stdout слушателя: состояние — себе, остальное — в лог
fn on_stdout(inner: &Arc<Mutex<Inner>>, run: u64, line: &str) {
    let mut g = lk(inner);
    match parse_status(line) {
        Some(st) if g.run == run => {
            if g.phase == VoicePhase::Starting {
                g.phase = VoicePhase::Running;
            }
            g.status = Some(st);
        }
        Some(_) => {}
        None => {
            let log = g.log.clone();
            drop(g);
            lk(&log).line(line);
        }
    }
}

fn parse_status(line: &str) -> Option<WakeStatus> {
    serde_json::from_str(line.trim_end().strip_prefix(STATUS_PREFIX)?).ok()
}

fn shorten(s: &str, n: usize) -> String {
    if s.chars().count() <= n {
        s.to_string()
    } else {
        format!("{}…", s.chars().take(n).collect::<String>())
    }
}

#[cfg(test)]
mod tests {
    //! Слушатель на поддельном модуле (вместо распознавания — печать
    //! состояния): включение, состояние из stdout, мягкая остановка SIGTERM,
    //! «нет модулей» без перезапуска, падение и перезапуск. Микрофон не нужен.

    use super::*;
    use crate::config::Config;
    use std::fs;
    use std::net::TcpListener;

    const FAKE_LISTENER: &str = r#"
import json, os, signal, sys, time
mode = open("mode").read().strip() if os.path.exists("mode") else "ok"
def st(**kw):
    base = {"running": True, "state": "idle", "persona": None, "held": False, "stage": "listening",
            "names": ["коннор"], "error": "", "issue": "", "last_heard": "коннор привет"}
    base.update(kw)
    print("WAKE_STATUS " + json.dumps(base, ensure_ascii=False), flush=True)
if mode == "setup":
    st(running=False, stage="model", error="нет модулей в этом Python", issue="modules")
    sys.exit(2)
print("args " + " ".join(sys.argv[1:]), flush=True)
st()
if mode == "crash":
    print("RuntimeError: boom", flush=True)
    sys.exit(1)
stop = []
signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
while not stop:
    time.sleep(0.05)
st(running=False)
print("listener stopped", flush=True)
"#;

    fn python() -> Option<PathBuf> {
        let path = std::env::var("PATH").unwrap_or_default();
        std::env::split_paths(&path)
            .map(|d| d.join(if cfg!(windows) { "python.exe" } else { "python3" }))
            .find(|p| p.is_file())
    }

    fn wait_for(v: &Voice, secs: u64, what: &str, f: impl Fn(&VoiceView) -> bool) -> VoiceView {
        let deadline = Instant::now() + Duration::from_secs(secs);
        loop {
            let view = v.view();
            if f(&view) {
                return view;
            }
            assert!(Instant::now() < deadline, "не дождались: {what}; сейчас {:?} {:?}", view.phase, view.error);
            thread::sleep(Duration::from_millis(100));
        }
    }

    fn read_logs(dir: &Path) -> String {
        fs::read_dir(dir)
            .map(|rd| rd.flatten().filter_map(|e| fs::read_to_string(e.path()).ok()).collect())
            .unwrap_or_default()
    }

    #[test]
    fn lifecycle_on_fake_listener() {
        let Some(py) = python() else {
            eprintln!("нет python3 в PATH — тест пропущен");
            return;
        };
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let repo = std::env::temp_dir().join(format!("vpc-voice-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&repo);
        fs::create_dir_all(repo.join("app").join("voice")).unwrap();
        fs::write(repo.join("app").join("__init__.py"), "").unwrap();
        fs::write(repo.join("app").join("main.py"), "").unwrap();
        fs::write(repo.join("app").join("voice").join("__init__.py"), "").unwrap();
        fs::write(repo.join("app").join("voice").join("wake_listener.py"), FAKE_LISTENER).unwrap();
        fs::write(repo.join(".env"), format!("API_PORT={port}\nWAKE_LISTEN=1\n")).unwrap();
        let cfg = Config {
            repo_dir: display_path(&repo),
            python: display_path(&py),
            manage_web: false,
            start_on_launch: false,
            wake_listen: Some(false),
            ..Config::default()
        };
        let cfg_path = repo.join("cfg.json");
        let sup = Supervisor::new(cfg, cfg_path.clone(), repo.join("fallback"), Lang::En);
        let voice = Voice::new(sup.clone(), Lang::En);
        voice.spawn_monitor(|_| {});
        let logs_dir = repo.join("logs");

        // Выключено в настройках панели — WAKE_LISTEN=1 из .env не включает
        thread::sleep(Duration::from_millis(1500));
        assert_eq!(voice.view().phase, VoicePhase::Off);
        assert!(!voice.summary().enabled);

        // Включение: процесс, состояние из stdout, адрес ядра из .env
        voice.set_enabled(true).unwrap();
        let v = wait_for(&voice, 30, "слушает", |v| v.phase == VoicePhase::Running && v.status.is_some());
        let st = v.status.unwrap();
        assert!(st.running && st.names == ["коннор"] && st.last_heard == "коннор привет");
        assert!(v.pid.is_some() && v.enabled);
        assert_eq!(voice.summary(), VoiceSummary { enabled: true, listening: true });
        assert!(fs::read_to_string(&cfg_path).unwrap().contains("\"wakeListen\": true"));
        let text = read_logs(&logs_dir);
        assert!(text.contains(&format!("args --status --core http://127.0.0.1:{port}")), "{text}");
        assert!(!text.contains("WAKE_STATUS"), "строки состояния — не в лог: {text}");

        // Выключение: SIGTERM, слушатель закрывается сам
        voice.set_enabled(false).unwrap();
        wait_for(&voice, 20, "выключен", |v| v.phase == VoicePhase::Off && v.pid.is_none());
        assert!(read_logs(&logs_dir).contains("listener stopped"));
        assert!(voice.view().status.is_none());

        // Нет модулей: код 2 — причина видна, без перезапуска
        fs::write(repo.join("mode"), "setup").unwrap();
        voice.set_enabled(true).unwrap();
        let v = wait_for(&voice, 30, "нет модулей", |v| v.phase == VoicePhase::Failed);
        assert!(v.error.unwrap_or_default().contains("нет модулей"));
        assert_eq!(v.status.map(|s| s.issue), Some("modules".into()));
        assert!(voice.lock().blocked);
        thread::sleep(Duration::from_millis(RESTART_DELAY.as_millis() as u64 + 1500));
        assert_eq!(voice.view().phase, VoicePhase::Failed, "код 2 не перезапускается");

        // Падение — перезапуск через RESTART_DELAY; включение заново снимает запрет
        fs::write(repo.join("mode"), "crash").unwrap();
        voice.set_enabled(true).unwrap();
        wait_for(&voice, 30, "падение", |v| {
            v.phase == VoicePhase::Failed && v.error.as_deref().is_some_and(|e| e.contains("boom"))
        });
        fs::write(repo.join("mode"), "ok").unwrap();
        wait_for(&voice, 30, "перезапуск", |v| v.phase == VoicePhase::Running && v.status.is_some());
        assert_eq!(voice.lock().crashes.len(), 1);

        // Выход из панели: дождаться слушателя и больше не поднимать
        voice.stop_blocking();
        assert_eq!(voice.view().phase, VoicePhase::Off);
        thread::sleep(Duration::from_millis(1500));
        assert!(voice.view().pid.is_none(), "после выхода монитор не запускает слушатель снова");
        let _ = fs::remove_dir_all(&repo);
    }

    #[test]
    fn status_line() {
        let st = parse_status(
            r#"WAKE_STATUS {"running": true, "state": "idle", "persona": null, "held": false, "stage": "listening", "names": ["коннор"], "error": "", "issue": "", "last_heard": "коннор привет"}"#,
        )
        .unwrap();
        assert!(st.running && st.stage == "listening" && st.names == ["коннор"]);
        assert_eq!(st.last_heard, "коннор привет");
        assert!(parse_status("2026-10-14 12:00:00,000 [INFO] [Wake] Микрофон: MacBook").is_none());
        // Слушатель старее панели: лишние и недостающие поля — не ошибка
        let st = parse_status(r#"WAKE_STATUS {"running": false, "error": "нет модулей", "extra": 1}"#).unwrap();
        assert!(!st.running && st.error == "нет модулей" && st.stage.is_empty());
    }
}
