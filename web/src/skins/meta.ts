/* Метаданные файла скина: объявляются <meta>-тегами в <head>.

     <meta name="vpc-skin-name" content="Лесная роща">
     <meta name="vpc-skin-author" content="...">
     <meta name="vpc-skin-version" content="1.2">
     <meta name="vpc-skin-contract" content="3">

   Разбор regex'ами (без DOM), чтобы работало и вне браузерного рендера.
   Файл без vpc-skin-contract считается скином контракта v1. */

// Версия контракта (hook-точки, снапшот, события), которую поддерживает приложение
export const SKIN_CONTRACT_VERSION = 3;

export interface SkinMeta {
  name?: string;
  author?: string;
  version?: string;
  contract: number;
}

function decodeEntities(s: string): string {
  return s
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}

// Значение <meta name="..." content="..."> (порядок атрибутов любой)
function metaContent(html: string, name: string): string | undefined {
  const re = /<meta\b[^>]*>/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html))) {
    const tag = m[0];
    const n = /\bname\s*=\s*["']([^"']*)["']/i.exec(tag);
    if (!n || n[1].toLowerCase() !== name) continue;
    const c = /\bcontent\s*=\s*"([^"]*)"|\bcontent\s*=\s*'([^']*)'/i.exec(tag);
    if (!c) return undefined;
    const value = decodeEntities((c[1] ?? c[2] ?? '').trim());
    return value ? value.slice(0, 200) : undefined;
  }
  return undefined;
}

export function readSkinMeta(source: string): SkinMeta {
  // Примеры <meta> в комментариях-инструкциях шаблона не должны перебивать настоящие
  const html = source.replace(/<!--[\s\S]*?-->/g, '');
  const contractRaw = metaContent(html, 'vpc-skin-contract');
  const contract = contractRaw ? parseInt(contractRaw, 10) : NaN;
  return {
    name: metaContent(html, 'vpc-skin-name'),
    author: metaContent(html, 'vpc-skin-author'),
    version: metaContent(html, 'vpc-skin-version'),
    contract: Number.isFinite(contract) && contract > 0 ? contract : 1,
  };
}
