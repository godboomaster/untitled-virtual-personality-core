//! Портрет ходит сам (модуль portrait): гуляет по экрану, падает, когда его
//! бросили, стоит на нижнем крае экрана или на верхнем крае окон и едет
//! вместе с окном, прыгает на окно, идёт к курсору, прячется за край.
//!
//! Голова — квадрат (hx, hy, size) в логических px экрана; «ноги» — её низ.
//! Опора под точкой x (ground_at) — ближайшая поверхность не выше ног:
//! нижний край рабочей области монитора, а с «Ходит по окнам» (physics) —
//! ещё верхние края видимых окон других программ, где край не закрыт окном
//! спереди. Без physics гравитации нет вовсе: портрет стоит, где поставили, и
//! ходит по горизонтали на той же высоте.
//!
//! Бросок (toss): голова летит с половиной скорости мыши, когда отпустили;
//! подкинули — не выше своей высоты над местом броска, вбок — не быстрее
//! TOSS_X_MAX; почти не двигали мышь — просто падает. Приземлилась быстро —
//! один невысокий отскок (BOUNCE_UP своей высоты), о верх экрана —
//! ударяется и вниз.
//!
//! Шаги — только когда у персоны есть своя анимация шагов (страница говорит
//! portrait_walker): без неё сама не гуляет (stroll — false), а «Иди сюда» и
//! «Спрячься» переезжают в DASH_K раз быстрее шага, голова неподвижна.
//! Фигура в кадрах шагов сама уходит вперёд (нарисована так) — окно идёт её
//! шагами: страница держит фигуру в окне на месте и на каждой смене кадра
//! шлёт шаг (portrait_gait); иначе — ровно, WALK_SPEED.
//!
//! Движение — один поток на раз (run): падение и прыжок — тик TICK_AIR, шаги —
//! TICK_WALK; стоит на окне — тик TICK_PERCH, только сверить окно под ногами
//! (сдвинули — едет с ним, ушло — падает). Стоит на нижнем крае экрана —
//! потоков нет. Новое движение или перетаскивание гасит прежнее по номеру
//! (gen). Странице — событие portrait-motion {state, dir[, speed]} при каждой
//! смене состояния: walk, idle, held, fall, land, jump, hidden, perch.
//!
//! Подглядывает (peek): анимация с краем экрана (anim.edge — сбоку или сверху)
//! — страница просит поставить голову так, чтобы линия из кадра стояла на
//! краю; сбоку — у ближнего края, нарисована для другого — отражённой. Пока
//! подглядывает, занята (busy): не падает, сама не гуляет, место не
//! поправляется. Анимация кончилась — назад, где стояла (peek_end); любое
//! другое движение (схватили, «Иди сюда», «Спрячься») — возврата нет.

use super::{area_among, fit, frame, head_pos, keep, lk, primary_area, work_areas, Area, Portrait};
use crate::platform::{self, WinRect};
use serde::Serialize;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, LogicalPosition, PhysicalPosition, WebviewWindow};

/// Гравитация и предел скорости падения, px/s², px/s
const G: f64 = 2600.0;
const V_MAX: f64 = 2400.0;
/// Скорость мыши в конце перетаскивания — не быстрее, px/s
const THROW_MAX: f64 = 3000.0;
/// Скорость броска — по перетаскиванию за последние столько секунд
const THROW_WINDOW: f64 = 0.08;
/// Бросок (toss): голова тяжелее курсора — летит с такой долей скорости мыши
const THROW_K: f64 = 0.5;
/// Подкинули — выше стольких своих высот не взлетает (над местом, где отпустили)
const TOSS_UP: f64 = 1.0;
/// Вбок бросок не быстрее, px/s
const TOSS_X_MAX: f64 = 1500.0;
/// Медленнее — просто отпустили: падает отвесно
const TOSS_MIN: f64 = 120.0;
/// Приземление: ударилась быстрее BOUNCE_MIN — отскок с долей RESTITUTION
/// скорости, но не выше BOUNCE_UP своей высоты; по x гаснет до BOUNCE_X
const BOUNCE_MIN: f64 = 500.0;
const RESTITUTION: f64 = 0.2;
const BOUNCE_UP: f64 = 0.1;
const BOUNCE_X: f64 = 0.45;
/// В полёте скорость по x гаснет: за секунду остаётся такая доля
const AIR_DRAG: f64 = 0.35;
/// От боков экрана отскакивает с такой долей скорости
const BOUNCE: f64 = 0.5;
/// Ударилась о верх экрана — вниз с такой долей скорости
const CEIL_BOUNCE: f64 = 0.2;
const TICK_AIR: Duration = Duration::from_millis(16);
const TICK_WALK: Duration = Duration::from_millis(33);
const TICK_PERCH: Duration = Duration::from_millis(100);
/// Шаг: px/s при голове SIZE_REF (больше голова — быстрее, в той же пропорции)
const WALK_SPEED: f64 = 70.0;
const SIZE_REF: f64 = 240.0;
/// Шаги из кадров: страница молчит дольше — снова ровно (страница пропала)
const GAIT_STALE: Duration = Duration::from_secs(3);
/// Один шаг из кадров — не длиннее, логических px
const GAIT_STEP_MAX: f64 = 600.0;
/// Без физики к курсору — «летит» во столько раз быстрее шага
const GLIDE_K: f64 = 1.5;
/// Своих шагов нет — «Иди сюда» и «Спрячься» во столько раз быстрее шага:
/// голова не идёт, а быстро переезжает
const DASH_K: f64 = 8.0;
/// Край окна: стоять не ближе к его углам
const EDGE_IN: f64 = 8.0;
/// Край окна чуть выше ног — ещё опора (округления, тень рамки)
const FEET_TOL: f64 = 4.0;
/// Опора ушла из-под ног дальше — падает
const FALL_GAP: f64 = 6.0;
/// Мелкие окна (всплывающие, значки) — не опора
const WIN_MIN_W: f64 = 120.0;
const WIN_MIN_H: f64 = 60.0;
/// Верх окна у самого верха рабочей области — окно развёрнуто, его край — не опора
const MAXIMIZED_GAP: f64 = 2.0;
/// Прогулка: на столько px от места
const STROLL_MIN: f64 = 150.0;
const STROLL_MAX: f64 = 600.0;
/// Ближе — не прогулка
const STROLL_SHORT: f64 = 60.0;
/// Прыжок на окно: по горизонтали до HOP_X голов, вверх — от HOP_MIN до HOP_UP голов
const HOP_X: f64 = 3.0;
const HOP_UP: f64 = 4.0;
const HOP_MIN: f64 = 0.25;
/// Список окон в движении — не чаще
const WINS_EVERY: Duration = Duration::from_millis(50);
/// Мониторы — переспросить раз в столько (стоит на окне долго)
const AREAS_EVERY: Duration = Duration::from_secs(10);
/// Окно под ногами двигают — следить с тиком шагов ещё столько после сдвига
const RIDE_FAST: Duration = Duration::from_secs(1);
/// Окно сдвинули отсюда — столько ждать, пока место дойдёт до настроек (Moved)
const SETTLED: Duration = Duration::from_millis(400);

// ── Чистые функции: опора, полёт, цели ─────────────────────────────────

/// Поверхность, на которой можно стоять: высота края, где она есть по x,
/// окно (None — нижний край экрана)
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Surface {
    pub y: f64,
    pub left: f64,
    pub right: f64,
    pub win: Option<u64>,
}

fn contains(w: &WinRect, x: f64, y: f64) -> bool {
    x >= w.x && x < w.x + w.w && y >= w.y && y < w.y + w.h
}

/// На верхнем крае окна можно стоять: не мелкое, не развёрнутое, край — в
/// пределах рабочей области по высоте
fn ledge_ok(w: &WinRect, area: Area) -> bool {
    w.w >= WIN_MIN_W && w.h >= WIN_MIN_H && w.y > area.1 + MAXIMIZED_GAP && w.y < area.1 + area.3
}

/// Край окна wins[i] в точке x виден: x на краю (не у самых углов) и под
/// ногами — само окно, а не окно спереди
fn ledge_visible(wins: &[WinRect], i: usize, x: f64) -> bool {
    let w = &wins[i];
    x >= w.x + EDGE_IN && x <= w.x + w.w - EDGE_IN && !wins[..i].iter().any(|f| contains(f, x, w.y + 2.0))
}

/// Нижний край рабочей области
pub fn floor_of(area: Area) -> Surface {
    Surface { y: area.1 + area.3, left: area.0, right: area.0 + area.2, win: None }
}

