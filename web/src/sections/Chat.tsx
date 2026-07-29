import { useState } from 'react';
import { personas, chatMessages, ltmFacts, generationDefaults } from '../mockData';

export default function Chat() {
  const [selectedId, setSelectedId] = useState('connor');
  const [panelOpen, setPanelOpen] = useState(true);
  const persona = personas.find((p) => p.id === selectedId)!;
  const recentFacts = ltmFacts.slice(-2);

  return (
    <div className="chat-layout">
      {/* Список персон */}
      <div className="chat-persona-list">
        <div className="chat-persona-list-title">Персоны</div>
        {personas.map((p, i) => (
          <button
            key={p.id}
            className={`chat-persona-item stagger-item ${p.id === selectedId ? 'chat-persona-item--active' : ''}`}
            style={{ animationDelay: `${i * 40}ms` }}
            onClick={() => setSelectedId(p.id)}
          >
            <div className="avatar">{p.name.charAt(0)}</div>
            <div className="chat-persona-info">
              <div className="chat-persona-name">{p.name}</div>
              <div className="chat-persona-status">{p.status}</div>
            </div>
            {p.active && <span className="dot dot--green" title="Активная персона" />}
          </button>
        ))}
      </div>

      {/* Окно чата */}
      <div className="chat-main">
        <div className="chat-header">
          <div className="avatar avatar--large">{persona.name.charAt(0)}</div>
          <div>
            <div className="chat-header-name">{persona.name}</div>
            <div className="chat-header-status">
              {persona.status === 'онлайн' ? 'печатает...' : persona.status} · {persona.model}
            </div>
          </div>
          <div className="chat-header-actions">
            <button className="btn btn--ghost" title="Очистить память диалога">
              ✕ Очистить память
            </button>
          </div>
        </div>

        <div className="chat-messages">
          {chatMessages.map((m, i) => (
            <div
              key={m.id}
              className={`message message--${m.role}`}
              style={{ animationDelay: `${i * 60}ms` }}
            >
              <div className="message-bubble">
                <div className="message-text">{m.text}</div>
                <div className="message-time">{m.time}</div>
              </div>
            </div>
          ))}
          <div className="message message--bot" style={{ animationDelay: `${chatMessages.length * 60}ms` }}>
            <div className="message-bubble message-bubble--typing">
              <span className="typing-dot" />
              <span className="typing-dot" />
              <span className="typing-dot" />
            </div>
          </div>
        </div>

        <div className="chat-status-bar">
          <div className="chat-status-left">
            <span className="dot dot--green" />
            <span>
              STM {chatMessages.length}/{generationDefaults.stmSize} | LTM {ltmFacts.length}
            </span>
          </div>
          <span>VPC CORE // ONLINE</span>
        </div>

        <div className="chat-input-bar">
          <button className="btn btn--icon" title="Прикрепить файл (для векторного поиска)">
            +
          </button>
          <input className="chat-input" type="text" placeholder={`Сообщение для ${persona.name}...`} />
          <button className="btn btn--primary" title="Отправить">
            Отправить
          </button>
        </div>
      </div>

      {/* Контекстная панель: статус персоны */}
      <aside className={`chat-context ${panelOpen ? '' : 'chat-context--collapsed'}`}>
        <button
          type="button"
          className="chat-context-toggle"
          onClick={() => setPanelOpen((v) => !v)}
          title={panelOpen ? 'Свернуть панель' : 'Развернуть панель'}
        >
          {panelOpen ? '»' : '«'}
        </button>
        <div className="chat-context-body">
          <div className="chat-context-title">Статус персоны</div>
          <div className="chat-mood">
            <span className="dot dot--green" />
            <span>Настроение: норма</span>
          </div>
          <div className="stats-row stats-row--compact">
            <div className="stat-card">
              <div className="stat-value">{chatMessages.length}</div>
              <div className="stat-label">сообщений в памяти</div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{ltmFacts.length}</div>
              <div className="stat-label">фактов о вас</div>
            </div>
          </div>
          <div className="chat-context-title">Недавние факты</div>
          <ul className="memory-list">
            {recentFacts.map((f) => (
              <li key={f.id} className="memory-item">
                <span className="memory-item-text">{f.fact}</span>
              </li>
            ))}
          </ul>
        </div>
      </aside>
    </div>
  );
}
