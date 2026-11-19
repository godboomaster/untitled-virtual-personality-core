import { useCallback, useEffect, useState } from 'react';
import { api, ApiError, isMissingRoute } from '../api';
import type { PersonaPhraseBank, PersonaPhraseGroup } from '../api';
import { useApiOnline } from '../apiData';
import { useI18n } from '../i18n';
import { useOfflineMode } from '../offline/state';
import Collapsible from './Collapsible';
import './personaPhrases.css';

type Draft = { group: PersonaPhraseGroup; variants: string[] };
// Досье перемонтируется при смене вкладки: набранное остаётся до сохранения
// или явной отмены. Черновики изолированы по персоне, затем по группе.
const draftsByPersona: Record<string, Record<string, Draft>> = {};

function readDrafts(personaId: string): Record<string, Draft> {
  if (draftsByPersona[personaId]) return draftsByPersona[personaId];
  try {
    const stored = JSON.parse(sessionStorage.getItem(`vpc-phrase-drafts:${personaId}`) ?? '{}');
    if (stored && typeof stored === 'object' && !Array.isArray(stored)) {
      const valid = Object.fromEntries(Object.entries(stored).filter(([id, draft]) => {
        const value = draft as Draft;
        return value?.group?.id === id && typeof value.group.revision === 'string'
          && Array.isArray(value.variants) && value.variants.every((v) => typeof v === 'string');
      }));
      return draftsByPersona[personaId] = valid as Record<string, Draft>;
    }
  } catch { /* память вкладки всё равно доступна */ }
  return draftsByPersona[personaId] = {};
}

