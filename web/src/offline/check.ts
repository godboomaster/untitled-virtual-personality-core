import { api } from '../api';
import { offlinePersonaList } from '../apiData';
import { getKit, kitError, refreshKit } from './kit';
import { getMemory, memoryHasVectors, memoryHistory } from './memory';
import { OfflineLlm } from './plugin';
import { embedError, embedTexts, releaseEmbedder } from './embed';

/* «Проверить» в «Без ноутбука»: что сейчас работает для ответов без
   ноутбука — по шагам, с причиной, если не работает. Шаги идут по одному,
   onStep получает список после каждого (карточка показывает ход проверки). */

export type CheckState = 'ok' | 'fail' | 'warn' | 'run';

export interface CheckStep {
  key: 'core' | 'kits' | 'memory' | 'search' | 'cloud' | 'internet' | 'model';
  state: CheckState;
  // ключ i18n и подстановки; detail — техническая причина как есть
  text: string;
  vars?: Record<string, string | number>;
  detail?: string;
}

const msg = (e: unknown) => (e instanceof Error ? e.message : String(e));

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return Promise.race([p, new Promise<T>((_, rej) => window.setTimeout(() => rej(new Error(`timeout ${ms / 1000} s`)), ms))]);
}

export async function runOfflineCheck(onStep: (steps: CheckStep[]) => void): Promise<CheckStep[]> {
  const steps: CheckStep[] = [];
  const push = (s: CheckStep) => {
    const i = steps.findIndex((x) => x.key === s.key);
    if (i >= 0) steps[i] = s;
    else steps.push(s);
    onStep([...steps]);
  };

  // 1. Ноутбук
  push({ key: 'core', state: 'run', text: 'offline.check.running' });
  let core = false;
  try {
    await withTimeout(api.health(), 5000);
    core = true;
    push({ key: 'core', state: 'ok', text: 'offline.check.coreOk' });
  } catch {
    push({ key: 'core', state: 'warn', text: 'offline.check.coreDown' });
  }

  // 2. Данные персон: ноутбук на связи — заодно скачать недостающие
  push({ key: 'kits', state: 'run', text: 'offline.check.running' });
  const personas = offlinePersonaList();
  if (core) for (const p of personas) await refreshKit(p.id);
  const have = personas.filter((p) => getKit(p.id));
  const missing = personas.filter((p) => !getKit(p.id));
  const errors = missing.map((p) => kitError(p.id)?.message).filter(Boolean) as string[];
  push({
    key: 'kits',
    state: !personas.length ? 'fail' : missing.length ? (have.length ? 'warn' : 'fail') : 'ok',
    text: !personas.length ? 'offline.check.kitsNoList' : missing.length ? 'offline.check.kitsMissing' : 'offline.check.kitsOk',
    vars: { have: have.map((p) => p.name).join(', ') || '—', missing: missing.map((p) => p.name).join(', ') },
    detail: errors[0],
  });

  // 2б. Память на телефоне: сколько переписки у каждой персоны
  if (have.length) {
    const counts: string[] = [];
    for (const p of have) {
      const kit = getKit(p.id)!;
      const mem = await getMemory(p.id).catch(() => null);
      const n = Math.max(memoryHistory(kit, mem).length, kit.history.length);
      counts.push(`${p.name} — ${n}`);
    }
    push({ key: 'memory', state: 'ok', text: 'offline.check.memoryOk', vars: { list: counts.join(', ') } });
  }

  // 2в. Поиск по смыслу: модель векторов на телефоне и векторы памяти с ноутбука
  push({ key: 'search', state: 'run', text: 'offline.check.running' });
  const embed = await OfflineLlm.embedStatus().catch(() => null);
  if (!embed?.downloaded) {
    push({ key: 'search', state: 'warn', text: 'offline.check.searchNone' });
  } else {
    embedError(true); // сбой был раньше — проверить заново, а не ждать
    const t0 = Date.now();
    const vecs = await embedTexts(['проверка поиска по смыслу']);
    releaseEmbedder(); // дальше проверка Gemma — ей нужна память
    const withVectors: string[] = [];
    for (const p of have) if (memoryHasVectors(await getMemory(p.id).catch(() => null))) withVectors.push(p.name);
    if (!vecs) push({ key: 'search', state: 'fail', text: 'offline.check.searchFail', detail: embedError() });
    else if (!withVectors.length) push({ key: 'search', state: 'warn', text: 'offline.check.searchNoVectors' });
    else
      push({
        key: 'search',
        state: 'ok',
        text: 'offline.check.searchOk',
        vars: { sec: ((Date.now() - t0) / 1000).toFixed(1), list: withVectors.join(', ') },
      });
  }

  // 3. Облако по ключу: ключи есть — пробный короткий запрос
  push({ key: 'cloud', state: 'run', text: 'offline.check.running' });
  let withKeys: string | null = null;
  for (const p of have) {
    const info = await OfflineLlm.providersInfo({ persona: p.id }).catch(() => ({ names: [] as string[] }));
    if (info.names.length) {
      withKeys = p.id;
      break;
    }
  }
  if (!withKeys) {
    push({ key: 'cloud', state: 'warn', text: 'offline.check.cloudNoKeys' });
  } else {
    try {
      const r = await withTimeout(
        OfflineLlm.cloudChat({
          persona: withKeys,
          messages: [
            { role: 'system', content: 'Reply with exactly one word: OK' },
            { role: 'user', content: 'ping' },
          ],
          maxTokens: 5,
          temperature: 0,
        }),
        60000,
      );
      push({ key: 'cloud', state: 'ok', text: 'offline.check.cloudOk', vars: { provider: r.provider, model: r.model } });
    } catch (e) {
      push({ key: 'cloud', state: 'fail', text: 'offline.check.cloudFail', detail: msg(e) });
    }
  }

  // 4. Интернет (поиск и погода без ноутбука)
  push({ key: 'internet', state: 'run', text: 'offline.check.running' });
  try {
    const r = await withTimeout(fetch('https://ru.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json&origin=*'), 8000);
    push({ key: 'internet', state: r.ok ? 'ok' : 'warn', text: r.ok ? 'offline.check.netOk' : 'offline.check.netFail', detail: r.ok ? undefined : `HTTP ${r.status}` });
  } catch (e) {
    push({ key: 'internet', state: 'warn', text: 'offline.check.netFail', detail: msg(e) });
  }

  // 5. Модель на телефоне: настоящий короткий ответ
  push({ key: 'model', state: 'run', text: 'offline.check.modelRunning' });
  const status = await OfflineLlm.modelStatus().catch(() => null);
  if (!status?.downloaded) {
    push({ key: 'model', state: 'warn', text: 'offline.check.modelNone' });
  } else {
    const t0 = Date.now();
    try {
      const r = await withTimeout(
        OfflineLlm.localChat({
          requestId: `check${t0}`,
          system: 'You are a test. Reply with one short word.',
          history: [],
          text: 'Say OK',
          maxTokens: 8,
          temperature: 0.1,
        }),
        300000, // первый раз: видеоядро (до минуты) и, если не подошло, загрузка на процессор
      );
      push({
        key: 'model',
        state: 'ok',
        text: 'offline.check.modelOk',
        vars: { sec: ((Date.now() - t0) / 1000).toFixed(1), backend: (r.backend || '?').toUpperCase(), reply: r.text.trim().slice(0, 40) },
      });
    } catch (e) {
      push({ key: 'model', state: 'fail', text: 'offline.check.modelFail', detail: msg(e) });
    }
  }
  return steps;
}
