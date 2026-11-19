import type { OfflineKit } from './kit';
import type { HistoryLine } from './prompt';

/* Режимы персоны без ядра — как в промпте ядра (app/core/persona.py,
   prepare_messages): история у режимов общая, а ответ, написанный в другом
   режиме, модель видит с пометкой («[in your "Sancho" mode]») и правилом
   не перенимать его тон; в конце системного блока — образцы голоса режима,
   что сейчас. Какой режим у ответа — по времени, журналом из кита
   (kit.modes.log). Без режимов у персоны (kit.modes нет) — без пометок.
   Без зависимостей приложения: scripts/test_offline_recall.py гоняет под Node. */

/** Режим чата в момент ts ("" — обычный); раньше журнала — null (не известно). */
export function modeAt(kit: Pick<OfflineKit, 'modes'>, ts?: number): string | null {
  const log = kit.modes?.log;
  if (!log?.length || typeof ts !== 'number') return null;
  let found: string | null = null;
  for (const [t, m] of log) {
    if (t <= ts) found = m;
    else break;
  }
  return found;
}

/** Ответы другого режима — с пометкой в начале; tags — какие пометки есть,
 * lastTagged — последний ответ в истории другого режима (ход сразу после смены). */
export function tagHistory(
  kit: Pick<OfflineKit, 'modes'>,
  lines: HistoryLine[],
): { lines: HistoryLine[]; tags: string[]; lastTagged: boolean } {
  const modes = kit.modes;
  if (!modes) return { lines, tags: [], lastTagged: false };
  const used = new Set<string>();
  let lastTagged = false;
  const out = lines.map((l) => {
    if (l.role !== 'assistant') return l;
    const m = modeAt(kit, l.ts);
    const tag = m === null || m === modes.current ? undefined : modes.tags[m];
    lastTagged = !!tag;
    if (!tag) return l;
    used.add(tag);
    return { ...l, text: `${tag} ${l.text}` };
  });
  return { lines: out, tags: [...used].sort(), lastTagged };
}

const CYRILLIC_RE = /[а-яё]/i;
const LATIN_RE = /[a-z]/i;

/** Образец голоса на письменности реплики (кириллица или латиница): образец
 * на другом языке уводил ответ на него. Нет такого — '' (как voice_like ядра). */
export function voiceLike(examples: string[], text: string): string {
  const cyr = CYRILLIC_RE.test(text);
  const hit = examples.find((v) => (cyr ? CYRILLIC_RE.test(v) : LATIN_RE.test(v) && !CYRILLIC_RE.test(v)));
  return hit ? `, in the tone of "${hit}" (tone only — never repeat these words)` : '';
}

/** Реплика с заметкой приложения, если последний ответ в истории — другого
 * режима (MODE_TURN_NOTE ядра): ближе всего к месту ответа. */
export function withTurnNote(kit: Pick<OfflineKit, 'modes' | 'voice'>, lastTagged: boolean, text: string): string {
  const t = kit.modes?.turn;
  if (!t || !lastTagged) return text;
  return `${text}\n\n${t.replace('{like}', () => voiceLike(kit.voice?.lines ?? [], text))}`;
}

/** Заметка о режиме (с пометками — правило к ним) и образцы голоса (не
 * больше voiceMax) — в конец системного блока; нечего — ''. */
export function modeNote(kit: Pick<OfflineKit, 'modes' | 'voice'>, tags: string[], voiceMax = Infinity): string {
  const parts: string[] = [];
  const m = kit.modes;
  if (m) {
    if (tags.length) parts.push(`${m.now} ${m.tagged.replace('{tags}', () => tags.join(', '))}`);
    else if (m.note) parts.push(m.note);
  }
  const lines = (kit.voice?.lines ?? []).slice(0, voiceMax);
  if (kit.voice?.note && lines.length) parts.push(kit.voice.note.replace('{lines}', () => lines.map((x) => `- ${x}`).join('\n')));
  return parts.join('\n\n');
}