export default function PersonaPhrasesPanel({ personaId }: { personaId: string }) {
  const { t } = useI18n();
  const online = useApiOnline();
  const offline = useOfflineMode();
  const connected = online && !offline;
  const [bank, setBank] = useState<PersonaPhraseBank | null>(null);
  const [drafts, setDrafts] = useState(() => readDrafts(personaId));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [groupErrors, setGroupErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [section, setSection] = useState('');
  const [mode, setMode] = useState('*');
  const [language, setLanguage] = useState('');
  const [query, setQuery] = useState('');

  const describeError = useCallback((e: unknown) => {
    if (isMissingRoute(e)) return t('common.coreOutdated');
    if (e instanceof ApiError && typeof e.data?.code === 'string') {
      return t(`phrases.error.${e.data.code}`);
    }
    return e instanceof Error ? e.message : String(e);
  }, [t]);

  useEffect(() => {
    if (!connected) return;
    let stale = false;
    setLoading(true);
    api.getPersonaPhrases(personaId)
      .then((value) => { if (!stale) setBank(value); })
      .catch((e) => { if (!stale) setError(describeError(e)); })
      .finally(() => { if (!stale) setLoading(false); });
    return () => { stale = true; };
  }, [connected, personaId, describeError]);

  useEffect(() => {
    if (!Object.keys(drafts).length) return;
    const preventLoss = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ''; };
    window.addEventListener('beforeunload', preventLoss);
    return () => window.removeEventListener('beforeunload', preventLoss);
  }, [drafts]);

  const storeDrafts = (next: Record<string, Draft>) => {
    draftsByPersona[personaId] = next;
    try {
      if (Object.keys(next).length) sessionStorage.setItem(`vpc-phrase-drafts:${personaId}`, JSON.stringify(next));
      else sessionStorage.removeItem(`vpc-phrase-drafts:${personaId}`);
    } catch { /* черновик остаётся в памяти */ }
    setDrafts(next);
  };
  const refresh = async () => {
    setLoading(true);
    setError('');
    try { setBank(await api.getPersonaPhrases(personaId)); }
    catch (e) { setError(describeError(e)); }
    finally { setLoading(false); }
  };
  const title = (group: PersonaPhraseGroup) =>
    t(`phrases.${group.section}.${group.key}`) +
    (group.bucket ? ` · ${t(`phrases.${group.bucket}`)}` : '');
  const edit = (group: PersonaPhraseGroup, index: number, text: string) => {
    const draft = drafts[group.id] ?? { group, variants: [...group.variants] };
    const variants = draft.variants.map((value, i) => i === index ? text : value);
    const next = { ...drafts };
    if (variants.every((value, i) => value === draft.group.variants[i])) delete next[group.id];
    else next[group.id] = { ...draft, variants };
    storeDrafts(next);
    setSaved(null);
    setGroupErrors((prev) => ({ ...prev, [group.id]: '' }));
  };
  const save = async (group: PersonaPhraseGroup) => {
    const draft = drafts[group.id];
    if (!draft || saving) return;
    setSaving(group.id);
    setGroupErrors((prev) => ({ ...prev, [group.id]: '' }));
    try {
      const result = await api.savePersonaPhrases(personaId, draft.group, draft.variants);
      setBank((prev) => prev ? { ...prev, groups: prev.groups.map((g) => g.id === group.id ? result.group : g) } : prev);
      const next = { ...(draftsByPersona[personaId] ?? {}) };
      delete next[group.id];
      storeDrafts(next);
      setSaved(group.id);
    } catch (e) {
      setGroupErrors((prev) => ({ ...prev, [group.id]: describeError(e) }));
    } finally { setSaving(null); }
  };

  const groups = bank?.groups ?? [];
  const modes = [...new Map(groups.map((g) => [g.mode, g.mode_name])).entries()];
  const term = query.trim().toLocaleLowerCase();
  const visible = groups.filter((g) => (!section || g.section === section)
    && (mode === '*' || g.mode === mode) && (!language || g.language === language)
    && (!term || [title(g), g.mode_name, ...(drafts[g.id]?.variants ?? g.variants)]
      .some((value) => value.toLocaleLowerCase().includes(term))));
  const count = groups.reduce((sum, g) => sum + g.variants.length, 0);

  return <div className="card phrase-panel">
    <div className="card-title-row">
      <h3 className="card-title">{t('phrases.title')}</h3>
      {bank && <span className="badge">{t('phrases.count', { n: count })}</span>}
      <button type="button" className="btn btn--ghost" disabled={!connected || loading || !!saving}
        onClick={() => void refresh()}>{loading ? t('phrases.loading') : t('phrases.refresh')}</button>
    </div>
    <p className="field-hint">{t('phrases.hint')}</p>
    {!connected ? <p className="files-empty">{t('phrases.needCore')}</p> : <>
      {error && <p className="phrase-error" role="alert">{error}</p>}
      {bank?.generating && <p className="field-hint" role="status">{t('phrases.generating')}</p>}
      {Object.keys(drafts).length > 0 && <p className="field-hint">{t('phrases.dirty', { n: Object.keys(drafts).length })}</p>}
      {groups.length > 0 && <div className="phrase-filters">
        <label>{t('phrases.section')}<select className="input" value={section} onChange={(e) => setSection(e.target.value)}>
          <option value="">{t('phrases.all')}</option>
          {(['errors', 'phrases', 'kinds'] as const).map((value) => <option key={value} value={value}>{t(`phrases.section.${value}`)}</option>)}
        </select></label>
        <label>{t('phrases.mode')}<select className="input" value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="*">{t('phrases.all')}</option>
          {modes.map(([value, name]) => <option key={value} value={value}>{name || t('phrases.base')}</option>)}
        </select></label>
        <label>{t('phrases.language')}<select className="input" value={language} onChange={(e) => setLanguage(e.target.value)}>
          <option value="">{t('phrases.all')}</option><option value="ru">Русский</option><option value="en">English</option>
        </select></label>
        <label>{t('phrases.search')}<input className="input" type="search" value={query}
          placeholder={t('phrases.searchPlaceholder')} onChange={(e) => setQuery(e.target.value)} /></label>
      </div>}
      {!loading && !groups.length && <p className="files-empty">{t('phrases.empty')}</p>}
      {groups.length > 0 && !visible.length && <p className="files-empty">{t('phrases.noMatches')}</p>}
      <div className="phrase-groups">{visible.map((group, index) => {
        const draft = drafts[group.id];
        const values = draft?.variants ?? group.variants;
        return <div className="phrase-group" key={group.id}>
          <Collapsible title={title(group)} defaultOpen={index === 0}
            summary={`${group.mode_name || t('phrases.base')} · ${group.language.toUpperCase()} · ${values.length}`}>
            <div className="phrase-group-meta">
              <span className="badge">{t(`phrases.section.${group.section}`)}</span>
              {group.edited && <span className="badge">{t('phrases.edited')}</span>}
              {group.stale && <span className="field-hint">{t('phrases.stale')}</span>}
            </div>
            {group.placeholders.length > 0 && <p className="field-hint">
              {t('phrases.placeholders', { values: group.placeholders.map((p) => `{${p}}`).join(', ') })}
              {group.required_placeholders.length > 0 && <> {t('phrases.required', { values: group.required_placeholders.map((p) => `{${p}}`).join(', ') })}</>}
            </p>}
            {values.map((value, i) => <label className="phrase-variant" key={i}>
              <span>{t('phrases.variant', { n: i + 1 })}</span>
              <textarea className="input" value={value} rows={Math.min(5, Math.max(2, Math.ceil(value.length / 90)))}
                maxLength={group.max_length} disabled={saving === group.id}
                aria-label={`${title(group)} · ${group.mode_name || t('phrases.base')} · ${group.language.toUpperCase()} · ${t('phrases.variant', { n: i + 1 })}`}
                onChange={(e) => edit(group, i, e.target.value)} />
            </label>)}
            <div className="phrase-group-actions">
              <button type="button" className="btn btn--primary" disabled={!draft || !!saving || values.some((v) => !v.trim())}
                onClick={() => void save(group)}>{saving === group.id ? t('yaml.saving') : t('common.save')}</button>
              {draft && <button type="button" className="btn btn--ghost" disabled={!!saving} onClick={() => {
                const next = { ...drafts }; delete next[group.id]; storeDrafts(next);
                setGroupErrors((prev) => ({ ...prev, [group.id]: '' }));
              }}>{t('common.cancel')}</button>}
              {saved === group.id && <span role="status">{t('phrases.saved')}</span>}
              {groupErrors[group.id] && <span className="phrase-error" role="alert">{groupErrors[group.id]}</span>}
            </div>
          </Collapsible>
        </div>;
      })}</div>
    </>}
  </div>;
}
