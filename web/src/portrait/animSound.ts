import { useEffect, useRef } from 'react';
import type { PortraitAnim } from './animTypes';

/* Звук анимации (anim.sound_url, app/api/portrait_anim.py). Начинается с
   первым кадром, громкость anim.volume. sound_loop — по кругу, пока эта
   анимация на экране; иначе звучит один раз и доигрывает, даже если
   анимация уже кончилась (короткий «мяу» не обрывается на последнем кадре).
   Новый звук обрывает прежний — мягко, без щелчка. */

const FADE_MS = 120;

/** Убавить до тишины и остановить */
export function fadeOut(audio: HTMLAudioElement, ms = FADE_MS) {
  const from = audio.volume;
  const t0 = performance.now();
  const step = () => {
    const k = ms > 0 ? Math.min(1, (performance.now() - t0) / ms) : 1;
    audio.volume = from * (1 - k);
    if (k < 1) window.setTimeout(step, 16);
    else {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    }
  };
  step();
}

/** Сыграть звук анимации; нет звука — null */
export function playAnimSound(anim: Pick<PortraitAnim, 'sound_url' | 'volume'>, loop = false): HTMLAudioElement | null {
  if (!anim.sound_url) return null;
  const audio = new Audio(anim.sound_url);
  audio.volume = Math.max(0, Math.min(1, (anim.volume ?? 80) / 100));
  audio.loop = loop;
  void audio.play().catch(() => {});
  return audio;
}

/** Звук того, что сейчас играет (showing: key меняется — новая анимация).
 *  on — «Звуки анимаций» в меню портрета: выключили — тишина сразу */
export function useAnimSound(showing: { key: number; anim: PortraitAnim } | null, on: boolean) {
  const cur = useRef<{ key: number; audio: HTMLAudioElement; loop: boolean } | null>(null);
  const showingRef = useRef(showing);
  showingRef.current = showing;
  const key = showing?.key ?? 0;

  useEffect(() => {
    const sh = showingRef.current;
    const was = cur.current;
    if (was && was.key === key && on) return;
    const sound = on && sh?.anim.sound_url ? sh.anim : null;
    // Новый звук или звуки выключили — прежний тише и стоп; кончилась
    // анимация без своего звука — по кругу стоп, разовый доигрывает
    if (was && (sound || !on || was.loop)) {
      fadeOut(was.audio);
      cur.current = null;
    }
    if (sound && sh) {
      const audio = playAnimSound(sound, !!sound.sound_loop);
      if (audio) {
        cur.current = { key, audio, loop: !!sound.sound_loop };
        audio.addEventListener('ended', () => {
          if (cur.current?.audio === audio) cur.current = null;
        });
      }
    }
  }, [key, on]);

  useEffect(
    () => () => {
      if (cur.current) fadeOut(cur.current.audio, 0);
      cur.current = null;
    },
    [],
  );
}
