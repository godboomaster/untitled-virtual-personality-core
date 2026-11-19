import { useEffect, useState } from 'react';
import { api } from '../api';
import { useI18n } from '../i18n';
import InitiativeBadgeMark, { BADGE_MIN_SIZE, BADGE_MAX_SIZE, initiativeBadgeSize } from './InitiativeBadgeMark';
import type { InitiativeBadge } from './portraitTypes';

const DEFAULT: InitiativeBadge = { kind: 'symbol', symbol: '!', image: null };
const SYMBOLS = ['!', '?', '★', '✦', '♥', '●'];

export default function InitiativeBadgeCard({ persona }: { persona: string }) {
  const { t } = useI18n();
  const [badge, setBadge] = useState<InitiativeBadge>(DEFAULT);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState('');
  const size = initiativeBadgeSize(badge);

  useEffect(() => {
    let alive = true;
    api.getInitiativeBadge(persona)
      .then((value) => { if (alive) setBadge(value); })
      .catch(() => { if (alive) setNotice(t('avatar.badge.loadError')); });
    return () => { alive = false; };
  }, [persona]);

  const chooseImage = (file?: File) => {
    if (!file) return;
    if (file.size > 256 * 1024) {
      setNotice(t('avatar.badge.tooLarge'));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      setBadge((current) => ({ ...current, kind: 'image', symbol: '!', image: String(reader.result) }));
      setNotice('');
    };
    reader.onerror = () => setNotice(t('avatar.badge.loadError'));
    reader.readAsDataURL(file);
  };

  const save = async () => {
    setSaving(true);
    setNotice('');
    try {
      setBadge(await api.putInitiativeBadge(persona, { ...badge, size }));
      setNotice(t('avatar.badge.saved'));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : t('avatar.badge.saveError'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card avatar-badge-settings">
      <header className="avatar-badge-header">
        <h3 className="card-title">{t('avatar.badge.title')}</h3>
        <p className="field-hint">{t('avatar.badge.hint')}</p>
      </header>
      <div className="avatar-badge-controls">
        <div className="avatar-badge-scale">
          <div className="avatar-badge-preview" style={{ width: BADGE_MAX_SIZE, height: BADGE_MAX_SIZE }}>
            <InitiativeBadgeMark badge={badge} label={t('avatar.badge.preview')} />
          </div>
          <div className="avatar-badge-size">
            <div className="field-label-row">
              <label className="field-label" htmlFor="avatar-badge-size">{t('avatar.badge.size')}</label>
              <output className="field-value" htmlFor="avatar-badge-size">{t('avatar.badge.sizeValue', { size })}</output>
            </div>
            <input id="avatar-badge-size" type="range" min={BADGE_MIN_SIZE} max={BADGE_MAX_SIZE} value={size}
              onChange={(event) => setBadge({ ...badge, size: event.target.valueAsNumber })} />
          </div>
        </div>
        <div className="avatar-badge-appearance">
          <div className="avatar-badge-symbols">
            <div className="avatar-badge-field">
              <span className="field-label" id="avatar-badge-presets">{t('avatar.badge.symbols')}</span>
              <div className="anim-chips avatar-badge-options" role="group" aria-labelledby="avatar-badge-presets">
                {SYMBOLS.map((symbol) => (
                  <button key={symbol} type="button" className="anim-chip"
                    aria-pressed={badge.kind === 'symbol' && badge.symbol === symbol}
                    onClick={() => { setBadge({ ...badge, kind: 'symbol', symbol, image: null }); setNotice(''); }}>
                    {symbol}
                  </button>
                ))}
              </div>
            </div>
            <div className="avatar-badge-field">
              <label className="field-label" htmlFor="avatar-badge-symbol">{t('avatar.badge.custom')}</label>
              <input id="avatar-badge-symbol" className="field-input avatar-badge-symbol-input" maxLength={4}
                value={badge.kind === 'symbol' ? badge.symbol : ''}
                onChange={(event) => setBadge({ ...badge, kind: 'symbol', symbol: event.target.value, image: null })} />
            </div>
          </div>
          <div className="avatar-badge-field">
            <div className="avatar-badge-upload">
              <label className="field-label" htmlFor="avatar-badge-image">{t('avatar.badge.image')}</label>
              <input id="avatar-badge-image" type="file" accept="image/png,image/jpeg,image/webp"
                aria-describedby="avatar-badge-image-hint" onChange={(event) => chooseImage(event.target.files?.[0])} />
            </div>
            <p id="avatar-badge-image-hint" className="field-hint">{t('avatar.badge.imageHint')}</p>
          </div>
        </div>
      </div>
      <div className="avatar-badge-actions">
        <button className="btn btn--primary" type="button" disabled={saving || (badge.kind === 'symbol' && !badge.symbol)} onClick={save}>
          {saving ? t('avatar.badge.saving') : t('avatar.badge.save')}
        </button>
        {notice && <p className="field-hint" role="status">{notice}</p>}
      </div>
    </div>
  );
}
