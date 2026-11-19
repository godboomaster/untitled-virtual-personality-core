//! Голосовая активация («Коннор, …») — в панели, а не в ядре: слушатель
//! имени — дочерний процесс панели (python -m app.voice.wake_listener
//! --status). Микрофон принадлежит панели — macOS спрашивает доступ у
//! «Virtual Persona»; слушание включается в трее, в окне панели и кнопкой
//! микрофона у портрета (одна настройка wakeListen) и живёт, пока живёт
//! панель, ядро нужно только для ответа. Состояние слушатель печатает строками
//! «WAKE_STATUS {json}»; ядру, запущенному панелью, — WAKE_LISTEN=0
//! (supervisor.rs), а ядро из терминала само уступает микрофон, пока
//! слушатель панели сообщает ему о себе.
//!
//! Разговор у портрета (--portrait): прозвучало имя, а чат персоны в вебе на
//! этом компьютере не открыт — в состоянии появляется talk (персона, фаза,
//! что вы говорите); панель зовёт портрет на рабочий стол (set_talk_hook →
//! portrait::on_voice). В разговоре можно назвать другую персону — встаёт и
//! её портрет (talk.personas). Команды слушателю — строками в stdin:
//! «end» — разговор кончился (меню портрета, end_talk), «leave <id>» —
//! портрет персоны убрали, разговор дальше без неё (leave_talk);
//! «calibrate» и «forget_voice» — запомнить и забыть громкость вашего
//! голоса для фильтра фона (status.near).

use crate::config::{self, display_path};
use crate::logs::{self, LogSink};
use crate::platform;
use crate::supervisor::Supervisor;
use crate::texts::{t, tf, Lang};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::io::Write;
use std::process::{Child, ChildStdin, Command, Stdio};
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
    /// Идёт разговор у портрета; None — нет
    pub talk: Option<WakeTalk>,
    /// Фильтр фона; None — слушатель старее панели
    pub near: Option<NearStatus>,
    /// Языки распознавания, которые слушает (ru, en); пусто — слушатель
    /// старее панели. lang — язык разговора сейчас
    pub langs: Vec<String>,
    pub lang: String,
    /// Модель одного из языков не загрузилась (слушает без неё) — почему
    #[serde(alias = "lang_error")]
    pub lang_error: String,
}

/// Фильтр фона «кто ближе к микрофону» — status()["near"] слушателя
/// (app/voice/near_gate.py): речь тише вашего голоса больше чем на margin дБ
/// распознаватель не слышит
#[derive(Clone, Debug, Default, PartialEq, Deserialize, Serialize)]
#[serde(default, rename_all = "camelCase")]
pub struct NearStatus {
    pub on: bool,
    /// Громкость вашего голоса, дБ; None — ещё не знает
    pub level: Option<f64>,
    /// panel — записали кнопкой, name — по фразе с именем
    pub source: String,
    pub margin: f64,
    /// Идёт «Запомнить мой голос» и сколько секунд осталось
    pub calibrating: bool,
    #[serde(alias = "calib_left")]
    pub calib_left: f64,
    /// Чем кончилась запись: ok, no_speech
    #[serde(alias = "calib_result")]
    pub calib_result: String,
}

/// Разговор у портрета — status()["talk"] слушателя: имя прозвучало, а чат
/// персоны в вебе на этом компьютере не открыт
#[derive(Clone, Debug, Default, PartialEq, Deserialize, Serialize)]
#[serde(default)]
pub struct WakeTalk {
    /// Кто слушает сейчас (назвали последней)
    pub persona: String,
    /// listening — ждёт фразу, hearing — слышит (text — что распознано на
    /// сейчас), thinking — ждёт ответ (sent — что ушло персоне), speaking — говорит
    pub phase: String,
    pub text: String,
    pub sent: String,
    /// Все, кого назвали в этом разговоре, — их портреты на столе
    pub personas: Vec<String>,
    /// Сколько раз персону перебили (растёт — её реплику на портрете
    /// оборвать) и кого последней
    pub cut: u64,
    pub cut_persona: String,
}

/// Кому сказать, что разговор у портрета начался, изменился или кончился
pub type TalkHook = Arc<dyn Fn(Option<WakeTalk>) + Send + Sync>;
/// Кому сказать, что состояние слушателя изменилось (кнопка микрофона у портрета)
pub type ViewHook = Arc<dyn Fn(VoiceView) + Send + Sync>;

