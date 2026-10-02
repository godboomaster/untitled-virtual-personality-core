/* Общие хуки комнаты, переносимые в другой документ (PiP-окно): документ
   берётся из ownerDocument узла (или передаётся явно), слушатели вешаются
   на него, а не на глобальный document. Таймеры — одиночные setTimeout,
   не чаще раза в минуту; пока документ скрыт — стоят. */

import { useCallback, useEffect, useState, useSyncExternalStore } from 'react';
import type { RefObject } from 'react';

// Документ-владелец узла (или явно переданный); null до монтирования
export function ownerDocOf(ref: RefObject<Element | null>, explicit?: Document | null): Document | null {
  return explicit ?? ref.current?.ownerDocument ?? null;
}

// Виден ли документ-владелец (visibilitychange)
export function useDocumentVisible(ref: RefObject<Element | null>, explicitDoc?: Document | null): boolean {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const doc = ownerDocOf(ref, explicitDoc);
    if (!doc) return;
    const update = () => setVisible(!doc.hidden);
    update();
    doc.addEventListener('visibilitychange', update);
    return () => doc.removeEventListener('visibilitychange', update);
  }, [ref, explicitDoc]);
  return visible;
}

// Текущее время с точностью до минуты: таймер выровнен по началу минуты,
// скрытый документ — без тиков, при возврате — сразу свежее значение
export function useMinuteNow(ref: RefObject<Element | null>, explicitDoc?: Document | null, enabled = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return;
    const doc = ownerDocOf(ref, explicitDoc);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = () => {
      if (timer) clearTimeout(timer);
      timer = undefined;
      if (doc?.hidden) return;
      setNow(Date.now());
      timer = setTimeout(tick, 60_000 - (Date.now() % 60_000) + 50);
    };
    tick();
    const onVis = () => tick();
    doc?.addEventListener('visibilitychange', onVis);
    return () => {
      if (timer) clearTimeout(timer);
      doc?.removeEventListener('visibilitychange', onVis);
    };
  }, [ref, explicitDoc, enabled]);
  return now;
}

// ── Увиденные предметы (следы «новое в комнате») ──
// Множество названий в localStorage на персону. Первый визит: всё текущее
// считается увиденным, кроме полученного сегодня — иначе при первом открытии
// подсвечивалась бы вся комната.

const seenCache = new Map<string, Set<string> | null>();
const seenListeners = new Set<() => void>();
const seenKey = (persona: string) => `vpc-room-seen-items:${persona}`;

function readSeen(persona: string): Set<string> | null {
  if (seenCache.has(persona)) return seenCache.get(persona) ?? null;
  let set: Set<string> | null = null;
  try {
    const raw = localStorage.getItem(seenKey(persona));
    if (raw) set = new Set(JSON.parse(raw) as string[]);
  } catch {
    set = null;
  }
  seenCache.set(persona, set);
  return set;
}

function writeSeen(persona: string, set: Set<string>) {
  seenCache.set(persona, set);
  try {
    localStorage.setItem(seenKey(persona), JSON.stringify([...set].slice(-300)));
  } catch {
    // Квота/приватный режим — следы живут в рамках сессии
  }
  seenListeners.forEach((l) => l());
}

function subscribeSeen(cb: () => void) {
  seenListeners.add(cb);
  return () => {
    seenListeners.delete(cb);
  };
}

// enabled: false — список предметов ещё не настоящий (загрузка, демо/моки,
// раскладка со скрытыми предметами не пришла): не инициализируем «увиденное»
// чужим списком и ничего не подсвечиваем
export function useSeenItems(persona: string, names: string[], freshNames: ReadonlySet<string>, enabled = true) {
  const seen = useSyncExternalStore(subscribeSeen, () => seenCache.get(persona) ?? readSeen(persona));
  const namesKey = names.join('\n');
  useEffect(() => {
    // Инициализация при первом визите (как только предметы известны)
    if (!enabled || readSeen(persona) || !names.length) return;
    writeSeen(persona, new Set(names.filter((n) => !freshNames.has(n))));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persona, namesKey, enabled]);
  const markSeen = useCallback(
    (name: string) => {
      // До инициализации не пишем: множество из одного имени сделало бы
      // «новыми» все остальные предметы
      const cur = enabled ? readSeen(persona) : null;
      if (!cur || cur.has(name)) return;
      writeSeen(persona, new Set([...cur, name]));
    },
    [persona, enabled],
  );
  return { seen: enabled ? seen : null, markSeen };
}
