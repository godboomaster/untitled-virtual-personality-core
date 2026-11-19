import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import type { Persona } from '../mockData';
import { api, avatarOf, isNativeApp } from '../api';
import type { PortraitModesOverview } from '../api';
import { useApiOnline } from '../apiData';
import type { AvatarModeRef } from '../portrait/AnimCard';
import { useI18n, useMockData } from '../i18n';
import PageTabs from '../components/PageTabs';
import PersonaAvatar from '../components/PersonaAvatar';
import PortraitCard from '../portrait/PortraitCard';
import AnimCard from '../portrait/AnimCard';
import FrameEditor from '../portrait/frames/FrameEditor';
import InitiativeBadgeCard from '../portrait/InitiativeBadgeCard';
import RoomPersonaPicker from '../room/RoomPersonaPicker';
import AvatarOverlayCard from '../phone/AvatarOverlayCard';
import { useNarrowScreen } from '../useNarrowScreen';

/* «Аватар» — голова персоны вне чата. Вкладками (и на компьютере):
   «Портрет» — листы ракурсов головы (голосовой режим, PiP, виджет, рабочий
   стол); «Анимации» — свои листы кадров для рабочего стола; «Кадры» —
   редактор: листы слоями друг на друге, сетка, сдвиг и масштаб кадров;
   «На рабочем столе» — как включить портрет и что с ним делать (панель в
   трее, только компьютер); на телефоне вместо неё — «Поверх приложений»
   (AvatarOverlayCard). Чей аватар — строка кнопок под заголовком (не
   влезли — прокрутка вбок и стрелки), на узком экране — кнопка со списком
   снизу.

   У персоны с режимами (вкладка «Режимы» в досье) под шапкой — выбор
   режима: у каждого режима свой аватар (листы, анимации, кадры; ключ
   «персона@режим», app/api/avatar_modes.py) и переходы в него. Чего у
   режима нет — показывается обычное (подсказка под выбором). Точка у
   режима — он сейчас включён в чате. */

const PERSONA_KEY = 'vpc-avatar-persona';
// Какой режим правим у каждой персоны: {персона: режим}
const MODE_KEY = 'vpc-avatar-mode';

function readModes(): Record<string, string> {
  try {
    const v = JSON.parse(localStorage.getItem(MODE_KEY) ?? '{}') as unknown;
    return v && typeof v === 'object' ? (v as Record<string, string>) : {};
  } catch {
    return {};
  }
}

