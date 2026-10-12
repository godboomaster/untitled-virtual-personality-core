/* Кадры с камеры для чата → JPEG разумного размера (CameraCapture, системная
   камера Android в приложении). */

// Длинная сторона кадра — как у снимка экрана в ядре (screen_look.MAX_SIDE):
// мелкий текст ещё читается, а запрос не раздувается мегабайтами
export const CAMERA_MAX_SIDE = 1600;

export function canvasJpeg(source: CanvasImageSource, w: number, h: number): string {
  const k = Math.min(1, CAMERA_MAX_SIDE / Math.max(w, h));
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(w * k);
  canvas.height = Math.round(h * k);
  canvas.getContext('2d')?.drawImage(source, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL('image/jpeg', 0.85);
}

/** Фото с камеры телефона (12+ Мп, мегабайты) → JPEG не больше
 * CAMERA_MAX_SIDE. Поворот по EXIF браузер учитывает сам. */
export function downscalePhoto(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(canvasJpeg(img, img.naturalWidth, img.naturalHeight));
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('decode'));
    };
    img.src = url;
  });
}
