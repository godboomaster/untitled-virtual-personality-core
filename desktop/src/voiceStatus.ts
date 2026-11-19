import type { VoiceView } from './api';
import { t } from './i18n';

/* Состояние слушателя имени (voice.rs) словами — одно для окна панели и
   кнопки микрофона у портрета на рабочем столе */

export type VoiceTone = 'ok' | 'warn' | 'bad' | 'off';

export function voiceTone(v: VoiceView): VoiceTone {
  const st = v.status;
  if (!v.enabled) return 'off';
  if (v.phase === 'failed' || st?.issue === 'mic_access' || st?.issue === 'modules') return 'bad';
  if (v.phase === 'running' && st?.running && st.stage === 'listening' && !st.held && st.names.length > 0) return 'ok';
  return 'warn';
}

export function voiceText(v: VoiceView): string {
  const st = v.status;
  if (!v.enabled) return v.phase === 'stopping' ? t('voiceStopping') : t('voiceOff');
  if (v.phase === 'failed') return t('voiceFailed');
  if (v.phase === 'stopping') return t('voiceStopping');
  if (!st || v.phase === 'starting') return t('voiceStarting');
  if (st.stage === 'model') return t('voiceModel');
  if (st.stage === 'names') return t('voiceNames');
  if (st.issue === 'mic_access') return t('voiceNoAccess');
  if (st.stage === 'mic') return t('voiceMicRetry');
  if (st.held) return t('voiceHeld');
  if (st.state === 'busy') return t('voiceBusy', { persona: st.persona ?? '' });
  if (st.state === 'await') return t('voiceAwait', { persona: st.persona ?? '' });
  if (st.names.length === 0) return t('voiceNoNames');
  return t('voiceListening');
}

/* Ошибка слушателя: доступ к микрофону и модули — своими словами панели,
   остальное — как есть (выход процесса, сбой ответа бота) */
export function voiceError(v: VoiceView): string | null {
  if (!v.enabled) return null;
  const st = v.status;
  if (st?.issue === 'mic_access') return t('voiceMicAccess');
  if (st?.issue === 'modules') {
    const cmd = st.error.split(': ').slice(1).join(': ') || st.error;
    return t('voiceModules', { cmd });
  }
  return v.error ?? (st?.error || null);
}
