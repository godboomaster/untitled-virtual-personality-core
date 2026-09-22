/* Лайтбокс: клик по контентной картинке открывает её крупно.
   Клики слушаются делегированно на document — догружаемые динамически
   сообщения не нуждаются в своих обработчиках; картинки скинов
   (sandboxed iframe) открываются через imageZoomStore из SkinFrame.
   Закрытие: клик по подложке / ✕ / Esc. */

import { useEffect } from 'react';
import { useI18n } from '../i18n';
import { closeImageZoom, openImageZoom, useImageZoom } from '../imageZoomStore';

// Контентные картинки, по которым клик открывает лайтбокс:
// сообщения чата, превью прикрепления над вводом, превью в панели арта
const ZOOMABLE_SELECTOR = [
  'img.message-image',
  'img.chat-attach-thumb',
  'img.art-thumb',
].join(', ');

export default function ImageLightbox() {
  const { t } = useI18n();
  const src = useImageZoom();

  // Один слушатель на документ вместо обработчика на каждой картинке
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      const el = e.target as HTMLElement | null;
      const img = el?.closest?.(ZOOMABLE_SELECTOR) as HTMLImageElement | null;
      if (img?.src) openImageZoom(img.src);
    };
    document.addEventListener('click', onClick);
    return () => document.removeEventListener('click', onClick);
  }, []);

  // Esc закрывает (слушатель только пока лайтбокс открыт)
  useEffect(() => {
    if (src === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeImageZoom();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [src]);

  if (src === null) return null;

  return (
    <div className="image-lightbox" onClick={closeImageZoom} role="dialog" aria-modal="true">
      {/* клик по самой картинке не закрывает — подложка и ✕ закрывают */}
      <img
        className="image-lightbox-img"
        src={src}
        alt={t('chat.attachment')}
        onClick={(e) => e.stopPropagation()}
      />
      <button
        type="button"
        className="image-lightbox-close"
        title={t('common.close')}
        onClick={closeImageZoom}
      >
        ✕
      </button>
    </div>
  );
}
