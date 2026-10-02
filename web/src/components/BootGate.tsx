import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { api, API_HOST } from '../api';
import { refetchPersonas } from '../apiData';
import { useI18n } from '../i18n';
import { getInitialTheme } from '../useAppTheme';
import { DetroitBackground } from '../effects/DetroitBackground';

/* Экран запуска: Vite поднимается за секунду, а ядро API (импорты, прогрев
   ботов) — заметно дольше. Без гейта приложение успевало отрисоваться на
   моковых данных и оставалось в MOCK, пока пользователь не походит по
   разделам. Здесь опрашиваем /api/health, затем грузим список персон —
   и только потом монтируем приложение. Если API не запущен вовсе
   (прототипный режим) — через пару секунд доступен выход в MOCK.
   Браузер не отличит «ядро стартует» от «ядро не запущено» (порт закрыт
   в обоих случаях), поэтому долгое молчание считаем вторым: экран
   объясняет, что без бэкенда интерфейс не работает, и как его поднять. */

const POLL_MS = 700; // пауза между попытками достучаться до ядра
const POLL_BG_MS = 3000; // то же после выхода в MOCK: ядро подхватится, как только встанет
const PROBE_TIMEOUT_MS = 2000; // зависший запрос не должен держать опрос
const SHOW_DELAY_MS = 250; // быстрый старт (API уже работает) — экран не мелькает
const SKIP_AFTER_MS = 3000; // когда предложить продолжить без API
const OFFLINE_AFTER_MS = 15000; // молчит дольше — ядро не запущено (обычный старт — секунды)
const API_START_CMD = 'python -m app.main api';
const CELLS = 36; // ячеек в LED-строке прогресса
const DONE_HOLD_MS = 450; // задержка на «всё готово», чтобы шаги успели дорисоваться

// 0 — ждём ядро, 1 — грузим персон, 2 — готово
type Stage = 0 | 1 | 2;

function formatElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

export default function BootGate({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [stage, setStage] = useState<Stage>(0);
  const [attempt, setAttempt] = useState(1);
  const [elapsed, setElapsed] = useState(0);
  const [open, setOpen] = useState(false); // приложение смонтировано
  const [copied, setCopied] = useState(false);
  const openRef = useRef(false);
  useEffect(() => {
    openRef.current = open;
  }, [open]);

  // Тему выставляем сами: App ещё не смонтирован, а экран должен
  // сразу быть в выбранной теме
  useEffect(() => {
    if (!document.documentElement.hasAttribute('data-theme')) {
      document.documentElement.setAttribute('data-theme', getInitialTheme());
    }
  }, []);

  // Опрос ядра → загрузка персон → открытие приложения
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const startedAt = Date.now();

    const finish = () => {
      if (cancelled) return;
      setStage(2);
      // Экран так и не успел показаться — открываем сразу, без паузы
      const shown = Date.now() - startedAt > SHOW_DELAY_MS;
      timer = window.setTimeout(() => !cancelled && setOpen(true), shown ? DONE_HOLD_MS : 0);
    };

    const probe = async () => {
      try {
        await api.health({ signal: AbortSignal.timeout(PROBE_TIMEOUT_MS) });
      } catch {
        if (cancelled) return;
        setAttempt((n) => n + 1);
        // Опрос продолжается и после «продолжить без API»: когда ядро
        // встанет, refetchPersonas переключит приложение из MOCK само
        timer = window.setTimeout(probe, openRef.current ? POLL_BG_MS : POLL_MS);
        return;
      }
      if (cancelled) return;
      setStage(1);
      // Ошибку списка персон refetchPersonas гасит сам (приложение уйдёт в
      // MOCK и перечитает при навигации) — висеть на экране из-за неё незачем
      await refetchPersonas();
      finish();
    };

    probe();
    const tick = window.setInterval(() => setElapsed(Date.now() - startedAt), 1000);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      window.clearInterval(tick);
    };
  }, []);

  if (open) return <>{children}</>;

  const copyCmd = () => {
    navigator.clipboard
      ?.writeText(API_START_CMD)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {});
  };

  // Ядро так и не ответило — считаем, что бэкенд не запущен (опрос идёт
  // дальше: поднимется — экран сам перейдёт к загрузке персон)
  const offline = stage === 0 && elapsed >= OFFLINE_AFTER_MS;
  const steps: { label: string; state: 'ok' | 'run' | 'wait' | 'fail' }[] = [
    { label: t('boot.stepWeb'), state: 'ok' },
    { label: t('boot.stepApi', { host: API_HOST }), state: stage > 0 ? 'ok' : offline ? 'fail' : 'run' },
    { label: t('boot.stepPersonas'), state: stage > 1 ? 'ok' : stage === 1 ? 'run' : 'wait' },
  ];
  const tag = { ok: '[ OK ]', run: '[ .. ]', wait: '[ -- ]', fail: '[FAIL]' };
  // Горящие ячейки прогресса: по трети на каждый пройденный этап
  const litCells = Math.round((CELLS * (stage + 1)) / 3);

  return (
    <div className={`boot${stage === 2 ? ' boot--done' : ''}${offline ? ' boot--offline' : ''}`}>
      <DetroitBackground />
      <div className="boot-panel bracketed" role={offline ? 'alert' : 'status'} aria-live="polite">
        <span className="corner tl plus" />
        <span className="corner tr" />
        <span className="corner bl" />
        <span className="corner br" />

        <div className="home-eyebrow">{offline ? t('boot.offlineEyebrow') : t('boot.eyebrow')}</div>
        <h1 className="home-title glitch boot-title" data-text="Virtual Persona Core">
          Virtual Persona Core
        </h1>
        <p className="boot-lead">
          {stage === 2 ? t('boot.ready') : offline ? t('boot.offlineLead') : t('boot.lead')}
        </p>

        <ul className="boot-steps">
          {steps.map((s) => (
            <li key={s.label} className={`boot-step boot-step--${s.state}`}>
              <span className="boot-step-tag">{tag[s.state]}</span>
              <span className="boot-step-label">{s.label}</span>
            </li>
          ))}
        </ul>

        <div className="boot-cells" aria-hidden="true">
          {Array.from({ length: CELLS }, (_, i) => (
            <i
              key={i}
              className={i < litCells ? 'on' : undefined}
              style={i < litCells ? undefined : { animationDelay: `${(i - litCells) * 50}ms` }}
            />
          ))}
        </div>

        <div className="boot-meta">
          <span>{t('boot.attempt', { n: attempt })}</span>
          <span>T+{formatElapsed(elapsed)}</span>
        </div>

        {offline && (
          <div className="boot-cmd">
            <div className="boot-cmd-label">{t('boot.offlineCmd')}</div>
            <div className="boot-cmd-row">
              <code>
                <span className="boot-cmd-prompt">$</span> {API_START_CMD}
              </code>
              <button type="button" className="boot-cmd-copy" onClick={copyCmd}>
                {copied ? t('boot.copied') : t('boot.copy')}
              </button>
            </div>
          </div>
        )}
        {offline && <p className="boot-hint">{t('boot.offlineWait')}</p>}

        {stage === 0 && elapsed >= SKIP_AFTER_MS && (
          <div className="boot-actions">
            <button
              type="button"
              className={`btn ${offline ? 'btn--primary' : 'btn--ghost'}`}
              onClick={() => setOpen(true)}
            >
              {t('boot.skip')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
