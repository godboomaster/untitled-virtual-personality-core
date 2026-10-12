import { useCallback, useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import type { Persona } from '../mockData';
import { useI18n } from '../i18n';
import { usePersonaAvatars } from '../avatarStore';
import { BACK_OVERLAY, useBackHandler } from '../backStack';
import Icon from '../components/icons';

/* Узкий экран: чья комната — одна кнопка (аватар, имя, статус) и список
   снизу экрана вместо вкладок персон, которые на телефоне занимали
   полэкрана над сценой. Закрывается выбором, тапом мимо, «Назад» и Esc. */

interface RoomPersonaPickerProps {
  personas: Persona[];
  current: Persona;
  statusLine: string; // под именем на кнопке: «онлайн · читает»
  onPick: (id: string) => void;
}

export default function RoomPersonaPicker({ personas, current, statusLine, onPick }: RoomPersonaPickerProps) {
  const { t } = useI18n();
  const avatars = usePersonaAvatars();
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  useBackHandler(open, BACK_OVERLAY, close);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, close]);

  const face = (p: Persona) => (
    <span className="room-pick-face" style={p.color ? ({ '--persona': p.color } as CSSProperties) : undefined}>
      {avatars[p.id] ? <img src={avatars[p.id]} alt="" /> : p.name.charAt(0)}
    </span>
  );

  return (
    <>
      <button
        type="button"
        className="room-pick"
        aria-haspopup="dialog"
        aria-expanded={open}
        title={t('room.pickOpen')}
        onClick={() => setOpen(true)}
      >
        {face(current)}
        <span className="room-pick-text">
          <span className="room-pick-name">{current.name}</span>
          <span className="room-pick-status">{statusLine}</span>
        </span>
        <Icon name="chevronDown" size={16} />
      </button>

      {open &&
        createPortal(
          <div className="sheet-overlay" onClick={close}>
            <div className="sheet" role="dialog" aria-label={t('room.pickTitle')} onClick={(e) => e.stopPropagation()}>
              <div className="sheet-grip" aria-hidden="true" />
              <div className="sheet-title">{t('room.pickTitle')}</div>
              <div className="sheet-list">
                {personas.map((p) => (
                  <button
                    key={p.id}
                    type="button"
                    className={`sheet-row${p.id === current.id ? ' is-current' : ''}`}
                    aria-current={p.id === current.id || undefined}
                    onClick={() => {
                      onPick(p.id);
                      close();
                    }}
                  >
                    {face(p)}
                    <span className="room-pick-text">
                      <span className="room-pick-name">{p.name}</span>
                      <span className="room-pick-status">{t(`status.${p.status}`)}</span>
                    </span>
                    {p.id === current.id && (
                      <span className="sheet-check" aria-hidden="true">
                        ✓
                      </span>
                    )}
                  </button>
                ))}
              </div>
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
