import { llmProviders, generationDefaults, featureFlags } from '../mockData';
import InfoButton from '../components/InfoButton';
import type { HelpKey } from '../helpTexts';

const featureHelpKeys: Record<string, HelpKey> = {
  web_search: 'settings.feature.web_search',
  moderation: 'settings.feature.moderation',
  ltm_extraction: 'settings.feature.ltm_extraction',
  rag: 'settings.feature.rag',
  self_memory: 'settings.feature.self_memory',
  proactive: 'settings.feature.proactive',
};

export default function Settings() {
  return (
    <div className="section">
      <div className="section-header">
        <div>
          <h1 className="section-title">Настройки</h1>
          <p className="section-subtitle">Провайдеры LLM, параметры генерации и включаемые фичи.</p>
        </div>
        <button className="btn btn--primary">Сохранить</button>
      </div>

      {/* Провайдеры */}
      <div className="card">
        <h2 className="card-title">
          LLM-провайдеры
          <InfoButton helpKey="settings.activeProvider" />
        </h2>
        <ul className="memory-list">
          {llmProviders
            .filter((p) => !p.local)
            .map((p, i) => (
              <li key={p.id} className="provider-item stagger-item" style={{ animationDelay: `${i * 40}ms` }}>
                <label className="provider-main">
                  <input type="radio" name="provider" defaultChecked={p.active} readOnly />
                  <span className="provider-name">{p.name}</span>
                  {p.model && <span className="provider-model">{p.model}</span>}
                </label>
                <div className="provider-side">
                  {p.keySet ? (
                    <span className="badge badge--success">
                      ключ задан{p.keysCount > 1 ? ` · ${p.keysCount} шт (ротация)` : ''}
                    </span>
                  ) : (
                    <span className="badge badge--muted">ключ не задан</span>
                  )}
                  <InfoButton helpKey="settings.keyStatus" />
                  <button className="btn btn--ghost">Ключи</button>
                  <InfoButton helpKey="settings.keyRotation" />
                </div>
              </li>
            ))}
        </ul>
      </div>

      {/* Локальные модели */}
      <div className="card">
        <div className="card-title-row">
          <h2 className="card-title">
            Локальные модели (local_router)
            <InfoButton helpKey="settings.localModels" />
          </h2>
          <button className="btn btn--ghost">Проверить доступность</button>
        </div>
        <ul className="memory-list">
          {llmProviders
            .filter((p) => p.local)
            .map((p, i) => (
              <li key={p.id} className="provider-item stagger-item" style={{ animationDelay: `${i * 40}ms` }}>
                <label className="provider-main">
                  <input type="radio" name="provider" defaultChecked={p.active} readOnly />
                  <span className="provider-name">{p.name}</span>
                  {p.model && <span className="provider-model">{p.model}</span>}
                </label>
                <div className="provider-side">
                  <span className="badge badge--success">доступен</span>
                </div>
              </li>
            ))}
        </ul>
        <div className="field">
          <label className="field-label">
            URL локального сервера
            <InfoButton helpKey="settings.localUrl" />
          </label>
          <input className="input" type="text" defaultValue="http://localhost:11434" readOnly />
        </div>
      </div>

      {/* Параметры генерации */}
      <div className="card">
        <h2 className="card-title">Параметры генерации</h2>
        <div className="field-grid">
          <div className="field">
            <label className="field-label">
              Temperature
              <InfoButton helpKey="settings.temperature" />
            </label>
            <input className="input" type="number" step="0.1" defaultValue={generationDefaults.temperature} readOnly />
          </div>
          <div className="field">
            <label className="field-label">
              Max tokens
              <InfoButton helpKey="settings.maxTokens" />
            </label>
            <input className="input" type="number" defaultValue={generationDefaults.maxTokens} readOnly />
          </div>
          <div className="field">
            <label className="field-label">
              Top-p
              <InfoButton helpKey="settings.topP" />
            </label>
            <input className="input" type="number" step="0.05" defaultValue={generationDefaults.topP} readOnly />
          </div>
          <div className="field">
            <label className="field-label">
              Размер STM (сообщений)
              <InfoButton helpKey="settings.stmSize" />
            </label>
            <input className="input" type="number" defaultValue={generationDefaults.stmSize} readOnly />
          </div>
        </div>
      </div>

      {/* Фичи */}
      <div className="card">
        <h2 className="card-title">Фичи</h2>
        <div className="features-grid">
          {featureFlags.map((f) => (
            <label key={f.id} className="checkbox-row">
              <input type="checkbox" defaultChecked={f.enabled} readOnly />
              <span>{f.label}</span>
              {featureHelpKeys[f.id] && <InfoButton helpKey={featureHelpKeys[f.id]} />}
            </label>
          ))}
        </div>
      </div>
    </div>
  );
}
