import type { ReactNode } from 'react';
import InfoButton from '../components/InfoButton';
import type { HelpKey } from '../helpTexts';
import type { ServiceCheck, ServiceState } from '../api';
import { timeLabel } from './servicesText';
import type { T } from './servicesText';

/* Общие детали раздела «Сервисы»: с компьютером (Services.tsx) и на
   телефоне без него (offline/PhoneServicesView.tsx). */

export function Switch({ checked, onChange, title, disabled }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  title?: string;
  disabled?: boolean;
}) {
  return (
    <label className="switch" title={title}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="switch-slider" />
    </label>
  );
}

export function ToggleRow({ label, hint, checked, onChange, disabled }: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="svc-toggle">
      <Switch checked={checked} onChange={onChange} title={label} disabled={disabled} />
      <div>
        <div className="svc-toggle-label">{label}</div>
        {hint && <div className="field-hint" style={{ marginTop: 2 }}>{hint}</div>}
      </div>
    </div>
  );
}

export function StateLine({ state, t, locale, pending }: { state?: ServiceState; t: T; locale: string; pending?: string }) {
  if (!state) return null;
  if (state.auth_error) return <p className="svc-error">{t('services.authLost')}</p>;
  if (state.last_error) return <p className="svc-error">{t('services.lastError', { error: state.last_error })}</p>;
  if (state.last_ok) return <p className="field-hint">{t('services.lastOk', { time: timeLabel(state.last_ok, locale) })}</p>;
  return <p className="field-hint">{pending ?? t('services.notChecked')}</p>;
}

export function CheckResult({ res, t }: { res: ServiceCheck | null; t: T }) {
  if (!res) return null;
  if (res.ok) return <p className="svc-ok">{t('services.checkOk', { account: res.account ?? '' })}</p>;
  return <p className="svc-error">{res.error || t('services.checkFail')}</p>;
}

export function ServiceCard({ title, badge, children, help, className }: {
  title: string;
  badge: { text: string; on: boolean; warn?: boolean };
  children: ReactNode;
  help?: HelpKey;
  className?: string;
}) {
  return (
    <div className={'card svc-card' + (className ? ` ${className}` : '')}>
      <div className="card-title-row">
        <h3 className="card-title">
          {title}
          {help && <InfoButton helpKey={help} />}
        </h3>
        <span className={'badge' + (badge.warn ? ' svc-badge-warn' : badge.on ? ' badge--success' : ' badge--muted')}>
          {badge.text}
        </span>
      </div>
      {children}
    </div>
  );
}

export function Header({ t, subtitle }: { t: T; subtitle?: string }) {
  return (
    <div className="section-header">
      <div>
        <h1 className="section-title">{t('nav.services')}</h1>
        <p className="section-subtitle">
          {subtitle ?? t('services.subtitle')}
          <InfoButton helpKey="services.about" />
        </p>
      </div>
    </div>
  );
}
