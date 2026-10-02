import { memo } from 'react';
import { useI18n } from '../i18n';
import { OPEN_CHAT_EVENT } from '../notifications';
import { FOCUS_OPTIONS } from './focusStore';
import type { FocusApi } from './focusStore';

/* «Поработать вместе»: кнопки старта/остановки (шапка раздела и PiP) и
   оверлей сцены — тонкая полоса прогресса (обновляется раз в 30 с вместе с
   часами сессии) и пузырь с репликой персоны после окончания. */

// Перейти в чат персоны: тот же путь, что у клика по системному уведомлению
// (App слушает OPEN_CHAT_EVENT). Код портала PiP исполняется в главном
// окне — window здесь всегда окно приложения
function openChatWith(personaId: string) {
  try {
    window.focus();
  } catch {
    // фокус окна — необязательная часть перехода
  }
  window.dispatchEvent(new CustomEvent(OPEN_CHAT_EVENT, { detail: personaId }));
}

export const RoomFocusControls = memo(function RoomFocusControls({
  focus,
  compact = false,
}: {
  focus: FocusApi;
  compact?: boolean;
}) {
  const { t } = useI18n();
  const busy = !!focus.result?.pending;
  const cls = compact ? 'room-focus-btn' : 'btn btn--ghost room-focus-btn';
  if (focus.active) {
    return (
      <div className={`room-focus-controls ${compact ? 'room-focus-controls--compact' : ''}`}>
        {!compact && <span className="room-focus-label">{t('room.focus.left', { n: focus.remainingMin })}</span>}
        <button type="button" className={cls} title={t('room.focus.stopHint')} onClick={focus.stop}>
          {t('room.focus.stop')}
        </button>
      </div>
    );
  }
  return (
    <div className={`room-focus-controls ${compact ? 'room-focus-controls--compact' : ''}`} title={t('room.focus.hint')}>
      <span className="room-focus-label">{t('room.focus.together')}</span>
      {FOCUS_OPTIONS.map((m) => (
        <button key={m} type="button" className={cls} disabled={busy} onClick={() => focus.start(m)}>
          {t('room.focus.minutes', { n: m })}
        </button>
      ))}
    </div>
  );
});

export const RoomFocusOverlay = memo(function RoomFocusOverlay({
  focus,
  personaId,
  personaName,
}: {
  focus: FocusApi;
  personaId: string;
  personaName: string;
}) {
  const { t } = useI18n();
  const { active, progress, result, dismiss } = focus;
  if (!active && !result) return null;
  return (
    <>
      {active && (
        <div className="room-focus-bar" aria-hidden="true">
          <div className="room-focus-bar-fill" style={{ transform: `scaleX(${progress.toFixed(3)})` }} />
        </div>
      )}
      {result && (
        <div className="room-focus-bubble" role="status" onClick={(e) => e.stopPropagation()}>
          <div className="room-focus-bubble-who">{personaName}</div>
          <div className="room-focus-bubble-text">
            {result.pending ? t('room.focus.pending') : result.line || t(result.early ? 'room.focus.doneEarly' : 'room.focus.done')}
          </div>
          <div className="room-focus-bubble-actions">
            <button
              type="button"
              className="room-focus-btn"
              disabled={result.pending}
              onClick={() => {
                dismiss();
                openChatWith(personaId);
              }}
            >
              {t('room.focus.reply')}
            </button>
            <button type="button" className="room-focus-btn room-focus-btn--muted" onClick={dismiss}>
              {t('room.focus.dismiss')}
            </button>
          </div>
        </div>
      )}
    </>
  );
});
