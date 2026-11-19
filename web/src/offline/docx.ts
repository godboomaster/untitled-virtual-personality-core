/* Текст из .docx на телефоне без ядра (лор, localLore.ts). Файл Word — zip:
   нужен только word/document.xml (и styles.xml — какие стили заголовки).
   Распаковка — встроенным DecompressionStream('deflate-raw'), без
   библиотек. Заголовки Word (стиль «heading N», «Title» или уровень
   структуры абзаца) — строками «#», как их выдаёт разбор на ядре
   (markitdown), таблицы — строками «ячейка | ячейка», списки — «- ».
   Без зависимостей приложения: scripts/test_lore.py гоняет под Node. */

const EOCD_SIG = 0x06054b50;
const CENTRAL_SIG = 0x02014b50;
const LOCAL_SIG = 0x04034b50;

export class DocxError extends Error {}

async function inflate(data: Uint8Array): Promise<Uint8Array> {
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** Файлы zip-архива по именам (только нужные). */
async function unzip(buf: ArrayBuffer, wanted: Set<string>): Promise<Map<string, Uint8Array>> {
  const bytes = new Uint8Array(buf);
  const view = new DataView(buf);
  let eocd = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 0xffff); i--) {
    if (view.getUint32(i, true) === EOCD_SIG) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new DocxError('not a zip');
  const count = view.getUint16(eocd + 10, true);
  let p = view.getUint32(eocd + 16, true);
  const names = new TextDecoder();
  const out = new Map<string, Uint8Array>();
  for (let n = 0; n < count && p + 46 <= bytes.length; n++) {
    if (view.getUint32(p, true) !== CENTRAL_SIG) throw new DocxError('bad zip directory');
    const method = view.getUint16(p + 10, true);
    const size = view.getUint32(p + 20, true);
    const nameLen = view.getUint16(p + 28, true);
    const extraLen = view.getUint16(p + 30, true);
    const commentLen = view.getUint16(p + 32, true);
    const local = view.getUint32(p + 42, true);
    const name = names.decode(bytes.subarray(p + 46, p + 46 + nameLen));
    p += 46 + nameLen + extraLen + commentLen;
    if (!wanted.has(name)) continue;
    if (view.getUint32(local, true) !== LOCAL_SIG) throw new DocxError('bad zip entry');
    const start = local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
    const raw = bytes.subarray(start, start + size);
    if (method === 0) out.set(name, raw);
    else if (method === 8) out.set(name, await inflate(raw));
    else throw new DocxError(`zip method ${method}`);
  }
  return out;
}

function decodeXml(s: string): string {
  return s.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos);/gi, (_m, e: string) => {
    const k = e.toLowerCase();
    if (k === 'amp') return '&';
    if (k === 'lt') return '<';
    if (k === 'gt') return '>';
    if (k === 'quot') return '"';
    if (k === 'apos') return "'";
    const code = k.startsWith('#x') ? parseInt(k.slice(2), 16) : parseInt(k.slice(1), 10);
    return Number.isFinite(code) ? String.fromCodePoint(code) : '';
  });
}

const attr = (xml: string, tag: string) => new RegExp(`<w:${tag}\\b[^>]*w:val="([^"]*)"`).exec(xml)?.[1];

/** Стили-заголовки: id стиля → уровень. Имена встроенных стилей в файле
 * всегда английские («heading 1»), в каком бы языке ни был Word. */
function headingStyles(stylesXml: string): Map<string, number> {
  const out = new Map<string, number>();
  for (const m of stylesXml.matchAll(/<w:style\b[^>]*w:styleId="([^"]+)"[^>]*>([\s\S]*?)<\/w:style>/g)) {
    const [, id, body] = m;
    const name = (attr(body, 'name') ?? '').toLowerCase();
    const h = /^heading\s*(\d)$/.exec(name);
    const lvl = attr(body, 'outlineLvl');
    if (h) out.set(id, Number(h[1]));
    else if (name === 'title') out.set(id, 1);
    else if (lvl !== undefined && Number(lvl) < 9) out.set(id, Number(lvl) + 1);
  }
  return out;
}

function paragraphText(xml: string): string {
  let s = '';
  for (const m of xml.matchAll(/<w:t\b[^>]*>([^<]*)<\/w:t>|<w:(tab|br|cr)\b[^>]*\/>/g)) {
    if (m[1] !== undefined) s += decodeXml(m[1]);
    else s += m[2] === 'tab' ? ' ' : '\n';
  }
  return s;
}

function paragraph(xml: string, styles: Map<string, number>): string {
  const text = paragraphText(xml).trim();
  if (!text) return '';
  const pPr = /<w:pPr\b[\s\S]*?<\/w:pPr>/.exec(xml)?.[0] ?? '';
  const style = attr(pPr, 'pStyle');
  const outline = attr(pPr, 'outlineLvl');
  const level = (style && styles.get(style)) || (outline !== undefined && Number(outline) < 9 ? Number(outline) + 1 : 0);
  if (level && !text.includes('\n')) return `${'#'.repeat(Math.min(level, 6))} ${text}`;
  return /<w:numPr\b/.test(pPr) ? `- ${text}` : text;
}

function table(xml: string, styles: Map<string, number>): string {
  const rows: string[] = [];
  for (const row of xml.matchAll(/<w:tr\b[\s\S]*?<\/w:tr>/g)) {
    const cells = [...row[0].matchAll(/<w:tc\b[\s\S]*?<\/w:tc>/g)].map((c) =>
      [...c[0].matchAll(/<w:p\b[^>]*\/>|<w:p\b[\s\S]*?<\/w:p>/g)]
        .map((p) => paragraph(p[0], styles).replace(/^#+ /, ''))
        .filter(Boolean)
        .join(' '),
    );
    if (cells.some(Boolean)) rows.push(cells.join(' | '));
  }
  return rows.join('\n');
}

/** Текст .docx: абзацы через пустую строку, заголовки — «#». */
export async function docxText(buf: ArrayBuffer): Promise<string> {
  let files: Map<string, Uint8Array>;
  try {
    files = await unzip(buf, new Set(['word/document.xml', 'word/styles.xml']));
  } catch (e) {
    throw new DocxError(e instanceof Error ? e.message : String(e));
  }
  const doc = files.get('word/document.xml');
  if (!doc) throw new DocxError('no word/document.xml');
  const dec = new TextDecoder();
  const styles = headingStyles(files.has('word/styles.xml') ? dec.decode(files.get('word/styles.xml')) : '');
  const body = dec.decode(doc);
  const blocks: string[] = [];
  for (const m of body.matchAll(/<w:tbl\b[\s\S]*?<\/w:tbl>|<w:p\b[^>]*\/>|<w:p\b[\s\S]*?<\/w:p>/g)) {
    const part = m[0].startsWith('<w:tbl') ? table(m[0], styles) : paragraph(m[0], styles);
    if (part) blocks.push(part);
  }
  return blocks.join('\n\n');
}
