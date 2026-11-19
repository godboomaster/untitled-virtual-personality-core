/* Скин через свою нейросеть: промпт генерации для любого чат-бота
   (ChatGPT, Claude, DeepSeek…) — тот же текст, что генератор ядра
   отправляет модели (skins/manualPrompt.ts), только генерирует человек
   сам. Работает без ядра (телефон без компьютера) и на компьютере.

   Шаги: экраны, основа (шаблон или скин библиотеки — «перекрасить»),
   описание стиля; по желанию — арт-направление: промпт идей → ответ
   чат-бота вставляется сюда, разбирается в карточки (SkinDirectionPicker),
   выбранное входит в промпт каждого экрана — экраны выходят одним продуктом.
   Промпт экрана копируется (или скачивается .txt); ответ чат-бота
   вставляется в поле ответа: проверка разметки (validateSkin + hook-точки
   основы), при ошибках — промпт исправления. Прошедшие экраны уходят в
   обычную загрузку (onGenerated → SkinUploader: runtime-проверка,
   предпросмотр, сохранение). Ответ можно и сохранить файлом .html и
   загрузить через «Новый скин из файлов». */

import { useMemo, useState } from 'react';
import { detectScreens } from '../../skins/engine';
import type { SkinScreen } from '../../skins/engine';
import { buildSkinContractDoc } from '../../skins/contract';
import {
  PROMPT_DIRECTION_COUNT,
  PROMPT_MAX_DESCRIPTION,
  PROMPT_MAX_HTML,
  buildDirectionMessages,
  buildScreenMessages,
  chatbotPrompt,
  extractHtmlDocument,
  normalizeDirection,
  parseDirections,
  prepareBaseHtml,
  restoreAssets,
  stripAssets,
} from '../../skins/manualPrompt';
import type { PromptBaseKind } from '../../skins/manualPrompt';
import { SKIN_SCREENS, applyEntryColors, useSkinFiles } from '../../skins/skinStore';
import type { PersonaSkins, SkinEntry } from '../../skins/skinStore';
import { useI18n } from '../../i18n';
import Select from '../Select';
import type { SelectOption } from '../Select';
import SkinDirectionPicker from './SkinDirectionPicker';
import { SCREEN_NAME_KEY, TEMPLATES, checkSkinMarkup, copyText, downloadFile, formatSize, skinTitle } from './skinUi';

interface SkinPromptKitProps {
  entries: SkinEntry[];
  onGenerated: (files: PersonaSkins) => void;
}

interface ScreenBase {
  html: string; // основа как есть (с перекраской скина) — для проверки hook-точек
  kind: PromptBaseKind;
  prepared: string; // что уходит модели (stripBase + stripAssets)
  assets: string[];
}

interface Checked {
  screen: SkinScreen | null;
  errors: string[];
  html: string | null; // ответ (с возвращёнными ассетами) — для промпта исправления
}

const byteLen = (s: string) => new TextEncoder().encode(s).length;

export default function SkinPromptKit(props: SkinPromptKitProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  return (
    <div className="skin-gen skin-kit">
      <button
        type="button"
        className={`btn btn--ghost ${open ? 'skin-btn--open' : ''}`}
        onClick={() => setOpen((o) => !o)}
      >
        ⧉ {t('skin.kitToggle')}
      </button>
      {open && <KitBody {...props} />}
    </div>
  );
}

