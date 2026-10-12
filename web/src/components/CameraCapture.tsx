import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useI18n } from '../i18n';
import { BACK_OVERLAY, useBackHandler } from '../backStack';
import { canvasJpeg } from '../cameraImage';

/* Кадр с веб-камеры (браузер на компьютере): живое превью getUserMedia →
   «Снять» → JPEG. Кадр смотрит только локальная модель ядра
   (ChatRequest.image_source = camera). В приложении на телефоне вместо
   этого окна — системная камера Android (input capture в чате). */

export default function CameraCapture({ onCapture, onClose }: { onCapture: (dataUrl: string) => void; onClose: () => void }) {
  const { t } = useI18n();
  const videoRef = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  useBackHandler(true, BACK_OVERLAY, onClose);

  useEffect(() => {
    let stream: MediaStream | null = null;
    let stopped = false;
    if (!navigator.mediaDevices?.getUserMedia) {
      setError(t('camera.unsupported'));
      return;
    }
    navigator.mediaDevices
      .getUserMedia({ video: { width: { ideal: 1920 }, height: { ideal: 1080 } }, audio: false })
      .then((s) => {
        if (stopped) {
          s.getTracks().forEach((tr) => tr.stop());
          return;
        }
        stream = s;
        const video = videoRef.current;
        if (video) {
          video.srcObject = s;
          void video.play().catch(() => {});
        }
      })
      .catch((e: unknown) => {
        const name = e instanceof DOMException ? e.name : '';
        setError(
          name === 'NotAllowedError'
            ? t('camera.denied')
            : name === 'NotFoundError' || name === 'OverconstrainedError'
              ? t('camera.none')
              : t('camera.failed'),
        );
      });
    // Окно закрыто — камеру выключаем (иначе горит индикатор)
    return () => {
      stopped = true;
      stream?.getTracks().forEach((tr) => tr.stop());
    };
    // t — на время жизни окна не меняется по смыслу
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const shoot = () => {
    const video = videoRef.current;
    if (!video || !video.videoWidth) return;
    onCapture(canvasJpeg(video, video.videoWidth, video.videoHeight));
  };

  return createPortal(
    <div className="camera-capture" role="dialog" aria-modal="true" aria-label={t('camera.title')}>
      <div className="camera-capture-view">
        <video ref={videoRef} playsInline muted onLoadedData={() => setReady(true)} />
      </div>
      <p className="camera-capture-hint">{error ?? t('camera.hint')}</p>
      <div className="camera-capture-actions">
        <button type="button" className="btn btn--primary" disabled={!ready || !!error} onClick={shoot}>
          {t('camera.shoot')}
        </button>
        <button type="button" className="btn btn--ghost" onClick={onClose}>
          {t('common.cancel')}
        </button>
      </div>
    </div>,
    document.body,
  );
}
