import { useEffect, useMemo, useRef, useState } from 'react';
import type { DiaryEntry, LtmFact, StmMessage } from '../mockData';
import { useI18n, useMockData } from '../i18n';
import { api } from '../api';
import { useApiOnline } from '../apiData';
import FormModal from '../components/FormModal';
import InfoButton from '../components/InfoButton';

type Tab = 'stm' | 'ltm' | 'diary';

// Размер полного буфера STM (мок) и число реплик в сокращённом виде
const STM_FULL = 20;
const STM_SHORT = 6;

// Полный буфер STM (мок): хвост диалога персоны + догенерация старших реплик
function buildFullStm(stmByPersona: Record<string, StmMessage[]>, personaId: string): StmMessage[] {
  const base = stmByPersona[personaId] ?? [];
  if (base.length === 0) return [];
  if (base.length >= STM_FULL) return base;
  const older: StmMessage[] = [];
  for (let i = 0; i < STM_FULL - base.length; i++) {
    const src = base[i % base.length];
    older.push({ id: 1000 + i, role: src.role, text: src.text, time: src.time });
  }
  return [...older, ...base];
}

// Строка LTM бэкенда «Категория: факт» → структура факта UI
function parseFact(raw: string, i: number): LtmFact {
  const sep = raw.indexOf(':');
  return sep > 0
    ? { id: i + 1, category: raw.slice(0, sep).trim(), fact: raw.slice(sep + 1).trim() }
    : { id: i + 1, category: 'General', fact: raw };
}

interface MemoryProps {
  personaId?: string; // фиксированная персона (модалка «Досье») — без табов персон
  embedded?: boolean; // встраивание без заголовка раздела
  onStmChange?: () => void; // STM изменился на бэкенде — чату перечитать историю
  stmEpoch?: number; // счётчик завершённых обменов — перечитать STM (бот мог ответить, пока досье открыто)
}

