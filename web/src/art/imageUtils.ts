/* Уменьшение референс-изображения стиля перед сохранением: длинная сторона
   ≤ MAX_SIDE, итоговая dataURL ≤ MAX_BYTES (webp с понижением качества,
   затем — при необходимости — доуменьшение стороны). Без внешних зависимостей
   (canvas + FileReader), лёгкая клиентская операция без сети. */

const MAX_SIDE = 768;
const MAX_BYTES = 1_000_000; // ~1 МБ — как лимит reference в бэкенд-хранилище
// base64 длиннее исходных байт примерно в 4/3 — сравниваем по длине строки
const MAX_DATAURL_LEN = MAX_BYTES * 1.37;

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('read error'));
    reader.onload = () => resolve(String(reader.result));
    reader.readAsDataURL(file);
  });
}

function loadImage(dataUrl: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onerror = () => reject(new Error('decode error'));
    img.onload = () => resolve(img);
    img.src = dataUrl;
  });
}

// Кодирует канвас в webp, подбирая качество под лимит длины dataURL
function encodeUnderLimit(canvas: HTMLCanvasElement): string {
  let quality = 0.85;
  let out = canvas.toDataURL('image/webp', quality);
  while (out.length > MAX_DATAURL_LEN && quality > 0.35) {
    quality -= 0.15;
    out = canvas.toDataURL('image/webp', quality);
  }
  return out;
}

// Файл изображения → downscaled dataURL (webp), с доуменьшением стороны,
// если после подбора качества всё ещё превышен лимит размера
export async function downscaleImageFile(file: File): Promise<string> {
  if (!file.type.startsWith('image/')) throw new Error('not an image');
  const raw = await readAsDataUrl(file);
  const img = await loadImage(raw);
  let width = img.width;
  let height = img.height;
  const longest = Math.max(width, height);
  if (longest > MAX_SIDE) {
    const scale = MAX_SIDE / longest;
    width = Math.max(1, Math.round(width * scale));
    height = Math.max(1, Math.round(height * scale));
  }
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  if (!ctx) return raw;
  canvas.width = width;
  canvas.height = height;
  ctx.drawImage(img, 0, 0, width, height);
  let out = encodeUnderLimit(canvas);
  let attempts = 0;
  while (out.length > MAX_DATAURL_LEN && attempts < 3) {
    width = Math.max(1, Math.round(width * 0.7));
    height = Math.max(1, Math.round(height * 0.7));
    canvas.width = width;
    canvas.height = height;
    ctx.drawImage(img, 0, 0, width, height);
    out = encodeUnderLimit(canvas);
    attempts += 1;
  }
  return out;
}
