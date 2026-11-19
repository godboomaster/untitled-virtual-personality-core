import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../api';
import type { DesktopPortraitStatus } from '../api';
import { useI18n } from '../i18n';

// Нативный аватар с движением и перетаскиванием — окно desktop/, как в трее.
export default function DesktopAvatarButton({ personaId }: { personaId: string }) {
  const { t } = useI18n();
  const [status, setStatus] = useState<DesktopPortraitStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    try {
      setStatus(await api.getDesktopPortrait());
    } catch {
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh();
    }, 2000);
    const visible = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [refresh]);

  useEffect(() => setError(''), [personaId]);
  const shown = !!status?.connected && status.shown && status.persona === personaId;
  const toggle = async () => {
    setBusy(true);
    setError('');
    try {
      setStatus(await api.setDesktopPortrait(personaId, !shown));
    } catch (e) {
      const unavailable = e instanceof ApiError && (e.status === 404 || e.message === 'desktop_unavailable');
      setError(unavailable ? t('chat.avatarUnavailable') : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button
        type="button"
        className={`btn ${shown ? 'btn--primary' : 'btn--ghost'}`}
        title={t(shown ? 'chat.avatarHideHint' : 'chat.avatarShowHint')}
        aria-pressed={shown}
        disabled={busy || !!status?.pending}
        onClick={() => void toggle()}
      >
        {t(busy || status?.pending ? 'chat.avatarPending' : shown ? 'chat.avatarHide' : 'chat.avatarShow')}
      </button>
      {error && <div className="ctx-note" role="alert">{error}</div>}
    </>
  );
}
