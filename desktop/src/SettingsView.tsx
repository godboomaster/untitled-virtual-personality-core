import { useEffect, useState } from 'react';
import { api, type Config, type Settings } from './api';
import { t } from './i18n';

/* Настройки панели: пути (проект, Python, Node), что запускать и когда.
   Автозапуск при входе применяется сразу, остальное — кнопкой «Сохранить» */
export default function SettingsView({ onBack }: { onBack: () => void }) {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [cfg, setCfg] = useState<Config | null>(null);
  const [note, setNote] = useState<{ text: string; error: boolean } | null>(null);
  const [detecting, setDetecting] = useState(false);

  useEffect(() => {
    api.getSettings().then((s) => {
      setSettings(s);
      setCfg(s.config);
    });
  }, []);

  if (!settings || !cfg) return null;

  const set = <K extends keyof Config>(key: K, value: Config[K]) => {
    setCfg({ ...cfg, [key]: value });
    setNote(null);
  };

  const save = async () => {
    try {
      const s = await api.saveSettings(cfg);
      setSettings(s);
      setCfg(s.config);
      setNote({ text: t('saved'), error: false });
    } catch (e) {
      setNote({ text: String(e), error: true });
    }
  };

  const detect = async () => {
    setDetecting(true);
    try {
      set('python', await api.detectPython(cfg.repoDir));
    } catch (e) {
      setNote({ text: String(e), error: true });
    } finally {
      setDetecting(false);
    }
  };

  const toggleAutostart = async (enabled: boolean) => {
    try {
      const now = await api.setAutostart(enabled);
      setSettings({ ...settings, autostart: now });
    } catch (e) {
      setNote({ text: String(e), error: true });
    }
  };

  return (
    <>
      <header className="head head-sub">
        <button className="link" onClick={onBack}>
          ← {t('back')}
        </button>
        <div className="title">{t('settings')}</div>
      </header>
      <div className="scroll form">
        <label className="field">
          <span>{t('repoDir')}</span>
          <input value={cfg.repoDir} onChange={(e) => set('repoDir', e.target.value)} spellCheck={false} />
          {!settings.repoOk && cfg.repoDir === settings.config.repoDir && (
            <span className="field-error">{t('repoBad')}</span>
          )}
        </label>
        <label className="field">
          <span>{t('python')}</span>
          <div className="field-row">
            <input
              value={cfg.python}
              placeholder={t('pythonAuto')}
              onChange={(e) => set('python', e.target.value)}
              spellCheck={false}
            />
            <button className="btn" onClick={detect} disabled={detecting}>
              {detecting ? t('detecting') : t('detect')}
            </button>
          </div>
        </label>
        <label className="field">
          <span>{t('node')}</span>
          <input
            value={cfg.node}
            placeholder={t('nodeAuto')}
            onChange={(e) => set('node', e.target.value)}
            spellCheck={false}
          />
        </label>
        <label className="check">
          <input type="checkbox" checked={cfg.manageWeb} onChange={(e) => set('manageWeb', e.target.checked)} />
          <span>{t('manageWeb')}</span>
        </label>
        <label className="field field-short">
          <span>{t('webPort')}</span>
          <input
            type="number"
            min={1}
            max={65535}
            value={cfg.webPort}
            onChange={(e) => set('webPort', Number(e.target.value) || 0)}
          />
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={cfg.startOnLaunch}
            onChange={(e) => set('startOnLaunch', e.target.checked)}
          />
          <span>{t('startOnLaunch')}</span>
        </label>
        <label className="check">
          <input type="checkbox" checked={cfg.autoRestart} onChange={(e) => set('autoRestart', e.target.checked)} />
          <span>{t('autoRestart')}</span>
        </label>
        <label className="check">
          <input type="checkbox" checked={settings.autostart} onChange={(e) => toggleAutostart(e.target.checked)} />
          <span>{t('autostart')}</span>
        </label>
        {note && <div className={note.error ? 'error' : 'hint'}>{note.text}</div>}
        <div className="paths">{t('paths', { cfg: settings.configPath, logs: settings.logsDir })}</div>
      </div>
      <footer className="foot">
        <button className="btn btn-primary" onClick={save}>
          {t('save')}
        </button>
      </footer>
    </>
  );
}
