//! Проверки снаружи процесса бота: HTTP к API, порты, CDP браузеров бота,
//! Ollama, память системы. Все запросы — к своей машине и с короткими
//! таймаутами: панель не должна ждать зависший бэкенд.

use crate::config::ApiEnv;
use serde::Serialize;
use serde_json::Value;
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

fn agent(timeout_ms: u64) -> ureq::Agent {
    ureq::AgentBuilder::new().timeout(Duration::from_millis(timeout_ms)).build()
}

fn finish(res: Result<ureq::Response, ureq::Error>) -> Result<Value, String> {
    match res {
        Ok(r) => r.into_json::<Value>().map_err(|e| e.to_string()),
        Err(ureq::Error::Status(code, r)) => {
            let detail = r
                .into_json::<Value>()
                .ok()
                .and_then(|v| v.get("detail").and_then(|d| d.as_str()).map(str::to_string));
            Err(match detail {
                Some(d) => format!("HTTP {code}: {d}"),
                None => format!("HTTP {code}"),
            })
        }
        Err(e) => Err(e.to_string()),
    }
}

pub fn api_get(api: &ApiEnv, path: &str, timeout_ms: u64) -> Result<Value, String> {
    let mut req = agent(timeout_ms).get(&format!("{}{path}", api.base));
    if let Some(t) = &api.token {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    finish(req.call())
}

pub fn api_post(api: &ApiEnv, path: &str, timeout_ms: u64) -> Result<Value, String> {
    let mut req = agent(timeout_ms).post(&format!("{}{path}", api.base));
    if let Some(t) = &api.token {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    finish(req.send_string(""))
}

pub fn api_post_json(api: &ApiEnv, path: &str, body: &Value, timeout_ms: u64) -> Result<Value, String> {
    let mut req = agent(timeout_ms).post(&format!("{}{path}", api.base));
    if let Some(t) = &api.token {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    finish(req.send_json(body.clone()))
}

/// Тело формы multipart с одним файлом (поле field, имя файла filename)
pub fn multipart(boundary: &str, field: &str, filename: &str, bytes: &[u8]) -> Vec<u8> {
    // Кавычки и переводы строк в имени сломали бы заголовок части
    let name: String = filename.chars().map(|c| if matches!(c, '"' | '\\' | '\r' | '\n') { '_' } else { c }).collect();
    let mut body = Vec::with_capacity(bytes.len() + 256);
    body.extend_from_slice(
        format!(
            "--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
        )
        .as_bytes(),
    );
    body.extend_from_slice(bytes);
    body.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    body
}

/// Файл формой multipart — как <input type=file> веба: POST /api/personas/{p}/files
pub fn api_post_file(api: &ApiEnv, path: &str, field: &str, filename: &str, bytes: &[u8], timeout_ms: u64) -> Result<Value, String> {
    let nonce = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let boundary = format!("----vpc{nonce:x}");
    let body = multipart(&boundary, field, filename, bytes);
    let mut req = agent(timeout_ms)
        .post(&format!("{}{path}", api.base))
        .set("Content-Type", &format!("multipart/form-data; boundary={boundary}"));
    if let Some(t) = &api.token {
        req = req.set("Authorization", &format!("Bearer {t}"));
    }
    finish(req.send_bytes(&body))
}

pub fn health(api: &ApiEnv) -> bool {
    api_get(api, "/api/health", 1500).map(|v| v["status"] == "ok").unwrap_or(false)
}

/// Слушает ли кто-то порт на loopback. vite на новых Node слушает
/// «localhost», а это бывает только ::1 — пробуем оба адреса
pub fn port_open(port: u16) -> bool {
    ["127.0.0.1", "[::1]"].iter().any(|h| {
        format!("{h}:{port}")
            .parse::<SocketAddr>()
            .map(|a| TcpStream::connect_timeout(&a, Duration::from_millis(300)).is_ok())
            .unwrap_or(false)
    })
}

/// Chrome бота отвечает на CDP (/json/version) — живой
pub fn cdp_alive(port: u16) -> bool {
    agent(600).get(&format!("http://127.0.0.1:{port}/json/version")).call().is_ok()
}

#[derive(Serialize, Clone, Default)]
#[serde(rename_all = "camelCase")]
pub struct OllamaModel {
    pub name: String,
    pub size_gb: f64,
}

#[derive(Serialize, Clone, Default)]
#[serde(rename_all = "camelCase")]
pub struct OllamaView {
    pub up: bool,
    pub models: Vec<OllamaModel>,
}

/// Ollama и модели, загруженные в память сейчас (/api/ps)
pub fn ollama(url: &str) -> OllamaView {
    let Ok(v) = finish(agent(800).get(&format!("{url}/api/ps")).call()) else {
        return OllamaView::default();
    };
    let models = v["models"]
        .as_array()
        .map(|arr| {
            arr.iter()
                .map(|m| OllamaModel {
                    name: m["name"].as_str().unwrap_or("?").to_string(),
                    size_gb: gb(m["size"].as_f64().unwrap_or(0.0)),
                })
                .collect()
        })
        .unwrap_or_default();
    OllamaView { up: true, models }
}

fn gb(bytes: f64) -> f64 {
    (bytes / 1024f64.powi(3) * 10.0).round() / 10.0
}

#[derive(Serialize, Clone, Default)]
#[serde(rename_all = "camelCase")]
pub struct MemView {
    /// Свободная память, % — на macOS то же число, что у `memory_pressure`
    pub free_percent: u32,
    pub used_gb: f64,
    pub total_gb: f64,
    pub swap_used_gb: f64,
}

pub fn memory(sys: &mut sysinfo::System) -> MemView {
    sys.refresh_memory();
    let total = sys.total_memory() as f64;
    let avail = sys.available_memory() as f64;
    let free = if total > 0.0 { (avail / total * 100.0).round() as u32 } else { 0 };
    #[cfg(target_os = "macos")]
    let free = macos_free_percent().unwrap_or(free);
    MemView {
        free_percent: free,
        used_gb: gb(total - avail),
        total_gb: gb(total),
        swap_used_gb: gb(sys.used_swap() as f64),
    }
}

#[cfg(target_os = "macos")]
fn macos_free_percent() -> Option<u32> {
    let name = std::ffi::CString::new("kern.memorystatus_level").ok()?;
    let mut val: libc::c_int = 0;
    let mut len = std::mem::size_of::<libc::c_int>();
    let rc = unsafe {
        libc::sysctlbyname(
            name.as_ptr(),
            &mut val as *mut libc::c_int as *mut libc::c_void,
            &mut len,
            std::ptr::null_mut(),
            0,
        )
    };
    (rc == 0 && (0..=100).contains(&val)).then_some(val as u32)
}

#[cfg(test)]
mod tests {
    use super::multipart;

    #[test]
    fn multipart_body() {
        let body = multipart("XX", "file", "отчёт \"1\".pdf", b"%PDF");
        let text = String::from_utf8(body).unwrap();
        assert_eq!(
            text,
            "--XX\r\nContent-Disposition: form-data; name=\"file\"; filename=\"отчёт _1_.pdf\"\r\nContent-Type: application/octet-stream\r\n\r\n%PDF\r\n--XX--\r\n"
        );
    }
}
