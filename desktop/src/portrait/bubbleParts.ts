/* Длинная реплика — по частям, чтобы каждая влезала в облачко над головой
   целиком: по предложениям, а предложение длиннее облачка — по словам, с
   многоточием на месте разреза. Влезает ли — решает fits (мерка по тем же
   стилям, что у облачка: measureFits). */

const ELLIPSIS = '…';
// Конец предложения: знак и пробел после него (или перевод строки)
const SENTENCE_END = /(?<=[.!?…])\s+|\n+/;

export function splitParts(text: string, fits: (s: string) => boolean): string[] {
  const units = text
    .split(SENTENCE_END)
    .map((u) => u.replace(/\s+/g, ' ').trim())
    .filter(Boolean);
  const parts: string[] = [];
  let cur = '';
  const push = () => {
    if (cur) parts.push(cur);
    cur = '';
  };
  for (const unit of units) {
    const joined = cur ? `${cur} ${unit}` : unit;
    if (fits(joined)) {
      cur = joined;
      continue;
    }
    push();
    if (fits(unit)) {
      cur = unit;
      continue;
    }
    // Предложение само не влезает — по словам; слово длиннее облачка
    // переносится облачком по буквам, но часть из него одна
    for (const word of unit.split(' ')) {
      const next = cur ? `${cur} ${word}` : word;
      if (!cur || fits(next + ELLIPSIS)) {
        cur = next;
        continue;
      }
      parts.push(cur + ELLIPSIS);
      cur = word;
    }
  }
  push();
  return parts;
}

/* Влезает ли текст в lines строк облачка шириной текста width px: скрытый
   элемент с теми же шрифтом и переносами (portrait.css), высота против
   строк. Элемент один на окно */
export function measureFits(width: number, lines: number): (s: string) => boolean {
  let el = document.getElementById('portrait-bubble-measure');
  if (!el) {
    el = document.createElement('div');
    el.id = 'portrait-bubble-measure';
    el.className = 'portrait-bubble-measure';
    el.setAttribute('aria-hidden', 'true');
    document.body.appendChild(el);
  }
  const box = el;
  box.style.width = `${Math.max(40, Math.floor(width))}px`;
  box.textContent = 'Ag';
  const line = box.getBoundingClientRect().height || 18;
  const limit = line * lines + 1;
  return (s: string) => {
    box.textContent = s;
    return box.getBoundingClientRect().height <= limit;
  };
}
