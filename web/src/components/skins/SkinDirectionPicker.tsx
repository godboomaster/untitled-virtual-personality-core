/* Карточки арт-направлений генератора скина (POST /api/skins/direction):
   образец в цветах и шрифтах направления, палитра, шрифты своими стеками,
   фирменный элемент; подробности (раскладка, фактура, движение, запреты)
   — под катом. Выбор — радио-группа; заметка пользователя уходит в
   direction.note выбранного направления. */

import type { ApiSkinDirection } from '../../api';
import { useI18n } from '../../i18n';

export const DIRECTION_NOTE_MAX = 1000;

// Первое имя шрифтового стека — подпись шрифта
function familyName(stack: string): string {
  return (stack.split(',')[0] ?? '').trim().replace(/^["']|["']$/g, '');
}

function roleHex(d: ApiSkinDirection, role: string): string | undefined {
  return d.palette.find((c) => c.role === role)?.hex;
}

interface DirectionCardProps {
  direction: ApiSkinDirection;
  checked: boolean;
  disabled: boolean;
  onSelect: () => void;
}

function DirectionCard({ direction: d, checked, disabled, onSelect }: DirectionCardProps) {
  const { t } = useI18n();
  const bg = roleHex(d, 'background');
  const ink = roleHex(d, 'ink');
  const muted = roleHex(d, 'muted') ?? ink;
  const accent = roleHex(d, 'accent');
  const surface = roleHex(d, 'surface') ?? bg;
  const display = familyName(d.fonts.display);
  const text = familyName(d.fonts.text);
  return (
    <div
      className={`skin-dir-card ${checked ? 'skin-dir-card--on' : ''} ${disabled ? 'skin-dir-card--off' : ''}`}
      onClick={() => !disabled && onSelect()}
    >
      <label className="skin-dir-main">
        <input
          type="radio"
          name="skin-direction"
          className="skin-dir-radio"
          checked={checked}
          disabled={disabled}
          onChange={onSelect}
        />
        {/* Образец: направление в собственных цветах и шрифтах */}
        <span
          className="skin-dir-specimen"
          style={{ background: bg, color: ink, borderColor: accent, boxShadow: `inset 0 -3px 0 ${surface}` }}
        >
          <span className="skin-dir-name" style={{ fontFamily: d.fonts.display }}>
            {d.name}
          </span>
          {d.mood && (
            <span className="skin-dir-mood" style={{ fontFamily: d.fonts.text, color: muted }}>
              {d.mood}
            </span>
          )}
          <span className="skin-dir-accent" style={{ background: accent }} />
        </span>
        <span className="skin-dir-subject">{d.subject}</span>
        <span className="skin-dir-swatches" aria-label={t('skin.dirPalette')}>
          {d.palette.map((c, i) => (
            <span
              key={i}
              className="skin-dir-swatch"
              style={{ background: c.hex }}
              title={`${c.role} · ${c.hex}${c.reason ? ` — ${c.reason}` : ''}`}
            />
          ))}
        </span>
        <span className="skin-dir-fonts" title={d.fonts.why || undefined}>
          <span style={{ fontFamily: d.fonts.display }} title={d.fonts.display}>
            {display}
          </span>
          {text && text !== display && (
            <>
              {' · '}
              <span style={{ fontFamily: d.fonts.text }} title={d.fonts.text}>
                {text}
              </span>
            </>
          )}
        </span>
        {d.signature && (
          <span className="skin-dir-line">
            <b>{t('skin.dirSignature')}</b> {d.signature}
          </span>
        )}
      </label>
      {(d.layout || d.texture || d.motion || d.avoid.length > 0 || d.fonts.why) && (
        <details className="skin-dir-more">
          <summary>{t('skin.dirDetails')}</summary>
          {d.layout && (
            <p>
              <b>{t('skin.dirLayout')}</b> {d.layout}
            </p>
          )}
          {d.texture && (
            <p>
              <b>{t('skin.dirTexture')}</b> {d.texture}
            </p>
          )}
          {d.motion && (
            <p>
              <b>{t('skin.dirMotion')}</b> {d.motion}
            </p>
          )}
          {d.fonts.why && (
            <p>
              <b>{t('skin.dirFonts')}</b> {d.fonts.why}
            </p>
          )}
          {d.avoid.length > 0 && (
            <p>
              <b>{t('skin.dirAvoid')}</b> {d.avoid.join('; ')}
            </p>
          )}
        </details>
      )}
    </div>
  );
}

interface SkinDirectionPickerProps {
  directions: ApiSkinDirection[];
  selected: number | null;
  onSelect: (index: number) => void;
  note: string;
  onNote: (note: string) => void;
  disabled: boolean;
}

export default function SkinDirectionPicker({
  directions,
  selected,
  onSelect,
  note,
  onNote,
  disabled,
}: SkinDirectionPickerProps) {
  const { t } = useI18n();
  return (
    <div className="skin-dir">
      <div className="skin-dir-grid" role="radiogroup" aria-label={t('skin.dirTitle')}>
        {directions.map((d, i) => (
          <DirectionCard
            key={`${i}-${d.name}`}
            direction={d}
            checked={selected === i}
            disabled={disabled}
            onSelect={() => onSelect(i)}
          />
        ))}
      </div>
      {selected !== null && (
        <label className="field skin-dir-note">
          <span className="field-label">{t('skin.dirNote')}</span>
          <textarea
            className="input pcreate-textarea"
            rows={2}
            value={note}
            maxLength={DIRECTION_NOTE_MAX}
            disabled={disabled}
            placeholder={t('skin.dirNotePh')}
            onChange={(e) => onNote(e.target.value)}
          />
        </label>
      )}
    </div>
  );
}
