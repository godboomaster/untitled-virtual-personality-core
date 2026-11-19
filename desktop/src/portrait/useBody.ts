import { invoke } from '@tauri-apps/api/core';
import { listen } from './winEvents';
import { useEffect, useRef, useState } from 'react';
import {
  ANIM_SETTINGS_DEFAULT,
  animPlayable,
  animsFor,
  followUp,
  leadIn,
  pickWeighted,
  sleepAnimFor,
  walkFor,
  type AnimTrigger,
  type PortraitAnim,
  type PortraitAnims,
} from '../../../web/src/portrait/animTypes';

/* «Тело» портрета на рабочем столе: что сейчас играет вместо головы.

   Четыре слоя, сверху вниз:
   1. Движение окна (portrait-motion от панели, src-tauri): идёт, держат,
      падает, в прыжке — цикл своей анимации на это (walk, grab, fall, jump).
      Шаги — по направлению (walkFor): нарисованные в эту сторону, иначе
      отражённые из другой. Своей нет: идёт или держат — голова неподвижна
      (ни покачивания, ни раскачки), падает и прыгает — вытягивается и
      поджимается (класс fx у головы, CSS ступеньками — окно поверх всех).
   2. Разовая анимация события (shot): погладили, вернулись, засыпает,
      персона выбрала сама (act), ротация… Своей нет — голова не прыгает и
      не трясётся; только приземление чуть сплющивает её (WAAPI, на месте).
      Зацикленная (anim.loop) идёт по кругу, пока не случится что-то ещё:
      другое событие, движение, персона занялась делом или освободилась.
   3. Выбранная анимация сна (settings.sleep_anim): цикл до пробуждения.
      Событие «засыпает» играет перед ней, один раз со своими связками.
   4. Цикл занятия (ctx): читает брошенный файл, думает над ответом,
      «посиди со мной».

   Ротация: свободна (решает страница: не спит, не говорит, вас нет — нет…)
   и сама ничем не занята — через every_min × [0.5…1.5] минут одно из:
   анимации ротации (по весам), прогулка по экрану (settings.walk и есть
   своя анимация шагов), прыжок на окно (физика). Таймер один, без дела
   ничего не крутится.

   Есть ли шаги — панель знает от страницы (portrait_walker): без них
   «Иди сюда» и «Спрячься» не идут, а быстро переезжают.

   Связки (anim.before, anim.after, anim.chance; animTypes leadIn/followUp):
   разовая начинается — сначала те, что «перед» ней (по шансу), потом она;
   кончилась сама — следом та, что «после» неё. Зацикленная «кончается»,
   когда её выключили той же клавишей. Циклы движения (упала, отпустили) и
   занятия (думает, читает) кончились — их «после» играет разовой; перед
   занятием — его «перед». Перед движением ничего не играет: окно уже едет.
   Анимация в цепочке не повторяется, цепочка не длиннее CHAIN_MAX; другое
   событие перебивает всю цепочку. В цепочке «перед» зацикленная играет
   один раз, иначе до той, ради которой цепочка, дело бы не дошло. */

export type MotionState = 'idle' | 'walk' | 'held' | 'fall' | 'land' | 'jump' | 'hidden' | 'perch';

export interface Motion {
  state: MotionState;
  dir: number; // −1 влево, 1 вправо, 0 — на месте
}

const MOVING: ReadonlySet<MotionState> = new Set(['walk', 'held', 'fall', 'jump']);
const MOTION_TRIGGER: Partial<Record<MotionState, AnimTrigger>> = {
  walk: 'walk',
  held: 'grab',
  fall: 'fall',
  jump: 'jump',
};

/** Движение без своей анимации — класс у головы (идёт и держат — никакого) */
export type BodyFx = 'stretch' | 'tuck';
const MOTION_FX: Partial<Record<MotionState, BodyFx>> = { fall: 'stretch', jump: 'tuck' };

// Разовые эффекты головы, когда своей анимации нет: только сплющить на
// месте (опора — низ). Ни подскока, ни тряски: голову гладят кликами, и
// двойной клик или частые клики не должны её подбрасывать
const SHOT_FX: Partial<Record<AnimTrigger, { frames: Keyframe[]; ms: number }>> = {
  land: {
    frames: [
      { transform: 'scale(1, 1)' },
      { transform: 'scale(1.08, 0.88)', offset: 0.3 },
      { transform: 'scale(0.97, 1.04)', offset: 0.65 },
      { transform: 'scale(1, 1)' },
    ],
    ms: 340,
  },
};

export interface Showing {
  key: number;
  anim: PortraitAnim;
  loop: boolean;
  mirror: boolean;
  /** Шаги: фигура в кадрах уходит вперёд — окно идёт её шагами (portrait_gait) */
  walk: boolean;
}