/// Опора под x для ног на высоте feet: ближайшая поверхность не выше ног
/// (с допуском FEET_TOL). Без physics — только нижний край экрана
pub fn ground_at(x: f64, feet: f64, area: Area, wins: &[WinRect], physics: bool) -> Surface {
    let mut best = floor_of(area);
    if !physics {
        return best;
    }
    for (i, w) in wins.iter().enumerate() {
        if !ledge_ok(w, area) || w.y < feet - FEET_TOL || w.y >= best.y {
            continue;
        }
        if ledge_visible(wins, i, x) {
            best = Surface { y: w.y, left: w.x + EDGE_IN, right: w.x + w.w - EDGE_IN, win: Some(w.id) };
        }
    }
    best
}

/// Видимый участок края окна wins[i] вокруг x — шагами по 8 px
pub fn ledge_span(wins: &[WinRect], i: usize, x: f64) -> (f64, f64) {
    const STEP: f64 = 8.0;
    let (mut l, mut r) = (x, x);
    while ledge_visible(wins, i, l - STEP) {
        l -= STEP;
    }
    while ledge_visible(wins, i, r + STEP) {
        r += STEP;
    }
    (l, r)
}

/// Тело в полёте: голова и скорость
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Body {
    pub hx: f64,
    pub hy: f64,
    pub vx: f64,
    pub vy: f64,
}

/// Стены и потолок для всей фигуры, а не только квадрата головы. Запас
/// музыкальной панели сюда не входит: он не меняет физический размер тела.
fn flight_area(area: Area, size: f64, room: super::Room) -> Area {
    let (ax, ay, aw, ah) = area;
    let left = ax + (size * (room.kl - 0.5)).max(0.0).ceil();
    let right = ax + aw - size - (size * (room.kr - 0.5)).max(0.0).ceil();
    // Если кадр шире монитора, оставить его по центру, без двух отскоков
    // между перевёрнутыми границами за каждый тик.
    let (x, width) = if left <= right { (left, right - left + size) } else { ((left + right) / 2.0, size) };
    let top = ay + (size * (room.ky - 1.0)).max(0.0).ceil();
    (x, top, width, (ay + ah - top).max(size))
}

/// Шаг полёта на dt секунд: гравитация, затухание по x, отскок от боков
/// области, потолок — верх области
pub fn fly(b: Body, dt: f64, size: f64, area: Area) -> Body {
    let mut n = b;
    n.vy = (n.vy + G * dt).min(V_MAX);
    n.vx *= AIR_DRAG.powf(dt);
    n.hx += n.vx * dt;
    n.hy += n.vy * dt;
    let (l, r) = (area.0, area.0 + area.2 - size);
    if r <= l {
        n.hx = (l + r) / 2.0;
        n.vx = 0.0;
    } else if n.hx < l {
        n.hx = l;
        n.vx = n.vx.abs() * BOUNCE;
    } else if n.hx > r {
        n.hx = r;
        n.vx = -n.vx.abs() * BOUNCE;
    }
    if n.hy < area.1 {
        n.hy = area.1;
        n.vy = (-n.vy * CEIL_BOUNCE).max(0.0);
    }
    n
}

/// Скорость, с которой голова полетит, из скорости мыши, когда отпустили:
/// доля THROW_K; вверх — не выше TOSS_UP своих высот, вбок — не быстрее
/// TOSS_X_MAX; совсем медленно — ноль (просто отпустили)
pub fn toss(vx: f64, vy: f64, size: f64) -> (f64, f64) {
    let (vx, vy) = (vx * THROW_K, vy * THROW_K);
    if vx.hypot(vy) < TOSS_MIN {
        return (0.0, 0.0);
    }
    let up_max = (2.0 * G * TOSS_UP * size).sqrt();
    (vx.clamp(-TOSS_X_MAX, TOSS_X_MAX), vy.max(-up_max))
}

/// Отскок при приземлении со скоростью v (вниз), px/s вверх; 0 — не отскакивает
pub fn bounce_up(v: f64, size: f64) -> f64 {
    if v < BOUNCE_MIN {
        return 0.0;
    }
    (v * RESTITUTION).min((2.0 * G * BOUNCE_UP * size).sqrt())
}

/// Скорость броска по точкам перетаскивания (секунды, hx, hy): от самой
/// старой за последние THROW_WINDOW до последней. Мышь стояла перед тем, как
/// отпустили, — ноль
pub fn release_velocity(samples: &[(f64, f64, f64)], now: f64) -> (f64, f64) {
    let Some(&(t1, x1, y1)) = samples.last() else { return (0.0, 0.0) };
    if now - t1 > THROW_WINDOW {
        return (0.0, 0.0);
    }
    let Some(&(t0, x0, y0)) = samples.iter().find(|s| t1 - s.0 <= THROW_WINDOW) else { return (0.0, 0.0) };
    let dt = t1 - t0;
    if dt < 0.008 {
        return (0.0, 0.0);
    }
    let (vx, vy) = ((x1 - x0) / dt, (y1 - y0) / dt);
    let v = vx.hypot(vy);
    if v > THROW_MAX {
        (vx * THROW_MAX / v, vy * THROW_MAX / v)
    } else {
        (vx, vy)
    }
}

/// Куда прогуляться: середина головы из cx в пределах опоры span (голова на
/// ней целиком), на STROLL_MIN..STROLL_MAX в случайную сторону (r_dist,
/// r_dir — от 0 до 1); в ту сторону не помещается — в другую, нигде — к
/// дальнему краю. Ближе STROLL_SHORT — некуда
pub fn stroll_target(cx: f64, span: (f64, f64), size: f64, r_dist: f64, r_dir: f64) -> Option<f64> {
    let (lo, hi) = (span.0 + size / 2.0, span.1 - size / 2.0);
    if hi - lo < STROLL_SHORT {
        return None;
    }
    let dist = STROLL_MIN + r_dist.clamp(0.0, 1.0) * (STROLL_MAX - STROLL_MIN);
    let dir = if r_dir < 0.5 { -1.0 } else { 1.0 };
    let fits = |t: f64| t >= lo && t <= hi;
    let t = [cx + dir * dist, cx - dir * dist]
        .into_iter()
        .find(|t| fits(*t))
        .unwrap_or(if cx - lo > hi - cx { lo } else { hi });
    ((t - cx).abs() >= STROLL_SHORT).then_some(t)
}

/// Край окна, на который можно запрыгнуть: середина головы x, высота края y
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Ledge {
    pub x: f64,
    pub y: f64,
    pub win: u64,
}

/// Края окон, на которые можно запрыгнуть из (cx, feet): выше ног на
/// HOP_MIN..HOP_UP голов, по горизонтали до HOP_X голов, голова над краем — на
/// экране, место приземления — на видимом участке края
pub fn hop_targets(cx: f64, feet: f64, size: f64, area: Area, wins: &[WinRect]) -> Vec<Ledge> {
    let mut out = Vec::new();
    for (i, w) in wins.iter().enumerate() {
        if !ledge_ok(w, area) {
            continue;
        }
        let rise = feet - w.y;
        if rise < HOP_MIN * size || rise > HOP_UP * size || w.y - size < area.1 {
            continue;
        }
        let (lo, hi) = (w.x + EDGE_IN + size / 2.0, w.x + w.w - EDGE_IN - size / 2.0);
        let x = if lo <= hi { cx.clamp(lo, hi) } else { w.x + w.w / 2.0 };
        let x = x.clamp(area.0 + size / 2.0, area.0 + area.2 - size / 2.0);
        if (x - cx).abs() > HOP_X * size || !ledge_visible(wins, i, x) {
            continue;
        }
        out.push(Ledge { x, y: w.y, win: w.id });
    }
    out
}

/// Спрятаться: левый верх головы за ближним боковым краем области (на виду
/// keep(size)) и куда смотреть потом — к экрану (1 — вправо)
pub fn hide_x(cx: f64, size: f64, area: Area) -> (f64, i8) {
    if cx < area.0 + area.2 / 2.0 {
        (area.0 - size + keep(size), 1)
    } else {
        (area.0 + area.2 - keep(size), -1)
    }
}

/// Прыжок по дуге: середина головы и ноги в доле t пути из (x0, f0) в (x1, f1),
/// lift — насколько дуга выше прямой
pub fn arc_point(x0: f64, f0: f64, x1: f64, f1: f64, lift: f64, t: f64) -> (f64, f64) {
    let t = t.clamp(0.0, 1.0);
    (x0 + (x1 - x0) * t, f0 + (f1 - f0) * t - 4.0 * lift * t * (1.0 - t))
}

