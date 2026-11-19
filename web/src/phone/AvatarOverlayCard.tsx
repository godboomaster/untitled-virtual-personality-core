import { useCallback, useEffect, useRef, useState } from 'react';
import type { Persona } from '../mockData';
import { useI18n } from '../i18n';
import { openAppInfo } from './control';
import {
  OVERLAY_SIZE_DEFAULT,
  avatarOverlayStatus,
  openGazeSettings,
  openOverlaySettings,
  setAvatarPassThrough,
  startAvatarOverlay,
  stopAvatarOverlay,
} from './avatarOverlay';
import type { AvatarOverlayStatus } from './avatarOverlay';

/* «Аватар» → «Поверх приложений» (только телефон): голова персоны в
   маленьком окне над любым приложением. Здесь — что для этого нужно (право
   «Поверх других приложений», служба «куда вы нажимаете», листы головы),
   размер окна (на экране — щипком), «сквозь касания» и «Показать» /
   «Убрать». Убрать и «сквозь касания» — ещё и из уведомления. */

type Mark = 'ok' | 'fail' | 'warn';

export default function AvatarOverlayCard({ persona, personas }: { persona: Persona; personas: Persona[] }) {
  const { t } = useI18n();
  const [st, setSt] = useState<AvatarOverlayStatus | null | undefined>(undefined);
  const [size, setSize] = useState<number>(OVERLAY_SIZE_DEFAULT);
  // Ползунок тянут — опрос состояния его не перебивает
  const sliding = useRef(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(() => {
    void avatarOverlayStatus(persona.id).then((s) => {
      setSt(s);
      // Размер мог смениться щипком на экране
      if (s?.on && !sliding.current) setSize(s.size);
    });
  }, [persona.id]);

  useEffect(() => {
    refresh();
    // Вернулись из настроек Android — право или служба могли включиться
    const onVisible = () => {
      if (document.visibilityState === 'visible') refresh();
    };
    document.addEventListener('visibilitychange', onVisible);
    const timer = window.setInterval(refresh, 4000);
    return () => {
      document.removeEventListener('visibilitychange', onVisible);
      window.clearInterval(timer);
    };
  }, [refresh]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      const code = (e as { code?: string }).code;
      setErr(code ? t(`avatarOverlay.err.${code}`, { name: persona.name }) : String(e));
    } finally {
      setBusy(false);
      window.setTimeout(refresh, 400);
    }
  };

  if (st === undefined) return null;
  if (st === null) {
    return (
      <div className="card">
        <h3 className="card-title">{t('avatarOverlay.title')}</h3>
        <p className="field-hint">{t('avatarOverlay.pluginMissing')}</p>
      </div>
    );
  }

  const shownPersona = personas.find((p) => p.id === st.persona);
  const mine = st.running && st.persona === persona.id;
  const rows: { key: string; mark: Mark; text: string; action?: { label: string; onClick: () => void } }[] = [
    st.canDraw
      ? { key: 'draw', mark: 'ok', text: t('avatarOverlay.drawOn') }
      : {
          key: 'draw',
          mark: 'fail',
          text: t('avatarOverlay.drawOff'),
          action: { label: t('avatarOverlay.open'), onClick: () => void openOverlaySettings().catch(() => {}) },
        },
    st.gazeEnabled
      ? { key: 'gaze', mark: st.gazeRunning ? 'ok' : 'warn', text: t(st.gazeRunning ? 'avatarOverlay.gazeOn' : 'avatarOverlay.gazeStarting') }
      : {
          key: 'gaze',
          mark: 'warn',
          text: t('avatarOverlay.gazeOff'),
          action: { label: t('avatarOverlay.openA11y'), onClick: () => void openGazeSettings().catch(() => {}) },
        },
    st.hasSheets
      ? { key: 'sheets', mark: 'ok', text: t('avatarOverlay.sheetsOn') }
      : { key: 'sheets', mark: 'fail', text: t('avatarOverlay.sheetsOff', { name: persona.name }) },
  ];

  return (
    <div className="card">
      <div className="card-title-row">
        <h3 className="card-title">{t('avatarOverlay.title')}</h3>
        <span className="badge hud-code">AVA_04</span>
      </div>
      <p className="field-hint">{t('avatarOverlay.lead', { name: persona.name })}</p>

      <ul className="offline-check">
        {rows.map((r) => (
          <li key={r.key} className={`offline-check-item offline-check-item--${r.mark}`}>
            <span className="offline-check-mark" aria-hidden="true">
              {r.mark === 'ok' ? '✓' : r.mark === 'fail' ? '✗' : '!'}
            </span>
            <span>
              <b>{t(`avatarOverlay.step.${r.key}`)}:</b> {r.text}{' '}
              {r.action && (
                <button type="button" className="btn btn--ghost btn--chip" onClick={r.action.onClick}>
                  {r.action.label}
                </button>
              )}
            </span>
          </li>
        ))}
      </ul>
      {!st.gazeEnabled && (
        <p className="field-hint">
          {t('avatarOverlay.restricted')}{' '}
          <button type="button" className="btn btn--ghost btn--chip" onClick={() => void openAppInfo().catch(() => {})}>
            {t('phoneCtl.appInfo')}
          </button>
        </p>
      )}

      <div className="field">
        <label className="field-label" htmlFor="avatar-overlay-size">
          {t('avatarOverlay.size')}: {t('avatarOverlay.sizeDp', { n: size })}
        </label>
        <input
          id="avatar-overlay-size"
          type="range"
          min={st.sizeMin || 64}
          max={st.sizeMax || 220}
          step={4}
          value={size}
          onPointerDown={() => (sliding.current = true)}
          onChange={(e) => setSize(Number(e.target.value))}
          onPointerUp={() => {
            sliding.current = false;
            // Уже на экране — сразу новым размером (центр головы на месте)
            if (mine) void run(() => startAvatarOverlay(persona.id, persona.name, size));
          }}
          onKeyUp={() => {
            if (mine) void run(() => startAvatarOverlay(persona.id, persona.name, size));
          }}
          style={{ width: '100%', accentColor: 'var(--accent)' }}
        />
        <p className="field-hint">{t('avatarOverlay.sizeHint')}</p>
      </div>

      <div className="field">
        <label className="field-label">{t('avatarOverlay.pass')}</label>
        <div className="offline-model-row" style={{ alignItems: 'center' }}>
          <label className="switch" title={t('avatarOverlay.pass')}>
            <input
              type="checkbox"
              checked={st.passThrough}
              disabled={busy}
              onChange={(e) => {
                const on = e.target.checked;
                setSt({ ...st, passThrough: on });
                void run(() => setAvatarPassThrough(on));
              }}
            />
            <span className="switch-slider" />
          </label>
        </div>
        <p className="field-hint">
          {t('avatarOverlay.passHint')}
          {st.running && st.keyboard && !st.passThrough && <> {t('avatarOverlay.passKeyboardNow')}</>}
        </p>
      </div>

      <div className="field">
        <div className="offline-model-row">
          {mine ? (
            <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => void run(stopAvatarOverlay)}>
              {t('avatarOverlay.hide')}
            </button>
          ) : (
            <button
              type="button"
              className="btn btn--primary"
              disabled={busy || !st.canDraw || !st.hasSheets}
              onClick={() => void run(() => startAvatarOverlay(persona.id, persona.name, size))}
            >
              {st.running && shownPersona
                ? t('avatarOverlay.showInstead', { name: persona.name, other: shownPersona.name })
                : t('avatarOverlay.show', { name: persona.name })}
            </button>
          )}
          {st.running && !mine && (
            <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => void run(stopAvatarOverlay)}>
              {t('avatarOverlay.hide')}
            </button>
          )}
        </div>
        {(err || (st.error && !st.running)) && (
          <p className="field-hint">{err ?? t(`avatarOverlay.err.${st.error}`, { name: persona.name })}</p>
        )}
      </div>

      <dl className="avatar-desk-list">
        {(['pet', 'write', 'chat', 'move', 'size', 'hide'] as const).map((k) => (
          <div key={k} className="avatar-desk-row">
            <dt>{t(`avatarOverlay.tip.${k}T`)}</dt>
            <dd>{t(`avatarOverlay.tip.${k}`)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
