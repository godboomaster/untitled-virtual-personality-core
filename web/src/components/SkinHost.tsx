import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { loadAmbient, loadSkinCss } from '../../skins';
import type { SkinAmbient } from '../../skins/types';
import { setAppliedSkin, useEffectiveSkin } from '../skinTheme';
import { syncSystemBars } from '../nativeBars';

/* Хозяин темы оформления: ставит на <html> data-skin и data-theme, когда CSS
   скина уже загружен (смена — мягким перетеканием, если браузер умеет
   View Transitions и движение не отключено), и держит живой фон скина
   (ambient.tsx) слоем под интерфейсом. Смонтирован над BootGate — тема
   действует и на экране запуска. */

type ViewTransitionDoc = Document & { startViewTransition?: (cb: () => void) => unknown };

function reducedMotionNow(): boolean {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export default function SkinHost() {
  const { skinId, scheme } = useEffectiveSkin();
  const [ambient, setAmbient] = useState<{ id: string; Comp: SkinAmbient } | null>(null);
  const [reducedMotion, setReducedMotion] = useState(reducedMotionNow);
  const seq = useRef(0);
  const first = useRef(true);

  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const on = () => setReducedMotion(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);

  useEffect(() => {
    const my = ++seq.current;
    const root = document.documentElement;
    const apply = () => {
      if (skinId) root.setAttribute('data-skin', skinId);
      else root.removeAttribute('data-skin');
      root.setAttribute('data-theme', scheme);
      setAppliedSkin(skinId);
    };
    const run = () => {
      if (my !== seq.current) return; // пока грузилось, выбрали другое
      const same = root.getAttribute('data-skin') === skinId && root.getAttribute('data-theme') === scheme;
      if (!same) {
        const doc = document as ViewTransitionDoc;
        if (!first.current && doc.startViewTransition && !reducedMotionNow()) doc.startViewTransition(apply);
        else apply();
      } else {
        setAppliedSkin(skinId);
      }
      first.current = false;
      syncSystemBars(scheme);
    };
    if (skinId) void loadSkinCss(skinId).then(run);
    else run();
  }, [skinId, scheme]);

  useEffect(() => {
    if (!skinId) {
      setAmbient(null);
      return;
    }
    let alive = true;
    void loadAmbient(skinId).then((Comp) => {
      if (alive) setAmbient(Comp ? { id: skinId, Comp } : null);
    });
    return () => {
      alive = false;
    };
  }, [skinId]);

  if (!ambient || ambient.id !== skinId) return null;
  const { Comp } = ambient;
  return createPortal(
    <div className="skin-ambient" aria-hidden="true">
      <Comp scheme={scheme} reducedMotion={reducedMotion} />
    </div>,
    document.body,
  );
}