/// Высота дуги и длительность прыжка из (x0, f0) в (x1, f1): в конце дуга
/// идёт вниз — приземляется на край сверху
pub fn arc_shape(x0: f64, f0: f64, x1: f64, f1: f64, size: f64) -> (f64, f64) {
    let lift = (f0 - f1).max(0.0) / 2.0 + size * 0.35;
    let dur = (0.5 + (x1 - x0).hypot(f1 - f0) / 1500.0).clamp(0.5, 0.9);
    (lift, dur)
}

fn sign(v: f64) -> i8 {
    if v > 0.5 {
        1
    } else if v < -0.5 {
        -1
    } else {
        0
    }
}

// ── Состояние и события ────────────────────────────────────────────────

#[derive(Default)]
pub struct Motion {
    /// Номер движения: новое гасит прежнее
    gen: AtomicU64,
    /// Идёт, падает или прыгает (стоит на окне — нет): место не поправлять
    busy: AtomicBool,
    /// В полёте (падает, прыгает) — «Иди сюда» и «стоп» не перебивают
    air: AtomicBool,
    /// Ушла за край экрана: куда смотреть — к экрану (1 — вправо)
    hidden: Mutex<Option<i8>>,
    /// Стоит на окне (id) — следит за ним
    perch: Mutex<Option<u64>>,
    /// Что последним сказали странице
    last: Mutex<Option<(&'static str, i8)>>,
    /// У персоны есть своя анимация шагов (от страницы, portrait_walker)
    walker: AtomicBool,
    /// Шаги из кадров играющей анимации шагов (portrait_gait)
    gait: Mutex<Gait>,
    /// Подглядывает из-за края: где стояла голова до того (вернуть)
    peek: Mutex<Option<(f64, f64)>>,
    seed: AtomicU64,
}

/// Окно идёт шагами фигуры из кадров: on — страница их шлёт, owed — сколько
/// ещё пройти (px), at — последний шаг
#[derive(Default)]
struct Gait {
    on: bool,
    owed: f64,
    at: Option<Instant>,
}

#[derive(Clone, Serialize)]
struct MotionEvent {
    state: &'static str,
    dir: i8,
    #[serde(skip_serializing_if = "Option::is_none")]
    speed: Option<f64>,
}

impl Motion {
    pub fn busy(&self) -> bool {
        self.busy.load(Ordering::SeqCst)
    }

    pub fn hidden(&self) -> bool {
        lk(&self.hidden).is_some()
    }

    /// Есть ли своя анимация шагов: без неё сама не гуляет
    pub fn set_walker(&self, on: bool) {
        self.walker.store(on, Ordering::SeqCst);
    }

    fn walker(&self) -> bool {
        self.walker.load(Ordering::SeqCst)
    }

    /// Шаги из кадров: Some(px) — сменился кадр, пройти ещё px (0 — шаги
    /// будут, пока стоит первый кадр); None — шагов из кадров нет
    pub fn gait(&self, step: Option<f64>) {
        let mut g = lk(&self.gait);
        match step {
            Some(px) if px.is_finite() => {
                g.on = true;
                g.owed += px.clamp(-GAIT_STEP_MAX, GAIT_STEP_MAX);
                g.at = Some(Instant::now());
            }
            _ => *g = Gait::default(),
        }
    }

    /// Сколько пройти на этом тике: шагами из кадров (Some — накопленное,
    /// между сменами кадров 0) или ровно (None)
    fn gait_take(&self) -> Option<f64> {
        let mut g = lk(&self.gait);
        if !g.on || g.at.map(|t| t.elapsed() > GAIT_STALE).unwrap_or(true) {
            return None;
        }
        Some(std::mem::take(&mut g.owed))
    }

    /// Погасить текущее движение (и слежение за окном под ногами)
    pub fn cancel(&self) {
        self.gen.fetch_add(1, Ordering::SeqCst);
        self.busy.store(false, Ordering::SeqCst);
        self.air.store(false, Ordering::SeqCst);
        *lk(&self.perch) = None;
        *lk(&self.peek) = None;
    }

    /// Случайное число от 0 до 1 (xorshift; зерно — часы)
    fn rnd(&self) -> f64 {
        let mut s = self.seed.load(Ordering::Relaxed);
        if s == 0 {
            s = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos() as u64).unwrap_or(0x9E37) | 1;
        }
        s ^= s << 13;
        s ^= s >> 7;
        s ^= s << 17;
        self.seed.store(s, Ordering::Relaxed);
        (s >> 11) as f64 / (1u64 << 53) as f64
    }
}

/// Странице окна p — новое состояние (повтор того же — нет; land — всегда)
fn say(app: &AppHandle, p: &Portrait, state: &'static str, dir: i8, speed: Option<f64>) {
    let m = &p.motion;
    {
        let mut last = lk(&m.last);
        if speed.is_none() && *last == Some((state, dir)) {
            return;
        }
        *last = Some((state, dir));
    }
    p.emit(app, "portrait-motion", MotionEvent { state, dir, speed });
}

/// Скорость по опоре, px/s: шаг (больше голова — быстрее); своих шагов нет
/// — переезд в DASH_K раз быстрее; glide — без физики к курсору
fn ground_speed(size: f64, walker: bool, glide: bool) -> f64 {
    let step = WALK_SPEED * size / SIZE_REF;
    if !walker {
        step * DASH_K
    } else if glide {
        step * GLIDE_K
    } else {
        step
    }
}

// ── Движение ───────────────────────────────────────────────────────────

#[derive(Clone, Copy, Debug, PartialEq)]
enum Mode {
    Ground,
    Air,
    /// Прыжок: середина и ноги в начале и в конце, высота дуги, длительность, доля пути
    Arc { x0: f64, f0: f64, x1: f64, f1: f64, lift: f64, dur: f64, t: f64 },
}

#[derive(Clone, Copy, Debug, PartialEq)]
enum Goal {
    /// Стоит (на окне — следит за ним)
    Stay,
    /// Идёт: середина головы — к x. free — не держаться в пределах экрана
    /// (прячется и выходит); hide — дойдя, спряталась (куда смотреть);
    /// hop_to — дойдя, прыгнуть на окно под этой точкой («Иди сюда»)
    Walk { x: f64, free: bool, hide: Option<i8>, hop_to: Option<(f64, f64)> },
    /// Без физики «Иди сюда»: голова — в (hx, hy) напрямую
    Glide { hx: f64, hy: f64 },
}

enum Start {
    Fall { vx: f64, vy: f64 },
    Hop { x: f64, y: f64 },
    Go(Goal),
    Watch,
}

/// Начать движение: прежнее гаснет. false — окна нет или голову тащат
fn launch(app: &AppHandle, p: &Portrait, start: Start) -> bool {
    let p = p.clone();
    let Some(w) = p.window(app) else { return false };
    if p.dragging.load(Ordering::SeqCst) {
        return false;
    }
    let pl = p.placement();
    let Some((hx, hy, s)) = head_pos(&w, &pl) else { return false };
    let size = pl.size;
    let m = &p.motion;
    m.cancel();
    let my = m.gen.load(Ordering::SeqCst);
    let mut b = Body { hx, hy, vx: 0.0, vy: 0.0 };
    let (mode, goal) = match start {
        Start::Fall { vx, vy } => {
            b.vx = vx;
            b.vy = vy;
            (Mode::Air, Goal::Stay)
        }
        Start::Hop { x, y } => {
            let (x0, f0) = (hx + size / 2.0, hy + size);
            let (lift, dur) = arc_shape(x0, f0, x, y, size);
            (Mode::Arc { x0, f0, x1: x, f1: y, lift, dur, t: 0.0 }, Goal::Stay)
        }
        Start::Go(goal) => (Mode::Ground, goal),
        Start::Watch => (Mode::Ground, Goal::Stay),
    };
    if !matches!(goal, Goal::Stay) || mode != Mode::Ground {
        m.busy.store(true, Ordering::SeqCst);
        // Пошла куда-то — уже не спрятана
        *lk(&m.hidden) = None;
    }
    m.air.store(mode != Mode::Ground, Ordering::SeqCst);
    let app = app.clone();
    std::thread::spawn(move || run(app, w, p, my, b, mode, goal, s, pl.physics));
    true
}

/// Чей край под курсором: окно, на котором курсор (самое переднее), или
/// край под курсором, если курсор над пустым местом
fn under_cursor(ux: f64, uy: f64, area: Area, wins: &[WinRect]) -> Option<u64> {
    wins.iter().find(|w| contains(w, ux, uy)).map(|w| w.id).or_else(|| ground_at(ux, uy, area, wins, true).win)
}

/// Окно под ногами (pw) сейчас: на месте или сдвинуто — новое, ушло (закрыли,
/// свернули, закрыли окном спереди, развернули) — None
fn follow(wins: &[WinRect], pw: &WinRect, cx: f64, area: Area) -> Option<WinRect> {
    let i = wins.iter().position(|w| w.id == pw.id)?;
    let w = wins[i];
    (ledge_ok(&w, area) && ledge_visible(wins, i, cx + (w.x - pw.x))).then_some(w)
}

