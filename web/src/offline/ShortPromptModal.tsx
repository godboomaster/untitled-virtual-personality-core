import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import { api, isNativeApp } from '../api';
import type { ShortPrompt } from '../api';
import { useApiOnline } from '../apiData';
import FormModal from '../components/FormModal';
import { getKit } from './kit';
import { localCharacter } from './prompt';

/* Краткое описание персоны для модели телефона (app/features/short_prompt.py)
   — посмотреть. Ноутбук на связи — с него (не составлено — начнёт
   составляться); нет — из данных персоны на телефоне, и там же видно, что
   получит Gemma, пока описания нет: начало полного характера. */

interface View {
  text: string; // что видно в окне: описание или (на телефоне) замена ему
  status: ShortPrompt['status'];
  source: ShortPrompt['source'];
  at: number; // с, когда составлено
  chars: number; // символов в полном характере
  error?: string;
  phoneAt?: number; // мс: копия на телефоне — от этого времени
  cut?: boolean; // описания нет — показано начало полного характера
}

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
    phoneAt: kit.fetched_at,
    cut: !short,
  };
}

export default function ShortPromptModal({ persona, name, onClose }: { persona: string; name: string; onClose: () => void }) {
  const { t, lang } = useI18n();
  const online = useApiOnline();
  const [view, setView] = useState<View | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let alive = true;
    const phone = () => (isNativeApp() ? fromKit(persona) : null);
    setLoading(true);
    const done = (v: View | null) => {
      if (!alive) return;
      setView(v);
      setLoading(false);
    };
    if (!online) {
      done(phone());
      return;
    }
    api
      .getShortPrompt(persona)
      .then((r) => {
        const kit = phone();
        // Не готово — на телефоне видно, что модель получает взамен
        const cut = r.status !== 'ready' && kit?.cut ? kit.text : '';
        done({ text: r.text || cut, status: r.status, source: r.source, at: r.at, chars: r.chars ?? 0, error: r.error, cut: !!cut });
      })
      .catch(() => done(phone()));
    return () => {
      alive = false;
    };
  }, [persona, online, tick]);

  const locale = lang === 'ru' ? 'ru-RU' : 'en-US';
  const date = (ms: number) =>
    new Date(ms).toLocaleString(locale, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });

  let status = '';
  if (view) {
    if (view.source === 'yaml') status = t('shortPrompt.yaml');
    else if (view.status === 'ready') status = view.at ? t('shortPrompt.auto', { date: date(view.at * 1000) }) : t('shortPrompt.autoNoDate');
    else if (view.status === 'pending') status = t('shortPrompt.pending');
    else if (view.status === 'failed') status = t('shortPrompt.failed', { err: view.error || '—' });
    else status = view.cut ? t('shortPrompt.notYet') : t('shortPrompt.none');
    if (view.cut && view.text) status += ` ${t('shortPrompt.cut')}`;
  }

  return (
    <FormModal
      title={t('shortPrompt.title', { name })}
      wide
      onClose={onClose}
      onSubmit={online && view?.status === 'pending' ? () => setTick((x) => x + 1) : undefined}
      submitLabel={t('shortPrompt.refresh')}
    >
      {loading && <p className="field-hint">{t('shortPrompt.loading')}</p>}
      {!loading && !view && <p className="field-hint">{t('shortPrompt.unavailable')}</p>}
      {!loading && view && (
        <>
          <p className="field-hint">{status}</p>
          {view.text && (
            <>
              <pre className="short-prompt-text">{view.text}</pre>
              <p className="field-hint">
                {view.chars
                  ? t('shortPrompt.size', { n: view.text.length, full: view.chars })
                  : t('shortPrompt.sizeOnly', { n: view.text.length })}
              </p>
            </>
          )}
          {view.phoneAt != null && <p className="field-hint">{t('shortPrompt.phoneCopy', { date: date(view.phoneAt) })}</p>}
          <p className="field-hint">{t('shortPrompt.hint')}</p>
        </>
      )}
    </FormModal>
  );
}
