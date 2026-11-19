import { useI18n } from '../i18n';
import type { AnimTrigger, PortraitAnim } from './animTypes';

/* Связки анимации в карточке (шаг «Когда играть»): после каких других она
   играет (любой из них: та кончилась — эта), перед какими (сначала эта,
   потом та) и как часто связка срабатывает (chance, %). Играет цепочки
   портрет на столе (desktop/src/portrait/useBody.ts). */

// Движения окна: перед ними ничего не успевает — окно уже едет
const MOTION: ReadonlySet<AnimTrigger> = new Set(['walk', 'grab', 'fall', 'jump']);
const motionOnly = (a: PortraitAnim) => a.triggers.length > 0 && a.triggers.every((tr) => MOTION.has(tr)) && !a.keys?.length;

export interface AnimLinksValue {
  after: string[];
  before: string[];
  chance: number;
}

export default function AnimLinks({
  selfId,
  anims,
  value,
  onChange,
}: {
  /** Правим эту (null — новая, ещё без id) */
  selfId: string | null;
  anims: PortraitAnim[];
  value: AnimLinksValue;
  onChange: (next: AnimLinksValue) => void;
}) {
  const { t } = useI18n();
  const others = anims.filter((a) => a.id !== selfId);
  const name = (a: PortraitAnim) => a.name || t('anim.untitled');
  const names = (ids: string[]) =>
    ids
      .map((id) => others.find((a) => a.id === id))
      .filter((a): a is PortraitAnim => !!a)
      .map((a) => `«${name(a)}»`)
      .join(', ');
  const flip = (k: 'after' | 'before', id: string) => {
    const list = value[k];
    onChange({ ...value, [k]: list.includes(id) ? list.filter((x) => x !== id) : [...list, id] });
  };
  const linked = value.after.length > 0 || value.before.length > 0;
  const p = value.chance;
  const chanceText = p >= 100 ? t('anim.links.always') : p <= 50 ? t('anim.links.oneIn', { p, n: Math.round(100 / p) }) : t('anim.links.percent', { p });
  // Зацикленная кончается сама, только если её выключили той же клавишей
  const loopTargets = others.filter((a) => value.after.includes(a.id) && a.loop && !motionOnly(a));
  // Кто связан с этой с их стороны
  const comeAfter = selfId ? others.filter((a) => a.after?.includes(selfId)) : [];
  const goBefore = selfId ? others.filter((a) => a.before?.includes(selfId)) : [];

  const row = (k: 'after' | 'before') => (
    <div className="anim-links-row">
      <div className="anim-links-label">{t(`anim.links.${k}`)}</div>
      <div className="anim-chips" role="group" aria-label={t(`anim.links.${k}`)}>
        {others.map((a) => {
          const off = k === 'before' && motionOnly(a) && !value.before.includes(a.id);
          return (
            <button
              key={a.id}
              type="button"
              className="anim-chip"
              aria-pressed={value[k].includes(a.id)}
              disabled={off}
              title={off ? t('anim.links.motionNo') : undefined}
              onClick={() => flip(k, a.id)}
            >
              {name(a)}
            </button>
          );
        })}
      </div>
    </div>
  );

  return (
    <div className="anim-links">
      <div className="anim-chip-group-label">{t('anim.links.title')}</div>
      {others.length === 0 ? (
        <div className="field-hint">{t('anim.links.none')}</div>
      ) : (
        <>
          {row('after')}
          {row('before')}
          {linked && (
            <div className="field anim-links-chance">
              <label className="field-label" htmlFor={`anim-chance-${selfId ?? 'new'}`}>
                {t('anim.links.chance')}
              </label>
              <div className="anim-rate-value">{chanceText}</div>
              <input
                id={`anim-chance-${selfId ?? 'new'}`}
                type="range"
                min={5}
                max={100}
                step={5}
                value={p}
                onChange={(e) => onChange({ ...value, chance: Number(e.target.value) })}
              />
            </div>
          )}
          {value.after.length > 0 && <div className="field-hint">{t('anim.links.sayAfter', { names: names(value.after), chance: chanceText })}</div>}
          {value.before.length > 0 && <div className="field-hint">{t('anim.links.sayBefore', { names: names(value.before), chance: chanceText })}</div>}
          {loopTargets.length > 0 && <div className="field-hint">{t('anim.links.loopNote', { names: loopTargets.map((a) => `«${name(a)}»`).join(', ') })}</div>}
          {comeAfter.length > 0 && <div className="field-hint">{t('anim.links.comeAfter', { names: comeAfter.map((a) => `«${name(a)}»`).join(', ') })}</div>}
          {goBefore.length > 0 && <div className="field-hint">{t('anim.links.goBefore', { names: goBefore.map((a) => `«${name(a)}»`).join(', ') })}</div>}
          <div className="field-hint">{t('anim.links.hint')}</div>
        </>
      )}
    </div>
  );
}