#[allow(clippy::too_many_arguments)]
fn run(app: AppHandle, w: WebviewWindow, p: Portrait, my: u64, mut b: Body, mut mode: Mode, mut goal: Goal, s: f64, physics: bool) {
    let m = &p.motion;
    let mine = || m.gen.load(Ordering::SeqCst) == my;
    let (mut areas, fallback) = (work_areas(&app), primary_area(&app));
    let mut areas_at = Instant::now();
    let mut wins = if physics { platform::windows(s) } else { Vec::new() };
    let mut wins_at = Instant::now();
    let mut perch: Option<WinRect> = None;
    let mut last = Instant::now();
    let size0 = p.placement().size;
    let pos_of = |b: &Body, size: f64, pl: &super::Placement| {
        let (_, _, dx, dy) = frame(size, pl.bubbles, pl.field(), pl.window_room_for(size));
        (((b.hx - dx) * s).round() as i32, ((b.hy - dy) * s).round() as i32)
    };
    let mut last_pos = pos_of(&b, size0, &p.placement());
    let mut walk_dir: i8 = 0;
    // Шаги из кадров, пришедшие до этого движения, — не его
    let _ = m.gait_take();
    // Когда окно последний раз двигали отсюда: до события Moved настройки
    // ещё со старым местом — не брать его
    let mut set_at: Option<Instant> = None;
    // Окно под ногами только что двигали — следить чаще, пока двигают
    let mut ride_until: Option<Instant> = None;
    // На чём стоит в начале: край окна под ногами
    if physics && mode == Mode::Ground {
        if let Some(area) = area_among(&areas, fallback, b.hx, b.hy, size0) {
            let feet = b.hy + size0;
            let g = ground_at(b.hx + size0 / 2.0, feet, area, &wins, true);
            if (g.y - feet).abs() <= FEET_TOL * 2.0 {
                perch = g.win.and_then(|id| wins.iter().find(|w| w.id == id).copied());
            }
        }
    }
    match mode {
        Mode::Air => say(&app, &p, "fall", sign(b.vx / 30.0), None),
        Mode::Arc { x0, x1, .. } => say(&app, &p, "jump", sign(x1 - x0), None),
        Mode::Ground => {
            if goal == Goal::Stay {
                match perch {
                    Some(pw) => {
                        *lk(&m.perch) = Some(pw.id);
                        say(&app, &p, "perch", 0, None);
                    }
                    // На нижнем крае экрана следить не за чем
                    None => return,
                }
            }
        }
    }
    loop {
        let riding = ride_until.map(|t| t > Instant::now()).unwrap_or(false);
        let tick = match (mode, goal) {
            (Mode::Ground, Goal::Stay) if !riding => TICK_PERCH,
            (Mode::Ground, _) => TICK_WALK,
            _ => TICK_AIR,
        };
        std::thread::sleep(tick);
        if !mine() || p.window(&app).is_none() {
            return;
        }
        let now = Instant::now();
        let dt = (now - last).as_secs_f64().min(0.1);
        last = now;
        let pl = p.placement();
        let size = pl.size;
        if mode == Mode::Ground && goal == Goal::Stay && set_at.map(|t| t.elapsed() > SETTLED).unwrap_or(true) {
            // Стоит на окне: место — из настроек (колёсико, меню могли сдвинуть окно)
            if let (Some(x), Some(y)) = (pl.x, pl.y) {
                b.hx = x;
                b.hy = y;
                last_pos = pos_of(&b, size, &pl);
            }
        }
        if areas_at.elapsed() >= AREAS_EVERY {
            areas = work_areas(&app);
            areas_at = now;
        }
        if physics && (wins_at.elapsed() >= WINS_EVERY || goal == Goal::Stay) {
            wins = platform::windows(s);
            wins_at = now;
        }
        let Some(area) = area_among(&areas, fallback, b.hx, b.hy, size) else { continue };
        let mut landed: Option<Surface> = None;
        match mode {
            Mode::Air => {
                let n = fly(b, dt, size, flight_area(area, size, pl.room));
                let g = ground_at(n.hx + size / 2.0, b.hy + size, area, &wins, physics);
                if n.hy + size >= g.y {
                    say(&app, &p, "land", sign(n.vx / 30.0), Some(n.vy.max(0.0).round()));
                    // Ударилась сильно — один невысокий отскок, вбок скорость гаснет
                    let up = bounce_up(n.vy, size);
                    if up > 0.0 {
                        b = Body { hx: n.hx, hy: g.y - size, vx: n.vx * BOUNCE_X, vy: -up };
                    } else {
                        b = Body { hx: n.hx, hy: g.y - size, vx: 0.0, vy: 0.0 };
                        landed = Some(g);
                    }
                } else {
                    b = n;
                }
            }
            Mode::Arc { x0, f0, x1, f1, lift, dur, t } => {
                let t = (t + dt / dur).min(1.0);
                let (x, f) = arc_point(x0, f0, x1, f1, lift, t);
                b.hx = x - size / 2.0;
                b.hy = f - size;
                mode = Mode::Arc { x0, f0, x1, f1, lift, dur, t };
                if t >= 1.0 {
                    let g = ground_at(x1, f1, area, &wins, physics);
                    if (g.y - f1).abs() <= FEET_TOL * 2.0 {
                        say(&app, &p, "land", sign(x1 - x0), Some(0.0));
                        b.hy = g.y - size;
                        landed = Some(g);
                    } else {
                        // Окно ушло, пока летела — падает дальше
                        mode = Mode::Air;
                        b.vx = 0.0;
                        b.vy = 200.0;
                        say(&app, &p, "fall", 0, None);
                    }
                }
            }
            Mode::Ground => {
                let cx = b.hx + size / 2.0;
                let feet = b.hy + size;
                // Опора: окно под ногами едет — вместе с ним; ушло — падает
                let mut falls = false;
                if physics {
                    match perch {
                        Some(pw) => match follow(&wins, &pw, cx, area) {
                            Some(nw) => {
                                if nw.x != pw.x || nw.y != pw.y {
                                    ride_until = Some(Instant::now() + RIDE_FAST);
                                }
                                b.hx += nw.x - pw.x;
                                b.hy = nw.y - size;
                                perch = Some(nw);
                            }
                            None => falls = true,
                        },
                        None => falls = ground_at(cx, feet, area, &wins, true).y > feet + FALL_GAP,
                    }
                }
                if falls {
                    perch = None;
                    *lk(&m.perch) = None;
                    m.busy.store(true, Ordering::SeqCst);
                    m.air.store(true, Ordering::SeqCst);
                    mode = Mode::Air;
                    let speed = ground_speed(size, m.walker(), false);
                    b.vx = if matches!(goal, Goal::Walk { .. }) { walk_dir as f64 * speed } else { 0.0 };
                    b.vy = 0.0;
                    say(&app, &p, "fall", walk_dir, None);
                } else {
                    match goal {
                        Goal::Walk { x, free, hide, hop_to } => {
                            let speed = ground_speed(size, m.walker(), false);
                            let (lo, hi) = (area.0 + size / 2.0, area.0 + area.2 - size / 2.0);
                            let target = if free { x } else { x.clamp(lo.min(hi), hi.max(lo)) };
                            let d = target - cx;
                            // Шаги из кадров — когда фигура шагнула; иначе ровно
                            let step = m.gait_take().unwrap_or(speed * dt);
                            if d.abs() <= step.max(0.5) {
                                b.hx = target - size / 2.0;
                                goal = Goal::Stay;
                                walk_dir = 0;
                                if let Some(dir) = hide {
                                    *lk(&m.hidden) = Some(dir);
                                    say(&app, &p, "hidden", dir, None);
                                } else if let Some((ux, uy)) = hop_to.filter(|_| physics) {
                                    // «Иди сюда»: курсор на окне (или над его краем) выше — прыгнуть на него
                                    let under = under_cursor(ux, uy, area, &wins);
                                    let ledge = under.and_then(|id| {
                                        hop_targets(target, b.hy + size, size, area, &wins).into_iter().find(|l| l.win == id)
                                    });
                                    if let Some(l) = ledge {
                                        let (x0, f0) = (target, b.hy + size);
                                        let (lift, dur) = arc_shape(x0, f0, l.x, l.y, size);
                                        mode = Mode::Arc { x0, f0, x1: l.x, f1: l.y, lift, dur, t: 0.0 };
                                        m.air.store(true, Ordering::SeqCst);
                                        perch = None;
                                        say(&app, &p, "jump", sign(l.x - x0), None);
                                    } else {
                                        say(&app, &p, if perch.is_some() { "perch" } else { "idle" }, 0, None);
                                    }
                                } else {
                                    say(&app, &p, if perch.is_some() { "perch" } else { "idle" }, 0, None);
                                }
                            } else {
                                walk_dir = sign(d);
                                b.hx += walk_dir as f64 * step;
                                say(&app, &p, "walk", walk_dir, None);
                            }
                        }
                        Goal::Glide { hx, hy } => {
                            let (ddx, ddy) = (hx - b.hx, hy - b.hy);
                            let dist = ddx.hypot(ddy);
                            // Едет ровно: шаги из кадров не копить на потом
                            let _ = m.gait_take();
                            let step = ground_speed(size, m.walker(), true) * dt;
                            if dist <= step.max(0.5) {
                                b.hx = hx;
                                b.hy = hy;
                                goal = Goal::Stay;
                                walk_dir = 0;
                                say(&app, &p, "idle", 0, None);
                            } else {
                                b.hx += ddx / dist * step;
                                b.hy += ddy / dist * step;
                                walk_dir = sign(ddx);
                                say(&app, &p, "walk", walk_dir, None);
                            }
                        }
                        Goal::Stay => {}
                    }
                }
            }
        }
        if let Some(g) = landed {
            mode = Mode::Ground;
            m.air.store(false, Ordering::SeqCst);
            perch = g.win.and_then(|id| wins.iter().find(|w| w.id == id).copied());
            match goal {
                // Шла куда-то и упала — идёт дальше
                Goal::Walk { .. } => say(&app, &p, "walk", walk_dir, None),
                _ => say(&app, &p, if perch.is_some() { "perch" } else { "idle" }, 0, None),
            }
        }
        let pos = pos_of(&b, size, &pl);
        if pos != last_pos {
            last_pos = pos;
            set_at = Some(Instant::now());
            let _ = w.set_position(PhysicalPosition::new(pos.0, pos.1));
        }
        if mode == Mode::Ground && goal == Goal::Stay {
            m.busy.store(false, Ordering::SeqCst);
            match perch {
                // Стоит на окне — следить за ним дальше (тик TICK_PERCH)
                Some(pw) if physics => *lk(&m.perch) = Some(pw.id),
                _ => {
                    *lk(&m.perch) = None;
                    p.fit_soon(&w);
                    return;
                }
            }
        }
    }
}

