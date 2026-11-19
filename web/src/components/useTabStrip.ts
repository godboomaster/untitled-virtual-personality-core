import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

/** Строка вкладок с прокруткой вбок (досье и страницы): активная выезжает
 * к середине, края гаснут с той стороны, где есть ещё вкладки. */
export function useTabStrip(active: string, count: number) {
  const ref = useRef<HTMLDivElement>(null);
  const [edge, setEdge] = useState({ left: false, right: false });
  const update = useCallback(() => {
    const c = ref.current;
    if (!c) return;
    const left = c.scrollLeft > 2;
    const right = c.scrollLeft + c.clientWidth < c.scrollWidth - 2;
    setEdge((e) => (e.left === left && e.right === right ? e : { left, right }));
  }, []);
  useLayoutEffect(() => {
    const c = ref.current;
    const el = c?.querySelector<HTMLElement>('.tab--active');
    if (c && el && c.scrollWidth > c.clientWidth) {
      const cr = c.getBoundingClientRect();
      const r = el.getBoundingClientRect();
      c.scrollTo({ left: c.scrollLeft + (r.left - cr.left) - (cr.width - r.width) / 2, behavior: 'smooth' });
    }
    update();
  }, [active, count, update]);
  useEffect(() => {
    window.addEventListener('resize', update);
    return () => window.removeEventListener('resize', update);
  }, [update]);
  const className = `tabs dossier-tabs${edge.left ? ' dossier-tabs--more-left' : ''}${edge.right ? ' dossier-tabs--more-right' : ''}`;
  return { ref, className, onScroll: update };
}
