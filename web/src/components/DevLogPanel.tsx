import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { LogEntry, SysMemory } from '../api';
import { useI18n } from '../i18n';
import { useApiOnline } from '../apiData';

/* Панель логов ядра (режим разработчика). Плавает поверх интерфейса справа
   снизу; в развёрнутом виде опрашивает /api/logs каждые 1.5 с инкрементально
   (по seq) и автопрокручивается к свежим записям. Над панелью — чип загрузки
   памяти/свопа хоста (опрос /api/system/memory каждые 3 с): переполненный
   своп приводит к фризам хоста, за цифрой нужно следить. */

const MAX_LOCAL = 500; // сколько записей держим на клиенте

// Пороги чипа памяти: цвет — по ведущим индикаторам фризов (мало available
// или RAM под завязку). Размер свопа — индикатор ЗАПАЗДЫВАЮЩИЙ (мёртвый
// хвост прошлой нагрузки), в цвет не входит: показываем числом.
function memLevel(m: SysMemory): 'ok' | 'warn' | 'crit' {
  const avail = m.mem_available_gb ?? 99;
  const pct = m.mem_percent ?? 0;
  if (pct >= 95 || avail < 1.2) return 'crit';
  if (pct >= 85 || avail < 2.5) return 'warn';
  return 'ok';
}

export default function DevLogPanel() {
  const { t } = useI18n();
  const apiOnline = useApiOnline();
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [mem, setMem] = useState<SysMemory | null>(null);
  const lastSeq = useRef(0);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || !apiOnline) return;
    let stop = false;
    const tick = async () => {
      try {
        const r = await api.getLogs(lastSeq.current);
        if (stop) return;
        lastSeq.current = r.latest;
        if (r.entries.length) {
          setEntries((prev) => [...prev, ...r.entries].slice(-MAX_LOCAL));
        }
      } catch {
        // бэкенд перезапускается — пропускаем тик
      }
    };
    tick();
    const timer = setInterval(tick, 1500);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, [open, apiOnline]);

  // Чип памяти — всегда, пока режим разработчика включён (и свёрнуто, и нет)
  useEffect(() => {
    if (!apiOnline) return;
    let stop = false;
    const tick = async () => {
      try {
        const r = await api.getSysMemory();
        if (!stop) setMem(r.ok ? r : null);
      } catch {
        if (!stop) setMem(null);
      }
    };
    tick();
    const timer = setInterval(tick, 3000);
    return () => {
      stop = true;
      clearInterval(timer);
    };
  }, [apiOnline]);

  // Автопрокрутка к свежим записям
  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight });
  }, [entries, open]);

  if (!apiOnline) return null;

  const memChip = mem && mem.ok ? (
    <div
      className={`devmem-chip devmem-chip--${memLevel(mem)}`}
      style={{ bottom: open ? 344 : 54 }}
      title={t('devmem.title', {
        used: mem.mem_used_gb ?? 0, total: mem.mem_total_gb ?? 0,
        avail: mem.mem_available_gb ?? 0,
        swUsed: mem.swap_used_gb ?? 0, swTotal: mem.swap_total_gb ?? 0,
      })}
    >
      RAM {Math.round(mem.mem_percent ?? 0)}% · SW {(mem.swap_used_gb ?? 0).toFixed(1)}
    </div>
  ) : null;

  if (!open) {
    return (
      <>
        {memChip}
        <button type="button" className="devlog-fab" onClick={() => setOpen(true)}>
          LOGS //
        </button>
      </>
    );
  }

  return (
    <>
      {memChip}
      <div className="devlog-panel">
      <div className="devlog-head">
        <span>{t('devlog.title')}</span>
        <span className="devlog-head-actions">
          <button type="button" className="btn btn--ghost" onClick={() => setEntries([])}>
            {t('devlog.clear')}
          </button>
          <button type="button" className="btn btn--ghost" onClick={() => setOpen(false)}>
            —
          </button>
        </span>
      </div>
      <div className="devlog-body" ref={bodyRef}>
        {entries.length === 0 && <div className="devlog-line devlog-line--dim">// {t('devlog.empty')}</div>}
        {entries.map((e) => (
          <div key={e.seq} className={`devlog-line devlog-line--${e.level.toLowerCase()}`}>
            <span className="devlog-ts">
              {new Date(e.ts * 1000).toLocaleTimeString('ru-RU', { hour12: false })}
            </span>
            <span className="devlog-msg">{e.msg}</span>
          </div>
        ))}
      </div>
      </div>
    </>
  );
}