// ── Команды ────────────────────────────────────────────────────────────

/// Где голова, масштаб, размер, область монитора и окна (с физикой) — для выбора цели
fn here(app: &AppHandle, p: &Portrait) -> Option<(Portrait, f64, f64, f64, f64, Area, Vec<WinRect>, bool)> {
    let p = p.clone();
    let w = p.window(app)?;
    let pl = p.placement();
    let (hx, hy, s) = head_pos(&w, &pl)?;
    let area = area_among(&work_areas(app), primary_area(app), hx, hy, pl.size)?;
    let wins = if pl.physics { platform::windows(s) } else { Vec::new() };
    Some((p, hx, hy, s, pl.size, area, wins, pl.physics))
}

/// С физикой: висит в воздухе — падает на опору; стоит на окне — следить за
/// ним. Зовут после загрузки страницы, включения физики, остановки
pub fn settle(app: &AppHandle, p: &Portrait) {
    if p.dragging.load(Ordering::SeqCst) || p.motion.busy() || lk(&p.motion.perch).is_some() {
        return;
    }
    let Some((_, hx, hy, _, size, area, wins, physics)) = here(app, p) else { return };
    if !physics {
        return;
    }
    let feet = hy + size;
    let g = ground_at(hx + size / 2.0, feet, area, &wins, true);
    if feet < g.y - 2.0 {
        launch(app, p, Start::Fall { vx: 0.0, vy: 0.0 });
    } else if g.win.is_some() && (g.y - feet).abs() <= FEET_TOL * 2.0 {
        launch(app, p, Start::Watch);
    }
}

/// Прогулка по текущей опоре. false — своих шагов нет, некуда или занята
pub fn stroll(app: &AppHandle, p: &Portrait) -> bool {
    if !p.motion.walker() || p.dragging.load(Ordering::SeqCst) || p.motion.busy() || p.motion.hidden() {
        return false;
    }
    let Some((p, hx, hy, _, size, area, wins, physics)) = here(app, p) else { return false };
    let (cx, feet) = (hx + size / 2.0, hy + size);
    let mut span = (area.0, area.0 + area.2);
    if physics {
        let g = ground_at(cx, feet, area, &wins, true);
        if g.y > feet + FALL_GAP {
            settle(app, &p);
            return false;
        }
        if let Some(i) = g.win.and_then(|id| wins.iter().position(|w| w.id == id)) {
            span = ledge_span(&wins, i, cx);
        }
    }
    let Some(x) = stroll_target(cx, span, size, p.motion.rnd(), p.motion.rnd()) else { return false };
    launch(app, &p, Start::Go(Goal::Walk { x, free: false, hide: None, hop_to: None }))
}

/// Прыжок на край окна поблизости. false — физика выключена, окон нет, занята
pub fn hop(app: &AppHandle, p: &Portrait) -> bool {
    if p.dragging.load(Ordering::SeqCst) || p.motion.busy() || p.motion.hidden() {
        return false;
    }
    let Some((p, hx, hy, _, size, area, wins, physics)) = here(app, p) else { return false };
    if !physics {
        return false;
    }
    let targets = hop_targets(hx + size / 2.0, hy + size, size, area, &wins);
    if targets.is_empty() {
        return false;
    }
    let l = targets[((p.motion.rnd() * targets.len() as f64) as usize).min(targets.len() - 1)];
    launch(app, &p, Start::Hop { x: l.x, y: l.y })
}

/// «Иди сюда»: к курсору. С физикой — по опоре к x курсора (падает с края
/// окна, если надо), курсор над окном выше — в конце прыгает на него; без
/// физики — напрямую, голова серединой под курсор
pub fn come(app: &AppHandle, p: &Portrait) {
    if p.dragging.load(Ordering::SeqCst) || p.motion.air.load(Ordering::SeqCst) {
        return;
    }
    let Some((p, _, _, s, size, area, _, physics)) = here(app, p) else { return };
    let Ok(c) = app.cursor_position() else { return };
    let (ux, uy) = (c.x / s, c.y / s);
    let hidden = p.motion.hidden();
    if physics {
        launch(app, &p, Start::Go(Goal::Walk { x: ux, free: hidden, hide: None, hop_to: Some((ux, uy)) }));
    } else {
        let cursor_area = area_among(&work_areas(app), Some(area), ux - size / 2.0, uy - size / 2.0, size).unwrap_or(area);
        let (gx, gy) = fit(cursor_area, ux - size / 2.0, uy - size / 2.0, size);
        launch(app, &p, Start::Go(Goal::Glide { hx: gx, hy: gy }));
    }
}

/// «Спрячься» — к ближнему боковому краю и за него (на виду keep(size));
/// уже спрятана — «Выгляни»: на size внутрь
pub fn hide_toggle(app: &AppHandle, p: &Portrait) {
    if p.dragging.load(Ordering::SeqCst) || p.motion.air.load(Ordering::SeqCst) {
        return;
    }
    let Some((p, hx, _, _, size, area, _, _)) = here(app, p) else { return };
    let cx = hx + size / 2.0;
    let hidden = *lk(&p.motion.hidden);
    let goal = match hidden {
        Some(dir) => Goal::Walk { x: cx + dir as f64 * size, free: true, hide: None, hop_to: None },
        None => {
            let (tx, dir) = hide_x(cx, size, area);
            Goal::Walk { x: tx + size / 2.0, free: true, hide: Some(dir), hop_to: None }
        }
    };
    launch(app, &p, Start::Go(goal));
}

/// Где встать, чтобы подглядывать: линия (off — px от левого края головы, у
/// top — от верха; off_m — то же у отражённой) на краю области. Сбоку — у
/// ближнего к home края (нарисована для другого — отражённая), по высоте —
/// где была; сверху — по горизонтали где была. (hx, hy, край, отражена)
pub fn peek_spot(side: &str, off: f64, off_m: f64, home: (f64, f64), size: f64, area: Area) -> Option<(f64, f64, &'static str, bool)> {
    if !off.is_finite() || !off_m.is_finite() {
        return None;
    }
    let (ax, ay, aw, ah) = area;
    match side {
        "top" => Some((home.0.clamp(ax, (ax + aw - size).max(ax)), ay - off, "top", false)),
        "left" | "right" => {
            let near_left = home.0 + size / 2.0 < ax + aw / 2.0;
            let want = if near_left { "left" } else { "right" };
            let mirror = want != side;
            let edge = if near_left { ax } else { ax + aw };
            let y = home.1.clamp(ay, (ay + ah - size).max(ay));
            Some((edge - if mirror { off_m } else { off }, y, want, mirror))
        }
        _ => None,
    }
}

