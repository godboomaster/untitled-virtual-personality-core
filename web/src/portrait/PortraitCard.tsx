import { useCallback, useEffect, useState } from 'react';
import type { CSSProperties, FormEvent, KeyboardEvent } from 'react';
import type { Persona } from '../mockData';
import { ApiError, api } from '../api';
import { useI18n } from '../i18n';
import ArtDropZone from '../components/ArtDropZone';
import Collapsible from '../components/Collapsible';
import NumInput from '../components/NumInput';
import Select from '../components/Select';
import { closePip, isPipOf, openPip, pipSupported, usePip } from '../pipStore';
import LivePortrait from './LivePortrait';
import WebMiniPlayer from '../player/WebMiniPlayer';
import type { PlayerLayout } from '../player/MiniPlayer';
import { hasPortrait, setPortrait, setPortraitActivity, usePortrait } from './portraitStore';
import { PORTRAIT_SHEETS, VOWEL_SHEETS, sheetKey } from './portraitTypes';
import type { EmotionMood, FaceSheet, PersonaMood, PortraitData, PortraitEmotion, PortraitSheet, PortraitSheets } from './portraitTypes';
import { MOOD_BAD, MOOD_CALM, MOOD_EXCITED, MOOD_GOOD, moodEmotion, moodScore } from './emotionHold';
import { usePersonaMood } from './portraitEmotion';

/* Вкладка «Портрет» раздела «Аватар»: живой портрет — листы ракурсов
   головы (сетка 5×5, по листу на сочетание глаз и рта). Слева — превью
   (смотрит на курсор, по кнопке «говорит») и что портрет уже умеет; справа —
   таблица листов глаза × рот, под ней — рот на гласные (по желанию), и
   выбранный лист по шагам: промпт для генератора картинок → загрузка;
   нарезка свёрнута. Нарезка и фон — на ядре (app/api/portrait_api.py).

   Над таблицей — лицо: обычное или своя эмоция. У эмоции — имя, подсказка
   «когда» и свой набор тех же листов (основной обязателен); персона
   выбирает её сама к ответу. Выбранная эмоция — и в превью слева. */

// Таблица листов: строки — глаза, столбцы — рот
const MATRIX: FaceSheet[][] = [
  ['A', 'B', 'C'],
  ['D', 'E', 'F'],
];
const MOUTHS = ['closed', 'half', 'open'] as const;
const EYES = ['open', 'closed'] as const;
const MAX_EMOTIONS = 16; // как в ядре

// Шаблон ракурсов 5×5 — в исходном проекте (лицензия его ассетов не даёт
// класть файл к себе): там 01_image_template.png
const TEMPLATE_URL = 'https://github.com/rotejin/tomari-guruguru';

const EYES_SHUT = 'the eyes are closed and relaxed, as in the middle of a blink';
const MOUTH_HALF = 'the mouth is slightly open, as when saying "eh"';
const MOUTH_OPEN = 'the mouth is open, as when saying "ah"';
const CHANGES: Record<Exclude<PortraitSheet, 'A'>, string[]> = {
  B: [MOUTH_HALF],
  C: [MOUTH_OPEN],
  D: [EYES_SHUT],
  E: [EYES_SHUT, MOUTH_HALF],
  F: [EYES_SHUT, MOUTH_OPEN],
  VA: ['the mouth is shaped for the vowel "a" as in "father": open wide, jaw dropped, lips relaxed'],
  VI: ['the mouth is shaped for the vowel "i" as in "see": lips spread wide and flat, teeth almost together'],
  VU: ['the mouth is shaped for the vowel "u" as in "moon": lips rounded and pushed forward into a small circle'],
  VE: ['the mouth is shaped for the vowel "e" as in "bed": half open, lips slightly spread'],
  VO: ['the mouth is shaped for the vowel "o" as in "more": lips rounded into an open oval'],
};
const KEEP = 'Nothing else changes: same face, hair, outfit, line art and lighting.';

