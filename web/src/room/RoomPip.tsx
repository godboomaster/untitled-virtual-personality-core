import { useEffect, useMemo, useRef } from 'react';
import { useI18n } from '../i18n';
import type { Persona } from '../mockData';
import { closePip } from '../pipStore';
import { RoomFocusControls, RoomFocusOverlay } from './RoomFocus';
import RoomScene from './RoomScene';
import { useRoomScene, useTitleStatus } from './useRoomScene';

/* Комната в PiP-окне («Вынести комнату»); само окно и выбор, что в нём
   показать, — PipHost (web/src/PipHost.tsx). */

// Компактная комната: сцена + одна строка статуса + кнопки фокуса
export default function RoomPipView({ persona, doc }: { persona: Persona; doc: Document }) {
  const { t } = useI18n();
  const rootRef = useRef<HTMLDivElement>(null);
  const room = useRoomScene(persona, { rootRef, ownerDocument: doc, keepAlive: true, statusWithPlace: true });
  const { focus, statusText } = room;

  // Заголовок вкладки приложения и самого PiP-окна
  useTitleStatus('pip', statusText);
  useEffect(() => {
    doc.title = statusText;
  }, [doc, statusText]);

  const overlay = useMemo(
    () => <RoomFocusOverlay focus={focus} personaId={persona.id} personaName={persona.name} />,
    [focus, persona.id, persona.name],
  );

  return (
    <div className="room-pip" ref={rootRef}>
      <RoomScene {...room.sceneProps} compact ownerDocument={doc} overlay={overlay} className="room-pip-scene" />
      <div className="room-pip-bar">
        <RoomFocusControls focus={focus} compact />
        <button type="button" className="room-focus-btn room-focus-btn--muted" onClick={closePip}>
          {t('room.pip.back')}
        </button>
      </div>
    </div>
  );
}
