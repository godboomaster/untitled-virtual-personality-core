import { useEffect } from 'react';
import { isNativeApp } from '../api';
import { useCoreHealth } from '../apiData';
import { alertDialog, choiceDialog } from '../dialogStore';
import { useI18n } from '../i18n';
import { keysToOffer, moveKeysToCore, PHONE_CATALOG, rememberOffer } from './phoneKeys';

/* App: ядро на связи, а на телефоне есть ключи облачных моделей, о которых
   ещё не спрашивали, — один раз спросить, перенести ли их на компьютер
   (phoneKeys.ts). «Не переносить» — тоже ответ: этот набор ключей больше не
   предлагается. Сеть оборвалась посреди переноса — спросит при следующей
   связи (то, что уже ушло, ядро второй раз не возьмёт). */

let asking = false;

export function useKeysOffer() {
  const health = useCoreHealth();
  const { t } = useI18n();
  useEffect(() => {
    if (!isNativeApp() || health !== 'ok' || asking) return;
    asking = true;
    void (async () => {
      try {
        const list = await keysToOffer();
        if (!list.length) return;
        const names = list.map((p) => PHONE_CATALOG.find((c) => c.id === p.name)?.name ?? p.name).join(', ');
        const answer = await choiceDialog({
          title: t('keysOffer.title'),
          message: t('keysOffer.message', { names }),
          choices: [
            { value: 'move', label: t('keysOffer.move'), primary: true },
            { value: 'keep', label: t('keysOffer.keep') },
          ],
        });
        if (answer === null) return; // закрыли окно — спросим при следующей связи
        if (answer === 'keep') {
          rememberOffer(list);
          return;
        }
        try {
          const r = await moveKeysToCore();
          rememberOffer(list);
          await alertDialog({
            title: t('keysOffer.title'),
            message: r.keys || r.models.length
              ? t('keysOffer.done', { keys: r.keys, models: r.models.join(', ') || '—' })
              : t('keysOffer.nothing'),
          });
        } catch (e) {
          await alertDialog({
            title: t('keysOffer.title'),
            message: t('keysOffer.failed', { err: e instanceof Error ? e.message : String(e) }),
          });
        }
      } finally {
        asking = false;
      }
    })();
  }, [health, t]);
}