#[derive(Clone, Serialize)]
pub struct Peeked {
    side: &'static str,
    mirror: bool,
}

fn put_head(app: &AppHandle, p: &Portrait, hx: f64, hy: f64) {
    let Some(w) = p.window(app) else { return };
    let pl = p.placement();
    let (_, _, dx, dy) = frame(pl.size, pl.bubbles, pl.field(), pl.window_room());
    let _ = w.set_position(LogicalPosition::new(hx - dx, hy - dy));
}

/// Подглядывать из-за края (см. peek_spot). Уже подглядывает (следующая
/// такая же анимация) — возвращаться всё равно на прежнее место. В воздухе
/// или в руке — None: страница эту анимацию не играет
pub fn peek(app: &AppHandle, p: &Portrait, side: &str, off: f64, off_m: f64) -> Option<Peeked> {
    if p.dragging.load(Ordering::SeqCst) || p.motion.air.load(Ordering::SeqCst) {
        return None;
    }
    let (p, hx, hy, _, size, area, _, _) = here(app, p)?;
    let home = lk(&p.motion.peek).unwrap_or((hx, hy));
    let (tx, ty, side, mirror) = peek_spot(side, off, off_m, home, size, area)?;
    p.motion.cancel();
    *lk(&p.motion.peek) = Some(home);
    p.motion.busy.store(true, Ordering::SeqCst);
    put_head(app, &p, tx, ty);
    Some(Peeked { side, mirror })
}

/// Подглядывание кончилось: назад, где стояла, и на опору. Не подглядывала
/// (или другое движение уже увело) — ничего
pub fn peek_end(app: &AppHandle, p: &Portrait) {
    let Some((hx, hy)) = lk(&p.motion.peek).take() else { return };
    p.motion.busy.store(false, Ordering::SeqCst);
    put_head(app, p, hx, hy);
    // Окно переедет в главном потоке — опору искать уже от нового места
    std::thread::sleep(Duration::from_millis(40));
    settle(app, p);
}

/// Остановить ходьбу (падение и прыжок — нет)
pub fn stop(app: &AppHandle, p: &Portrait) {
    if p.motion.air.load(Ordering::SeqCst) || !p.motion.busy() {
        return;
    }
    p.motion.cancel();
    say(app, p, "idle", 0, None);
    settle(app, p);
}

/// Физику выключили — движение и слежение за окном гаснут; включили — на опору
pub fn physics_changed(app: &AppHandle, p: &Portrait, on: bool) {
    if on {
        settle(app, p);
    } else {
        p.motion.cancel();
        say(app, p, "idle", 0, None);
    }
}

/// Голову схватили: движение гаснет, уже не спрятана
pub fn grabbed(app: &AppHandle, p: &Portrait) {
    p.motion.cancel();
    *lk(&p.motion.hidden) = None;
    say(app, p, "held", 0, None);
}

/// Точки перетаскивания для скорости броска: (секунды от начала, hx, hy)
pub struct Throw {
    t0: Instant,
    samples: VecDeque<(f64, f64, f64)>,
}

impl Throw {
    pub fn new() -> Self {
        Throw { t0: Instant::now(), samples: VecDeque::with_capacity(16) }
    }

    pub fn push(&mut self, hx: f64, hy: f64) {
        if self.samples.len() >= 16 {
            self.samples.pop_front();
        }
        self.samples.push_back((self.t0.elapsed().as_secs_f64(), hx, hy));
    }

    fn velocity(&self) -> (f64, f64) {
        let v: Vec<_> = self.samples.iter().copied().collect();
        release_velocity(&v, self.t0.elapsed().as_secs_f64())
    }
}

/// Ноги на опоре (с допуском) — стоит, а не висит
pub fn resting(feet: f64, ground: f64) -> bool {
    (ground - feet).abs() <= FEET_TOL * 2.0
}

/// Отпустили: с физикой — бросок (toss) и падение на опору, без — стоит.
/// Поставили на опору и не подкинули — не падает (без мелькания «падает» на
/// один кадр): стоит, на окне — следит за ним
pub fn released(app: &AppHandle, p: &Portrait, throw: &Throw) {
    let pl = p.placement();
    if !pl.physics {
        say(app, p, "idle", 0, None);
        return;
    }
    let (mx, my) = throw.velocity();
    let (vx, vy) = toss(mx, my, pl.size);
    // dragging ещё стоит — снять до запуска
    p.dragging.store(false, Ordering::SeqCst);
    if vy >= 0.0 {
        if let Some((_, hx, hy, _, size, area, wins, _)) = here(app, p) {
            let feet = hy + size;
            if resting(feet, ground_at(hx + size / 2.0, feet, area, &wins, true).y) {
                say(app, p, "idle", 0, None);
                settle(app, p);
                return;
            }
        }
    }
    launch(app, p, Start::Fall { vx, vy });
}

#[cfg(test)]
mod tests {
    use super::*;

    const AREA: Area = (0.0, 33.0, 1512.0, 949.0); // низ — 982

    fn win(id: u64, x: f64, y: f64, w: f64, h: f64) -> WinRect {
        WinRect { id, x, y, w, h }
    }

    #[test]
    fn ground_floor_and_ledges() {
        // Окно 2 спереди (первое в списке), окно 1 — за ним
        let wins = [win(2, 400.0, 300.0, 600.0, 400.0), win(1, 100.0, 500.0, 600.0, 400.0)];
        // Без физики — только низ экрана
        assert_eq!(ground_at(500.0, 100.0, AREA, &wins, false).y, 982.0);
        // Сверху над окном 2 — его край
        let g = ground_at(500.0, 100.0, AREA, &wins, true);
        assert_eq!((g.y, g.win), (300.0, Some(2)));
        // Над краем окна 1 там, где его не закрывает окно 2
        let g = ground_at(200.0, 100.0, AREA, &wins, true);
        assert_eq!((g.y, g.win), (500.0, Some(1)));
        // Край окна 1 под окном 2 (x 500, y 502 — внутри окна 2) — не опора:
        // под ногами ниже края окна 2 — падает дальше, на низ экрана
        assert_eq!(ground_at(500.0, 400.0, AREA, &wins, true).y, 982.0);
        // Край чуть выше ног (допуск) — ещё опора; намного выше — нет
        assert_eq!(ground_at(200.0, 503.0, AREA, &wins, true).y, 500.0);
        assert_eq!(ground_at(200.0, 520.0, AREA, &wins, true).y, 982.0);
        // У самого угла окна (EDGE_IN) — не опора
        assert_eq!(ground_at(104.0, 100.0, AREA, &wins, true).y, 982.0);
        // Мелкое окно и развёрнутое (верх у верха области) — не опора
        let wins = [win(3, 100.0, 400.0, 100.0, 40.0), win(4, 0.0, 34.0, 1512.0, 900.0)];
        assert_eq!(ground_at(150.0, 100.0, AREA, &wins, true).y, 982.0);
        assert_eq!(ground_at(700.0, 0.0, AREA, &wins, true).y, 982.0);
    }

    #[test]
    fn ledge_span_ends_at_cover() {
        // Окно 2 спереди закрывает край окна 1 с x = 400
        let wins = [win(2, 400.0, 300.0, 600.0, 400.0), win(1, 100.0, 500.0, 600.0, 400.0)];
        let (l, r) = ledge_span(&wins, 1, 200.0);
        assert!(l >= 108.0 && l < 116.0, "{l}");
        assert!(r < 400.0 && r >= 392.0, "{r}");
    }

    #[test]
    fn falling_and_bounce() {
        // Падает вниз, по x скорость гаснет
        let b = Body { hx: 500.0, hy: 100.0, vx: 600.0, vy: 0.0 };
        let n = fly(b, 0.016, 240.0, AREA);
        assert!(n.vy > 0.0 && n.hy > b.hy && n.vx < b.vx && n.hx > b.hx);
        // Предел скорости
        let n = fly(Body { vy: 2390.0, ..b }, 0.1, 240.0, AREA);
        assert_eq!(n.vy, V_MAX);
        // Отскок от правого края — назад с половиной скорости
        let n = fly(Body { hx: 1270.0, hy: 100.0, vx: 1000.0, vy: 0.0 }, 0.016, 240.0, AREA);
        assert_eq!(n.hx, 1512.0 - 240.0);
        assert!(n.vx < 0.0 && n.vx.abs() < 500.0);
        // От левого
        let n = fly(Body { hx: 2.0, hy: 100.0, vx: -1000.0, vy: 0.0 }, 0.016, 240.0, AREA);
        assert_eq!(n.hx, 0.0);
        assert!(n.vx > 0.0);
        // Брошена вверх выше экрана — ударилась о верх области и вниз, без прилипания
        let n = fly(Body { hx: 500.0, hy: 40.0, vx: 0.0, vy: -2000.0 }, 0.016, 240.0, AREA);
        assert_eq!(n.hy, 33.0);
        assert!(n.vy > 0.0 && n.vy < 500.0, "{}", n.vy);
    }

