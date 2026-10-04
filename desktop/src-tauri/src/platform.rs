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
