/* Загрузка файлов скина: новый скин в библиотеку или замена экранов
   существующего. Строка экрана — кнопка загрузки и дроп-зона именно этого
   экрана: из файла берётся только его слот (комбинированный legacy-файл
   тоже годится), файла без этого экрана — понятная ошибка. Общая кнопка
   «Загрузить файлы» и дроп на панель раскладывают файлы по содержимому.
   Перед сохранением — валидация, runtime-проверка новых файлов в скрытой
   песочнице (smokeTest.ts; ошибки — в общий отчёт панели) и предпросмотр
   на моковых данных. */

import { useEffect, useRef, useState } from 'react';
import type { DragEvent } from 'react';
import { validateSkin } from '../../skins/engine';
import type { SkinScreen } from '../../skins/engine';
import { readSkinMeta } from '../../skins/meta';
import { runSkinSmokeTest } from '../../skins/smokeTest';
import {
  SKIN_SCREENS,
  applyEntryColors,
  assignSkin,
  createSkin,
  updateSkin,
  useSkinFiles,
} from '../../skins/skinStore';
import type { PersonaSkins, SkinEntry } from '../../skins/skinStore';
import type { Persona } from '../../mockData';
import { useI18n } from '../../i18n';
import SkinPreview from './SkinPreview';
import { usePreviewStates } from './usePreviewStates';
import { SCREEN_NAME_KEY, TEMPLATES, downloadFile, formatSize, htmlFiles, skinSlug, skinTitle } from './skinUi';

// Новый файл экрана или null — экран будет убран
type Slot = { html: string; file: string } | null;

interface SkinUploaderProps {
  entry: SkinEntry | null; // null — новый скин
  persona: Persona;
  // Файлы, пришедшие извне (например, от генератора скинов)
  initial?: PersonaSkins;
  onErrors: (errors: string[]) => void;
  onDone: () => void;
}