    #[test]
    fn thrown_sprite_stays_inside_walls_and_ceiling() {
        // Текущий кадр падения Дон: 416×372, ×1.75, пол на 44 px выше низа.
        // С квадратом головы вместо кадра у стены пропадало до 192 px.
        let room = super::super::Room { kl: 0.875, kr: 0.875, ky: 328.0 * 1.75 / 416.0, kb: 44.0 * 1.75 / 416.0 };
        let area = (80.0, -300.0, 1920.0, 1600.0);
        for size in [196.0, 240.0, 512.0] {
            let bounds = flight_area(area, size, room);
            let width = (size * 1.75_f64).round();
            let height = (size * 1.75 * 372.0 / 416.0_f64).round();
            let sink = height * 44.0 / 372.0;
            for (hx, vx) in [(area.0, -900.0), (area.0 + area.2 - size, 900.0)] {
                let n = fly(Body { hx, hy: area.1, vx, vy: -1800.0 }, 0.016, size, bounds);
                let cx = n.hx + size / 2.0;
                assert!(cx - width / 2.0 >= area.0 && cx + width / 2.0 <= area.0 + area.2, "{n:?}");
                assert!(n.hy + size + sink - height >= area.1, "{n:?}");
                assert!(n.vx * vx < 0.0 && n.vy > 0.0, "{n:?}");
            }
        }
        // В середине экрана траектория и скорость остаются прежними.
        let b = Body { hx: 800.0, hy: 200.0, vx: 250.0, vy: -400.0 };
        let old = fly(b, 0.016, 196.0, area);
        let new = fly(b, 0.016, 196.0, flight_area(area, 196.0, room));
        assert_eq!((new.hx, new.hy, new.vx, new.vy), (old.hx, old.hy, old.vx, old.vy));
        assert_eq!(flight_area(area, 196.0, super::super::Room::default()), area);
    }

    #[test]
    fn sprite_wider_than_screen_does_not_oscillate() {
        let area = (100.0, 33.0, 500.0, 900.0);
        let room = super::super::Room { kl: 1.0, kr: 2.0, ..super::super::Room::default() };
        let bounds = flight_area(area, 240.0, room);
        let mut b = Body { hx: 500.0, hy: 200.0, vx: 900.0, vy: 0.0 };
        for _ in 0..10 {
            b = fly(b, 0.016, 240.0, bounds);
            // Середина кадра, включая несимметричный якорь, — середина монитора.
            assert_eq!(b.hx + 120.0 + 120.0, area.0 + area.2 / 2.0);
            assert_eq!(b.vx, 0.0);
        }
    }

    /// Подкинули со скоростью мыши (vx, vy): до какой высоты над местом броска
    /// поднимется голова (шаги fly по 16 мс, экран высокий — потолка нет)
    fn apex(vx: f64, vy: f64, size: f64) -> f64 {
        let area: Area = (0.0, -100_000.0, 100_000.0, 200_000.0);
        let (tx, ty) = toss(vx, vy, size);
        let mut b = Body { hx: 50_000.0, hy: 0.0, vx: tx, vy: ty };
        let mut top: f64 = 0.0;
        for _ in 0..400 {
            b = fly(b, 0.016, size, area);
            top = top.min(b.hy);
        }
        -top
    }

    #[test]
    fn toss_is_gentle() {
        // Резкий взмах мышью вверх — не выше своей высоты (было ~1700 px)
        for size in [120.0, 240.0, 400.0] {
            let h = apex(0.0, -THROW_MAX, size);
            assert!(h <= size * TOSS_UP * 1.02 && h > size * 0.8, "size {size}: {h}");
        }
        // Лёгкий подброс — невысоко, пропорционально
        let h = apex(0.0, -800.0, 240.0);
        assert!(h > 15.0 && h < 60.0, "{h}");
        // Почти неподвижная мышь — просто отпустили
        assert_eq!(toss(100.0, -150.0, 240.0), (0.0, 0.0));
        // Вбок — половина скорости мыши, не быстрее TOSS_X_MAX; вниз — как есть (×K)
        assert_eq!(toss(1000.0, 0.0, 240.0), (500.0, 0.0));
        assert_eq!(toss(-3000.0, 0.0, 240.0).0, -1500.0);
        assert_eq!(toss(0.0, 2000.0, 240.0), (0.0, 1000.0));
    }

    #[test]
    fn peek_spots() {
        let area = (0.0, 25.0, 1000.0, 700.0);
        // Сбоку у левого края: линия (60 px от левого края головы) — на краю
        assert_eq!(peek_spot("left", 60.0, 140.0, (100.0, 500.0, ), 200.0, area), Some((-60.0, 500.0, "left", false)));
        // Нарисована для левого, а ближний — правый: отражённая (off_m)
        assert_eq!(peek_spot("left", 60.0, 140.0, (800.0, 500.0), 200.0, area), Some((860.0, 500.0, "right", true)));
        assert_eq!(peek_spot("right", 150.0, 50.0, (800.0, 500.0), 200.0, area), Some((850.0, 500.0, "right", false)));
        // Сверху: линия (90 px от верха головы) на верхнем крае области, по x — где была
        assert_eq!(peek_spot("top", 90.0, 90.0, (300.0, 500.0), 200.0, area), Some((300.0, -65.0, "top", false)));
        // Была за краем — по горизонтали в области
        assert_eq!(peek_spot("top", 90.0, 90.0, (-150.0, 500.0), 200.0, area).map(|v| v.0), Some(0.0));
        assert_eq!(peek_spot("bottom", 1.0, 1.0, (0.0, 0.0), 200.0, area), None);
        assert_eq!(peek_spot("left", f64::NAN, 1.0, (0.0, 0.0), 200.0, area), None);
    }

    #[test]
    fn resting_on_ground() {
        // Ноги на опоре или чуть мимо (округления) — стоит; выше или глубже — нет
        assert!(resting(982.0, 982.0));
        assert!(resting(978.0, 982.0) && resting(986.0, 982.0));
        assert!(!resting(950.0, 982.0));
        assert!(!resting(1000.0, 982.0));
    }

    #[test]
    fn landing_bounce() {
        // Мягко — без отскока
        assert_eq!(bounce_up(BOUNCE_MIN - 1.0, 240.0), 0.0);
        // Сильно — отскок, но не выше BOUNCE_UP своей высоты
        let up = bounce_up(V_MAX, 240.0);
        assert!(up > 0.0);
        assert!(up * up / (2.0 * G) <= 240.0 * BOUNCE_UP + 0.01);
        // Второй удар (со скоростью отскока) — уже без отскока: один подскок, не мячик
        assert_eq!(bounce_up(up, 240.0), 0.0);
        assert_eq!(bounce_up(bounce_up(V_MAX, 400.0), 400.0), 0.0);
    }

    #[test]
    fn falls_to_ground() {
        // Брошена над окном: падает на его край, не насквозь
        let wins = [win(1, 100.0, 500.0, 600.0, 400.0)];
        let size = 240.0;
        let mut b = Body { hx: 280.0, hy: 0.0, vx: 0.0, vy: 0.0 };
        let mut landed = None;
        for _ in 0..300 {
            let n = fly(b, 0.016, size, AREA);
            let g = ground_at(n.hx + size / 2.0, b.hy + size, AREA, &wins, true);
            if n.hy + size >= g.y {
                landed = Some(g);
                break;
            }
            b = n;
        }
        assert_eq!(landed.map(|g| (g.y, g.win)), Some((500.0, Some(1))));
    }

    #[test]
    fn gait_steps_from_frames() {
        let m = Motion::default();
        // Шагов из кадров нет — ровно
        assert_eq!(m.gait_take(), None);
        // Первый кадр: шаги будут, пока стоим
        m.gait(Some(0.0));
        assert_eq!(m.gait_take(), Some(0.0));
        // Два кадра сменились между тиками — оба шага разом, потом снова стоит
        m.gait(Some(40.0));
        m.gait(Some(12.5));
        assert_eq!(m.gait_take(), Some(52.5));
        assert_eq!(m.gait_take(), Some(0.0));
        // Безумный шаг — не дальше предела
        m.gait(Some(1e9));
        assert_eq!(m.gait_take(), Some(GAIT_STEP_MAX));
        // Шаги ушли (не шагает) — снова ровно; NaN — тоже
        m.gait(None);
        assert_eq!(m.gait_take(), None);
        m.gait(Some(f64::NAN));
        assert_eq!(m.gait_take(), None);
    }

