import { useEffect, useState } from 'react';
import type { Persona } from '../mockData';
import { ApiError, api } from '../api';
import { useI18n } from '../i18n';
import ArtDropZone from '../components/ArtDropZone';
import Select from '../components/Select';
import { closePip, isPipOf, openPip, pipSupported, usePip } from '../pipStore';
import LivePortrait from './LivePortrait';
import { hasPortrait, setPortrait, setPortraitActivity, usePortrait } from './portraitStore';
import { PORTRAIT_SHEETS } from './portraitTypes';
import type { PortraitSheet } from './portraitTypes';

/* Карточка арт-мастерской «Живой портрет»: листы ракурсов головы для
   голосового режима — промпт для генератора картинок, загрузка листов по
   слотам (глаза × рот), сетка и поля, превью, которое смотрит на курсор и
   по кнопке «говорит». Нарезка и фон — на ядре (app/api/portrait_api.py). */

type PromptKind = 'base' | 'closed' | 'half' | 'open';
const PROMPT_KINDS: PromptKind[] = ['base', 'closed', 'half', 'open'];

// Шаблон ракурсов 5×5 — в исходном проекте (лицензия его ассетов не даёт
// класть файл к себе): там 01_image_template.png
const TEMPLATE_URL = 'https://github.com/rotejin/tomari-guruguru';