// Промпты — по-английски, как остальные промпт-паки; имя и подсказка
// эмоции — как их написал пользователь
function buildPrompt(slot: PortraitSheet, name: string, emotion: PortraitEmotion | null): string {
  const feel = emotion ? `"${emotion.name}"${emotion.hint ? ` (${emotion.hint})` : ''}` : '';
  if (slot === 'A' && !emotion) {
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
  if (slot === 'A') {
    return [
      `Redraw the attached 5×5 head-angle sheet of ${name} exactly: the same angles, positions, sizes, colors and background in every cell.`,
      `Change only the facial expression in every cell to ${feel}: the eyebrows, eyes and mouth show it clearly. Eyes open, mouth closed.`,
      KEEP,
    ].join('\n');
  }
  const changes = CHANGES[slot];
  const change =
    changes.length === 1
      ? `Change only one thing in every cell: ${changes[0]}.`
      : `Change only two things in every cell: ${changes[0]}, and ${changes[1]}.`;
  return [
    `Redraw the attached 5×5 head-angle sheet of ${name}${emotion ? ` with the ${feel} expression` : ''} exactly: the same angles, positions, sizes, colors and background in every cell.`,
    change,
    emotion ? `Keep the ${feel} expression. ${KEEP}` : KEEP,
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

// Миниатюра листа — средняя клетка атласа (анфас, на уровне глаз)
function faceStyle(data: PortraitData, sheet: string): CSSProperties {
  const { rows, cols } = data;
  const c = Math.floor(cols / 2);
  const r = Math.floor(rows / 2);
  return {
    backgroundImage: `url(${sheet})`,
    backgroundSize: `${cols * 100}% ${rows * 100}%`,
    backgroundPosition: `${cols > 1 ? (c / (cols - 1)) * 100 : 0}% ${rows > 1 ? (r / (rows - 1)) * 100 : 0}%`,
  };
}

const isVowel = (k: PortraitSheet) => k.startsWith('V');

export default function PortraitCard({
  persona,
  avatar = persona.id,
  modeName,
  onChanged,
}: {
  persona: Persona;
  /** Чей портрет правим: id персоны или «персона@режим» (аватар режима) */
  avatar?: string;
  /** Имя режима, если правим аватар режима */
  modeName?: string;
  /** Листы записаны (раздел «Аватар» обновит, что есть у режимов) */
  onChanged?: () => void;
}) {
  const { t } = useI18n();
  const data = usePortrait(avatar);
  // Лицо, листы которого правим: '' — обычное, иначе id эмоции
  const [emo, setEmo] = useState('');
  const [slot, setSlot] = useState<PortraitSheet>('A');
  const [rows, setRows] = useState(5);
  const [cols, setCols] = useState(5);
  const [offsetX, setOffsetX] = useState(0);
  const [offsetY, setOffsetY] = useState(0);
  const [bg, setBg] = useState<'auto' | 'keep'>('auto');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [talk, setTalk] = useState(false);
  const [miniWidth, setMiniWidth] = useState(0);
  const playerLayout = useCallback(({ width }: PlayerLayout) => setMiniWidth(width), []);
  const [copied, setCopied] = useState(false);
  // Новая эмоция: поле имени вместо кнопки «+ Эмоция»
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState('');
  // Имя и подсказка выбранной эмоции — пишутся, когда поле теряет фокус
  const [emoName, setEmoName] = useState('');
  const [emoHint, setEmoHint] = useState('');
  // Слова условия «по настроению», через запятую — пишутся, когда поле теряет фокус
  const [moodWords, setMoodWords] = useState('');
  const [sure, setSure] = useState(false);
  const [shown, setShown] = useState(false);
  const pip = usePip();
  const portraitOut = isPipOf(pip, 'portrait', persona.id);

  const emotions = data?.emotions ?? [];
  const cur = emo ? (emotions.find((e) => e.id === emo) ?? null) : null;
  // Эмоцию удалили (здесь или в другой вкладке) — к обычному лицу
  useEffect(() => {
    if (emo && data && !cur) setEmo('');
  }, [emo, data, cur]);
  useEffect(() => {
    setEmoName(cur?.name ?? '');
    setEmoHint(cur?.hint ?? '');
    setSure(false);
    setShown(false);
  }, [cur?.id, cur?.name, cur?.hint]);
  const wordsSaved = (cur?.mood?.words ?? []).join(', ');
  useEffect(() => setMoodWords(wordsSaved), [cur?.id, wordsSaved]);
  // Настроение персоны сейчас — видно, встаёт ли лицо в эту эмоцию
  const mood = usePersonaMood(persona.id, !!cur?.mood);

  const sample = t('portrait.talkSample');
  // «Проверить речь» — говорит и вынесенный портрет
  useEffect(() => {
    if (!talk) return;
    setPortraitActivity(persona.id, { speaking: true, listening: false, thinking: false, nod: 0, speech: sample });
    return () => setPortraitActivity(persona.id, null);
  }, [talk, persona.id, sample]);

  // Сетка — общая на портрет, поля и фон — свои у листа
  const sheets: PortraitSheets = cur ? cur.sheets : (data?.sheets ?? {});
  const loaded = sheets[slot];
  const saved = (cur ? cur.settings : data?.settings)?.[slot];
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

  const run = async (call: () => Promise<PortraitData>, after?: (d: PortraitData) => void) => {
    setBusy(true);
    setError('');
    try {
      const d = await call();
      setPortrait(avatar, d);
      after?.(d);
      onChanged?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : t('portrait.error'));
    } finally {
      setBusy(false);
    }
  };
  const key = sheetKey(slot, emo);
  const grid = { rows, cols, offset_x: offsetX, offset_y: offsetY, bg };
  const upload = (file: File) => {
    if (!file.type.startsWith('image/')) return;
    void run(async () => api.putPortraitSheet(avatar, key, { ...grid, data_url: await readAsDataUrl(file) }));
  };
  const reslice = () => void run(() => api.putPortraitSheet(avatar, key, grid));
  const remove = () => void run(() => api.deletePortraitSheet(avatar, key));

  // Лицо и лист: у эмоции без основного листа — сразу он
  const focus = (id: string, sheet: PortraitSheet) => {
    setEmo(id);
    setSlot(sheet);
    setAdding(false);
  };
  const pickFace = (id: string) => {
    const e = emotions.find((x) => x.id === id);
    focus(id, id && !e?.sheets.A ? 'A' : slot);
  };
  const create = (ev: FormEvent) => {
    ev.preventDefault();
    const name = newName.trim();
    if (!name) return;
    void run(
      () => api.putPortraitEmotion(avatar, 'new', name, ''),
      (d) => {
        setNewName('');
        if (d.created) focus(d.created, 'A');
      },
    );
  };
  const cancelAdd = () => {
    setAdding(false);
    setNewName('');
  };
  const saveEmo = () => {
    if (!cur) return;
    const name = emoName.trim();
    if (!name) {
      setEmoName(cur.name);
      return;
    }
    if (name === cur.name && emoHint.trim() === cur.hint) return;
    void run(() => api.putPortraitEmotion(avatar, cur.id, name, emoHint.trim()));
  };
  // Условие «по настроению»: null — только к ответу
  const saveMood = (next: EmotionMood | null) => {
    if (!cur) return;
    void run(() => api.putPortraitEmotion(avatar, cur.id, cur.name, cur.hint, next));
  };
  const saveWords = () => {
    if (!cur?.mood) return;
    const words = moodWords
      .split(/[,;\n]/)
      .map((w) => w.trim())
      .filter(Boolean);
    if (words.join(', ') === wordsSaved) {
      setMoodWords(wordsSaved);
      return;
    }
    saveMood({ ...cur.mood, words });
  };
  const blurOnEnter = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') e.currentTarget.blur();
  };
  const deleteEmo = () => {
    if (!cur) return;
    if (!sure) {
      setSure(true);
      return;
    }
    void run(() => api.deletePortraitEmotion(avatar, cur.id), () => focus('', 'A'));
  };
  const showOnDesk = () => {
    if (!cur) return;
    setError('');
    api
      .showPortraitEmotion(avatar, cur.id)
      .then(() => {
        setShown(true);
        setTimeout(() => setShown(false), 2500);
      })
      .catch((e) => setError(e instanceof ApiError ? e.message : t('portrait.error')));
  };

  // «Сейчас у персоны: …» — и встаёт ли лицо в эту эмоцию
  const moodNow = (e: PortraitEmotion, m: PersonaMood | null) => {
    if (!m) return t('portrait.emo.noState');
    const valence = m.valence > MOOD_GOOD ? 'good' : m.valence < MOOD_BAD ? 'bad' : 'ordinary';
    const arousal = m.arousal > MOOD_EXCITED ? 'excited' : m.arousal < MOOD_CALM ? 'calm' : 'even';
    const now = t('portrait.emo.now', {
      tag: m.tag || '—',
      valence: t(`portrait.emo.now.${valence}`),
      arousal: t(`portrait.emo.now.${arousal}`),
    });
    if (!e.mood || !moodScore(e.mood, e.name, m)) return `${now} ${t('portrait.emo.nowNot')}`;
    if (!e.sheets.A) return `${now} ${t('portrait.emo.nowNoBase')}`;
    const face = moodEmotion(data, m);
    const other = face && face !== e.id ? emotions.find((x) => x.id === face) : null;
    return `${now} ${other ? t('portrait.emo.nowOther', { name: other.name }) : t('portrait.emo.nowFits')}`;
  };

  const prompt = buildPrompt(slot, persona.name, cur);
  const copy = () => {
    void navigator.clipboard?.writeText(prompt).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  const has = (k: PortraitSheet) => !!sheets[k];
  const count = PORTRAIT_SHEETS.filter(has).length;
  const ready = hasPortrait(data);
  // Что умеет портрет (по обычному лицу) и куда вести за недостающим листом
  const base = (k: PortraitSheet) => !!data?.sheets[k];
  const vowelsGot = VOWEL_SHEETS.filter(base);
  const emoReady = emotions.filter((e) => e.sheets.A);
  const abilities = [
    { id: 'look', ok: base('A'), go: () => focus('', 'A') },
    { id: 'blink', ok: base('D'), go: () => focus('', 'D') },
    { id: 'talk', ok: base('B') || base('C'), go: () => focus('', (['B', 'C'] as const).find((k) => !base(k)) ?? 'B') },
    {
      id: 'vowels',
      ok: vowelsGot.length > 0,
      okText: t('portrait.can.vowelsOk', { list: vowelsGot.map((k) => t(`portrait.role.${k}`)).join(', ') }),
      go: () => focus('', VOWEL_SHEETS.find((k) => !base(k)) ?? 'VA'),
    },
    {
      id: 'emotions',
      ok: emoReady.length > 0,
      okText: emoReady.some((e) => e.mood)
        ? t('portrait.can.emotionsMoodOk', {
            list: emoReady.map((e) => e.name).join(', '),
            mood: emoReady.filter((e) => e.mood).map((e) => e.name).join(', '),
          })
        : t('portrait.can.emotionsOk', { list: emoReady.map((e) => e.name).join(', ') }),
      go: () => (emotions.length ? pickFace(emotions[0].id) : setAdding(true)),
    },
  ];

  // Сначала — основной лист: обычный для обычного лица и основного эмоции,
  // основной эмоции — для остальных её листов
  const firstNote =
    !base('A') && (slot !== 'A' || cur)
      ? 'portrait.baseFirst'
      : cur && slot !== 'A' && !cur.sheets.A
        ? 'portrait.emo.baseFirst'
        : '';
  const promptHint = cur
    ? slot === 'A'
      ? 'portrait.emo.promptHintBase'
      : 'portrait.emo.promptHintVariant'
    : slot === 'A'
      ? 'portrait.promptHint'
      : 'portrait.promptHintVariant';
  const sheetTitle = `${cur ? `${cur.name} · ` : ''}${t(`portrait.sheetLong.${slot}`)}`;
  const purpose = cur && slot === 'A' ? t('portrait.emo.purposeA') : isVowel(slot) ? t('portrait.purpose.vowel', { v: t(`portrait.role.${slot}`) }) : t(`portrait.purpose.${slot}`);

  const cell = (k: PortraitSheet) => {
    const src = sheets[k];
    return (
      <button
        key={k}
        type="button"
        className={`portrait-cell ${slot === k ? 'portrait-cell--active' : ''} ${src ? 'portrait-cell--done' : ''}`}
        aria-pressed={slot === k}
        aria-label={t(`portrait.sheetLong.${k}`)}
        onClick={() => setSlot(k)}
      >
        <span className="portrait-cell-face" style={src && data ? faceStyle(data, src) : undefined}>
          {!src && <span className="portrait-cell-plus">+</span>}
        </span>
        <span className="portrait-cell-role">{t(`portrait.role.${k}`)}</span>
      </button>
    );
  };

  return (
    <div className="avatar-split">
      {/* Слева — сам портрет и что он уже умеет */}
      <aside className="avatar-side">
        <div className="card avatar-stage">
          <div className="card-title-row">
            <h3 className="card-title">{t('portrait.title')}</h3>
            <span className="badge hud-code">AVA_01</span>
          </div>
          <div className={`portrait-with-player${miniWidth ? ' portrait-with-player--active' : ''}`} style={{ '--portrait-player-width': `${miniWidth}px` } as CSSProperties}>
          <div className="portrait-preview">
            {ready ? (
              <LivePortrait
                data={data}
                speaking={talk}
                emotion={cur?.id ?? null}
                speech={talk ? { text: sample } : null}
                label={persona.name}
              />
            ) : (
              <div className="portrait-empty">{modeName ? t('avatarMode.noHead', { mode: modeName }) : t('portrait.empty')}</div>
            )}
          </div>
          <WebMiniPlayer onLayout={playerLayout} />
          </div>
          {ready && cur && <div className="field-hint portrait-preview-face">{cur.sheets.A ? t('portrait.emo.previewing', { name: cur.name }) : t('portrait.emo.previewNone')}</div>}
          {ready && (
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

          <div className="field-label avatar-side-label">{t('portrait.can')}</div>
          <ul className="avatar-abilities">
            {abilities.map((a) => (
              <li key={a.id}>
                <button
                  type="button"
                  className={`avatar-ability ${a.ok ? 'avatar-ability--ok' : ''}`}
                  onClick={a.go}
                  title={t('portrait.canGo')}
                >
                  <span className="avatar-ability-mark" aria-hidden>
                    {a.ok ? '✓' : ''}
                  </span>
                  <span className="avatar-ability-text">
                    <span className="avatar-ability-name">{t(`portrait.can.${a.id}`)}</span>
                    <span className="avatar-ability-need">
                      {a.ok ? (a.okText ?? t(`portrait.can.${a.id}Ok`)) : t(`portrait.can.${a.id}Need`)}
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
          <div className="field-hint">{t('portrait.where')}</div>
        </div>
      </aside>

      <div className="avatar-main">
        {/* Лицо (обычное или эмоция) → таблица его листов: глаза × рот и гласные */}
        <div className="card">
          <div className="card-title-row">
            <h3 className="card-title">{t('portrait.sheets')}</h3>
            <span className={`badge ${count ? 'badge--active' : ''}`}>
              {t('portrait.sheetsCount', { n: count, total: PORTRAIT_SHEETS.length })}
            </span>
          </div>
          <p className="avatar-lead">{t('portrait.sheetsLead')}</p>

          <div className="portrait-faces">
            <div className="anim-chips" role="group" aria-label={t('portrait.emo.label')}>
              <span className="avatar-modes-label">{t('portrait.emo.label')}</span>
              <button type="button" className="anim-chip" aria-pressed={!cur} onClick={() => pickFace('')}>
                {t('portrait.emo.base')}
              </button>
              {emotions.map((e) => (
                <button
                  key={e.id}
                  type="button"
                  className={`anim-chip ${e.sheets.A ? '' : 'portrait-face--draft'}`}
                  aria-pressed={cur?.id === e.id}
                  title={e.sheets.A ? e.hint || undefined : t('portrait.emo.noBase')}
                  onClick={() => pickFace(e.id)}
                >
                  {e.name}
                </button>
              ))}
              {adding ? (
                <form className="portrait-face-add" onSubmit={create}>
                  <input
                    className="input"
                    autoFocus
                    value={newName}
                    maxLength={40}
                    placeholder={t('portrait.emo.namePlaceholder')}
                    aria-label={t('portrait.emo.name')}
                    onChange={(e) => setNewName(e.target.value)}
                    onKeyDown={(e) => e.key === 'Escape' && cancelAdd()}
                  />
                  <button type="submit" className="btn btn--primary" disabled={busy || !newName.trim()}>
                    {t('portrait.emo.create')}
                  </button>
                  <button type="button" className="btn btn--ghost" onClick={cancelAdd}>
                    {t('common.cancel')}
                  </button>
                </form>
              ) : (
                emotions.length < MAX_EMOTIONS && (
                  <button type="button" className="anim-chip portrait-face-new" onClick={() => setAdding(true)}>
                    {t('portrait.emo.add')}
                  </button>
                )
              )}
            </div>
            <div className="field-hint">{cur ? t('portrait.emo.lead') : t('portrait.emo.hintBase')}</div>
          </div>

          {cur && (
            <div className="portrait-face-edit">
              <div className="field-grid portrait-face-fields">
                <div className="field">
                  <label className="field-label" htmlFor={`emo-name-${avatar}`}>
                    {t('portrait.emo.name')}
                  </label>
                  <input
                    id={`emo-name-${avatar}`}
                    className="input"
                    value={emoName}
                    maxLength={40}
                    onChange={(e) => setEmoName(e.target.value)}
                    onBlur={saveEmo}
                    onKeyDown={blurOnEnter}
                  />
                </div>
                <div className="field">
                  <label className="field-label" htmlFor={`emo-hint-${avatar}`}>
                    {t('portrait.emo.when')}
                  </label>
                  <input
                    id={`emo-hint-${avatar}`}
                    className="input"
                    value={emoHint}
                    maxLength={200}
                    placeholder={t('portrait.emo.whenPlaceholder')}
                    onChange={(e) => setEmoHint(e.target.value)}
                    onBlur={saveEmo}
                    onKeyDown={blurOnEnter}
                  />
                </div>
              </div>
              <div className="field-hint">{t('portrait.emo.nameHint')}</div>
              <label className="anim-switch-row portrait-face-mood-toggle">
                <span className="switch">
                  <input type="checkbox" checked={!!cur.mood} disabled={busy} onChange={() => saveMood(cur.mood ? null : {})} />
                  <span className="switch-slider" />
                </span>
                <span>{t('portrait.emo.mood')}</span>
              </label>
              {cur.mood ? (
                <div className="portrait-face-mood">
                  <div className="anim-chips" role="group" aria-label={t('portrait.emo.valence')}>
                    <span className="avatar-modes-label">{t('portrait.emo.valence')}</span>
                    {(['', 'good', 'bad'] as const).map((v) => (
                      <button
                        key={v || 'any'}
                        type="button"
                        className="anim-chip"
                        disabled={busy}
                        aria-pressed={(cur.mood?.valence ?? '') === v}
                        onClick={() => saveMood({ ...cur.mood, valence: v || undefined })}
                      >
                        {t(`portrait.emo.valence.${v || 'any'}`)}
                      </button>
                    ))}
                  </div>
                  <div className="anim-chips" role="group" aria-label={t('portrait.emo.arousal')}>
                    <span className="avatar-modes-label">{t('portrait.emo.arousal')}</span>
                    {(['', 'calm', 'excited'] as const).map((v) => (
                      <button
                        key={v || 'any'}
                        type="button"
                        className="anim-chip"
                        disabled={busy}
                        aria-pressed={(cur.mood?.arousal ?? '') === v}
                        onClick={() => saveMood({ ...cur.mood, arousal: v || undefined })}
                      >
                        {t(`portrait.emo.arousal.${v || 'any'}`)}
                      </button>
                    ))}
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor={`emo-words-${avatar}`}>
                      {t('portrait.emo.words')}
                    </label>
                    <input
                      id={`emo-words-${avatar}`}
                      className="input"
                      value={moodWords}
                      maxLength={200}
                      placeholder={t('portrait.emo.wordsPlaceholder')}
                      onChange={(e) => setMoodWords(e.target.value)}
                      onBlur={saveWords}
                      onKeyDown={blurOnEnter}
                    />
                  </div>
                  <div className="field-hint">{t('portrait.emo.wordsHint')}</div>
                  <div className="field-hint portrait-face-now">{moodNow(cur, mood)}</div>
                </div>
              ) : (
                <div className="field-hint">{t('portrait.emo.moodOff')}</div>
              )}
              <div className="anim-editor-foot portrait-face-foot">
                {cur.sheets.A && (
                  <button type="button" className="btn btn--ghost" onClick={showOnDesk} title={t('portrait.emo.showHint')}>
                    {shown ? t('anim.played') : t('portrait.emo.show')}
                  </button>
                )}
                <span className="anim-editor-danger">
                  {sure && <span className="anim-confirm-text">{t('portrait.emo.deleteSure', { name: cur.name })}</span>}
                  <button type="button" className="btn btn--danger" disabled={busy} onClick={deleteEmo}>
                    {sure ? t('common.delete') : t('portrait.emo.delete')}
                  </button>
                  {sure && (
                    <button type="button" className="btn btn--ghost" onClick={() => setSure(false)}>
                      {t('common.cancel')}
                    </button>
                  )}
                </span>
              </div>
            </div>
          )}

          <div className="portrait-matrix" role="group" aria-label={t('portrait.sheets')}>
            <span />
            {MOUTHS.map((m) => (
              <span key={m} className="portrait-matrix-head">
                {t(`portrait.mouth.${m}`)}
              </span>
            ))}
            {MATRIX.map((row, i) => (
              <div key={EYES[i]} className="portrait-matrix-row">
                <span className="portrait-matrix-side">{t(`portrait.eyes.${EYES[i]}`)}</span>
                {row.map(cell)}
              </div>
            ))}
            <div className="portrait-matrix-row portrait-matrix-row--vowels">
              <span className="portrait-matrix-side">{t('portrait.vowels')}</span>
              {VOWEL_SHEETS.map(cell)}
            </div>
          </div>
          <div className="field-hint portrait-vowels-hint">{t('portrait.vowelsHint')}</div>
        </div>

        {/* Выбранный лист: промпт → загрузка, нарезка свёрнута */}
        <div className="card">
          <div className="card-title-row">
            <h3 className="card-title">{sheetTitle}</h3>
            <span className={`badge ${loaded ? 'badge--active' : ''}`}>
              {loaded ? t('portrait.state.done') : t(slot === 'A' ? 'portrait.state.need' : 'portrait.state.optional')}
            </span>
          </div>
          <p className="avatar-lead">{purpose}</p>
          {firstNote && <div className="avatar-note">{t(firstNote)}</div>}

          <div className="avatar-steps">
            <section className="avatar-step">
              <div className="avatar-step-head">
                <span className="avatar-step-n">1</span>
                <span className="avatar-step-title">{t('portrait.step.draw')}</span>
              </div>
              <div className="avatar-step-body">
                <div className="field-hint avatar-step-hint">{t(promptHint)}</div>
                <pre className="art-prompt avatar-prompt">{prompt}</pre>
                <div className="art-prompt-actions">
                  <button type="button" className="btn btn--primary" onClick={copy}>
                    {copied ? t('common.copied') : t('portrait.copyPrompt')}
                  </button>
                  {slot === 'A' && !cur && (
                    <a className="btn btn--ghost" href={TEMPLATE_URL} target="_blank" rel="noreferrer">
                      {t('portrait.template')}
                    </a>
                  )}
                </div>
              </div>
            </section>

            <section className="avatar-step">
              <div className="avatar-step-head">
                <span className="avatar-step-n">2</span>
                <span className="avatar-step-title">{t('portrait.step.upload')}</span>
              </div>
              <div className="avatar-step-body">
                <ArtDropZone title={sheetTitle} spec={t('portrait.spec')} thumb={loaded} onPick={upload} onClear={remove} />
                {busy && <div className="field-hint">{t('portrait.busy')}</div>}
                {error && <div className="field-hint portrait-error">{error}</div>}
              </div>
            </section>
          </div>

          <Collapsible
            title={t('portrait.slicing')}
            summary={t('portrait.slicingSummary', {
              rows,
              cols,
              bg: t(bg === 'keep' ? 'portrait.bgKeep' : 'portrait.bgAuto'),
            })}
          >
            <div className="field-hint">{t('portrait.gridHint')}</div>
            <div className="field-grid portrait-grid">
              <div className="field">
                <label className="field-label">{t('portrait.rows')}</label>
                <NumInput value={rows} min={1} max={10} onChange={setRows} />
              </div>
              <div className="field">
                <label className="field-label">{t('portrait.cols')}</label>
                <NumInput value={cols} min={1} max={10} onChange={setCols} />
              </div>
              <div className="field">
                <label className="field-label">{t('portrait.offsetX')}</label>
                <NumInput value={offsetX} min={0} max={2000} onChange={setOffsetX} />
              </div>
              <div className="field">
                <label className="field-label">{t('portrait.offsetY')}</label>
                <NumInput value={offsetY} min={0} max={2000} onChange={setOffsetY} />
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
            {loaded && (
              <div className="art-prompt-actions">
                <button type="button" className="btn btn--ghost" disabled={busy} onClick={reslice}>
                  {t('portrait.reslice')}
                </button>
              </div>
            )}
          </Collapsible>
        </div>
      </div>
    </div>
  );
}
