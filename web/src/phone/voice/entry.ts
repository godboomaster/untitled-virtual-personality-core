/* Точка входа бандла для голосовой службы телефона (assets/phone-voice.js,
   глобальный vpcPhone): «Коннор, включи фонарик» разбирается тем же
   actions.ts, что и в чате, — с переспросами («на какое время?») и выбором
   приложения. Исполнение — в Java (ops), ядру уходит итог (report). */

import { runPhoneCommand } from '../actions';
import { setApps, takeOps } from './capacitorShim';

// Последний результат: движок без возврата промисов читает его следующим
// вызовом. Пустая строка — ещё не готов (plan обнуляет его сразу): так Java
// не примет итог прошлой фразы за итог этой
export let last = '';

export async function plan(
  persona: string,
  text: string,
  names: string[],
  nowMs: number,
  apps: { label: string; package: string }[],
): Promise<string> {
  last = '';
  setApps(apps);
  takeOps();
  const report = await runPhoneCommand(persona, text, names, new Date(nowMs));
  last = JSON.stringify({ report, ops: report ? takeOps() : [] });
  takeOps();
  return last;
}
