import type { Ref } from 'react';
import type { RoomActivity } from '../mockData';
import { useI18n } from '../i18n';

/* Лента «пока тебя не было»: недавние события офлайн-жизни персоны.
   highlightId — событие, открытое кликом по записке на столе. */

export default function RoomFeed({
  feed,
  highlightId,
  feedRef,
}: {
  feed: RoomActivity[];
  highlightId?: number | null;
  feedRef?: Ref<HTMLDivElement>;
}) {
  const { t } = useI18n();
  return (
    <div className="card stagger-item" ref={feedRef}>
      <div className="card-title-row">
        <h3 className="card-title">{t('room.feedTitle')}</h3>
        <span className="badge">LOG_{feed.length}</span>
      </div>
      <div className="room-feed">
        {feed.map((a) => (
          <div
            key={a.id}
            className={`room-feed-item${a.dim ? ' room-feed-item--dim' : ''}${a.id === highlightId ? ' room-feed-item--hl' : ''}`}
            title={a.dim ? t('room.feedDim') : undefined}
          >
            <span className="room-feed-time">{a.time}</span>
            <span>{a.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
