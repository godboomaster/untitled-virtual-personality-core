/* Перекраска скина: цветовые CSS-переменные его файлов (группами, самые
   используемые первыми) с нативными пикерами, сдвиг тона всей палитры и
   живой предпросмотр. Сохраняются только переопределения — файлы скина не
   меняются. Встроенный скин при сохранении копируется в пользовательский. */

import { useMemo, useState } from 'react';
import {
  extractSkinColorVars,
  formatColor,
  parseColor,
  pickerToColor,
  prettyVarName,
  rotateHue,
  toHex,
} from '../../skins/recolor';
import type { ColorGroup, ColorVar } from '../../skins/recolor';
import { updateSkin, useSkinFiles } from '../../skins/skinStore';
import type { SkinEntry } from '../../skins/skinStore';
import type { Persona } from '../../mockData';
import { useI18n } from '../../i18n';
import SkinPreview from './SkinPreview';
import { skinTitle } from './skinUi';

const GROUPS: [ColorGroup, string][] = [
  ['surface', 'skin.groupSurface'],
  ['text', 'skin.groupText'],
  ['accent', 'skin.groupAccent'],
  ['line', 'skin.groupLine'],
];
const GROUP_LIMIT = 8; // свёрнутая группа показывает самые используемые

interface SkinColorEditorProps {
  entry: SkinEntry;
  persona: Persona;
  onDone: () => void;
}

export default function SkinColorEditor({ entry, persona, onDone }: SkinColorEditorProps) {
  const { t } = useI18n();
  const { files, loading } = useSkinFiles(entry.id);
  const [colors, setColors] = useState<Record<string, string>>(entry.colors);
  const [hue, setHue] = useState(entry.hueShift ?? 0);
  const [expanded, setExpanded] = useState<Partial<Record<ColorGroup, boolean>>>({});
  const [busy, setBusy] = useState(false);

  // Редактируются литералы: переменные-ссылки следуют за своей базой сами
  const { chat, dossier, room } = files;
  const vars = useMemo(
    () => extractSkinColorVars([chat, dossier, room].filter((f): f is string => !!f)).filter((v) => !v.alias),
    [chat, dossier, room],
  );

  // Цвет, который переменная получит сейчас: явный > сдвинутый тон > исходный
  const effective = (v: ColorVar): string => {
    if (colors[v.name]) return colors[v.name];
    const c = parseColor(v.color);
    return c && hue ? formatColor(rotateHue(c, hue)) : v.color;
  };

  const setColor = (v: ColorVar, hex: string) => setColors((cur) => ({ ...cur, [v.name]: pickerToColor(hex, v.color) }));
  const resetColor = (name: string) =>
    setColors((cur) => {
      const next = { ...cur };
      delete next[name];
      return next;
    });

  const dirty = hue !== (entry.hueShift ?? 0) || JSON.stringify(colors) !== JSON.stringify(entry.colors);

  const save = async () => {
    setBusy(true);
    try {
      // Копия встроенного получает его имя на языке интерфейса
      await updateSkin(entry.id, { colors, hueShift: hue, ...(entry.builtin ? { name: skinTitle(entry, t) } : {}) });
      onDone();
    } catch {
      /* ошибка уже в сторе — панель её показывает */
    } finally {
      setBusy(false);
    }
  };

  const swatch = (v: ColorVar) => (
    <div key={v.name} className={`skin-color ${colors[v.name] ? 'skin-color--set' : ''}`} title={`${v.name}\n${v.contexts.join('\n')}`}>
      <input
        type="color"
        className="skin-color-input"
        value={toHex(parseColor(effective(v)) ?? { r: 0, g: 0, b: 0, a: 1 })}
        onChange={(e) => setColor(v, e.target.value)}
        aria-label={prettyVarName(v.name)}
      />
      <span className="skin-color-label">
        <span className="skin-color-name">{prettyVarName(v.name)}</span>
        <span className="skin-color-var">
          {v.name} · {t('skin.colorUses', { n: v.uses })}
        </span>
      </span>
      {colors[v.name] && (
        <button type="button" className="btn btn--icon" title={t('skin.resetColor')} onClick={() => resetColor(v.name)}>
          ↺
        </button>
      )}
    </div>
  );

  return (
    <div className="skin-workspace">
      <div className="skin-workspace-title">{t('skin.colorsTitle', { name: skinTitle(entry, t) })}</div>
      {entry.builtin && <p className="ctx-note">{t('skin.builtinFork')}</p>}
      {loading && <p className="ctx-note">{t('skin.loadingFiles')}</p>}
      {!loading && vars.length === 0 && <p className="ctx-note">{t('skin.noColorVars')}</p>}

      {vars.length > 0 && (
        <>
          <div className="skin-hue">
            <span className="field-label">{t('skin.hue')}</span>
            <input
              type="range"
              min={-180}
              max={180}
              step={1}
              value={hue}
              onChange={(e) => setHue(Number(e.target.value))}
            />
            <span className="field-value">{hue > 0 ? '+' : ''}{hue}°</span>
          </div>

          {GROUPS.map(([group, labelKey]) => {
            const list = vars.filter((v) => v.group === group);
            if (!list.length) return null;
            const open = expanded[group] || list.length <= GROUP_LIMIT;
            return (
              <div key={group} className="skin-color-group">
                <div className="field-label">{t(labelKey)}</div>
                <div className="skin-color-grid">{(open ? list : list.slice(0, GROUP_LIMIT)).map(swatch)}</div>
                {list.length > GROUP_LIMIT && (
                  <button
                    type="button"
                    className="btn btn--ghost skin-color-more"
                    onClick={() => setExpanded((cur) => ({ ...cur, [group]: !open }))}
                  >
                    {open ? t('skin.showLess') : t('skin.showAll', { n: list.length })}
                  </button>
                )}
              </div>
            );
          })}

          <SkinPreview files={files} persona={persona} colors={colors} hueShift={hue} />
        </>
      )}

      <div className="skin-actions">
        <button type="button" className="btn btn--primary" disabled={!dirty || busy} onClick={save}>
          {t('skin.saveColors')}
        </button>
        <button
          type="button"
          className="btn btn--ghost"
          disabled={!Object.keys(colors).length && !hue}
          onClick={() => {
            setColors({});
            setHue(0);
          }}
        >
          {t('skin.resetColors')}
        </button>
        <button type="button" className="btn btn--ghost" onClick={onDone}>
          {t('skin.cancel')}
        </button>
      </div>
    </div>
  );
}
