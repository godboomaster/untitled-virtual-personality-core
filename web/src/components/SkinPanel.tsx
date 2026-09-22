/* SkinPanel — блок управления скинами персон (раздел «Персоны»).
   Скин персоны — до трёх отдельных файлов: чат, досье, комната.
   Флоу: скачать шаблон(ы) → отдать нейросети (по одному экрану за раз) →
   загрузить файл(ы) → валидация → предпросмотр в песочнице → применить.
   При runtime-ошибке скина здесь же показывается отчёт по каждому экрану,
   который можно скопировать и вернуть редактору. */

import { useMemo, useRef, useState } from 'react';
import skinTemplateChat from '../skins/skin-template.chat.html?raw';
import skinTemplateDossier from '../skins/skin-template.dossier.html?raw';
import skinTemplateRoom from '../skins/skin-template.room.html?raw';
import { validateSkin } from '../skins/engine';
import type { SkinScreen } from '../skins/engine';
import { usePersonaSkin } from '../skins/skinStore';
import { buildChatPayload, buildRoomPayload } from '../skins/payloads';
import { useI18n, useMockData } from '../i18n';
import SkinFrame from './SkinFrame';

const TEMPLATES: Record<SkinScreen, string> = {
  chat: skinTemplateChat,
  dossier: skinTemplateDossier,
  room: skinTemplateRoom,
};
const SCREEN_ORDER: SkinScreen[] = ['chat', 'dossier', 'room'];
const SCREEN_NAME_KEY: Record<SkinScreen, string> = {
  chat: 'skin.previewChat',
  dossier: 'skin.previewDossier',
  room: 'skin.previewRoom',
};

