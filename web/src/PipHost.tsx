import { useEffect } from 'react';
import { createPortal } from 'react-dom';
import { useI18n, useMockData } from './i18n';
import { usePip } from './pipStore';
import PortraitPipView from './portrait/PortraitPip';
import { primeFocusSessions } from './room/focusStore';
import RoomPipView from './room/RoomPip';

/* Хост PiP-окна (комната или живой портрет) — смонтирован в App.tsx над
   разделами, поэтому окно переживает уход из раздела, откуда его вынесли.
   Рендер — один портал в body PiP-документа (не второй React-корень):
   контексты (i18n, моки) и сторы общие с приложением, а при закрытии окна
   портал просто размонтируется.
   Заодно подгружает сессии «поработать вместе» всех персон: сессия,
   начатая до перезагрузки, завершится вовремя в любом разделе. */

export default function PipHost() {
  const { personas } = useMockData();
  const { win, kind, personaId } = usePip();

  const idsKey = personas.map((p) => p.id).join('\n');
  useEffect(() => {
    if (idsKey) primeFocusSessions(idsKey.split('\n'));
  }, [idsKey]);

  if (!win || !kind || !personaId) return null;
  const persona = personas.find((p) => p.id === personaId);
  const view = !persona ? (
    <PipMissing />
  ) : kind === 'portrait' ? (
    <PortraitPipView key={`portrait:${persona.id}`} persona={persona} doc={win.document} />
  ) : (
    <RoomPipView key={`room:${persona.id}`} persona={persona} doc={win.document} />
  );
  return createPortal(view, win.document.body);
}

// Персоны нет в текущем списке (удалена/переименована или список ещё
// моковый при недоступном бэкенде) — окно не закрываем, ждём возврата
function PipMissing() {
  const { t } = useI18n();
  return <div className="room-pip room-pip--empty">{t('room.pip.missing')}</div>;
}
