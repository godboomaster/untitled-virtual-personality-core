/* SkinFrame — хост-обёртка скина: рендерит файл скина в sandboxed
   iframe (opaque origin, без доступа к приложению, localStorage и сети)
   и обменивается с ним сообщениями по узкому протоколу:

     хост → скин:  { vpc:'host', type:'state', payload }   снапшот данных
                   { vpc:'host', type:'send-result', sid, ok } принята ли отправка
     скин → хост:  { vpc:'skin', type:'ready' }            скин загрузился
                   { vpc:'skin', type:'send', text, image?, sid } отправка сообщения
                   { vpc:'skin', type:'clear' }            очистка диалога
                   { vpc:'skin', type:'select-persona', id }
                   { vpc:'skin', type:'open-dossier' | 'close-dossier' }
                   { vpc:'skin', type:'action', action, values, id }
                   { vpc:'skin', type:'set-setting', key, value }
                   { vpc:'skin', type:'zoom-image', src }  клик по картинке —
                                                          хост открывает лайтбокс
                   { vpc:'skin', type:'key', key, code, ctrlKey, … } хоткей
                                                          при фокусе в скине
                   { vpc:'skin', type:'error', message }   runtime-ошибка скина

   Каждое событие скина несёт gen — метку документа (prepareSkin вшивает её
   в bridge): события прошлого документа того же iframe (смена скина/экрана)
   отбрасываются. Второй load одного и того же документа значит, что скин
   увёл iframe на другую страницу: хост перестаёт с ним общаться, гасит
   документ и сообщает об ошибке (скин отключается).

   Снапшот уходит не чаще раза за кадр и только если изменился; ready
   принимается один раз на документ. Входящие события скина проходят
   лимиты частоты и размера: скин — сторонний код. */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { SyntheticEvent } from 'react';
import { prepareSkin } from '../skins/engine';
import type { SkinScreen } from '../skins/engine';
import type { SkinStatePayload } from '../skins/payloads';
import { openImageZoom } from '../imageZoomStore';

interface SkinFrameProps {
  skin: string; // исходный файл скина (до prepareSkin)
  screen: SkinScreen;
  state: SkinStatePayload;
  onSend?: (text: string, image?: string) => void;
  onClear?: () => void;
  onSelectPersona?: (id: string) => void;
  onOpenDossier?: () => void;
  onCloseDossier?: () => void;
  // Действия записи из скина: кнопки [data-vpc-action] и [data-vpc-item-action]
  onAction?: (action: string, values: Record<string, string>, id: string | null) => void;
  // Инпуты настроек [data-vpc-setting]
  onSetSetting?: (key: string, value: string) => void;
  onError?: (message: string) => void;
  className?: string;
  title?: string;
}

// Лимиты входящих событий скина: отдельные токен-бакеты для хоткеев
// (автоповтор зажатой клавиши) и остальных событий — повтор клавиши
// не должен съедать клик «отправить»
const RATE_PER_SEC = 30;
const RATE_BURST = 30;
const KEY_RATE_PER_SEC = 20;
const KEY_RATE_BURST = 20;
const SEND_INTERVAL_MS = 500; // send — не чаще раза в полсекунды (лишние ждут в очереди)
const SEND_QUEUE_MAX = 5;
const TEXT_MAX = 4000;
const IMAGE_MAX_CHARS = 8 * 1024 * 1024; // data-URL картинки ~8 МБ (bridge ужимает больше)
const ACTION_MAX_KEYS = 32;
const ACTION_KEY_MAX = 64;
const ACTION_VALUE_MAX = 4000;
const ID_MAX = 200;
const SETTING_VALUE_MAX = 1000;

// Документ вместо скина, уведшего iframe на чужую страницу
const DEAD_DOC = '<!DOCTYPE html><meta http-equiv="Content-Security-Policy" content="default-src \'none\'"><title>-</title>';
const SKIN_NAVIGATED_ERROR = 'skin navigated away from its document';

