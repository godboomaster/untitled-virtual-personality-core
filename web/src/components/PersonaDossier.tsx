import { useEffect, useRef, useState } from 'react';
import type { Persona } from '../mockData';
import { useI18n, useMockData } from '../i18n';
import { api, CLEAR_PARTS } from '../api';
import type { ClearPart } from '../api';
import { usePersonaAvatars } from '../avatarStore';
import { refetchProviders, useApiOnline, useApiProviders, useApiPersonaLlm, refetchPersonaLlm } from '../apiData';
import Memory from '../sections/Memory';
import Tasks from '../sections/Tasks';
import Initiative from '../sections/Initiative';
import Settings from '../sections/Settings';
import ComputerControl from '../sections/ComputerControl';
import LearningPanel from './LearningPanel';
import FilesPanel from './FilesPanel';
import InfoButton from '../components/InfoButton';
import Collapsible from './Collapsible';
import { BACK_PANEL, useBackHandler } from '../backStack';

/* Встроенное «Досье персоны»: память, напоминания и задачи, инициатива и
   настройки выбранной в чате персоны. Рендерится в потоке вместо окна
   переписки (не поверх чата). Содержимое вкладок — те же секции
   в embedded-режиме (без заголовков и табов персон, разметка не дублируется). */

// Выбранные персоной модели по провайдерам (module-state, переживает перемонтирование)
const modelChoices: Record<string, Record<string, string>> = {};

type DossierTab = 'memory' | 'tasks' | 'initiative' | 'computer' | 'learning' | 'files' | 'settings';

const baseTabs: DossierTab[] = ['memory', 'tasks', 'initiative', 'computer'];

interface PersonaDossierProps {
  persona: Persona; // персона, выбранная в чате
  onClose: () => void;
  // Очистка диалога (из чата): без parts — всё сразу, parts — только эти части
  onClearDialog?: (parts?: ClearPart[]) => void | Promise<void>;
  onStmChange?: () => void; // STM изменился (удаление реплик) — чату перечитать историю
  stmEpoch?: number; // счётчик завершённых обменов — перечитать STM (бот мог отвечать, пока досье открыто)
  initialTab?: string; // открыть на вкладке (переход со «Старта»); нет — «Память»
}

