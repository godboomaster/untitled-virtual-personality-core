import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { RoomAvatarPreset } from '../mockData';
import { useI18n } from '../i18n';
import AvatarFigure from './AvatarFigure';
import { AccessoryIcon, HeadShapeIcon } from './avatarHead';
import { accessoryOptions, avatarShades, avatarTone, headShapes } from './avatarOptions';
import { BodyIcon, CLOTH_GRAY, bodyOptions } from './avatarBody';
import { EYE_COUNT, EyesIcon } from './avatarEyes';

/* Конструктор аватара (вкладка «Загрузка» арт-мастерской): черновик +
   «Применить». Черновик сбрасывается к value при смене персоны — родитель
   передаёт key={personaId}. Варианты — рядами в одну строку с прокруткой
   вбок (ScrollRow): форм, глаз, аксессуаров и тел много, переносом ряды
   занимали полэкрана. */

// Ряд вариантов: листается вбок (тачпад, перетаскивание, колесо мыши — пока
// есть куда, дальше прокрутка уходит странице); край гаснет с той стороны,
// где есть ещё; выбранный вариант при открытии — в видимой части
function ScrollRow({ selected, children }: { selected: number; children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [edge, setEdge] = useState({ left: false, right: false });
  const update = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const left = el.scrollLeft > 2;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 2;
    setEdge((e) => (e.left === left && e.right === right ? e : { left, right }));
  }, []);
  const first = useRef(true);
  useLayoutEffect(() => {
    const el = ref.current;
    const item = el?.children[selected] as HTMLElement | undefined;
    if (el && item) {
      const a = item.offsetLeft - el.offsetLeft;
      const b = a + item.offsetWidth;
      if (first.current) el.scrollLeft = a - (el.clientWidth - item.offsetWidth) / 2;
      else if (a < el.scrollLeft) el.scrollLeft = a - 8;
      else if (b > el.scrollLeft + el.clientWidth) el.scrollLeft = b - el.clientWidth + 8;
    }
    first.current = false;
    update();
  }, [selected, update]);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
      const max = el.scrollWidth - el.clientWidth;
      if (max <= 0) return;
      if ((e.deltaY < 0 && el.scrollLeft <= 0) || (e.deltaY > 0 && el.scrollLeft >= max - 1)) return;
      e.preventDefault();
      el.scrollLeft = Math.max(0, Math.min(max, el.scrollLeft + e.deltaY));
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    const view = el.ownerDocument.defaultView ?? window;
    view.addEventListener('resize', update);
    return () => {
      el.removeEventListener('wheel', onWheel);
      view.removeEventListener('resize', update);
    };
  }, [update]);
  return (
    <div
      ref={ref}
      className={`room-options room-options--scroll${edge.left ? ' room-options--more-left' : ''}${edge.right ? ' room-options--more-right' : ''}`}
      onScroll={update}
    >
      {children}
    </div>
  );
}

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
  // Кнопки тела — в оттенке корпуса черновика
  const tone = avatarTone(draft);
  const bodyIconColors = { shell: tone.shade, line: tone.stroke, eye: tone.eyeColor, accent: 'var(--accent)', cloth: CLOTH_GRAY };

  return (
    <div className="room-builder-slot">
      <div className="field-label">{t('room.avatarBuilder')}</div>
      <div className="room-builder">
        <div className="room-preview bracketed">
          <div className="corner tl" />
          <div className="corner br" />
          <AvatarFigure config={draft} pose="stand" height={108} seed={seed} />
        </div>
        <div className="room-builder-options">
          <div className="room-option-row">
            <div className="field-label">{t('room.headShape')}</div>
            <ScrollRow selected={Math.max(0, headShapes.findIndex((s) => s.id === draft.head))}>
              {headShapes.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className={`room-option room-option--icon ${draft.head === s.id ? 'room-option--selected' : ''}`}
                  title={t(`room.head.${s.id}`)}
                  aria-label={t(`room.head.${s.id}`)}
                  aria-pressed={draft.head === s.id}
                  onClick={() => setDraft((d) => ({ ...d, head: s.id }))}
                >
                  <HeadShapeIcon head={s.id} size={20} />
                </button>
              ))}
            </ScrollRow>
          </div>
          <div className="room-option-row">
            <div className="field-label">{t('room.eyes')}</div>
            <ScrollRow selected={draft.eyes}>
              {Array.from({ length: EYE_COUNT }, (_, i) => (
                <button
                  key={i}
                  type="button"
                  className={`room-option room-option--icon room-option--eyes ${draft.eyes === i ? 'room-option--selected' : ''}`}
                  aria-label={`${t('room.eyes')} ${i + 1}`}
                  aria-pressed={draft.eyes === i}
                  onClick={() => setDraft((d) => ({ ...d, eyes: i }))}
                >
                  <EyesIcon kind={i} height={14} />
                </button>
              ))}
            </ScrollRow>
          </div>
          <div className="room-option-row">
            <div className="field-label">{t('room.accessory')}</div>
            <ScrollRow selected={draft.accessory}>
              {accessoryOptions.map((a, i) => (
                <button
                  key={a}
                  type="button"
                  className={`room-option room-option--icon room-option--acc ${draft.accessory === i ? 'room-option--selected' : ''}`}
                  title={t(`room.acc.${a}`)}
                  aria-label={t(`room.acc.${a}`)}
                  aria-pressed={draft.accessory === i}
                  onClick={() => setDraft((d) => ({ ...d, accessory: i }))}
                >
                  <AccessoryIcon config={{ ...draft, accessory: i }} height={28} />
                </button>
              ))}
            </ScrollRow>
          </div>
          <div className="room-option-row">
            <div className="field-label">{t('room.body')}</div>
            <ScrollRow selected={draft.body ?? 0}>
              {bodyOptions.map((b, i) => (
                <button
                  key={b}
                  type="button"
                  className={`room-option room-option--icon room-option--body ${(draft.body ?? 0) === i ? 'room-option--selected' : ''}`}
                  title={t(`room.bodyKind.${b}`)}
                  aria-label={t(`room.bodyKind.${b}`)}
                  aria-pressed={(draft.body ?? 0) === i}
                  onClick={() => setDraft((d) => ({ ...d, body: i }))}
                >
                  <BodyIcon kind={b} c={bodyIconColors} height={36} />
                </button>
              ))}
            </ScrollRow>
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
