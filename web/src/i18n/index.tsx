import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import ru from './ru';
import en from './en';
import { mockRu, mockEn } from '../mockData';
import type { MockData } from '../mockData';
import { helpTextsRu, helpTextsEn } from '../helpTexts';
import type { HelpEntry, HelpKey } from '../helpTexts';
import { useApiPersonas } from '../apiData';

/* Локализация интерфейса: контекст языка (RU/EN), словарь строк t(),
   моковые данные и тексты подсказок текущей локали. Выбор языка
   персистится в localStorage (ключ vpc-lang) по образцу темы (vpc-theme). */

export type Lang = 'ru' | 'en';

const dicts: Record<Lang, Record<string, string>> = { ru, en };

// Начальный язык: сохранённый выбор или русский
function getInitialLang(): Lang {
  const saved = localStorage.getItem('vpc-lang');
  return saved === 'ru' || saved === 'en' ? saved : 'ru';
}

// Подсказки («?»): по умолчанию включены, выбор персистится (vpc-hints)
function getInitialHints(): boolean {
  return localStorage.getItem('vpc-hints') !== 'off';
}

interface I18nContextValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  // Показывать ли кнопки-подсказки «?»
  hintsEnabled: boolean;
  setHintsEnabled: (on: boolean) => void;
  // Перевод строки по ключу; {name}-плейсхолдеры заменяются значениями vars.
  // Фолбэк: русская строка, затем сам ключ.
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLang] = useState<Lang>(getInitialLang);
  const [hintsEnabled, setHintsEnabled] = useState<boolean>(getInitialHints);

  // Запоминаем выбор языка и отражаем его в <html lang>
  useEffect(() => {
    localStorage.setItem('vpc-lang', lang);
    document.documentElement.setAttribute('lang', lang);
  }, [lang]);

  // Запоминаем настройку подсказок
  useEffect(() => {
    localStorage.setItem('vpc-hints', hintsEnabled ? 'on' : 'off');
  }, [hintsEnabled]);

  const value = useMemo<I18nContextValue>(() => {
    const t = (key: string, vars?: Record<string, string | number>) => {
      let s = dicts[lang][key] ?? dicts.ru[key] ?? key;
      if (vars) {
        for (const [name, v] of Object.entries(vars)) {
          s = s.replaceAll(`{${name}}`, String(v));
        }
      }
      return s;
    };
    return { lang, setLang, hintsEnabled, setHintsEnabled, t };
  }, [lang, hintsEnabled]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error('useI18n outside I18nProvider');
  return ctx;
}

// Датасет текущей локали. Если бэкенд доступен, список персон подменяется
// реальным (из API) — остальные срезы (комнаты, инициатива и т.п.) пока моковые.
export function useMockData(): MockData {
  const { lang } = useI18n();
  const base = lang === 'en' ? mockEn : mockRu;
  const apiPersonas = useApiPersonas();
  return apiPersonas ? { ...base, personas: apiPersonas } : base;
}

// Тексты подсказок InfoButton текущей локали
export function useHelpTexts(): Record<HelpKey, HelpEntry> {
  const { lang } = useI18n();
  return lang === 'en' ? helpTextsEn : helpTextsRu;
}
