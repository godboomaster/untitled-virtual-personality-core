import { useEffect, useState } from 'react';
import { SKINS, getSkin } from '../../skins';
import { useI18n, useMockData } from '../i18n';
import Select from './Select';
import { OPEN_CHAT_EVENT } from '../notifications';
import {
  PERSONA_DEFAULT,
  resolveScheme,
  setGlobalSkin,
  setPersonaSkin,
  usePersonaSkinScope,
  useSkinThemeState,
} from '../skinTheme';
import type { AppTheme } from '../useAppTheme';

/* Галерея тем оформления (web/skins): выбрать тему на всё приложение или
   свою для одной персоны. В режиме «персона» галерея сама ведёт себя как
   экран этой персоны — выбранная тема видна сразу, не уходя в её чат.
   Миниатюры (.skin-swatch) рисует swatch.css каждого скина. */

type Target = 'app' | 'persona';

export function SkinSwatch({ id, scheme }: { id: string | null; scheme: AppTheme }) {
  return (
    <span className="skin-swatch" data-swatch={id ?? 'default'} data-scheme={scheme} aria-hidden="true">
      <span className="skin-swatch-bar">
        <i />
        <i />
        <i />
      </span>
      <span className="skin-swatch-body">
        <span className="skin-swatch-side">
          <i />
          <i />
          <i />
          <i />
        </span>
        <span className="skin-swatch-main">
          <span className="skin-swatch-title">Aa</span>
          <span className="skin-swatch-msg skin-swatch-msg--in" />
          <span className="skin-swatch-msg skin-swatch-msg--out" />
          <span className="skin-swatch-input">
            <i className="skin-swatch-send" />
          </span>
        </span>
      </span>
    </span>
  );
}

export default function SkinThemeGallery() {
  const { t, lang } = useI18n();
  const { personas } = useMockData();
  const st = useSkinThemeState();
  const [target, setTarget] = useState<Target>('app');
  const [personaId, setPersonaId] = useState<string>(() => personas[0]?.id ?? '');

  // Список персон пришёл позже или выбранную удалили — берём первую
  useEffect(() => {
    if (!personas.some((p) => p.id === personaId)) setPersonaId(personas[0]?.id ?? '');
  }, [personas, personaId]);

  const personaMode = target === 'persona' && personaId !== '';
  usePersonaSkinScope(personaMode ? personaId : null);

  const own = personaMode ? st.personas[personaId] : undefined;
  // Что выбрано для текущей цели: id скина, null — стандартный вид
  const selected: string | null = personaMode
    ? own === PERSONA_DEFAULT
      ? null
      : own ?? st.global
    : st.global;
  const inherits = personaMode && own === undefined;

  const choose = (id: string | null) => {
    if (personaMode) setPersonaSkin(personaId, id ?? PERSONA_DEFAULT);
    else setGlobalSkin(id);
  };

  const nameOf = (id: string | null) => {
    const skin = getSkin(id);
    return skin ? skin.name[lang] : t('skinTheme.default');
  };

  const usersOf = (id: string | null) =>
    personas.filter((p) => {
      const v = st.personas[p.id];
      return id == null ? v === PERSONA_DEFAULT : v === id;
    });

  const assigned = personas.filter((p) => st.personas[p.id] !== undefined);

  const cards: { id: string | null; name: string; tagline: string; schemes: AppTheme[] }[] = [
    { id: null, name: t('skinTheme.default'), tagline: t('skinTheme.defaultTagline'), schemes: ['light', 'dark'] },
    ...SKINS.map((s) => ({ id: s.id, name: s.name[lang], tagline: s.tagline[lang], schemes: s.schemes })),
  ];

  return (
    <div className="card skin-themes">
      <div className="card-title-row">
        <h3 className="card-title">{t('skinTheme.title')}</h3>
      </div>
      <p className="skin-themes-lead">{t('skinTheme.lead')}</p>

      <div className="skin-themes-target">
        <div className="tabs skin-themes-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={target === 'app'}
            className={`tab${target === 'app' ? ' tab--active' : ''}`}
            onClick={() => setTarget('app')}
          >
            {t('skinTheme.targetApp')}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={target === 'persona'}
            className={`tab${target === 'persona' ? ' tab--active' : ''}`}
            onClick={() => setTarget('persona')}
            disabled={personas.length === 0}
          >
            {t('skinTheme.targetPersona')}
          </button>
        </div>
        {target === 'persona' && personas.length > 0 && (
          <div className="skin-themes-persona">
            <Select
              className="skin-themes-persona-select"
              value={personaId}
              options={personas.map((p) => ({ value: p.id, label: p.name }))}
              onChange={setPersonaId}
              title={t('skinTheme.personaPick')}
            />
            <button
              type="button"
              className={`btn btn--ghost skin-themes-inherit${inherits ? ' skin-themes-inherit--on' : ''}`}
              aria-pressed={inherits}
              onClick={() => setPersonaSkin(personaId, null)}
            >
              {t('skinTheme.inherit', { name: nameOf(st.global) })}
            </button>
            <button
              type="button"
              className="btn btn--chip"
              onClick={() => window.dispatchEvent(new CustomEvent(OPEN_CHAT_EVENT, { detail: personaId }))}
            >
              {t('skinTheme.openChat')}
            </button>
          </div>
        )}
        {target === 'persona' && <p className="skin-themes-hint">{t('skinTheme.personaHint')}</p>}
      </div>

      <div className="skin-themes-grid">
        {cards.map((c) => {
          const active = !inherits && c.id === selected;
          const scheme = resolveScheme(st.preference, c.id);
          const users = usersOf(c.id);
          return (
            <button
              key={c.id ?? 'default'}
              type="button"
              className={`skin-theme-card${active ? ' skin-theme-card--active' : ''}`}
              aria-pressed={active}
              onClick={() => choose(c.id)}
            >
              <SkinSwatch id={c.id} scheme={scheme} />
              <span className="skin-theme-card-name">{c.name}</span>
              {c.tagline && <span className="skin-theme-card-tagline">{c.tagline}</span>}
              <span className="skin-theme-card-meta">
                {c.schemes.map((s) => (
                  <span key={s} className="badge skin-theme-card-scheme">
                    {t(`skinTheme.scheme.${s}`)}
                  </span>
                ))}
                {st.global === c.id && c.id != null && <span className="badge badge--active">{t('skinTheme.everywhere')}</span>}
                {users.map((p) => (
                  <span key={p.id} className="badge badge--user">
                    {p.name}
                  </span>
                ))}
              </span>
            </button>
          );
        })}
      </div>

      {assigned.length > 0 && (
        <div className="skin-themes-assigned">
          <div className="skin-themes-assigned-title">{t('skinTheme.assigned')}</div>
          {assigned.map((p) => (
            <div key={p.id} className="skin-themes-assigned-row">
              <span className="skin-themes-assigned-name">{p.name}</span>
              <span className="skin-themes-assigned-skin">
                {st.personas[p.id] === PERSONA_DEFAULT ? t('skinTheme.default') : nameOf(st.personas[p.id])}
              </span>
              <button type="button" className="btn btn--icon" onClick={() => setPersonaSkin(p.id, null)} title={t('skinTheme.resetTitle')}>
                {t('skinTheme.reset')}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
