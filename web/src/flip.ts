/* FLIP-переход между двумя раскладками одних и тех же элементов (карточки
   всех чатов ↔ строки списка персон): до смены вида запоминаем прямоугольники
   элементов с data-flip-id, после — каждый элемент стартует со старого места
   и едет на новое. Масштаб — равномерный (по ширине), чтобы текст не
   растягивался. */

export type FlipRects = Record<string, DOMRect>;

const DURATION_MS = 460;
const STEP_MS = 22; // каскад: элементы стартуют чуть вразнобой
const EASING = 'cubic-bezier(0.2, 0.8, 0.2, 1)';

export function reducedMotion(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
}

export function captureRects(root: HTMLElement | null): FlipRects {
  const out: FlipRects = {};
  root?.querySelectorAll<HTMLElement>('[data-flip-id]').forEach((el) => {
    const id = el.dataset.flipId;
    const rect = el.getBoundingClientRect();
    // Скрытые (свёрнутый список персон) — без старта: элемент просто появится
    if (id && rect.width > 0 && rect.height > 0) out[id] = rect;
  });
  return out;
}

// Проиграть переход; промис — когда все элементы доехали (или анимации нет)
export function playFlip(root: HTMLElement | null, from: FlipRects | null): Promise<void> {
  if (!root || !from || reducedMotion()) return Promise.resolve();
  const runs: Promise<unknown>[] = [];
  let i = 0;
  root.querySelectorAll<HTMLElement>('[data-flip-id]').forEach((el) => {
    const a = from[el.dataset.flipId ?? ''];
    const b = el.getBoundingClientRect();
    if (!a || !b.width || !b.height || typeof el.animate !== 'function') return;
    const scale = Math.min(2, Math.max(0.5, a.width / b.width));
    const dx = a.left - b.left;
    const dy = a.top - b.top;
    const anim = el.animate(
      [
        { transformOrigin: 'top left', transform: `translate(${dx}px, ${dy}px) scale(${scale})`, opacity: 0.6 },
        { transformOrigin: 'top left', transform: 'none', opacity: 1 },
      ],
      { duration: DURATION_MS, delay: i * STEP_MS, easing: EASING, fill: 'backwards' },
    );
    runs.push(anim.finished.catch(() => undefined));
    i += 1;
  });
  return Promise.all(runs).then(() => undefined);
}
