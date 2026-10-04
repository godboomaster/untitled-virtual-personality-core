//! Настройки панели (config.json в папке настроек приложения) и параметры
//! бэкенда из .env проекта: порт API, токен, адрес Ollama.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct Config {
    /// Корень virtual-persona-core (там app/main.py)
    pub repo_dir: String,
    /// Интерпретатор бэкенда; пусто — найти автоматически
    pub python: String,
    /// Node.js для веб-интерфейса (vite); пусто — найти автоматически
    pub node: String,
    /// Запускать веб-интерфейс (vite dev из web/) вместе с ботом
    pub manage_web: bool,
    pub web_port: u16,
    /// Запускать бота, когда стартует панель
    pub start_on_launch: bool,
    /// Перезапускать бота после падения (не больше 3 раз за 10 минут)
    pub auto_restart: bool,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            repo_dir: default_repo_dir(),
            python: String::new(),
            node: String::new(),
            manage_web: true,
            web_port: 5173,
            start_on_launch: true,
            auto_restart: true,
        }
    }
}

fn default_repo_dir() -> String {
    // Панель собирается из desktop/src-tauri репозитория — корень на два уровня выше
    let p = Path::new(env!("CARGO_MANIFEST_DIR")).join("..").join("..");
    display_path(&p.canonicalize().unwrap_or(p))
}

/// Путь для показа и для дочерних процессов: без префикса \\?\ Windows
pub fn display_path(p: &Path) -> String {
    let s = p.to_string_lossy().into_owned();
    s.strip_prefix(r"\\?\").map(str::to_string).unwrap_or(s)
}

pub fn repo_ok(repo: &Path) -> bool {
    repo.join("app").join("main.py").is_file()
}

pub fn load(path: &Path) -> Config {
    fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

pub fn save(path: &Path, cfg: &Config) -> Result<(), String> {
    if let Some(dir) = path.parent() {
        fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let tmp = path.with_extension("json.tmp");
    let body = serde_json::to_string_pretty(cfg).map_err(|e| e.to_string())?;
    fs::write(&tmp, body).map_err(|e| e.to_string())?;
    fs::rename(&tmp, path).map_err(|e| e.to_string())
}

/// Куда смотрит бэкенд — по тем же правилам, что app/main.py: переменная
/// окружения, затем .env, затем .env.config (первое заданное побеждает)
#[derive(Clone, Debug)]
pub struct ApiEnv {
    pub base: String,
    pub port: u16,
    pub token: Option<String>,
    pub ollama: String,
}

impl ApiEnv {
    pub fn read(repo: &Path) -> ApiEnv {
        let files = [read_env_file(&repo.join(".env")), read_env_file(&repo.join(".env.config"))];
        let get = |key: &str| -> Option<String> {
            std::env::var(key)
                .ok()
                .or_else(|| files.iter().find_map(|f| f.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone())))
        };
        let port = get("API_PORT").and_then(|p| p.trim().parse().ok()).unwrap_or(8000);
        let host = match get("API_HOST").unwrap_or_default().trim() {
            "" | "0.0.0.0" | "::" => "127.0.0.1".to_string(),
            h if h.contains(':') => format!("[{h}]"),
            h => h.to_string(),
        };
        let token = get("API_TOKEN").map(|t| t.trim().to_string()).filter(|t| !t.is_empty());
        let ollama = get("OLLAMA_URL")
            .map(|u| u.trim().trim_end_matches('/').to_string())
            .filter(|u| !u.is_empty())
            .unwrap_or_else(|| "http://localhost:11434".into());
        ApiEnv { base: format!("http://{host}:{port}"), port, token, ollama }
    }
}

/// Значения .env. Семантика как у app/core/envfile.py: «KEY=   # комментарий» —
/// пустое значение; последнее присваивание ключа побеждает
fn read_env_file(path: &Path) -> Vec<(String, String)> {
    let Ok(text) = fs::read_to_string(path) else { return Vec::new() };
    let mut out: Vec<(String, String)> = Vec::new();
    for line in text.lines() {
        if let Some((k, v)) = parse_env_line(line) {
            out.retain(|(key, _)| *key != k);
            out.push((k, v));
        }
    }
    out
}

fn parse_env_line(line: &str) -> Option<(String, String)> {
    let s = line.trim_start();
    if s.is_empty() || s.starts_with('#') {
        return None;
    }
    let s = s.strip_prefix("export ").unwrap_or(s);
    let (k, v) = s.split_once('=')?;
    let key = k.trim();
    if key.is_empty() || !key.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '.') {
        return None;
    }
    let raw = v.trim_start_matches([' ', '\t']);
    let spaced = raw.len() != v.len();
    let value = match raw.chars().next() {
        Some(q @ ('"' | '\'')) => {
            let rest = &raw[1..];
            rest.find(q).map(|end| &rest[..end]).unwrap_or(rest).to_string()
        }
        Some('#') if spaced => String::new(),
        _ => {
            let cut = raw.find(" #").or_else(|| raw.find("\t#")).unwrap_or(raw.len());
            raw[..cut].trim().to_string()
        }
    };
    Some((key.to_string(), value))
}

pub fn logs_dir(repo: &Path, fallback: &Path) -> PathBuf {
    if repo_ok(repo) {
        repo.join("logs")
    } else {
        fallback.to_path_buf()
    }
}

#[cfg(test)]
mod tests {
    use super::parse_env_line;

    fn p(s: &str) -> Option<(String, String)> {
        parse_env_line(s)
    }

    #[test]
    fn env_lines() {
        assert_eq!(p("API_PORT=8001"), Some(("API_PORT".into(), "8001".into())));
        assert_eq!(p("export API_TOKEN=\"a b\""), Some(("API_TOKEN".into(), "a b".into())));
        assert_eq!(p("API_TOKEN=   # комментарий"), Some(("API_TOKEN".into(), "".into())));
        assert_eq!(p("API_TOKEN=#abc"), Some(("API_TOKEN".into(), "#abc".into())));
        assert_eq!(p("API_TOKEN=abc # хвост"), Some(("API_TOKEN".into(), "abc".into())));
        assert_eq!(p("# API_PORT=1"), None);
        assert_eq!(p("not a pair"), None);
    }
}
