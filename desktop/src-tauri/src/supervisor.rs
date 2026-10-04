//! Бэкенд бота и веб-интерфейс (vite) как дочерние процессы панели: запуск,
//! мягкая остановка, перезапуск, падения. Процесс, запущенный вне панели
//! (в терминале), панель видит как «внешний» и тоже умеет остановить.

use crate::config::{self, display_path, ApiEnv, Config};
use crate::logs::{self, LogSink};
use crate::platform;
use crate::probe;
use crate::texts::{t, tf, Lang};
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// Сколько ждать первого ответа /api/health после запуска, прежде чем
/// показать «не отвечает» (импорт моделей и ChromaDB — десятки секунд)
const START_TIMEOUT: Duration = Duration::from_secs(120);
/// Мягкая остановка бэкенда: идущая генерация ответа держит uvicorn
const BACKEND_STOP_GRACE: Duration = Duration::from_secs(25);
const TERM_GRACE: Duration = Duration::from_secs(8);
/// Автоперезапуск: не больше CRASH_MAX падений за CRASH_WINDOW
const CRASH_WINDOW: Duration = Duration::from_secs(600);
const CRASH_MAX: usize = 3;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Stopped,
    Starting,
    Running,
    Stopping,
    Crashed,
    /// Работает, но запущен не панелью (например, в терминале)
    External,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Backend,
    Web,
}

struct Service {
    phase: Phase,
    child: Option<Child>,
    pid: Option<u32>,
    started_ms: Option<u64>,
    starting_since: Option<Instant>,
    responding: bool,
    error: Option<String>,
    stop_requested: bool,
    crashes: Vec<Instant>,
    log: Arc<Mutex<LogSink>>,
}

impl Service {
    fn new(log: LogSink) -> Self {
        Service {
            phase: Phase::Stopped,
            child: None,
            pid: None,
            started_ms: None,
            starting_since: None,
            responding: false,
            error: None,
            stop_requested: false,
            crashes: Vec::new(),
            log: Arc::new(Mutex::new(log)),
        }
    }

    fn clear_run(&mut self) {
        self.child = None;
        self.pid = None;
        self.started_ms = None;
        self.starting_since = None;
        self.responding = false;
    }
}

#[derive(Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct ServiceView {
    pub phase: Phase,
    pub ours: bool,
    pub pid: Option<u32>,
    pub uptime_sec: Option<u64>,
    pub responding: bool,
    pub error: Option<String>,
    pub port: u16,
    pub log_file: String,
}

/// То, что показывает значок в трее
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Summary {
    pub phase: Phase,
    pub responding: bool,
    pub busy: bool,
}

struct Inner {
    backend: Service,
    web: Service,
    /// Идёт операция (запуск/остановка/перезапуск)
    busy: bool,
    cfg: Config,
    api: ApiEnv,
    /// PATH из login-shell — один раз за жизнь панели
    path: Option<String>,
}

impl Inner {
    fn svc(&mut self, kind: Kind) -> &mut Service {
        match kind {
            Kind::Backend => &mut self.backend,
            Kind::Web => &mut self.web,
        }
    }

    fn port(&self, kind: Kind) -> u16 {
        match kind {
            Kind::Backend => self.api.port,
            Kind::Web => self.cfg.web_port,
        }
    }
}

fn lk<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn now_ms() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

#[derive(Clone)]
pub struct Supervisor {
    inner: Arc<Mutex<Inner>>,
    op: Arc<Mutex<()>>,
    config_path: PathBuf,
    fallback_logs: PathBuf,
    lang: Lang,
}

impl Supervisor {
    pub fn new(cfg: Config, config_path: PathBuf, fallback_logs: PathBuf, lang: Lang) -> Self {
        let repo = PathBuf::from(&cfg.repo_dir);
        let dir = config::logs_dir(&repo, &fallback_logs);
        logs::cleanup(&dir);
        let inner = Inner {
            backend: Service::new(LogSink::new(dir.clone(), "backend")),
            web: Service::new(LogSink::new(dir, "web")),
            busy: false,
            api: ApiEnv::read(&repo),
            cfg,
            path: None,
        };
        Supervisor {
            inner: Arc::new(Mutex::new(inner)),
            op: Arc::new(Mutex::new(())),
            config_path,
            fallback_logs,
            lang,
        }
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        lk(&self.inner)
    }

    pub fn config(&self) -> Config {
        self.lock().cfg.clone()
    }

