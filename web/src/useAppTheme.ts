import { useEffect, useState } from 'react';

/* Текущая тема приложения — у неё нет ни стора, ни контекста: App.tsx
   держит её в своём useState и синхронизирует с <html data-theme> (+
   localStorage 'vpc-theme'), начальное значение — getInitialTheme() ниже.
   Здесь просто наблюдаем за атрибутом через MutationObserver, ничего в App.tsx не трогая —
   это нужно скинам (buildSkinEnv), которым тема нужна вне дерева App. */

export type AppTheme = 'light' | 'dark';

// Начальная тема: сохранённый выбор или системное предпочтение
// (App.tsx и экран запуска, пока App ещё не выставил атрибут на <html>)
export function getInitialTheme(): AppTheme {
  const saved = localStorage.getItem('vpc-theme');
  if (saved === 'light' || saved === 'dark') return saved;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

// Атрибут на <html>, а до того, как App его выставит, — начальная тема
function readTheme(): AppTheme {
  const attr = document.documentElement.getAttribute('data-theme');
  if (attr === 'light' || attr === 'dark') return attr;
  return getInitialTheme();
}

export function useAppTheme(): AppTheme {
  const [theme, setTheme] = useState<AppTheme>(readTheme);

  useEffect(() => {
    const sync = () => {
      const next = readTheme();
      setTheme((prev) => (prev === next ? prev : next));
    };
    sync(); // атрибут мог выставиться между рендером и подпиской
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
  }, []);

  return theme;
}