// Отпечаток длинной строки (data-URL): длина + начало, конец и выборка
function fingerprint(s: string): string {
  const step = Math.max(1, Math.floor(s.length / 256));
  let out = s.length + '#' + s.slice(0, 64);
  for (let i = 64; i < s.length - 64; i += step) out += s.charAt(i);
  return out + s.slice(-64);
}

// Дешёвая сигнатура снапшота: картинки (data-URL) не сериализуются целиком,
// обычный текст — целиком (правку в середине длинного факта/сообщения
// отпечаток бы не заметил)
function payloadSignature(p: SkinStatePayload): string {
  return JSON.stringify(p, (_k, v: unknown) =>
    typeof v === 'string' && v.length > 512 && v.startsWith('data:') ? fingerprint(v) : v,
  );
}

const clip = (v: unknown, max: number) => String(v ?? '').slice(0, max);

// Значения действия: только строки, ограниченное число ключей и длина
function sanitizeValues(raw: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
  for (const [k, v] of Object.entries(raw as Record<string, unknown>).slice(0, ACTION_MAX_KEYS)) {
    if (v != null && typeof v === 'object') continue;
    out[k.slice(0, ACTION_KEY_MAX)] = clip(v, ACTION_VALUE_MAX);
  }
  return out;
}

// Токен-бакет: true — событие укладывается в лимит
function makeBucket(perSec: number, burst: number) {
  let tokens = burst;
  let last = performance.now();
  return () => {
    const now = performance.now();
    tokens = Math.min(burst, tokens + ((now - last) / 1000) * perSec);
    last = now;
    if (tokens < 1) return false;
    tokens -= 1;
    return true;
  };
}

// Метка документа скина: случайная, чтобы чужая страница её не угадала
function newGen(): string {
  const a = new Uint32Array(2);
  crypto.getRandomValues(a);
  return a[0].toString(36) + a[1].toString(36);
}

interface QueuedSend {
  text: string;
  image?: string;
  sid: unknown;
  gen: string;
  personaId: string | undefined;
}

