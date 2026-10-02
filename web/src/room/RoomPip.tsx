import { useEffect, useMemo, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useI18n, useMockData } from '../i18n';
import type { Persona } from '../mockData';
import { primeFocusSessions } from './focusStore';
import { RoomFocusControls, RoomFocusOverlay } from './RoomFocus';
import { closeRoomPip, useRoomPip } from './roomPipStore';
import RoomScene from './RoomScene';
import { useRoomScene, useTitleStatus } from './useRoomScene';

/* Хост PiP-окна комнаты — смонтирован в App.tsx над разделами, поэтому окно
   переживает уход из «Комнаты». Рендер — один портал в body PiP-документа
   (не второй React-корень): контексты (i18n, моки) и сторы общие с
   приложением, а при закрытии окна портал просто размонтируется.
   Заодно подгружает сессии «поработать вместе» всех персон: сессия,
   начатая до перезагрузки, завершится вовремя в любом разделе. */

export default function RoomPipHost() {
  const { personas } = useMockData();
  const { win, personaId } = useRoomPip();

  const idsKey = personas.map((p) => p.id).join('\n');
  useEffect(() => {
    if (idsKey) primeFocusSessions(idsKey.split('\n'));
  }, [idsKey]);

  if (!win || !personaId) return null;
  const persona = personas.find((p) => p.id === personaId);
  return createPortal(
    persona ? <RoomPipView key={persona.id} persona={persona} doc={win.document} /> : <RoomPipMissing />,
    win.document.body,
  );
}

// Персоны нет в текущем списке (удалена/переименована или список ещё
// моковый при недоступном бэкенде) — окно не закрываем, ждём возврата
function RoomPipMissing() {
  const { t } = useI18n();
  return <div className="room-pip room-pip--empty">{t('room.pip.missing')}</div>;
}

// Компактная комната: сцена + одна строка статуса + кнопки фокуса
function RoomPipView({ persona, doc }: { persona: Persona; doc: Document }) {
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
        <button type="button" className="room-focus-btn room-focus-btn--muted" onClick={closeRoomPip}>
          {t('room.pip.back')}
        </button>
      </div>
    </div>
  );
}
