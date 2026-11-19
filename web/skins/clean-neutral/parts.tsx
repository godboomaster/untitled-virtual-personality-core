import { useEffect, useState } from 'react';
import PersonaAvatar from '../../src/components/PersonaAvatar';
import type { SkinBootScreenProps, SkinBrowserStateProps, SkinTaskCardProps } from '../types';

/* Свои части интерфейса «Чистого нейтрального». */

type Tone = 'sage' | 'butter' | 'rose' | 'plain';

// Цвет статуса: работает и готово — шалфей, ждёт вас — масло, сбой — роза
const TONE: Record<string, Tone> = {
  working: 'sage',
  done: 'sage',
  ask: 'butter',
  confirm: 'butter',
  paused: 'butter',
  payment: 'butter',
  cancelled: 'plain',
  stopped: 'rose',
};

// Шагов хода видно сразу; остальные — по «весь ход»
const TAIL = 3;

function Mark({ ok }: { ok: boolean }) {
  return (
    <svg className={`cn-task-mark cn-task-mark--${ok ? 'ok' : 'fail'}`} viewBox="0 0 16 16" aria-hidden="true">
      <path d={ok ? 'M3.5 8.5 6.5 11.5 12.5 4.5' : 'M4.5 4.5 11.5 11.5 M11.5 4.5 4.5 11.5'} />
    </svg>
  );
}

/* Окно задачи режима управления — как реплика персоны: цель антиквой и
   статус таблеткой, полоса шагов плана с подписями (если у задачи есть план),
   последние шаги хода галочками, фраза о том, чего персона ждёт, и мелко —
   счётчики; весь ход раскрывается по кнопке. Данные — те же, что у
   стандартной карточки (components/TaskCard). */
export function TaskCard({ card, t }: SkinTaskCardProps) {
  const [all, setAll] = useState(false);
  const tone = TONE[card.status] ?? 'plain';
  const live = card.status === 'working';
  const first = all ? 0 : Math.max(0, card.log.length - TAIL);
  const log = card.log.slice(first);
  return (
    <div className={`cn-task cn-task--${card.status}`} role="status" aria-live="polite">
      <div className="cn-task-head">
        <span className="cn-task-goal">{card.goal}</span>
        <span className={`cn-task-pill cn-task-pill--${tone}`}>
          {live && <span className="cn-task-led" aria-hidden="true" />}
          {t(`task.st.${card.status}`)}
        </span>
      </div>

      {card.plan.length > 0 && (
        <ol
          className="cn-task-plan"
          aria-label={t('task.plan')}
          style={{ gridTemplateColumns: `repeat(${card.plan.length}, minmax(0, 1fr))` }}
        >
          {card.plan.map((p, i) => (
            <li key={i} className={`cn-task-step cn-task-step--${p.state} cn-task-step--${tone}`}>
              <span className="cn-task-seg" aria-hidden="true" />
              <span className="cn-task-step-text">{p.text}</span>
            </li>
          ))}
        </ol>
      )}

      {log.length > 0 && (
        <ul className="cn-task-log">
          {log.map((x, i) => (
            <li key={first + i} className={x.ok ? undefined : 'cn-task-log-fail'}>
              <Mark ok={x.ok} />
              <span>{x.text}</span>
            </li>
          ))}
        </ul>
      )}

      <div className={`cn-task-say cn-task-say--${tone}`}>
        <span className={`cn-task-dot${live ? ' cn-task-dot--live' : ''}`} aria-hidden="true" />
        <span>{t(`task.line.${card.status}`)}</span>
      </div>

      <div className="cn-task-meta">
        <span>
          {t('task.steps', { n: card.steps })}
          {card.calls != null && ` · ${t('task.calls', { n: card.calls })}`}
        </span>
        {card.log.length > TAIL && (
          <button type="button" className="cn-task-more" aria-expanded={all} onClick={() => setAll((v) => !v)}>
            {all ? t('task.hideLog') : t('task.fullLog')}
          </button>
        )}
      </div>
    </div>
  );
}