    #[test]
    fn speed_without_steps() {
        // Свои шаги — шаг (больше голова — быстрее), без физики к курсору — glide
        assert_eq!(ground_speed(SIZE_REF, true, false), WALK_SPEED);
        assert_eq!(ground_speed(SIZE_REF * 2.0, true, false), WALK_SPEED * 2.0);
        assert_eq!(ground_speed(SIZE_REF, true, true), WALK_SPEED * GLIDE_K);
        // Своих шагов нет — быстрый переезд, и по опоре, и к курсору
        assert_eq!(ground_speed(SIZE_REF, false, false), WALK_SPEED * DASH_K);
        assert_eq!(ground_speed(SIZE_REF, false, true), WALK_SPEED * DASH_K);
        // Пока страница не сказала — шагов нет: сама не гуляет
        let m = Motion::default();
        assert!(!m.walker());
        m.set_walker(true);
        assert!(m.walker());
    }

    #[test]
    fn throw_speed() {
        // Точки старше 80 мс не в счёт: 100 px за 0.08 с — 1250 px/s
        let s = [(0.0, 0.0, 0.0), (0.92, 0.0, 0.0), (1.0, 100.0, 0.0)];
        let (vx, vy) = release_velocity(&s, 1.0);
        assert!((vx - 1250.0).abs() < 1.0 && vy == 0.0, "{vx}");
        let s = [(0.95, 0.0, 0.0), (1.0, 100.0, 0.0)];
        let (vx, vy) = release_velocity(&s, 1.0);
        assert!((vx - 2000.0).abs() < 1e-6 && vy == 0.0, "{vx}");
        // Мышь стояла перед тем, как отпустили, — без броска
        assert_eq!(release_velocity(&s, 1.5), (0.0, 0.0));
        // Не быстрее THROW_MAX
        let s = [(0.99, 0.0, 0.0), (1.0, 500.0, 0.0)];
        let (vx, _) = release_velocity(&s, 1.0);
        assert!((vx - THROW_MAX).abs() < 1e-6);
        assert_eq!(release_velocity(&[], 1.0), (0.0, 0.0));
    }

    #[test]
    fn stroll_targets() {
        let span = (0.0, 1512.0);
        // Вправо на 150..600
        let t = stroll_target(700.0, span, 240.0, 0.0, 0.9).unwrap();
        assert_eq!(t, 850.0);
        // Влево на 600 не помещается (голова — 120 от края) — вправо
        assert_eq!(stroll_target(700.0, span, 240.0, 1.0, 0.1), Some(1300.0));
        // Узкий край окна: только к дальнему краю
        let t = stroll_target(300.0, (100.0, 600.0), 240.0, 1.0, 0.9).unwrap();
        assert_eq!(t, 480.0);
        // Совсем узко — некуда
        assert_eq!(stroll_target(300.0, (100.0, 380.0), 240.0, 0.5, 0.5), None);
        // Уже у дальнего края, а ближний — рядом: некуда
        assert_eq!(stroll_target(250.0, (100.0, 400.0), 240.0, 1.0, 0.9), None);
    }

    #[test]
    fn hop_ledges() {
        let size = 240.0;
        // Стоит на низу экрана (ноги 982) у x 700
        let wins = [
            win(1, 500.0, 600.0, 600.0, 300.0), // рядом и выше на 382 — можно
            win(2, 50.0, 100.0, 300.0, 300.0),  // голова над краем ушла бы за верх (100 − 240 < 33) — нет
            win(3, 1300.0, 950.0, 200.0, 200.0), // ниже HOP_MIN — нет
        ];
        let t = hop_targets(700.0, 982.0, size, AREA, &wins);
        assert_eq!(t, vec![Ledge { x: 700.0, y: 600.0, win: 1 }]);
        // Далеко по горизонтали (> 3 голов) — нет
        let wins = [win(1, 1300.0, 600.0, 200.0, 300.0)];
        assert!(hop_targets(100.0, 982.0, size, AREA, &wins).is_empty());
        // Край закрыт окном спереди там, куда прыгать, — нет
        let wins = [win(9, 600.0, 500.0, 300.0, 300.0), win(1, 500.0, 600.0, 600.0, 300.0)];
        let t = hop_targets(700.0, 982.0, size, AREA, &wins);
        assert!(t.iter().all(|l| l.win != 1 || !(600.0..900.0).contains(&l.x)), "{t:?}");
    }

    /// Настоящий список окон этого Mac: только рамки, без заголовков
    /// (cargo test windows_live -- --ignored --nocapture)
    #[test]
    #[ignore]
    fn windows_live() {
        let t = std::time::Instant::now();
        let wins = platform::windows(1.0);
        let took = t.elapsed();
        println!("окон: {}, за {:?}", wins.len(), took);
        let t = std::time::Instant::now();
        for _ in 0..20 {
            platform::windows(1.0);
        }
        println!("повторно: {:?} на вызов", t.elapsed() / 20);
        for w in &wins {
            println!("  {:>8} {:>7.0} {:>7.0} {:>7.0}×{:<7.0}", w.id, w.x, w.y, w.w, w.h);
        }
        assert!(wins.iter().all(|w| w.w >= 1.0 && w.h >= 1.0));
        let area = (0.0, 33.0, 1512.0, 949.0);
        let g = ground_at(756.0, 0.0, area, &wins, true);
        println!("опора под серединой экрана сверху: {g:?}");
    }

    #[test]
    fn rides_the_window() {
        // Стоит на окне 1 (x 100..700, верх 500), середина головы — 400
        let pw = win(1, 100.0, 500.0, 600.0, 400.0);
        // На месте — то же окно
        assert_eq!(follow(&[pw], &pw, 400.0, AREA), Some(pw));
        // Окно сдвинули — новая рамка (едет вместе: страница сдвинет голову на разницу)
        let moved = win(1, 160.0, 420.0, 600.0, 400.0);
        assert_eq!(follow(&[moved], &pw, 400.0, AREA), Some(moved));
        // Закрыли или свернули — падает
        assert_eq!(follow(&[win(2, 0.0, 0.0, 10.0, 10.0)], &pw, 400.0, AREA), None);
        // Закрыли окном спереди точку под ногами — падает
        let front = win(3, 300.0, 450.0, 300.0, 300.0);
        assert_eq!(follow(&[front, pw], &pw, 400.0, AREA), None);
        // Развернули на весь экран — край у верха области, не опора
        assert_eq!(follow(&[win(1, 0.0, 33.0, 1512.0, 949.0)], &pw, 400.0, AREA), None);
    }

    #[test]
    fn cursor_target() {
        // Курсор на окне — его край; над пустым местом — край под курсором;
        // над низом экрана — никакого
        let wins = [win(2, 400.0, 300.0, 600.0, 400.0), win(1, 100.0, 500.0, 600.0, 400.0)];
        assert_eq!(under_cursor(500.0, 600.0, AREA, &wins), Some(2));
        assert_eq!(under_cursor(200.0, 700.0, AREA, &wins), Some(1));
        assert_eq!(under_cursor(200.0, 450.0, AREA, &wins), Some(1));
        assert_eq!(under_cursor(1300.0, 900.0, AREA, &wins), None);
    }

    #[test]
    fn hide_and_arc() {
        // Слева от середины — за левый край, смотреть вправо
        assert_eq!(hide_x(300.0, 240.0, AREA), (-240.0 + 72.0, 1));
        assert_eq!(hide_x(1300.0, 240.0, AREA), (1512.0 - 72.0, -1));
        // Дуга: из начала в конец, в середине — выше прямой; в конце идёт вниз
        let (lift, dur) = arc_shape(700.0, 982.0, 800.0, 600.0, 240.0);
        assert!((0.5..=0.9).contains(&dur));
        assert_eq!(arc_point(700.0, 982.0, 800.0, 600.0, lift, 0.0), (700.0, 982.0));
        assert_eq!(arc_point(700.0, 982.0, 800.0, 600.0, lift, 1.0), (800.0, 600.0));
        let (_, mid) = arc_point(700.0, 982.0, 800.0, 600.0, lift, 0.5);
        assert!(mid < 791.0);
        let (_, before) = arc_point(700.0, 982.0, 800.0, 600.0, lift, 0.97);
        assert!(before < 600.0, "в конце сверху вниз: {before}");
    }
}