interface Played {
  key: number;
  anim: PortraitAnim;
  trigger: AnimTrigger | 'act' | 'key' | 'after';
  /** Шаги: отразить (нарисованы в другую сторону) — решено при выборе */
  mirror?: boolean;
  /** Свободна ли была в начале: зацикленная идёт, пока это не сменится */
  calm?: boolean;
  /** Связка: что играть следом (те, что «перед», и та, ради которой они) */
  queue?: PortraitAnim[];
  /** id анимаций этой цепочки — в ней не повторяются */
  seen?: string[];
  /** false — цепочка «перед» занятием: кончилась — идёт оно, без «после» */
  follow?: boolean;
}

/** Идёт по кругу: зацикленная и не в середине цепочки */
const looping = (p: Played) => !!p.anim.loop && !p.queue?.length;

/** Движется в сторону dir, а фигура в кадрах смотрит в другую — отразить */
export function mirrorFor(anim: PortraitAnim, dir: number): boolean {
  return (anim.faces === 'right' && dir < 0) || (anim.faces === 'left' && dir > 0);
}

export function useBody(o: {
  anims: PortraitAnims | null;
  /** «Анимации» в меню: выключены — ни ротации, ни своих анимаций */
  on: boolean;
  physics: boolean;
  asleep: boolean;
  /** Свободна для ротации — с точки зрения страницы */
  free: boolean;
  /** Не спит, не говорит, ничем не занята; в отличие от free, не зависит от мыши */
  calm: boolean;
  loops: { drop: boolean; think: boolean; focus: boolean };
  /** .live-portrait — для разовых эффектов головы */
  headEl: () => Element | null;
}) {
  const [motion, setMotion] = useState<Motion>({ state: 'idle', dir: 0 });
  const motionRef = useRef(motion.state);
  motionRef.current = motion.state;
  const [shot, setShot] = useState<Played | null>(null);
  const [cont, setCont] = useState<Played | null>(null);
  const [ctx, setCtx] = useState<Played | null>(null);
  const [sleep, setSleep] = useState<Showing | null>(null);
  const [rearm, setRearm] = useState(0);
  const key = useRef(0);
  const data = o.on ? o.anims : null;
  const dataRef = useRef(data);
  dataRef.current = data;
  const headRef = useRef(o.headEl);
  headRef.current = o.headEl;
  const calmRef = useRef(o.calm);
  calmRef.current = o.calm;

  const shotRef = useRef(shot);
  shotRef.current = shot;

  const pick = (t: AnimTrigger): PortraitAnim | null => pickWeighted(animsFor(dataRef.current, t));

  /** anim со связкой «перед»: сначала те, потом она */
  const chain = (anim: PortraitAnim, trigger: Played['trigger'], seen = new Set<string>()): Played => {
    const all = [...leadIn(dataRef.current, anim, seen), anim];
    return { key: ++key.current, anim: all[0], trigger, calm: calmRef.current, queue: all.slice(1), seen: [...seen] };
  };

  /** prev кончилась: следующая в цепочке, иначе — связка «после»; нечего — null */
  const after = (prev: Played): Played | null => {
    const rest = [...(prev.queue ?? [])];
    while (rest.length) {
      const id = rest.shift()?.id;
      // Пока шла цепочка, анимацию могли выключить или удалить
      const a = dataRef.current?.anims.find((x) => x.id === id && animPlayable(x));
      if (a) return { ...prev, key: ++key.current, anim: a, queue: rest, calm: calmRef.current };
    }
    if (prev.follow === false) return null;
    const seen = new Set(prev.seen ?? [prev.anim.id]);
    const next = followUp(dataRef.current, prev.anim, seen);
    return next ? chain(next, prev.trigger === 'sleep' ? 'sleep' : 'after', seen) : null;
  };

  /** Цикл движения или занятия кончился: его «после» — разовой, а играет
   *  что-то уже (приземлилась) — следом за ней */
  const queueAfter = (was: Played) => {
    const seen = new Set([was.anim.id]);
    const f = followUp(dataRef.current, was.anim, seen);
    if (!f) return;
    const next = chain(f, 'after', seen);
    setShot((prev) =>
      prev
        ? {
            ...prev,
            queue: [...(prev.queue ?? []), next.anim, ...(next.queue ?? [])],
            seen: [...new Set([...(prev.seen ?? [prev.anim.id]), ...(next.seen ?? [])])],
            follow: undefined,
          }
        : next,
    );
  };

  /** Разовая анимация на событие; своей нет — эффект головы. true — своя есть */
  const play = (t: AnimTrigger): boolean => {
    const a = pick(t);
    if (a) {
      // Погладили, пока играет другое (не ротация: сердится, привет, выбранное
      // персоной, та же «погладили») — не перебивать; круг перебивается всегда
      const next = chain(a, t);
      setShot((prev) => (prev && t === 'pet' && prev.trigger !== 'idle' && !looping(prev) ? prev : next));
      return true;
    }
    setShot((prev) => (prev && looping(prev) ? null : prev));
    const fx = SHOT_FX[t];
    if (fx) headRef.current()?.animate(fx.frames, { duration: fx.ms, easing: 'ease-out' });
    return false;
  };
  const playRef = useRef(play);
  playRef.current = play;

  /** Анимация по id — персона выбрала сама или «Проиграть на рабочем столе» */
  const act = (id: string): boolean => {
    const a = dataRef.current?.anims.find((x) => x.id === id && x.enabled && x.atlas && x.frames > 0);
    if (!a) return false;
    setShot(chain(a, 'act'));
    return true;
  };

  /** Переход между режимами персоны (повод mode, app/api/avatar_modes):
   *  анимация из нового набора — ставится тем же обновлением, что и сам
   *  набор с новой головой, чтобы голова не мелькнула раньше перехода */
  const switchTo = (anim: PortraitAnim) => {
    setShot({ key: ++key.current, anim, trigger: 'act', calm: calmRef.current, queue: [], seen: [anim.id] });
  };

  /** Нажали клавишу или кнопку мыши (anim.keys): анимация с этим
   *  сочетанием, их несколько — по весам. Та же уже идёт — разовая
   *  доигрывает, зацикленная останавливается (клавиша — как выключатель;
   *  это её конец — дальше её «после»). Спряталась — не показывается */
  const keyed = (combo: string): boolean => {
    if (motionRef.current === 'hidden') return false;
    const a = pickWeighted(
      (dataRef.current?.anims ?? []).filter((x) => x.enabled && x.atlas && x.frames > 0 && Array.isArray(x.keys) && x.keys.includes(combo)),
    );
    if (!a) return false;
    const prev = shotRef.current;
    if (prev?.anim.id === a.id) {
      if (looping(prev)) setShot(after(prev));
      return true;
    }
    setShot(chain(a, 'key'));
    return true;
  };

  useEffect(() => {
    const un = listen<{ state: MotionState; dir?: number }>('portrait-motion', (e) => {
      const state = e.payload.state;
      setMotion({ state, dir: Math.sign(e.payload.dir ?? 0) });
      if (state === 'land') playRef.current('land');
    });
    return () => {
      void un.then((f) => f());
    };
  }, []);

  const moving = MOVING.has(motion.state);
  // Шаги — свои у каждой стороны: сменилась сторона — выбрать заново
  const walkDir = motion.state === 'walk' ? motion.dir : 0;
  const contRef = useRef(cont);
  contRef.current = cont;
  // Своя анимация движения — выбирается, когда движение началось (сменилось)
  useEffect(() => {
    // Движение кончилось (приземлилась, отпустили на опору) — его «после»;
    // спряталась — не к месту
    const was = contRef.current;
    if (was && !MOVING.has(motion.state) && motion.state !== 'hidden') queueAfter(was);
    if (motion.state === 'walk') {
      const w = walkFor(data, walkDir);
      setCont(w ? { key: ++key.current, anim: w.anim, trigger: 'walk', mirror: w.mirror } : null);
    } else {
      const t = MOTION_TRIGGER[motion.state];
      const a = t ? pickWeighted(animsFor(data, t)) : null;
      setCont(a && t ? { key: ++key.current, anim: a, trigger: t } : null);
    }
    // Поехали — разовая анимация не к месту; выбранная персоной — подождёт
    if (MOVING.has(motion.state)) setShot((s) => (s?.trigger === 'act' ? s : null));
  }, [motion.state, walkDir, data]);

  // Есть ли свои шаги — панели: без них сама не гуляет, а «Иди сюда» и
  // «Спрячься» переезжают быстро, без шагов
  const canWalk = animsFor(data, 'walk').length > 0;
  useEffect(() => {
    void invoke('portrait_walker', { on: canWalk }).catch(() => {});
  }, [canWalk]);

  const ctxTrigger: AnimTrigger | null = o.loops.drop ? 'drop' : o.loops.think ? 'think' : o.loops.focus ? 'focus' : null;
  const ctxRef = useRef(ctx);
  ctxRef.current = ctx;
  useEffect(() => {
    const was = ctxRef.current;
    const a = ctxTrigger ? pickWeighted(animsFor(data, ctxTrigger)) : null;
    setCtx(a && ctxTrigger ? { key: ++key.current, anim: a, trigger: ctxTrigger } : null);
    // Занятие кончилось — его «после»; началось — его «перед» (разовая поверх
    // занятия, кончилась — видно занятие), если ничего разового не играет
    if (!ctxTrigger && was) queueAfter(was);
    else if (a && ctxTrigger && was?.trigger !== ctxTrigger) {
      const seen = new Set<string>();
      const lead = leadIn(dataRef.current, a, seen);
      if (lead.length) {
        const first: Played = {
          key: ++key.current,
          anim: lead[0],
          trigger: ctxTrigger,
          calm: calmRef.current,
          queue: lead.slice(1),
          seen: [...seen],
          follow: false,
        };
        setShot((prev) => (prev && !looping(prev) ? prev : first));
      }
    }
  }, [ctxTrigger, data]);

  // Анимацию удалили, выключили или сняли с круга — не доигрывать
  useEffect(() => {
    const alive = (p: Played | null) => !p || !!data?.anims.some((a) => a.id === p.anim.id && a.enabled && (!p.anim.loop || a.loop));
    if (!alive(shot)) setShot(null);
  }, [data]);

  // Зацикленная: заговорила, заснула, ушли, закончила дело — конец круга
  useEffect(() => {
    if (shot && looping(shot) && shot.calm !== o.calm) setShot(null);
  }, [shot, o.calm]);

  // Сон начинается и при открытии уже спящего аватара. «Засыпает» —
  // отдельная конечная цепочка; после неё выбранный цикл до пробуждения.
  const sleepAnim = sleepAnimFor(data);
  useEffect(() => {
    setSleep(o.asleep && sleepAnim
      ? { key: ++key.current, anim: sleepAnim, loop: true, mirror: false, walk: false }
      : null);
    // Обновления состояния ядра дают новые объекты того же набора:
    // спокойное дыхание не должно начинаться заново при каждом опросе.
  }, [o.asleep, sleepAnim?.id, sleepAnim?.updated_at, sleepAnim?.atlas]);
  useEffect(() => {
    setShot((prev) => o.asleep || prev?.trigger === 'sleep' ? null : prev);
  }, [o.asleep]);

  let showing: Showing | null = null;
  if (moving) {
    if (cont)
      showing = {
        key: cont.key,
        anim: cont.anim,
        loop: true,
        mirror: cont.mirror ?? mirrorFor(cont.anim, motion.dir),
        walk: cont.trigger === 'walk',
      };
  } else if (shot && (shot.trigger !== 'sleep' || o.asleep))
    showing = { key: shot.key, anim: shot.anim, loop: !o.asleep && looping(shot), mirror: false, walk: false };
  else if (o.asleep && sleepAnim && sleep?.anim.id === sleepAnim.id) showing = { ...sleep, anim: sleepAnim };
  else if (!o.asleep && ctx) showing = { key: ctx.key, anim: ctx.anim, loop: true, mirror: false, walk: false };
  const fx: BodyFx | null = moving && !cont ? (MOTION_FX[motion.state] ?? null) : null;

  // ── Ротация ──
  const settings = o.anims?.settings ?? ANIM_SETTINGS_DEFAULT;
  const free = o.free && o.on && !o.asleep && !moving && motion.state !== 'hidden' && !shot && !ctx;
  const physicsRef = useRef(o.physics);
  physicsRef.current = o.physics;
  useEffect(() => {
    if (!free || !(settings.every_min > 0)) return;
    const ms = settings.every_min * 60_000 * (0.5 + Math.random());
    let alive = true;
    const timer = window.setTimeout(async () => {
      type Option = { weight: number; run: () => Promise<boolean> | boolean };
      const options: Option[] = animsFor(dataRef.current, 'idle').map((a) => ({
        weight: a.weight,
        run: () => {
          setShot(chain(a, 'idle'));
          return true;
        },
      }));
      // Гулять — только на своих ногах: без анимации шагов не гуляет
      if (settings.walk && animsFor(dataRef.current, 'walk').length > 0)
        options.push({ weight: 1, run: () => invoke<boolean>('portrait_stroll').catch(() => false) });
      if (physicsRef.current) options.push({ weight: 0.5, run: () => invoke<boolean>('portrait_hop').catch(() => false) });
      // Не вышло (некуда идти, окон рядом нет) — следующее по весам
      while (options.length && alive) {
        const one = pickWeighted(options);
        if (!one) break;
        if (await one.run()) return;
        options.splice(options.indexOf(one), 1);
      }
      if (alive) setRearm((n) => n + 1);
    }, ms);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [free, settings.every_min, settings.walk, rearm]);

  return {
    motion,
    moving,
    showing,
    fx,
    play,
    act,
    switchTo,
    key: keyed,
    /** Разовая доиграла: дальше её цепочка или «после» */
    onEnd: () => {
      const prev = shotRef.current;
      setShot(prev ? after(prev) : null);
    },
  };
}
