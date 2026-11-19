import { useEffect, useState } from 'react';
import { api, ApiError } from '../api';
import type { AvatarText } from '../api';
import { useApiOnline } from '../apiData';
import { useI18n } from '../i18n';

/* «Настройки» → «Общие»: скорость текста у аватара — облачко портрета на
   рабочем столе и виджета «Живой портрет» на телефоне (app/core/avatar_text).
   Слова идут одно за другим, длинная реплика — частями. Хранится в ядре:
   панель и телефон получают её вместе с репликами, поэтому поля — только при
   живом ядре. Ползунок — по шагам; сохраняется, когда его отпустили. */

// Слов в секунду; 0 — сразу всей частью (последний шаг — «быстрее некуда»)
const WORD_STEPS = [1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 0];
// Секунд на часть средней длины
const PART_STEPS = [2, 3, 4, 5, 6.5, 8, 10, 13, 16, 20, 25, 30];

function nearest(steps: number[], value: number): number {
  let best = 0;
  steps.forEach((s, i) => {
    if (Math.abs(s - value) < Math.abs(steps[best] - value)) best = i;
  });
  return best;
}

export default function AvatarTextFields() {
  const { t } = useI18n();
  const apiOnline = useApiOnline();
  const [data, setData] = useState<AvatarText | null>(null);
  const [word, setWord] = useState(0);
  const [part, setPart] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const apply = (d: AvatarText) => {
    setData(d);
    setWord(nearest(WORD_STEPS, d.words_per_sec));
    setPart(nearest(PART_STEPS, d.part_sec));
  };

  useEffect(() => {
    if (!apiOnline) return;
    api.getAvatarText().then(apply).catch(() => {});
  }, [apiOnline]);

  if (!apiOnline || !data) return null;

  const save = (patch: Partial<Pick<AvatarText, 'words_per_sec' | 'part_sec'>>) => {
    setError(null);
    api
      .setAvatarText(patch)
      .then(apply)
      .catch((e) => {
        setError(e instanceof ApiError ? e.message : String(e));
        api.getAvatarText().then(apply).catch(() => {});
      });
  };
  const wps = WORD_STEPS[word];
  const sec = PART_STEPS[part];
  const isDefault = wps === data.defaults.words_per_sec && sec === data.defaults.part_sec;
  const fmt = (n: number) => String(n).replace('.', t('settings.avatarText.decimal'));

  return (
    <div className="field">
      <label className="field-label" htmlFor="avatar-words">
        {t('settings.avatarText.words')}:{' '}
        {wps === 0 ? t('settings.avatarText.instant') : t('settings.avatarText.wps', { n: fmt(wps) })}
      </label>
      <input
        id="avatar-words"
        type="range"
        min={0}
        max={WORD_STEPS.length - 1}
        step={1}
        value={word}
        onChange={(e) => setWord(Number(e.target.value))}
        onPointerUp={() => save({ words_per_sec: WORD_STEPS[word] })}
        onKeyUp={() => save({ words_per_sec: WORD_STEPS[word] })}
        style={{ width: '100%', accentColor: 'var(--accent)' }}
      />
      <label className="field-label" htmlFor="avatar-parts" style={{ marginTop: 12 }}>
        {t('settings.avatarText.parts')}: {t('settings.avatarText.sec', { n: fmt(sec) })}
      </label>
      <input
        id="avatar-parts"
        type="range"
        min={0}
        max={PART_STEPS.length - 1}
        step={1}
        value={part}
        onChange={(e) => setPart(Number(e.target.value))}
        onPointerUp={() => save({ part_sec: PART_STEPS[part] })}
        onKeyUp={() => save({ part_sec: PART_STEPS[part] })}
        style={{ width: '100%', accentColor: 'var(--accent)' }}
      />
      <p className="field-hint">{error ?? t('settings.avatarText.hint')}</p>
      {!isDefault && (
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => save({ words_per_sec: data.defaults.words_per_sec, part_sec: data.defaults.part_sec })}
        >
          {t('settings.avatarText.reset')}
        </button>
      )}
    </div>
  );
}