export default function SkinFrame({
  skin,
  screen,
  state,
  onSend,
  onClear,
  onSelectPersona,
  onOpenDossier,
  onCloseDossier,
  onAction,
  onSetSetting,
  onError,
  className,
  title,
}: SkinFrameProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  // Последний снапшот — чтобы отправить его по сигналу ready
  const stateRef = useRef(state);
  stateRef.current = state;
  // Актуальные обработчики: подписка на message одна на всё время жизни,
  // инлайновые колбэки родителя не пересоздают её на каждый рендер
  const handlersRef = useRef({
    onSend, onClear, onSelectPersona, onOpenDossier, onCloseDossier, onAction, onSetSetting, onError,
  });
  handlersRef.current = {
    onSend, onClear, onSelectPersona, onOpenDossier, onCloseDossier, onAction, onSetSetting, onError,
  };

  // Полный документ скина: CSP + bridge (с меткой документа) + активный экран
  const doc = useMemo(() => {
    const gen = newGen();
    return { gen, srcdoc: prepareSkin(skin, screen, { gen }) };
  }, [skin, screen]);

  /* Жизненный цикл документа: метка текущего документа, сколько раз
     загрузился его iframe (второй load — скин ушёл на другую страницу),
     принят ли уже его ready и не погашен ли он. На каждый документ —
     новый iframe (key), так что load старого сюда не долетит */
  const genRef = useRef(doc.gen);
  const loadRef = useRef<{ el: HTMLIFrameElement | null; loads: number }>({ el: null, loads: 0 });
  const readyRef = useRef<string | null>(null);
  const deadRef = useRef(false);
  const [deadGen, setDeadGen] = useState<string | null>(null);

  // Отправка снапшота внутрь iframe: не чаще раза за кадр и только если
  // он изменился с прошлой отправки (при стриминге ответа рендер — на каждый токен)
  const sentRef = useRef<{ state: SkinStatePayload | null; sig: string | null }>({ state: null, sig: null });
  const rafRef = useRef<number | null>(null);

  // Новый документ скина: layout-эффект отрабатывает сразу после смены
  // srcdoc, раньше любого load/message нового документа
  useLayoutEffect(() => {
    genRef.current = doc.gen;
    readyRef.current = null;
    deadRef.current = false;
    // Прошлая отправка новому документу не досталась
    sentRef.current = { state: null, sig: null };
  }, [doc]);

  const postToSkin = (msg: Record<string, unknown>) => {
    if (deadRef.current) return;
    iframeRef.current?.contentWindow?.postMessage(msg, '*');
  };

  const postState = (force: boolean) => {
    if (deadRef.current || !iframeRef.current?.contentWindow) return;
    const cur = stateRef.current;
    const sent = sentRef.current;
    if (!force && sent.state === cur) return;
    const sig = payloadSignature(cur);
    sent.state = cur;
    if (!force && sent.sig === sig) return;
    sent.sig = sig;
    postToSkin({ vpc: 'host', type: 'state', payload: cur });
  };

  // Документ загрузился. Первый load метки — сам скин; повторный — iframe
  // ушёл на другую страницу (location/ссылка/reload): дальше снапшоты
  // уходили бы чужому документу. Гасим документ и отключаем скин
  const onLoad = (e: SyntheticEvent<HTMLIFrameElement>) => {
    const el = e.currentTarget;
    if (el !== iframeRef.current) return;
    const l = loadRef.current;
    if (l.el !== el) {
      loadRef.current = { el, loads: 1 };
      return;
    }
    l.loads += 1;
    if (deadRef.current) return;
    deadRef.current = true;
    setDeadGen(genRef.current);
    handlersRef.current.onError?.(SKIN_NAVIGATED_ERROR);
  };

  // Снапшот меняется → планируем пересылку на ближайший кадр
  // (bridge применит его, когда будет готов; до ready он всё равно придёт повторно)
  useEffect(() => {
    if (rafRef.current != null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      postState(false);
    });
  });

  // Ссылку обнуляем: StrictMode и Fast Refresh снимают и заново ставят
  // эффекты, и с «висящим» id кадра планирование выше больше не сработало бы
  useEffect(
    () => () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    },
    [],
  );

  // Приём событий от скина
  useEffect(() => {
    const allow = makeBucket(RATE_PER_SEC, RATE_BURST);
    const allowKey = makeBucket(KEY_RATE_PER_SEC, KEY_RATE_BURST);

    // Очередь отправок: вторая отправка в пределах SEND_INTERVAL_MS ждёт,
    // а не теряется; результат уходит скину (при отказе bridge вернёт текст
    // и картинку в поле ввода)
    const queue: QueuedSend[] = [];
    let lastSend = -Infinity;
    let timer: number | null = null;
    const sendResult = (sid: unknown, ok: boolean, reason?: string) => {
      if (typeof sid !== 'number') return;
      postToSkin({ vpc: 'host', type: 'send-result', sid, ok, ...(reason ? { reason } : {}) });
    };
    const pump = () => {
      timer = null;
      while (queue.length) {
        const wait = lastSend + SEND_INTERVAL_MS - performance.now();
        if (wait > 0) {
          timer = window.setTimeout(pump, wait);
          return;
        }
        const item = queue.shift()!;
        // Документ сменился или персона другая — отправлять уже некуда
        if (item.gen !== genRef.current || deadRef.current) continue;
        if (item.personaId !== stateRef.current.persona?.id) {
          sendResult(item.sid, false, 'persona');
          continue;
        }
        lastSend = performance.now();
        handlersRef.current.onSend?.(item.text, item.image);
        sendResult(item.sid, true);
      }
    };

    const onMessage = (e: MessageEvent) => {
      if (deadRef.current || e.source !== iframeRef.current?.contentWindow) return;
      const d = e.data;
      if (!d || d.vpc !== 'skin' || typeof d.type !== 'string') return;
      // События прошлого документа (смена скина/экрана в том же iframe)
      if (d.gen !== genRef.current) return;
      // ready — служебный сигнал загрузки документа, принимается один раз
      if (d.type === 'ready') {
        if (readyRef.current === genRef.current) return;
        readyRef.current = genRef.current;
        postState(true);
        return;
      }
      if (d.type === 'key' ? !allowKey() : !allow()) {
        if (d.type === 'send') sendResult(d.sid, false, 'rate');
        return;
      }
      const h = handlersRef.current;
      switch (d.type) {
        case 'send': {
          const text = typeof d.text === 'string' ? d.text.slice(0, TEXT_MAX) : '';
          // Картинка — только inline data:image/ разумного размера; не
          // подошла — отказ целиком (bridge вернёт её в поле ввода)
          let image: string | undefined;
          if (d.image != null) {
            if (typeof d.image !== 'string' || !d.image.startsWith('data:image/') || d.image.length > IMAGE_MAX_CHARS) {
              sendResult(d.sid, false, 'image');
              break;
            }
            image = d.image;
          }
          if (!text.trim() && !image) {
            sendResult(d.sid, false, 'empty');
            break;
          }
          if (queue.length >= SEND_QUEUE_MAX) {
            sendResult(d.sid, false, 'rate');
            break;
          }
          queue.push({ text, image, sid: d.sid, gen: genRef.current, personaId: stateRef.current.persona?.id });
          if (timer == null) pump();
          break;
        }
        case 'clear':
          h.onClear?.();
          break;
        case 'select-persona':
          if (typeof d.id === 'string' && d.id) h.onSelectPersona?.(d.id.slice(0, ID_MAX));
          break;
        case 'open-dossier':
          h.onOpenDossier?.();
          break;
        case 'close-dossier':
          h.onCloseDossier?.();
          break;
        case 'action':
          if (typeof d.action === 'string' && d.action) {
            h.onAction?.(
              d.action.slice(0, ACTION_KEY_MAX),
              sanitizeValues(d.values),
              d.id != null && typeof d.id !== 'object' ? clip(d.id, ID_MAX) : null,
            );
          }
          break;
        case 'set-setting':
          if (typeof d.key === 'string' && d.key) {
            h.onSetSetting?.(d.key.slice(0, ACTION_KEY_MAX), clip(d.value, SETTING_VALUE_MAX));
          }
          break;
        case 'error':
          h.onError?.(typeof d.message === 'string' ? d.message.slice(0, 1000) : 'unknown error');
          break;
        case 'zoom-image':
          // Клик по картинке внутри скина — открыть лайтбокс на хосте
          // (только inline-источники: сеть скину закрыта, чужие URL не нужны)
          if (typeof d.src === 'string' && /^(data:image\/|blob:)/.test(d.src) && d.src.length <= IMAGE_MAX_CHARS) {
            openImageZoom(d.src);
          }
          break;
        case 'key': {
          // Хоткей при фокусе внутри скина: повторяем keydown в документе
          // приложения (слушатели на window его тоже получат — всплытие).
          // Только Escape и сочетания с модификаторами — Enter/символы скин
          // не может «нажать» за пользователя (напр. подтвердить диалог)
          if (typeof d.key !== 'string' || d.key.length > 32) break;
          const mods = d.ctrlKey === true || d.metaKey === true || d.altKey === true;
          if (d.key !== 'Escape' && !mods) break;
          document.dispatchEvent(
            new KeyboardEvent('keydown', {
              key: d.key,
              code: typeof d.code === 'string' ? d.code.slice(0, 32) : '',
              ctrlKey: d.ctrlKey === true,
              metaKey: d.metaKey === true,
              altKey: d.altKey === true,
              shiftKey: d.shiftKey === true,
              bubbles: true,
              cancelable: true,
            }),
          );
          break;
        }
      }
    };
    window.addEventListener('message', onMessage);
    return () => {
      window.removeEventListener('message', onMessage);
      if (timer != null) window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <iframe
      key={doc.gen}
      ref={iframeRef}
      className={className}
      title={title ?? 'skin'}
      // allow-scripts без allow-same-origin: opaque origin, нет localStorage/сети/доступа к родителю
      sandbox="allow-scripts"
      srcDoc={deadGen === doc.gen ? DEAD_DOC : doc.srcdoc}
      onLoad={onLoad}
    />
  );
}
