import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import { api } from '../api';
import { useApiOnline } from '../apiData';
import InfoButton from '../components/InfoButton';

/* Управление компьютером (досье → «Настройки»): allowlist'ы сайтов,
   приложений, поисковых шаблонов и задач персоны. Пишется в
   features.computer_control YAML персоны; на живом боте применяется сразу,
   без перезапуска. Без бэкенда карточка не рендерится (мок не редактируем). */

type Row = { k: string; v: string };
type SearchRow = { k: string; url: string; first: string };

// Значение apps/tasks может быть per-OS словарём: в поле показываем JSON,
// при сохранении парсим обратно (строка без «{» остаётся строкой)
const packVal = (v: unknown): string =>
  v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v ?? '');
const unpackVal = (s: string): unknown => {
  const v = s.trim();
  if (v.startsWith('{')) {
    try {
      return JSON.parse(v);
    } catch {
      /* кривой JSON — сохраняем как строку */
    }
  }
  return v;
};

const rowsFrom = (obj: unknown): Row[] =>
  obj !== null && typeof obj === 'object'
    ? Object.entries(obj as Record<string, unknown>).map(([k, v]) => ({ k, v: packVal(v) }))
    : [];

const searchRowsFrom = (obj: unknown): SearchRow[] =>
  obj !== null && typeof obj === 'object'
    ? Object.entries(obj as Record<string, unknown>).map(([k, v]) =>
        v !== null && typeof v === 'object'
          ? {
              k,
              url: String((v as Record<string, unknown>).url ?? ''),
              first: String((v as Record<string, unknown>).first ?? ''),
            }
          : { k, url: String(v ?? ''), first: '' })
    : [];

function RowList({
  rows,
  onChange,
  keyPh,
  valPh,
}: {
  rows: Row[];
  onChange: (next: Row[]) => void;
  keyPh: string;
  valPh: string;
}) {
  return (
    <>
      <ul className="memory-list">
        {rows.map((r, i) => (
          <li key={i} className="pmodel-row" style={{ display: 'flex', gap: 6 }}>
            <input
              className="input"
              style={{ maxWidth: 170 }}
              value={r.k}
              placeholder={keyPh}
              spellCheck={false}
              onChange={(e) => {
                const n = [...rows];
                n[i] = { ...r, k: e.target.value };
                onChange(n);
              }}
            />
            <input
              className="input"
              style={{ flex: 1 }}
              value={r.v}
              placeholder={valPh}
              spellCheck={false}
              onChange={(e) => {
                const n = [...rows];
                n[i] = { ...r, v: e.target.value };
                onChange(n);
              }}
            />
            <button type="button" className="btn btn--ghost" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
              ✕
            </button>
          </li>
        ))}
      </ul>
      <button type="button" className="btn btn--ghost" onClick={() => onChange([...rows, { k: '', v: '' }])}>
        ＋
      </button>
    </>
  );
}

