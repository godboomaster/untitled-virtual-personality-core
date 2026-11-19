import { answerOffline, OfflineUnavailable } from './engine';
import type { OfflineAnswer } from './engine';
import { addOfflineMessage, offlineMessages, unansweredTail, updateOfflineMessage } from './store';
import type { HistoryLine } from './prompt';
import { getKit } from './kit';
import { dialogueLanguage } from './language';
import { notesTurn } from './notesTurn';

/* Реплика без ядра: в переписку на телефоне, ответ облака или модели
   телефона — туда же. Общий путь поля ввода (Chat.sendOffline) и голоса
   (voice/wakeOffline.ts). Ответить некому — исключение answerOffline
   (OfflineUnavailable), а реплика остаётся в очереди (queued): уйдёт ядру,
   когда оно вернётся. Просьбу про заметки исполняет код (notesTurn.ts) до
   ответа; ответить некому — итог говорит само приложение, и реплика не
   ждёт: ядро, вернувшись, записало бы заметку второй раз. */

export interface ReplyOptions {
  onToken?: (chunk: string) => void;
  onLocalStart?: () => void; // ответ взяла модель телефона
  actionNote?: string; // итог команды телефону (phone/notes.ts)
}

export async function replyOffline(
  persona: string,
  text: string,
  history: HistoryLine[],
  opts: ReplyOptions = {},
): Promise<OfflineAnswer> {
  // Прежние реплики без ответа модель увидит в истории — ответ и на них
  const pending = unansweredTail(offlineMessages(persona));
  const userMsg = addOfflineMessage(persona, { role: 'user', text, source: 'queued' });
  const notes = notesTurn(persona, text, history, dialogueLanguage(text, history, getKit(persona)?.lang));
  const actionNote = [opts.actionNote, notes?.note].filter(Boolean).join('\n\n') || undefined;
  let res: OfflineAnswer;
  try {
    // Ход забран заметками — напоминания и поиск его не берут (как у ядра)
    res = await answerOffline(persona, text, history, opts.onToken, opts.onLocalStart, actionNote, !!notes?.claimed);
  } catch (e) {
    const plain = notes?.code ?? notes?.list;
    if (!(e instanceof OfflineUnavailable) || !plain) throw e;
    res = { text: plain, source: 'local' };
    if (plain === notes?.list) notes.list = undefined;
  }
  for (const m of [...pending, userMsg]) updateOfflineMessage(persona, m.id, { source: res.source });
  // Режим персоны, которым ответил телефон (из кита): ядро запишет его в
  // журнал режимов — ответ другого режима потом будет помечен в истории
  const mode = getKit(persona)?.modes?.current;
  addOfflineMessage(persona, {
    role: 'assistant',
    text: res.text,
    source: res.source,
    provider: res.provider,
    ...(typeof mode === 'string' ? { mode } : {}),
    ...(res.reminder ? { reminder: { id: res.reminder.id, task: res.reminder.task, at: res.reminder.at } } : {}),
  });
  // «Покажи заметки» — список отдельным сообщением после ответа
  if (notes?.list) addOfflineMessage(persona, { role: 'assistant', text: notes.list, source: res.source, ...(typeof mode === 'string' ? { mode } : {}) });
  return res;
}
