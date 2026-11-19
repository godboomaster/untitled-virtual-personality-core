import { useContext, useEffect, useRef } from 'react';
import { isNativeApp } from '../api';
import { setWidgetHead } from '../bgInbox';
import { setOverlayPersonas } from '../phone/avatarOverlay';
import { usePersonaAvatarPreset } from '../room/personaAvatarPreset';
import { HEAD_DRAW_VERSION } from '../room/avatarHead';
import { headAtlas } from './headAtlas';
import { HeadLookContext } from './headLook';

/* Телефон: виджет «Живой портрет» (PortraitWidget) режет кадры из листов
   портрета, а у персоны без своих листов — из листов головы её фигуры
   (headAtlas). Печатает их веб: виджет нативный и SVG рисовать не умеет.
   Листы уходят нативу (плагин BackgroundInbox, setHeadPortrait), когда облик
   персоны известен и сменился с прошлого раза (ключ — в localStorage), по
   одной персоне за раз; свои листы портрета у виджета главнее. */

const SENT_KEY = 'vpc-widget-heads';
// Облик из раскладки ядра приходит не сразу — не печатать облик по умолчанию зря
const SETTLE_MS = 4000;

let queue: Promise<void> = Promise.resolve();

function sentKeys(): Record<string, string> {
  try {
    const v = JSON.parse(localStorage.getItem(SENT_KEY) ?? '{}') as Record<string, string>;
    return v && typeof v === 'object' ? v : {};
  } catch {
    return {};
  }
}

function rememberSent(persona: string, key: string) {
  try {
    localStorage.setItem(SENT_KEY, JSON.stringify({ ...sentKeys(), [persona]: key }));
  } catch {
    /* хранилище недоступно — напечатаем снова при следующем запуске */
  }
}

function HeadSync({ personaId }: { personaId: string }) {
  const preset = usePersonaAvatarPreset(personaId);
  // Рисовка скина: сменили скин или схему — листы печатаются заново; сменилась
  // рисовка головы (HEAD_DRAW_VERSION) — тоже
  const look = useContext(HeadLookContext);
  const presetRef = useRef({ preset, look });
  presetRef.current = { preset, look };
  const key = `${preset.head}:${preset.eyes}:${preset.accessory}:${preset.shade}:${preset.body ?? 0}:${look?.key ?? 'base'}:${HEAD_DRAW_VERSION}`;
  useEffect(() => {
    if (sentKeys()[personaId] === key) return;
    const timer = window.setTimeout(() => {
      queue = queue.then(async () => {
        if (sentKeys()[personaId] === key) return;
        try {
          const cur = presetRef.current;
          await setWidgetHead(personaId, await headAtlas(cur.preset, { look: cur.look }));
          rememberSent(personaId, key);
        } catch (e) {
          console.warn('[widget-head] листы головы не ушли виджету:', personaId, e);
        }
      });
    }, SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [personaId, key]);
  return null;
}

export default function WidgetHeads({ personas }: { personas: { id: string; name: string }[] }) {
  // Имена — и меню аватара поверх приложений («Сменить персону»)
  const names = personas.map((p) => `${p.id}\u0000${p.name}`).join('\u0001');
  useEffect(() => {
    setOverlayPersonas(personas);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- по составу, не по ссылке
  }, [names]);
  if (!isNativeApp()) return null;
  return (
    <>
      {personas.map((p) => (
        <HeadSync key={p.id} personaId={p.id} />
      ))}
    </>
  );
}