function KitBody({ entries, onGenerated }: SkinPromptKitProps) {
  const { t, lang } = useI18n();
  const locale = lang === 'en' ? 'en' : 'ru';

  const [screens, setScreens] = useState<SkinScreen[]>(['chat', 'dossier', 'room']);
  const [description, setDescription] = useState('');
  const [base, setBase] = useState('template'); // 'template' или id скина библиотеки
  const [dirText, setDirText] = useState('');
  const [selected, setSelected] = useState<number | null>(null);
  const [dirNote, setDirNote] = useState('');
  const [answer, setAnswer] = useState('');
  const [checked, setChecked] = useState<Checked | null>(null);
  const [ready, setReady] = useState<PersonaSkins>({});
  // Последнее скопированное (подпись «Скопировано ✓») и сбой копирования
  const [copied, setCopied] = useState<string | null>(null);
  const [copyFailed, setCopyFailed] = useState(false);

  const baseEntry = base === 'template' ? null : (entries.find((e) => e.id === base) ?? null);
  const { files: baseFiles, loading: baseLoading } = useSkinFiles(baseEntry?.id ?? null);
  const order = SKIN_SCREENS.filter((s) => screens.includes(s));

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

  // Направления из вставленного ответа; выбранное — с заметкой пользователя
  const directions = useMemo(() => parseDirections(dirText, PROMPT_DIRECTION_COUNT * 4), [dirText]);
  const pick = selected !== null && selected < directions.length ? selected : null;
  const chosen = useMemo(
    () => (pick === null ? null : normalizeDirection({ ...directions[pick], note: dirNote })),
    [directions, pick, dirNote],
  );

  const contract = useMemo(() => buildSkinContractDoc(locale), [locale]);

  // Основа экрана: экран скина библиотеки (с его перекраской) или шаблон;
  // готовится один раз на смену основы, а не на каждую букву описания
  const bases = useMemo(() => {
    const out = {} as Record<SkinScreen, ScreenBase>;
    for (const s of SKIN_SCREENS) {
      const skinHtml = baseEntry && baseFiles[s] ? applyEntryColors(baseFiles[s]!, baseEntry) : null;
      const kind: PromptBaseKind = skinHtml ? 'skin' : 'template';
      const html = skinHtml ?? TEMPLATES[s];
      const assets: string[] = [];
      out[s] = { html, kind, prepared: prepareBaseHtml(html, kind, assets), assets };
    }
    return out;
  }, [baseEntry, baseFiles]);
  const baseFor = (s: SkinScreen) => bases[s];

  const prompts = useMemo(() => {
    const out: Partial<Record<SkinScreen, { text: string; bytes: number; tooBig: number }>> = {};
    for (const s of SKIN_SCREENS.filter((x) => screens.includes(x))) {
      const b = bases[s];
      const text = chatbotPrompt(
        buildScreenMessages({
          screen: s,
          description,
          baseHtml: b.prepared,
          baseKind: b.kind,
          contractDoc: contract,
          locale,
          direction: chosen,
        }),
      );
      out[s] = { text, bytes: byteLen(text), tooBig: b.prepared.length > PROMPT_MAX_HTML ? b.prepared.length : 0 };
    }
    return out;
  }, [screens, description, bases, contract, locale, chosen]);

  const copy = async (key: string, text: string) => {
    const ok = await copyText(text);
    setCopyFailed(!ok);
    if (ok) {
      setCopied(key);
      setTimeout(() => setCopied((cur) => (cur === key ? null : cur)), 1500);
    }
  };

  const directionPrompt = () => chatbotPrompt(buildDirectionMessages(description, locale, PROMPT_DIRECTION_COUNT));

  // Ответ чат-бота: документ → экран → ассеты основы на место → проверка
  const checkAnswer = () => {
    const { html, truncated } = extractHtmlDocument(answer);
    const found = html && !truncated ? detectScreens(html) : [];
    const screen = found.find((s) => screens.includes(s)) ?? found[0];
    if (!html || !screen) {
      const key = !html ? 'skin.kitNoHtml' : truncated ? 'skin.kitTruncated' : 'skin.kitNoScreen';
      setChecked({ screen: null, errors: [t(key)], html: null });
      return;
    }
    const b = baseFor(screen);
    const restored = restoreAssets(html, b.assets);
    const errors = checkSkinMarkup(restored, screen, b.html, locale, t);
    setChecked({ screen, errors, html: restored });
    if (!errors.length) {
      setReady((cur) => ({ ...cur, [screen]: restored }));
      setAnswer('');
    }
  };

  // Промпт исправления — как итерация исправления генератора ядра
  const fixPrompt = (c: Checked) => {
    const screen = c.screen!;
    const b = baseFor(screen);
    const assets = [...b.assets];
    return chatbotPrompt(
      buildScreenMessages({
        screen,
        description,
        baseHtml: b.prepared,
        baseKind: b.kind,
        contractDoc: contract,
        locale,
        direction: chosen,
        previousHtml: stripAssets(c.html!, assets),
        errors: c.errors,
      }),
    );
  };

  const readyList = SKIN_SCREENS.filter((s) => ready[s]);

  return (
    <div className="skin-workspace skin-gen-body skin-kit-body">
      <div className="skin-workspace-title">{t('skin.kitTitle')}</div>
      <p className="ctx-note">{t('skin.kitHint')}</p>
      <ol className="skin-kit-steps">
        <li>{t('skin.kitStep1')}</li>
        <li>{t('skin.kitStep2')}</li>
        <li>{t('skin.kitStep3')}</li>
        <li>{t('skin.kitStep4')}</li>
      </ol>

      <div className="skin-gen-row">
        <span className="field-label">{t('skin.genScreens')}</span>
        {SKIN_SCREENS.map((s) => (
          <label key={s} className="checkbox-row skin-gen-screen">
            <input
              type="checkbox"
              checked={screens.includes(s)}
              onChange={(e) => setScreens((cur) => (e.target.checked ? [...cur, s] : cur.filter((x) => x !== s)))}
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
          maxLength={PROMPT_MAX_DESCRIPTION}
          placeholder={t('skin.genDescriptionPh')}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>

      <div className="skin-gen-grid">
        {/* div, а не label: клик по подписи не должен «нажимать» триггер Select */}
        <div className="field">
          <span className="field-label">{t('skin.genBase')}</span>
          <Select value={base} options={baseOptions} onChange={setBase} />
        </div>
      </div>
      {baseEntry && baseLoading && <p className="ctx-note">{t('skin.kitBaseLoading')}</p>}
      {baseEntry && !baseLoading && order.some((s) => !baseEntry.screens[s]) && (
        <p className="ctx-note">
          {t('skin.genBaseMissing', {
            list: order
              .filter((s) => !baseEntry.screens[s])
              .map((s) => t(SCREEN_NAME_KEY[s]))
              .join(', '),
          })}
        </p>
      )}

      {/* Арт-направление: промпт идей → ответ чат-бота → карточки */}
      <div className="skin-kit-section">
        <div className="skin-workspace-title skin-dir-title">{t('skin.kitDirTitle')}</div>
        <div className="skin-actions">
          <button type="button" className="btn btn--ghost" onClick={() => void copy('dir', directionPrompt())}>
            {copied === 'dir' ? t('skin.copied') : t('skin.kitDirCopy')}
          </button>
        </div>
        <label className="field">
          <span className="field-label">{t('skin.kitDirPaste')}</span>
          <textarea
            className="input pcreate-textarea skin-kit-paste"
            rows={3}
            value={dirText}
            placeholder={t('skin.kitDirPastePh')}
            onChange={(e) => {
              setDirText(e.target.value);
              setSelected(0);
              setDirNote('');
            }}
          />
        </label>
        {dirText.trim() && !directions.length && <p className="ctx-note skin-gen-warn">{t('skin.kitDirNone')}</p>}
        {directions.length > 0 && (
          <>
            <SkinDirectionPicker
              directions={directions}
              selected={pick}
              onSelect={setSelected}
              note={dirNote}
              onNote={setDirNote}
              disabled={false}
            />
            {pick !== null && (
              <div className="skin-actions">
                <button type="button" className="btn btn--ghost" onClick={() => setSelected(null)}>
                  {t('skin.kitDirOff')}
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {/* Промпты экранов */}
      <div className="skin-kit-section">
        <div className="skin-workspace-title skin-dir-title">{t('skin.kitPromptsTitle')}</div>
        {!order.length && <p className="ctx-note">{t('skin.kitNoScreens')}</p>}
        {order.length > 1 && !chosen && <p className="ctx-note">{t('skin.kitNoDirection')}</p>}
        <div className="skin-kit-prompts">
          {order.map((s) => {
            const p = prompts[s];
            if (!p) return null;
            return (
              <div key={s} className="skin-kit-prompt">
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={!!baseEntry && baseLoading}
                  onClick={() => void copy(s, p.text)}
                >
                  {copied === s ? t('skin.copied') : t('skin.kitCopyScreen', { screen: t(SCREEN_NAME_KEY[s]) })}
                </button>
                <button
                  type="button"
                  className="btn btn--ghost"
                  title={t('skin.kitDownloadTitle')}
                  disabled={!!baseEntry && baseLoading}
                  onClick={() => downloadFile(`skin-prompt.${s}.txt`, p.text, 'text/plain')}
                >
                  ⬇ .txt
                </button>
                <span className="skin-kit-size">{formatSize(p.bytes)}</span>
                {p.tooBig > 0 && (
                  <span className="ctx-note skin-gen-warn">{t('skin.kitTooBig', { kb: Math.round(p.tooBig / 1024) })}</span>
                )}
              </div>
            );
          })}
        </div>
        {copyFailed && <p className="ctx-note skin-gen-warn">{t('skin.kitCopyFailed')}</p>}
      </div>

      {/* Ответ чат-бота: проверка и передача в загрузку */}
      <div className="skin-kit-section">
        <div className="skin-workspace-title skin-dir-title">{t('skin.kitAnswerTitle')}</div>
        <textarea
          className="input pcreate-textarea skin-kit-paste"
          rows={4}
          value={answer}
          placeholder={t('skin.kitAnswerPh')}
          onChange={(e) => {
            setAnswer(e.target.value);
            setChecked(null);
          }}
        />
        <div className="skin-actions">
          <button type="button" className="btn btn--ghost" disabled={!answer.trim()} onClick={checkAnswer}>
            {t('skin.kitCheck')}
          </button>
        </div>
        {checked && checked.errors.length === 0 && checked.screen && (
          <p className="ctx-note skin-gen-summary">{t('skin.kitScreenOk', { screen: t(SCREEN_NAME_KEY[checked.screen]) })}</p>
        )}
        {checked && checked.errors.length > 0 && (
          <div className="skin-report">
            <div className="skin-report-title">
              {checked.screen ? t('skin.kitFailed', { screen: t(SCREEN_NAME_KEY[checked.screen]) }) : t('skin.errorsTitle')}
            </div>
            <ul className="skin-error-list">
              {checked.errors.map((err, i) => (
                <li key={i}>{err}</li>
              ))}
            </ul>
            {checked.screen && checked.html && (
              <div className="skin-actions">
                <button type="button" className="btn btn--ghost" onClick={() => void copy('fix', fixPrompt(checked))}>
                  {copied === 'fix' ? t('skin.copied') : t('skin.kitCopyFix')}
                </button>
              </div>
            )}
          </div>
        )}
        {readyList.length > 0 && (
          <div className="skin-kit-ready">
            <span className="field-label">{t('skin.kitReady')}</span>
            {readyList.map((s) => (
              <span key={s} className="skin-chip skin-chip--on">
                {t(SCREEN_NAME_KEY[s])}
              </span>
            ))}
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => {
                onGenerated(ready);
                setReady({});
                setChecked(null);
              }}
            >
              {t('skin.kitOpenUpload', { n: readyList.length })}
            </button>
          </div>
        )}
      </div>
      <p className="ctx-note">{t('skin.screenshotHint')}</p>
    </div>
  );
}
