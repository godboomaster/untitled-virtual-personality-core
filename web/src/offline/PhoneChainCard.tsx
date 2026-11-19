import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import { excludeIn, makeFirst, moveIn, restoreIn, toChain } from './chainOrder';
import { loadChain, onChainChange, saveChain } from './personaChain';
import type { ChainItem, LoadedChain } from './personaChain';
import { onPhoneKeysChange } from './phoneKeys';
import { OfflineLlm } from './plugin';
import { onWebchatChange } from './webchat';

/* Досье → «Настройки» → «LLM-провайдеры» без связи с ядром: цепочка этой
   персоны на телефоне — как на компьютере: основной, порядок после него
   (стрелки), «сделать основным», убрать и вернуть, «Сохранить». Звенья —
   ключи (с компьютера и телефона), включённые веб-чаты телефона, Gemma
   (personaChain.ts). Веб-чаты и локальная модель ПК подписаны «ПК» и без
   ядра всегда в убранных: «вернуть» — только пояснение, почему нельзя. */

type Draft = { order: string[]; exclude: string[] };

export default function PhoneChainCard({ personaId }: { personaId: string }) {
  const { t } = useI18n();
  const [loaded, setLoaded] = useState<LoadedChain | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [gemma, setGemma] = useState<boolean | null>(null);
  const [savedFlash, setSavedFlash] = useState(false);
  const [noCore, setNoCore] = useState<string | null>(null); // «вернуть» звено ПК — почему нельзя

  useEffect(() => {
    let alive = true;
    const load = () => {
      loadChain(personaId)
        .then((c) => {
          if (!alive) return;
          setLoaded(c);
          setDraft({ order: c.order, exclude: c.exclude });
        })
        .catch(() => {});
    };
    const status = () =>
      OfflineLlm.modelStatus()
        .then((s) => alive && setGemma(s.downloaded))
        .catch(() => alive && setGemma(false));
    load();
    status();
    const timer = setInterval(status, 10000);
    const offs = [onChainChange(load), onWebchatChange(load), onPhoneKeysChange(load)];
    return () => {
      alive = false;
      clearInterval(timer);
      offs.forEach((off) => off());
    };
  }, [personaId]);

  if (!loaded || !draft) return null;

  const byId = new Map(loaded.items.map((i) => [i.id, i]));
  const { order, exclude } = draft;
  const excluded = loaded.items.filter((i) => exclude.includes(i.id));
  const same = (a: string[], b: string[]) => a.join('\n') === b.join('\n');
  const dirty = !same(order, loaded.order) || !same([...exclude].sort(), [...loaded.exclude].sort());

  const save = () => {
    saveChain(personaId, toChain(order, exclude));
    setSavedFlash(true);
    setTimeout(() => setSavedFlash(false), 2000);
  };

  const kindBadge = (i: ChainItem) => {
    if (i.kind === 'pc') return t(i.pc === 'local' ? 'phoneChain.localPc' : 'phoneChain.webPc');
    if (i.kind === 'webchat') return t('phoneChain.webPhone');
    if (i.kind === 'gemma') return t('phoneChain.gemmaPhone');
    const p = i.provider;
    if (p?.core && p.phone) return t('chain.keyBoth');
    return t(p?.core ? 'chain.keyCore' : 'chain.keyPhone');
  };

  const model = (i: ChainItem) => (i.kind === 'api' ? (i.provider?.model ?? '') : i.kind === 'webchat' ? (i.site?.host ?? '') : '');

  return (
    <div className="card">
      <h2 className="card-title">{t('settings.providers')}</h2>
      <p className="field-hint" style={{ marginTop: 0 }}>
        {t(loaded.saved ? 'phoneChain.leadSaved' : 'phoneChain.lead')}
      </p>
      <ul className="memory-list">
        {order.map((id, idx) => {
          const i = byId.get(id);
          if (!i) return null;
          const isMain = idx === 0;
          return (
            <li key={id} className={`provider-item ${isMain ? 'prov-main' : ''}`}>
              <div className="provider-main">
                <span className="provider-name">{i.name}</span>
                {model(i) && <span className="provider-model">{model(i)}</span>}
                {isMain && <span className="badge badge--active">{t('settings.mainBadge')}</span>}
              </div>
              <div className="provider-side">
                <span className={`badge ${i.kind === 'api' ? 'badge--success' : ''}`}>{kindBadge(i)}</span>
                {i.kind === 'gemma' && gemma === false && <span className="badge badge--muted">{t('phoneChain.gemmaMissing')}</span>}
                {!isMain && (
                  <>
                    <button
                      className="btn btn--ghost"
                      title={t('settings.moveUp')}
                      aria-label={t('settings.moveUp')}
                      disabled={idx === 1}
                      onClick={() => setDraft({ order: moveIn(order, id, -1), exclude })}
                    >
                      ↑
                    </button>
                    <button
                      className="btn btn--ghost"
                      title={t('settings.moveDown')}
                      aria-label={t('settings.moveDown')}
                      disabled={idx === order.length - 1}
                      onClick={() => setDraft({ order: moveIn(order, id, 1), exclude })}
                    >
                      ↓
                    </button>
                    <button className="btn btn--ghost" onClick={() => setDraft({ order: makeFirst(order, id), exclude })}>
                      {t('settings.makeMain')}
                    </button>
                    <button
                      className="btn btn--ghost"
                      title={t('phoneChain.excludeHint')}
                      aria-label={t('phoneChain.excludeHint')}
                      onClick={() => setDraft(excludeIn(order, exclude, id))}
                    >
                      ✕
                    </button>
                  </>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      {order.length === 0 && <p className="field-hint">{t('providers.nobody')}</p>}

      {excluded.length > 0 && (
        <div className="prov-excluded">
          <div className="field-hint">{t('settings.excludedTitle')}</div>
          <ul className="memory-list">
            {excluded.map((i) => (
              <li key={i.id} className="provider-item prov-excluded-item">
                <div className="provider-main">
                  <span className="provider-name">{i.name}</span>
                  <span className="badge">{kindBadge(i)}</span>
                </div>
                <div className="provider-side">
                  <button
                    className="btn btn--ghost"
                    onClick={() => {
                      if (i.kind === 'pc') setNoCore(i.id);
                      else setDraft(restoreIn(order, exclude, i.id));
                    }}
                  >
                    {t('settings.restoreToChain')}
                  </button>
                </div>
                {noCore === i.id && (
                  <p className="field-hint provider-local-detail" role="alert" style={{ margin: 0 }}>
                    {t('ui.commentPrefix')}{t(i.pc === 'local' ? 'phoneChain.noCoreLocal' : 'phoneChain.noCoreWeb')}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="field-hint">{t('phoneChain.hint')}</div>
      <div className="dossier-confirm-actions" style={{ marginTop: 12 }}>
        <button className="btn btn--primary" onClick={save} disabled={!dirty}>
          {savedFlash ? '✓' : t('common.save')}
        </button>
        {loaded.saved && (
          <button className="btn btn--ghost" onClick={() => saveChain(personaId, null)}>
            {t('phoneChain.reset')}
          </button>
        )}
      </div>
    </div>
  );
}
