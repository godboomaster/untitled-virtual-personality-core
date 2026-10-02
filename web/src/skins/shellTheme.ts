/* Подключение shell-темы скина к каркасу приложения. Пока открыт экран
   персоны со скином (чат или комната), переменные каркаса выставляются
   inline на <html> — они перекрывают и светлую, и тёмную тему, — а
   дефолтный фоновый canvas прячется (html[data-vpc-shell]). Палитра
   берётся с учётом текущей темы приложения (блоки [data-theme] скина).
   При уходе на другой раздел или смене персоны всё откатывается к дефолту. */

import { useEffect } from 'react';
import { extractShellTheme } from './engine';
import { useAppTheme } from '../useAppTheme';

export function useShellTheme(skin: string | null) {
  const theme = useAppTheme();
  useEffect(() => {
    if (!skin) return;
    const vars = extractShellTheme(skin, theme);
    const keys = Object.keys(vars);
    if (keys.length === 0) return;
    const root = document.documentElement;
    keys.forEach((k) => root.style.setProperty(k, vars[k]));
    root.setAttribute('data-vpc-shell', '');
    return () => {
      keys.forEach((k) => root.style.removeProperty(k));
      root.removeAttribute('data-vpc-shell');
    };
  }, [skin, theme]);
}
