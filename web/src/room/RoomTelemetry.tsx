import { useI18n } from '../i18n';
import Icon from '../components/icons';
import type { AwaySummary } from './usePresenceCues';

/* Колонка состояния справа от сцены: энергия с баром, настроение +
   последнее событие, занятие и место с реальной длительностью. Внизу —
   приглушённо, откуда взято состояние (веб-чат или Telegram). Неизвестное
   значение — прочерк, без бара и длительности. */

export default function RoomTelemetry({
  energy,
  energyPct,
  mood,
  lastEvent,
  pastime,
  place,
  duration,
  sourceLabel,
}: {
  energy: string;
  energyPct: number | null;
  mood: string;
  lastEvent?: { time: string; text: string } | null;
  pastime: string;
  place: string;
  duration: string | null;
  sourceLabel?: string | null;
}) {
  const { t } = useI18n();
  return (
    <div className="home-telemetry room-telemetry stagger-item">
      <div className="home-tele-cell">
        <div className="home-tele-label">{t('room.energy')}</div>
        <div className="home-tele-value room-tele-text">{energy}</div>
        {energyPct != null && (
          <div className="room-tele-bar">
            <div className="room-tele-bar-fill" style={{ width: `${energyPct}%` }} />
          </div>
        )}
      </div>
      <div className="home-tele-cell">
        <div className="home-tele-label">{t('room.mood')}</div>
        <div className="home-tele-value room-tele-text" title={mood}>{mood}</div>
        {lastEvent && (
          <div className="room-tele-sub" title={`${t('room.lastEvent')}: ${lastEvent.text}`}>
            {t('room.lastEvent')}: {lastEvent.time} — {lastEvent.text}
          </div>
        )}
      </div>
      <div className="home-tele-cell">
        <div className="home-tele-label">{t('room.pastimeNow')}</div>
        <div className="home-tele-value room-tele-text" title={pastime}>{pastime}</div>
        {duration && <div className="room-tele-sub">{duration}</div>}
      </div>
      <div className="home-tele-cell">
        <div className="home-tele-label">{t('room.placeInRoom')}</div>
        <div className="home-tele-value room-tele-text" title={place}>{place}</div>
        {sourceLabel && <div className="room-tele-sub room-tele-source">{sourceLabel}</div>}
      </div>
    </div>
  );
}

// Полоса «пока тебя не было»: что изменилось с прошлого визита (закрывается)
export function AwayStrip({ summary, onDismiss }: { summary: AwaySummary; onDismiss: () => void }) {
  const { t } = useI18n();
  const parts: string[] = [];
  if (summary.pastime) parts.push(t('room.away.pastime', { from: summary.pastime.from, to: summary.pastime.to }));
  else if (summary.place) parts.push(t('room.away.place', { from: summary.place.from, to: summary.place.to }));
  if (summary.newItems.length) parts.push(t('room.away.items', { list: summary.newItems.slice(0, 3).join(', ') }));
  if (summary.newEvents) parts.push(t('room.away.events', { n: summary.newEvents }));
  const span =
    summary.minutes >= 60 ? t('room.dur.hShort', { h: Math.round(summary.minutes / 60) }) : t('room.dur.minShort', { n: summary.minutes });
  return (
    <div className="room-away-strip stagger-item" role="status">
      <span className="room-away-strip-k">{t('room.away.title', { span })}</span>
      <span className="room-away-strip-text">{parts.join(' · ')}</span>
      <button type="button" className="btn btn--icon room-away-strip-x" title={t('common.close')} aria-label={t('common.close')} onClick={onDismiss}>
        <Icon name="close" size={12} />
      </button>
    </div>
  );
}
