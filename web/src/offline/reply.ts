import { answerOffline } from './engine';
import type { OfflineAnswer } from './engine';
import { addOfflineMessage, offlineMessages, unansweredTail, updateOfflineMessage } from './store';
import type { HistoryLine } from './prompt';

/* Реплика без ядра: в переписку на телефоне, ответ облака или модели
   телефона — туда же. Общий путь поля ввода (Chat.sendOffline) и голоса
   (voice/wakeOffline.ts). Ответить некому — исключение answerOffline
   (OfflineUnavailable), а реплика остаётся в очереди (queued): уйдёт ядру,
   когда оно вернётся. */

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
  const res = await answerOffline(persona, text, history, opts.onToken, opts.onLocalStart, opts.actionNote);
  for (const m of [...pending, userMsg]) updateOfflineMessage(persona, m.id, { source: res.source });
  addOfflineMessage(persona, {
    role: 'assistant',
    text: res.text,
    source: res.source,
    provider: res.provider,
    ...(res.reminder ? { reminder: { id: res.reminder.id, task: res.reminder.task, at: res.reminder.at } } : {}),
  });
  return res;
}
