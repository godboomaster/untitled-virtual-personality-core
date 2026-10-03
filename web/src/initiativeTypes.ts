import type { InitiativeEvent } from './mockData';

// Тип самоинициативы бэкенда → тип события в интерфейсе (раздел
// «Самоинициатива» и досье скина)
export const INIT_TYPE_MAP: Record<string, InitiativeEvent['type']> = {
  continuation: 'continuation',
  memory_recall: 'observation',
  self_reflection: 'thought',
  user_reflection: 'thought',
  todo_reflection: 'observation',
  inventory_reflection: 'observation',
};
