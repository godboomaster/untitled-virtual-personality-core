// Иконки предметов инвентаря: набор на выбор и безопасный резолвер имени.
// Отдельно от icons.tsx, чтобы компонентный файл экспортировал только компонент.
import type { IconName } from './icons';

// Иконки на выбор для предметов инвентаря
export const ICON_CHOICES: IconName[] = ['book', 'gem', 'cup', 'cards', 'photo', 'pencil', 'frame', 'disc', 'cable'];

// Иконка предмета: известное имя из набора, иначе запасная
export const itemIcon = (icon: string): IconName =>
  ICON_CHOICES.includes(icon as IconName) ? (icon as IconName) : 'gem';
