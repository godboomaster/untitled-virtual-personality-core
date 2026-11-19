import { Fragment, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useNarrowScreen } from '../useNarrowScreen';
import { useTabStrip } from './useTabStrip';

/* Длинная страница настроек на телефоне — вкладками, как досье персоны:
   заголовки разделов одной строкой (прокрутка вбок), открыт один раздел.
   Строка прилипает к верху, пока раздел листают. Остальные разделы
   остаются в дереве скрытыми — набранное в полях не теряется при смене
   вкладки. Строка и разделы — в одной обёртке: в раскладке страницы
   (колонка с gap на «Главной») это один блок. Широкий экран — всё
   подряд, как было (always — вкладками и там; jump — подряд, но со
   строкой заголовков-переходов). Открытая вкладка запоминается для
   страницы (localStorage — только удобство). */

export interface PageTab {
  id: string;
  label: string;
  content: ReactNode;
}

function readTab(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** page — имя страницы (ключ запомненной вкладки); вкладка false/null —
 * её на этом устройстве нет (раздел не показывается); always — вкладками
 * и на широком экране; jump — на широком экране разделы подряд, а строка
 * заголовков прилипает к верху и переносит к разделу. */
export default function PageTabs({
  page,
  tabs,
  always = false,
  jump = false,
}: {
  page: string;
  tabs: (PageTab | false | null | undefined)[];
  always?: boolean;
  jump?: boolean;
}) {
  const narrow = useNarrowScreen();
  const list = tabs.filter((x): x is PageTab => !!x);
  if (jump && !narrow && !always && list.length >= 2) return <JumpTabs tabs={list} />;
  return <SwitchTabs page={page} list={list} narrow={narrow} always={always} />;
}

function SwitchTabs({ page, list, narrow, always }: { page: string; list: PageTab[]; narrow: boolean; always: boolean }) {
  const key = `vpc-page-tab:${page}`;
  const [picked, setPicked] = useState<string | null>(() => readTab(key));
  const active = list.find((x) => x.id === picked)?.id ?? list[0]?.id ?? '';
  const strip = useTabStrip(active, list.length);
  const hostRef = useRef<HTMLDivElement>(null);

  if ((!narrow && !always) || list.length < 2) return <>{list.map((x) => <Fragment key={x.id}>{x.content}</Fragment>)}</>;

  const pick = (id: string) => {
    setPicked(id);
    try {
      localStorage.setItem(key, id);
    } catch {
      /* без памяти — вкладка до перезапуска */
    }
    // Страница пролистана ниже строки вкладок — новый раздел с его начала
    // (обёртка не липкая: её верх — место строки без прилипания)
    const host = hostRef.current;
    const scroller = host?.closest('.content-scroll');
    if (host && scroller) {
      const d = host.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
      if (d < 0) scroller.scrollTop += d;
    }
  };

  return (
    <div ref={hostRef} className={`page-tabs-host${narrow ? '' : ' page-tabs-host--wide'}`}>
      <div className="page-tabs">
        <div ref={strip.ref} className={strip.className} onScroll={strip.onScroll} role="tablist">
          {list.map((x) => (
            <button
              key={x.id}
              type="button"
              role="tab"
              aria-selected={x.id === active}
              className={`tab ${x.id === active ? 'tab--active' : ''}`}
              onClick={() => pick(x.id)}
            >
              {x.label}
            </button>
          ))}
        </div>
      </div>
      {list.map((x) => (
        <div key={x.id} className="page-tab-panel" role="tabpanel" hidden={x.id !== active}>
          {x.content}
        </div>
      ))}
    </div>
  );
}

// Зазор между низом строки заголовков и началом раздела после перехода
const JUMP_GAP = 20;
// Сколько ждать тишины прокрутки, чтобы считать переход законченным
const JUMP_SETTLE_MS = 160;

/* Широкий экран, режим jump: все разделы видны подряд, строка заголовков
   прилипает к верху прокрутки. Нажатие — плавно к разделу; при обычной
   прокрутке подсвечен раздел, который сейчас под строкой. */
function JumpTabs({ tabs }: { tabs: PageTab[] }) {
  const [active, setActive] = useState(tabs[0].id);
  const strip = useTabStrip(active, tabs.length);
  const hostRef = useRef<HTMLDivElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const panels = useRef(new Map<string, HTMLDivElement>());
  // Идёт переход по нажатию: подсветку держим на выбранном, пока прокрутка
  // не утихнет (раздел у самого низа до верха не доезжает — иначе
  // подсветилась бы последняя вкладка)
  const jumping = useRef<string | null>(null);
  const settle = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const ids = tabs.map((x) => x.id).join('|');

  useEffect(() => {
    const scroller = hostRef.current?.closest<HTMLElement>('.content-scroll');
    if (!scroller) return;
    let frame = 0;
    const spy = () => {
      frame = 0;
      const bar = barRef.current;
      if (!bar) return;
      const line = bar.getBoundingClientRect().bottom + JUMP_GAP + 8;
      const order = ids.split('|');
      let current = order[0];
      if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) {
        current = order[order.length - 1];
      } else {
        for (const id of order) {
          const el = panels.current.get(id);
          if (el && el.getBoundingClientRect().top <= line) current = id;
        }
      }
      setActive((a) => (a === current ? a : current));
    };
    const onScroll = () => {
      if (jumping.current) {
        clearTimeout(settle.current);
        settle.current = setTimeout(() => {
          jumping.current = null;
        }, JUMP_SETTLE_MS);
        return;
      }
      if (!frame) frame = requestAnimationFrame(spy);
    };
    scroller.addEventListener('scroll', onScroll, { passive: true });
    spy();
    return () => {
      scroller.removeEventListener('scroll', onScroll);
      if (frame) cancelAnimationFrame(frame);
      clearTimeout(settle.current);
    };
  }, [ids]);

  const go = (id: string) => {
    const el = panels.current.get(id);
    const bar = barRef.current;
    const scroller = hostRef.current?.closest<HTMLElement>('.content-scroll');
    if (!el || !bar || !scroller) return;
    setActive(id);
    jumping.current = id;
    clearTimeout(settle.current);
    settle.current = setTimeout(() => {
      jumping.current = null;
    }, JUMP_SETTLE_MS);
    // Строка прилипла не всегда (страница ещё не пролистана до неё) —
    // считаем от её высоты, а не от текущего положения
    const top =
      scroller.scrollTop +
      el.getBoundingClientRect().top -
      scroller.getBoundingClientRect().top -
      bar.offsetHeight -
      JUMP_GAP;
    let reduce = false;
    try {
      reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch {
      /* старый движок — плавно */
    }
    scroller.scrollTo({ top: Math.max(0, top), behavior: reduce ? 'auto' : 'smooth' });
  };

  return (
    <div ref={hostRef} className="page-tabs-host page-tabs-host--wide page-tabs-host--jump">
      <div ref={barRef} className="page-tabs">
        <nav ref={strip.ref} className={strip.className} onScroll={strip.onScroll}>
          {tabs.map((x) => (
            <button
              key={x.id}
              type="button"
              aria-current={x.id === active ? 'location' : undefined}
              className={`tab ${x.id === active ? 'tab--active' : ''}`}
              onClick={() => go(x.id)}
            >
              {x.label}
            </button>
          ))}
        </nav>
      </div>
      {tabs.map((x) => (
        <div
          key={x.id}
          ref={(el) => {
            if (el) panels.current.set(x.id, el);
            else panels.current.delete(x.id);
          }}
          className="page-tab-panel"
        >
          {x.content}
        </div>
      ))}
    </div>
  );
}