    pub fn api(&self) -> ApiEnv {
        self.lock().api.clone()
    }

    pub fn config_path(&self) -> &Path {
        &self.config_path
    }

    pub fn logs_dir(&self) -> PathBuf {
        config::logs_dir(Path::new(&self.config().repo_dir), &self.fallback_logs)
    }

    pub fn set_config(&self, cfg: Config) -> Result<(), String> {
        config::save(&self.config_path, &cfg)?;
        let repo = PathBuf::from(&cfg.repo_dir);
        let dir = config::logs_dir(&repo, &self.fallback_logs);
        let mut g = self.lock();
        g.api = ApiEnv::read(&repo);
        lk(&g.backend.log).set_dir(dir.clone());
        lk(&g.web.log).set_dir(dir);
        g.cfg = cfg;
        Ok(())
    }

    pub fn summary(&self) -> Summary {
        let g = self.lock();
        Summary { phase: g.backend.phase, responding: g.backend.responding, busy: g.busy }
    }

    pub fn views(&self) -> (ServiceView, ServiceView, bool) {
        let g = self.lock();
        let view = |s: &Service, port: u16| ServiceView {
            phase: s.phase,
            ours: s.child.is_some(),
            pid: s.pid,
            uptime_sec: s.started_ms.map(|ms| now_ms().saturating_sub(ms) / 1000),
            responding: s.responding,
            error: s.error.clone(),
            port,
            log_file: display_path(&lk(&s.log).current_path()),
        };
        (view(&g.backend, g.api.port), view(&g.web, g.cfg.web_port), g.busy)
    }

    /// Последние строки вывода бэкенда, запущенного панелью (traceback падения)
    pub fn backend_tail(&self, n: usize) -> Vec<String> {
        let g = self.lock();
        let log = lk(&g.backend.log);
        let skip = log.tail.len().saturating_sub(n);
        log.tail.iter().skip(skip).cloned().collect()
    }

    // ── Операции: по одной за раз, в фоне — окно и трей не ждут ──

    pub fn start(&self) {
        self.run_op(|s| s.start_all());
    }

    pub fn stop(&self) {
        self.run_op(|s| s.stop_all());
    }

    pub fn restart(&self) {
        self.run_op(|s| {
            s.stop_all();
            s.start_all();
        });
    }

    /// Выход из панели: остановить то, что запустила она сама; бот,
    /// запущенный в терминале, продолжает работать
    pub fn stop_ours_blocking(&self) {
        let _op = lk(&self.op);
        self.lock().busy = true;
        self.stop_kind(Kind::Web, true);
        self.stop_kind(Kind::Backend, true);
        self.lock().busy = false;
    }

