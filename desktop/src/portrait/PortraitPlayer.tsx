import { invoke } from '@tauri-apps/api/core';
import MiniPlayer, { type PlayerLayout } from '../../../web/src/player/MiniPlayer';
import type { BrowserPlayerStatus, PlayerCommand } from '../../../web/src/api';
import { t } from '../i18n';

const status = () => invoke<BrowserPlayerStatus>('portrait_player_status');
let playerHasFocus = false;
const control = async (command: PlayerCommand, value?: number) => {
  const next = await invoke<BrowserPlayerStatus>('portrait_player_control', { command, value });
  if (command === 'stop' && !next.running) { playerHasFocus = false; await invoke('portrait_focus', { on: false }); }
  return next;
};
let queued: PlayerLayout | undefined;
let sending = false;
let last = '';
function visibilityChanged(visible: boolean) {
  if (!visible && playerHasFocus) {
    playerHasFocus = false;
    const focused = document.activeElement as HTMLElement | null;
    // A disabled range can already have lost DOM focus by the time it hides.
    // Return the keyboard unless the user has since focused the chat editor.
    if (!focused?.matches('input, textarea, [contenteditable="true"]') || focused.closest('.portrait-player-controls')) {
      void invoke('portrait_focus', { on: false }).catch(console.error);
    }
  }
}
function layout(size: PlayerLayout) {
  const host = document.querySelector('.portrait-player');
  // Include the lift above the avatar's floor in the native window reserve.
  const lift = size.width && host ? Math.max(0, parseFloat(getComputedStyle(host).bottom) || 0) : 0;
  const next = { width: size.width, height: size.height + lift };
  if (!next.width) visibilityChanged(false);
  const key = `${next.width}:${next.height}`;
  if (key === last && !queued && !sending) return;
  queued = next;
  if (sending) return;
  sending = true;
  void (async () => {
    try {
      while (queued) {
        const value = queued;
        queued = undefined;
        await invoke('portrait_player_layout', { ...value });
        last = `${value.width}:${value.height}`;
      }
    } catch (e) { last = ''; console.error('portrait_player_layout', e); }
    finally { sending = false; }
  })();
}

export default function PortraitPlayer({ visible, hidden = false, onActivity }: { visible: boolean; hidden?: boolean; onActivity: () => void }) {
  return <div className="portrait-player-controls" onFocusCapture={() => { playerHasFocus = true; }} onPointerDownCapture={(e) => {
    const target = e.target;
    if (target instanceof HTMLInputElement) {
      playerHasFocus = true;
      void invoke('portrait_focus', { on: true }).then(() => target.focus()).catch(console.error);
    }
  }}><MiniPlayer status={status} control={control} onLayout={layout} visible={visible} hidden={hidden} onActivity={onActivity} onVisibilityChange={visibilityChanged} labels={{
    player: t('playerTitle'), pause: t('playerPause'), resume: t('playerResume'),
    previous: t('playerPrevious'), next: t('playerNext'), volume: t('playerVolume'),
    louder: t('playerLouder'), quieter: t('playerQuieter'),
    expand: t('playerExpand'), collapse: t('playerCollapse'), close: t('playerClose'),
    nowPlaying: t('playerNowPlaying'), seek: t('playerSeek'), local: t('playerLocal'), library: t('playerLibrary'),
  }} /></div>;
}
