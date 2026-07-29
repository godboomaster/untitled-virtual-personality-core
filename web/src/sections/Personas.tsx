import { personas } from '../mockData';
import InfoButton from '../components/InfoButton';

export default function Personas() {
  return (
    <div className="section">
      <div className="section-header">
        <div>
          <h1 className="section-title">
            Персоны
            <InfoButton helpKey="persona.yaml" />
          </h1>
          <p className="section-subtitle">Персонажи-боты, определяются YAML-конфигами: системный промпт, генерация, фичи.</p>
        </div>
        <button className="btn btn--primary">+ Создать персону</button>
      </div>

      <div className="persona-grid">
        {personas.map((p, i) => (
          <div
            key={p.id}
            className={`card persona-card stagger-item ${p.active ? 'persona-card--active' : ''}`}
            style={{ animationDelay: `${i * 50}ms` }}
          >
            <div className="persona-card-head">
              <div className="avatar avatar--large">{p.name.charAt(0)}</div>
              <div>
                <div className="persona-card-name">{p.name}</div>
                <div className="persona-card-model">{p.model}</div>
              </div>
              {p.active && <span className="badge badge--active">активна</span>}
            </div>
            <p className="persona-card-desc">{p.description}</p>
            <div className="badge-row">
              {p.features.map((f) => (
                <span key={f} className="badge">
                  {f}
                </span>
              ))}
              <InfoButton helpKey="persona.features" />
            </div>
            <div className="persona-card-params">
              temp {p.temperature} · tokens {p.maxTokens} · top_p {p.topP}
              <InfoButton helpKey="persona.genParams" />
            </div>
            <div className="persona-card-actions">
              <button className="btn btn--ghost" disabled={p.active}>
                {p.active ? 'Выбрана' : 'Выбрать активной'}
              </button>
              <button className="btn btn--ghost">Редактировать</button>
              <button className="btn btn--ghost">Дублировать</button>
              <button className="btn btn--danger">Удалить</button>
              <InfoButton helpKey="persona.actions" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
