import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { listen } from '@tauri-apps/api/event';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { api, isUp, type LogEntry, type Phase, type Snapshot } from './api';
import { duration, t, type TextKey } from './i18n';
import SettingsView from './SettingsView';

const POLL_MS = 2000;
const LOG_MAX = 300;

/* Снимок состояния раз в 2 с, пока панель видна; спрятана — не опрашивает.
   Лог дописывается по курсору /api/logs; reset — заменить ленту целиком
   (панель открылась заново, бэкенд перезапущен или упал и лог — вывод процесса) */
function useSnapshot() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const cursor = useRef(0);
  // При запуске при входе в систему окно скрыто — не опрашивать, пока не покажут
  const visible = useRef(false);
  const inFlight = useRef(false);

  const poll = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const s = await api.snapshot(cursor.current);
      cursor.current = s.logs.cursor;
      setLogs((prev) => (s.logs.reset ? s.logs.entries : [...prev, ...s.logs.entries].slice(-LOG_MAX)));
      setSnap(s);
    } catch {
      // Окно открыто раньше, чем готово состояние, — следующий тик
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    let stopped = false;
    let timer = 0;
    const loop = async () => {
      if (stopped) return;
      if (visible.current) await poll();
      timer = window.setTimeout(loop, POLL_MS);
    };
    getCurrentWindow()
      .isVisible()
      .then((v) => {
        visible.current = visible.current || v;
      })
      .finally(loop);
    const unlisten = listen<boolean>('panel-visible', (e) => {
      visible.current = e.payload;
      if (e.payload) {
        cursor.current = 0;
        poll();
      }
    });
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      unlisten.then((f) => f());
    };
  }, [poll]);

  return { snap, logs, refresh: poll };
}

type Tone = 'ok' | 'warn' | 'bad' | 'off';

function phaseTone(phase: Phase, responding: boolean): Tone {
  switch (phase) {
    case 'running':
    case 'external':
      return responding ? 'ok' : 'warn';
    case 'starting':
    case 'stopping':
      return 'warn';
    case 'crashed':
      return 'bad';
    default:
      return 'off';
  }
}

function phaseText(phase: Phase, responding: boolean): string {
  if ((phase === 'running' || phase === 'external') && !responding) return t('noAnswer');
  return t(phase as TextKey);
}

function Dot({ tone }: { tone: Tone }) {
  return <span className={`dot dot-${tone}`} aria-hidden />;
}

function Row({ label, tone, value, title }: { label: string; tone: Tone; value: string; title?: string }) {
  return (
    <div className="row" title={title}>
      <Dot tone={tone} />
      <span className="row-label">{label}</span>
      <span className="row-value">{value}</span>
    </div>
  );
}

function Header({ snap, onSettings }: { snap: Snapshot | null; onSettings: () => void }) {
  const b = snap?.backend;
  const tone = b ? phaseTone(b.phase, b.responding) : 'off';
  const status = b ? phaseText(b.phase, b.responding) : '…';
  const uptime = b?.uptimeSec != null && b.phase === 'running' ? ` · ${duration(b.uptimeSec)}` : '';
  return (
    <header className="head">
      <div className="head-title">
        <Dot tone={tone} />
        <div>
          <div className="title">Virtual Persona</div>
          <div className="subtitle">
            {status}
            {uptime}
          </div>
        </div>
      </div>
      <button className="icon-btn" onClick={onSettings} title={t('settings')} aria-label={t('settings')}>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
          <circle cx="12" cy="12" r="3" />
          <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" />
        </svg>
      </button>
    </header>
  );
}

function Controls({ snap, onAction }: { snap: Snapshot | null; onAction: (f: () => Promise<void>) => void }) {
  if (!snap) return null;
  const up = isUp(snap.backend.phase);
  const busy = snap.busy;
  const error = snap.backend.error ?? (snap.manageWeb ? snap.web.error : null);
  return (
    <section className="controls">
      <div className="btn-row">
        {up ? (
          <>
            <button className="btn" disabled={busy} onClick={() => onAction(api.restart)}>
              {t('restart')}
            </button>
            <button className="btn" disabled={busy} onClick={() => onAction(api.stop)}>
              {t('stop')}
            </button>
          </>
        ) : (
          <button className="btn btn-primary" disabled={busy} onClick={() => onAction(api.start)}>
            {t('start')}
          </button>
        )}
      </div>
      {error && <div className="error">{error}</div>}
    </section>
  );
}

function poolHText(snap: Snapshot): string {
  const h = snap.pools.h;
  if (!h.alive) return t('poolOff');
  if (h.rescue) return t('poolRescue');
  if (h.mode === 'headed') return t('poolVisible');
  return h.mode === 'headless' ? t('poolHeadless') : t('poolOpen');
}

function memTone(free: number): Tone {
  if (free < 15) return 'bad';
  if (free < 30) return 'warn';
  return 'ok';
}

