export const MAX_AUDIO_BYTES = 100 * 1024 * 1024;
export const AUDIO_ACCEPT = 'audio/*,.mp3,.wav,.flac,.ogg,.opus,.m4a,.aac,.weba,.aif,.aiff';
const AUDIO_EXT = /\.(mp3|wav|flac|ogg|opus|m4a|aac|weba|aif|aiff)$/i;

export interface Track {
  id: string;
  name: string;
  size: number;
  modified: number;
  added: number;
}
interface StoredTrack extends Track { blob: Blob }

let opening: Promise<IDBDatabase> | undefined;
function database(): Promise<IDBDatabase> {
  if (!opening) {
    opening = new Promise<IDBDatabase>((resolve, reject) => {
      const req = indexedDB.open('vpc-music', 1);
      req.onupgradeneeded = () => req.result.createObjectStore('tracks', { keyPath: 'id' });
      req.onsuccess = () => {
        req.result.onversionchange = () => { req.result.close(); opening = undefined; };
        resolve(req.result);
      };
      req.onerror = () => reject(req.error);
      req.onblocked = () => reject(new Error('Music storage blocked'));
    }).catch((e: unknown) => { opening = undefined; throw e; });
  }
  return opening;
}

async function transact<T>(mode: IDBTransactionMode, action: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('tracks', mode);
    const req = action(tx.objectStore('tracks'));
    tx.oncomplete = () => resolve(req.result);
    tx.onabort = () => reject(tx.error ?? req.error ?? new Error('Music storage unavailable'));
    tx.onerror = () => { /* onabort reports the failed transaction */ };
  });
}

export async function listTracks(): Promise<Track[]> {
  const rows = await transact<StoredTrack[]>('readonly', (store) => store.getAll());
  return rows.map(({ id, name, size, modified, added }) => ({ id, name, size, modified, added }))
    .sort((a, b) => a.added - b.added || a.name.localeCompare(b.name));
}

export function audioFileError(file: File): 'format' | 'size' | 'empty' | null {
  if (!file.type.startsWith('audio/') && !AUDIO_EXT.test(file.name)) return 'format';
  if (!file.size) return 'empty';
  if (file.size > MAX_AUDIO_BYTES) return 'size';
  return null;
}

export async function saveTrack(file: File): Promise<Track> {
  // Identical file imports replace the same record instead of adding duplicates.
  const id = JSON.stringify([file.name, file.size, file.lastModified]);
  const old = await transact<StoredTrack | undefined>('readonly', (store) => store.get(id));
  const track: Track = { id, name: file.name, size: file.size, modified: file.lastModified, added: old?.added ?? Date.now() };
  await transact('readwrite', (store) => store.put({ ...track, blob: file }));
  return track;
}

export async function readTrack(id: string): Promise<Blob> {
  const row = await transact<StoredTrack | undefined>('readonly', (store) => store.get(id));
  if (!row) throw new Error('Music file missing');
  return row.blob;
}

export async function deleteTrack(id: string): Promise<void> {
  await transact('readwrite', (store) => store.delete(id));
}
