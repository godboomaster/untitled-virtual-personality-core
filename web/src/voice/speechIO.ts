import { registerPlugin } from '@capacitor/core';
import { isNativeApp } from '../api';
import { createBrowserEngine, createNativeEngine } from './speechEngines';
import type { NativeSpeechPlugin, SpeechEngine } from './speechEngines';

/* Движок голосового режима: в приложении Android — плагин NativeSpeech
   (распознавание и озвучка телефона), в браузере — Web Speech API.
   Один на всё приложение: плагин хранит ответ «есть ли распознавание». */

export type { ListenCallbacks, ListenSession, SpeakStatus, SpeechEngine } from './speechEngines';

let engine: SpeechEngine | null = null;

export function getSpeechEngine(): SpeechEngine {
  if (!engine) {
    engine = isNativeApp()
      ? createNativeEngine(registerPlugin<NativeSpeechPlugin>('NativeSpeech'))
      : createBrowserEngine(window as unknown as Record<string, unknown>);
  }
  return engine;
}

// Тег языка речи по языку интерфейса
export function speechLangTag(lang: string): string {
  return lang === 'ru' ? 'ru-RU' : 'en-US';
}
