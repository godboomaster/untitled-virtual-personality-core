import { useEffect } from 'react';
import { isNativeApp } from '../api';
import { useApiPersonas, useCoreHealth } from '../apiData';
import { alertDialog, choiceDialog } from '../dialogStore';
import { useI18n } from '../i18n';
import { isStandalone } from '../standalone';
import {
  initPhoneServices,
  moveServicesToCore,
  rememberServicesOffer,
  servicesToOffer,
  syncPhoneServicesMode,
} from './phoneServices';
import type { PhoneServicesStatus, PhoneSource } from './phoneServices';

/* App: «Сервисы» телефона (phoneServices.ts).

   usePhoneServices — плагину: режим (проверять ли сервисы — только без
   компьютера), кто из персон может сообщать и язык; очередь доставленных
   сообщений — в чат.

   useServicesOffer — ядро на связи, а на телефоне есть сервисы, о переносе
   которых ещё не спрашивали: один раз спросить, перенести ли их на
   компьютер (как ключи — useKeysOffer.ts). «Перенести» — ключи, курсоры,
   правила и лента уходят на ядро, телефон их больше не проверяет. «Не
   переносить» — тоже ответ: этот набор больше не предлагается, а телефон, пока
   подключён компьютер, их всё равно не проверяет. Сеть оборвалась посреди
   переноса — спросит при следующей связи (ядро второй раз не задвоит). */

export function usePhoneServices() {
  const personas = useApiPersonas();
  const { lang } = useI18n();
  useEffect(() => {
    initPhoneServices();
  }, []);
  const key = (personas ?? []).map((p) => `${p.id}:${p.name}`).join('|');
  useEffect(() => {
    if (!isNativeApp()) return;
    syncPhoneServicesMode((personas ?? []).map((p) => ({ id: p.id, name: p.name })), lang);
    // personas — тем же списком, что key
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, lang]);
}

let asking = false;

function sourceNames(st: PhoneServicesStatus, t: (k: string, v?: Record<string, string | number>) => string): string {
  const one = (s: PhoneSource) =>
    s === 'rss'
      ? t('phoneSvc.offer.rss', { n: st.settings.services.rss.feeds.length })
      : s === 'github'
        ? `GitHub (${st.accounts.github.login || st.accounts.github.token_hint})`
        : s === 'imap'
          ? `${t('services.src.imap')} (${st.accounts.imap.user})`
          : `Gmail (${st.accounts.google.email})`;
  return st.movable.map(one).join(', ');
}

export function useServicesOffer() {
  const health = useCoreHealth();
  const { t } = useI18n();
  useEffect(() => {
    if (!isNativeApp() || isStandalone() || health !== 'ok' || asking) return;
    asking = true;
    void (async () => {
      try {
        const st = await servicesToOffer();
        if (!st) return;
        const answer = await choiceDialog({
          title: t('phoneSvc.offer.title'),
          message: t('phoneSvc.offer.message', { names: sourceNames(st, t) }),
          choices: [
            { value: 'move', label: t('phoneSvc.offer.move'), primary: true },
            { value: 'keep', label: t('phoneSvc.offer.keep') },
          ],
        });
        if (answer === null) return; // закрыли окно — спросим при следующей связи
        if (answer === 'keep') {
          rememberServicesOffer(st);
          return;
        }
        try {
          const r = await moveServicesToCore();
          rememberServicesOffer(st);
          const label = (s: string) => t(`services.src.${s}`);
          const skipped = Object.entries(r.skipped).map(([k, why]) => `${label(k)}: ${why}`).join('; ');
          await alertDialog({
            title: t('phoneSvc.offer.title'),
            message:
              t('phoneSvc.offer.done', { names: r.moved.map(label).join(', ') || '—', rules: r.rules, items: r.items })
              + (skipped ? `\n\n${t('phoneSvc.offer.skipped', { list: skipped })}` : ''),
          });
        } catch (e) {
          await alertDialog({
            title: t('phoneSvc.offer.title'),
            message: t('phoneSvc.offer.failed', { err: e instanceof Error ? e.message : String(e) }),
          });
        }
      } finally {
        asking = false;
      }
    })();
  }, [health, t]);
}
