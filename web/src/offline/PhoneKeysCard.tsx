import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import { offlinePersonaList } from '../apiData';
import { editProvider, keyring, onPhoneKeysChange, PHONE_CATALOG, updatePhoneProvider } from './phoneKeys';
import type { RingKey, RingProvider } from './plugin';

/* «Провайдеры» → «Ключи провайдеров»: все ключи облачных моделей на
   телефоне по провайдерам — с компьютера (ПК) и введённые здесь (тел.).
   Ключи хранит нативный плагин зашифрованными, здесь — маски.
   - Ключ телефона — добавить и удалить; при подключении к ядру телефон
     предложит перенести свои ключи туда (phoneKeys.ts, только добавить).
   - Ключ компьютера — убрать и вернуть, модель провайдера — сменить. Это
     правка поверх ключей с ядра: действует только на телефоне, на компьютер
     не уходит и переживает обновление ключей с ядра; «как на компьютере»
     её снимает.
   Без модели провайдер не отвечает (как у ядра). Ключи общие для всех
   персон; в каком порядке персона ими отвечает — в её досье. */

function displayName(id: string) {
  return PHONE_CATALOG.find((c) => c.id === id)?.name ?? id.charAt(0).toUpperCase() + id.slice(1);
}

export default function PhoneKeysCard({ standalone }: { standalone: boolean }) {
  const { t } = useI18n();
  const [ring, setRing] = useState<RingProvider[] | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [visible, setVisible] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () => {
      keyring()
        .then((r) => alive && setRing(r))
        .catch(() => alive && setRing([]));
    };
    load();
    // Ключи с ядра обновляются при связи — перечитываем
    const timer = setInterval(load, 30000);
    const off = onPhoneKeysChange(load);
    return () => {
      alive = false;
      clearInterval(timer);
      off();
    };
  }, []);

  const run = (id: string, job: () => Promise<unknown>, clearDraft = false) => {
    setBusy(id);
    setError(null);
    job()
      .then(() => {
        if (clearDraft) setDraft((d) => ({ ...d, [id]: '' }));
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setBusy(null));
  };

  if (!ring) return null;
  const byName = new Map(ring.map((r) => [r.name, r]));
  // С ключами — первыми (порядок ядра), потом остальные из каталога
  const ids = [...ring.filter((r) => r.keys.length).map((r) => r.name), ...PHONE_CATALOG.map((c) => c.id)].filter(
    (x, i, a) => a.indexOf(x) === i,
  );
  const names = new Map(offlinePersonaList().map((p) => [p.id, p.name]));

  const addKey = (id: string, r?: RingProvider) =>
    run(id, () => updatePhoneProvider(id, { addKey: draft[id] }, r?.base_url), true);

  const saveModel = (id: string, r: RingProvider | undefined, v: string) => {
    if (!r || v === r.model) return;
    const core = r.keys.some((k) => k.src === 'core');
    const phone = r.keys.some((k) => k.src === 'phone');
    run(id, async () => {
      // Модель с компьютера — своя правка поверх ("" — как там)
      if (core) await editProvider({ name: id, model: v === r.core_model ? '' : v });
      // Ключам телефона — их модель (с ней они и переедут на компьютер)
      if (phone || !core) await updatePhoneProvider(id, { model: v }, r.base_url);
    });
  };

  const removeKey = (id: string, k: RingKey, r?: RingProvider) => {
    if (k.src === 'core') run(id, () => editProvider(k.hidden ? { name: id, showKey: k.id } : { name: id, hideKey: k.id }));
    else if (k.index != null) run(id, () => updatePhoneProvider(id, { removeKey: k.index }, r?.base_url));
  };

  return (
    <div className="card">
      <h2 className="card-title">{t('keys.title')}</h2>
      <p className="field-hint" style={{ marginTop: 0 }}>
        {t(standalone ? 'keys.leadStandalone' : 'keys.lead')}
      </p>
      <ul className="memory-list">
        {ids.map((id) => {
          const r = byName.get(id);
          const keys = r?.keys ?? [];
          const live = keys.filter((k) => !k.hidden).length;
          const model = r?.model ?? '';
          const example = PHONE_CATALOG.find((c) => c.id === id)?.example;
          const personas = (r?.personas ?? []).map((p) => names.get(p) ?? p);
          return (
            <li key={id} className="apikey-row">
              <div className="apikey-main">
                <span className="provider-name">{displayName(id)}</span>
                <span className={`badge ${live ? 'badge--success' : 'badge--muted'}`}>
                  {live ? t('apikeys.keySet') : t('apikeys.keyNotSet')}
                </span>
                {live > 0 && !model && (
                  <span className="badge" title={t('apikeys.noModelHint')}>
                    {t('apikeys.noModel')}
                  </span>
                )}
                {r?.edited && <span className="badge badge--muted">{t('keys.edited')}</span>}
              </div>
              {keys.length > 0 && (
                <div className="apikey-keys">
                  {keys.map((k) => (
                    <span
                      key={`${id}-${k.id}`}
                      className="apikey-chip"
                      style={k.hidden ? { textDecoration: 'line-through', opacity: 0.55 } : undefined}
                    >
                      {k.mask} · {t(k.src === 'core' ? 'keys.srcCore' : 'keys.srcPhone')}
                      <button
                        type="button"
                        className="apikey-chip-remove"
                        title={t(k.src === 'core' ? (k.hidden ? 'keys.showCore' : 'keys.hideCore') : 'common.delete')}
                        disabled={busy !== null}
                        onClick={() => removeKey(id, k, r)}
                      >
                        {k.hidden ? '↺' : '×'}
                      </button>
                    </span>
                  ))}
                </div>
              )}
              {keys.length > 0 && (
                <div className="apikey-model">
                  <span className="apikey-model-label">{t('apikeys.model')}</span>
                  <input
                    key={`${id}:${model}`}
                    className="input pmodel-input"
                    defaultValue={model}
                    placeholder={example ? t('phoneKeys.modelExample', { model: example }) : t('dossier.modelPh')}
                    spellCheck={false}
                    autoCapitalize="off"
                    autoCorrect="off"
                    onBlur={(e) => saveModel(id, r, e.target.value.trim())}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                    }}
                  />
                </div>
              )}
              <div className="apikey-form">
                <input
                  className="input"
                  type={visible[id] ? 'text' : 'password'}
                  placeholder={t('apikeys.keyPh')}
                  value={draft[id] ?? ''}
                  onChange={(e) => setDraft((d) => ({ ...d, [id]: e.target.value }))}
                  onKeyDown={(e) => e.key === 'Enter' && (draft[id] ?? '').trim() && addKey(id, r)}
                  spellCheck={false}
                  autoCapitalize="off"
                  autoCorrect="off"
                />
                <button
                  type="button"
                  className="btn btn--icon"
                  title={visible[id] ? t('apikeys.hide') : t('apikeys.show')}
                  onClick={() => setVisible((v) => ({ ...v, [id]: !v[id] }))}
                >
                  {visible[id] ? '○' : '◉'}
                </button>
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={!(draft[id] ?? '').trim() || busy !== null}
                  onClick={() => addKey(id, r)}
                >
                  {t('common.add')}
                </button>
              </div>
              {(personas.length > 0 || r?.edited) && (
                <div className="offline-model-row">
                  {personas.length > 0 && (
                    <span className="field-hint" style={{ margin: 0 }}>
                      {personas.length <= 3
                        ? t('keys.personas', { list: personas.join(', ') })
                        : t('keys.personasN', { n: personas.length })}
                    </span>
                  )}
                  {r?.edited && (
                    <button
                      type="button"
                      className="btn btn--ghost btn--chip"
                      disabled={busy !== null}
                      onClick={() => run(id, () => editProvider({ name: id, reset: true }))}
                    >
                      {t('keys.reset')}
                    </button>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {error && <p className="field-hint">{t('ui.commentPrefix')}{error}</p>}
      <p className="field-hint">{t('phoneKeys.hint')}</p>
    </div>
  );
}