export default function PersonaDossier({ persona, onClose, onClearDialog, onStmChange, stmEpoch, initialTab }: PersonaDossierProps) {
  const { t, lang } = useI18n();
  const { llmProviders, providerModels } = useMockData();
  const avatars = usePersonaAvatars();
  const apiOnline = useApiOnline();
  const apiProviders = useApiProviders();
  const [tab, setTab] = useState<DossierTab>((initialTab as DossierTab | undefined) ?? 'memory');
  // Двухшаговое подтверждение очистки диалога
  const [confirmClear, setConfirmClear] = useState(false);
  // Очистка по частям: какая часть ждёт подтверждения и какая только что стёрта
  const [confirmPart, setConfirmPart] = useState<ClearPart | null>(null);
  const [donePart, setDonePart] = useState<ClearPart | null>(null);
  // Корзина очистки: снапшот последнего сброса (для восстановления)
  const [backup, setBackup] = useState<{
    ts?: number;
    counts?: { stm: number; ltm: number; diary: boolean; initiatives?: number };
    parts?: ClearPart[] | null;
  } | null>(null);
  const [restoredFlash, setRestoredFlash] = useState(false);

  const refreshBackup = () => {
    if (!apiOnline) return;
    api
      .getClearBackup(persona.id)
      .then((b) => setBackup(b.exists ? b : null))
      .catch(() => {});
  };
  useEffect(refreshBackup, [apiOnline, persona.id]);

  const restoreBackup = () => {
    api
      .restoreClearBackup(persona.id)
      .then(() => {
        setBackup(null);
        setRestoredFlash(true);
        setTimeout(() => setRestoredFlash(false), 2500);
        onStmChange?.(); // чат перечитывает историю
      })
      .catch(() => {});
  };
  // Выбранные модели по провайдерам для текущей персоны
  const [models, setModels] = useState<Record<string, string>>(() => ({ ...(modelChoices[persona.id] ?? {}) }));

  const modelFor = (providerId: string, fallback?: string) =>
    models[providerId] ?? modelChoices[persona.id]?.[providerId] ?? fallback ?? '';
  const setModelFor = (providerId: string, value: string) => {
    const next = { ...models, [providerId]: value };
    setModels(next);
    modelChoices[persona.id] = next;
  };

  // Персональные модели по провайдерам (llm.models в YAML персоны)
  const [llmModels, setLlmModels] = useState<Record<string, string>>({});
  useEffect(() => {
    setLlmModels({});
    if (!apiOnline) return;
    let stale = false;
    api
      .getPersonaConfig(persona.id)
      .then((c) => !stale && setLlmModels(c.llm?.models ?? {}))
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [apiOnline, persona.id]);

  // Основной провайдер персоны (как в карточке «LLM-провайдеры»):
  // персональный primary, иначе глобальный активный — бейдж «основной»
  // в «Моделях провайдеров» должен совпадать с карточкой провайдеров
  const personaLlm = useApiPersonaLlm(persona.id);
  const globalActiveId = apiProviders?.find((p) => p.active)?.id ?? null;
  const effPrimary = personaLlm?.primary ?? globalActiveId;

  // Сохранить/снять персональный override модели (пустая строка — снять)
  const saveOwnModel = (providerId: string, value: string, prev: string, input: HTMLInputElement) => {
    if (value === prev) return;
    api
      .updatePersonaConfig(persona.id, { llm: { models: { [providerId]: value } } })
      .then(() => {
        setLlmModels((cur) => {
          const next = { ...cur };
          if (value) next[providerId] = value;
          else delete next[providerId];
          return next;
        });
        refetchPersonaLlm(persona.id); // шапка чата покажет новую модель сразу
      })
      .catch(() => {
        input.value = prev; // откат при ошибке
      });
  };

  // Вкладка «Обучение» — только у персон с включённой фичей, «Файлы» — у всех
  const tabs: { id: DossierTab; label: string }[] = [
    ...baseTabs.map((id) => ({ id, label: t(`dossier.${id}`) })),
    ...(persona.features.includes('learning')
      ? [{ id: 'learning' as DossierTab, label: t('dossier.learning') }]
      : []),
    { id: 'files' as DossierTab, label: t('dossier.files') },
    { id: 'settings' as DossierTab, label: t('dossier.settings') },
  ];

  // При смене персоны сбрасываем вкладку (активная могла скрыться) и подтверждение.
  // Первый запуск эффекта (открытие досье) вкладку не трогает — там может быть
  // запрошенная «Стартом»
  const shownPersona = useRef(persona.id);
  useEffect(() => {
    if (shownPersona.current !== persona.id) setTab('memory');
    shownPersona.current = persona.id;
    setConfirmClear(false);
    setConfirmPart(null);
    setModels({ ...(modelChoices[persona.id] ?? {}) });
  }, [persona.id]);

  useBackHandler(true, BACK_PANEL, onClose);

  // Закрытие по Esc
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className="dossier-panel bracketed">
      <div className="corner tl" />
      <div className="corner tr" />
      <div className="corner bl" />
      <div className="corner br" />

        {/* Шапка досье */}
        <div className="dossier-head">
          <div className="avatar avatar--large">
            {avatars[persona.id] ? <img src={avatars[persona.id]} alt={persona.name} /> : persona.name.charAt(0)}
          </div>
          <div>
            <div className="dossier-name">{persona.name}</div>
            <div className="dossier-meta">{persona.model} · {t(`status.${persona.status}`)}</div>
          </div>
          <span className="badge">DOSSIER // {persona.id.toUpperCase()}</span>
          <button type="button" className="pxe-close" onClick={onClose} aria-label={t('common.close')}>✕</button>
        </div>

        {/* Вкладки досье */}
        <div className="tabs dossier-tabs">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`tab ${tab === t.id ? 'tab--active' : ''}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Содержимое вкладки — существующие секции в embedded-режиме */}
        <div className="dossier-body">
          {tab === 'memory' && <Memory personaId={persona.id} embedded onStmChange={onStmChange} stmEpoch={stmEpoch} />}
          {tab === 'tasks' && <Tasks personaId={persona.id} embedded />}
          {tab === 'initiative' && <Initiative personaId={persona.id} embedded />}
          {tab === 'computer' && <ComputerControl personaId={persona.id} />}
          {tab === 'learning' && <LearningPanel personaId={persona.id} />}
          {tab === 'files' && <FilesPanel personaId={persona.id} />}
          {tab === 'settings' && (
            <>
              {/* Настройки ядра: провайдеры, генерация, флаги */}
              <Settings embedded personaId={persona.id} />

              {/* Модели провайдеров: реальные с бэкенда или мок-редактор.
                  Сворачиваемый блок; в свёрнутом заголовке — свои модели персоны */}
              <div className="card">
                <Collapsible
                  title={t('dossier.modelsTitle')}
                  headExtra={<InfoButton helpKey="settings.activeProvider" />}
                  storageKey="vpc-dossier-open-models"
                  summary={
                    apiOnline && apiProviders
                      ? (() => {
                          const own = apiProviders
                            .filter((p) => !p.local && llmModels[p.id])
                            .map((p) => `${p.name}: ${llmModels[p.id]}`);
                          return own.length ? own.join(' · ') : t('dossier.modelsAllGlobal');
                        })()
                      : undefined
                  }
                >
                  {apiOnline && apiProviders ? (
                    <>
                      <ul className="memory-list">
                        {apiProviders.map((p) => {
                          // Своя модель персоны (пусто — глобальная из placeholder).
                          // Ollama — синглтон, ей персональный override недоступен:
                          // её поле редактирует глобальную модель.
                          const own = llmModels[p.id] ?? '';
                          return (
                            <li key={p.id} className="pmodel-row">
                              <span className="provider-name">{p.name}</span>
                              {p.local && <span className="badge">{t('settings.localBadge')}</span>}
                              <input
                                key={`${p.id}:${p.local ? p.model : own}`}
                                className="input pmodel-input"
                                list={`pmodels-api-${p.id}`}
                                placeholder={p.model || t('dossier.modelPh')}
                                defaultValue={p.local ? p.model : own}
                                spellCheck={false}
                                onBlur={(e) => {
                                  const v = e.target.value.trim();
                                  if (p.local) {
                                    if (v && v !== p.model) {
                                      api.setProviderModel(p.id, v).then(refetchProviders).catch(() => {});
                                    } else {
                                      e.target.value = p.model;
                                    }
                                  } else {
                                    saveOwnModel(p.id, v, own, e.target);
                                  }
                                }}
                                onKeyDown={(e) => {
                                  if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                                }}
                              />
                              <datalist id={`pmodels-api-${p.id}`}>
                                {(providerModels[p.id] ?? []).map((m) => (
                                  <option key={m} value={m} />
                                ))}
                              </datalist>
                              {own && !p.local && <span className="badge badge--success">{t('dossier.modelOwnBadge')}</span>}
                              {p.id === effPrimary && <span className="badge badge--active">{t('settings.mainBadge')}</span>}
                            </li>
                          );
                        })}
                      </ul>
                      <div className="field-hint">{t('dossier.modelsHint')}</div>
                    </>
                  ) : (
                    <ul className="memory-list">
                      {llmProviders.map((p) => (
                        <li key={p.id} className="pmodel-row">
                          <span className="provider-name">{p.name}</span>
                          {p.local && <span className="badge">{t('settings.localBadge')}</span>}
                          <input
                            className="input pmodel-input"
                            list={`pmodels-${p.id}`}
                            placeholder={t('dossier.modelPh')}
                            value={modelFor(p.id, p.model)}
                            onChange={(e) => setModelFor(p.id, e.target.value)}
                            spellCheck={false}
                          />
                          <datalist id={`pmodels-${p.id}`}>
                            {(providerModels[p.id] ?? []).map((m) => (
                              <option key={m} value={m} />
                            ))}
                          </datalist>
                        </li>
                      ))}
                    </ul>
                  )}
                </Collapsible>
              </div>

              {/* Опасная зона: очистка диалога с подтверждением */}
              {onClearDialog && (
                <div className="card">
                  <h2 className="card-title">{t('dossier.dangerZone')}</h2>
                  {!confirmClear ? (
                    <button type="button" className="btn btn--danger" onClick={() => setConfirmClear(true)}>
                      {t('dossier.clearDialog')}
                    </button>
                  ) : (
                    <div className="dossier-confirm">
                      <span className="dossier-confirm-text">
                        {t('dossier.confirmClear', { name: persona.name })}
                      </span>
                      <div className="dossier-confirm-actions">
                        <button
                          type="button"
                          className="btn btn--danger"
                          onClick={() => {
                            setConfirmClear(false);
                            setConfirmPart(null);
                            // Снапшот пишется на бэкенде в момент очистки —
                            // после ответа обновляем состояние корзины
                            void Promise.resolve(onClearDialog()).then(refreshBackup);
                          }}
                        >
                          {t('dossier.yesClear')}
                        </button>
                        <button type="button" className="btn btn--ghost" onClick={() => setConfirmClear(false)}>
                          {t('common.cancel')}
                        </button>
                      </div>
                    </div>
                  )}
                  {/* То же по частям: каждая кнопка стирает одну часть того,
                      что стирает «Очистить диалог»; снапшот — в ту же корзину */}
                  <div className="danger-parts-title">{t('dossier.clearPartsTitle')}</div>
                  <div className="danger-parts">
                    {CLEAR_PARTS.map((part) => (
                      <button
                        key={part}
                        type="button"
                        className={`danger-part${confirmPart === part ? ' danger-part--armed' : ''}`}
                        title={t(`dossier.partHint.${part}`)}
                        onClick={() => {
                          setConfirmClear(false);
                          setConfirmPart(confirmPart === part ? null : part);
                        }}
                      >
                        <span aria-hidden="true">{donePart === part ? '✓' : '✕'}</span>
                        {t(`dossier.part.${part}`)}
                      </button>
                    ))}
                  </div>
                  {confirmPart && (
                    <div className="dossier-confirm" style={{ marginTop: 10 }}>
                      <span className="dossier-confirm-text">
                        {t('dossier.confirmPart', {
                          part: t(`dossier.part.${confirmPart}`),
                          hint: t(`dossier.partHint.${confirmPart}`),
                          name: persona.name,
                        })}
                      </span>
                      <div className="dossier-confirm-actions">
                        <button
                          type="button"
                          className="btn btn--danger"
                          onClick={() => {
                            const part = confirmPart;
                            setConfirmPart(null);
                            void Promise.resolve(onClearDialog([part])).then(() => {
                              refreshBackup();
                              setDonePart(part);
                              setTimeout(() => setDonePart((d) => (d === part ? null : d)), 2500);
                            });
                          }}
                        >
                          {t('dossier.yesDelete')}
                        </button>
                        <button type="button" className="btn btn--ghost" onClick={() => setConfirmPart(null)}>
                          {t('common.cancel')}
                        </button>
                      </div>
                    </div>
                  )}
                  {/* Корзина: восстановление последнего снапшота очистки */}
                  {backup && (
                    <div className="dossier-confirm" style={{ marginTop: 10 }}>
                      <span className="dossier-confirm-text">
                        {backup.parts && backup.parts.length > 0
                          ? t('dossier.backupInfoParts', {
                              ts: backup.ts ? new Date(backup.ts * 1000).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US') : '—',
                              parts: backup.parts.map((p) => t(`dossier.part.${p}`)).join(', '),
                            })
                          : t('dossier.backupInfo', {
                              ts: backup.ts ? new Date(backup.ts * 1000).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US') : '—',
                              stm: backup.counts?.stm ?? 0,
                              ltm: backup.counts?.ltm ?? 0,
                              init: backup.counts?.initiatives ?? 0,
                            })}
                      </span>
                      <div className="dossier-confirm-actions">
                        <button type="button" className="btn btn--ghost" onClick={restoreBackup}>
                          {restoredFlash ? '✓' : t('dossier.restoreBackup')}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>
    </div>
  );
}
