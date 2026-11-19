import { useState } from 'react';
import type { Persona } from '../../mockData';
import { ApiError, api } from '../../api';
import { useI18n } from '../../i18n';
import ArtDropZone from '../../components/ArtDropZone';
import { buildAnimPrompt, readAsDataUrl } from '../animPrompts';
import { NEW_ANIM } from '../animTypes';
import type { AnimMove, PortraitAnim } from '../animTypes';
import { NumField } from './frameFields';

/* Кадров на это пока нет: движение рабочего стола или новая анимация. Лист
   загружается прямо в редакторе — сразу с нужным поводом, названием и
   промптом для генератора, как «+ Добавить» во вкладке «Анимации». */

export type FrameAddKind = AnimMove | 'action';

export default function FrameAdd({
  persona,
  avatar = persona.id,
  kind,
  onDone,
  onCancel,
}: {
  persona: Persona;
  /** Чей аватар: id персоны или «персона@режим» */
  avatar?: string;
  kind: FrameAddKind;
  onDone: (anim: PortraitAnim) => void;
  onCancel: () => void;
}) {
  const { t } = useI18n();
  const [rows, setRows] = useState(4);
  const [cols, setCols] = useState(4);
  const [what, setWhat] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);

  const move = kind === 'action' ? null : kind;
  const prompt = buildAnimPrompt(kind, persona.name, what, rows, cols, 'body');

  const copy = () => {
    void navigator.clipboard?.writeText(prompt).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  const upload = async (file: File) => {
    if (!file.type.startsWith('image/')) return;
    setBusy(true);
    setError('');
    try {
      const res = await api.putPortraitAnim(avatar, 'new', {
        ...NEW_ANIM,
        name: (move ? t(`anim.move.name.${move}`) : what.trim() || t('anim.untitled')).slice(0, 40),
        rows,
        cols,
        triggers: [move ?? 'idle'],
        faces: move === 'walk' ? 'right' : 'none',
        data_url: await readAsDataUrl(file),
      });
      onDone(res.anims[res.anims.length - 1]);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t('frames.error'));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fe-add">
      <p className="fe-note">{t(move ? `anim.move.none.${move}` : 'frames.add.newHint')}</p>
      {!move && (
        <label className="fe-num">
          <span className="fe-num-label">{t('frames.add.what')}</span>
          <input className="input" value={what} maxLength={40} placeholder={t('frames.add.whatHint')} onChange={(e) => setWhat(e.target.value)} />
        </label>
      )}
      <div className="fe-nums">
        <NumField label={t('frames.rows')} value={rows} min={1} max={12} onChange={(v) => setRows(Math.round(v))} />
        <NumField label={t('frames.cols')} value={cols} min={1} max={12} onChange={(v) => setCols(Math.round(v))} />
      </div>
      <button type="button" className="btn btn--ghost" onClick={copy}>
        {t(copied ? 'frames.add.copied' : 'frames.add.prompt')}
      </button>
      <ArtDropZone
        title={t(busy ? 'frames.add.busy' : 'frames.add.drop')}
        spec={t('frames.add.dropSpec', { rows, cols })}
        onPick={(f) => {
          if (!busy) void upload(f);
        }}
      />
      {error && <p className="fe-error">{error}</p>}
      <button type="button" className="btn btn--ghost" onClick={onCancel}>
        {t('frames.add.cancel')}
      </button>
    </div>
  );
}
