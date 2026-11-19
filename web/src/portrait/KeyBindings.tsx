import { useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import { comboOfKey, comboOfMouse, formatCombo, isModifier, keyCode, KEYS_MAX, useKeyWords } from './animKeys';

/* Клавиши анимации: список сочетаний и запись нового. Запись — по нажатию:
   клавиша (с модификаторами) — сразу, модификатор сам по себе — когда его
   отпустят, ничего больше не нажав; кнопка мыши — нажатая над полем записи.
   Щелчок мимо поля или уход со страницы — отмена. */

export default function KeyBindings({
  keys,
  onChange,
  others,
}: {
  keys: string[];
  onChange: (keys: string[]) => void;
  /** Сочетания других анимаций этой персоны — подсказать, что заняты */
  others: { combo: string; name: string }[];
}) {
  const { t } = useI18n();
  const words = useKeyWords();
  const [rec, setRec] = useState(false);
  const padRef = useRef<HTMLDivElement>(null);
  const keysRef = useRef(keys);
  keysRef.current = keys;
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    if (!rec) return;
    const add = (combo: string | null) => {
      setRec(false);
      const cur = keysRef.current;
      if (combo && !cur.includes(combo) && cur.length < KEYS_MAX) onChangeRef.current([...cur, combo]);
    };
    // Модификатор нажат: станет сочетанием, если его отпустят, ничего больше не нажав
    let pending: { code: string; combo: string | null } | null = null;
    const down = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.repeat) return;
      const code = keyCode(e);
      if (isModifier(code)) {
        pending = { code, combo: comboOfKey(e) };
        return;
      }
      pending = null;
      add(comboOfKey(e));
    };
    const up = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (pending && pending.code === keyCode(e)) add(pending.combo);
      pending = null;
    };
    const mouse = (e: MouseEvent) => {
      if (padRef.current?.contains(e.target as Node)) {
        e.preventDefault();
        add(comboOfMouse(e));
      } else setRec(false);
    };
    const cancel = () => setRec(false);
    window.addEventListener('keydown', down, true);
    window.addEventListener('keyup', up, true);
    window.addEventListener('mousedown', mouse, true);
    window.addEventListener('blur', cancel);
    padRef.current?.focus();
    return () => {
      window.removeEventListener('keydown', down, true);
      window.removeEventListener('keyup', up, true);
      window.removeEventListener('mousedown', mouse, true);
      window.removeEventListener('blur', cancel);
    };
  }, [rec]);

  const taken = keys.flatMap((k) => {
    const names = others.filter((o) => o.combo === k).map((o) => o.name || t('anim.untitled'));
    return names.length ? [{ k, names }] : [];
  });

  return (
    // Кнопки мыши «назад»/«вперёд» над полем — не листать историю браузера
    <div
      className="anim-keys"
      onMouseUp={(e) => e.button > 2 && e.preventDefault()}
      onAuxClick={(e) => e.preventDefault()}
      onContextMenu={(e) => rec && e.preventDefault()}
    >
      <div className="anim-chip-group-label">{t('anim.keys.title')}</div>
      <div className="anim-keys-list">
        {keys.map((k) => (
          <span key={k} className="anim-key">
            <span className="anim-key-text">{formatCombo(k, words)}</span>
            <button
              type="button"
              className="anim-key-del"
              aria-label={t('anim.keys.remove', { key: formatCombo(k, words) })}
              title={t('anim.keys.remove', { key: formatCombo(k, words) })}
              onClick={() => onChange(keys.filter((x) => x !== k))}
            >
              ×
            </button>
          </span>
        ))}
        {rec ? (
          <>
            <div ref={padRef} className="anim-key-pad" tabIndex={-1} role="status">
              {t('anim.keys.press')}
            </div>
            <button type="button" className="btn btn--ghost" onClick={() => setRec(false)}>
              {t('common.cancel')}
            </button>
          </>
        ) : (
          keys.length < KEYS_MAX && (
            <button type="button" className="anim-chip" onClick={() => setRec(true)}>
              {t('anim.keys.add')}
            </button>
          )
        )}
      </div>
      {taken.map(({ k, names }) => (
        <div key={k} className="field-hint">
          {t('anim.keys.taken', { key: formatCombo(k, words), names: names.map((n) => `«${n}»`).join(', ') })}
        </div>
      ))}
      <div className="field-hint">{t('anim.keys.hint')}</div>
    </div>
  );
}
