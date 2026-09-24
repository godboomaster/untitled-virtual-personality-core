import { useState } from 'react';
import type { ReactNode } from 'react';
import { useI18n } from '../i18n';

/* Форматирование ответов бота (_md_to_html / RichMessageFormatter.to_current_html):
   **жирный**, *курсив*, _курсив_, __подчёркнутый__, ~~зачёркнутый~~,
   ==выделение==, ||спойлер|| (раскрывается кликом), `inline-код`,
   ```блоки кода```, [ссылки](url), > цитаты. Рендерим в React-узлы —
   без dangerouslySetInnerHTML. */

// Порядок альтернатив важен: ** раньше *, __ раньше _
const INLINE_RE = new RegExp(
  [
    '\\*\\*(?<bold>[\\s\\S]+?)\\*\\*',
    '\\*(?<ital>[^*\\n]+?)\\*',
    '__(?<und>[\\s\\S]+?)__',
    '(?<!\\w)_(?<ital2>[\\s\\S]+?)_(?!\\w)',
    '~~(?<strike>[\\s\\S]+?)~~',
    '\\|\\|(?<spoiler>[\\s\\S]+?)\\|\\|',
    '==(?<mark>[\\s\\S]+?)==',
    '`(?<code>[^`\\n]+?)`',
    '\\[(?<linktext>[^\\]\\n]+?)\\]\\((?<linkurl>https?://[^)\\s]+?)\\)',
  ].join('|'),
  'g',
);

function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE_RE)) {
    const g = m.groups ?? {};
    const idx = m.index ?? 0;
    if (idx > last) nodes.push(text.slice(last, idx));
    const key = nodes.length;
    if (g.bold) nodes.push(<strong key={key}>{renderInline(g.bold)}</strong>);
    else if (g.ital) nodes.push(<em key={key}>{renderInline(g.ital)}</em>);
    else if (g.und) nodes.push(<u key={key}>{renderInline(g.und)}</u>);
    else if (g.ital2) nodes.push(<em key={key}>{renderInline(g.ital2)}</em>);
    else if (g.strike) nodes.push(<s key={key}>{renderInline(g.strike)}</s>);
    else if (g.spoiler) nodes.push(<Spoiler key={key}>{renderInline(g.spoiler)}</Spoiler>);
    else if (g.mark) nodes.push(<mark key={key} className="msg-mark">{renderInline(g.mark)}</mark>);
    else if (g.code) nodes.push(<code key={key} className="msg-code">{g.code}</code>);
    else if (g.linktext) {
      nodes.push(
        <a key={key} href={g.linkurl} target="_blank" rel="noreferrer">
          {g.linktext}
        </a>,
      );
    }
    last = idx + m[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

// Спойлер: скрыт до клика
function Spoiler({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  return (
    <span
      className={`msg-spoiler${open ? ' msg-spoiler--open' : ''}`}
      onClick={open ? undefined : () => setOpen(true)}
      title={open ? undefined : t('chat.spoiler')}
    >
      {children}
    </span>
  );
}

/* Стриминг: на «хвосте» накопленного текста прячем незакрытые маркеры,
   чтобы сырой markdown не мелькал в пузыре по ходу генерации — сообщение
   выглядит отформатированным сразу, а не после финальной замены.
   Финальный текст (streaming=false) рендерится как есть. */
function hideDangling(text: string): string {
  let s = text;
  // Незаконченная ссылка [текст](url… — прячем целиком до закрывающей скобки
  const lb = s.lastIndexOf('[');
  if (lb !== -1) {
    const rest = s.slice(lb);
    const paren = rest.indexOf('](');
    if (paren !== -1 && !rest.includes(')', paren + 2)) s = s.slice(0, lb);
  }
  // Одиночные ` (исключая fence ```): нечётно — прячем последний
  const fences = (s.match(/```/g) ?? []).length;
  const backticks = (s.match(/`/g) ?? []).length - fences * 3;
  if (backticks % 2 === 1) {
    const idx = s.lastIndexOf('`');
    // не трогаем backtick, если это часть fence
    if (s.slice(Math.max(0, idx - 2), idx + 1) !== '```') s = s.slice(0, idx) + s.slice(idx + 1);
  }
  // Парные маркеры: нечётное число — прячем висячий открыватель (последний)
  for (const marker of ['**', '__', '~~', '||', '==']) {
    if ((s.split(marker).length - 1) % 2 === 1) {
      const idx = s.lastIndexOf(marker);
      s = s.slice(0, idx) + s.slice(idx + marker.length);
    }
  }
  // Одиночные * и _ (после снятия парных ** и __; не трогаем mid-word _)
  for (const ch of ['*', '_']) {
    if ((s.split(ch).length - 1) % 2 === 1) {
      const idx = s.lastIndexOf(ch);
      if (idx === 0 || !/\w/.test(s[idx - 1])) s = s.slice(0, idx) + s.slice(idx + 1);
    }
  }
  // Незакрытый fence кода — достраиваем: контент внутри литеральный
  if (fences % 2 === 1) s += '\n```';
  return s;
}

// Блочный уровень: ```блоки кода``` вырезаем до inline-разметки
type Part = string | { lang: string; code: string };

function splitCodeBlocks(text: string): Part[] {
  const parts: Part[] = [];
  const re = /```(\w*)\n?([\s\S]*?)```/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    const idx = m.index ?? 0;
    if (idx > last) parts.push(text.slice(last, idx));
    parts.push({ lang: m[1], code: m[2].replace(/\n$/, '') });
    last = idx + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

// Текстовый кусок: строки-подряд с '> ' собираем в цитату, остальное — построчно
function TextPart({ text }: { text: string }) {
  const out: ReactNode[] = [];
  let quote: string[] = [];
  const flushQuote = () => {
    if (!quote.length) return;
    out.push(
      <blockquote key={out.length} className="msg-quote">
        {renderInline(quote.join('\n'))}
      </blockquote>,
    );
    quote = [];
  };
  for (const line of text.split('\n')) {
    const qm = /^> ?(.*)$/.exec(line);
    if (qm) {
      quote.push(qm[1]);
      continue;
    }
    flushQuote();
    out.push(renderInline(line), '\n');
  }
  flushQuote();
  if (out[out.length - 1] === '\n') out.pop(); // хвостовой перенос лишний
  return <>{out}</>;
}

export default function MessageText({ text, streaming = false }: { text: string; streaming?: boolean }) {
  const parts = splitCodeBlocks(streaming ? hideDangling(text) : text);
  return (
    <>
      {parts.map((part, i) =>
        typeof part === 'string' ? (
          <TextPart key={i} text={part} />
        ) : (
          <pre key={i} className="msg-pre">
            {part.lang && <div className="msg-pre-lang">{part.lang}</div>}
            <code>{part.code}</code>
          </pre>
        ),
      )}
    </>
  );
}
