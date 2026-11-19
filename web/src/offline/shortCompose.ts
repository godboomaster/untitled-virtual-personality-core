import { getKit } from './kit';
import { OfflineLlm, pluginCode } from './plugin';
import { askWebchats, WebchatsFailed } from './webchat';
import { answerSteps, defaultSteps } from './personaChain';
import { crc32 } from './localPersonas';
import { cardMessages, cleanCard, setPendingShort } from './shortPrompt';

/* «Составить» краткое описание без ядра: тот же запрос карточки, что у ядра
   (shortPrompt.cardMessages), по цепочке провайдеров персоны — облако по
   ключу, веб-чаты свежим чатом (сайты со входом не тратят чат персоны).
   Gemma телефона пропускается, как локальная модель у ядра: маленькая модель
   сама плохо сожмёт характер. Готовая карточка — в очередь правок
   (shortPrompt.ts): Gemma получает её сразу, ядро — при связи. */

export class ComposeFailed extends Error {
  // no_kit — данных персоны на телефоне нет; no_route — никто не ответил
  // (cloud/web — коды последнего отказа); bad_answer — ответ не похож на карточку
  reason: 'no_kit' | 'no_route' | 'bad_answer';
  cloud: string;
  web: string;
  constructor(reason: 'no_kit' | 'no_route' | 'bad_answer', cloud = '', web = '') {
    super(reason);
    this.reason = reason;
    this.cloud = cloud;
    this.web = web;
  }
}

/** → карточка и кто её составил; не вышло — ComposeFailed. */
export async function composeOnPhone(persona: string): Promise<{ text: string; provider: string }> {
  const kit = getKit(persona);
  const character = kit && !kit.fallback ? kit.parts.character.trim() : '';
  if (!kit || !character) throw new ComposeFailed('no_kit');
  const messages = cardMessages(character);
  const steps = await answerSteps(persona).catch(() => defaultSteps());
  let cloud = '';
  let web = '';
  let bad = false;
  const done = (raw: string, provider: string) => {
    const text = cleanCard(raw);
    if (!text) {
      bad = true;
      return null;
    }
    setPendingShort(persona, { text, source: 'auto', at: Date.now(), crc: crc32(kit.parts.character), provider });
    return { text, provider };
  };
  for (const step of steps) {
    if (step.kind === 'cloud') {
      try {
        const r = await OfflineLlm.cloudChat({ persona, messages, maxTokens: 1200, temperature: 0.4, only: step.only ?? undefined });
        const got = done(r.text, r.provider);
        if (got) return got;
      } catch (e) {
        cloud = pluginCode(e);
      }
    } else if (step.kind === 'web' && step.sites.length) {
      try {
        const r = await askWebchats(persona, [messages], true, step.sites);
        const got = done(r.text, r.site.name);
        if (got) return got;
      } catch (e) {
        web = e instanceof WebchatsFailed ? e.code : 'failed';
      }
    }
  }
  throw new ComposeFailed(bad ? 'bad_answer' : 'no_route', cloud, web);
}