    fn run_op<F: FnOnce(&Supervisor) + Send + 'static>(&self, f: F) {
        let s = self.clone();
        thread::spawn(move || {
            let _op = lk(&s.op);
            s.lock().busy = true;
            f(&s);
            s.lock().busy = false;
        });
    }

    fn start_all(&self) {
        self.start_kind(Kind::Backend);
        if self.config().manage_web {
            self.start_kind(Kind::Web);
        }
    }

    fn stop_all(&self) {
        if self.config().manage_web {
            self.stop_kind(Kind::Web, false);
        }
        self.stop_kind(Kind::Backend, false);
    }

    fn user_path(&self) -> String {
        if let Some(p) = self.lock().path.clone() {
            return p;
        }
        let p = platform::user_path();
        self.lock().path = Some(p.clone());
        p
    }

    /// Python из настроек или найденный автоматически (найденный
    /// запоминается: поиск с проверкой импорта небыстрый, а путь виден в настройках)
    fn resolve_python(&self, repo: &Path) -> Result<PathBuf, String> {
        let configured = self.config().python.trim().to_string();
        if !configured.is_empty() {
            let p = PathBuf::from(&configured);
            return if p.is_file() { Ok(p) } else { Err(tf(self.lang, "err_python_path", configured)) };
        }
        let found = platform::find_python(repo, &self.user_path()).ok_or_else(|| t(self.lang, "err_python"))?;
        let mut cfg = self.config();
        cfg.python = display_path(&found);
        let _ = self.set_config(cfg);
        Ok(found)
    }

    /// Кнопка «найти» в настройках: только поиск, без сохранения
    pub fn detect_python(&self, repo: &Path) -> Result<String, String> {
        platform::find_python(repo, &self.user_path())
            .map(|p| display_path(&p))
            .ok_or_else(|| t(self.lang, "err_python"))
    }

    fn start_kind(&self, kind: Kind) {
        {
            let mut g = self.lock();
            let s = g.svc(kind);
            if s.child.is_some() {
                return;
            }
            s.error = None;
        }
        match kind {
            Kind::Backend => self.start_backend(),
            Kind::Web => self.start_web(),
        }
    }

    fn start_backend(&self) {
        let lang = self.lang;
        let (cfg, api) = (self.config(), self.api());
        if probe::health(&api) {
            return self.set_external(Kind::Backend);
        }
        if probe::port_open(api.port) {
            return self.refuse(Kind::Backend, tf(lang, "err_port", api.port));
        }
        let repo = PathBuf::from(&cfg.repo_dir);
        if !config::repo_ok(&repo) {
            return self.refuse(Kind::Backend, tf(lang, "err_repo", &cfg.repo_dir));
        }
        let python = match self.resolve_python(&repo) {
            Ok(p) => p,
            Err(e) => return self.refuse(Kind::Backend, e),
        };
        let mut cmd = Command::new(&python);
        cmd.args(["-m", "app.main", "api"])
            .current_dir(&repo)
            .env("PATH", self.user_path())
            .env("PYTHONUNBUFFERED", "1")
            // Вывод в трубу на Windows иначе в cp1251/cp866 — русские строки
            // лога падали бы с UnicodeEncodeError
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8");
        let label = format!("{} -m app.main api", display_path(&python));
        self.spawn(Kind::Backend, cmd, &label);
    }

    fn start_web(&self) {
        let lang = self.lang;
        let cfg = self.config();
        if probe::port_open(cfg.web_port) {
            return self.set_external(Kind::Web);
        }
        let web = PathBuf::from(&cfg.repo_dir).join("web");
        let vite = web.join("node_modules").join("vite").join("bin").join("vite.js");
        if !vite.is_file() {
            return self.refuse(Kind::Web, t(lang, "err_vite"));
        }
        let path = self.user_path();
        let node = match cfg.node.trim() {
            "" => platform::find_node(&path),
            n => Some(PathBuf::from(n)).filter(|p| p.is_file()),
        };
        let Some(node) = node else { return self.refuse(Kind::Web, t(lang, "err_node")) };
        // node напрямую, без npm: на Windows npm — это .cmd, и останавливать
        // пришлось бы ещё и оболочку
        let mut cmd = Command::new(&node);
        cmd.arg(&vite)
            .args(["--port", &cfg.web_port.to_string(), "--strictPort"])
            .current_dir(&web)
            .env("PATH", path)
            .env("NO_COLOR", "1")
            .env("BROWSER", "none");
        self.spawn(Kind::Web, cmd, &format!("vite :{}", cfg.web_port));
    }

    fn spawn(&self, kind: Kind, mut cmd: Command, label: &str) {
        cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        platform::detach(&mut cmd);
        let log = lk(&self.inner).svc(kind).log.clone();
        match cmd.spawn() {
            Ok(mut child) => {
                let pid = child.id();
                lk(&log).marker(&format!("{} (pid {pid}): {label}", t(self.lang, "log_start")));
                logs::pump(child.stdout.take(), log.clone());
                logs::pump(child.stderr.take(), log);
                let mut g = self.lock();
                let s = g.svc(kind);
                s.child = Some(child);
                s.pid = Some(pid);
                s.phase = Phase::Starting;
                s.started_ms = Some(now_ms());
                s.starting_since = Some(Instant::now());
                s.stop_requested = false;
                s.responding = false;
                s.error = None;
            }
            Err(e) => self.refuse(kind, tf(self.lang, "err_spawn", e)),
        }
    }

    fn set_external(&self, kind: Kind) {
        let mut g = self.lock();
        let s = g.svc(kind);
        s.phase = Phase::External;
        s.responding = true;
        s.error = None;
    }

    /// Запуск не состоялся (порт занят, нет Python…) — процесса нет, есть причина
    fn refuse(&self, kind: Kind, msg: String) {
        let mut g = self.lock();
        let s = g.svc(kind);
        s.phase = Phase::Stopped;
        s.error = Some(msg);
    }

    /// Остановить сервис. only_ours — только запущенный панелью (выход из
    /// панели); иначе и внешний: его PID берётся по слушаемому порту
    fn stop_kind(&self, kind: Kind, only_ours: bool) {
        let lang = self.lang;
        let (ours, pid, port, api) = {
            let mut g = self.lock();
            let port = g.port(kind);
            let api = g.api.clone();
            let s = g.svc(kind);
            (s.child.is_some(), s.pid, port, api)
        };
        if !ours && (only_ours || !probe::port_open(port)) {
            let mut g = self.lock();
            let s = g.svc(kind);
            if s.phase == Phase::External {
                s.phase = Phase::Stopped;
            }
            return;
        }
        let external_pids = if ours { Vec::new() } else { platform::pids_listening(port) };
        {
            let mut g = self.lock();
            let s = g.svc(kind);
            s.phase = Phase::Stopping;
            s.stop_requested = true;
        }
        let mut gone = false;
        // Бэкенд — сначала штатно, как Ctrl+C: uvicorn гасит сервер и браузер бота
        if kind == Kind::Backend && probe::health(&api) && probe::api_post(&api, "/api/system/shutdown", 3000).is_ok()
        {
            gone = self.wait_gone(kind, port, &external_pids, BACKEND_STOP_GRACE);
        }
        if !gone {
            // Свой процесс — группой (unix) / деревом (Windows); чужой — только
            // он сам: его группа — это, например, терминал пользователя
            let targets: Vec<(u32, bool)> = match pid {
                Some(p) if ours => vec![(p, true)],
                _ => external_pids.iter().map(|p| (*p, false)).collect(),
            };
            for (p, group) in &targets {
                // Python-бэкенду SIGTERM — только ему: он сам закроет Chrome бота
                platform::terminate(*p, *group && kind == Kind::Web);
            }
            gone = self.wait_gone(kind, port, &external_pids, TERM_GRACE);
            if !gone {
                for (p, group) in &targets {
                    platform::kill(*p, *group);
                }
                gone = self.wait_gone(kind, port, &external_pids, Duration::from_secs(3));
            }
        }
        let log = {
            let mut g = self.lock();
            let s = g.svc(kind);
            if gone {
                if let Some(mut c) = s.child.take() {
                    let _ = c.wait();
                }
                s.clear_run();
                s.phase = Phase::Stopped;
                s.error = None;
                Some(s.log.clone())
            } else {
                s.phase = if ours { Phase::Running } else { Phase::External };
                s.stop_requested = false;
                s.error = Some(t(lang, "err_stop"));
                None
            }
        };
        if let Some(log) = log {
            lk(&log).marker(&t(lang, "log_stop"));
        }
    }

    fn wait_gone(&self, kind: Kind, port: u16, external_pids: &[u32], timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        loop {
            let ours_state = {
                let mut g = self.lock();
                let s = g.svc(kind);
                // None — процесса панели нет (внешний или уже подобран монитором)
                s.child.as_mut().map(|c| matches!(c.try_wait(), Ok(Some(_))))
            };
            let gone = match ours_state {
                Some(exited) => exited,
                None => !probe::port_open(port) && external_pids.iter().all(|p| !platform::pid_alive(*p)),
            };
            if gone {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            thread::sleep(Duration::from_millis(250));
        }
    }

    // ── Монитор: падения, ответы, внешние процессы ──

    pub fn spawn_monitor<F: Fn(Summary) + Send + 'static>(&self, notify: F) {
        let s = self.clone();
        thread::spawn(move || {
            let mut last: Option<Summary> = None;
            let mut tick: u64 = 0;
            loop {
                s.reap();
                let fast = {
                    let g = s.lock();
                    g.busy || g.backend.phase == Phase::Starting || g.web.phase == Phase::Starting
                };
                if fast || tick % 3 == 0 {
                    s.probe();
                }
                let sum = s.summary();
                if last != Some(sum) {
                    notify(sum);
                    last = Some(sum);
                }
                tick += 1;
                thread::sleep(Duration::from_secs(1));
            }
        });
    }

    fn reap(&self) {
        let lang = self.lang;
        let mut restart = Vec::new();
        {
            let mut g = self.lock();
            let auto = g.cfg.auto_restart;
            for kind in [Kind::Backend, Kind::Web] {
                let s = g.svc(kind);
                let Some(child) = s.child.as_mut() else { continue };
                let Ok(Some(status)) = child.try_wait() else { continue };
                s.clear_run();
                if s.stop_requested {
                    s.phase = Phase::Stopped;
                    continue;
                }
                let how = exit_text(lang, &status);
                let last_line = lk(&s.log).tail.back().cloned();
                lk(&s.log).marker(&format!("{}: {how}", t(lang, "log_exit")));
                let mut err = tf(lang, "err_exit", &how);
                if let Some(l) = last_line {
                    err = format!("{err} — {}", shorten(&l, 160));
                }
                s.phase = Phase::Crashed;
                if auto {
                    s.crashes.retain(|t| t.elapsed() < CRASH_WINDOW);
                    if s.crashes.len() < CRASH_MAX {
                        s.crashes.push(Instant::now());
                        restart.push(kind);
                    } else {
                        err = format!("{} {err}", t(lang, "err_restart_limit"));
                    }
                }
                s.error = Some(err);
            }
        }
        for kind in restart {
            let me = self.clone();
            thread::spawn(move || {
                thread::sleep(Duration::from_secs(3));
                me.run_op(move |s| {
                    // Пока ждали, пользователь мог запустить/остановить сам
                    if s.lock().svc(kind).phase == Phase::Crashed {
                        s.start_kind(kind);
                    }
                });
            });
        }
    }

    fn probe(&self) {
        let (api, web_port) = {
            let g = self.lock();
            (g.api.clone(), g.cfg.web_port)
        };
        let backend_up = probe::health(&api);
        let web_up = probe::port_open(web_port);
        let lang = self.lang;
        let mut g = self.lock();
        let busy = g.busy;
        apply_probe(&mut g.backend, backend_up, busy, lang);
        apply_probe(&mut g.web, web_up, busy, lang);
    }
}

