import { useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import { api, isNativeApp } from '../api';
import type { ShortPrompt } from '../api';
import { useApiOnline } from '../apiData';
import FormModal from '../components/FormModal';
import { copyText } from '../clipboard';
import { getKit, patchKitShort } from './kit';
import { isLocalPersona } from './localPersonas';
import { localCharacter } from './prompt';
import { manualPrompt, OWN_MAX, pendingShort, setPendingShort } from './shortPrompt';
import type { PendingShort } from './shortPrompt';
import { ComposeFailed, composeOnPhone } from './shortCompose';
import { useOfflineMode } from './state';

/* Краткое описание персоны для модели телефона (app/features/short_prompt.py).
   Два пути:
   - «Составит модель персоны» — как было: модель из цепочки провайдеров
     персоны (кроме локальной) сжимает полный характер; «Составить заново» —
     ещё раз;
   - «Своё описание» — вставить текст: промпт карточки (тот же, что уходит
     модели) копируется в любую нейросеть, её ответ вставляется сюда; или
     написать самому.
   Ядро на связи — всё на нём (своё — поле short_prompt в YAML). Нет — на
   телефоне: своё и составленное его цепочкой (облако, веб-чаты) ждут ядра в
   очереди (shortPrompt.ts), Gemma получает их сразу. */

type Way = 'model' | 'own';

interface View {
  text: string; // что видно в окне: описание или (на телефоне) замена ему
  status: ShortPrompt['status'];
  source: ShortPrompt['source'];
  at: number; // с, когда составлено
  chars: number; // символов в полном характере
  error?: string;
  composing?: boolean; // готовое есть, модель составляет новое
  prompt: string; // запрос карточки для своей нейросети
  phoneAt?: number; // мс: копия на телефоне — от этого времени
  cut?: boolean; // описания нет — показано начало полного характера
  pending?: PendingShort | null; // без ядра: правка ждёт ядра
}

const POLL_MS = 4000; // пока составляется — заглядывать на ядро

function fromKit(persona: string): View | null {
  const kit = getKit(persona);
  if (!kit || kit.fallback) return null;
  const short = kit.parts.short?.trim();
  return {
    text: localCharacter(kit),
    status: short ? 'ready' : (kit.short_prompt?.status ?? 'none'),
    source: short ? (kit.short_prompt?.source ?? 'auto') : '',
    at: kit.short_prompt?.at ?? 0,
    chars: kit.parts.character.length,
    prompt: manualPrompt(kit.parts.character.trim()),
    phoneAt: kit.fetched_at || undefined,
    cut: !short,
    pending: pendingShort(persona),
  };
}

function fromCore(r: ShortPrompt, kit: View | null): View {
  // Не готово — на телефоне видно, что модель получает взамен
  const cut = r.status !== 'ready' && kit?.cut ? kit.text : '';
  return {
    text: r.text || cut,
    status: r.status,
    source: r.source,
    at: r.at,
    chars: r.chars ?? 0,
    error: r.error,
    composing: r.composing,
    prompt: r.prompt ?? kit?.prompt ?? '',
    cut: !!cut,
  };
}

export default function ShortPromptModal({ persona, name, onClose }: { persona: string; name: string; onClose: () => void }) {
  const { t, lang } = useI18n();
  const native = isNativeApp();
  // Ядро на связи и персона на нём — всё через ядро; созданная на телефоне
  // и ещё не перенесённая — как без связи. Список персон остаётся и после
  // обрыва связи — «без ядра» по проверке связи, иначе окно ждало бы ядро
  const offlineMode = useOfflineMode();
  const online = useApiOnline() && !offlineMode && !(native && isLocalPersona(persona));
  const [view, setView] = useState<View | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const [way, setWay] = useState<Way | null>(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const [showPrompt, setShowPrompt] = useState(false);
  const started = useRef(false); // вкладка и черновик — по первому ответу

  const apply = (v: View | null) => {
    setView(v);
    setLoading(false);
    if (v && !started.current) {
      started.current = true;
      const own = v.source === 'yaml';
      setWay(own ? 'own' : 'model');
      setDraft(own ? v.text : '');
    }
  };

  // Ответ ядра: и в окно, и в кит телефона — Gemma получит описание сразу
  const fromResponse = (r: ShortPrompt) => {
    if (native) patchKitShort(persona, r);
    apply(fromCore(r, native ? fromKit(persona) : null));
  };

  useEffect(() => {
    let alive = true;
    const phone = () => (native ? fromKit(persona) : null);
    if (!online) {
      apply(phone());
      return;
    }
    api
      .getShortPrompt(persona)
      .then((r) => {
        if (!alive) return;
        if (native) patchKitShort(persona, r);
        apply(fromCore(r, phone()));
      })
      .catch(() => alive && apply(phone()));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [persona, online, tick, native]);

  // Составляется на ядре — заглядывать, пока не готово
  const waiting = online && !!view && (view.status === 'pending' || !!view.composing);
  useEffect(() => {
    if (!waiting) return;
    const id = window.setTimeout(() => setTick((x) => x + 1), POLL_MS);
    return () => window.clearTimeout(id);
  }, [waiting, tick]);

  const locale = lang === 'ru' ? 'ru-RU' : 'en-US';
  const date = (ms: number) =>
    new Date(ms).toLocaleString(locale, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });

  let status = '';
  if (view) {
    const p = view.pending;
    if (!online && p?.source === 'yaml') status = p.text.trim() ? t('shortPrompt.phoneOwn') : t('shortPrompt.phoneOwnRemoved');
    else if (!online && p?.source === 'auto' && view.source === 'auto')
      status = t('shortPrompt.phoneAuto', { who: p.provider || '—', date: date(p.at) });
    else if (view.source === 'yaml') status = t('shortPrompt.yaml');
    else if (view.status === 'ready') {
      status = view.at ? t('shortPrompt.auto', { date: date(view.at * 1000) }) : t('shortPrompt.autoNoDate');
      if (view.composing) status += ` ${t('shortPrompt.recomposing')}`;
      else if (view.error) status += ` ${t('shortPrompt.recomposeFailed', { err: view.error })}`;
    } else if (view.status === 'pending') status = t('shortPrompt.pending');
    else if (view.status === 'failed') status = t('shortPrompt.failed', { err: view.error || '—' });
    else status = view.cut ? t('shortPrompt.notYet') : t('shortPrompt.none');
    if (view.cut && view.text) status += ` ${t('shortPrompt.cut')}`;
  }

  const own = view?.source === 'yaml' ? view.text : '';
  const modelText = view && view.source !== 'yaml' ? view.text : '';
  const autoReady = !!view && view.source === 'auto' && view.status === 'ready' && !view.cut;

  const run = async (job: () => Promise<void>) => {
    setBusy(true);
    setError('');
    try {
      await job();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // Своё описание (пустое — убрать своё, составит модель)
  const saveOwn = (text: string) =>
    run(async () => {
      const value = text.trim();
      if (online) {
        fromResponse(await api.setShortPrompt(persona, value));
      } else {
        setPendingShort(persona, { text: value, source: 'yaml', at: Date.now() });
        apply(fromKit(persona));
      }
      setDraft(value);
      if (!value) setWay('model');
    });

  const compose = () =>
    run(async () => {
      setDraft(''); // своё убирается — в поле его больше нет
      if (online) {
        fromResponse(await api.composeShortPrompt(persona));
        return;
      }
      try {
        await composeOnPhone(persona);
      } catch (e) {
        if (!(e instanceof ComposeFailed)) throw e;
        const key = e.reason === 'bad_answer' ? 'shortPrompt.phoneBad' : e.reason === 'no_kit' ? 'shortPrompt.unavailable' : 'shortPrompt.phoneNoRoute';
        throw new Error(t(key));
      }
      apply(fromKit(persona));
    });

  const copyPrompt = async () => {
    if (!view?.prompt) return;
    const ok = await copyText(view.prompt);
    setError(ok ? '' : t('shortPrompt.copyFailed'));
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  // Характер есть (промпт из него) — составить можно, даже если описания ещё нет
  const canCompose = !!view?.prompt && !waiting && !busy && (online || native);
  const draftValue = draft.trim();
  let submit: (() => void) | undefined;
  let submitLabel = '';
  let submitDisabled = false;
  if (view && way === 'model') {
    submit = () => void compose();
    submitLabel = busy ? t('shortPrompt.composing') : autoReady || view.source === 'yaml' ? t('shortPrompt.recompose') : t('shortPrompt.compose');
    submitDisabled = !canCompose;
  } else if (view && way === 'own') {
    submit = () => void saveOwn(draft);
    submitLabel = t('common.save');
    submitDisabled = busy || !draftValue || draftValue === own.trim();
  }

  return (
    <FormModal
      title={t('shortPrompt.title', { name })}
      wide
      onClose={onClose}
      onSubmit={submit}
      submitLabel={submitLabel}
      submitDisabled={submitDisabled}
      cancelLabel={t('common.close')}
    >
      {loading && <p className="field-hint">{t('shortPrompt.loading')}</p>}
      {!loading && !view && <p className="field-hint">{t('shortPrompt.unavailable')}</p>}
      {!loading && view && (
        <>
          <p className="field-hint">{status}</p>

          <div className="tabs short-prompt-tabs" role="tablist">
            {(['model', 'own'] as const).map((w) => (
              <button
                key={w}
                type="button"
                role="tab"
                aria-selected={way === w}
                className={`tab${way === w ? ' tab--active' : ''}`}
                onClick={() => {
                  setWay(w);
                  setError('');
                }}
              >
                {t(w === 'model' ? 'shortPrompt.wayModel' : 'shortPrompt.wayOwn')}
              </button>
            ))}
          </div>

          {way === 'model' && (
            <>
              <p className="field-hint">{online ? t('shortPrompt.modelHint') : t('shortPrompt.modelHintPhone')}</p>
              {view.source === 'yaml' && <p className="field-hint">{t('shortPrompt.ownActive')}</p>}
              {modelText && (
                <>
                  <pre className="short-prompt-text">{modelText}</pre>
                  <p className="field-hint">
                    {view.chars
                      ? t('shortPrompt.size', { n: modelText.length, full: view.chars })
                      : t('shortPrompt.sizeOnly', { n: modelText.length })}
                  </p>
                </>
              )}
            </>
          )}

          {way === 'own' && (
            <>
              <ol className="short-prompt-steps">
                <li>{t('shortPrompt.ownStep1')}</li>
                <li>{t('shortPrompt.ownStep2')}</li>
              </ol>
              <div className="short-prompt-actions">
                <button type="button" className="btn btn--ghost" disabled={!view.prompt} onClick={() => void copyPrompt()}>
                  {copied ? t('common.copied') : t('shortPrompt.copyPrompt')}
                </button>
                <button type="button" className="btn btn--ghost" disabled={!view.prompt} onClick={() => setShowPrompt((x) => !x)}>
                  {showPrompt ? t('shortPrompt.hidePrompt') : t('shortPrompt.showPrompt')}
                </button>
              </div>
              {showPrompt && view.prompt && <pre className="short-prompt-text short-prompt-prompt">{view.prompt}</pre>}
              <textarea
                className="input pcreate-textarea short-prompt-input"
                rows={10}
                value={draft}
                maxLength={OWN_MAX}
                placeholder={t('shortPrompt.ownPh')}
                onChange={(e) => setDraft(e.target.value)}
              />
              <p className="field-hint">{t('shortPrompt.ownSize', { n: draftValue.length })}</p>
              <div className="short-prompt-actions">
                {autoReady && modelText && draftValue !== modelText.trim() && (
                  <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => setDraft(modelText)}>
                    {t('shortPrompt.takeModel')}
                  </button>
                )}
                {own && (
                  <button type="button" className="btn btn--ghost" disabled={busy} onClick={() => void saveOwn('')}>
                    {t('shortPrompt.removeOwn')}
                  </button>
                )}
              </div>
              <p className="field-hint">{t('shortPrompt.ownHint')}</p>
            </>
          )}

          {error && <p className="field-hint short-prompt-error">{error}</p>}
          {!online && view.phoneAt != null && <p className="field-hint">{t('shortPrompt.phoneCopy', { date: date(view.phoneAt) })}</p>}
          <p className="field-hint">{t('shortPrompt.hint')}</p>
        </>
      )}
    </FormModal>
  );
}
