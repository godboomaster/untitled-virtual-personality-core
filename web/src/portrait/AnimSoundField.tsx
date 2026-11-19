import { useEffect, useRef, useState } from 'react';
import type { DragEvent } from 'react';
import { useI18n } from '../i18n';
import { readAsDataUrl } from './animPrompts';
import { fadeOut, playAnimSound } from './animSound';

/* Звук анимации в карточке: по умолчанию без звука; файл — кнопкой или
   перетащить, послушать, громкость, «повторять, пока идёт анимация».
   Записывается вместе с анимацией (sound_url в PUT, app/api/portrait_anim.py),
   играет портрет на столе (animSound.useAnimSound). */

export const SOUND_MAX_BYTES = 2 * 1024 * 1024;
const SOUND_EXT = /\.(mp3|wav|ogg|oga|opus|m4a|aac|flac|webm)$/i;

export interface AnimSoundValue {
  url: string | null;
  name: string;
  volume: number;
  loop: boolean;
}

export default function AnimSoundField({
  value,
  animSec,
  onPick,
  onClear,
  onVolume,
  onLoop,
}: {
  value: AnimSoundValue;
  /** Сколько секунд идёт анимация за раз; null — по кругу или неизвестно */
  animSec: number | null;
  onPick: (url: string, name: string) => void;
  onClear: () => void;
  onVolume: (v: number) => void;
  onLoop: (v: boolean) => void;
}) {
  const { t, lang } = useI18n();
  const fileRef = useRef<HTMLInputElement>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [dur, setDur] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [over, setOver] = useState(false);

  // Длительность — по метаданным файла; не читается — звук в этом браузере не играет
  useEffect(() => {
    setDur(null);
    setError('');
    if (!value.url) return;
    const a = new Audio();
    a.preload = 'metadata';
    const ok = () => setDur(Number.isFinite(a.duration) ? a.duration : null);
    const bad = () => setError(t('anim.sound.cantPlay'));
    a.addEventListener('loadedmetadata', ok);
    a.addEventListener('error', bad);
    a.src = value.url;
    return () => {
      a.removeEventListener('loadedmetadata', ok);
      a.removeEventListener('error', bad);
      a.removeAttribute('src');
    };
  }, [value.url, t]);

  const stop = () => {
    if (audioRef.current) fadeOut(audioRef.current, 60);
    audioRef.current = null;
    setPlaying(false);
  };
  // Сменили файл или закрыли карточку — тишина
  useEffect(() => stop, [value.url]);
  useEffect(() => {
    if (audioRef.current) audioRef.current.volume = value.volume / 100;
  }, [value.volume]);

  const listen = () => {
    if (playing) return stop();
    const a = value.url ? playAnimSound({ sound_url: value.url, volume: value.volume }) : null;
    if (!a) return;
    audioRef.current = a;
    setPlaying(true);
    a.addEventListener('ended', () => {
      if (audioRef.current === a) {
        audioRef.current = null;
        setPlaying(false);
      }
    });
  };

  const take = (file: File | undefined) => {
    if (!file) return;
    setError('');
    if (!file.type.startsWith('audio/') && !SOUND_EXT.test(file.name)) return setError(t('anim.sound.notAudio'));
    if (file.size > SOUND_MAX_BYTES) return setError(t('anim.sound.tooBig', { mb: SOUND_MAX_BYTES / 1024 / 1024 }));
    void readAsDataUrl(file).then((url) => onPick(url, file.name.slice(0, 80)));
  };

  const drop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    take(e.dataTransfer.files[0]);
  };

  const sec = (v: number) => t('anim.sound.sec', { s: v.toLocaleString(lang, { maximumFractionDigits: 1 }) });
  const note = value.loop
    ? t('anim.sound.loopNote')
    : dur != null && animSec != null && dur > animSec + 0.15
      ? t('anim.sound.longer', { sound: sec(dur), anim: sec(animSec) })
      : t('anim.sound.onceNote');

  return (
    <div
      className={`anim-sound ${over ? 'anim-sound--over' : ''}`}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={drop}
    >
      <div className="anim-sound-row">
        {value.url ? (
          <>
            <button type="button" className="btn btn--ghost anim-sound-play" aria-pressed={playing} onClick={listen}>
              {playing ? t('anim.sound.stop') : t('anim.sound.listen')}
            </button>
            <span className="anim-sound-name" title={value.name}>
              {value.name || t('anim.sound.file')}
            </span>
            {dur != null && <span className="anim-sound-dur">{sec(dur)}</span>}
            <span className="anim-sound-acts">
              <button type="button" className="btn btn--ghost" onClick={() => fileRef.current?.click()}>
                {t('anim.sound.replace')}
              </button>
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => {
                  stop();
                  setError('');
                  onClear();
                }}
              >
                {t('anim.sound.remove')}
              </button>
            </span>
          </>
        ) : (
          <>
            <span className="anim-sound-none">{t('anim.sound.none')}</span>
            <button type="button" className="btn btn--ghost" onClick={() => fileRef.current?.click()}>
              {t('anim.sound.add')}
            </button>
          </>
        )}
        <input
          ref={fileRef}
          type="file"
          hidden
          accept="audio/*,.mp3,.wav,.ogg,.oga,.opus,.m4a,.aac,.flac,.webm"
          onChange={(e) => {
            take(e.target.files?.[0]);
            e.target.value = '';
          }}
        />
      </div>
      {value.url && (
        <>
          <div className="field anim-sound-volume">
            <label className="field-label" htmlFor="anim-sound-volume">
              {t('anim.sound.volume')}
            </label>
            <div className="anim-rate-value">{t('anim.sound.volumeValue', { v: value.volume })}</div>
            <input id="anim-sound-volume" type="range" min={0} max={100} step={5} value={value.volume} onChange={(e) => onVolume(Number(e.target.value))} />
          </div>
          <label className="anim-switch-row">
            <span className="switch">
              <input type="checkbox" checked={value.loop} onChange={() => onLoop(!value.loop)} />
              <span className="switch-slider" />
            </span>
            <span>{t('anim.sound.loop')}</span>
          </label>
          <div className="field-hint">{note}</div>
        </>
      )}
      {error && <div className="field-hint portrait-error">{error}</div>}
      <div className="field-hint">{t('anim.sound.hint', { mb: SOUND_MAX_BYTES / 1024 / 1024 })}</div>
    </div>
  );
}
