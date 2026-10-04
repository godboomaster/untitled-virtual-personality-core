//! Команды окна панели (invoke из src/api.ts).

use crate::config::{self, display_path, Config};
use crate::panel;
use crate::probe::{self, MemView, OllamaView};
use crate::state::AppState;
use crate::supervisor::ServiceView;
use crate::texts::{t, tf};
use serde::Serialize;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager, State};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_opener::OpenerExt;

/// Порты CDP браузеров бота по умолчанию (app/features/browser_actions.py).
/// Нужны, только когда бэкенд не отвечает: иначе состояние пулов отдаёт он сам
const POOL_H_PORT: u16 = 9223;
const POOL_V_PORT: u16 = 9222;

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PoolH {
    alive: bool,
    mode: Option<String>,
    rescue: bool,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PoolV {
    alive: bool,
    idle_sec: Option<u64>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Pools {
    from_api: bool,
    h: PoolH,
    v: PoolV,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Quarantine {
    site: String,
    kind: String,
    reason: String,
    left_sec: u64,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogEntry {
    seq: u64,
    time: String,
    level: String,
    msg: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogBatch {
    /// api — лента /api/logs; raw — вывод процесса (бэкенд не отвечает); none — пусто
    source: &'static str,
    /// Заменить ленту целиком, а не дописать
    reset: bool,
    cursor: u64,
    entries: Vec<LogEntry>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Snapshot {
    backend: ServiceView,
    web: ServiceView,
    busy: bool,
    manage_web: bool,
    web_url: String,
    pools: Pools,
    quarantine: Vec<Quarantine>,
    ollama: OllamaView,
    memory: MemView,
    logs: LogBatch,
}

fn now_secs() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs_f64()).unwrap_or(0.0)
}

#[tauri::command]
pub async fn snapshot(state: State<'_, AppState>, log_cursor: u64) -> Result<Snapshot, String> {
    let st = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || build_snapshot(&st, log_cursor))
        .await
        .map_err(|e| e.to_string())
}

fn build_snapshot(st: &AppState, cursor: u64) -> Snapshot {
    let (backend, web, busy) = st.sup.views();
    let cfg = st.sup.config();
    let api = st.sup.api();
    let up = backend.responding;
    // 404 — бэкенд старее панели (без /api/system/status): как недоступный
    let status = if up { probe::api_get(&api, "/api/system/status", 4000).ok() } else { None };
    let pools = match &status {
        Some(v) => pools_from_api(&v["browser_pools"]),
        None => Pools {
            from_api: false,
            h: PoolH { alive: probe::cdp_alive(POOL_H_PORT), mode: None, rescue: false },
            v: PoolV { alive: probe::cdp_alive(POOL_V_PORT), idle_sec: None },
        },
    };
    let quarantine = status.as_ref().map(|v| quarantine_list(&v["webchat_quarantine"])).unwrap_or_default();
    let ollama = probe::ollama(&api.ollama);
    let memory = match st.sys.lock() {
        Ok(mut sys) => probe::memory(&mut sys),
        Err(_) => MemView::default(),
    };
    let logs = if up { api_logs(&api, cursor).unwrap_or_else(|| raw_logs(st)) } else { raw_logs(st) };
    Snapshot {
        backend,
        web,
        busy,
        manage_web: cfg.manage_web,
        web_url: web_url(&cfg),
        pools,
        quarantine,
        ollama,
        memory,
        logs,
    }
}

fn pools_from_api(v: &Value) -> Pools {
    let h = &v["h"];
    let pv = &v["v"];
    let v_alive = pv["alive"].as_bool().unwrap_or(false);
    Pools {
        from_api: true,
        h: PoolH {
            alive: h["alive"].as_bool().unwrap_or(false),
            mode: h["mode"].as_str().map(str::to_string),
            rescue: h["rescue"].as_bool().unwrap_or(false),
        },
        v: PoolV { alive: v_alive, idle_sec: pv["idle_sec"].as_u64().filter(|_| v_alive) },
    }
}

fn quarantine_list(v: &Value) -> Vec<Quarantine> {
    let now = now_secs();
    let mut out: Vec<Quarantine> = v
        .as_object()
        .map(|m| {
            m.iter()
                .filter_map(|(site, q)| {
                    let left = q["until"].as_f64()? - now;
                    (left > 0.0).then(|| Quarantine {
                        site: site.clone(),
                        kind: q["kind"].as_str().unwrap_or("challenge").to_string(),
                        reason: q["reason"].as_str().unwrap_or("").to_string(),
                        left_sec: left as u64,
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    out.sort_by(|a, b| a.site.cmp(&b.site));
    out
}

fn fmt_time(ts: Option<f64>) -> String {
    ts.and_then(|s| chrono::DateTime::from_timestamp(s as i64, 0))
        .map(|d| d.with_timezone(&chrono::Local).format("%H:%M:%S").to_string())
        .unwrap_or_default()
}

fn api_logs(api: &config::ApiEnv, cursor: u64) -> Option<LogBatch> {
    let v = probe::api_get(api, &format!("/api/logs?since={cursor}&limit=150"), 2500).ok()?;
    let latest = v["latest"].as_u64().unwrap_or(0);
    if cursor > 0 && latest < cursor {
        // Бэкенд перезапущен — нумерация записей началась заново
        return api_logs(api, 0);
    }
    let entries = v["entries"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .map(|e| LogEntry {
                    seq: e["seq"].as_u64().unwrap_or(0),
                    time: fmt_time(e["ts"].as_f64()),
                    level: e["level"].as_str().unwrap_or("INFO").to_string(),
                    msg: e["msg"].as_str().unwrap_or("").to_string(),
                })
                .collect()
        })
        .unwrap_or_default();
    Some(LogBatch { source: "api", reset: cursor == 0, cursor: latest, entries })
}

/// Строка лога Python: «2026-10-03 14:02:11,123 [INFO] текст»
fn parse_raw(seq: u64, line: &str) -> LogEntry {
    let stamped = line.len() > 24 && line.as_bytes()[4] == b'-' && line.as_bytes()[13] == b':';
    let (time, rest) = match (stamped, line.get(11..19), line.get(24..)) {
        (true, Some(tm), Some(rest)) => (tm.to_string(), rest.trim_start()),
        _ => (String::new(), line),
    };
    let (level, msg) = match rest.strip_prefix('[').and_then(|r| r.split_once("] ")) {
        Some((lvl, msg)) if lvl.chars().all(|c| c.is_ascii_uppercase()) => (lvl.to_string(), msg),
        _ => {
            let err = rest.contains("Traceback") || rest.contains("Error");
            ((if err { "ERROR" } else { "" }).to_string(), rest)
        }
    };
    LogEntry { seq, time, level, msg: msg.to_string() }
}

fn raw_logs(st: &AppState) -> LogBatch {
    let lines = st.sup.backend_tail(80);
    let entries: Vec<LogEntry> = lines.iter().enumerate().map(|(i, l)| parse_raw(i as u64 + 1, l)).collect();
    LogBatch { source: if entries.is_empty() { "none" } else { "raw" }, reset: true, cursor: 0, entries }
}

fn web_url(cfg: &Config) -> String {
    format!("http://localhost:{}", cfg.web_port)
}

#[tauri::command]
pub fn start(state: State<'_, AppState>) {
    state.sup.start();
}

#[tauri::command]
pub fn stop(state: State<'_, AppState>) {
    state.sup.stop();
}

#[tauri::command]
pub fn restart(state: State<'_, AppState>) {
    state.sup.restart();
}

/// Браузер бота: finish=false — показать пул H (rescue), true — «готово»
#[tauri::command]
pub async fn rescue(state: State<'_, AppState>, finish: bool) -> Result<bool, String> {
    let st = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let api = st.sup.api();
        if !probe::health(&api) {
            return Err(t(st.lang, "err_api_down"));
        }
        if finish {
            probe::api_post(&api, "/api/browser/rescue/finish", 15_000).map(|v| v["finished"].as_bool().unwrap_or(false))
        } else {
            // Перезапуск Chrome видимым — до десятков секунд
            probe::api_post(&api, "/api/browser/rescue", 60_000).map(|v| v["ok"].as_bool().unwrap_or(false))
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

pub fn open_web_url(app: &AppHandle) {
    let url = web_url(&app.state::<AppState>().sup.config());
    let _ = app.opener().open_url(url, None::<&str>);
    panel::hide(app);
}

#[tauri::command]
pub fn open_web(app: AppHandle) {
    open_web_url(&app);
}

#[tauri::command]
pub fn open_logs(app: AppHandle, state: State<'_, AppState>) -> Result<(), String> {
    let dir = state.sup.logs_dir();
    let _ = std::fs::create_dir_all(&dir);
    app.opener().open_path(display_path(&dir), None::<&str>).map_err(|e| e.to_string())
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Settings {
    config: Config,
    autostart: bool,
    config_path: String,
    logs_dir: String,
    repo_ok: bool,
}

fn settings(app: &AppHandle, st: &AppState) -> Settings {
    let cfg = st.sup.config();
    Settings {
        repo_ok: config::repo_ok(Path::new(&cfg.repo_dir)),
        config: cfg,
        autostart: app.autolaunch().is_enabled().unwrap_or(false),
        config_path: display_path(st.sup.config_path()),
        logs_dir: display_path(&st.sup.logs_dir()),
    }
}

#[tauri::command]
pub fn get_settings(app: AppHandle, state: State<'_, AppState>) -> Settings {
    settings(&app, &state)
}

#[tauri::command]
pub fn save_settings(app: AppHandle, state: State<'_, AppState>, config: Config) -> Result<Settings, String> {
    let lang = state.lang;
    let mut cfg = config;
    cfg.repo_dir = cfg.repo_dir.trim().to_string();
    cfg.python = cfg.python.trim().to_string();
    cfg.node = cfg.node.trim().to_string();
    if !config::repo_ok(Path::new(&cfg.repo_dir)) {
        return Err(tf(lang, "err_repo", &cfg.repo_dir));
    }
    if !cfg.python.is_empty() && !PathBuf::from(&cfg.python).is_file() {
        return Err(tf(lang, "err_python_path", &cfg.python));
    }
    if !cfg.node.is_empty() && !PathBuf::from(&cfg.node).is_file() {
        return Err(t(lang, "err_node"));
    }
    if cfg.web_port == 0 {
        cfg.web_port = Config::default().web_port;
    }
    state.sup.set_config(cfg)?;
    Ok(settings(&app, &state))
}

#[tauri::command]
pub async fn detect_python(state: State<'_, AppState>, repo_dir: String) -> Result<String, String> {
    let st = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || st.sup.detect_python(Path::new(repo_dir.trim())))
        .await
        .map_err(|e| e.to_string())?
}

#[tauri::command]
pub fn set_autostart(app: AppHandle, enabled: bool) -> Result<bool, String> {
    let al = app.autolaunch();
    if enabled { al.enable() } else { al.disable() }.map_err(|e| e.to_string())?;
    al.is_enabled().map_err(|e| e.to_string())
}

#[tauri::command]
pub fn hide_panel(app: AppHandle) {
    panel::hide(&app);
}

/// Выход: остановить запущенное панелью (бот из терминала не трогается)
pub fn quit_app(app: AppHandle) {
    panel::hide(&app);
    let st = app.state::<AppState>().inner().clone();
    std::thread::spawn(move || {
        st.sup.stop_ours_blocking();
        app.exit(0);
    });
}

#[tauri::command]
pub fn quit(app: AppHandle) {
    quit_app(app);
}

#[cfg(test)]
mod tests {
    use super::parse_raw;

    #[test]
    fn raw_python_line() {
        let e = parse_raw(1, "2026-10-03 14:02:11,123 [WARNING] [WebChat] карантин");
        assert_eq!((e.time.as_str(), e.level.as_str(), e.msg.as_str()), ("14:02:11", "WARNING", "[WebChat] карантин"));
        let e = parse_raw(2, "ModuleNotFoundError: No module named 'x'");
        assert_eq!((e.time.as_str(), e.level.as_str()), ("", "ERROR"));
        let e = parse_raw(3, "INFO:     Uvicorn running on http://127.0.0.1:8000");
        assert_eq!(e.level, "");
    }
}
