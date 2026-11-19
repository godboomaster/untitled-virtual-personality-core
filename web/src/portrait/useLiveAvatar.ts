import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { animPlayable, pickWeighted, type PortraitAnim } from './animTypes';
import { useLivePortrait } from './portraitStore';
import type { PortraitData } from './portraitTypes';

/* Живой аватар персоны с переходом между режимами (голосовой режим, PiP).
   Аватар — того режима, что включён в чате (portraitStore.useLivePortrait,
   app/api/avatar_modes). Режим сменился — сначала анимация перехода из
   прежнего режима в новый, если её загрузили (вкладка «Анимации» аватара
   режима, повод «Смена режима»): пока она идёт, голову не видно; кончилась —
   голова нового режима. Перехода нет — голова меняется сразу. Пока переход
   грузится, видна прежняя голова. */

export interface AvatarSwitch {
  key: number;
  anim: PortraitAnim;
}

export function useLiveAvatar(personaId: string): {
  data: PortraitData | null;
  switching: AvatarSwitch | null;
  switchDone: () => void;
} {
  const live = useLivePortrait(personaId);
  const [shown, setShown] = useState<PortraitData | null>(live);
  const [switching, setSwitching] = useState<AvatarSwitch | null>(null);
  const next = useRef<PortraitData | null>(live);
  const mode = useRef<{ persona: string; mode: string } | null>(null);

  useEffect(() => {
    if (!live) return;
    next.current = live;
    const to = live.mode ?? '';
    const from = mode.current?.persona === personaId ? mode.current.mode : null;
    mode.current = { persona: personaId, mode: to };
    if (from === null || from === to) {
      setShown(live);
      return;
    }
    let alive = true;
    api
      .getPortraitSwitch(personaId, from, to)
      .then((r) => {
        if (!alive) return;
        const anim = pickWeighted((r.anims ?? []).filter(animPlayable));
        if (anim) setSwitching({ key: Date.now(), anim });
        else setShown(next.current);
      })
      .catch(() => {
        if (alive) setShown(next.current);
      });
    return () => {
      alive = false;
    };
  }, [live, personaId]);

  const switchDone = useCallback(() => {
    setSwitching(null);
    setShown(next.current);
  }, []);

  return { data: shown, switching, switchDone };
}