fn apply_probe(s: &mut Service, up: bool, busy: bool, lang: Lang) {
    let ours = s.child.is_some();
    s.responding = up;
    match s.phase {
        Phase::Starting if up => {
            s.phase = Phase::Running;
            s.starting_since = None;
            s.error = None;
        }
        Phase::Starting => {
            if let Some(since) = s.starting_since.filter(|t| t.elapsed() > START_TIMEOUT) {
                s.error = Some(tf(lang, "err_no_answer", since.elapsed().as_secs()));
            }
        }
        Phase::Stopped | Phase::Crashed if up && !ours && !busy => {
            s.phase = Phase::External;
            s.error = None;
        }
        Phase::External if !up && !busy => s.phase = Phase::Stopped,
        _ => {}
    }
}

fn exit_text(lang: Lang, st: &ExitStatus) -> String {
    if let Some(c) = st.code() {
        return tf(lang, "err_code", c);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(sig) = st.signal() {
            return tf(lang, "err_signal", sig);
        }
    }
    st.to_string()
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
    //! Супервизор на поддельном бэкенде (stdlib-сервер вместо бота): запуск,
    //! мягкая остановка через /api/system/shutdown, падение и автоперезапуск,
    //! бэкенд «из терминала», перезапуск. Настоящий бот не трогается.

    use super::*;
    use std::fs;
    use std::net::TcpListener;

    const FAKE_MAIN: &str = r#"
import http.server, json, os, sys, threading, time
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
port = int([l.split("=", 1)[1] for l in open(os.path.join(root, ".env")) if l.startswith("API_PORT=")][0])
flag = os.path.join(root, "crash_once")
if os.path.exists(flag):
    os.remove(flag)
    print("RuntimeError: boom", flush=True)
    sys.exit(3)

class H(http.server.BaseHTTPRequestHandler):
    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path.startswith("/api/health"):
            return self._json({"status": "ok"})
        self.send_response(404); self.end_headers()
    def do_POST(self):
        if self.path == "/api/system/shutdown":
            self._json({"ok": True})
            threading.Thread(target=lambda: (time.sleep(0.2), srv.shutdown())).start()
            return
        self.send_response(404); self.end_headers()
    def log_message(self, *a):
        pass

srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
print("fake backend up", flush=True)
srv.serve_forever()
print("fake backend stopped", flush=True)
"#;

    fn python() -> Option<PathBuf> {
        let path = std::env::var("PATH").unwrap_or_default();
        std::env::split_paths(&path)
            .map(|d| d.join(if cfg!(windows) { "python.exe" } else { "python3" }))
            .find(|p| p.is_file())
    }

    fn wait_for(sup: &Supervisor, secs: u64, what: &str, f: impl Fn(&ServiceView) -> bool) -> ServiceView {
        let deadline = Instant::now() + Duration::from_secs(secs);
        loop {
            let v = sup.views().0;
            if f(&v) {
                return v;
            }
            assert!(Instant::now() < deadline, "не дождались: {what}; сейчас {:?} {:?}", v.phase, v.error);
            thread::sleep(Duration::from_millis(100));
        }
    }

    fn read_logs(dir: &Path) -> String {
        fs::read_dir(dir)
            .map(|rd| rd.flatten().filter_map(|e| fs::read_to_string(e.path()).ok()).collect())
            .unwrap_or_default()
    }

    #[test]
    fn lifecycle_on_fake_backend() {
        let Some(py) = python() else {
            eprintln!("нет python3 в PATH — тест пропущен");
            return;
        };
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let repo = std::env::temp_dir().join(format!("vpc-panel-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&repo);
        fs::create_dir_all(repo.join("app")).unwrap();
        fs::write(repo.join("app").join("__init__.py"), "").unwrap();
        fs::write(repo.join("app").join("main.py"), FAKE_MAIN).unwrap();
        fs::write(repo.join(".env"), format!("API_PORT={port}\n")).unwrap();
        let cfg = Config {
            repo_dir: display_path(&repo),
            python: display_path(&py),
            manage_web: false,
            start_on_launch: false,
            auto_restart: true,
            ..Config::default()
        };
        let sup = Supervisor::new(cfg, repo.join("cfg.json"), repo.join("fallback"), Lang::En);
        sup.spawn_monitor(|_| {});
        let logs_dir = repo.join("logs");

        // Запуск и мягкая остановка
        sup.start();
        let v = wait_for(&sup, 20, "запуск", |v| v.phase == Phase::Running);
        assert!(v.ours && v.pid.is_some());
        sup.stop();
        wait_for(&sup, 30, "остановка", |v| v.phase == Phase::Stopped && !sup.summary().busy);
        assert!(!probe::port_open(port));
        let text = read_logs(&logs_dir);
        assert!(text.contains("fake backend up"), "{text}");
        assert!(text.contains("fake backend stopped"), "остановка не через /api/system/shutdown: {text}");
        assert!(text.contains("stopped by the panel"), "{text}");

        // Падение → Crashed с причиной → автоперезапуск
        fs::write(repo.join("crash_once"), "").unwrap();
        sup.start();
        let v = wait_for(&sup, 20, "падение", |v| v.phase == Phase::Crashed);
        let err = v.error.unwrap_or_default();
        assert!(err.contains("code 3") && err.contains("boom"), "{err}");
        let v = wait_for(&sup, 20, "автоперезапуск", |v| v.phase == Phase::Running);
        let first_pid = v.pid;

        // Перезапуск — новый процесс
        sup.restart();
        let v = wait_for(&sup, 40, "перезапуск", |v| {
            v.phase == Phase::Running && v.pid.is_some() && v.pid != first_pid
        });
        assert!(v.ours);
        sup.stop();
        wait_for(&sup, 30, "остановка", |v| v.phase == Phase::Stopped && !sup.summary().busy);

        // Бэкенд «из терминала»: панель видит его внешним и останавливает
        let mut ext = Command::new(&py);
        ext.args(["-m", "app.main", "api"]).current_dir(&repo).stdout(Stdio::null()).stderr(Stdio::null());
        let mut child = ext.spawn().unwrap();
        let ext_pid = child.id();
        // Подбирать своего ребёнка, иначе он зомби — «живой» для kill(pid, 0)
        let reaper = thread::spawn(move || child.wait());
        let v = wait_for(&sup, 20, "внешний", |v| v.phase == Phase::External);
        assert!(!v.ours);
        sup.stop();
        wait_for(&sup, 30, "остановка внешнего", |v| v.phase == Phase::Stopped && !sup.summary().busy);
        assert!(reaper.join().unwrap().is_ok());
        assert!(!platform::pid_alive(ext_pid));

        // Порт занят не ботом — запуск отказывает с причиной
        let blocker = TcpListener::bind(("127.0.0.1", port)).unwrap();
        sup.start();
        let v = wait_for(&sup, 10, "отказ", |v| v.error.is_some() && !sup.summary().busy);
        assert_eq!(v.phase, Phase::Stopped);
        assert!(v.error.unwrap().contains(&port.to_string()));
        drop(blocker);

        let _ = fs::remove_dir_all(&repo);
    }
}
