/* Постоянный id ядра, с которым телефон работает: приходит в /api/health
   (apiData), по нему coreMove.ts узнаёт своё ядро на новом адресе. */

const KEY = 'vpc-core-id';

export function knownCoreId(): string {
  try {
    return localStorage.getItem(KEY) ?? '';
  } catch {
    return '';
  }
}

/** Выбран другой сервер (новый адрес, QR-код): прежний id — не наш. */
export function forgetCoreId() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* см. выше */
  }
}

export function rememberCoreId(id: unknown) {
  if (typeof id !== 'string' || !/^[A-Za-z0-9]{16,64}$/.test(id) || id === knownCoreId()) return;
  try {
    localStorage.setItem(KEY, id);
  } catch {
    /* хранилище недоступно — без поиска ядра на новом адресе */
  }
}
