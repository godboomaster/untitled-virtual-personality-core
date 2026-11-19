import { useMemo } from 'react';
import { useI18n } from '../i18n';

/* Клавиши и кнопки мыши, на которые анимация играет на рабочем столе
   (anim.keys, app/api/portrait_anim.py). Сочетание — модификаторы в порядке
   Ctrl, Alt, Shift, Meta и одна клавиша по KeyboardEvent.code (физическая,
   от раскладки не зависит: KeyA, Digit1, F5, ShiftLeft…) или кнопка мыши:
   «Ctrl+Shift+KeyK», «MouseBack». Панель слушает их по всей системе и
   называет так же (desktop/src-tauri/src/input_watch.rs).

   Клавиша-модификатор сама по себе — её код без своего семейства в
   приставке: Shift при зажатом Ctrl — «Ctrl+ShiftLeft». */

export const KEYS_MAX = 8;

type Mod = 'Ctrl' | 'Alt' | 'Shift' | 'Meta';
const MODS: readonly Mod[] = ['Ctrl', 'Alt', 'Shift', 'Meta'];

// Код клавиши-модификатора → её семейство
const MOD_OF: Record<string, Mod> = {
  ControlLeft: 'Ctrl',
  ControlRight: 'Ctrl',
  AltLeft: 'Alt',
  AltRight: 'Alt',
  ShiftLeft: 'Shift',
  ShiftRight: 'Shift',
  MetaLeft: 'Meta',
  MetaRight: 'Meta',
};
// Старые браузеры зовут «Win»/«⌘» OSLeft/OSRight
const CODE_ALIAS: Record<string, string> = { OSLeft: 'MetaLeft', OSRight: 'MetaRight' };

export const KEY_RE = /^(Ctrl\+)?(Alt\+)?(Shift\+)?(Meta\+)?[A-Z][A-Za-z0-9]{0,23}$/;

interface Held {
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  metaKey: boolean;
}

function prefix(e: Held, skip?: Mod): string {
  const on: Record<Mod, boolean> = { Ctrl: e.ctrlKey, Alt: e.altKey, Shift: e.shiftKey, Meta: e.metaKey };
  return MODS.filter((m) => m !== skip && on[m])
    .map((m) => `${m}+`)
    .join('');
}

export function keyCode(e: KeyboardEvent): string {
  return CODE_ALIAS[e.code] ?? e.code;
}

/** Модификатор ли эта клавиша (Ctrl, Alt, Shift, Meta — левые и правые) */
export function isModifier(code: string): boolean {
  return code in MOD_OF;
}

/** Сочетание нажатой клавиши; код не годится (пустой у части клавиш) — null */
export function comboOfKey(e: KeyboardEvent): string | null {
  const code = keyCode(e);
  const combo = prefix(e, MOD_OF[code]) + code;
  return KEY_RE.test(combo) ? combo : null;
}

const MOUSE_BUTTON = ['MouseLeft', 'MouseMiddle', 'MouseRight', 'MouseBack', 'MouseForward'];

/** Сочетание нажатой кнопки мыши (MouseEvent.button: 0 левая … 4 вперёд) */
export function comboOfMouse(e: MouseEvent): string | null {
  const name = MOUSE_BUTTON[e.button];
  return name ? prefix(e) + name : null;
}

const MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

const MOD_LABEL: Record<Mod, string> = MAC
  ? { Ctrl: '⌃ Control', Alt: '⌥ Option', Shift: '⇧ Shift', Meta: '⌘ Cmd' }
  : { Ctrl: 'Ctrl', Alt: 'Alt', Shift: 'Shift', Meta: 'Win' };

const NAMED: Record<string, string> = {
  ArrowLeft: '←',
  ArrowRight: '→',
  ArrowUp: '↑',
  ArrowDown: '↓',
  Backquote: '`',
  Minus: '-',
  Equal: '=',
  BracketLeft: '[',
  BracketRight: ']',
  Backslash: '\\',
  IntlBackslash: '§',
  Semicolon: ';',
  Quote: "'",
  Comma: ',',
  Period: '.',
  Slash: '/',
  Escape: 'Esc',
  PageUp: 'Page Up',
  PageDown: 'Page Down',
  CapsLock: 'Caps Lock',
  NumpadAdd: 'Num +',
  NumpadSubtract: 'Num −',
  NumpadMultiply: 'Num *',
  NumpadDivide: 'Num /',
  NumpadDecimal: 'Num .',
  NumpadEnter: 'Num Enter',
  NumpadEqual: 'Num =',
};

/** Как показать клавишу: «A», «1», «F5», «←», «Пробел»; mouse — подписи кнопок мыши */
function keyLabel(code: string, words: Record<string, string>): string {
  if (words[code]) return words[code];
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit\d$/.test(code)) return code.slice(5);
  if (/^Numpad\d$/.test(code)) return `Num ${code.slice(6)}`;
  if (NAMED[code]) return NAMED[code];
  const mod = MOD_OF[code];
  if (mod) return `${MOD_LABEL[mod]} ${code.endsWith('Right') ? words.right : words.left}`;
  return code;
}

/** Сочетание для людей: «Ctrl + Shift + K», «⌘ Cmd + Левая кнопка мыши» */
export function formatCombo(combo: string, words: Record<string, string>): string {
  const parts = combo.split('+');
  const key = parts.pop() ?? '';
  return [...parts.map((m) => MOD_LABEL[m as Mod] ?? m), keyLabel(key, words)].join(' + ');
}

/** Подписи клавиш и кнопок мыши для formatCombo — на языке интерфейса */
export function useKeyWords(): Record<string, string> {
  const { t } = useI18n();
  return useMemo(
    () => ({
      Space: t('anim.keys.k.space'),
      Enter: 'Enter',
      Backspace: 'Backspace',
      Tab: 'Tab',
      Delete: 'Delete',
      Insert: 'Insert',
      Home: 'Home',
      End: 'End',
      MouseLeft: t('anim.keys.m.left'),
      MouseRight: t('anim.keys.m.right'),
      MouseMiddle: t('anim.keys.m.middle'),
      MouseBack: t('anim.keys.m.back'),
      MouseForward: t('anim.keys.m.forward'),
      left: t('anim.keys.side.left'),
      right: t('anim.keys.side.right'),
    }),
    [t],
  );
}
