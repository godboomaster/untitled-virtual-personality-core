import { initiativeState, initiativeHistory } from '../mockData';
import InfoButton from '../components/InfoButton';

const emotionalStages = ['норма', 'лёгкая обида', 'обида', 'сильная обида', 'глубокая обида'];

export default function Initiative() {
  const s = initiativeState;

  return (
    <div className="section">
      <div className="section-header">
        <div>
          <h1 className="section-title">
            Инициатива
            <InfoButton helpKey="init.enabled" />
          </h1>
          <p className="section-subtitle">proactive_messaging: бот пишет первым при длительном молчании пользователя.</p>
        </div>
      </div>

      <div className="two-col">
        {/* Настройки */}
        <div className="card">
          <h2 className="card-title">Параметры</h2>

          <div className="field">
            <div className="field-label-row">
              <label className="field-label">
                Порог молчания
                <InfoButton helpKey="init.silenceThreshold" />
              </label>
              <span className="field-value">{s.silenceThresholdMin} мин</span>
            </div>
            <input type="range" min={15} max={720} defaultValue={s.silenceThresholdMin} readOnly />
          </div>

          <div className="field">
            <div className="field-label-row">
              <label className="field-label">
                Вероятность инициативы
                <InfoButton helpKey="init.probability" />
              </label>
              <span className="field-value">{Math.round(s.probability * 100)}%</span>
            </div>
            <input type="range" min={0} max={100} defaultValue={Math.round(s.probability * 100)} readOnly />
          </div>

          <div className="field-grid">
            <div className="field">
              <label className="field-label">
                Макс. инициатив в день
                <InfoButton helpKey="init.maxPerDay" />
              </label>
              <input className="input" type="number" defaultValue={s.maxPerDay} readOnly />
            </div>
            <div className="field">
              <label className="field-label">
                Интервал проверки (мин)
                <InfoButton helpKey="init.checkInterval" />
              </label>
              <input className="input" type="number" defaultValue={s.checkIntervalMin} readOnly />
            </div>
          </div>

          <label className="checkbox-row">
            <input type="checkbox" defaultChecked={s.adaptiveThreshold} readOnly />
            <span>Адаптивный порог молчания</span>
            <InfoButton helpKey="init.adaptiveThreshold" />
          </label>
          <label className="checkbox-row">
            <input type="checkbox" defaultChecked={s.bayesianFeedback} readOnly />
            <span>Байесовская обратная связь</span>
            <InfoButton helpKey="init.bayesianFeedback" />
          </label>

          <div className="field">
            <label className="field-label">
              Типы инициатив
              <InfoButton helpKey="init.typeBalance" />
            </label>
            <div className="badge-row">
              <span className="badge">question · вопрос</span>
              <span className="badge">observation · наблюдение</span>
              <span className="badge">continuation · продолжение</span>
              <span className="badge">thought · мысль</span>
            </div>
          </div>

          <button className="btn btn--primary">Сохранить настройки</button>
        </div>

        {/* Текущее состояние */}
        <div className="card">
          <h2 className="card-title">
            Текущее состояние
            <InfoButton helpKey="init.multiTurn" />
          </h2>
          <div className="stats-row stats-row--compact">
            <div className="stat-card">
              <div className="stat-value">{s.ignoreStreak}</div>
              <div className="stat-label">
                ignore streak
                <InfoButton helpKey="init.ignoreStreak" />
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{s.initiativesToday}</div>
              <div className="stat-label">
                инициатив сегодня
                <InfoButton helpKey="init.initiativesToday" />
              </div>
            </div>
          </div>

          <div className="field">
            <label className="field-label">
              Эмоциональное состояние
              <InfoButton helpKey="init.emotionalState" />
            </label>
            <div className="emotion-scale">
              {emotionalStages.map((stage) => (
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
              Молчание пользователя
              <InfoButton helpKey="init.silenceProgress" />
            </label>
            <div className="progress-bar">
              <div className="progress-fill" style={{ width: '55%' }} />
            </div>
            <div className="field-hint">99 мин из порога {s.silenceThresholdMin} мин</div>
          </div>
        </div>
      </div>

      {/* История инициатив */}
      <div className="card">
        <h2 className="card-title">
          Последние инициативы
          <InfoButton helpKey="init.history" />
        </h2>
        <ul className="memory-list">
          {initiativeHistory.map((e, i) => (
            <li key={e.id} className="memory-item stagger-item" style={{ animationDelay: `${i * 50}ms` }}>
              <span className={`badge badge--type-${e.type}`}>{e.typeLabel}</span>
              <span className="memory-item-text">{e.text}</span>
              <span className="memory-item-time">{e.time}</span>
              <span
                className={`badge ${
                  e.outcome === 'ответили'
                    ? 'badge--success'
                    : e.outcome === 'проигнорировали'
                      ? 'badge--muted'
                      : ''
                }`}
              >
                {e.outcome}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
