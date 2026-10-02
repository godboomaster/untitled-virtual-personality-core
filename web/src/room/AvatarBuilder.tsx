import { useState } from 'react';
import type { RoomAvatarPreset } from '../mockData';
import { useI18n } from '../i18n';
import AvatarFigure from './AvatarFigure';
import { accessoryOptions, avatarShades, eyeOptions, headShapes } from './avatarOptions';

/* Конструктор аватара (вкладка «Загрузка» арт-мастерской): черновик +
   «Применить». Черновик сбрасывается к value при смене персоны — родитель
   передаёт key={personaId}. */

export default function AvatarBuilder({
  value,
  seed,
  onApply,
}: {
  value: RoomAvatarPreset;
  seed: string;
  onApply: (next: RoomAvatarPreset) => void;
}) {
  const { t } = useI18n();
  const [draft, setDraft] = useState<RoomAvatarPreset>(value);

  return (
    <div className="room-builder-slot">
      <div className="field-label">{t('room.avatarBuilder')}</div>
      <div className="room-builder">
        <div className="room-preview bracketed">
          <div className="corner tl" />
          <div className="corner br" />
          <AvatarFigure config={draft} pose="stand" height={108} seed={seed} />
        </div>
        <div>
          <div className="room-option-row">
            <div className="field-label">{t('room.headShape')}</div>
            <div className="room-options">
              {headShapes.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className={`room-option ${draft.head === s.id ? 'room-option--selected' : ''}`}
                  title={t(`room.head.${s.id}`)}
                  aria-label={t(`room.head.${s.id}`)}
                  aria-pressed={draft.head === s.id}
                  onClick={() => setDraft((d) => ({ ...d, head: s.id }))}
                >
                  {s.glyph}
                </button>
              ))}
            </div>
          </div>
          <div className="room-option-row">
            <div className="field-label">{t('room.eyes')}</div>
            <div className="room-options">
              {eyeOptions.map((e, i) => (
                <button
                  key={e}
                  type="button"
                  className={`room-option ${draft.eyes === i ? 'room-option--selected' : ''}`}
                  aria-label={`${t('room.eyes')} ${i + 1}`}
                  aria-pressed={draft.eyes === i}
                  onClick={() => setDraft((d) => ({ ...d, eyes: i }))}
                >
                  {e}
                </button>
              ))}
            </div>
          </div>
          <div className="room-option-row">
            <div className="field-label">{t('room.accessory')}</div>
            <div className="room-options">
              {accessoryOptions.map((a, i) => (
                <button
                  key={a}
                  type="button"
                  className={`room-option ${draft.accessory === i ? 'room-option--selected' : ''}`}
                  onClick={() => setDraft((d) => ({ ...d, accessory: i }))}
                >
                  {t(`room.acc.${a}`)}
                </button>
              ))}
            </div>
          </div>
          <div className="room-option-row">
            <div className="field-label">{t('room.bodyShade')}</div>
            <div className="room-options">
              {avatarShades.map((s, i) => (
                <button
                  key={s}
                  type="button"
                  className={`room-shade ${draft.shade === i ? 'room-shade--selected' : ''}`}
                  style={{ background: s }}
                  aria-label={t('room.shadeN', { n: i + 1 })}
                  onClick={() => setDraft((d) => ({ ...d, shade: i }))}
                />
              ))}
            </div>
          </div>
          <button className="btn btn--primary" onClick={() => onApply(draft)}>
            {t('common.apply')}
          </button>
        </div>
      </div>
    </div>
  );
}