// Промпты — по-английски, как остальные промпт-паки
function buildPrompt(kind: PromptKind, name: string): string {
  if (kind === 'base') {
    return [
      `Using the attached character reference image and the attached 5×5 head-angle template, draw a 5×5 head-angle sheet of ${name}.`,
      'Keep the character exactly: face, hairstyle, hair color, eyes, outfit collar, accessories, color palette, line art, shading and art style.',
      'Take from the template only the head direction, position, scale and spacing of each cell.',
      'Columns, left to right: full left profile, 45° left, front, 45° right, full right profile.',
      'Rows, top to bottom: looking strongly up, slightly up, eye level, slightly down, strongly down.',
      "Each cell combines its column's horizontal angle with its row's vertical angle.",
      'Square image, 25 heads evenly spaced in a 5×5 grid, the same head size in every cell, a little empty space between heads.',
      'Head and shoulders only. Eyes open, mouth closed. Plain flat gray background, no text, no borders, no shadows on the background.',
    ].join('\n');
  }
  const change = {
    closed: 'the eyes are closed and relaxed, as in the middle of a blink',
    half: 'the mouth is slightly open, as when saying "eh"',
    open: 'the mouth is open, as when saying "ah"',
  }[kind];
  return [
    `Redraw the attached 5×5 head-angle sheet of ${name} exactly: the same angles, positions, sizes, colors and background in every cell.`,
    `Change only one thing in every cell: ${change}.`,
    'Nothing else changes: same face, hair, outfit, line art and lighting.',
  ].join('\n');
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

export default function PortraitCard({ persona }: { persona: Persona }) {
  const { t } = useI18n();
  const data = usePortrait(persona.id);
  const [slot, setSlot] = useState<PortraitSheet>('A');
  const [rows, setRows] = useState(5);
  const [cols, setCols] = useState(5);
  const [offsetX, setOffsetX] = useState(0);
  const [offsetY, setOffsetY] = useState(0);
  const [bg, setBg] = useState<'auto' | 'keep'>('auto');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [talk, setTalk] = useState(false);
  const [promptKind, setPromptKind] = useState<PromptKind>('base');
  const [copied, setCopied] = useState(false);
  const pip = usePip();
  const portraitOut = isPipOf(pip, 'portrait', persona.id);

  // «Проверить речь» — говорит и вынесенный портрет
  useEffect(() => {
    if (!talk) return;
    setPortraitActivity(persona.id, { speaking: true, listening: false, thinking: false });
    return () => setPortraitActivity(persona.id, null);
  }, [talk, persona.id]);

  // Сетка — общая на портрет, поля и фон — свои у листа
  const loaded = data?.sheets[slot];
  const saved = data?.settings[slot];
  useEffect(() => {
    if (!data) return;
    setRows(data.rows);
    setCols(data.cols);
  }, [data]);
  useEffect(() => {
    setOffsetX(saved?.offset_x ?? 0);
    setOffsetY(saved?.offset_y ?? 0);
    setBg(saved?.bg ?? 'auto');
  }, [saved, slot]);

  const run = async (call: () => Promise<Parameters<typeof setPortrait>[1]>) => {
    setBusy(true);
    setError('');
    try {
      setPortrait(persona.id, await call());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t('portrait.error'));
    } finally {
      setBusy(false);
    }
  };
  const grid = { rows, cols, offset_x: offsetX, offset_y: offsetY, bg };
  const upload = (file: File) => {
    if (!file.type.startsWith('image/')) return;
    void run(async () => api.putPortraitSheet(persona.id, slot, { ...grid, data_url: await readAsDataUrl(file) }));
  };
  const reslice = () => void run(() => api.putPortraitSheet(persona.id, slot, grid));
  const remove = () => void run(() => api.deletePortraitSheet(persona.id, slot));

  const prompt = buildPrompt(promptKind, persona.name);
  const copy = () => {
    void navigator.clipboard?.writeText(prompt).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };
  const num = (v: string, min: number, max: number) => Math.max(min, Math.min(max, Math.round(Number(v) || 0)));

  return (
    <div className="card">
      <div className="card-title-row">
        <h3 className="card-title">{t('portrait.title')}</h3>
        <span className="badge">ART_04</span>
      </div>
      <div className="field-hint">{t('portrait.lead')}</div>

      <div className="portrait-layout">
        <div className="portrait-preview">
          {hasPortrait(data) ? (
            <LivePortrait data={data} speaking={talk} label={persona.name} />
          ) : (
            <div className="portrait-empty">{t('portrait.empty')}</div>
          )}
          {hasPortrait(data) && (
            <div className="portrait-preview-actions">
              <button type="button" className={`btn ${talk ? 'btn--primary' : 'btn--ghost'}`} onClick={() => setTalk((v) => !v)}>
                {talk ? t('portrait.testTalkStop') : t('portrait.testTalk')}
              </button>
              {/* PiP: только Chromium (Document Picture-in-Picture); иначе кнопки нет */}
              {pipSupported() && (
                <button
                  type="button"
                  className="btn btn--ghost"
                  title={portraitOut ? t('portrait.pip.closeHint') : t('portrait.pip.openHint')}
                  onClick={() => (portraitOut ? closePip() : void openPip('portrait', persona.id))}
                >
                  {portraitOut ? t('portrait.pip.back') : t('portrait.pip.open')}
                </button>
              )}
            </div>
          )}
        </div>

        <div className="portrait-controls">
          <div className="tabs portrait-tabs">
            {PORTRAIT_SHEETS.map((k) => (
              <button
                key={k}
                type="button"
                className={`tab ${slot === k ? 'tab--active' : ''} ${data?.sheets[k] ? 'portrait-tab--done' : ''}`}
                onClick={() => setSlot(k)}
              >
                {t(`portrait.sheet.${k}`)}
              </button>
            ))}
          </div>
          <ArtDropZone
            title={t(`portrait.sheetLong.${slot}`)}
            spec={t('portrait.spec')}
            thumb={loaded}
            onPick={upload}
            onClear={remove}
          />
          <div className="field-grid portrait-grid">
            <div className="field">
              <label className="field-label">{t('portrait.rows')}</label>
              <input className="input" type="number" min={1} max={10} value={rows}
                onChange={(e) => setRows(num(e.target.value, 1, 10))} />
            </div>
            <div className="field">
              <label className="field-label">{t('portrait.cols')}</label>
              <input className="input" type="number" min={1} max={10} value={cols}
                onChange={(e) => setCols(num(e.target.value, 1, 10))} />
            </div>
            <div className="field">
              <label className="field-label">{t('portrait.offsetX')}</label>
              <input className="input" type="number" min={0} value={offsetX}
                onChange={(e) => setOffsetX(num(e.target.value, 0, 2000))} />
            </div>
            <div className="field">
              <label className="field-label">{t('portrait.offsetY')}</label>
              <input className="input" type="number" min={0} value={offsetY}
                onChange={(e) => setOffsetY(num(e.target.value, 0, 2000))} />
            </div>
            <div className="field">
              <label className="field-label">{t('portrait.bg')}</label>
              <Select
                value={bg}
                options={[
                  { value: 'auto', label: t('portrait.bgAuto') },
                  { value: 'keep', label: t('portrait.bgKeep') },
                ]}
                onChange={(v) => setBg(v === 'keep' ? 'keep' : 'auto')}
              />
            </div>
          </div>
          <div className="field-hint">{t('portrait.gridHint')}</div>
          {loaded && (
            <div className="art-prompt-actions">
              <button type="button" className="btn btn--ghost" disabled={busy} onClick={reslice}>
                {t('portrait.reslice')}
              </button>
            </div>
          )}
          {busy && <div className="field-hint">{t('portrait.busy')}</div>}
          {error && <div className="field-hint portrait-error">{error}</div>}
        </div>
      </div>

      <div className="field-label portrait-prompt-label">{t('art.promptPack')}</div>
      <div className="tabs">
        {PROMPT_KINDS.map((k) => (
          <button
            key={k}
            type="button"
            className={`tab ${promptKind === k ? 'tab--active' : ''}`}
            onClick={() => setPromptKind(k)}
          >
            {t(`portrait.prompt.${k}`)}
          </button>
        ))}
      </div>
      <pre className="art-prompt">{prompt}</pre>
      <div className="art-prompt-actions">
        <button type="button" className="btn btn--primary" onClick={copy}>
          {copied ? t('common.copied') : t('common.copy')}
        </button>
        <a className="btn btn--ghost" href={TEMPLATE_URL} target="_blank" rel="noreferrer">
          {t('portrait.template')}
        </a>
      </div>
      <div className="field-hint" style={{ marginTop: 12 }}>
        {t(promptKind === 'base' ? 'portrait.promptHint' : 'portrait.promptHintVariant')}
      </div>
    </div>
  );
}
