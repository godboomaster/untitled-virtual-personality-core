import { useEffect, useMemo, useState } from 'react';
import InfoButton from '../components/InfoButton';
import Select from '../components/Select';
import { useI18n, useMockData } from '../i18n';
import { api, ApiError, isNativeApp } from '../api';
import ConnectionCard from '../link/ConnectionCard';
import { testNativeNotification } from '../bgInbox';
import LinkSettings from '../link/LinkSettings';
import type { LocalStatus, LocationConfig, TimezoneConfig } from '../api';
import { refetchProviders, useApiOnline, useApiProviders, useApiWebchat } from '../apiData';
import { useDevMode, setDevMode } from '../devMode';
import {
  notificationsEnabled,
  notifyIconMode,
  notifyPermissionState,
  notifySoundId,
  notifyVolume,
  previewNotifySound,
  setNotificationsEnabled,
  setNotifyIconMode,
  setNotifySoundId,
  setNotifyVolume,
  testNotification,
  type NotifyIconMode,
  type NotifySoundId,
} from '../notifications';

/* Глобальные настройки: язык интерфейса и добавление API-ключей к
   провайдерам. Общие для всех персон, ничего больше
   (провайдеры/генерация — в досье). При доступном бэкенде ключи
   сохраняются в .env на сервере и переживают перезапуск. */

// Введённые ключи живут в module-state, чтобы не теряться при перемонтировании
// (только для мокового режима без бэкенда)
const savedKeys: Record<string, string> = {};

