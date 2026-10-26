//! Общее состояние панели для команд окна, меню трея и монитора.

use crate::portrait::Portrait;
use crate::supervisor::Supervisor;
use crate::texts::Lang;
use crate::voice::Voice;
use std::sync::{Arc, Mutex};
use std::time::Instant;

#[derive(Clone)]
pub struct AppState {
    pub sup: Supervisor,
    /// Слушатель имени («Коннор, …») — процесс панели
    pub voice: Voice,
    /// Портрет на рабочем столе: чей, где, какого размера (portrait.rs)
    pub portrait: Portrait,
    pub sys: Arc<Mutex<sysinfo::System>>,
    /// Когда панель спряталась (потеря фокуса) — см. panel::toggle
    pub hidden_at: Arc<Mutex<Option<Instant>>>,
    pub lang: Lang,
}
