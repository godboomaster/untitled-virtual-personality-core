/* Генерация скина нейросетью прямо в приложении. Пользователь выбирает
   экраны, описывает стиль, основу (шаблон экрана или экран скина из
   библиотеки — «перекрасить») и, по желанию, провайдера. Экраны идут
   по одному на запрос (POST /api/skins/generate, SSE с прогрессом).

   Перед кодом — арт-направление: «Придумать направления» (POST
   /api/skins/direction) даёт карточки на выбор (SkinDirectionPicker),
   выбранное (с заметкой пользователя) уходит с КАЖДЫМ запросом — всеми
   экранами и попытками исправления. «Пропустить» шлёт первый запрос без
   направления: сервер подбирает его сам (событие status=direction), и оно
   используется для остальных запросов.

   Каждый ответ проверяется: validateSkin, сохранность hook-точек основы
   (missingHooks — пропавшая точка = пропавшая часть интерфейса), затем
   runtime-проверка в скрытой песочнице (smokeTest.ts). Не прошёл — ошибки и прошлый файл уходят
   обратно модели запросом на исправление; всего до MAX_ATTEMPTS попыток на
   экран. Прошедшие файлы отдаются в обычную загрузку (onGenerated →
   SkinUploader): там предпросмотр и сохранение. Журнал попыток виден. */

import { useEffect, useRef, useState } from 'react';
import { SkinGenStreamError, api, streamSkinGeneration } from '../../api';
import type { ApiSkinDirection, ApiSkinGenResult } from '../../api';
import { useApiOnline, useApiProviders, useApiWebchat } from '../../apiData';
import { buildSkinContractDoc } from '../../skins/contract';
import { validateSkin } from '../../skins/engine';
import { readSkinMeta } from '../../skins/meta';
import type { SkinScreen } from '../../skins/engine';
import { runSkinSmokeTest } from '../../skins/smokeTest';
import { SKIN_SCREENS, applyEntryColors, findEntry, loadSkinFiles } from '../../skins/skinStore';
import type { PersonaSkins, SkinEntry } from '../../skins/skinStore';
import type { Persona } from '../../mockData';
import { useI18n } from '../../i18n';
import Select from '../Select';
import type { SelectOption } from '../Select';
import SkinDirectionPicker from './SkinDirectionPicker';
import { SCREEN_NAME_KEY, TEMPLATES, downloadFile, skinTitle } from './skinUi';
import { usePreviewStates } from './usePreviewStates';

const MAX_ATTEMPTS = 3;
const DESCRIPTION_MAX = 4000;
// Направлений за запрос и всего показанных (сервер принимает до 12 исключений)
const DIRECTION_COUNT = 3;
const DIRECTIONS_MAX = 12;

type Phase = 'gen' | 'check' | 'ok' | 'fail' | 'error' | 'cancel';

interface LogEntry {
  id: number;
  screen: SkinScreen;
  attempt: number;
  phase: Phase;
  chars: number;
  elapsed: number;
  note?: string;
  dirNote?: string; // стадия арт-направления на сервере (режим «пропустить»)
  errors?: string[];
  provider?: string | null;
  model?: string | null;
}

interface Failed {
  screen: SkinScreen;
  html: string | null; // последняя попытка (для ручной доработки)
  errors: string[];
}

// Сигналы прерывания цикла генерации
class Stop extends Error {}

