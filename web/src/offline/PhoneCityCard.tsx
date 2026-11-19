import { useEffect, useState } from 'react';
import InfoButton from '../components/InfoButton';
import { useI18n } from '../i18n';
import { findCity, onPhoneLocationChange, phoneLocation, setPhoneLocation } from './phoneEnv';
import type { GeoMatch } from './phoneEnv';

/* «Настройки» → «Местоположение и погода» без связи с ядром: город для
   погоды в ответах без ядра (phoneEnv.ts). Город ядра, если он задан там,
   приходит в ките и важнее этого. */

export default function PhoneCityCard() {
  const { t, lang } = useI18n();
  const [cur, setCur] = useState(phoneLocation);
  const [draft, setDraft] = useState(() => phoneLocation()?.city ?? '');
  const [found, setFound] = useState<GeoMatch[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => onPhoneLocationChange(() => setCur(phoneLocation())), []);

  const search = () => {
    if (!draft.trim()) return;
    setBusy(true);
    setError(null);
    setFound(null);
    findCity(draft, lang)
      .then((list) => {
        if (list.length === 1) {
          setPhoneLocation({ city: list[0].city, lat: list[0].lat, lon: list[0].lon });
          setDraft(list[0].city);
        } else setFound(list);
      })
      .catch(() => setError(t('phoneCity.failed')))
      .finally(() => setBusy(false));
  };

  return (
    <div className="card">
      <h2 className="card-title">
        {t('settings.locationTitle')}
        <InfoButton helpKey="settings.location" />
      </h2>
      <div className="field">
        <div className="apikey-form">
          <input
            className="input"
            type="text"
            placeholder={t('phoneCity.ph')}
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && search()}
            spellCheck={false}
          />
          <button type="button" className="btn btn--primary" disabled={busy || !draft.trim()} onClick={search}>
            {busy ? '…' : t('phoneCity.find')}
          </button>
          {cur && (
            <button
              type="button"
              className="btn btn--ghost"
              disabled={busy}
              onClick={() => {
                setPhoneLocation(null);
                setDraft('');
              }}
            >
              {t('phoneCity.off')}
            </button>
          )}
        </div>
        {found && found.length > 1 && (
          <div className="boot-found" style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
            {found.map((g) => (
              <button
                key={`${g.lat},${g.lon}`}
                type="button"
                className="btn btn--ghost btn--chip"
                onClick={() => {
                  setPhoneLocation({ city: g.city, lat: g.lat, lon: g.lon });
                  setDraft(g.city);
                  setFound(null);
                }}
              >
                {g.label}
              </button>
            ))}
          </div>
        )}
        {found && found.length === 0 && <div className="field-hint">{t('phoneCity.none')}</div>}
        {error && <div className="field-hint">{t('ui.commentPrefix')}{error}</div>}
        <div className="field-hint">{cur ? t('phoneCity.current', { city: cur.city }) : t('phoneCity.hint')}</div>
      </div>
    </div>
  );
}
