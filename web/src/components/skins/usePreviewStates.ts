/* Снапшоты предпросмотра скина на моковых данных персоны — те же билдеры,
   что у боевых экранов, плюс окружение приложения (тема, язык, подписи,
   время суток, погода — useSkinEnv). Нужны предпросмотру, runtime-проверке
   загруженных файлов и генератору скинов. Чат и досье делят один снапшот.
   Снапшоты «спокойные» (без печати и плашки ответа) — смоук-тест сам
   досылает второй с ними; предпросмотр показывает чат «в работе»
   (withBusyChat), чтобы были видны все части экрана. */

import { useMemo } from 'react';
import type { SkinScreen } from '../../skins/engine';
import { buildChatPayload, buildRoomPayload } from '../../skins/payloads';
import type { SkinStatePayload } from '../../skins/payloads';
import { useSkinEnv } from '../../skins/useSkinEnv';
import { useApiOnline } from '../../apiData';
import type { Persona } from '../../mockData';
import { useI18n, useMockData } from '../../i18n';
import { inferPose } from '../../room/roomModel';

// «~15 мин» → 15 (минут); без числа — 0
function durationMinutes(duration: string | undefined): number {
  const m = /\d+/.exec(duration ?? '');
  return m ? Number(m[0]) : 0;
}

// Чат «в работе» для предпросмотра: персона печатает, над полем ввода —
// плашка ответа на её последнюю реплику. Без них превью не показывает
// индикатор печати и плашку ответа — половину строки ввода
export function withBusyChat(state: SkinStatePayload): SkinStatePayload {
  const last = [...(state.messages ?? [])].reverse().find((m) => m.role === 'persona');
  const text = last ? (last.text.length > 120 ? `${last.text.slice(0, 120)}…` : last.text) : '…';
  return {
    ...state,
    typing: true,
    reply: state.reply ?? { author: state.persona?.name ?? '', text },
  };
}

export function usePreviewStates(persona: Persona): Record<SkinScreen, SkinStatePayload> {
  const { t, lang } = useI18n();
  const data = useMockData();
  const apiOnline = useApiOnline();
  const env = useSkinEnv({ locale: lang, t, personaName: persona.name, apiOnline });
  const { personas } = data;
  return useMemo(() => {
    const statusText = t(`status.${persona.status}`);
    const init = data.initiativeStateByPersona[persona.id];
    const cfg = data.roomConfigs[persona.id];
    const pastime = cfg?.pastimes[0];
    const spot = pastime?.key ?? 'desk';
    const room = buildRoomPayload({
      persona,
      statusText,
      pastimeLabel: pastime?.label ?? '',
      pastimePlace: pastime?.place ?? '',
      duration: pastime?.duration ?? '',
      x: cfg?.points[spot] ?? 50,
      mood: cfg?.mood ?? '',
      energy: cfg?.energy ?? '',
      pet: cfg?.pet ?? 'none',
      petLabel: cfg?.petLabel,
      feed: data.activitiesByPersona[persona.id] ?? [],
      inventory: data.inventoryByPersona[persona.id] ?? [],
      env,
      // Как у боевой комнаты: место, поза для него и начало занятия
      spot,
      pose: inferPose(null, spot, []),
      pastimeSince: pastime ? Math.floor(Date.now() / 1000) - durationMinutes(pastime.duration) * 60 : null,
    });
    // У реальной персоны (бэкенд онлайн) моковой переписки нет — берём
    // первую моковую: превью без ленты не показывает половину скина
    const messages =
      data.chatByPersona[persona.id] ?? Object.values(data.chatByPersona).find((m) => m.length) ?? [];
    const nextReminder = (data.remindersByPersona[persona.id] ?? []).find((r) => r.active);
    const chat = buildChatPayload({
      persona,
      statusText,
      youLabel: t('chat.you'),
      typing: false, // превью скина на мок-данных — генерации нет
      messages,
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
      initSilenceText: init ? t('init.silenceProgress', { n: 99, max: init.silenceThresholdMin }) : '',
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
      env,
    });
    return { chat, dossier: chat, room };
  }, [persona, data, t, personas, env]);
}