// Модель часто оставляет имя основы (<meta name="vpc-skin-name"> шаблона —
// «VPC Default»): тогда имя — название арт-направления, а без него — начало
// описания пользователя (до ~40 символов)
function nameGenerated(html: string, base: string, description: string, dirName?: string): string {
  const name = readSkinMeta(html).name;
  const desc = (dirName?.trim() || description.trim()).replace(/\s+/g, ' ');
  if (!name || !desc || name !== readSkinMeta(base).name) return html;
  let short = desc;
  if (!dirName?.trim() && short.length > 40) {
    short = short.slice(0, 40);
    const sp = short.lastIndexOf(' ');
    short = (sp > 20 ? short.slice(0, sp) : short).replace(/[\s.,;:!?-]+$/, '') + '…';
  }
  const esc = short.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  let done = false;
  // Первая настоящая мета имени (примеры в комментариях-инструкциях не трогаем)
  return html.replace(/<!--[\s\S]*?-->|<meta\b[^>]*>/gi, (tag) => {
    if (done || tag.startsWith('<!--') || !/\bname\s*=\s*["']vpc-skin-name["']/i.test(tag)) return tag;
    done = true;
    return `<meta name="vpc-skin-name" content="${esc}" />`;
  });
}

// Hook-атрибуты, которые bridge заполняет и слушает: каждая точка основы
// должна дожить до результата (переносить и оборачивать можно, удалять — нет)
const HOOK_ATTRS = ['data-vpc', 'data-vpc-field', 'data-vpc-action', 'data-vpc-setting'] as const;
const HOOK_SELECTOR = HOOK_ATTRS.map((a) => `[${a}]`).join(',');
const MISSING_HOOKS_SHOWN = 30;

// Точки внутри узла, включая содержимое <template> (querySelectorAll в него не заходит)
function collectHooks(root: ParentNode, out: Set<string>) {
  root.querySelectorAll(HOOK_SELECTOR).forEach((el) => {
    for (const a of HOOK_ATTRS) {
      const v = el.getAttribute(a);
      if (v) out.add(`[${a}="${v}"]`);
    }
  });
  root.querySelectorAll('template').forEach((tpl) => collectHooks(tpl.content, out));
}

// Hook-точки экрана основы, которых нет в результате. У основы берём только
// её блок [data-vpc-screen=screen] (у legacy-файла в нём все три экрана)
function missingHooks(base: string, html: string, screen: SkinScreen): string[] {
  const parser = new DOMParser();
  const baseDoc = parser.parseFromString(base, 'text/html');
  const want = new Set<string>();
  collectHooks(baseDoc.querySelector(`[data-vpc-screen="${screen}"]`) ?? baseDoc, want);
  const got = new Set<string>();
  collectHooks(parser.parseFromString(html, 'text/html'), got);
  return [...want].filter((h) => !got.has(h));
}

function missingHooksError(lang: 'ru' | 'en', missing: string[]): string {
  const shown = missing.slice(0, MISSING_HOOKS_SHOWN).join(', ');
  const more = missing.length - MISSING_HOOKS_SHOWN;
  return lang === 'en'
    ? `Hook points of the base file are missing (${missing.length}): ${shown}${more > 0 ? ` and ${more} more` : ''}. ` +
        'Bring every one of them back: move, wrap and restyle the elements freely, but never remove or rename them — ' +
        'without them parts of the interface are not drawn.'
    : `Из базового файла пропали hook-точки (${missing.length}): ${shown}${more > 0 ? ` и ещё ${more}` : ''}. ` +
        'Верни их все: элементы можно переносить, оборачивать и перестилизовать, но не удалять и не переименовывать — ' +
        'без них часть интерфейса не рисуется.';
}

interface SkinGeneratorProps {
  persona: Persona;
  entries: SkinEntry[];
  onGenerated: (files: PersonaSkins) => void;
}

export default function SkinGenerator(props: SkinGeneratorProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  // Тело монтируется при первом раскрытии и дальше живёт (свёрнутое —
  // скрыто): генерация не обрывается от сворачивания
  const [mounted, setMounted] = useState(false);
  return (
    <div className="skin-gen">
      <button
        type="button"
        className={`btn btn--ghost ${open ? 'skin-btn--open' : ''}`}
        onClick={() => {
          setOpen((o) => !o);
          setMounted(true);
        }}
      >
        ✨ {t('skin.genToggle')}
      </button>
      {mounted && (
        <div hidden={!open}>
          <GeneratorBody {...props} />
        </div>
      )}
    </div>
  );
}

function GeneratorBody({ persona, entries, onGenerated }: SkinGeneratorProps) {
  const { t, lang } = useI18n();
  const apiOnline = useApiOnline();
  const providers = useApiProviders();
  const webchat = useApiWebchat();
  const states = usePreviewStates(persona);
  const statesRef = useRef(states);
  statesRef.current = states;

  const [screens, setScreens] = useState<SkinScreen[]>(['chat']);
  const [description, setDescription] = useState('');
  const [base, setBase] = useState('template'); // 'template' или id скина библиотеки
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [running, setRunning] = useState(false);
  // Арт-направления: показанные карточки, выбранная, заметка к ней
  const [directions, setDirections] = useState<ApiSkinDirection[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [dirNote, setDirNote] = useState('');
  const [dirLoading, setDirLoading] = useState(false);
  const [dirError, setDirError] = useState<string | null>(null);
  const dirCtrlRef = useRef<AbortController | null>(null);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [failed, setFailed] = useState<Failed[]>([]);
  const [summary, setSummary] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const ctrlRef = useRef<AbortController | null>(null);
  const logId = useRef(0);

  // Уход со страницы/смена персоны — генерацию останавливаем
  useEffect(
    () => () => {
      ctrlRef.current?.abort();
      dirCtrlRef.current?.abort();
    },
    [],
  );

  // Основа уже удалена из библиотеки — назад к шаблону
  const baseEntry = base === 'template' ? null : (entries.find((e) => e.id === base) ?? null);
  useEffect(() => {
    if (base !== 'template' && !entries.some((e) => e.id === base)) setBase('template');
  }, [base, entries]);

  const baseOptions: SelectOption[] = [
    { value: 'template', label: t('skin.genBaseTemplate') },
    ...entries.map((e) => ({
      value: e.id,
      label: skinTitle(e, t),
      hint: SKIN_SCREENS.filter((s) => e.screens[s])
        .map((s) => t(SCREEN_NAME_KEY[s]))
        .join(', '),
    })),
  ];

  // Провайдеры: цепочка по умолчанию, облачные с ключом, Ollama, веб-чаты
  const providerOptions: SelectOption[] = [
    { value: '', label: t('skin.genProviderDefault') },
    ...(providers ?? [])
      .filter((p) => p.key_set)
      .map((p) => ({ value: p.id, label: p.local ? p.name : `${p.name} · ${p.model}` })),
    ...(webchat?.sites ?? []).map((site) => ({ value: `webchat:${site}`, label: t('skin.genWebchat', { site }) })),
  ];
  const cloud = (providers ?? []).find((p) => p.id === provider && !p.local);

  const addLog = (e: Omit<LogEntry, 'id'>) => {
    const id = ++logId.current;
    setLog((cur) => [...cur, { ...e, id }]);
    return id;
  };
  const patchLog = (id: number, patch: Partial<LogEntry>) =>
    setLog((cur) => cur.map((e) => (e.id === id ? { ...e, ...patch } : e)));

  // Проверка разметки: файл обязан содержать свой экран, пройти валидатор
  // и сохранить все hook-точки основы
  const checkMarkup = (html: string, screen: SkinScreen, base: string): string[] => {
    const v = validateSkin(html);
    const errs: string[] = [];
    if (!v.screens.includes(screen)) {
      errs.push(t('skin.errScreenMissing', { file: 'skin', screen: t(SCREEN_NAME_KEY[screen]), id: screen }));
    }
    const missing = missingHooks(base, html, screen);
    if (missing.length) errs.push(missingHooksError(lang === 'en' ? 'en' : 'ru', missing));
    return [...errs, ...v.errors];
  };

  const modelParam = () => (cloud && model.trim() ? model.trim() : null);

  // more — «ещё варианты»: новые карточки добавляются к показанным, названия
  // показанных уходят в exclude; иначе — новый набор вместо старого
  const proposeDirections = async (more: boolean) => {
    const ctrl = new AbortController();
    dirCtrlRef.current = ctrl;
    setDirLoading(true);
    setDirError(null);
    try {
      const res = await api.skinDirections(
        {
          description: description.trim(),
          locale: lang,
          count: DIRECTION_COUNT,
          exclude: more ? directions.map((d) => d.name).slice(-DIRECTIONS_MAX) : [],
          provider: provider || null,
          model: modelParam(),
        },
        ctrl.signal,
      );
      if (more) {
        setDirections((cur) => [...cur, ...res.directions].slice(0, DIRECTIONS_MAX));
      } else {
        setDirections(res.directions);
        setSelected(res.directions.length ? 0 : null);
        setDirNote('');
      }
    } catch (e) {
      if (!ctrl.signal.aborted) setDirError(e instanceof Error ? e.message : String(e));
    } finally {
      if (dirCtrlRef.current === ctrl) dirCtrlRef.current = null;
      setDirLoading(false);
    }
  };

  // chosen — выбранное направление (с заметкой) или null: «пропустить»,
  // направление подберёт сервер на первом запросе
  const run = async (chosen: ApiSkinDirection | null) => {
    const ctrl = new AbortController();
    ctrlRef.current = ctrl;
    const signal = ctrl.signal;
    setRunning(true);
    setDirError(null);
    setLog([]);
    setFailed([]);
    setSummary(null);

    const contract = buildSkinContractDoc(lang);
    const entry = baseEntry ? (findEntry(baseEntry.id) ?? null) : null;
    const done: PersonaSkins = {};
    const failedList: Failed[] = [];
    let stopped = false;
    let direction = chosen;

    try {
      const baseFiles = entry ? await loadSkinFiles(entry.id) : {};
      for (const screen of SKIN_SCREENS.filter((s) => screens.includes(s))) {
        const skinBase = entry && baseFiles[screen] ? applyEntryColors(baseFiles[screen]!, entry) : null;
        let prev: string | null = null;
        let errors: string[] = [];
        let lastHtml: string | null = null;
        let ok = false;

        for (let attempt = 1; attempt <= MAX_ATTEMPTS && !ok; attempt++) {
          const id = addLog({ screen, attempt, phase: 'gen', chars: 0, elapsed: 0 });
          let res: ApiSkinGenResult;
          try {
            res = await streamSkinGeneration(
              {
                screen,
                description: description.trim(),
                base_html: skinBase ?? TEMPLATES[screen],
                base_kind: skinBase ? 'skin' : 'template',
                contract_doc: contract,
                locale: lang,
                previous_html: attempt > 1 ? prev : null,
                errors: attempt > 1 ? errors : [],
                provider: provider || null,
                model: modelParam(),
                direction,
              },
              (chars, elapsed) => patchLog(id, { chars, elapsed }),
              (status, info) => {
                if (status === 'direction_start') patchLog(id, { dirNote: t('skin.genDirStage') });
                else if (status === 'direction' && info.direction) {
                  // Подобранное сервером — для остальных экранов и исправлений;
                  // в карточки — чтобы было видно и можно было взять снова
                  const got = info.direction;
                  direction = got;
                  patchLog(id, { dirNote: t('skin.genDirPicked', { name: got.name }) });
                  // (во время генерации карточки заблокированы — directions не менялся)
                  const next = [...directions, got].slice(-DIRECTIONS_MAX);
                  setDirections(next);
                  setSelected(next.length - 1);
                  setDirNote('');
                } else if (status === 'direction_failed') patchLog(id, { dirNote: t('skin.genDirFailed') });
                else if (status === 'retry_small_limit') patchLog(id, { note: t('skin.genRetryLimit') });
                // Ответ оборван лимитом вывода — сервер дописывает его продолжениями
                else if (status === 'continue') patchLog(id, { note: t('skin.genContinue', { n: info.round ?? 1 }) });
              },
              signal,
            );
          } catch (e) {
            if (signal.aborted) throw new Stop();
            if (e instanceof SkinGenStreamError && e.code === 'no_html') {
              // Модель ответила не документом — это попытка, а не авария
              errors = [t('skin.genErrNoHtml')];
              prev = null;
              patchLog(id, { phase: 'fail', errors });
              continue;
            }
            patchLog(id, { phase: 'error', note: e instanceof Error ? e.message : String(e) });
            throw new Stop();
          }
          patchLog(id, { chars: res.chars, provider: res.provider, model: res.model, elapsed: res.elapsed });
          lastHtml = res.html;

          if (res.truncated) {
            // Не закрыт и после продолжений сервера: обрезанный файл править
            // бессмысленно — заново от основы, компактнее
            errors = [t('skin.genErrTruncated', { kb: Math.round(res.html.length / 1024) })];
            prev = null;
            patchLog(id, { phase: 'fail', errors });
            continue;
          }

          patchLog(id, { phase: 'check' });
          const errs = checkMarkup(res.html, screen, skinBase ?? TEMPLATES[screen]);
          if (!errs.length) {
            const smoke = await runSkinSmokeTest(res.html, screen, statesRef.current[screen], { lang, signal });
            if (smoke.aborted) throw new Stop();
            errs.push(...smoke.errors);
          }
          if (errs.length) {
            prev = res.html;
            errors = errs;
            patchLog(id, { phase: 'fail', errors: errs });
          } else {
            done[screen] = nameGenerated(res.html, skinBase ?? TEMPLATES[screen], description, direction?.name);
            ok = true;
            patchLog(id, { phase: 'ok' });
          }
        }
        if (!ok) failedList.push({ screen, html: lastHtml, errors });
      }
    } catch (e) {
      stopped = true;
      if (signal.aborted) {
        setLog((cur) => cur.map((x) => (x.phase === 'gen' || x.phase === 'check' ? { ...x, phase: 'cancel' } : x)));
      } else if (!(e instanceof Stop)) {
        addLog({ screen: screens[0] ?? 'chat', attempt: 0, phase: 'error', chars: 0, elapsed: 0, note: String(e) });
      }
    } finally {
      if (ctrlRef.current === ctrl) ctrlRef.current = null;
      setRunning(false);
    }

    if (signal.aborted) return;
    setFailed(failedList);
    const got = SKIN_SCREENS.filter((s) => done[s]);
    if (got.length) {
      setSummary(t('skin.genDone', { list: got.map((s) => t(SCREEN_NAME_KEY[s])).join(', ') }));
      onGenerated(done);
    } else if (!stopped) {
      setSummary(t('skin.genNothing'));
    }
  };

  const copyReport = (f: Failed) => {
    navigator.clipboard.writeText(t('skin.reportPrefix') + f.errors.join('\n')).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  const phaseText = (e: LogEntry) => {
    switch (e.phase) {
      case 'gen':
        return e.chars > 0
          ? t('skin.genPhaseGen', { kb: (e.chars / 1024).toFixed(1), sec: e.elapsed })
          : t('skin.genPhaseWait', { sec: e.elapsed });
      case 'check':
        return t('skin.genPhaseCheck');
      case 'ok':
        return t('skin.genPhaseOk');
      case 'fail':
        return t('skin.genPhaseFail', { n: e.errors?.length ?? 0 });
      case 'error':
        return t('skin.genPhaseError', { msg: e.note ?? '' });
      case 'cancel':
        return t('skin.genCancelled');
    }
  };

  const busy = running || dirLoading;
  const canRun = apiOnline && !busy && screens.length > 0;
  const chosen =
    selected !== null && directions[selected]
      ? { ...directions[selected], note: dirNote.trim() || undefined }
      : null;

  return (
    <div className="skin-workspace skin-gen-body">
      <div className="skin-workspace-title">{t('skin.genTitle')}</div>
      <p className="ctx-note">{t('skin.genHint', { max: MAX_ATTEMPTS })}</p>
      {!apiOnline && <p className="ctx-note skin-gen-warn">{t('skin.genOffline')}</p>}

      <div className="skin-gen-row">
        <span className="field-label">{t('skin.genScreens')}</span>
        {SKIN_SCREENS.map((s) => (
          <label key={s} className="checkbox-row skin-gen-screen">
            <input
              type="checkbox"
              checked={screens.includes(s)}
              disabled={busy}
              onChange={(e) =>
                setScreens((cur) => (e.target.checked ? [...cur, s] : cur.filter((x) => x !== s)))
              }
            />
            {t(SCREEN_NAME_KEY[s])}
          </label>
        ))}
      </div>

      <label className="field">
        <span className="field-label">{t('skin.genDescription')}</span>
        <textarea
          className="input pcreate-textarea"
          rows={3}
          value={description}
          maxLength={DESCRIPTION_MAX}
          disabled={busy}
          placeholder={t('skin.genDescriptionPh')}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>

      <div className="skin-gen-grid">
        {/* div, а не label: клик по подписи не должен «нажимать» триггер Select */}
        <div className="field">
          <span className="field-label">{t('skin.genBase')}</span>
          <Select value={base} options={baseOptions} onChange={setBase} disabled={busy} />
        </div>
        {providers && (
          <div className="field">
            <span className="field-label">{t('skin.genProvider')}</span>
            <Select value={provider} options={providerOptions} onChange={setProvider} disabled={busy} />
          </div>
        )}
        {cloud && (
          <label className="field">
            <span className="field-label">{t('skin.genModel')}</span>
            <input
              className="input"
              value={model}
              maxLength={200}
              disabled={busy}
              placeholder={cloud.model}
              onChange={(e) => setModel(e.target.value)}
            />
          </label>
        )}
      </div>
      {baseEntry && screens.some((s) => !baseEntry.screens[s]) && (
        <p className="ctx-note">
          {t('skin.genBaseMissing', {
            list: screens
              .filter((s) => !baseEntry.screens[s])
              .map((s) => t(SCREEN_NAME_KEY[s]))
              .join(', '),
          })}
        </p>
      )}

      {directions.length > 0 && (
        <>
          <div className="skin-workspace-title skin-dir-title">{t('skin.dirTitle')}</div>
          <SkinDirectionPicker
            directions={directions}
            selected={selected}
            onSelect={setSelected}
            note={dirNote}
            onNote={setDirNote}
            disabled={busy}
          />
        </>
      )}
      {dirLoading && <p className="ctx-note skin-dir-loading">{t('skin.dirLoading')}</p>}
      {dirError && <p className="ctx-note skin-gen-warn">{t('skin.dirError', { msg: dirError })}</p>}

      <div className="skin-actions">
        {chosen && (
          <button
            type="button"
            className="btn btn--primary skin-dir-start"
            disabled={!canRun}
            title={chosen.name}
            onClick={() => void run(chosen)}
          >
            {t('skin.genStartDir', { name: chosen.name })}
          </button>
        )}
        <button
          type="button"
          className={`btn ${chosen ? 'btn--ghost' : 'btn--primary'}`}
          disabled={!apiOnline || busy}
          onClick={() => void proposeDirections(false)}
        >
          {directions.length ? t('skin.dirProposeAgain') : t('skin.dirPropose')}
        </button>
        {directions.length > 0 && (
          <button
            type="button"
            className="btn btn--ghost"
            disabled={!apiOnline || busy || directions.length >= DIRECTIONS_MAX}
            onClick={() => void proposeDirections(true)}
          >
            {t('skin.dirMore')}
          </button>
        )}
        <button type="button" className="btn btn--ghost" disabled={!canRun} onClick={() => void run(null)}>
          {t('skin.dirSkip')}
        </button>
        {busy && (
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => {
              ctrlRef.current?.abort();
              dirCtrlRef.current?.abort();
            }}
          >
            {t('skin.genCancel')}
          </button>
        )}
      </div>

      {log.length > 0 && (
        <ol className="skin-gen-log">
          {log.map((e) => (
            <li key={e.id} className={`skin-gen-entry skin-gen-entry--${e.phase}`}>
              <div className="skin-gen-entry-head">
                <b>{e.attempt > 0 ? t('skin.genAttempt', { screen: t(SCREEN_NAME_KEY[e.screen]), n: e.attempt, max: MAX_ATTEMPTS }) : '—'}</b>
                <span>{phaseText(e)}</span>
                {e.provider && (
                  <span className="skin-gen-by">{e.model ? `${e.provider} · ${e.model}` : e.provider}</span>
                )}
              </div>
              {e.dirNote && <div className="skin-gen-note skin-gen-dir">{e.dirNote}</div>}
              {e.note && e.phase !== 'error' && <div className="skin-gen-note">{e.note}</div>}
              {e.errors && e.errors.length > 0 && (
                <details className="skin-gen-errors">
                  <summary>{t('skin.genShowErrors', { n: e.errors.length })}</summary>
                  <ul className="skin-error-list">
                    {e.errors.map((err, i) => (
                      <li key={i}>{err}</li>
                    ))}
                  </ul>
                </details>
              )}
            </li>
          ))}
        </ol>
      )}

      {summary && <p className="ctx-note skin-gen-summary">{summary}</p>}

      {failed.map((f) => (
        <div className="skin-report" key={f.screen}>
          <div className="skin-report-title">
            {t('skin.genFailed', { screen: t(SCREEN_NAME_KEY[f.screen]), max: MAX_ATTEMPTS })}
          </div>
          <ul className="skin-error-list">
            {f.errors.map((err, i) => (
              <li key={i}>{err}</li>
            ))}
          </ul>
          <div className="skin-actions">
            {f.html && (
              <button
                type="button"
                className="btn btn--ghost"
                onClick={() => downloadFile(`skin-generated.${f.screen}.html`, f.html!)}
              >
                {t('skin.genDownloadLast')}
              </button>
            )}
            <button type="button" className="btn btn--ghost" onClick={() => copyReport(f)}>
              {copied ? t('skin.copied') : t('skin.copyReport')}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