// Режимы персоны (YAML без ошибок) и что есть у их аватаров
function useAvatarModes(personaId: string, rev: number) {
  const online = useApiOnline();
  const [modes, setModes] = useState<AvatarModeRef[]>([]);
  const [overview, setOverview] = useState<PortraitModesOverview | null>(null);
  useEffect(() => {
    setModes([]);
    if (!online) return;
    let alive = true;
    api
      .getPersonaModes(personaId)
      .then((r) => {
        if (alive) setModes(r.modes.filter((m) => !m.error).map((m) => ({ id: m.id, name: m.name || m.id })));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [personaId, online]);
  useEffect(() => {
    if (!online) return;
    let alive = true;
    api
      .getPortraitModes(personaId)
      .then((r) => {
        if (alive) setOverview(r);
      })
      .catch(() => {
        if (alive) setOverview(null);
      });
    return () => {
      alive = false;
    };
  }, [personaId, online, rev]);
  return { modes, overview };
}

function ModeSwitch({
  modes,
  current,
  overview,
  onPick,
}: {
  modes: AvatarModeRef[];
  current: string;
  overview: PortraitModesOverview | null;
  onPick: (id: string) => void;
}) {
  const { t } = useI18n();
  const all: AvatarModeRef[] = [{ id: '', name: t('avatarMode.base') }, ...modes];
  const info = overview?.modes[current];
  const name = all.find((m) => m.id === current)?.name ?? current;
  let hint = t('avatarMode.hintBase');
  if (current && info) {
    const anims = info.anims > 0 ? t('avatarMode.animsOwn', { n: info.anims }) : info.head ? t('avatarMode.animsNone') : t('avatarMode.animsBase');
    hint = t('avatarMode.hintMode', { mode: name, head: t(info.head ? 'avatarMode.headOwn' : 'avatarMode.headBase'), anims });
  }
  return (
    <div className="avatar-modes">
      <div className="anim-chips" role="group" aria-label={t('avatarMode.label')}>
        <span className="avatar-modes-label">{t('avatarMode.label')}</span>
        {all.map((m) => {
          const own = m.id && overview?.modes[m.id] && (overview.modes[m.id].head || overview.modes[m.id].anims > 0);
          const now = overview?.active === m.id;
          return (
            <button
              key={m.id || '-'}
              type="button"
              className="anim-chip"
              aria-pressed={m.id === current}
              title={[now ? t('avatarMode.now') : '', m.id ? t(own ? 'avatarMode.own' : 'avatarMode.inherit') : ''].filter(Boolean).join(' · ')}
              onClick={() => onPick(m.id)}
            >
              {now && <span className="avatar-mode-now" aria-label={t('avatarMode.now')} />}
              {m.name}
            </button>
          );
        })}
      </div>
      <div className="field-hint">{hint}</div>
    </div>
  );
}

// Как включить портрет на рабочем столе — по шагам
const DESK_STEPS = ['tray', 'who', 'try'] as const;
// Памятка: что сделать — что будет
const DESK_TIPS = ['pet', 'throw', 'file', 'menu', 'call', 'size'] as const;

function readPersona(): string | null {
  try {
    return localStorage.getItem(PERSONA_KEY);
  } catch {
    return null;
  }
}

function DeskTab() {
  const { t } = useI18n();
  return (
    <div className="avatar-split avatar-split--even">
      <div className="card avatar-desk">
        <div className="card-title-row">
          <h3 className="card-title">{t('avatar.desk.howTitle')}</h3>
          <span className="badge hud-code">AVA_03</span>
        </div>
        <ol className="anim-guide-steps">
          {DESK_STEPS.map((k, i) => (
            <li key={k}>
              <span className="avatar-step-n">{i + 1}</span>
              <span>
                <span className="avatar-step-title">{t(`avatar.desk.step.${k}T`)}</span>
                <span className="anim-guide-text">{t(`avatar.desk.step.${k}`)}</span>
              </span>
            </li>
          ))}
        </ol>
      </div>
      <div className="card avatar-tips">
        <div className="card-title-row">
          <h3 className="card-title">{t('avatar.desk.title')}</h3>
        </div>
        <dl className="avatar-desk-list">
          {DESK_TIPS.map((k) => (
            <div key={k} className="avatar-desk-row">
              <dt>{t(`avatar.desk.${k}T`)}</dt>
              <dd>{t(`avatar.desk.${k}`)}</dd>
            </div>
          ))}
        </dl>
      </div>
    </div>
  );
}

// Чей аватар — на широком экране: строка небольших кнопок (лицо и имя
// обычным шрифтом) под заголовком. Не влезли — строка прокручивается вбок,
// стрелки у краёв листают дальше; выбранная всегда в видимой части
function PersonaSwitch({ personas, current, onPick }: { personas: Persona[]; current: Persona; onPick: (id: string) => void }) {
  const { t } = useI18n();
  const rowRef = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ left: false, right: false });

  const update = useCallback(() => {
    const el = rowRef.current;
    if (!el) return;
    const left = el.scrollLeft > 2;
    const right = el.scrollLeft + el.clientWidth < el.scrollWidth - 2;
    setEdges((cur) => (cur.left === left && cur.right === right ? cur : { left, right }));
  }, []);

  // Выбранную — в видимую часть строки (только вбок, страницу не двигаем)
  const reveal = useCallback(() => {
    const el = rowRef.current;
    const btn = el?.querySelector<HTMLElement>('[aria-pressed="true"]');
    if (!el || !btn) return;
    const pad = 40; // под стрелкой у края
    if (btn.offsetLeft < el.scrollLeft + pad) el.scrollLeft = Math.max(0, btn.offsetLeft - pad);
    else if (btn.offsetLeft + btn.offsetWidth > el.scrollLeft + el.clientWidth - pad) {
      el.scrollLeft = btn.offsetLeft + btn.offsetWidth - el.clientWidth + pad;
    }
  }, []);

  // Окно сузили — выбранная не уходит под стрелку
  useEffect(() => {
    const el = rowRef.current;
    if (!el) return;
    update();
    const ro = new ResizeObserver(() => {
      reveal();
      update();
    });
    ro.observe(el);
    el.addEventListener('scroll', update, { passive: true });
    return () => {
      ro.disconnect();
      el.removeEventListener('scroll', update);
    };
  }, [update, reveal, personas.length]);

  useEffect(reveal, [reveal, current.id]);

  const page = (dir: 1 | -1) => {
    const el = rowRef.current;
    if (!el) return;
    const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    el.scrollBy({ left: dir * el.clientWidth * 0.8, behavior: still ? 'auto' : 'smooth' });
  };

  return (
    <div className="avatar-who-bar">
      {edges.left && (
        <button type="button" className="avatar-who-arrow avatar-who-arrow--left" aria-label={t('avatar.whoPrev')} onClick={() => page(-1)}>
          ‹
        </button>
      )}
      <div className="avatar-who" ref={rowRef} role="group" aria-label={t('avatar.who')}>
        {personas.map((p) => (
          <button
            key={p.id}
            type="button"
            className={`avatar-who-btn ${p.id === current.id ? 'avatar-who-btn--active' : ''}`}
            aria-pressed={p.id === current.id}
            onClick={() => onPick(p.id)}
          >
            <span className="room-pick-face" style={p.color ? ({ '--persona': p.color } as CSSProperties) : undefined}>
              <PersonaAvatar persona={p} decorative />
            </span>
            <span className="avatar-who-name">{p.name}</span>
          </button>
        ))}
      </div>
      {edges.right && (
        <button type="button" className="avatar-who-arrow avatar-who-arrow--right" aria-label={t('avatar.whoNext')} onClick={() => page(1)}>
          ›
        </button>
      )}
    </div>
  );
}

export default function Avatar() {
  const { t } = useI18n();
  const { personas } = useMockData();
  const narrow = useNarrowScreen();
  const [pickedId, setPickedId] = useState<string | null>(readPersona);
  const [animsRev, setAnimsRev] = useState(0); // редактор кадров переписал атласы
  const [savedRev, setSavedRev] = useState(0); // что-то записали — что есть у режимов заново
  const [modeOf, setModeOf] = useState<Record<string, string>>(readModes);
  const persona = personas.find((p) => p.id === pickedId) ?? personas[0];
  const { modes, overview } = useAvatarModes(persona?.id ?? '', savedRev);
  const picked = persona ? (modeOf[persona.id] ?? '') : '';
  // Режим удалили (или его YAML с ошибкой) — обычный
  const mode = picked && modes.some((m) => m.id === picked) ? picked : '';
  const pickMode = (id: string) => {
    if (!persona) return;
    const next = { ...modeOf, [persona.id]: id };
    setModeOf(next);
    try {
      localStorage.setItem(MODE_KEY, JSON.stringify(next));
    } catch {
      /* без памяти — выбор до перезапуска */
    }
  };
  const saved = useCallback(() => setSavedRev((v) => v + 1), []);
  const pick = (id: string) => {
    setPickedId(id);
    try {
      localStorage.setItem(PERSONA_KEY, id);
    } catch {
      /* без памяти — выбор до перезапуска */
    }
  };
  const bumpAnims = useCallback(() => {
    setAnimsRev((v) => v + 1);
    setSavedRev((v) => v + 1);
  }, []);
  // Портрет на рабочем столе — только у панели на компьютере; на телефоне —
  // голова поверх приложений
  const phone = isNativeApp();

  if (!persona) return null;
  const avatar = avatarOf(persona.id, mode);
  const modeName = mode ? modes.find((m) => m.id === mode)?.name : undefined;
  const modeRefs: AvatarModeRef[] = modes.length ? [{ id: '', name: t('avatarMode.base') }, ...modes] : [];

  return (
    <div className="section avatar-section">
      <div className="section-header">
        {narrow ? (
          // Заголовок «Аватар» уже в верхней полосе — здесь чей
          <div className="room-pick-bar">
            <RoomPersonaPicker
              personas={personas}
              current={persona}
              statusLine={t(`status.${persona.status}`)}
              onPick={pick}
            />
          </div>
        ) : (
          <div>
            <h2 className="section-title">{t('nav.avatar')}</h2>
            <p className="section-subtitle">{t('avatar.subtitle')}</p>
          </div>
        )}
      </div>

      {!narrow && personas.length > 1 && <PersonaSwitch personas={personas} current={persona} onPick={pick} />}

      {modes.length > 0 && <ModeSwitch modes={modes} current={mode} overview={overview} onPick={pickMode} />}

      {/* У каждой персоны (и у каждого её режима) свой портрет и свои
          анимации: key — заново на смену */}
      <PageTabs
        page="avatar"
        always
        tabs={[
          {
            id: 'portrait',
            label: t('avatar.tab.portrait'),
            content: <PortraitCard key={`p:${avatar}`} persona={persona} avatar={avatar} modeName={modeName} onChanged={saved} />,
          },
          {
            id: 'anims',
            label: t('avatar.tab.anims'),
            content: (
              <AnimCard key={`a:${avatar}`} persona={persona} avatar={avatar} mode={mode} modes={modeRefs} rev={animsRev} onChanged={saved} />
            ),
          },
          {
            id: 'frames',
            label: t('avatar.tab.frames'),
            content: <FrameEditor key={`f:${avatar}`} persona={persona} avatar={avatar} onSaved={bumpAnims} />,
          },
          !phone && {
            id: 'badge',
            label: t('avatar.badge.tab'),
            content: <InitiativeBadgeCard key={`badge:${persona.id}`} persona={persona.id} />,
          },
          !phone && { id: 'desk', label: t(narrow ? 'avatar.tab.desk' : 'avatar.tab.deskLong'), content: <DeskTab /> },
          phone && {
            id: 'overlay',
            label: t(narrow ? 'avatar.tab.overlay' : 'avatar.tab.overlayLong'),
            content: <AvatarOverlayCard key={`o:${persona.id}`} persona={persona} personas={personas} />,
          },
        ]}
      />
    </div>
  );
}