function Services({ snap }: { snap: Snapshot }) {
  const { backend, web, pools, ollama, memory } = snap;
  const svcValue = (s: typeof backend) => `:${s.port} · ${phaseText(s.phase, s.responding).toLowerCase()}`;
  const pidTitle = (s: typeof backend) => (s.pid ? `pid ${s.pid}` : undefined);
  const webValue =
    !snap.manageWeb && web.phase === 'stopped' ? t('webUnmanaged') : svcValue(web);
  const vValue = pools.v.alive
    ? pools.v.idleSec != null && pools.v.idleSec >= 60
      ? t('poolIdle', { d: duration(pools.v.idleSec) })
      : t('poolOpen')
    : t('poolOff');
  const models = ollama.models.map((m) => `${m.name} · ${m.sizeGb} ${t('gb')}`).join(', ');
  return (
    <section className="block">
      <h2>{t('services')}</h2>
      <Row
        label={t('backend')}
        tone={phaseTone(backend.phase, backend.responding)}
        value={svcValue(backend)}
        title={pidTitle(backend)}
      />
      <Row label={t('web')} tone={phaseTone(web.phase, web.responding)} value={webValue} title={pidTitle(web)} />
      <Row label={t('poolH')} tone={pools.h.alive ? (pools.h.rescue ? 'warn' : 'ok') : 'off'} value={poolHText(snap)} />
      <Row label={t('poolV')} tone={pools.v.alive ? 'ok' : 'off'} value={vValue} />
      <Row
        label={t('ollama')}
        tone={ollama.up ? 'ok' : 'off'}
        value={!ollama.up ? t('ollamaOff') : models || t('ollamaIdle')}
        title={models}
      />
      <Row
        label={t('memory')}
        tone={memTone(memory.freePercent)}
        value={t('memoryLine', { free: memory.freePercent, swap: memory.swapUsedGb })}
        title={`${memory.usedGb} / ${memory.totalGb} ${t('gb')}`}
      />
      <div className="meter" aria-hidden>
        <div
          className={`meter-fill meter-${memTone(memory.freePercent)}`}
          style={{ width: `${Math.min(100, Math.max(0, 100 - memory.freePercent))}%` }}
        />
      </div>
    </section>
  );
}

function QuarantineBlock({ snap }: { snap: Snapshot }) {
  if (snap.quarantine.length === 0) return null;
  return (
    <section className="block">
      <h2>{t('quarantine')}</h2>
      {snap.quarantine.map((q) => {
        const kindKey = `kind_${q.kind}` as TextKey;
        const kind = ['challenge', 'login', 'ratelimit', 'refused'].includes(q.kind) ? t(kindKey) : q.kind;
        return (
          <Row
            key={q.site}
            label={q.site}
            tone={q.kind === 'challenge' || q.kind === 'login' ? 'bad' : 'warn'}
            value={`${kind} · ${t('left', { d: duration(q.leftSec) })}`}
            title={q.reason}
          />
        );
      })}
    </section>
  );
}

function BrowserBlock({ snap }: { snap: Snapshot }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const up = snap.backend.responding;
  const rescue = snap.pools.h.rescue;

  const run = async (finish: boolean) => {
    setBusy(true);
    setNote(finish ? null : t('browserOpening'));
    try {
      const ok = await api.rescue(finish);
      if (finish) setNote(ok ? t('browserFinished') : t('browserWaiting'));
      else setNote(ok ? t('browserOpened') : t('browserFailed'));
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="block">
      <h2>{t('browser')}</h2>
      <div className="btn-row">
        <button className="btn" disabled={!up || busy || rescue} onClick={() => run(false)}>
          {t('browserShow')}
        </button>
        {rescue && (
          <button className="btn btn-primary" disabled={busy} onClick={() => run(true)}>
            {t('browserDone')}
          </button>
        )}
      </div>
      <div className="hint">{note ?? (up ? t('browserHint') : t('browserNeedsBot'))}</div>
    </section>
  );
}

function levelClass(level: string): string {
  if (level === 'ERROR' || level === 'CRITICAL') return 'lv-error';
  if (level === 'WARNING') return 'lv-warn';
  return '';
}

function LogBlock({ snap, logs }: { snap: Snapshot; logs: LogEntry[] }) {
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  useLayoutEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [logs]);
  const source = snap.logs.source === 'api' ? t('logApi') : snap.logs.source === 'raw' ? t('logRaw') : '';
  return (
    <section className="block block-log">
      <div className="block-head">
        <h2>
          {t('log')}
          {source && <span className="h2-note"> · {source}</span>}
        </h2>
        <button className="link" onClick={() => api.openLogs()} title={snap.backend.logFile}>
          {t('logFolder')}
        </button>
      </div>
      <div
        className="log"
        ref={box}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
        }}
      >
        {logs.length === 0 && <div className="log-empty">{t('logEmpty')}</div>}
        {logs.map((e, i) => (
          <div key={`${e.seq}-${i}`} className={`log-line ${levelClass(e.level)}`}>
            {e.time && <span className="log-time">{e.time}</span>}
            <span className="log-msg">{e.msg}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

export default function App() {
  const { snap, logs, refresh } = useSnapshot();
  const [view, setView] = useState<'main' | 'settings'>('main');

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (view === 'settings') setView('main');
        else api.hidePanel();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [view]);

  const onAction = (f: () => Promise<void>) => {
    f().finally(() => window.setTimeout(refresh, 300));
  };

  if (view === 'settings') {
    return (
      <div className="panel">
        <SettingsView onBack={() => setView('main')} />
      </div>
    );
  }

  return (
    <div className="panel">
      <Header snap={snap} onSettings={() => setView('settings')} />
      <Controls snap={snap} onAction={onAction} />
      {snap && (
        <div className="scroll">
          <Services snap={snap} />
          <QuarantineBlock snap={snap} />
          <BrowserBlock snap={snap} />
          <LogBlock snap={snap} logs={logs} />
        </div>
      )}
      <footer className="foot">
        <button className="btn btn-primary" onClick={() => api.openWeb()} disabled={!snap || !isUp(snap.web.phase)}>
          {t('openWeb')} ↗
        </button>
        <button className="link" onClick={() => api.quit()} title={t('quitHint')}>
          {t('quit')}
        </button>
      </footer>
    </div>
  );
}
