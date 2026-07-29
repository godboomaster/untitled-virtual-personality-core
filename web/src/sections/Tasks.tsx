import { reminders, todoItems } from '../mockData';
import InfoButton from '../components/InfoButton';

export default function Tasks() {
  return (
    <div className="section">
      <div className="section-header">
        <div>
          <h1 className="section-title">Напоминания и задачи</h1>
          <p className="section-subtitle">reminder_manager — напоминалки с датой и повтором; todo_manager — список дел.</p>
        </div>
      </div>

      <div className="two-col">
        {/* Напоминания */}
        <div className="card">
          <div className="card-title-row">
            <h2 className="card-title">
              Напоминания
              <InfoButton helpKey="tasks.reminders" />
            </h2>
            <button className="btn btn--primary">+ Добавить</button>
          </div>
          <ul className="memory-list">
            {reminders.map((r, i) => (
              <li key={r.id} className="reminder-item stagger-item" style={{ animationDelay: `${i * 50}ms` }}>
                <div className="reminder-main">
                  <div className="memory-item-text">{r.text}</div>
                  <div className="reminder-meta">
                    {r.time} · {r.repeat}
                    <InfoButton helpKey="tasks.reminderRepeat" />
                  </div>
                </div>
                <div className="reminder-side">
                  <label className="switch">
                    <input type="checkbox" defaultChecked={r.active} readOnly />
                    <span className="switch-slider" />
                  </label>
                  <InfoButton helpKey="tasks.reminderSwitch" />
                  <button className="btn btn--icon" title="Удалить">✕</button>
                </div>
              </li>
            ))}
          </ul>
        </div>

        {/* To-do */}
        <div className="card">
          <div className="card-title-row">
            <h2 className="card-title">
              To-do список
              <InfoButton helpKey="tasks.todo" />
            </h2>
            <button className="btn btn--primary">+ Добавить</button>
          </div>
          <ul className="memory-list">
            {todoItems.map((t, i) => (
              <li key={t.id} className="todo-item stagger-item" style={{ animationDelay: `${i * 50}ms` }}>
                <label className="todo-label">
                  <input type="checkbox" defaultChecked={t.done} readOnly />
                  <span className={t.done ? 'todo-text todo-text--done' : 'todo-text'}>{t.text}</span>
                </label>
                <button className="btn btn--icon" title="Удалить">✕</button>
              </li>
            ))}
          </ul>
          <div className="todo-progress">
            Выполнено: {todoItems.filter((t) => t.done).length} из {todoItems.length}
            <InfoButton helpKey="tasks.todoProgress" />
          </div>
        </div>
      </div>
    </div>
  );
}
