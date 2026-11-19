import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { useI18n } from '../i18n';
import type { Persona } from '../mockData';
import { closePip } from '../pipStore';
import PettablePortrait from './PettablePortrait';
import { hasPortrait, usePortraitActivity } from './portraitStore';
import { useHeardAsk } from './useHeardAsk';
import { useLiveAvatar } from './useLiveAvatar';
import SwitchSprite from './SwitchSprite';
import WebMiniPlayer from '../player/WebMiniPlayer';
import type { PlayerLayout } from '../player/MiniPlayer';

/* Живой портрет в PiP-окне («Вынести портрет»): голова на всё окно поверх
   остальных окон — смотрит на курсор над окном, моргает; пока открыт
   голосовой режим этой персоны — говорит, слушает, кивает и думает вместе с ним
   (portraitStore → usePortraitActivity); по голове можно погладить
   (PettablePortrait). Кнопка «Вернуть» — при наведении,
   чтобы не заслонять портрет. Само окно — PipHost (web/src/PipHost.tsx).

   Вы написали персоне (откуда угодно — useHeardAsk): ваше сообщение
   облачком в нижнем углу окна, персона кивает, поворачивается к нему и
   «читает» (по длине текста), потом думает; пока оно висит, на курсор не
   отвлекается; ответ убирает облачко. */

// Читает столько (по длине текста) — как портрет на рабочем столе
const READ_MS = { perChar: 60, min: 2000, max: 6000 };
const readMs = (text: string) => Math.min(READ_MS.max, Math.max(READ_MS.min, text.length * READ_MS.perChar));

export default function PortraitPipView({ persona, doc }: { persona: Persona; doc: Document }) {
  const { t } = useI18n();
  // Аватар режима, что включён в чате; сменился — переход, потом новая голова
  const { data, switching, switchDone } = useLiveAvatar(persona.id);
  const activity = usePortraitActivity(persona.id);
  // Таймеры — окна PiP: вкладка приложения в фоне их не замораживает
  const view = doc.defaultView ?? window;
  const [miniWidth, setMiniWidth] = useState(0);
  const playerLayout = useCallback(({ width, height }: PlayerLayout) => {
    setMiniWidth(width);
    if (!width) return;
    const extraW = Math.max(0, width + 100 - view.innerWidth);
    const extraH = Math.max(0, height + 20 - view.innerHeight);
    if (extraW || extraH) {
      try { view.resizeTo(view.outerWidth + extraW, view.outerHeight + extraH); } catch { /* browser controls PiP sizing */ }
    }
  }, [view]);
  const { ask, heard, nod } = useHeardAsk(persona.id, view);
  const askEl = useRef<HTMLDivElement>(null);
  const [readingAsk, setReadingAsk] = useState(false);
  const [readAt, setReadAt] = useState<{ x: number; y: number } | null>(null);
  const [size, setSize] = useState(0);

  useEffect(() => {
    doc.title = persona.name;
  }, [doc, persona.name]);

  useEffect(() => {
    const onResize = () => setSize((n) => n + 1);
    view.addEventListener('resize', onResize);
    return () => view.removeEventListener('resize', onResize);
  }, [view]);

  // Новое сообщение — читает его, потом думает
  useEffect(() => {
    if (!ask) {
      setReadingAsk(false);
      return;
    }
    setReadingAsk(true);
    const timer = view.setTimeout(() => setReadingAsk(false), readMs(ask.text));
    return () => view.clearTimeout(timer);
  }, [ask, view]);

  // Куда смотреть, читая: середина облачка от середины головы — как курсор
  // для LivePortrait
  useLayoutEffect(() => {
    const el = askEl.current;
    const head = el?.ownerDocument.querySelector('.portrait-pip .live-portrait')?.getBoundingClientRect();
    if (!el || !head) {
      setReadAt(null);
      return;
    }
    const r = el.getBoundingClientRect();
    const range = Math.max(260, head.width * 1.1);
    const x = Math.max(-1, Math.min(1, (r.left + r.width / 2 - (head.left + head.width / 2)) / range));
    const y = Math.max(-1, Math.min(1, (r.top + r.height / 2 - (head.top + head.height * 0.45)) / range));
    setReadAt((prev) => (prev && Math.abs(prev.x - x) < 0.01 && Math.abs(prev.y - y) < 0.01 ? prev : { x, y }));
  }, [ask, size, data]);

  const reading = readingAsk ? readAt : null;
  const { speaking, listening } = activity;
  const thinking = (activity.thinking || heard) && !speaking && !reading;

  return (
    <div className={`portrait-pip ${listening ? 'portrait-pip--listening' : ''} ${switching ? 'avatar-switching' : ''}`}>
      {switching && <SwitchSprite key={switching.key} anim={switching.anim} onEnd={switchDone} view={view} />}
      <div className={`portrait-with-player portrait-pip-content${miniWidth ? ' portrait-with-player--active' : ''}`} style={{ '--portrait-player-width': `${miniWidth}px` } as CSSProperties}>
      {hasPortrait(data) ? (
        <PettablePortrait
          personaId={persona.id}
          data={data}
          speaking={speaking}
          listening={listening}
          thinking={thinking}
          attentive={!!ask}
          nod={activity.nod + nod}
          reading={reading}
          speech={activity.speech ? { text: activity.speech, at: activity.speechAt } : null}
          label={persona.name}
        />
      ) : (
        <div className="portrait-empty">{t('portrait.empty')}</div>
      )}
      <WebMiniPlayer onLayout={playerLayout} />
      </div>
      {ask && hasPortrait(data) && (
        <div key={ask.id} ref={askEl} className="portrait-pip-ask" aria-label={ask.text}>
          <span>{ask.text}</span>
        </div>
      )}
      <button type="button" className="room-focus-btn room-focus-btn--muted portrait-pip-back" onClick={closePip}>
        {t('portrait.pip.back')}
      </button>
    </div>
  );
}