#[derive(Serialize, Clone, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct VoiceView {
    pub enabled: bool,
    /// Выбор языков в панели: «ru,en», «ru» или «en»
    pub lang: String,
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
    /// stdin слушателя — команды панели (end)
    stdin: Option<ChildStdin>,
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
    /// Разговор у портрета, о котором hook уже знает
    talk: Option<WakeTalk>,
    talk_hook: Option<TalkHook>,
    view_hook: Option<ViewHook>,
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
            stdin: None,
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
            talk: None,
            talk_hook: None,
            view_hook: None,
        };
        Voice { inner: Arc::new(Mutex::new(inner)), op: Arc::new(Mutex::new(())), sup, lang }
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        lk(&self.inner)
    }

    pub fn view(&self) -> VoiceView {
        let enabled = self.sup.wake_enabled();
        let lang = self.sup.wake_lang().to_string();
        let g = self.lock();
        let log_file = display_path(&lk(&g.log).current_path());
        VoiceView { enabled, lang, phase: g.phase, pid: g.pid, status: g.status.clone(), error: g.error.clone(), log_file }
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

    /// Языки распознавания (окно панели). Модели слушатель грузит при
    /// запуске — работает, значит, перезапустить
    pub fn set_lang(&self, lang: &str) -> Result<(), String> {
        if self.sup.wake_lang() == crate::config::wake_lang_choice(lang) && self.sup.config().wake_lang.is_some() {
            return Ok(());
        }
        self.sup.set_wake_lang(lang)?;
        let me = self.clone();
        thread::spawn(move || {
            {
                let _op = lk(&me.op);
                if me.lock().child.is_some() {
                    me.stop_child();
                }
            }
            me.reconcile();
        });
        Ok(())
    }

    /// Разговор у портрета: начался, изменился (что вы говорите) или кончился
    pub fn set_talk_hook(&self, hook: TalkHook) {
        self.lock().talk_hook = Some(hook);
    }

    /// Состояние слушателя изменилось — монитор, не чаще раза в секунду
    pub fn set_view_hook(&self, hook: ViewHook) {
        self.lock().view_hook = Some(hook);
    }

    /// Закончить разговор у портрета (портрет убрали, меню): слушателю — «end»
    pub fn end_talk(&self) {
        self.command("end");
    }

    /// Портрет персоны убрали — разговор дальше без неё
    pub fn leave_talk(&self, persona: &str) {
        self.command(&format!("leave {persona}"));
    }

    /// «Запомнить мой голос»: следующие секунды слушатель меряет громкость
    /// вашей речи (фильтр фона)
    pub fn calibrate(&self) {
        self.command("calibrate");
    }

    /// Забыть громкость вашего голоса у этого микрофона
    pub fn forget_voice(&self) {
        self.command("forget_voice");
    }

    /// Команда слушателю строкой в stdin
    fn command(&self, line: &str) {
        let mut g = self.lock();
        if let Some(stdin) = g.stdin.as_mut() {
            if writeln!(stdin, "{line}").and_then(|_| stdin.flush()).is_err() {
                g.stdin = None;
            }
        }
    }

    /// Слушателя нет (вышел, остановлен) — и разговора нет
    fn talk_gone(&self) {
        let hook = {
            let mut g = self.lock();
            if g.talk.take().is_none() {
                return;
            }
            g.talk_hook.clone()
        };
        if let Some(h) = hook {
            h(None);
        }
    }

    /// Раз в секунду: подобрать вышедший процесс, привести к включено/выключено;
    /// трею — сводка, view_hook — всё состояние, когда изменились
    pub fn spawn_monitor<F: Fn(VoiceSummary) + Send + 'static>(&self, notify: F) {
        let me = self.clone();
        thread::spawn(move || {
            let mut last: Option<VoiceSummary> = None;
            let mut last_view: Option<VoiceView> = None;
            loop {
                me.reconcile();
                let sum = me.summary();
                if last != Some(sum) {
                    notify(sum);
                    last = Some(sum);
                }
                let view = me.view();
                if last_view.as_ref() != Some(&view) {
                    let hook = me.lock().view_hook.clone();
                    if let Some(h) = hook {
                        h(view.clone());
                    }
                    last_view = Some(view);
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
        if self.lock().child.is_none() {
            self.talk_gone();
        }
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
        // Выбор в панели; не выбран — слушатель возьмёт WAKE_LANG из .env сам
        if let Some(l) = cfg.wake_lang.as_deref() {
            cmd.env("WAKE_LANG", crate::config::wake_lang_choice(l));
        }
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
                g.stdin = child.stdin.take();
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
    /// не вышел — вся его группа (say, afplay) жёстко. Разговор у портрета
    /// кончается сразу: микрофон выключают, ждать закрытия незачем
    fn stop_child(&self) {
        let pid = {
            let mut g = self.lock();
            g.stop_requested = true;
            g.phase = VoicePhase::Stopping;
            g.pid
        };
        self.talk_gone();
        let Some(pid) = pid else { return };
        self.lock().stdin = None;
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
        drop(g);
        self.talk_gone();
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
    // --portrait — разговор у портрета: его рисует панель (portrait.rs)
    cmd.args(["-m", "app.voice.wake_listener", "--status", "--core", core, "--portrait"])
        .current_dir(repo)
        .env("PYTHONUNBUFFERED", "1")
        // Вывод в трубу на Windows иначе в cp1251 — русские строки падали бы
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    cmd
}

/// Строка stdout слушателя: состояние — себе, остальное — в лог. Разговор у
/// портрета изменился — hook (вне замка: он зовёт окна). Слушателя
/// останавливают — его последние строки состояния мимо: разговор уже кончился
fn on_stdout(inner: &Arc<Mutex<Inner>>, run: u64, line: &str) {
    let mut g = lk(inner);
    match parse_status(line) {
        Some(st) if g.run == run && !g.stop_requested => {
            if g.phase == VoicePhase::Starting {
                g.phase = VoicePhase::Running;
            }
            let talk = st.talk.clone();
            g.status = Some(st);
            if g.talk != talk {
                g.talk = talk.clone();
                let hook = g.talk_hook.clone();
                drop(g);
                if let Some(h) = hook {
                    h(talk);
                }
            }
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
    //! выключение посреди разговора, «нет модулей» без перезапуска, падение
    //! и перезапуск. Микрофон не нужен.

    use super::*;
    use crate::config::Config;
    use std::fs;
    use std::net::TcpListener;

    const FAKE_LISTENER: &str = r#"
import json, os, signal, sys, threading, time
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
print("lang " + os.environ.get("WAKE_LANG", "-"), flush=True)
st()
if mode in ("ok", "slow"):
    # Разговор у портрета: имя прозвучало; «end» в stdin — разговор кончился
    st(state="await", persona="connor", talk={"persona": "connor", "phase": "hearing", "text": "коннор привет", "sent": "",
                                              "personas": ["don", "connor"], "cut": 1, "cut_persona": "don"})
    def commands():
        for line in sys.stdin:
            print("stdin " + line.strip(), flush=True)
            if line.strip() == "leave don":
                st(state="await", persona="connor", talk={"persona": "connor", "phase": "listening", "text": "", "sent": "",
                                                          "personas": ["connor"], "cut": 1, "cut_persona": "don"})
            if line.strip() == "end":
                st()
    threading.Thread(target=commands, daemon=True).start()
if mode == "crash":
    print("RuntimeError: boom", flush=True)
    sys.exit(1)
stop = []
signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
while not stop:
    time.sleep(0.05)
if mode == "slow":
    # Микрофон закрывается долго, и напоследок — состояние со старым разговором
    st(state="await", persona="connor", talk={"persona": "connor", "phase": "hearing", "text": "коннор", "sent": "",
                                              "personas": ["connor"], "cut": 0, "cut_persona": ""})
    time.sleep(2)
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
        let talks: Arc<Mutex<Vec<Option<WakeTalk>>>> = Arc::new(Mutex::new(Vec::new()));
        let t2 = talks.clone();
        voice.set_talk_hook(Arc::new(move |t| lk(&t2).push(t)));
        let views: Arc<Mutex<Vec<VoiceView>>> = Arc::new(Mutex::new(Vec::new()));
        let v2 = views.clone();
        voice.set_view_hook(Arc::new(move |v| lk(&v2).push(v)));
        voice.spawn_monitor(|_| {});
        let logs_dir = repo.join("logs");

        // Выключено в настройках панели — WAKE_LISTEN=1 из .env не включает
        thread::sleep(Duration::from_millis(1500));
        assert_eq!(voice.view().phase, VoicePhase::Off);
        assert!(!voice.summary().enabled);

        // Включение: процесс, состояние из stdout, адрес ядра из .env
        voice.set_enabled(true).unwrap();
        let v = wait_for(&voice, 30, "разговор", |v| {
            v.phase == VoicePhase::Running && v.status.as_ref().is_some_and(|s| s.talk.is_some())
        });
        let st = v.status.unwrap();
        assert!(st.running && st.names == ["коннор"] && st.last_heard == "коннор привет");
        let talk = lk(&talks).last().cloned().flatten().expect("hook узнал о разговоре");
        assert_eq!((talk.persona.as_str(), talk.phase.as_str(), talk.text.as_str()), ("connor", "hearing", "коннор привет"));
        assert_eq!((talk.personas, talk.cut, talk.cut_persona.as_str()), (vec!["don".to_string(), "connor".to_string()], 1, "don"));

        // Портрет одной из персон убрали: «leave don» — разговор дальше без неё
        voice.leave_talk("don");
        wait_for(&voice, 10, "без don", |v| {
            v.status.as_ref().and_then(|s| s.talk.as_ref()).is_some_and(|t| t.personas == ["connor"])
        });
        assert!(read_logs(&logs_dir).contains("stdin leave don"));

        // «Запомнить мой голос» и «Забыть» — командами слушателю
        voice.calibrate();
        voice.forget_voice();
        let deadline = Instant::now() + Duration::from_secs(5);
        while !read_logs(&logs_dir).contains("stdin forget_voice") {
            assert!(Instant::now() < deadline, "calibrate/forget_voice не дошли до слушателя");
            thread::sleep(Duration::from_millis(100));
        }
        assert!(read_logs(&logs_dir).contains("stdin calibrate"));

        // Портрет убрали: «end» слушателю в stdin, разговор кончился — hook с None
        voice.end_talk();
        wait_for(&voice, 10, "конец разговора", |v| v.status.as_ref().is_some_and(|s| s.talk.is_none()));
        assert_eq!(lk(&talks).last().cloned(), Some(None));
        assert!(read_logs(&logs_dir).contains("stdin end"));
        assert!(v.pid.is_some() && v.enabled);
        assert_eq!(voice.summary(), VoiceSummary { enabled: true, listening: true });
        assert!(fs::read_to_string(&cfg_path).unwrap().contains("\"wakeListen\": true"));
        let text = read_logs(&logs_dir);
        assert!(text.contains(&format!("args --status --core http://127.0.0.1:{port} --portrait")), "{text}");
        assert!(!text.contains("WAKE_STATUS"), "строки состояния — не в лог: {text}");
        assert!(text.contains("lang -"), "язык не выбран — слушатель берёт WAKE_LANG из .env: {text}");
        assert_eq!(voice.view().lang, "ru,en");

        // Язык выбрали в панели — слушатель перезапущен с WAKE_LANG
        let pid = v.pid;
        voice.set_lang("en").unwrap();
        let v = wait_for(&voice, 30, "перезапуск с английским", |v| {
            v.phase == VoicePhase::Running && v.pid.is_some() && v.pid != pid && v.status.is_some()
        });
        assert!(read_logs(&logs_dir).contains("lang en"));
        assert_eq!(v.lang, "en");
        assert!(fs::read_to_string(&cfg_path).unwrap().contains("\"wakeLang\": \"en\""));
        let pid = v.pid;
        voice.set_lang("ru,en").unwrap();
        wait_for(&voice, 30, "перезапуск с двумя", |v| {
            v.lang == "ru,en" && v.pid.is_some() && v.pid != pid && v.status.as_ref().is_some_and(|s| s.talk.is_some())
        });
        assert!(read_logs(&logs_dir).contains("lang ru,en"));
        voice.end_talk();
        wait_for(&voice, 10, "конец разговора (2)", |v| v.status.as_ref().is_some_and(|s| s.talk.is_none()));

        // Кнопке микрофона у портрета — состояние от монитора (раз в секунду): слушает
        let wait_seen = |what: &str, f: &dyn Fn(&VoiceView) -> bool| {
            let deadline = Instant::now() + Duration::from_secs(3);
            while !lk(&views).iter().any(f) {
                assert!(Instant::now() < deadline, "монитор не сказал портретам: {what}");
                thread::sleep(Duration::from_millis(100));
            }
        };
        wait_seen("слушает", &|v| v.enabled && v.phase == VoicePhase::Running && v.status.as_ref().is_some_and(|s| s.running));

        // Выключение: SIGTERM, слушатель закрывается сам
        voice.set_enabled(false).unwrap();
        wait_for(&voice, 20, "выключен", |v| v.phase == VoicePhase::Off && v.pid.is_none());
        assert!(read_logs(&logs_dir).contains("listener stopped"));
        assert!(voice.view().status.is_none());
        wait_seen("выключен", &|v| !v.enabled && v.phase == VoicePhase::Off);

        // Выключили посреди разговора (кнопка у портрета): разговор кончается
        // сразу, а не когда слушатель закроет микрофон; его последняя строка
        // со старым разговором разговор не возвращает
        fs::write(repo.join("mode"), "slow").unwrap();
        voice.set_enabled(true).unwrap();
        wait_for(&voice, 30, "разговор (slow)", |v| v.status.as_ref().is_some_and(|s| s.talk.is_some()));
        assert!(lk(&talks).last().cloned().flatten().is_some());
        let t0 = Instant::now();
        voice.set_enabled(false).unwrap();
        while lk(&talks).last().cloned() != Some(None) {
            assert!(t0.elapsed() < Duration::from_millis(1500), "разговор не кончился сразу");
            thread::sleep(Duration::from_millis(20));
        }
        let ended = t0.elapsed();
        wait_for(&voice, 20, "выключен (slow)", |v| v.phase == VoicePhase::Off && v.pid.is_none());
        assert!(t0.elapsed() >= Duration::from_secs(2) && ended < Duration::from_millis(1500), "{ended:?}");
        assert_eq!(lk(&talks).last().cloned(), Some(None));
        assert!(read_logs(&logs_dir).contains("listener stopped"));

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
    fn status_line_talk() {
        let st = parse_status(
            r#"WAKE_STATUS {"running": true, "state": "busy", "talk": {"persona": "connor", "phase": "thinking", "text": "коннор какая погода", "sent": "какая погода"}}"#,
        )
        .unwrap();
        let talk = st.talk.unwrap();
        assert_eq!((talk.persona.as_str(), talk.phase.as_str()), ("connor", "thinking"));
        assert_eq!((talk.text.as_str(), talk.sent.as_str()), ("коннор какая погода", "какая погода"));
        let st = parse_status(r#"WAKE_STATUS {"running": true, "talk": null}"#).unwrap();
        assert!(st.talk.is_none());
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
        assert!(st.near.is_none(), "слушатель старее панели — без фильтра фона");
        // Фильтр фона: громкость голоса, запись идёт
        let st = parse_status(
            r#"WAKE_STATUS {"running": true, "near": {"on": true, "level": -13.7, "source": "panel", "margin": 10.0, "calibrating": true, "calib_left": 4.2, "calib_result": ""}}"#,
        )
        .unwrap();
        let near = st.near.unwrap();
        assert_eq!((near.on, near.level, near.source.as_str(), near.margin), (true, Some(-13.7), "panel", 10.0));
        assert!(near.calibrating && (near.calib_left - 4.2).abs() < 1e-9 && near.calib_result.is_empty());
        let near = parse_status(r#"WAKE_STATUS {"near": {"on": true, "level": null, "calib_result": "no_speech"}}"#)
            .unwrap()
            .near
            .unwrap();
        assert!(near.level.is_none() && near.calib_result == "no_speech" && !near.calibrating);
        // Панели — camelCase (api.ts NearStatus)
        let js = serde_json::to_value(&near).unwrap();
        assert!(js.get("calibResult").is_some() && js.get("calibLeft").is_some());
        // Языки распознавания; одна модель не загрузилась
        let st = parse_status(r#"WAKE_STATUS {"running": true, "langs": ["ru"], "lang": "ru", "lang_error": "en: нет сети"}"#)
            .unwrap();
        assert_eq!((st.langs, st.lang.as_str(), st.lang_error.as_str()), (vec!["ru".to_string()], "ru", "en: нет сети"));
        // Слушатель старее панели: лишние и недостающие поля — не ошибка
        let st = parse_status(r#"WAKE_STATUS {"running": false, "error": "нет модулей", "extra": 1}"#).unwrap();
        assert!(!st.running && st.error == "нет модулей" && st.stage.is_empty());
    }
}
