import { createContext } from 'react';
import type { ComponentType, ReactNode } from 'react';
import type { TaskCard } from './api';

/* Части интерфейса, которые скин может нарисовать по-своему
   (web/skins/<id>/parts.tsx, именованные экспорты). Данные и логика — общие:
   часть получает то же, что стандартный компонент, и только иначе это
   показывает. Нет части — стандартный компонент. */

type T = (key: string, vars?: Record<string, string | number>) => string;

export interface SkinTaskCardProps {
  card: TaskCard;
  t: T;
}

export interface SkinBrowserStateProps {
  /** connecting | waiting | no_browser | no_tab | error | off */
  status: string;
  persona: { id: string; name: string };
  /** Под экраном — последний кадр страницы (бледно) */
  stale: boolean;
  t: T;
}

/** Что сейчас на экране запуска (components/BootGate) */
export type SkinBootPhase = 'run' | 'personas' | 'ready' | 'offline' | 'auth' | 'denied' | 'server';

export interface SkinBootScreenProps {
  phase: SkinBootPhase;
  /** Пояснение базового экрана для этой фазы — на телефоне и в редких ветках
   *  (ядро отказало, нет связи с ноутбуком) свои тексты */
  lead: string;
  /** Адрес ядра */
  host: string;
  attempt: number;
  /** Сколько прошло, «00:04» */
  elapsed: string;
  /** Идёт опрос ядра (на форме адреса — нет): есть что показать про попытки */
  polling: boolean;
  /** Приложение на телефоне: ядро — на другом компьютере */
  native: boolean;
  t: T;
  /** Поля и кнопки экрана — команда запуска, QR-код и адрес, токен,
   *  «Посмотреть на примере»; их рисует BootGate, скин только ставит */
  children: ReactNode;
}

export interface SkinParts {
  /** Окно задачи режима управления в ленте чата */
  TaskCard?: ComponentType<SkinTaskCardProps>;
  /** Экран состояния в окне браузера агента, пока страницы нет или нет связи */
  BrowserState?: ComponentType<SkinBrowserStateProps>;
  /** Экран запуска, пока ядро поднимается (или не отвечает, или просит токен) */
  BootScreen?: ComponentType<SkinBootScreenProps>;
}

export const SkinPartsContext = createContext<SkinParts | null>(null);
