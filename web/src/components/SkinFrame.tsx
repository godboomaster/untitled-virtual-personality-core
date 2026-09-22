/* SkinFrame — хост-обёртка скина: рендерит файл скина в sandboxed
   iframe (opaque origin, без доступа к приложению, localStorage и сети)
   и обменивается с ним сообщениями по узкому протоколу:

     хост → скин:  { vpc:'host', type:'state', payload }   снапшот данных
     скин → хост:  { vpc:'skin', type:'ready' }            скин загрузился
                   { vpc:'skin', type:'send', text }       отправка сообщения
                   { vpc:'skin', type:'clear' }            очистка диалога
                   { vpc:'skin', type:'zoom-image', src }  клик по картинке —
                                                          хост открывает лайтбокс
                   { vpc:'skin', type:'error', message }   runtime-ошибка скина */

import { useEffect, useMemo, useRef } from 'react';
import { prepareSkin } from '../skins/engine';
import type { SkinScreen } from '../skins/engine';
import type { SkinStatePayload } from '../skins/payloads';
import { openImageZoom } from '../imageZoomStore';

interface SkinFrameProps {
  skin: string; // исходный файл скина (до prepareSkin)
  screen: SkinScreen;
  state: SkinStatePayload;
  onSend?: (text: string, image?: string) => void;
  onClear?: () => void;
  onSelectPersona?: (id: string) => void;
  onOpenDossier?: () => void;
  onCloseDossier?: () => void;
  // Действия записи из скина: кнопки [data-vpc-action] и [data-vpc-item-action]
  onAction?: (action: string, values: Record<string, string>, id: string | null) => void;
  // Инпуты настроек [data-vpc-setting]
  onSetSetting?: (key: string, value: string) => void;
  onError?: (message: string) => void;
  className?: string;
  title?: string;
}

export default function SkinFrame({
  skin,
  screen,
  state,
  onSend,
  onClear,
  onSelectPersona,
  onOpenDossier,
  onCloseDossier,
  onAction,
  onSetSetting,
  onError,
  className,
  title,
}: SkinFrameProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  // Последний снапшот — чтобы отправить его по сигналу ready
  const stateRef = useRef(state);
  stateRef.current = state;

  // Полный документ скина: CSP + bridge + активный экран
  const srcdoc = useMemo(() => prepareSkin(skin, screen), [skin, screen]);

  // Отправка снапшота внутрь iframe
  const postState = () => {
    iframeRef.current?.contentWindow?.postMessage(
      { vpc: 'host', type: 'state', payload: stateRef.current },
      '*',
    );
  };

  // Снапшот меняется → пересылаем (bridge применит его, когда будет готов)
  useEffect(() => {
    postState();
  });

  // Приём событий от скина
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (e.source !== iframeRef.current?.contentWindow) return;
      const d = e.data;
      if (!d || d.vpc !== 'skin' || typeof d.type !== 'string') return;
      switch (d.type) {
        case 'ready':
          postState();
          break;
        case 'send':
          if (typeof d.text === 'string' && (d.text.trim() || typeof d.image === 'string')) {
            onSend?.(d.text.slice(0, 4000), typeof d.image === 'string' ? d.image : undefined);
          }
          break;
        case 'clear':
          onClear?.();
          break;
        case 'select-persona':
          if (typeof d.id === 'string' && d.id) onSelectPersona?.(d.id);
          break;
        case 'open-dossier':
          onOpenDossier?.();
          break;
        case 'close-dossier':
          onCloseDossier?.();
          break;
        case 'action':
          if (typeof d.action === 'string' && d.action) {
            onAction?.(
              d.action,
              d.values && typeof d.values === 'object' ? (d.values as Record<string, string>) : {},
              d.id != null ? String(d.id) : null,
            );
          }
          break;
        case 'set-setting':
          if (typeof d.key === 'string' && d.key) onSetSetting?.(d.key, String(d.value ?? ''));
          break;
        case 'error':
          onError?.(typeof d.message === 'string' ? d.message : 'unknown error');
          break;
        case 'zoom-image':
          // Клик по картинке внутри скина — открыть лайтбокс на хосте
          if (typeof d.src === 'string' && d.src) openImageZoom(d.src);
          break;
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onSend, onClear, onSelectPersona, onOpenDossier, onCloseDossier, onAction, onSetSetting, onError]);

  return (
    <iframe
      ref={iframeRef}
      className={className}
      title={title ?? 'skin'}
      // allow-scripts без allow-same-origin: opaque origin, нет localStorage/сети/доступа к родителю
      sandbox="allow-scripts"
      srcDoc={srcdoc}
    />
  );
}