export default function SkinUploader({ entry, persona, initial, onErrors, onDone }: SkinUploaderProps) {
  const { t, lang } = useI18n();
  const { files: current } = useSkinFiles(entry?.id ?? null);
  const [slots, setSlots] = useState<Partial<Record<SkinScreen, Slot>>>(() => {
    const out: Partial<Record<SkinScreen, Slot>> = {};
    for (const s of SKIN_SCREENS) if (initial?.[s]) out[s] = { html: initial[s]!, file: 'generated' };
    return out;
  });
  const [name, setName] = useState(() => (entry ? '' : (initial && firstMetaName(initial)) || ''));
  const [assign, setAssign] = useState(true);
  const [busy, setBusy] = useState(false);
  const [dropScreen, setDropScreen] = useState<SkinScreen | 'all' | null>(null);
  const screenInputs = useRef<Partial<Record<SkinScreen, HTMLInputElement | null>>>({});
  const allInput = useRef<HTMLInputElement>(null);

  // Runtime-проверка новых файлов: пока идёт — сохранять нельзя, провал —
  // только осознанно («сохранить несмотря на ошибки»)
  const states = usePreviewStates(persona);
  const statesRef = useRef(states);
  statesRef.current = states;
  const [smoke, setSmoke] = useState<{ status: 'idle' | 'running' | 'ok' | 'failed'; errors: string[] }>({
    status: 'idle',
    errors: [],
  });
  const [forceSave, setForceSave] = useState(false);
  // Отчёт панели общий: ошибки валидации последних файлов + runtime-проверки
  const validationErrs = useRef<string[]>([]);
  const smokeErrs = useRef<string[]>([]);
  const onErrorsRef = useRef(onErrors);
  onErrorsRef.current = onErrors;
  const reportValidation = (errs: string[]) => {
    validationErrs.current = errs;
    onErrors([...errs, ...smokeErrs.current]);
  };

  useEffect(() => {
    const list = SKIN_SCREENS.filter((s) => slots[s]).map((s) => [s, slots[s]!] as const);
    setForceSave(false);
    smokeErrs.current = [];
    if (!list.length) {
      setSmoke({ status: 'idle', errors: [] });
      onErrorsRef.current(validationErrs.current);
      return;
    }
    const ctrl = new AbortController();
    setSmoke({ status: 'running', errors: [] });
    onErrorsRef.current(validationErrs.current);
    (async () => {
      const errs: string[] = [];
      for (const [s, slot] of list) {
        const r = await runSkinSmokeTest(slot.html, s, statesRef.current[s], { lang, signal: ctrl.signal });
        if (r.aborted) return;
        errs.push(...r.errors.map((e) => `${slot.file} · ${t(SCREEN_NAME_KEY[s])}: ${e}`));
      }
      smokeErrs.current = errs;
      setSmoke({ status: errs.length ? 'failed' : 'ok', errors: errs });
      onErrorsRef.current([...validationErrs.current, ...errs]);
    })();
    return () => ctrl.abort();
    // Перепроверка — только на смену файлов (язык/снапшот не в счёт)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slots]);

  const readAll = (list: File[]) => Promise.all(list.map((f) => f.text().then((text) => ({ name: f.name, text }))));

  // Имя нового скина по умолчанию — из <meta name="vpc-skin-name"> или имени файла
  const suggestName = (text: string, fileName: string) => {
    if (entry) return;
    setName((cur) => cur || readSkinMeta(text).name || fileName.replace(/\.html?$/i, ''));
  };

  // Файл(ы) в конкретный экран: берётся только его слот
  const onScreenFiles = (screen: SkinScreen, list: File[]) => {
    readAll(list.slice(0, 1)).then(([f]) => {
      if (!f) return;
      const result = validateSkin(f.text);
      const errs: string[] = [];
      if (!result.screens.includes(screen)) {
        errs.push(t('skin.errScreenMissing', { file: f.name, screen: t(SCREEN_NAME_KEY[screen]), id: screen }));
      } else if (!result.ok) {
        errs.push(...result.errors.map((e) => `${f.name}: ${e}`));
      }
      reportValidation(errs);
      if (errs.length) return;
      setSlots((cur) => ({ ...cur, [screen]: { html: f.text, file: f.name } }));
      suggestName(f.text, f.name);
    });
  };

  // Файлы без привязки к экрану: раскладка по найденным в них экранам
  const onAnyFiles = (list: File[]) => {
    readAll(list).then((loaded) => {
      const errs: string[] = [];
      const next: Partial<Record<SkinScreen, Slot>> = {};
      for (const f of loaded) {
        const result = validateSkin(f.text);
        if (!result.ok) {
          errs.push(...result.errors.map((e) => `${f.name}: ${e}`));
          continue;
        }
        for (const s of result.screens) next[s] = { html: f.text, file: f.name };
        suggestName(f.text, f.name);
      }
      reportValidation(errs);
      if (Object.keys(next).length) setSlots((cur) => ({ ...cur, ...next }));
    });
  };

  // Итоговые файлы экранов (для предпросмотра и нового скина)
  const merged: PersonaSkins = {};
  for (const s of SKIN_SCREENS) {
    const slot = slots[s];
    if (slot) merged[s] = slot.html;
    else if (slot === undefined && current[s]) merged[s] = current[s];
  }
  const changed = SKIN_SCREENS.some((s) => slots[s] !== undefined);
  const canSave =
    changed &&
    SKIN_SCREENS.some((s) => merged[s]) &&
    (entry != null || name.trim() !== '') &&
    smoke.status !== 'running' &&
    (smoke.status !== 'failed' || forceSave);

  const save = async () => {
    setBusy(true);
    try {
      if (entry) {
        const screens: Partial<Record<SkinScreen, string | null>> = {};
        for (const s of SKIN_SCREENS) if (slots[s] !== undefined) screens[s] = slots[s]?.html ?? null;
        const saved = await updateSkin(entry.id, { screens });
        if (entry.builtin && assign) await assignSkin(persona.id, saved.id);
      } else {
        const created = await createSkin({ name: name.trim(), screens: merged });
        if (assign) await assignSkin(persona.id, created.id);
      }
      onErrors([]);
      onDone();
    } catch {
      /* ошибка уже в сторе — панель её показывает */
    } finally {
      setBusy(false);
    }
  };

  const slotStatus = (s: SkinScreen) => {
    const slot = slots[s];
    if (slot) return t('skin.slotNew', { file: slot.file });
    if (slot === null) return t('skin.slotRemoved');
    if (entry?.screens[s]) return `${t('skin.slotKeep')} · ${formatSize(entry.sizes[s] ?? 0)}`;
    return t('skin.slotEmpty');
  };

  const dropProps = (target: SkinScreen | 'all') => ({
    onDragOver: (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDropScreen(target);
    },
    onDragLeave: (e: DragEvent) => {
      if (!e.currentTarget.contains(e.relatedTarget as Node)) setDropScreen((cur) => (cur === target ? null : cur));
    },
    onDrop: (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setDropScreen(null);
      const fs = htmlFiles(e.dataTransfer.files);
      if (!fs.length) return;
      if (target === 'all') onAnyFiles(fs);
      else onScreenFiles(target, fs);
    },
  });

  return (
    <div className={`skin-workspace ${dropScreen === 'all' ? 'skin-workspace--drop' : ''}`} {...dropProps('all')}>
      <div className="skin-workspace-title">
        {entry ? t('skin.uploadTitleUpdate', { name: skinTitle(entry, t) }) : t('skin.uploadTitleNew')}
      </div>

      {!entry && (
        <label className="field skin-name-field">
          <span className="field-label">{t('skin.skinName')}</span>
          <input className="input" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
        </label>
      )}

      <div className="skin-screens">
        {SKIN_SCREENS.map((s) => (
          <div
            key={s}
            className={`skin-screen-row ${dropScreen === s ? 'skin-screen-row--drop' : ''}`}
            {...dropProps(s)}
          >
            <span className="skin-screen-name">{t(SCREEN_NAME_KEY[s])}</span>
            <button
              type="button"
              className="btn btn--ghost"
              title={t('skin.uploadScreen')}
              onClick={() => screenInputs.current[s]?.click()}
            >
              ⬆ {t(SCREEN_NAME_KEY[s])}
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() =>
                downloadFile(
                  `skin-${entry ? skinSlug(entry.name) : persona.id}.${s}.html`,
                  current[s] && entry ? applyEntryColors(current[s]!, entry) : TEMPLATES[s],
                )
              }
            >
              ⬇ {t(current[s] && entry ? 'skin.currentFile' : 'skin.templateFile')}
            </button>
            <span className={`skin-slot-status ${slots[s] ? 'skin-slot-status--new' : ''}`}>{slotStatus(s)}</span>
            {slots[s] !== undefined ? (
              <button
                type="button"
                className="btn btn--icon"
                onClick={() =>
                  setSlots((cur) => {
                    const next = { ...cur };
                    delete next[s];
                    return next;
                  })
                }
              >
                {t('skin.slotUndo')}
              </button>
            ) : (
              entry?.screens[s] &&
              !entry.builtin && (
                <button
                  type="button"
                  className="btn btn--icon"
                  title={t('skin.slotRemove')}
                  onClick={() => setSlots((cur) => ({ ...cur, [s]: null }))}
                >
                  ✕
                </button>
              )
            )}
            <input
              ref={(el) => {
                screenInputs.current[s] = el;
              }}
              type="file"
              accept=".html,text/html"
              hidden
              onChange={(e) => {
                const fs = Array.from(e.target.files ?? []);
                if (fs.length) onScreenFiles(s, fs);
                e.target.value = '';
              }}
            />
          </div>
        ))}
      </div>
      <p className="ctx-note">{t('skin.dropHint')}</p>

      <div className="skin-actions">
        <button type="button" className="btn btn--ghost" onClick={() => allInput.current?.click()}>
          {t('skin.uploadFiles')}
        </button>
        <input
          ref={allInput}
          type="file"
          accept=".html,text/html"
          multiple
          hidden
          onChange={(e) => {
            const fs = Array.from(e.target.files ?? []);
            if (fs.length) onAnyFiles(fs);
            e.target.value = '';
          }}
        />
      </div>

      {changed && SKIN_SCREENS.some((s) => merged[s]) && (
        <SkinPreview
          files={merged}
          persona={persona}
          colors={entry?.colors}
          hueShift={entry?.hueShift}
          states={states}
        />
      )}

      {smoke.status !== 'idle' && (
        <p className={`ctx-note skin-smoke skin-smoke--${smoke.status}`}>
          {t(
            smoke.status === 'running'
              ? 'skin.smokeRunning'
              : smoke.status === 'ok'
                ? 'skin.smokeOk'
                : 'skin.smokeFailed',
            { n: smoke.errors.length },
          )}
        </p>
      )}
      {smoke.status === 'failed' && (
        <label className="checkbox-row">
          <input type="checkbox" checked={forceSave} onChange={(e) => setForceSave(e.target.checked)} />
          {t('skin.smokeForce')}
        </label>
      )}

      {(!entry || entry.builtin) && (
        <label className="checkbox-row">
          <input type="checkbox" checked={assign} onChange={(e) => setAssign(e.target.checked)} />
          {t('skin.assignAfter', { name: persona.name })}
        </label>
      )}

      <div className="skin-actions">
        <button type="button" className="btn btn--primary" disabled={!canSave || busy} onClick={save}>
          {t(entry ? 'skin.saveFiles' : 'skin.createSkin')}
        </button>
        <button type="button" className="btn btn--ghost" onClick={onDone}>
          {t('skin.cancel')}
        </button>
      </div>
    </div>
  );
}

function firstMetaName(files: PersonaSkins): string | undefined {
  for (const s of SKIN_SCREENS) {
    const html = files[s];
    if (html) {
      const n = readSkinMeta(html).name;
      if (n) return n;
    }
  }
  return undefined;
}
