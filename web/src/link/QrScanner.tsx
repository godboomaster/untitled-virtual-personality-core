import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import jsQR from 'jsqr';
import { useI18n } from '../i18n';
import { BACK_OVERLAY, useBackHandler } from '../backStack';

/* Сканер QR-кода сопряжения: камера через getUserMedia (в приложении
   разрешение спрашивает Android), распознавание — jsQR по кадрам видео.
   Ничего, кроме строки vpclink:…, не принимает. */

const SCAN_EVERY_MS = 200;
const MAX_SIDE = 720; // кадр уменьшается — распознавание быстрее, QR крупный

export default function QrScanner({ onResult, onClose }: { onResult: (text: string) => void; onClose: () => void }) {
  const { t } = useI18n();
  const videoRef = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [foreign, setForeign] = useState(false); // попался чужой QR-код
  useBackHandler(true, BACK_OVERLAY, onClose);

  useEffect(() => {
    let stream: MediaStream | null = null;
    let timer: number | undefined;
    let stopped = false;
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d', { willReadFrequently: true });

    const scan = () => {
      const video = videoRef.current;
      if (stopped || !video || !ctx) return;
      if (video.readyState >= 2 && video.videoWidth) {
        const k = Math.min(1, MAX_SIDE / Math.max(video.videoWidth, video.videoHeight));
        canvas.width = Math.round(video.videoWidth * k);
        canvas.height = Math.round(video.videoHeight * k);
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const code = jsQR(img.data, img.width, img.height, { inversionAttempts: 'attemptBoth' });
        if (code?.data) {
          if (code.data.startsWith('vpclink:')) {
            stopped = true;
            onResult(code.data);
            return;
          }
          setForeign(true);
        }
      }
      timer = window.setTimeout(scan, SCAN_EVERY_MS);
    };

    if (!navigator.mediaDevices?.getUserMedia) {
      setError(t('link.camUnsupported'));
      return;
    }
    navigator.mediaDevices
      .getUserMedia({ video: { facingMode: { ideal: 'environment' } }, audio: false })
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
        scan();
      })
      .catch((e: unknown) => {
        const name = e instanceof DOMException ? e.name : '';
        setError(name === 'NotAllowedError' ? t('link.camDenied') : t('link.camFailed'));
      });
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      stream?.getTracks().forEach((tr) => tr.stop());
    };
    // onResult/t — на время жизни сканера не меняются по смыслу
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Портал в body: у панели экрана запуска есть transform, и position: fixed
  // внутри неё считался бы от панели, а не от экрана
  return createPortal(
    <div className="link-scanner" role="dialog" aria-modal="true" aria-label={t('link.scanTitle')}>
      <div className="link-scanner-view">
        <video ref={videoRef} playsInline muted />
        <span className="link-scanner-frame" aria-hidden="true" />
      </div>
      <p className="link-scanner-hint">{error ?? (foreign ? t('link.scanForeign') : t('link.scanHint'))}</p>
      <button type="button" className="btn btn--ghost" onClick={onClose}>
        {t('common.cancel')}
      </button>
    </div>,
    document.body,
  );
}
