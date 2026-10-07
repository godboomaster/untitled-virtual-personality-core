import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { api, apiHost, AUTH_REQUIRED_EVENT, LINK_REVOKED_EVENT, getApiToken, getApiUrl, isLinkMode, isNativeApp, normalizeApiUrl, setApiToken, setApiUrl } from '../api';
import { getLinkConfig, pair, parsePairingUri, refusalKey, setLinkConfig } from '../link/client.ts';
import QrScanner from '../link/QrScanner';
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
   объясняет, что без бэкенда интерфейс не работает, и как его поднять.
   Ядро с API_TOKEN отвечает на список персон 401 — тогда экран спрашивает
   токен; то же, если 401 пришёл посреди работы (токен на бэке сменили).
   В приложении на телефоне ядро — на другом компьютере: экран сначала
   спрашивает его адрес и токен, а «ядро не отвечает» объясняет, что
   проверить, и даёт сменить сервер (MOCK там не нужен). Основной путь —
   QR-код с компьютера: защищённый канал VPC Link (link/client.ts), адрес и
   токен — запасной. */

const NATIVE = isNativeApp();
// Пауза между попытками достучаться до ядра; до ноутбука по сети — реже,
// чтобы не долбить посредника, пока ноутбук спит
const POLL_MS = NATIVE ? 2000 : 700;
const POLL_BG_MS = 3000; // то же после выхода в MOCK: ядро подхватится, как только встанет
// Зависший запрос не должен держать опрос; до ноутбука через сеть — дольше
const PROBE_TIMEOUT_MS = NATIVE ? 6000 : 2000;
const SHOW_DELAY_MS = 250; // быстрый старт (API уже работает) — экран не мелькает
const SKIP_AFTER_MS = 3000; // когда предложить продолжить без API
// Молчит дольше — ядро не запущено (обычный старт — секунды); удалённое
// ядро либо отвечает сразу, либо недоступно (ноутбук спит, нет сети)
const OFFLINE_AFTER_MS = NATIVE ? 10000 : 15000;
const API_START_CMD = 'python -m app.main api';
const CELLS = 36; // ячеек в LED-строке прогресса
const DONE_HOLD_MS = 450; // задержка на «всё готово», чтобы шаги успели дорисоваться

