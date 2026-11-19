import { useEffect, useState } from 'react';
import { useI18n } from '../i18n';
import { api, ApiError, isMissingRoute, isNativeApp } from '../api';
import type { LoreFileView } from '../api';
import FormModal from './FormModal';
import { saveBlob } from '../sentFiles';

/* Файл лора из вкладки «Лор»: «Текст» — как его прочитало ядро (после
   разбора pdf/docx) с заголовками, которые увидела нарезка; «Куски» — ровно
   то, что ищет поиск: на реплику персона получает до десятка таких кусков,
   каждый вместе с названием раздела; «PDF» — сам файл во встроенном
   просмотре браузера. Исходник любого формата можно скачать. Встроенно
   открывается только PDF: html, открытый со страницы веба, исполнил бы
   чужие скрипты. В приложении на телефоне WebView PDF не показывает.
   local — файл из памяти телефона без ядра (PhoneLorePanel): готовый вид,
   без запросов и скачиваний; текста нет (файл с ядра) — только куски. */

type Tab = 'text' | 'pieces' | 'pdf';

function errText(e: unknown, t: (key: string) => string): string {
  if (isMissingRoute(e)) return t('common.coreOutdated');
  return e instanceof ApiError || e instanceof Error ? e.message : String(e);
}

export default function LoreFileModal({
  personaId,
  name,
  onClose,
  local,
}: {
  personaId: string;
  name: string;
  onClose: () => void;
  local?: LoreFileView;
}) {
  const { t } = useI18n();
  const [view, setView] = useState<LoreFileView | null>(null);
  const [error, setError] = useState<unknown>(null); // сама ошибка: текст — при отрисовке, на языке веба
  const [tab, setTab] = useState<Tab>('text');
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [pdfError, setPdfError] = useState<unknown>(null);
  const [saved, setSaved] = useState(''); // на телефоне — куда сохранён файл

  useEffect(() => {
    if (local) {
      setView(local);
      setTab(local.blocks.length ? 'text' : 'pieces');
      return;
    }
    let stale = false;
    api
      .getLoreFile(personaId, name)
      .then((v) => {
        if (stale) return;
        setView(v);
        // PDF с сохранённым оригиналом — сразу сам файл
        if (v.ext === 'pdf' && v.original && !isNativeApp()) setTab('pdf');
      })
      .catch((e) => !stale && setError(e ?? 'error'));
    return () => {
      stale = true;
    };
  }, [personaId, name, local]);

  // Встроенный PDF: blob с типом, иначе браузер не включит просмотр
  useEffect(() => {
    if (tab !== 'pdf' || pdfUrl || pdfError != null) return;
    let stale = false;
    api
      .loreOriginal(personaId, name)
      .then((blob) => {
        if (stale) return;
        setPdfUrl(URL.createObjectURL(new Blob([blob], { type: 'application/pdf' })));
      })
      .catch((e) => !stale && setPdfError(e ?? 'error'));
    return () => {
      stale = true;
    };
  }, [tab, pdfUrl, pdfError, personaId, name]);

  useEffect(
    () => () => {
      if (pdfUrl) URL.revokeObjectURL(pdfUrl);
    },
    [pdfUrl],
  );

  // В приложении на телефоне <a download> не работает — плагин сохраняет в «Загрузки»
  const save = (fname: string, blob: Blob) =>
    saveBlob(fname, blob).then(() => isNativeApp() && setSaved(t('lore.savedPhone', { name: fname })));

  const downloadOriginal = () => {
    api
      .loreOriginal(personaId, name)
      .then((blob) => save(name, blob))
      .catch((e) => setError(e ?? 'error'));
  };

  const downloadText = () => {
    if (!view) return;
    const text = view.blocks.map((b) => (b.h ? `${'#'.repeat(Math.min(b.h, 6))} ${b.t}` : b.t)).join('\n\n');
    void save(`${name.replace(/\.[^.]+$/, '')}.txt`, new Blob([text], { type: 'text/plain;charset=utf-8' })).catch((e) =>
      setError(e ?? 'error'),
    );
  };

  const canPdf = !!view && view.ext === 'pdf' && view.original && !isNativeApp();
  const plainText = !!view && (view.ext === 'md' || view.ext === 'txt');
  const tabs: Tab[] = canPdf ? ['pdf', 'text', 'pieces'] : view && !view.blocks.length ? ['pieces'] : ['text', 'pieces'];

  return (
    <FormModal
      title={name}
      badge={view ? t('lore.viewBadge', { chars: view.chars, chunks: view.chunks }) : undefined}
      xl
      onClose={onClose}
    >
      <div className="lore-view">
        {error != null && <div className="field-hint">⚠ {errText(error, t)}</div>}
        {!view && error == null && <div className="yaml-view yaml-view--note">{t('yaml.loading')}</div>}
        {view && (
          <>
            <div className="lore-view-head">
              <div className="tabs lore-view-tabs">
                {tabs.map((id) => (
                  <button
                    key={id}
                    type="button"
                    className={`tab ${tab === id ? 'tab--active' : ''}`}
                    onClick={() => setTab(id)}
                  >
                    {id === 'pdf' ? 'PDF' : id === 'text' ? t('lore.viewText') : t('lore.viewPieces', { n: view.chunks })}
                  </button>
                ))}
              </div>
              <div className="lore-view-actions" hidden={!!local}>
                {view.original ? (
                  <button type="button" className="btn btn--ghost" onClick={downloadOriginal}>
                    {t('lore.downloadOriginal', { ext: view.ext.toUpperCase() || 'FILE' })}
                  </button>
                ) : null}
                <button type="button" className="btn btn--ghost" onClick={downloadText}>
                  {t('lore.downloadText')}
                </button>
              </div>
            </div>
            {!local && !view.original && !plainText && <div className="field-hint">{t('lore.noOriginal')}</div>}
            {saved && <div className="field-hint">✓ {saved}</div>}

            <div className="lore-view-body">
              {tab === 'pdf' &&
                (pdfError != null ? (
                  <div className="field-hint">⚠ {errText(pdfError, t)}</div>
                ) : pdfUrl ? (
                  <iframe className="lore-pdf" src={pdfUrl} title={name} />
                ) : (
                  <div className="yaml-view yaml-view--note">{t('yaml.loading')}</div>
                ))}

              {tab === 'text' && (
                <div className="lore-doc">
                  {view.blocks.map((b, i) =>
                    b.h ? (
                      <div key={i} className={`lore-doc-h lore-doc-h${Math.min(b.h, 4)}`}>
                        {b.t}
                      </div>
                    ) : (
                      <p key={i}>{b.t}</p>
                    ),
                  )}
                </div>
              )}

              {tab === 'pieces' && (
                <>
                  <div className="field-hint">{t('lore.piecesHint')}</div>
                  <ol className="lore-pieces">
                    {view.pieces.map((p, i) => (
                      <li key={i} className="lore-piece">
                        <div className="lore-piece-meta">
                          #{i + 1}
                          {p.s ? ` · ${p.s}` : ''} · {t('lore.pieceChars', { n: p.t.length })}
                        </div>
                        <div className="lore-piece-text">{p.t}</div>
                      </li>
                    ))}
                  </ol>
                </>
              )}
            </div>
          </>
        )}
      </div>
    </FormModal>
  );
}
