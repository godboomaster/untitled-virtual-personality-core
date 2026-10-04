//! Общее состояние панели для команд окна, меню трея и монитора.

use crate::supervisor::Supervisor;
use crate::texts::Lang;
use std::sync::{Arc, Mutex};
use std::time::Instant;

#[derive(Clone)]
pub struct AppState {
    pub sup: Supervisor,
    pub sys: Arc<Mutex<sysinfo::System>>,
    /// Когда панель спряталась (потеря фокуса) — см. panel::toggle
    pub hidden_at: Arc<Mutex<Option<Instant>>>,
    pub lang: Lang,
}