export default function ApiKeys() {
  const [keys, setKeys] = useState<Record<string, string>>({ ...savedKeys });
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [visible, setVisible] = useState<Record<string, boolean>>({});
  const { lang, setLang, hintsEnabled, setHintsEnabled, t } = useI18n();
  const [notifyOn, setNotifyOn] = useState(notificationsEnabled);
  const [notifySound, setNotifySound] = useState<NotifySoundId>(notifySoundId);
  const [notifyVol, setNotifyVol] = useState(() => Math.round(notifyVolume() * 100));
  const [notifyIcon, setNotifyIcon] = useState<NotifyIconMode>(notifyIconMode);
  const [notifyTestResult, setNotifyTestResult] = useState<string | null>(null);
  const [notifyTesting, setNotifyTesting] = useState(false);
  const runNotifyTest = () => {
    setNotifyTesting(true);
    // В приложении — уведомление Android (тот же канал, что у сообщений
    // персон); в браузере — системное уведомление браузера
    const run = isNativeApp()
      ? testNativeNotification().then((r) => t(`settings.notifyTestNative.${r}`))
      : testNotification();
    run
      .then(setNotifyTestResult)
      .catch(() => setNotifyTestResult('Не удалось проверить — см. консоль браузера (F12)'))
      .finally(() => setNotifyTesting(false));
  };
  const devMode = useDevMode();
  const { llmProviders, providerModels } = useMockData();
  const apiOnline = useApiOnline();
  const apiProviders = useApiProviders();

  // Свежая проверка локальной модели (кнопка у её строки)
  const [localStatus, setLocalStatus] = useState<LocalStatus | null>(null);
  const [localChecking, setLocalChecking] = useState(false);
  const checkLocal = () => {
    setLocalChecking(true);
    api
      .getLocalStatus()
      .then((s) => {
        setLocalStatus(s);
        refetchProviders(); // доступность могла измениться — синхронизируем кеш списка
      })
      .catch(() => {})
      .finally(() => setLocalChecking(false));
  };

  // Проба веб-чата (кнопка «Проверить» у каждого сайта): «test» в свежий чат
  const [wcTest, setWcTest] = useState<Record<string, { busy?: boolean; ok?: boolean; detail?: string }>>({});
  const testWc = (site: string) => {
    setWcTest((cur) => ({ ...cur, [site]: { busy: true } }));
    api.testWebchat(site)
      .then((r) => setWcTest((cur) => ({
        ...cur,
        [site]: {
          ok: r.ok,
          detail: r.ok
            ? `${r.latency_sec ?? '?'}с${r.preview ? ` — ${r.preview}` : ''}`
            : (r.error || '—'),
        },
      })))
      .catch(() => setWcTest((cur) => ({ ...cur, [site]: { ok: false, detail: 'API недоступен' } })));
  };

  // Единый вид списка: реальные провайдеры с бэкенда или моки
  const providers = apiOnline && apiProviders
    ? apiProviders.map((p) => ({ id: p.id, name: p.name, keySet: p.key_set, local: p.local, keys: p.keys, model: p.model }))
    : llmProviders.map((p) => ({ id: p.id, name: p.name, keySet: p.keySet, local: p.local, keys: [] as string[], model: p.model }));

  const save = (id: string) => {
    const value = (draft[id] ?? '').trim();
    if (!value) return;
    if (apiOnline) {
      // Ключ уходит на сервер (.env); поле очищаем только после подтверждения
      api
        .addProviderKey(id, value)
        .then(() => {
          setDraft((d) => ({ ...d, [id]: '' }));
          refetchProviders();
        })
        .catch(() => {});
      return;
    }
    savedKeys[id] = value;
    setKeys({ ...savedKeys });
    setDraft((d) => ({ ...d, [id]: '' }));
  };

  const removeKey = (id: string, index: number) => {
    if (!apiOnline) return;
    api
      .deleteProviderKey(id, index)
      .then(() => refetchProviders())
      .catch(() => {});
  };

  // Местоположение и погода: off/manual/geo; сохранённое — с бэкенда
  const [loc, setLoc] = useState<LocationConfig | null>(null);
  const [locMode, setLocMode] = useState<'off' | 'manual' | 'geo'>('off');
  const [cityDraft, setCityDraft] = useState('');
  const [envLine, setEnvLine] = useState<string | null>(null);
  const [geoBusy, setGeoBusy] = useState(false);
  const [geoError, setGeoError] = useState(false);

  // Часовой пояс пользователя (TIMEZONE, см. app/core/timeutil) — глобальная
  // настройка, как местоположение: от неё зависят время напоминаний, ритм
  // и суточные лимиты инициатив у всех персон.
  const [tz, setTz] = useState<TimezoneConfig | null>(null);
  const [tzDraft, setTzDraft] = useState('');
  const [tzSaving, setTzSaving] = useState(false);
  const [tzError, setTzError] = useState<string | null>(null);
  // Список зон — из Intl, если браузер его отдаёт; иначе просто текстовое
  // поле (валидацию всё равно делает бэкенд через zoneinfo.ZoneInfo)
  const tzOptions = useMemo<string[]>(() => {
    try {
      return typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf('timeZone') : [];
    } catch {
      return [];
    }
  }, []);

  // Веб-чаты как провайдеры без API-ключей: включённые сайты (порядок
  // перебора = порядок выбора) и доступные варианты — общий кеш с бэкенда
  const webchat = useApiWebchat();
  const webchatSites = webchat?.sites ?? [];
  const webchatOptions = webchat?.options ?? [];

  const toggleWebchat = (site: string, on: boolean) => {
    const next = on ? [...webchatSites, site] : webchatSites.filter((s) => s !== site);
    api
      .setWebchat(next)
      .then(() => refetchProviders())
      .catch(() => {});
  };

  useEffect(() => {
    if (!apiOnline) return;
    api.getLocation().then((c) => {
      setLoc(c);
      setLocMode(c.mode);
      if (c.city) setCityDraft(c.city);
    }).catch(() => {});
    api.getEnvPreview().then((r) => setEnvLine(r.line)).catch(() => {});
  }, [apiOnline]);

  const applyLocation = (cfg: LocationConfig) => {
    api
      .setLocation(cfg)
      .then((saved) => {
        setLoc(saved);
        setGeoError(false);
        // Превью строки, которая уйдёт персонам в контекст
        api.getEnvPreview().then((r) => setEnvLine(r.line)).catch(() => {});
      })
      .catch(() => {});
  };

  useEffect(() => {
    if (!apiOnline) return;
    api.getTimezone().then((c) => {
      setTz(c);
      setTzDraft(c.timezone);
    }).catch(() => {});
  }, [apiOnline]);

  const applyTimezone = (value: string) => {
    setTzSaving(true);
    setTzError(null);
    api
      .setTimezone(value)
      .then((saved) => {
        setTz(saved);
        setTzDraft(saved.timezone);
      })
      .catch((e) => setTzError(e instanceof ApiError ? e.message : String(e)))
      .finally(() => setTzSaving(false));
  };

  const detectGeo = () => {
    if (!navigator.geolocation) {
      setGeoError(true);
      return;
    }
    setGeoBusy(true);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setGeoBusy(false);
        applyLocation({ mode: 'geo', lat: pos.coords.latitude, lon: pos.coords.longitude });
      },
      () => {
        setGeoBusy(false);
        setGeoError(true);
      },
      { timeout: 10000 },
    );
  };

  return (
    <div className="section">
      <div className="section-header">
        <div>
          <h1 className="section-title">{t('nav.settings')}</h1>
          <p className="section-subtitle">
            {t('apikeys.subtitle')}
            <InfoButton helpKey="settings.keyStatus" />
          </p>
        </div>
      </div>

      {/* Телефон: к какому ноутбуку подключено и как. Компьютер: какие
          телефоны подключены (VPC Link) и QR-код для нового */}
      {isNativeApp() ? <ConnectionCard /> : <LinkSettings />}

      {/* Общие настройки: язык интерфейса, подсказки */}
      <div className="card">
        <h2 className="card-title">{t('settings.general')}</h2>
        <div className="field">
          <label className="field-label">{t('settings.language')}</label>
          <div>
            <div className="theme-toggle" role="button" aria-label={t('settings.languageToggle')}>
              <span className={lang === 'ru' ? 'active' : ''} onClick={() => setLang('ru')}>
                RU
              </span>
              <span className={lang === 'en' ? 'active' : ''} onClick={() => setLang('en')}>
                EN
              </span>
            </div>
          </div>
        </div>
        <div className="field">
          <label className="field-label">{t('settings.notifications')}</label>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
            <label className="switch" title={t('settings.notificationsToggle')}>
              <input
                type="checkbox"
                checked={notifyOn}
                onChange={(e) => {
                  setNotifyOn(e.target.checked);
                  setNotificationsEnabled(e.target.checked);
                }}
              />
              <span className="switch-slider" />
            </label>
            <button type="button" className="btn btn--ghost" onClick={runNotifyTest} disabled={notifyTesting}>
              {t('settings.notificationsTest')}
            </button>
          </div>
          <div className="field-grid" style={{ marginTop: 12 }}>
            <div>
              <label className="field-label" htmlFor="notify-sound">{t('settings.notifySound')}</label>
              <Select
                id="notify-sound"
                value={notifySound}
                options={(['ding', 'pop', 'chime', 'bell', 'none'] as const).map((id) => ({
                  value: id,
                  label: t(`settings.notifySound.${id}`),
                }))}
                onChange={(v) => {
                  const id = v as NotifySoundId;
                  setNotifySound(id);
                  setNotifySoundId(id);
                  previewNotifySound(); // смена звука — сразу послушать его
                }}
              />
            </div>
            <div>
              <label className="field-label" htmlFor="notify-icon">{t('settings.notifyIcon')}</label>
              <Select
                id="notify-icon"
                value={notifyIcon}
                options={[
                  { value: 'persona', label: t('settings.notifyIcon.persona') },
                  { value: 'app', label: t('settings.notifyIcon.app') },
                ]}
                onChange={(v) => {
                  const mode = v as NotifyIconMode;
                  setNotifyIcon(mode);
                  setNotifyIconMode(mode);
                }}
              />
            </div>
          </div>
          <div style={{ marginTop: 12 }}>
            <label className="field-label" htmlFor="notify-volume">
              {t('settings.notifyVolume')}: {notifyVol}%
            </label>
            <input
              id="notify-volume"
              type="range"
              min={0}
              max={100}
              step={5}
              value={notifyVol}
              onChange={(e) => {
                const v = Number(e.target.value);
                setNotifyVol(v);
                setNotifyVolume(v / 100);
              }}
              onPointerUp={previewNotifySound}
              onKeyUp={previewNotifySound}
              style={{ width: '100%', accentColor: 'var(--accent)' }}
            />
          </div>
          {/* В приложении Notification API нет — подсказка про браузер ни к чему:
              в шторку пишет фоновая служба («Подключение» выше) */}
          <p className="field-hint">
            {notifyTestResult ??
              (isNativeApp() && notifyPermissionState() === 'unsupported'
                ? t('settings.notifyState.app')
                : t(`settings.notifyState.${notifyPermissionState()}`))}
          </p>
        </div>
        <div className="field">
          <label className="field-label">{t('settings.hints')}</label>
          <div>
            <label className="switch" title={t('settings.hintsToggle')}>
              <input
                type="checkbox"
                checked={hintsEnabled}
                onChange={(e) => setHintsEnabled(e.target.checked)}
              />
              <span className="switch-slider" />
            </label>
          </div>
        </div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label className="field-label">{t('settings.devMode')}</label>
          <div>
            <label className="switch" title={t('settings.devModeToggle')}>
              <input
                type="checkbox"
                checked={devMode}
                onChange={(e) => setDevMode(e.target.checked)}
              />
              <span className="switch-slider" />
            </label>
          </div>
        </div>
      </div>

      {/* Местоположение и погода: строка окружения в контекст персон */}
      {apiOnline && (
        <div className="card">
          <h2 className="card-title">
            {t('settings.locationTitle')}
            <InfoButton helpKey="settings.location" />
          </h2>
          <div className="field">
            <div className="theme-toggle">
              <span
                className={locMode === 'off' ? 'active' : ''}
                onClick={() => {
                  setLocMode('off');
                  applyLocation({ mode: 'off' });
                  setEnvLine(null);
                }}
              >
                {t('settings.locationOff')}
              </span>
              <span
                className={locMode === 'manual' ? 'active' : ''}
                onClick={() => setLocMode('manual')}
              >
                {t('settings.locationManual')}
              </span>
              <span
                className={locMode === 'geo' ? 'active' : ''}
                onClick={() => setLocMode('geo')}
              >
                {t('settings.locationGeo')}
              </span>
            </div>
          </div>
          {locMode === 'manual' && (
            <div className="field">
              <div className="apikey-form">
                <input
                  className="input"
                  type="text"
                  placeholder={t('settings.locationCityPh')}
                  value={cityDraft}
                  onChange={(e) => setCityDraft(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && cityDraft.trim() && applyLocation({ mode: 'manual', city: cityDraft.trim() })}
                  spellCheck={false}
                />
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={!cityDraft.trim()}
                  onClick={() => applyLocation({ mode: 'manual', city: cityDraft.trim() })}
                >
                  {t('common.save')}
                </button>
              </div>
            </div>
          )}
          {locMode === 'geo' && (
            <div className="field">
              <button
                type="button"
                className="btn btn--ghost"
                disabled={geoBusy}
                onClick={detectGeo}
              >
                {geoBusy ? '…' : t('settings.locationDetect')}
              </button>
              {geoError && <div className="field-hint">// {t('settings.locationGeoError')}</div>}
            </div>
          )}
          {locMode !== 'off' && loc?.city && loc.mode === locMode && (
            <div className="field-hint">// {t('settings.locationSaved', { city: loc.city })}</div>
          )}
          {locMode !== 'off' && envLine && (
            <div className="field-hint">// {t('settings.locationPreview', { line: envLine })}</div>
          )}
        </div>
      )}

      {/* Часовой пояс: глобальная настройка (TIMEZONE в .env), как местоположение */}
      {apiOnline && (
        <div className="card">
          <h2 className="card-title">
            {t('settings.timezoneTitle')}
            <InfoButton helpKey="settings.timezone" />
          </h2>
          <div className="field">
            <label className="field-label">{t('settings.timezoneLabel')}</label>
            {tzOptions.length > 0 ? (
              <Select
                value={tzDraft}
                disabled={tzSaving}
                options={[
                  { value: '', label: t('settings.timezoneAuto') },
                  ...tzOptions.map((z) => ({ value: z, label: z })),
                ]}
                onChange={applyTimezone}
              />
            ) : (
              <div className="apikey-form">
                <input
                  className="input"
                  type="text"
                  placeholder={t('settings.timezoneCityPh')}
                  value={tzDraft}
                  disabled={tzSaving}
                  onChange={(e) => setTzDraft(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && applyTimezone(tzDraft.trim())}
                  spellCheck={false}
                />
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={tzSaving}
                  onClick={() => applyTimezone(tzDraft.trim())}
                >
                  {t('common.save')}
                </button>
                {tz?.source === 'env' && (
                  <button type="button" className="btn btn--ghost" disabled={tzSaving} onClick={() => applyTimezone('')}>
                    {t('settings.timezoneReset')}
                  </button>
                )}
              </div>
            )}
          </div>
          {tzSaving && <div className="field-hint">{t('settings.timezoneSaving')}</div>}
          {!tzSaving && tz && (
            <div className="field-hint">
              {tz.source === 'env'
                ? t('settings.timezoneSourceEnv', { tz: tz.effective })
                : t('settings.timezoneSourceSystem', { tz: tz.effective })}
            </div>
          )}
          {tzError && <div className="field-hint">// {tzError}</div>}
        </div>
      )}

      <div className="card">
        <h2 className="card-title">{t('apikeys.cardTitle')}</h2>
        <ul className="memory-list">
          {providers.map((p) => {
            const hasKey = Boolean(keys[p.id]) || p.keySet;
            // Строка локальной модели при живом бэкенде: статус доступности вместо формы ключа
            const localRow = p.local && apiOnline;
            const localAvailable = localStatus ? localStatus.available : p.keySet;
            return (
              <li key={p.id} className="apikey-row">
                <div className="apikey-main">
                  <span className="provider-name">{p.name}</span>
                  {localRow && p.model && <span className="provider-model">{p.model}</span>}
                  {p.local ? (
                    localRow ? (
                      <span className={`badge ${localAvailable ? 'badge--success' : 'badge--muted'}`}>
                        {localAvailable ? t('settings.localAvailable') : t('settings.localUnavailable')}
                      </span>
                    ) : (
                      /* У локальной модели ключа нет */
                      <span className="badge">{t('apikeys.noKey')}</span>
                    )
                  ) : (
                    <span className={`badge ${hasKey ? 'badge--success' : 'badge--muted'}`}>
                      {hasKey ? t('apikeys.keySet') : t('apikeys.keyNotSet')}
                    </span>
                  )}
                  {/* Моделей по умолчанию нет: без неё провайдер не используется */}
                  {!p.local && hasKey && !p.model && (
                    <span className="badge" title={t('apikeys.noModelHint')}>{t('apikeys.noModel')}</span>
                  )}
                </div>
                {localRow ? (
                  <div className="apikey-form">
                    <button
                      type="button"
                      className="btn btn--ghost"
                      disabled={localChecking}
                      onClick={checkLocal}
                    >
                      {localChecking ? '…' : t('settings.checkAvailability')}
                    </button>
                  </div>
                ) : (
                  <div className="apikey-form">
                    <input
                      className="input"
                      type={visible[p.id] ? 'text' : 'password'}
                      placeholder={p.local ? 'http://localhost:11434' : t('apikeys.keyPh')}
                      value={draft[p.id] ?? ''}
                      onChange={(e) => setDraft((d) => ({ ...d, [p.id]: e.target.value }))}
                      onKeyDown={(e) => e.key === 'Enter' && save(p.id)}
                      spellCheck={false}
                    />
                    <button
                      type="button"
                      className="btn btn--icon"
                      title={visible[p.id] ? t('apikeys.hide') : t('apikeys.show')}
                      onClick={() => setVisible((v) => ({ ...v, [p.id]: !v[p.id] }))}
                    >
                      {visible[p.id] ? '○' : '◉'}
                    </button>
                    <button
                      type="button"
                      className="btn btn--primary"
                      disabled={!(draft[p.id] ?? '').trim()}
                      onClick={() => save(p.id)}
                    >
                      {t('common.add')}
                    </button>
                  </div>
                )}
                {/* Модель провайдера — общая для всех персон (у персоны может быть
                    своя: досье → «Модели провайдеров»). Ollama — тоже здесь */}
                {apiOnline && (
                  <div className="apikey-model">
                    <span className="apikey-model-label">{t('apikeys.model')}</span>
                    <input
                      key={`${p.id}:${p.model}`}
                      className="input pmodel-input"
                      list={`apikey-models-${p.id}`}
                      defaultValue={p.model}
                      placeholder={p.local ? 'gemma4:e2b' : t('dossier.modelPh')}
                      spellCheck={false}
                      onBlur={(e) => {
                        const v = e.target.value.trim();
                        if (v && v !== p.model) {
                          api.setProviderModel(p.id, v).then(refetchProviders).catch(() => {});
                        } else {
                          e.target.value = p.model ?? '';
                        }
                      }}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                      }}
                    />
                    <datalist id={`apikey-models-${p.id}`}>
                      {(providerModels[p.id] ?? []).map((m) => (
                        <option key={m} value={m} />
                      ))}
                    </datalist>
                  </div>
                )}
                {/* Детали неудачной проверки локальной модели: что не так и как исправить */}
                {localRow && localStatus && !localStatus.available && (
                  <div className="field-hint provider-local-detail">
                    {!localStatus.server
                      ? t('settings.localServerDown', { url: localStatus.url })
                      : localStatus.model
                        ? t('settings.localModelMissing', { model: localStatus.model })
                        : t('settings.localModelNotSet')}
                  </div>
                )}
                {/* Уже сохранённые ключи (маскированы), чтобы было видно, что задано */}
                {p.keys.length > 0 && (
                  <div className="apikey-keys">
                    {p.keys.map((masked, i) => (
                      <span key={`${p.id}-${i}`} className="apikey-chip">
                        {masked}
                        <button
                          type="button"
                          className="apikey-chip-remove"
                          title={t('common.delete')}
                          onClick={() => removeKey(p.id, i)}
                        >
                          ×
                        </button>
                      </span>
                    ))}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </div>

      {/* Веб-чаты как провайдеры без API-ключей: чаты в общем окне браузера бота.
          Галочками — несколько; порядок выбора = порядок перебора (номер на бейдже) */}
      {apiOnline && webchatOptions.length > 0 && (
        <div className="card">
          <h2 className="card-title">{t('settings.webchatTitle')}</h2>
          <div className="features-grid">
            {webchatOptions.map((opt) => {
              const idx = webchatSites.indexOf(opt);
              const tst = wcTest[opt];
              return (
                <div key={opt} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <label className="checkbox-row" style={{ flex: 'none' }}>
                    <input
                      type="checkbox"
                      checked={idx >= 0}
                      onChange={(e) => toggleWebchat(opt, e.target.checked)}
                    />
                    <span>{opt}</span>
                    {idx >= 0 && <span className="badge badge--active">{idx + 1}</span>}
                  </label>
                  <button
                    className="btn btn--ghost"
                    disabled={tst?.busy}
                    title={(!tst || tst.busy ? '' : tst.detail) || t('settings.testWebchatHint')}
                    onClick={() => testWc(opt)}
                  >
                    {tst?.busy ? '…' : t('settings.testWebchat')}
                  </button>
                  {tst && !tst.busy && (
                    <span
                      className={`badge ${tst.ok ? 'badge--success' : 'badge--muted'}`}
                      title={tst.detail || ''}
                    >
                      {tst.ok ? '✓' : `✗ ${tst.detail || ''}`}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
          <div className="field-hint">// {t('settings.webchatHint')}</div>
        </div>
      )}
    </div>
  );
}
