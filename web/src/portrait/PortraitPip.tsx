import { useEffect } from 'react';
import { useI18n } from '../i18n';
import type { Persona } from '../mockData';
import { closePip } from '../pipStore';
import LivePortrait from './LivePortrait';
import { hasPortrait, usePortrait, usePortraitActivity } from './portraitStore';

/* Живой портрет в PiP-окне («Вынести портрет»): голова на всё окно поверх
   остальных окон — смотрит на курсор над окном, моргает; пока открыт
   голосовой режим этой персоны — говорит, слушает и думает вместе с ним
   (portraitStore → usePortraitActivity). Кнопка «Вернуть» — при наведении,
   чтобы не заслонять портрет. Само окно — PipHost (web/src/PipHost.tsx). */

export default function PortraitPipView({ persona, doc }: { persona: Persona; doc: Document }) {
  const { t } = useI18n();
  const data = usePortrait(persona.id);
  const { speaking, listening, thinking } = usePortraitActivity(persona.id);

  useEffect(() => {
    doc.title = persona.name;
  }, [doc, persona.name]);

  return (
    <div className={`portrait-pip ${listening ? 'portrait-pip--listening' : ''}`}>
      {hasPortrait(data) ? (
        <LivePortrait data={data} speaking={speaking} listening={listening} thinking={thinking} label={persona.name} />
      ) : (
        <div className="portrait-empty">{t('portrait.empty')}</div>
      )}
      <button type="button" className="room-focus-btn room-focus-btn--muted portrait-pip-back" onClick={closePip}>
        {t('portrait.pip.back')}
      </button>
    </div>
  );
}
