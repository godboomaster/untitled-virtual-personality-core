//! Вывод бэкенда и vite: в файл logs/<имя>-ГГГГ-ММ-ДД.log корня проекта
//! (раньше лог жил только в терминале) и в короткий хвост для панели —
//! когда бот упал, /api/logs уже некому отдавать, а traceback нужен.

use std::collections::VecDeque;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime};

const TAIL_MAX: usize = 300;
const KEEP_DAYS: u64 = 14;

pub struct LogSink {
    dir: PathBuf,
    name: &'static str,
    date: String,
    file: Option<File>,
    pub tail: VecDeque<String>,
}

impl LogSink {
    pub fn new(dir: PathBuf, name: &'static str) -> Self {
        LogSink { dir, name, date: String::new(), file: None, tail: VecDeque::new() }
    }

    pub fn set_dir(&mut self, dir: PathBuf) {
        if dir != self.dir {
            self.dir = dir;
            self.file = None;
        }
    }

    pub fn line(&mut self, raw: &str) {
        let line = strip_ansi(raw);
        let line = line.trim_end();
        if line.trim().is_empty() || is_poll_noise(line) {
            return;
        }
        self.write(line);
        self.tail.push_back(line.to_string());
        while self.tail.len() > TAIL_MAX {
            self.tail.pop_front();
        }
    }

    /// Отметка панели в логе: запуск, остановка, падение
    pub fn marker(&mut self, text: &str) {
        let stamp = chrono::Local::now().format("%Y-%m-%d %H:%M:%S");
        self.line(&format!("──── {stamp} {text} ────"));
    }

    pub fn current_path(&self) -> PathBuf {
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        self.dir.join(format!("{}-{today}.log", self.name))
    }

    fn write(&mut self, line: &str) {
        let today = chrono::Local::now().format("%Y-%m-%d").to_string();
        if self.file.is_none() || today != self.date {
            let _ = fs::create_dir_all(&self.dir);
            self.file = OpenOptions::new().create(true).append(true).open(self.current_path()).ok();
            self.date = today;
        }
        if let Some(f) = self.file.as_mut() {
            let _ = writeln!(f, "{line}");
        }
    }
}

/// access-лог uvicorn: опросы самой панели (раз в несколько секунд) забили
/// бы файл тысячами одинаковых строк
fn is_poll_noise(line: &str) -> bool {
    ["\"GET /api/health ", "\"GET /api/system/status ", "\"GET /api/logs?"]
        .iter()
        .any(|p| line.contains(p))
}

fn strip_ansi(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\u{1b}' {
            if chars.peek() == Some(&'[') {
                chars.next();
                for n in chars.by_ref() {
                    if ('@'..='~').contains(&n) {
                        break;
                    }
                }
            }
            continue;
        }
        if c != '\r' {
            out.push(c);
        }
    }
    out
}

/// Поток чтения stdout/stderr дочернего процесса построчно в LogSink
pub fn pump<R: Read + Send + 'static>(src: Option<R>, sink: Arc<Mutex<LogSink>>) {
    let Some(src) = src else { return };
    thread::spawn(move || {
        let mut reader = BufReader::new(src);
        let mut buf = Vec::new();
        loop {
            buf.clear();
            match reader.read_until(b'\n', &mut buf) {
                Ok(0) | Err(_) => break,
                Ok(_) => {
                    let text = String::from_utf8_lossy(&buf);
                    if let Ok(mut s) = sink.lock() {
                        s.line(&text);
                    }
                }
            }
        }
    });
}

/// Старые дневные файлы панели — удалить (старше KEEP_DAYS)
pub fn cleanup(dir: &Path) {
    let Ok(rd) = fs::read_dir(dir) else { return };
    let limit = Duration::from_secs(KEEP_DAYS * 24 * 3600);
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        let ours = (name.starts_with("backend-") || name.starts_with("web-")) && name.ends_with(".log");
        let old = e
            .metadata()
            .and_then(|m| m.modified())
            .ok()
            .and_then(|m| SystemTime::now().duration_since(m).ok())
            .map(|age| age > limit)
            .unwrap_or(false);
        if ours && old {
            let _ = fs::remove_file(e.path());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{is_poll_noise, strip_ansi};

    #[test]
    fn ansi_and_noise() {
        assert_eq!(strip_ansi("\u{1b}[32mVITE\u{1b}[0m ready\r"), "VITE ready");
        assert!(is_poll_noise("INFO:     127.0.0.1:5 - \"GET /api/health HTTP/1.1\" 200 OK"));
        assert!(!is_poll_noise("INFO:     127.0.0.1:5 - \"GET /api/inbox HTTP/1.1\" 200 OK"));
    }
}