export default function ComputerControl({ personaId }: { personaId: string }) {
  const { t } = useI18n();
  const apiOnline = useApiOnline();
  const [loaded, setLoaded] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [confirm, setConfirm] = useState(true);
  const [clickEnabled, setClickEnabled] = useState(true);
  const [sites, setSites] = useState<Row[]>([]);
  const [apps, setApps] = useState<Row[]>([]);
  const [tasks, setTasks] = useState<Row[]>([]);
  const [search, setSearch] = useState<SearchRow[]>([]);
  // Нередактируемые здесь ключи конфига (allow_domains и пр.) — возвращаем как были
  const [rest, setRest] = useState<Record<string, unknown>>({});
  const [dirty, setDirty] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);

  useEffect(() => {
    if (!apiOnline) return;
    let stale = false;
    setLoaded(false);
    setDirty(false);
    api
      .getPersonaConfig(personaId)
      .then((c) => {
        if (stale) return;
        const cc = c.features?.computer_control;
        const d = cc !== null && typeof cc === 'object' ? (cc as Record<string, unknown>) : {};
        // dict с enabled: false — режим выключен через «Фичи», списки сохранены
        setEnabled(!!cc && d.enabled !== false);
        setConfirm(d.confirm !== false);
        setClickEnabled(d.click !== false);
        setSites(rowsFrom(d.sites));
        setApps(rowsFrom(d.apps));
        setTasks(rowsFrom(d.tasks));
        setSearch(searchRowsFrom(d.search));
        const { confirm: _c, click: _cl, enabled: _e, sites: _s, apps: _a, tasks: _t, search: _q, ...restKeys } = d;
        setRest(restKeys);
        setLoaded(true);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [apiOnline, personaId]);

  if (!apiOnline || !loaded) return null;

  const wrap = <T,>(setter: (v: T) => void) => (v: T) => {
    setter(v);
    setDirty(true);
  };

  const save = (nextEnabled = enabled) => {
    const obj = (rows: Row[], unpack: boolean): Record<string, unknown> => {
      const out: Record<string, unknown> = {};
      rows.forEach((r) => {
        if (r.k.trim() && r.v.trim()) out[r.k.trim().toLowerCase()] = unpack ? unpackVal(r.v) : r.v.trim();
      });
      return out;
    };
    const searchObj: Record<string, unknown> = {};
    search.forEach((r) => {
      if (!r.k.trim() || !r.url.trim()) return;
      searchObj[r.k.trim().toLowerCase()] = r.first.trim()
        ? { url: r.url.trim(), first: r.first.trim() }
        : r.url.trim();
    });
    // Выключение — enabled: false внутри конфига (а не computer_control: false):
    // allowlist'ы остаются в YAML и вернутся при повторном включении
    const cfgOut: Record<string, unknown> = {
      ...rest,
      confirm,
      click: clickEnabled,
      sites: obj(sites, false),
      apps: obj(apps, true),
      tasks: obj(tasks, true),
      search: searchObj,
    };
    if (!nextEnabled) cfgOut.enabled = false;
    api
      .updatePersonaConfig(personaId, { features: { computer_control: cfgOut } })
      .then(() => {
        setDirty(false);
        if (nextEnabled) {
          setSavedFlash(true);
          setTimeout(() => setSavedFlash(false), 2000);
        }
      })
      .catch(() => {});
  };

  return (
    <div className="card">
      <h2 className="card-title">
        {t('computer.title')}
        <InfoButton helpKey="computer.title" />
      </h2>
      {!enabled ? (
        <>
          <div className="field-hint">{t('computer.disabledHint')}</div>
          <button
            type="button"
            className="btn btn--primary"
            style={{ marginTop: 10 }}
            onClick={() => {
              setEnabled(true);
              save(true);
            }}
          >
            {t('computer.enable')}
          </button>
        </>
      ) : (
        <>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={confirm}
              onChange={(e) => {
                setConfirm(e.target.checked);
                setDirty(true);
              }}
            />
            <span>{t('computer.confirm')}</span>
            <InfoButton helpKey="computer.confirm" />
          </label>

          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={clickEnabled}
              onChange={(e) => {
                setClickEnabled(e.target.checked);
                setDirty(true);
              }}
            />
            <span>{t('computer.click')}</span>
            <InfoButton helpKey="computer.click" />
          </label>

          <div className="field-label" style={{ marginTop: 12 }}>
            {t('computer.sites')}
            <InfoButton helpKey="computer.sites" />
          </div>
          <RowList rows={sites} onChange={wrap(setSites)} keyPh={t('computer.keyPh')} valPh={t('computer.sitePh')} />

          <div className="field-label" style={{ marginTop: 12 }}>
            {t('computer.apps')}
            <InfoButton helpKey="computer.apps" />
          </div>
          <RowList rows={apps} onChange={wrap(setApps)} keyPh={t('computer.keyPh')} valPh={t('computer.appPh')} />

          <div className="field-label" style={{ marginTop: 12 }}>
            {t('computer.search')}
            <InfoButton helpKey="computer.search" />
          </div>
          <ul className="memory-list">
            {search.map((r, i) => {
              const upd = (patch: Partial<SearchRow>) => {
                const n = [...search];
                n[i] = { ...r, ...patch };
                setSearch(n);
                setDirty(true);
              };
              return (
                <li key={i} className="pmodel-row" style={{ display: 'flex', gap: 6 }}>
                  <input
                    className="input"
                    style={{ maxWidth: 130 }}
                    value={r.k}
                    placeholder={t('computer.keyPh')}
                    spellCheck={false}
                    onChange={(e) => upd({ k: e.target.value })}
                  />
                  <input
                    className="input"
                    style={{ flex: 1 }}
                    value={r.url}
                    placeholder={t('computer.urlPh')}
                    spellCheck={false}
                    onChange={(e) => upd({ url: e.target.value })}
                  />
                  <input
                    className="input"
                    style={{ flex: 1 }}
                    value={r.first}
                    placeholder={t('computer.firstPh')}
                    spellCheck={false}
                    onChange={(e) => upd({ first: e.target.value })}
                  />
                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => {
                      setSearch(search.filter((_, j) => j !== i));
                      setDirty(true);
                    }}
                  >
                    ✕
                  </button>
                </li>
              );
            })}
          </ul>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => {
              setSearch([...search, { k: '', url: '', first: '' }]);
              setDirty(true);
            }}
          >
            ＋
          </button>

          <div className="field-label" style={{ marginTop: 12 }}>
            {t('computer.tasks')}
            <InfoButton helpKey="computer.tasks" />
          </div>
          <RowList rows={tasks} onChange={wrap(setTasks)} keyPh={t('computer.keyPh')} valPh={t('computer.taskPh')} />

          <div className="dossier-confirm-actions" style={{ marginTop: 12 }}>
            <button type="button" className="btn btn--primary" onClick={() => save()} disabled={!dirty}>
              {savedFlash ? '✓' : t('common.save')}
            </button>
            <button
              type="button"
              className="btn btn--danger"
              onClick={() => {
                setEnabled(false);
                save(false);
              }}
            >
              {t('computer.disable')}
            </button>
          </div>
          <div className="field-hint">{t('computer.hint')}</div>
        </>
      )}
    </div>
  );
}