// Как держится голова в каждом состоянии браузера
const BROWSER_MOOD: Record<string, { thinking?: boolean; asleep?: boolean; smile?: boolean }> = {
  connecting: { thinking: true },
  waiting: { thinking: true },
  no_browser: { smile: true },
  no_tab: { smile: true },
  error: { asleep: true },
  off: { asleep: true },
};

/* Экран состояния в окне браузера агента — сама персона: её голова
   (подключается — смотрит по сторонам, вкладок нет — улыбается, связи нет —
   дремлет), реплика её голосом в облачке и пояснение ниже. */
export function BrowserState({ status, persona, stale, t }: SkinBrowserStateProps) {
  const mood = BROWSER_MOOD[status] ?? {};
  return (
    <div className={`cn-bw-state${stale ? ' cn-bw-state--stale' : ''}`} role="status" aria-live="polite">
      <span className="cn-bw-head avatar">
        <PersonaAvatar persona={persona} decorative followWithin=".bw-view" {...mood} />
        {mood.asleep && (
          <span className="cn-bw-zz" aria-hidden="true">
            z<small>z</small>
          </span>
        )}
      </span>
      <span className="cn-bw-say">{t(`bw.say.${status}`, { name: persona.name })}</span>
      <span className="cn-bw-sub">{t(`bw.sub.${status}`)}</span>
    </div>
  );
}

// Строка печатается по буквам — за полторы секунды, какой бы длины ни была
function useTyping(text: string): string {
  const reduced = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const [typed, setTyped] = useState({ text: '', n: 0 });
  useEffect(() => {
    if (reduced) return;
    const step = Math.max(14, Math.min(45, 1500 / Math.max(1, text.length)));
    let n = 0;
    const id = window.setInterval(() => {
      n += 1;
      setTyped({ text, n });
      if (n >= text.length) window.clearInterval(id);
    }, step);
    return () => window.clearInterval(id);
  }, [text, reduced]);
  if (reduced) return text;
  return typed.text === text ? text.slice(0, typed.n) : '';
}

// Фазы, для которых у письма свои строки; остальные (форма адреса на
// телефоне, ядро отказало, ноутбук не отвечает) — пояснение базового экрана
const OWN_LINES = new Set(['run', 'personas', 'ready', 'auth']);

/* Экран запуска — письмо: приложение пишет вам крупной антиквой, что
   происходит; последняя строка печатается на глазах, прошлая бледнеет.
   Поля и кнопки (команда запуска, токен, адрес ядра, «Посмотреть на
   примере») — от BootGate, прямо под текстом; внизу подпись с попыткой. */
export function BootScreen({ phase, lead, attempt, elapsed, polling, native, t, children }: SkinBootScreenProps) {
  const own = OWN_LINES.has(phase) || (phase === 'offline' && !native);
  const done = own ? t(`boot.letter.${phase}.done`) : '';
  const now = own ? t(`boot.letter.${phase}.now`) : lead;
  const typed = useTyping(now);
  const alarm = phase === 'offline' || phase === 'denied';
  return (
    <div className={`cn-letter cn-letter--${phase}${native ? ' cn-letter--native' : ''}`}>
      <div className="cn-letter-text">
        {done && <p className="cn-letter-line cn-letter-line--done">{done}</p>}
        <p className={`cn-letter-line${alarm ? ' cn-letter-line--alarm' : ''}${own ? '' : ' cn-letter-line--long'}`}>
          <span className="cn-sr">{now}</span>
          <span aria-hidden="true">{typed}</span>
          <span className="cn-letter-caret" aria-hidden="true" />
        </p>
      </div>
      <div className="cn-letter-controls">{children}</div>
      <div className="cn-letter-sign">
        <i>— Virtual Persona Core</i>
        {polling && <span>{t('boot.letter.sign', { n: attempt, time: elapsed })}</span>}
      </div>
    </div>
  );
}