// 0 — ждём ядро, 1 — грузим персон, 2 — готово
type Stage = 0 | 1 | 2;
// Запрос токена: need — токена нет, wrong — сохранённый не подошёл
type Auth = null | 'need' | 'wrong';

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
  const [auth, setAuth] = useState<Auth>(null);
  const [tokenDraft, setTokenDraft] = useState('');
  // Форма адреса ядра (приложение на телефоне): сразу, если адрес не задан
  const [server, setServer] = useState(() => NATIVE && !getApiUrl() && !isLinkMode());
  const [urlDraft, setUrlDraft] = useState(() => getApiUrl());
  const [urlBad, setUrlBad] = useState(false);
  // Ядро ответило отказом (403 — удалённый доступ без API_TOKEN и т. п.):
  // в приложении остаёмся на экране и показываем причину
  const [denied, setDenied] = useState<string | null>(null);
  // Сопряжение по QR-коду: сканер, вставленный код, идёт рукопожатие, ошибка
  const [scanning, setScanning] = useState(false);
  const [codeDraft, setCodeDraft] = useState('');
  const [pairing, setPairing] = useState(false);
  const [pairError, setPairError] = useState<string | null>(null);
  const [run, setRun] = useState(0); // смена — запуск последовательности заново (после ввода токена)
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
    if (server) return;
    let cancelled = false;
    let timer: number | undefined;
    const startedAt = Date.now();
    setElapsed(0);
    setAttempt(1);

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
      } catch (e) {
        if (cancelled) return;
        // Ноутбук отказал каналу (телефон отвязан на компьютере) — повторять
        // бесполезно, нужен новый QR-код
        const refusal = NATIVE && isLinkMode() ? refusalKey(e) : null;
        if (refusal && refusal !== 'link.errLaptopOffline') {
          setDenied(t(refusal));
          return;
        }
        setAttempt((n) => n + 1);
        // Опрос продолжается и после «продолжить без API»: когда ядро
        // встанет, refetchPersonas переключит приложение из MOCK само
        timer = window.setTimeout(probe, openRef.current ? POLL_BG_MS : POLL_MS);
        return;
      }
      if (cancelled) return;
      setStage(1);
      // Прочие ошибки списка персон refetchPersonas гасит сам (приложение уйдёт
      // в MOCK и перечитает при навигации) — висеть на экране из-за них незачем
      const err = await refetchPersonas();
      if (cancelled) return;
      if (err?.status === 401) {
        setAuth(getApiToken() ? 'wrong' : 'need');
        return;
      }
      if (NATIVE && err) {
        setDenied(err.message || `HTTP ${err.status}`);
        return;
      }
      finish();
    };

    probe();
    const tick = window.setInterval(() => setElapsed(Date.now() - startedAt), 1000);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      window.clearInterval(tick);
    };
  }, [run, server]);

  // 401 посреди работы — назад на экран запуска с вопросом о токене
  useEffect(() => {
    const onAuthRequired = () => {
      setAuth(getApiToken() ? 'wrong' : 'need');
      setStage(1);
      setOpen(false);
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
  }, []);

  // Телефон отвязали на компьютере, пока приложение открыто — назад на
  // экран запуска с объяснением и кнопкой «Подключить заново»
  useEffect(() => {
    const onRevoked = () => {
      setDenied(t('link.errNotPaired'));
      setStage(1);
      setOpen(false);
    };
    window.addEventListener(LINK_REVOKED_EVENT, onRevoked);
    return () => window.removeEventListener(LINK_REVOKED_EVENT, onRevoked);
  }, [t]);

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

  const submitServer = () => {
    const url = normalizeApiUrl(urlDraft);
    if (!url) {
      setUrlBad(true);
      return;
    }
    setApiUrl(url);
    setUrlDraft(url);
    if (tokenDraft.trim()) setApiToken(tokenDraft.trim());
    setTokenDraft('');
    setUrlBad(false);
    setAuth(null);
    setDenied(null);
    setStage(0);
    setServer(false);
    setRun((n) => n + 1);
  };

  const pairWith = async (text: string) => {
    setScanning(false);
    const offer = parsePairingUri(text);
    if (!offer) {
      setPairError(t('link.badCode'));
      return;
    }
    setPairing(true);
    setPairError(null);
    try {
      await pair(offer, deviceName());
      setCodeDraft('');
      setAuth(null);
      setDenied(null);
      setStage(0);
      setServer(false);
      setRun((n) => n + 1);
    } catch (e) {
      const key = refusalKey(e);
      setPairError(key ? t(key) : t('link.pairFailed', { err: e instanceof Error ? e.message : String(e) }));
    } finally {
      setPairing(false);
    }
  };

  const changeServer = () => {
    // По каналу — забыть ноутбук (ключ этого телефона больше не нужен)
    if (getLinkConfig()) setLinkConfig(null);
    setUrlDraft(getApiUrl());
    setTokenDraft('');
    setAuth(null);
    setDenied(null);
    setServer(true);
  };

  const submitToken = () => {
    const token = tokenDraft.trim();
    if (!token) return;
    setApiToken(token);
    setTokenDraft('');
    setAuth(null);
    setRun((n) => n + 1);
  };

  // Ядро так и не ответило — считаем, что бэкенд не запущен (опрос идёт
  // дальше: поднимется — экран сам перейдёт к загрузке персон)
  const offline = !server && stage === 0 && elapsed >= OFFLINE_AFTER_MS;
  const stopped = offline || auth || denied || server;
  const steps: { label: string; state: 'ok' | 'run' | 'wait' | 'fail' }[] = [
    { label: t('boot.stepWeb'), state: 'ok' },
    {
      label: t('boot.stepApi', { host: apiHost() || '—' }),
      state: server ? 'wait' : stage > 0 ? 'ok' : offline ? 'fail' : 'run',
    },
    {
      label: t('boot.stepPersonas'),
      state: auth || denied ? 'fail' : stage > 1 ? 'ok' : stage === 1 ? 'run' : 'wait',
    },
  ];
  const tag = { ok: '[ OK ]', run: '[ .. ]', wait: '[ -- ]', fail: '[FAIL]' };
  // Горящие ячейки прогресса: по трети на каждый пройденный этап
  const litCells = Math.round((CELLS * (stage + 1)) / 3);

  return (
    <div className={`boot${stage === 2 ? ' boot--done' : ''}${stopped ? ' boot--offline' : ''}`}>
      <DetroitBackground />
      <div className="boot-panel bracketed" role={stopped ? 'alert' : 'status'} aria-live="polite">
        <span className="corner tl plus" />
        <span className="corner tr" />
        <span className="corner bl" />
        <span className="corner br" />

        <div className="home-eyebrow">
          {server
            ? t('boot.serverEyebrow')
            : auth
              ? t('boot.authEyebrow')
              : denied
                ? t('boot.deniedEyebrow')
                : offline
                  ? t('boot.offlineEyebrow')
                  : t('boot.eyebrow')}
        </div>
        <h1 className="home-title glitch boot-title" data-text="Virtual Persona Core">
          Virtual Persona Core
        </h1>
        <p className="boot-lead">
          {server
            ? t('boot.serverLead')
            : auth
              ? t('boot.authLead')
              : denied
                ? denied
                : stage === 2
                  ? t('boot.ready')
                  : offline
                    ? NATIVE
                      ? isLinkMode()
                        ? t(getLinkConfig()?.u ? 'link.offlineLead' : 'link.offlineLeadNoRelay', { name: getLinkConfig()?.n ?? '' })
                        : t('boot.remoteOfflineLead', { host: apiHost() })
                      : t('boot.offlineLead')
                    : t('boot.lead')}
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

        {offline && !NATIVE && (
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

        {server && (
          <div className="boot-cmd boot-link">
            <div className="boot-cmd-label">{t('link.bootLabel')}</div>
            <button type="button" className="btn btn--primary" disabled={pairing} onClick={() => setScanning(true)}>
              {pairing ? t('link.pairing') : t('link.scan')}
            </button>
            <form
              className="boot-cmd-row"
              onSubmit={(e) => {
                e.preventDefault();
                void pairWith(codeDraft);
              }}
            >
              <span className="boot-cmd-prompt">$</span>
              <input
                className="boot-token-input"
                type="text"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
                placeholder={t('link.codePh')}
                value={codeDraft}
                onChange={(e) => {
                  setCodeDraft(e.target.value);
                  setPairError(null);
                }}
              />
              <button type="submit" className="boot-cmd-copy" disabled={!codeDraft.trim() || pairing}>
                {t('link.codeSubmit')}
              </button>
            </form>
            {pairError && <p className="boot-hint">{pairError}</p>}
            <div className="boot-cmd-label boot-link-or">{t('link.orAddress')}</div>
          </div>
        )}
        {scanning && <QrScanner onResult={(text) => void pairWith(text)} onClose={() => setScanning(false)} />}

        {server && (
          <form
            className="boot-cmd boot-server"
            noValidate
            onSubmit={(e) => {
              e.preventDefault();
              submitServer();
            }}
          >
            <label className="boot-cmd-label" htmlFor="boot-url">
              {t('boot.serverLabel')}
            </label>
            <div className="boot-cmd-row">
              <span className="boot-cmd-prompt">$</span>
              <input
                id="boot-url"
                className="boot-token-input"
                type="text"
                inputMode="url"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
                placeholder="192.168.1.5:8000"
                autoFocus
                value={urlDraft}
                onChange={(e) => {
                  setUrlDraft(e.target.value);
                  setUrlBad(false);
                }}
              />
            </div>
            <label className="boot-cmd-label" htmlFor="boot-url-token">
              {t('boot.authLabel')}
            </label>
            <div className="boot-cmd-row">
              <span className="boot-cmd-prompt">$</span>
              <input
                id="boot-url-token"
                className="boot-token-input"
                type="password"
                autoComplete="current-password"
                placeholder={getApiToken() ? t('boot.serverTokenKeep') : ''}
                value={tokenDraft}
                onChange={(e) => setTokenDraft(e.target.value)}
              />
              <button type="submit" className="boot-cmd-copy" disabled={!urlDraft.trim()}>
                {t('boot.serverSubmit')}
              </button>
            </div>
            {urlBad && <p className="boot-hint">{t('boot.serverBad')}</p>}
          </form>
        )}

        {auth && (
          <form
            className="boot-cmd"
            onSubmit={(e) => {
              e.preventDefault();
              submitToken();
            }}
          >
            <label className="boot-cmd-label" htmlFor="boot-token">
              {t('boot.authLabel')}
            </label>
            <div className="boot-cmd-row">
              <span className="boot-cmd-prompt">$</span>
              <input
                id="boot-token"
                className="boot-token-input"
                type="password"
                autoComplete="current-password"
                autoFocus
                value={tokenDraft}
                onChange={(e) => setTokenDraft(e.target.value)}
              />
              <button type="submit" className="boot-cmd-copy" disabled={!tokenDraft.trim()}>
                {t('boot.authSubmit')}
              </button>
            </div>
            {auth === 'wrong' && <p className="boot-hint">{t('boot.authWrong')}</p>}
          </form>
        )}

        {NATIVE && !server && (offline || auth || denied) && (
          <div className="boot-actions">
            <button type="button" className="btn btn--ghost" onClick={changeServer}>
              {isLinkMode() ? t('link.repair') : t('boot.serverChange')}
            </button>
          </div>
        )}

        {!NATIVE && stage === 0 && elapsed >= SKIP_AFTER_MS && (
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

// Имя телефона для списка на компьютере: модель из User-Agent WebView
// («Android 15; Pixel 7 Build/…»), иначе просто «Android»
function deviceName(): string {
  const m = /Android [\d.]+; ([^;)]+?)(?: Build\/|\))/.exec(navigator.userAgent);
  return (m?.[1] ?? 'Android').trim().slice(0, 40);
}
