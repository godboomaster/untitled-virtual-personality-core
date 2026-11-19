import { browserPlayerStatus, controlBrowserMusic } from '../api';
import { useI18n } from '../i18n';
import MiniPlayer, { type PlayerLayout } from './MiniPlayer';

export default function WebMiniPlayer({ onLayout }: { onLayout?: (layout: PlayerLayout) => void }) {
  const { t } = useI18n();
  return <MiniPlayer status={browserPlayerStatus} control={controlBrowserMusic} onLayout={onLayout} labels={{
    player: t('nav.player'), pause: t('player.browser.pause'), resume: t('player.browser.resume'),
    previous: t('player.previous'), next: t('player.next'), volume: t('player.browser.volume'),
    louder: t('player.louder'), quieter: t('player.quieter'),
    expand: t('player.mini.expand'), collapse: t('player.mini.collapse'), close: t('player.mini.close'),
    nowPlaying: t('player.mini.nowPlaying'), seek: t('player.browser.seek'),
    local: t('player.mini.local'), library: t('player.mini.library'),
  }} />;
}
