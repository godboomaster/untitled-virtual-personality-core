import { useState } from 'react';
import { stmMessages, ltmFacts, diaryEntries } from '../mockData';
import InfoButton from '../components/InfoButton';

type Tab = 'stm' | 'ltm' | 'diary';

export default function Memory() {
  const [tab, setTab] = useState<Tab>('stm');

  const categories = [...new Set(ltmFacts.map((f) => f.category))];

  return (
    <div className="section">
      <div className="section-header">
        <div>
          <h1 className="section-title">Память</h1>
          <p className="section-subtitle">Двухуровневая память: STM (буфер сообщений), LTM (факты), дневник бота (self_memory).</p>
        </div>
      </div>

      {/* Статистика */}
      <div className="stats-row">
        <div className="card stat-card">
          <div className="stat-value">{stmMessages.length}</div>
          <div className="stat-label">
            сообщений в STM
            <InfoButton helpKey="mem.stm" />
          </div>
        </div>
        <div className="card stat-card">
          <div className="stat-value">{ltmFacts.length}</div>
          <div className="stat-label">
            фактов в LTM
            <InfoButton helpKey="mem.ltm" />
          </div>
        </div>
        <div className="card stat-card">
          <div className="stat-value">{diaryEntries.length}</div>
          <div className="stat-label">
            эпизодов в дневнике
            <InfoButton helpKey="mem.diary" />
          </div>
        </div>
        <div className="card stat-card">
          <div className="stat-value">{categories.length}</div>
          <div className="stat-label">
            категорий фактов
            <InfoButton helpKey="mem.ltm" />
          </div>
        </div>
      </div>

      {/* Вкладки */}
      <div className="tabs">
        <button className={`tab ${tab === 'stm' ? 'tab--active' : ''}`} onClick={() => setTab('stm')}>
          STM · краткосрочная
        </button>
        <button className={`tab ${tab === 'ltm' ? 'tab--active' : ''}`} onClick={() => setTab('ltm')}>
          LTM · долгосрочная
        </button>
        <button className={`tab ${tab === 'diary' ? 'tab--active' : ''}`} onClick={() => setTab('diary')}>
          Дневник бота
        </button>
        <InfoButton helpKey="mem.tabs" />
      </div>

      {tab === 'stm' && (
        <div className="card">
          <div className="card-title-row">
            <h2 className="card-title">
              Буфер последних сообщений
              <InfoButton helpKey="mem.stm" />
            </h2>
            <button className="btn btn--danger">Очистить STM</button>
            <InfoButton helpKey="mem.clearStm" />
          </div>
          <ul className="memory-list">
            {stmMessages.map((m, i) => (
              <li key={m.id} className="memory-item stagger-item" style={{ animationDelay: `${i * 40}ms` }}>
                <span className={`badge ${m.role === 'user' ? 'badge--user' : 'badge--bot'}`}>
                  {m.role === 'user' ? 'пользователь' : 'бот'}
                </span>
                <span className="memory-item-text">{m.text}</span>
                <span className="memory-item-time">{m.time}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {tab === 'ltm' && (
        <div className="card">
          <div className="card-title-row">
            <h2 className="card-title">
              Факты о пользователе
              <InfoButton helpKey="mem.ltm" />
            </h2>
            <button className="btn btn--primary">+ Добавить факт</button>
          </div>
          {categories.map((cat) => (
            <div key={cat} className="ltm-category">
              <div className="ltm-category-name">{cat}</div>
              <ul className="memory-list">
                {ltmFacts
                  .filter((f) => f.category === cat)
                  .map((f, i) => (
                    <li key={f.id} className="memory-item stagger-item" style={{ animationDelay: `${i * 40}ms` }}>
                      <span className="memory-item-text">{f.fact}</span>
                      <button className="btn btn--icon" title="Редактировать">✏️</button>
                      <button className="btn btn--icon" title="Удалить">✕</button>
                    </li>
                  ))}
              </ul>
            </div>
          ))}
        </div>
      )}

      {tab === 'diary' && (
        <div className="card">
          <div className="card-title-row">
            <h2 className="card-title">
              Дневник бота (self_memory)
              <InfoButton helpKey="mem.diary" />
            </h2>
            <button className="btn btn--primary">+ Добавить эпизод</button>
          </div>
          <ul className="memory-list">
            {diaryEntries.map((e, i) => (
              <li key={e.id} className="diary-item stagger-item" style={{ animationDelay: `${i * 50}ms` }}>
                <div className="diary-date">{e.date}</div>
                <div className="memory-item-text">{e.text}</div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
