import { useEffect, useState } from 'react';
import { useI18n, useMockData } from '../i18n';
import type { InitiativeEvent, InitiativeState } from '../mockData';
import { api } from '../api';
import type { InitiativeData } from '../api';
import { useApiOnline } from '../apiData';
import { formatSilence, INIT_TYPE_MAP, silenceProgress } from '../initiativeTypes';
import InfoButton from '../components/InfoButton';

interface InitiativeProps {
  personaId?: string; // фиксированная персона (модалка «Досье») — без табов персон
  embedded?: boolean; // встраивание без заголовка раздела
}

export default function Initiative({ personaId: fixedId, embedded }: InitiativeProps) {
  const { t, lang } = useI18n();
  const { personas, initiativeStateByPersona, initiativeByPersona } = useMockData();
  const apiOnline = useApiOnline();

  // Шкала эмоций — из словаря текущей локали
  const emotionalStages = t('init.stages').split('|');

  // Параметры и история самоинициативы индивидуальны для каждой персоны
  const [selectedId, setSelectedId] = useState(() => fixedId ?? personas[0].id);
  const persona = personas.find((p) => p.id === (fixedId ?? selectedId)) ?? personas[0];

  // Состояние проактивности с бэкенда (null — выключена у персоны или API недоступен)
  const [apiData, setApiData] = useState<InitiativeData | null>(null);
  useEffect(() => {
    setApiData(null);
    if (!apiOnline) return;
    let stale = false;
    api.getInitiative(persona.id).then((d) => !stale && setApiData(d)).catch(() => {});
    return () => {
      stale = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiOnline, persona.id]);

  // Время последней реплики пользователя в веб-чате — тот же источник, что у
  // «последний ответ: …» на главной (GET /api/home → last_user_ts); раз в
  // минуту, как главная. nowMs — момент замера, от него считается молчание
  const [lastUserTs, setLastUserTs] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    setLastUserTs(null);
    if (!apiOnline) return;
    let alive = true;
    const load = () => {
      setNowMs(Date.now());
      api
        .getHome()
        .then((o) => alive && setLastUserTs(o.personas[persona.id]?.last_user_ts ?? null))
        .catch(() => {});
    };
    load();
    const timer = setInterval(load, 60_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [apiOnline, persona.id]);

  // Эмоциональная ступень из ignore streak (пороги ядра: 3/5/7/10)
  const stageForStreak = (streak: number) =>
    emotionalStages[streak < 3 ? 0 : streak < 5 ? 1 : streak < 7 ? 2 : streak < 10 ? 3 : 4];

  const s: InitiativeState = apiData
    ? {
        enabled: apiData.enabled,
        silenceThresholdMin: apiData.silence_threshold_minutes,
        probability: apiData.initiative_probability,
        maxPerDay: apiData.max_daily_initiatives,
        checkIntervalMin: apiData.check_interval_minutes,
        adaptiveThreshold: apiData.adaptive_threshold,
        bayesianFeedback: apiData.feedback_enabled,
        ignoreStreak: apiData.ignore_streak,
        emotionalState: stageForStreak(apiData.ignore_streak),
        initiativesToday: apiData.initiatives_today,
      }
    : { enabled: false, ...(initiativeStateByPersona[persona.id] ?? initiativeStateByPersona.connor) };
  const history: InitiativeEvent[] = apiOnline
    ? (apiData?.history ?? []).map((h, i) => ({
        id: i + 1,
        type: INIT_TYPE_MAP[h.type] ?? 'thought',
        typeLabel: h.type,
        text: h.message,
        time: h.date,
        outcome: 'pending' as const,
      }))
    : (initiativeByPersona[persona.id] ?? []);
  // Шкала эмоций: нестандартное состояние персоны добавляем отдельной ступенью
  const stages = emotionalStages.includes(s.emotionalState)
    ? emotionalStages
    : [...emotionalStages, s.emotionalState];

  // Слайдеры с двусторонней синхронизацией: ползунок ↔ числовое поле.
  // Текстовый ввод свободный (можно стереть всё), clamp — только на blur.
  const [silence, setSilence] = useState(s.silenceThresholdMin);
  const [silenceText, setSilenceText] = useState(String(s.silenceThresholdMin));
  const [probability, setProbability] = useState(Math.round(s.probability * 100));
  const [probabilityText, setProbabilityText] = useState(String(Math.round(s.probability * 100)));
  // Остальные параметры проактивности (лимит в день, интервал, флаги)
  const [enabled, setEnabled] = useState(s.enabled ?? false);
  const [maxPerDay, setMaxPerDay] = useState(s.maxPerDay);
  const [maxPerDayText, setMaxPerDayText] = useState(String(s.maxPerDay));
  const [checkInterval, setCheckInterval] = useState(s.checkIntervalMin);
  const [checkIntervalText, setCheckIntervalText] = useState(String(s.checkIntervalMin));
  const [adaptive, setAdaptive] = useState(s.adaptiveThreshold);
  const [feedback, setFeedback] = useState(s.bayesianFeedback);
  // Окно времени самоинициативы (задаёт пользователь; null — круглые сутки)
  const [hoursOn, setHoursOn] = useState(false);
  const [hoursFrom, setHoursFrom] = useState('09:00');
  const [hoursTo, setHoursTo] = useState('22:00');
  const [savedFlash, setSavedFlash] = useState(false);

  // При смене персоны (или приходе состояния с бэкенда) подтягиваем её параметры
  useEffect(() => {
    setEnabled(s.enabled ?? false);
    setSilence(s.silenceThresholdMin);
    setSilenceText(String(s.silenceThresholdMin));
    setProbability(Math.round(s.probability * 100));
    setProbabilityText(String(Math.round(s.probability * 100)));
    setMaxPerDay(s.maxPerDay);
    setMaxPerDayText(String(s.maxPerDay));
    setCheckInterval(s.checkIntervalMin);
    setCheckIntervalText(String(s.checkIntervalMin));
    setAdaptive(s.adaptiveThreshold);
    setFeedback(s.bayesianFeedback);
    const win = (apiData?.initiative_hours ?? '') || '';
    const m = win.match(/^(\d{2}:\d{2})-(\d{2}:\d{2})$/);
    setHoursOn(Boolean(m));
    if (m) {
      setHoursFrom(m[1]);
      setHoursTo(m[2]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persona.id, apiData]);

  // Общий обработчик «число в пределах [min, max]»
  const clamp = (v: number, min: number, max: number) => Math.max(min, Math.min(max, v));

  // Свободный ввод числа: валидное значение сразу двигает слайдер
  const onNumInput = (
    text: string,
    min: number,
    max: number,
    setText: (v: string) => void,
    setNum: (v: number) => void,
  ) => {
    setText(text);
    const n = Number(text);
    if (text !== '' && Number.isFinite(n)) setNum(clamp(n, min, max));
  };

  // На blur: пустое/невалидное возвращается к текущему значению слайдера
  const onNumBlur = (min: number, max: number, num: number, setText: (v: string) => void, setNum: (v: number) => void, text: string) => {
    const n = Number(text);
    const next = text !== '' && Number.isFinite(n) ? clamp(n, min, max) : num;
    setNum(next);
    setText(String(next));
  };

  // Сохранение параметров проактивности на бэкенд (YAML персоны + живой конфиг)
  const saveParams = () => {
    if (!apiData) return;
    // Окно времени самоинициативы: включено и обе границы валидны — строка,
    // иначе null (круглые сутки). Переход через полночь (22:00-08:00) валиден
    const hoursValid = /^\d{2}:\d{2}$/.test(hoursFrom) && /^\d{2}:\d{2}$/.test(hoursTo);
    api
      .updateInitiative(persona.id, {
        enabled,
        silence_threshold_minutes: silence,
        initiative_probability: probability / 100,
        max_daily_initiatives: maxPerDay,
        check_interval_minutes: checkInterval,
        adaptive_threshold: adaptive,
        feedback_enabled: feedback,
        initiative_hours: hoursOn && hoursValid ? `${hoursFrom}-${hoursTo}` : null,
      })
      .then(() => {
        setSavedFlash(true);
        setTimeout(() => setSavedFlash(false), 2000);
        // Перечитываем состояние — сервер мог клампить значения
        api.getInitiative(persona.id).then(setApiData).catch(() => {});
      })
      .catch(() => {});
  };

  // Когда персона пишет сама: порог молчания с бэкенда (адаптивный — под
  // темп реплик оператора). Без бэкенда подсказки нет
  const effSilence = apiData ? (apiData.effective_silence_minutes ?? apiData.silence_threshold_minutes) : null;
  const freqHint = effSilence == null || !apiData
    ? null
    : t(apiData.adaptive_threshold ? 'init.freqHintAdaptive' : 'init.freqHint', { n: formatSilence(effSilence, t, lang) });

  // Молчание пользователя против действующего порога (шкала и подпись)
  const silenceBar = silenceProgress(lastUserTs, effSilence, nowMs, t, lang);

  return (
    <div className={embedded ? undefined : 'section'}>
      {!embedded && (
        <div className="section-header">
          <div>
            <h1 className="section-title">
              {t('init.title')}
              <InfoButton helpKey="init.enabled" />
            </h1>
            <p className="section-subtitle">{t('init.subtitle')}</p>
          </div>
          <span className="badge badge--active">{persona.name}</span>
        </div>
      )}

      {/* Переключатель персон (скрыт при фиксированной персоне) */}
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

      <div className="two-col">
        {/* Настройки */}
        <div className="card">
          <h2 className="card-title">{t('init.params')}</h2>

          <label className="checkbox-row">
            {apiData ? (
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            ) : (
              <input type="checkbox" defaultChecked={s.enabled ?? false} readOnly />
            )}
            <span>{t('init.enabledToggle')}</span>
            <InfoButton helpKey="init.enabled" />
          </label>

          {/* Выключенная фича → зависимые настройки приглушены (остаются редактируемыми) */}
          <div className={`settings-group ${(apiData ? enabled : (s.enabled ?? false)) ? '' : 'settings-group--off'}`}>
          <div className="field">
            <div className="field-label-row">
              <label className="field-label">
                {t('init.silenceThreshold')}
                <InfoButton helpKey="init.silenceThreshold" />
              </label>
              <span className="field-value">
                {silence >= 1440 && silence % 1440 === 0
                  ? t('init.days', { n: silence / 1440 })
                  : `${silence} ${t('init.min')}`}
              </span>
            </div>
            {/* Слайдер и число синхронизированы в обе стороны; максимум — сутки (1440) */}
            <div className="init-slider-row">
              <input
                type="range"
                min={15}
                max={1440}
                value={silence}
                onChange={(e) => {
                  setSilence(Number(e.target.value));
                  setSilenceText(e.target.value);
                }}
              />
              <input
                className="input init-num"
                type="number"
                min={15}
                max={1440}
                value={silenceText}
                onChange={(e) => onNumInput(e.target.value, 15, 1440, setSilenceText, setSilence)}
                onBlur={() => onNumBlur(15, 1440, silence, setSilenceText, setSilence, silenceText)}
              />
            </div>
          </div>

          <div className="field">
            <div className="field-label-row">
              <label className="field-label">
                {t('init.probability')}
                <InfoButton helpKey="init.probability" />
              </label>
              <span className="field-value">{probability}%</span>
            </div>
            <div className="init-slider-row">
              <input
                type="range"
                min={0}
                max={100}
                value={probability}
                onChange={(e) => {
                  setProbability(Number(e.target.value));
                  setProbabilityText(e.target.value);
                }}
              />
              <input
                className="input init-num"
                type="number"
                min={0}
                max={100}
                value={probabilityText}
                onChange={(e) => onNumInput(e.target.value, 0, 100, setProbabilityText, setProbability)}
                onBlur={() => onNumBlur(0, 100, probability, setProbabilityText, setProbability, probabilityText)}
              />
            </div>
          </div>

          <div className="field-grid">
            <div className="field">
              <label className="field-label">
                {t('init.maxPerDay')}
                <InfoButton helpKey="init.maxPerDay" />
              </label>
              {apiData ? (
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={100}
                  value={maxPerDayText}
                  onChange={(e) => onNumInput(e.target.value, 1, 100, setMaxPerDayText, setMaxPerDay)}
                  onBlur={() => onNumBlur(1, 100, maxPerDay, setMaxPerDayText, setMaxPerDay, maxPerDayText)}
                />
              ) : (
                <input className="input" type="number" defaultValue={s.maxPerDay} readOnly />
              )}
            </div>
            <div className="field">
              <label className="field-label">
                {t('init.checkInterval')}
                <InfoButton helpKey="init.checkInterval" />
              </label>
              {apiData ? (
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={1440}
                  value={checkIntervalText}
                  onChange={(e) => onNumInput(e.target.value, 1, 1440, setCheckIntervalText, setCheckInterval)}
                  onBlur={() => onNumBlur(1, 1440, checkInterval, setCheckIntervalText, setCheckInterval, checkIntervalText)}
                />
              ) : (
                <input className="input" type="number" defaultValue={s.checkIntervalMin} readOnly />
              )}
            </div>
          </div>

          {/* Окно времени самоинициативы — момент выбирает пользователь,
              не движок: вне окна персона не пишет сама (включая «жизнь») */}
          <div className="field">
            <div className="field-label-row">
              <label className="field-label">
                {t('init.hours')}
                <InfoButton helpKey="init.hours" />
              </label>
            </div>
            <div className="init-hours-row">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={hoursOn}
                  onChange={(e) => setHoursOn(e.target.checked)}
                />
                <span>{t('init.hoursLimit')}</span>
              </label>
              {hoursOn && (
                <>
                  <input
                    className="input init-hour"
                    type="time"
                    value={hoursFrom}
                    onChange={(e) => setHoursFrom(e.target.value || '09:00')}
                    aria-label={t('init.hoursFrom')}
                  />
                  <span className="field-hint">—</span>
                  <input
                    className="input init-hour"
                    type="time"
                    value={hoursTo}
                    onChange={(e) => setHoursTo(e.target.value || '22:00')}
                    aria-label={t('init.hoursTo')}
                  />
                </>
              )}
            </div>
            {hoursOn && <div className="field-hint">{t('init.hoursHint')}</div>}
          </div>

          <label className="checkbox-row">
            {apiData ? (
              <input type="checkbox" checked={adaptive} onChange={(e) => setAdaptive(e.target.checked)} />
            ) : (
              <input type="checkbox" defaultChecked={s.adaptiveThreshold} readOnly />
            )}
            <span>{t('init.adaptive')}</span>
            <InfoButton helpKey="init.adaptiveThreshold" />
          </label>
          <label className="checkbox-row">
            {apiData ? (
              <input type="checkbox" checked={feedback} onChange={(e) => setFeedback(e.target.checked)} />
            ) : (
              <input type="checkbox" defaultChecked={s.bayesianFeedback} readOnly />
            )}
            <span>{t('init.bayesian')}</span>
            <InfoButton helpKey="init.bayesianFeedback" />
          </label>

          <div className="field">
            <label className="field-label">
              {t('init.types')}
              <InfoButton helpKey="init.typeBalance" />
            </label>
            <div className="badge-row">
              <span className="badge">{t('init.typeQuestion')}</span>
              <span className="badge">{t('init.typeObservation')}</span>
              <span className="badge">{t('init.typeContinuation')}</span>
              <span className="badge">{t('init.typeThought')}</span>
            </div>
          </div>
          </div>

          <button className="btn btn--primary" onClick={apiData ? saveParams : undefined}>
            {savedFlash ? '✓' : t('init.saveParams')}
          </button>
          {freqHint && <div className="field-hint">{freqHint}</div>}
        </div>

        {/* Текущее состояние */}
        <div className="card">
          <h2 className="card-title">
            {t('init.currentState')}
            <InfoButton helpKey="init.multiTurn" />
          </h2>
          <div className="stats-row stats-row--compact">
            <div className="stat-card">
              <div className="stat-value">{s.ignoreStreak}</div>
              <div className="stat-label">
                {t('init.ignoreStreak')}
                <InfoButton helpKey="init.ignoreStreak" />
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{s.initiativesToday}</div>
              <div className="stat-label">
                {t('init.today')}
                <InfoButton helpKey="init.initiativesToday" />
              </div>
            </div>
          </div>

          <div className="field">
            <label className="field-label">
              {t('init.emotionalState')}
              <InfoButton helpKey="init.emotionalState" />
            </label>
            <div className="emotion-scale">
              {stages.map((stage) => (
                <span
                  key={stage}
                  className={`emotion-stage ${stage === s.emotionalState ? 'emotion-stage--current' : ''}`}
                >
                  {stage}
                </span>
              ))}
            </div>
          </div>

          <div className="field">
            <label className="field-label">
              {t('init.userSilence')}
              <InfoButton helpKey="init.silenceProgress" />
            </label>
            <div className="progress-bar">
              <div className="progress-fill" style={{ width: `${silenceBar.pct}%` }} />
            </div>
            <div className="field-hint">{silenceBar.text}</div>
          </div>
        </div>
      </div>

      {/* История инициатив */}
      <div className="card">
        <h2 className="card-title">
          {t('init.history')}
          <InfoButton helpKey="init.history" />
        </h2>
        <ul className="memory-list">
          {history.map((e, i) => (
            <li key={e.id} className="memory-item stagger-item" style={{ animationDelay: `${i * 50}ms` }}>
              <span className={`badge badge--type-${e.type}`}>{e.typeLabel}</span>
              <span className="memory-item-text">{e.text}</span>
              <span className="memory-item-time">{e.time}</span>
              <span
                className={`badge ${
                  e.outcome === 'answered'
                    ? 'badge--success'
                    : e.outcome === 'ignored'
                      ? 'badge--muted'
                      : ''
                }`}
              >
                {t(`outcome.${e.outcome}`)}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
