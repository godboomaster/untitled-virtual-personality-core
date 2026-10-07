import { SystemBars, SystemBarsStyle } from '@capacitor/core';
import { isNativeApp } from './api';

/* Приложение на Android: значки строки состояния и панели навигации —
   светлые на тёмной теме приложения и тёмные на светлой. По умолчанию
   Android берёт тему телефона, и при несовпадении значки не видно. */
export function syncSystemBars(theme: string) {
  if (!isNativeApp()) return;
  void SystemBars.setStyle({ style: theme === 'dark' ? SystemBarsStyle.Dark : SystemBarsStyle.Light }).catch(() => {});
}