function downloadFile(name: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/html' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

export default function SkinPanel() {
  const { t } = useI18n();
  const data = useMockData();
  const { personas } = data;

  const [personaId, setPersonaId] = useState(personas[0]?.id ?? '');
  const persona = personas.find((p) => p.id === personaId) ?? personas[0];
  const { skins, broken, apply, reset } = usePersonaSkin(persona?.id ?? '');

  // Загруженные файлы, ожидающие подтверждения: экран → содержимое
  const [candidate, setCandidate] = useState<Partial<Record<SkinScreen, string>> | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [previewScreen, setPreviewScreen] = useState<SkinScreen>('chat');
  const [copied, setCopied] = useState(false);
  // Экран, на строку которого сейчас тянут файл (подсветка дроп-зоны)
  const [dropScreen, setDropScreen] = useState<SkinScreen | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // Снапшоты для предпросмотра — те же билдеры, что у боевых экранов
  const previewState = useMemo(() => {
    if (!persona) return null;
    const statusText = t(`status.${persona.status}`);
    const init = data.initiativeStateByPersona[persona.id];
    const cfg = data.roomConfigs[persona.id];
    const pastime = cfg?.pastimes[0];
    if (previewScreen === 'room') {
      return buildRoomPayload({
        persona,
        statusText,
        pastimeLabel: pastime?.label ?? '',
        pastimePlace: pastime?.place ?? '',
        duration: pastime?.duration ?? '',
        x: cfg?.points[pastime?.key ?? 'desk'] ?? 50,
        mood: cfg?.mood ?? '',
        energy: cfg?.energy ?? '',
        pet: cfg?.pet ?? 'none',
        petLabel: cfg?.petLabel,
        feed: data.activitiesByPersona[persona.id] ?? [],
        inventory: data.inventoryByPersona[persona.id] ?? [],
      });
    }
    // chat и dossier делят один снапшот
    const nextReminder = (data.remindersByPersona[persona.id] ?? []).find((r) => r.active);
    return buildChatPayload({
      persona,
      statusText,
      youLabel: t('chat.you'),
      typing: false, // превью скина на мок-данных — генерации нет
      messages: data.chatByPersona[persona.id] ?? [],
      mood: init?.emotionalState ?? '',
      pastimeLabel: pastime?.label ?? '',
      allPersonas: personas.map((p) => ({ persona: p, statusText: t(`status.${p.status}`) })),
      context: {
        pastimePlace: pastime?.place ?? '',
        trend: '',
        initiative: init
          ? t('chat.probLine', {
              p: Math.round(init.probability * 100),
              today: init.initiativesToday,
              max: init.maxPerDay,
            })
          : '',
        lastReply: persona.lastReply,
        nextReminder: nextReminder
          ? `${nextReminder.time} — ${nextReminder.text}`
          : t('chat.noActiveReminders'),
        learning: '',
        features: persona.features.map((f) => t(`fb.${f}`)),
      },
      reply: null,
      todos: data.todosByPersona[persona.id] ?? [],
      inventory: data.inventoryByPersona[persona.id] ?? [],
      dossier: {
        facts: data.ltmByPersona[persona.id] ?? [],
        reminders: data.remindersByPersona[persona.id] ?? [],
        initiatives: data.initiativeByPersona[persona.id] ?? [],
        diary: data.diaryByPersona[persona.id] ?? [],
        stm: data.stmByPersona[persona.id] ?? [],
        courses: data.learningByPersona[persona.id] ?? [],
      },
      courseStatusLabels: {
        active: t('learn.statusActive'),
        paused: t('learn.statusPaused'),
        finished: t('learn.statusFinished'),
      },
      quizLineFor: (n) => t('learn.quizAlert', { n }),
      initState: init ?? {
        silenceThresholdMin: 120,
        probability: 0.35,
        maxPerDay: 5,
        checkIntervalMin: 30,
        adaptiveThreshold: true,
        bayesianFeedback: true,
        ignoreStreak: 0,
        emotionalState: '',
        initiativesToday: 0,
      },
      initStages: t('init.stages').split('|'),
      initSilenceText: init
        ? t('init.silenceProgress', { n: 99, max: init.silenceThresholdMin })
        : '',
      files: data.filesByPersona[persona.id] ?? [],
      providers: data.llmProviders,
      modelOverrides: {},
      providerMain: null,
      backupToggled: [],
      featureFlags: data.featureFlags,
      genOverrides: {},
      stmSizeDefault: data.generationDefaults.stmSize,
      keySetLabel: t('apikeys.keySet'),
      keyNotSetLabel: t('apikeys.keyNotSet'),
    });
  }, [persona, previewScreen, data, t, personas]);

  if (!persona) return null;

  const copyText = (text: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  const onFiles = (files: File[]) => {
    Promise.all(files.map((f) => f.text().then((text) => ({ name: f.name, text })))).then((loaded) => {
      const cand: Partial<Record<SkinScreen, string>> = {};
      const errs: string[] = [];
      for (const { name, text } of loaded) {
        const result = validateSkin(text);
        if (result.ok) {
          for (const s of result.screens) cand[s] = text;
        } else {
          errs.push(...result.errors.map((e) => `${name}: ${e}`));
        }
      }
      if (Object.keys(cand).length > 0) {
        setCandidate(cand);
        setPreviewScreen(SCREEN_ORDER.find((s) => cand[s]) ?? 'chat');
      } else {
        setCandidate(null);
      }
      setErrors(errs);
    });
  };

  // Статус экрана: свой файл / дефолт / сломан
  const screenStatus = (s: SkinScreen) =>
    broken[s] ? 'skin.statusBroken' : skins[s] ? 'skin.statusCustom' : 'skin.statusDefault';

  const hasSkin = SCREEN_ORDER.some((s) => skins[s]);
  const hasBroken = SCREEN_ORDER.some((s) => broken[s]);

  return (
    <div className="card skin-panel">
      <div className="card-title-row">
        <h3 className="card-title">{t('skin.title')}</h3>
        <span className={`badge ${hasSkin && !hasBroken ? 'badge--active' : ''} ${hasBroken ? 'skin-badge-broken' : ''}`}>
          {t(hasBroken ? 'skin.statusBroken' : hasSkin ? 'skin.statusCustom' : 'skin.statusDefault')}
        </span>
      </div>
      <p className="ctx-note">{t('skin.subtitle')}</p>

      {/* Выбор персоны, чей скин настраиваем */}
      <div className="tabs room-persona-tabs">
        {personas.map((p) => (
          <button
            key={p.id}
            type="button"
            className={`tab ${p.id === persona.id ? 'tab--active' : ''}`}
            onClick={() => {
              setPersonaId(p.id);
              setCandidate(null);
              setErrors([]);
            }}
          >
            {p.name}
          </button>
        ))}
      </div>

      {/* Файлы экранов: скачать/загрузить файл каждого экрана; строка —
          также дроп-зона (слот всё равно определяется по содержимому) */}
      <div className="skin-screens">
        {SCREEN_ORDER.map((s) => (
          <div
            key={s}
            className={`skin-screen-row ${dropScreen === s ? 'skin-screen-row--drop' : ''}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDropScreen(s);
            }}
            onDragLeave={(e) => {
              if (!e.currentTarget.contains(e.relatedTarget as Node)) {
                setDropScreen((cur) => (cur === s ? null : cur));
              }
            }}
            onDrop={(e) => {
              e.preventDefault();
              setDropScreen(null);
              const fs = Array.from(e.dataTransfer.files ?? []).filter(
                (f) => f.type === 'text/html' || /\.html?$/i.test(f.name),
              );
              if (fs.length > 0) onFiles(fs);
            }}
          >
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => downloadFile(`skin-${persona.id}.${s}.html`, skins[s] ?? TEMPLATES[s])}
            >
              ⬇ {t(SCREEN_NAME_KEY[s])}
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              title={t('skin.uploadScreen')}
              onClick={() => fileRef.current?.click()}
            >
              ⬆ {t(SCREEN_NAME_KEY[s])}
            </button>
            <span className={`badge ${skins[s] && !broken[s] ? 'badge--active' : ''} ${broken[s] ? 'skin-badge-broken' : ''}`}>
              {t(screenStatus(s))}
            </span>
          </div>
        ))}
      </div>
      <p className="ctx-note">{t('skin.dropHint')}</p>

      <div className="skin-actions">
        <button type="button" className="btn btn--ghost" onClick={() => copyText(t('skin.llmPrompt'))}>
          {copied ? t('skin.copied') : t('skin.copyPrompt')}
        </button>
        {hasSkin && (
          <button
            type="button"
            className="btn btn--danger"
            onClick={() => {
              reset();
              setCandidate(null);
              setErrors([]);
            }}
          >
            {t('skin.reset')}
          </button>
        )}
        <input
          ref={fileRef}
          type="file"
          accept=".html,text/html"
          multiple
          hidden
          onChange={(e) => {
            const fs = Array.from(e.target.files ?? []);
            if (fs.length > 0) onFiles(fs);
            e.target.value = '';
          }}
        />
      </div>

      <p className="ctx-note">{t('skin.screenshotHint')}</p>

      {/* Отчёты о runtime-ошибках по экранам: копируются нейросети-редактору */}
      {SCREEN_ORDER.filter((s) => broken[s]).map((s) => (
        <div className="skin-report" key={s}>
          <div className="skin-report-title">
            {t('skin.brokenTitle')} · {t(SCREEN_NAME_KEY[s])}
          </div>
          <code>{broken[s]}</code>
          <button type="button" className="btn btn--ghost" onClick={() => copyText(t('skin.reportPrefix') + broken[s])}>
            {copied ? t('skin.copied') : t('skin.copyReport')}
          </button>
        </div>
      ))}

      {/* Ошибки валидации загруженного файла */}
      {errors.length > 0 && (
        <div className="skin-report">
          <div className="skin-report-title">{t('skin.errorsTitle')}</div>
          <ul className="skin-error-list">
            {errors.map((err, i) => (
              <li key={i}>{err}</li>
            ))}
          </ul>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => copyText(t('skin.reportPrefix') + errors.join('\n'))}
          >
            {copied ? t('skin.copied') : t('skin.copyReport')}
          </button>
        </div>
      )}

      {/* Предпросмотр кандидатов на моковых данных перед применением */}
      {candidate && previewState && (
        <div className="skin-preview">
          <div className="tabs">
            {SCREEN_ORDER.filter((s) => candidate[s]).map((s) => (
              <button
                key={s}
                type="button"
                className={`tab ${previewScreen === s ? 'tab--active' : ''}`}
                onClick={() => setPreviewScreen(s)}
              >
                {t(SCREEN_NAME_KEY[s])}
              </button>
            ))}
          </div>
          {candidate[previewScreen] && (
            <SkinFrame
              skin={candidate[previewScreen]}
              screen={previewScreen}
              state={previewState}
              className="skin-frame skin-frame--preview"
              title="skin preview"
            />
          )}
          <div className="skin-actions">
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => {
                apply(SCREEN_ORDER.map((s) => candidate[s]).filter((f): f is string => f != null));
                setCandidate(null);
              }}
            >
              {t('skin.apply')}
            </button>
            <button type="button" className="btn btn--ghost" onClick={() => setCandidate(null)}>
              {t('skin.cancel')}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
