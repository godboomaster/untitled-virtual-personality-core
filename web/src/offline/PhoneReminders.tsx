import { useEffect, useReducer } from 'react';
import { useI18n } from '../i18n';
import { getKit, onKitRefreshed } from './kit';
import { cancelLocalReminder, localReminders, onRemindersChange } from './reminders';

/* «Напоминания и задачи» без ноутбука: что знает будильник телефона —
   поставленные на телефоне (их можно отменить) и напоминания ноутбука из
   кита (только посмотреть: правит их ядро). */

export default function PhoneReminders({ persona }: { persona: string }) {
  const { t, lang } = useI18n();
  const [, bump] = useReducer((x: number) => x + 1, 0);
  useEffect(() => {
    const offA = onRemindersChange(bump);
    const offB = onKitRefreshed(bump);
    const timer = window.setInterval(bump, 30_000); // прошедшие — из списка
    return () => {
      offA();
      offB();
      window.clearInterval(timer);
    };
  }, []);

  const now = Date.now() / 1000;
  const seen = new Set<string>();
  const items = [
    ...localReminders()
      .filter((r) => r.persona === persona && r.at > now)
      .map((r) => ({ key: `local:${r.id}`, id: r.id, task: r.task, at: r.at, local: true, recurring: false })),
    // Повторяющееся раскрыто в ките на две недели — здесь ближайшее
    ...(getKit(persona)?.reminders ?? [])
      .filter((r) => r.at > now && !seen.has(r.id) && seen.add(r.id))
      .map((r) => ({ key: r.ref, id: r.id, task: r.task || '', at: r.at, local: false, recurring: r.recurring })),
  ].sort((a, b) => a.at - b.at);

  const fmt = (ts: number) =>
    new Date(ts * 1000).toLocaleString(lang === 'ru' ? 'ru-RU' : 'en-US', {
      day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    });

  return (
    <>
      <p className="field-hint">{t('offline.tasks.lead')}</p>
      {items.length === 0 ? (
        <p className="field-hint">{t('offline.tasks.none')}</p>
      ) : (
        <ul className="memory-list">
          {items.map((r) => (
            <li key={r.key} className="reminder-item">
              <div className="reminder-main">
                <div className="memory-item-text">{r.task || '—'}</div>
                <div className="reminder-meta">
                  {fmt(r.at)} · {t(r.local ? 'offline.tasks.local' : 'offline.tasks.core')}
                  {r.recurring ? ` · ${t('offline.tasks.repeats')}` : ''}
                </div>
              </div>
              {r.local && (
                <div className="reminder-side">
                  <button className="btn btn--icon" title={t('offline.reminderCancel')} onClick={() => cancelLocalReminder(r.id)}>
                    ✕
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
