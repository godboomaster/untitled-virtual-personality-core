import { useState } from 'react';
import type { TaskCard as TaskCardData } from '../api';
import { useI18n } from '../i18n';

/* Карточка задачи агента в ленте чата (режим управления): цель, статус,
   план заказа с отметками и журнал хода. Обновляется на месте поллингом
   inbox, пока агент работает; итог висит ещё полчаса (CARD_TTL_SEC). */

const LOG_TAIL = 6; // строк журнала в свёрнутом виде

// Метка текущего шага плана по статусу задачи
const CUR_TAG: Record<string, string> = { ask: '[ ?? ]', confirm: '[ !! ]', paused: '[ || ]', payment: '[ !! ]' };
// Строка статуса внизу карточки
const LINE_TAG: Record<string, string> = {
  working: '[ .. ]', ask: '[ ?? ]', confirm: '[ !! ]', paused: '[ || ]',
  done: '[ OK ]', payment: '[ !! ]', cancelled: '[ -- ]', stopped: '[FAIL]',
};

export default function TaskCard({ card }: { card: TaskCardData }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const hidden = open ? 0 : Math.max(0, card.log.length - LOG_TAIL);
  const log = card.log.slice(hidden);
  const live = card.status === 'working';
  return (
    <div className={`task-card task-card--${card.status}`} role="status" aria-live="polite">
      <div className="task-card-head">
        <span className="task-card-title">{t('task.title')} · {card.goal}</span>
        <span className={`task-card-chip task-card-chip--${card.status}`}>
          {live && <span className="status-led" />}
          {t(`task.st.${card.status}`)}
        </span>
      </div>
      {card.plan.length > 0 && (
        <>
          <div className="task-card-sec">{t('task.plan')}</div>
          <ul className="task-card-list">
            {card.plan.map((p, i) => (
              <li key={i} className={`task-row task-row--${p.state}`}>
                <span className="task-tag">
                  {p.state === 'done' ? '[ OK ]' : p.state === 'current' ? (CUR_TAG[card.status] ?? '[ .. ]') : '[    ]'}
                </span>
                <span className="task-text">{p.text}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {card.log.length > 0 && (
        <>
          <div className="task-card-sec">
            {t('task.log')}
            <span className="task-card-steps">{t('task.steps', { n: card.steps })}</span>
            {card.log.length > LOG_TAIL && (
              <button type="button" className="task-card-more" onClick={() => setOpen((v) => !v)}>
                {open ? t('task.less') : t('task.more', { n: hidden })}
              </button>
            )}
          </div>
          <ul className="task-card-list">
            {log.map((x, i) => (
              <li key={hidden + i} className={`task-row task-row--${x.ok ? 'ok' : 'fail'}`}>
                <span className="task-tag">{x.ok ? '[ OK ]' : '[FAIL]'}</span>
                <span className="task-text">{x.text}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      <div className={`task-card-line task-card-line--${card.status}`}>
        <span className="task-tag">{LINE_TAG[card.status] ?? '[ .. ]'}</span>
        <span className="task-text">
          {t(`task.line.${card.status}`)}
          {live && <span className="typing-dots" aria-hidden="true"><span>.</span><span>.</span><span>.</span></span>}
        </span>
      </div>
    </div>
  );
}
