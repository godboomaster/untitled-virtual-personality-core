// Промпты для генератора картинок: лист кадров анимации. Общие для вкладки
// «Анимации» и загрузки листа в редакторе кадров

export type PromptKind = 'action' | 'walk' | 'grab' | 'fall';
export type Framing = 'body' | 'bust';
export type Side = 'left' | 'right';

export const otherSide = (f: Side): Side => (f === 'left' ? 'right' : 'left');

// Промпты — по-английски, как остальные промпт-паки
export function buildAnimPrompt(
  kind: PromptKind,
  name: string,
  action: string,
  rows: number,
  cols: number,
  framing: Framing,
  side: Side = 'right',
): string {
  const n = rows * cols;
  const keep =
    'Keep the character exactly: face, hairstyle, hair color, eyes, outfit, accessories, colors, line art and art style. ' +
    'The same character size in every cell; the whole figure and every prop fully inside its own cell, with empty space between cells.';
  const frame =
    framing === 'bust'
      ? 'Head and shoulders, framed like a portrait bust: the bottom of the shoulders on the same line in every cell.'
      : 'Full body, the feet on the same ground line in every cell.';
  const bg =
    'Plain flat background of one solid color (no gradient, no texture, no shadow on the background), no text, no frame numbers, no borders or grid lines.';
  if (kind === 'grab') {
    return [
      `Using the attached character reference image, draw a sprite sheet of ${name} picked up and held in the air from above by an unseen hand (by the back of the collar): the body hangs and dangles, the legs swing slightly, the arms hang loose, a surprised or pouting face.`,
      `Grid: ${rows} rows × ${cols} columns = ${n} frames of one seamless loop, in reading order: left to right, then top to bottom; the last frame leads back into the first.`,
      'Small changes between neighbouring frames; the character hangs in the middle of every cell, the top of the head on the same line in every cell. No hand, no rope, nothing holding the character is drawn.',
      keep,
      bg,
    ].join('\n');
  }
  if (kind === 'fall') {
    return [
      `Using the attached character reference image, draw a sprite sheet of ${name} falling through the air: arms and legs flailing, hair and clothes blown upwards by the wind, a startled face.`,
      `Grid: ${rows} rows × ${cols} columns = ${n} frames of one seamless loop, in reading order: left to right, then top to bottom; the last frame leads back into the first.`,
      'The character falls in place in the middle of every cell (no travel across the sheet), the same size in every cell; no ground, no motion lines.',
      keep,
      bg,
    ].join('\n');
  }
  if (kind === 'walk') {
    return [
      `Using the attached character reference image, draw a sprite sheet of ${name}: a side-view walk cycle, walking to the ${side}.`,
      `Grid: ${rows} rows × ${cols} columns = ${n} frames of one seamless loop, in reading order: left to right, then top to bottom; the last frame leads back into the first.`,
      'The character walks in place in the middle of every cell (no travel across the sheet): legs and arms alternate, the body bobs slightly.',
      keep,
      frame,
      bg,
    ].join('\n');
  }
  const what = action.trim() || 'doing an everyday action';
  return [
    `Using the attached character reference image, draw a sprite sheet of ${name} ${what}.`,
    `Grid: ${rows} rows × ${cols} columns = ${n} frames of one short animation, in reading order: left to right, then top to bottom; the last frame flows back into the first.`,
    'Small changes between neighbouring frames; the character stays in the same spot of every cell.',
    keep,
    frame,
    bg,
  ].join('\n');
}

export function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