export default function Memory({ personaId: fixedId, embedded, onStmChange, stmEpoch }: MemoryProps) {
  const { lang, t } = useI18n();
  const { personas, stmByPersona, ltmByPersona, diaryByPersona } = useMockData();
  const apiOnline = useApiOnline();
  const [tab, setTab] = useState<Tab>('stm');
  // Память индивидуальна: у каждой персоны свои STM, LTM и дневник
  const [selectedId, setSelectedId] = useState(() => fixedId ?? personas[0].id);
  const persona = personas.find((p) => p.id === (fixedId ?? selectedId)) ?? personas[0];

  // Данные бэкенда: STM-история и LTM-факты (raw — исходная строка «Категория: факт»
  // для точечного забывания через API)
  const [apiStm, setApiStm] = useState<StmMessage[] | null>(null);
  const [apiLtm, setApiLtm] = useState<{ f: LtmFact; raw: string }[] | null>(null);
  const [apiDiary, setApiDiary] = useState<DiaryEntry[] | null>(null);
  // Досье чата: интересы/темы/наблюдения из автоанализа диалога (не LTM-факты)
  const [apiDossier, setApiDossier] = useState<{
    interests: string[];
    topics: string[];
    personality_notes: string[];
  } | null>(null);
  const [removedFactIds, setRemovedFactIds] = useState<number[]>([]);

  const timeFmt = (ts: number | null) =>
    ts
      ? new Date(ts * 1000).toLocaleTimeString(lang === 'ru' ? 'ru-RU' : 'en-US', { hour: '2-digit', minute: '2-digit' })
      : '';

  // Подтягиваем STM/LTM персоны с бэкенда
  useEffect(() => {
    setApiStm(null);
    setApiLtm(null);
    setApiDiary(null);
    setApiDossier(null);
    setRemovedFactIds([]);
    if (!apiOnline) return;
    const id = persona.id;
    let stale = false;
    api
      .getHistory(id)
      .then((msgs) => {
        if (stale) return;
        setApiStm(
          msgs.map((m, i) => ({
            id: i + 1,
            role: m.role === 'user' ? 'user' : 'bot',
            text: m.content,
            time: timeFmt(m.timestamp),
          })),
        );
      })
      .catch(() => {});
    api
      .getLtmFacts(id)
      .then((facts) => {
        if (stale) return;
        setApiLtm(facts.map((raw, i) => ({ f: parseFact(raw, i), raw })));
      })
      .catch(() => {});
    api
      .getDossier(id)
      .then((d) => {
        if (stale) return;
        setApiDossier(d);
      })
      .catch(() => {});
    api
      .getDiary(id)
      .then((d) => {
        if (stale) return;
        const dateFmt = (iso: string) => {
          const dt = new Date(iso);
          return Number.isNaN(dt.getTime())
            ? iso
            : dt.toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
        };
        // Эпизоды и заметки — одной лентой, свежие сверху; сводка жизни — первой
        const entries = [
          ...d.episodes.map((e) => ({ date: dateFmt(e.timestamp), text: e.text, ts: e.timestamp })),
          ...d.notes.map((n) => ({ date: dateFmt(n.timestamp), text: n.text, ts: n.timestamp })),
        ].sort((a, b) => (a.ts < b.ts ? 1 : -1));
        if (d.life_summary) entries.unshift({ date: 'Σ', text: d.life_summary, ts: '' });
        setApiDiary(entries.map((e, i) => ({ id: i + 1, date: e.date, text: e.text })));
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, persona.id]);

  // STM: сколько последних сообщений удалено + развёрнут ли весь буфер
  const [trimmed, setTrimmed] = useState(0);
  // Поле ввода N храним строкой: свободный ввод (можно стереть всё),
  // clamp в 1..max — только при потере фокуса или отправке
  const [trimCount, setTrimCount] = useState('5');
  const [showAllStm, setShowAllStm] = useState(false);
  // Поштучное удаление из полного STM: удалённые id и id с открытым подтверждением
  const [removedIds, setRemovedIds] = useState<number[]>([]);
  const [confirmId, setConfirmId] = useState<number | null>(null);

  // Смена персоны — сброс всех правок буфера и локальных фактов
  useEffect(() => {
    setTrimmed(0);
    setRemovedIds([]);
    setConfirmId(null);
    setShowAllStm(false);
    setAddedFacts([]);
    setAddedRaws({});
    setEdited({});
  }, [persona.id]);

  // LTM: правки и добавленные факты (локальный state); модалка факта (id=null — добавление)
  const [edited, setEdited] = useState<Record<number, { fact: string; category: string }>>({});
  const [addedFacts, setAddedFacts] = useState<LtmFact[]>([]);
  // raw-строки («Категория: факт»), ушедшие на бэкенд для session-added фактов —
  // нужны чтобы правка такого факта могла заменить его и на бэкенде
  const [addedRaws, setAddedRaws] = useState<Record<number, string>>({});
  const [factModal, setFactModal] = useState<{ id: number | null } | null>(null);
  const [factText, setFactText] = useState('');
  const [factCategory, setFactCategory] = useState('');

  const fullStm = useMemo(
    () => (apiOnline ? (apiStm ?? []) : buildFullStm(stmByPersona, persona.id)),
    [apiOnline, apiStm, stmByPersona, persona.id],
  );
  const stmBase = fullStm.filter((m) => !removedIds.includes(m.id)); // поштучно удалённые
  const stm = stmBase.slice(0, Math.max(0, stmBase.length - trimmed)); // с конца удалено trimmed
  const stmShown = showAllStm ? stm : stm.slice(-STM_SHORT);

  const ltmSource = apiOnline ? (apiLtm ?? []).map((x) => x.f) : (ltmByPersona[persona.id] ?? []);
  const ltmBase = ltmSource.map((f) => (edited[f.id] ? { ...f, ...edited[f.id] } : f));
  const ltm = [...ltmBase.filter((f) => !removedFactIds.includes(f.id)), ...addedFacts];
  const diary = apiOnline ? (apiDiary ?? []) : (diaryByPersona[persona.id] ?? []);
  const categories = [...new Set(ltm.map((f) => f.category))];

  // Перечитать STM с бэкенда после удаления и сбросить локальные правки буфера
  const reloadStm = () => {
    api
      .getHistory(persona.id)
      .then((msgs) => {
        setApiStm(
          msgs.map((m, i) => ({
            id: i + 1,
            role: m.role === 'user' ? 'user' : 'bot',
            text: m.content,
            time: timeFmt(m.timestamp),
          })),
        );
        setRemovedIds([]);
        setTrimmed(0);
        setConfirmId(null);
      })
      .catch(() => {});
  };

  // Досье может быть открыто, пока бот ещё отвечает: по завершении обмена
  // чат бампит stmEpoch — перечитываем STM, чтобы показать свежую реплику.
  // Первое срабатывание пропускаем — начальная загрузка и так свежая.
  const prevEpoch = useRef(stmEpoch);
  useEffect(() => {
    if (prevEpoch.current === stmEpoch) return;
    prevEpoch.current = stmEpoch;
    if (apiOnline) reloadStm();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stmEpoch, apiOnline]);

  // Удалить последние N сообщений из буфера (не больше текущего размера)
  const trimStm = () => {
    const n = Math.min(Math.max(1, Number(trimCount) || 1), fullStm.length || 1);
    setTrimCount(String(n));
    if (!apiOnline) {
      setTrimmed((t) => Math.min(fullStm.length, t + n));
      return;
    }
    // На бэкенде — из STM и ChromaDB; чат перечитает историю через onStmChange
    api
      .trimStm(persona.id, n)
      .then(() => {
        reloadStm();
        onStmChange?.();
      })
      .catch(() => {});
  };

  // Поштучное удаление: в API-режиме уходит на бэкенд (индекс = позиция в истории),
  // оптимистично прячем сразу; при ошибке — откатываем
  const deleteStmMessage = (m: StmMessage) => {
    setConfirmId(null);
    setRemovedIds((ids) => [...ids, m.id]);
    if (!apiOnline) return;
    api
      .deleteStmMessage(persona.id, m.id - 1)
      .then(() => {
        reloadStm();
        onStmChange?.();
      })
      .catch(() => setRemovedIds((ids) => ids.filter((x) => x !== m.id)));
  };

  // Clamp поля при потере фокуса: пусто/меньше — минимум, больше — максимум
  const clampTrimInput = () => {
    const max = fullStm.length || 1;
    const n = Number(trimCount);
    if (!trimCount.trim() || Number.isNaN(n) || n < 1) setTrimCount('1');
    else if (n > max) setTrimCount(String(max));
  };

  // Открыть модалку факта: с фактом — редактирование, без — добавление
  const openFactEditor = (f?: LtmFact) => {
    setFactModal({ id: f?.id ?? null });
    setFactText(f?.fact ?? '');
    setFactCategory(f?.category ?? '');
  };

  const saveFact = () => {
    const text = factText.trim();
    if (!text || !factModal) return;
    const category = factCategory.trim() || 'General';
    // На бэкенд уходит исходный формат «Категория: факт»
    const newRaw = factCategory.trim() ? `${category}: ${text}` : text;
    if (factModal.id == null) {
      // Добавление нового факта (категория по умолчанию — General)
      const id = Date.now();
      setAddedFacts((prev) => [...prev, { id, fact: text, category }]);
      setAddedRaws((prev) => ({ ...prev, [id]: newRaw }));
      if (apiOnline) {
        api.addFact(persona.id, newRaw).catch(() => {});
      }
    } else if (addedFacts.some((f) => f.id === factModal.id)) {
      // Правка факта, добавленного в этой сессии: он уже ушёл на бэкенд —
      // заменяем и там, иначе после перезагрузки вернётся старый текст
      const oldRaw = addedRaws[factModal.id];
      setAddedFacts((prev) =>
        prev.map((f) => (f.id === factModal.id ? { ...f, fact: text, category } : f)),
      );
      if (oldRaw && oldRaw !== newRaw) {
        if (apiOnline) {
          api
            .updateFact(persona.id, oldRaw, newRaw)
            .then(() => setAddedRaws((prev) => ({ ...prev, [factModal.id!]: newRaw })))
            .catch(() => {});
        } else {
          setAddedRaws((prev) => ({ ...prev, [factModal.id!]: newRaw }));
        }
      }
    } else {
      // Правка факта из LTM: уходит на бэкенд (PUT), локально — оптимистичный
      // оверлей; при ошибке откатываем, чтобы не показывать несохранённое
      const id = factModal.id;
      const oldRaw = apiLtm?.find((x) => x.f.id === id)?.raw;
      setEdited((prev) => ({ ...prev, [id]: { fact: text, category } }));
      if (apiOnline && oldRaw && oldRaw !== newRaw) {
        api
          .updateFact(persona.id, oldRaw, newRaw)
          .then(() => {
            // Бэкенд заменил факт: обновляем исходный raw и снимаем оверлей
            setApiLtm(
              (prev) =>
                prev?.map((x) => (x.f.id === id ? { f: parseFact(newRaw, id - 1), raw: newRaw } : x)) ??
                prev,
            );
            setEdited((prev) => {
              const { [id]: _drop, ...rest } = prev;
              return rest;
            });
          })
          .catch(() => {
            setEdited((prev) => {
              const { [id]: _drop, ...rest } = prev;
              return rest;
            });
          });
      }
    }
    setFactModal(null);
  };

  // Забыть факт: локально убираем из списка, на бэкенде — точечное забывание;
  // при ошибке (бэкенд лежал/404) — откатываем скрытие, чтобы факт не «воскресал»
  // при следующей загрузке, а было видно, что удаление не прошло
  const deleteFact = (f: LtmFact) => {
    setRemovedFactIds((ids) => [...ids, f.id]);
    if (!apiOnline) return;
    const raw = apiLtm?.find((x) => x.f.id === f.id)?.raw ?? addedRaws[f.id] ?? f.fact;
    api
      .forgetFact(persona.id, raw)
      .catch(() => setRemovedFactIds((ids) => ids.filter((x) => x !== f.id)));
  };

  return (
    <div className={embedded ? undefined : 'section'}>
      {!embedded && (
        <div className="section-header">
          <div>
            <h1 className="section-title">{t('mem.title')}</h1>
            <p className="section-subtitle">{t('mem.subtitle')}</p>
          </div>
          <span className="badge badge--active">{persona.name}</span>
        </div>
      )}

      {/* Переключатель персон: память у каждой своя (скрыт при фиксированной персоне) */}
      {!fixedId && (
        <div className="tabs room-persona-tabs">
          {personas.map((p) => (
            <button
              key={p.id}
              type="button"
              className={`tab ${p.id === persona.id ? 'tab--active' : ''}`}
              onClick={() => setSelectedId(p.id)}
            >
              {p.name}
            </button>
          ))}
        </div>
      )}

      {/* Статистика */}
      <div className="stats-row">
        <div className="card stat-card">
          <div className="stat-value">{stm.length}</div>
          <div className="stat-label">
            {t('mem.statStm')}
            <InfoButton helpKey="mem.stm" />
          </div>
        </div>
        <div className="card stat-card">
          <div className="stat-value">{ltm.length}</div>
          <div className="stat-label">
            {t('mem.statLtm')}
            <InfoButton helpKey="mem.ltm" />
          </div>
        </div>
        <div className="card stat-card">
          <div className="stat-value">{diary.length}</div>
          <div className="stat-label">
            {t('mem.statDiary')}
            <InfoButton helpKey="mem.diary" />
          </div>
        </div>
        <div className="card stat-card">
          <div className="stat-value">{categories.length}</div>
          <div className="stat-label">
            {t('mem.statCats')}
            <InfoButton helpKey="mem.ltm" />
          </div>
        </div>
      </div>

      {/* Вкладки */}
      <div className="tabs">
        <button className={`tab ${tab === 'stm' ? 'tab--active' : ''}`} onClick={() => setTab('stm')}>
          {t('mem.tabStm')}
        </button>
        <button className={`tab ${tab === 'ltm' ? 'tab--active' : ''}`} onClick={() => setTab('ltm')}>
          {t('mem.tabLtm')}
        </button>
        <button className={`tab ${tab === 'diary' ? 'tab--active' : ''}`} onClick={() => setTab('diary')}>
          {t('mem.tabDiary')}
        </button>
        <InfoButton helpKey="mem.tabs" />
      </div>

      {tab === 'stm' && (
        <div className="card">
          <div className="card-title-row">
            <h2 className="card-title">
              {showAllStm ? t('mem.bufferAll') : t('mem.bufferLast')}
              <InfoButton helpKey="mem.stm" />
            </h2>
            <button className="btn btn--ghost" onClick={() => setShowAllStm((v) => !v)}>
              {showAllStm ? t('mem.collapse') : t('mem.showAll')}
            </button>
          </div>

          {/* Удаление последних N сообщений из буфера */}
          <div className="mem-trim">
            <span className="field-label" style={{ marginBottom: 0 }}>{t('mem.trimLast')}</span>
            <input
              className="input mem-trim-input"
              type="number"
              min={1}
              max={stm.length || 1}
              value={trimCount}
              onChange={(e) => setTrimCount(e.target.value)}
              onBlur={clampTrimInput}
            />
            <span className="field-label" style={{ marginBottom: 0 }}>{t('mem.messagesN')}</span>
            <button
              className="btn btn--danger"
              disabled={stm.length === 0}
              onClick={trimStm}
              title={t('mem.deleteTitle')}
            >
              {t('mem.delete')}
            </button>
            <InfoButton helpKey="mem.clearStm" />
            {trimmed > 0 && <span className="badge">{t('mem.deletedBadge', { n: trimmed })}</span>}
          </div>

          {!showAllStm && stm.length > STM_SHORT && (
            <div className="field-hint">{t('mem.shownHint', { short: STM_SHORT, total: stm.length })}</div>
          )}
          <ul className="memory-list">
            {stmShown.map((m, i) => (
              <li key={m.id} className="memory-item stagger-item" style={{ animationDelay: `${i * 40}ms` }}>
                <span className={`badge ${m.role === 'user' ? 'badge--user' : 'badge--bot'}`}>
                  {m.role === 'user' ? t('mem.userBadge') : t('mem.botBadge')}
                </span>
                <span className="memory-item-text">{m.text}</span>
                <span className="memory-item-time">{m.time}</span>
                {/* В развёрнутом виде — поштучное удаление с подтверждением */}
                {showAllStm &&
                  (confirmId === m.id ? (
                    <span className="mem-del-confirm">
                      <button
                        type="button"
                        className="btn btn--icon mem-del-yes"
                        title={t('mem.confirmDelete')}
                        onClick={() => deleteStmMessage(m)}
                      >
                        ✓
                      </button>
                      <button
                        type="button"
                        className="btn btn--icon"
                        title={t('mem.cancelTitle')}
                        onClick={() => setConfirmId(null)}
                      >
                        ✕
                      </button>
                    </span>
                  ) : (
                    <button
                      type="button"
                      className="btn btn--icon mem-del"
                      title={t('mem.deleteMsgTitle')}
                      onClick={() => setConfirmId(m.id)}
                    >
                      ✕
                    </button>
                  ))}
              </li>
            ))}
          </ul>
        </div>
      )}

      {tab === 'ltm' && (
        <div className="card">
          <div className="card-title-row">
            <h2 className="card-title">
              {t('mem.factsTitle')}
              <InfoButton helpKey="mem.ltm" />
            </h2>
            <button className="btn btn--primary" onClick={() => openFactEditor()}>{t('mem.addFact')}</button>
          </div>
          {categories.map((cat) => (
            <div key={cat} className="ltm-category">
              <div className="ltm-category-name">{cat}</div>
              <ul className="memory-list">
                {ltm
                  .filter((f) => f.category === cat)
                  .map((f, i) => (
                    <li key={f.id} className="memory-item stagger-item" style={{ animationDelay: `${i * 40}ms` }}>
                      <span className="memory-item-text">{f.fact}</span>
                      <button className="btn btn--icon" title={t('common.edit')} onClick={() => openFactEditor(f)}>
                        ✎
                      </button>
                      <button className="btn btn--icon" title={t('common.delete')} onClick={() => deleteFact(f)}>✕</button>
                    </li>
                  ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      {tab === 'ltm' && apiDossier &&
        (apiDossier.interests.length > 0 ||
          apiDossier.topics.length > 0 ||
          apiDossier.personality_notes.length > 0) && (
        <div className="card">
          <div className="card-title-row">
            <h2 className="card-title">
              {t('mem.dossierTitle')}
              <InfoButton helpKey="mem.dossier" />
            </h2>
          </div>
          {apiDossier.interests.length > 0 && (
            <div className="ltm-category">
              <div className="ltm-category-name">{t('mem.interestsTitle')}</div>
              <div className="badge-row">
                {apiDossier.interests.map((v) => (
                  <span key={v} className="badge">{v}</span>
                ))}
              </div>
            </div>
          )}
          {apiDossier.topics.length > 0 && (
            <div className="ltm-category">
              <div className="ltm-category-name">{t('mem.topicsTitle')}</div>
              <div className="badge-row">
                {apiDossier.topics.map((v) => (
                  <span key={v} className="badge badge--muted">{v}</span>
                ))}
              </div>
            </div>
          )}
          {apiDossier.personality_notes.length > 0 && (
            <div className="ltm-category">
              <div className="ltm-category-name">{t('mem.notesTitle')}</div>
              <ul className="memory-list">
                {apiDossier.personality_notes.map((v, i) => (
                  <li key={i} className="memory-item">
                    <span className="memory-item-text">{v}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {tab === 'diary' && (
        <div className="card">
          <div className="card-title-row">
            <h2 className="card-title">
              {t('mem.diaryTitle')}
              <InfoButton helpKey="mem.diary" />
            </h2>
          </div>
          <ul className="memory-list">
            {diary.map((e, i) => (
              <li key={e.id} className="diary-item stagger-item" style={{ animationDelay: `${i * 50}ms` }}>
                <div className="diary-date">{e.date}</div>
                <div className="memory-item-text">{e.text}</div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Модалка факта LTM: добавление (id=null) и редактирование */}
      {factModal && (
        <FormModal
          title={factModal.id == null ? t('mem.newFact') : t('mem.editFact')}
          badge="LTM_01"
          submitLabel={t('common.save')}
          submitDisabled={!factText.trim()}
          onSubmit={saveFact}
          onClose={() => setFactModal(null)}
        >
          <div className="field">
            <label className="field-label" htmlFor="mem-fact-text">{t('mem.factText')}</label>
            <input
              id="mem-fact-text"
              className="input"
              value={factText}
              onChange={(e) => setFactText(e.target.value)}
              autoFocus
            />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label className="field-label" htmlFor="mem-fact-cat">{t('mem.category')}</label>
            <input
              id="mem-fact-cat"
              className="input"
              value={factCategory}
              onChange={(e) => setFactCategory(e.target.value)}
            />
          </div>
        </FormModal>
      )}
    </div>
  );
}
