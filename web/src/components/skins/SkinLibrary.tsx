/* Библиотека скинов карточками: имя, автор, версия, контракт, какие экраны
   есть (и какие сломаны), каким персонам назначен. Действия: применить к
   выбранной персоне / снять, предпросмотр, цвета, переименовать, обновить
   файлы, скачать, удалить (с подтверждением; удаление снимает скин со всех
   персон). Встроенные скины не переименовываются, а удаление их скрывает —
   вернуть можно кнопкой «Восстановить встроенные» над библиотекой. */

import { useState } from 'react';
import { confirmDialog } from '../../dialogStore';
import { SKIN_CONTRACT_VERSION } from '../../skins/meta';
import {
  SKIN_SCREENS,
  applyEntryColors,
  assignSkin,
  deleteSkin,
  loadSkinFiles,
  skinBroken,
  updateSkin,
} from '../../skins/skinStore';
import type { SkinEntry } from '../../skins/skinStore';
import type { Persona } from '../../mockData';
import { useI18n } from '../../i18n';
import { SCREEN_NAME_KEY, downloadFile, formatSize, skinSlug, skinTitle } from './skinUi';

export type SkinWorkspaceKind = 'preview' | 'colors' | 'upload';

interface SkinLibraryProps {
  entries: SkinEntry[];
  assignments: Record<string, string>;
  persona: Persona;
  personas: Persona[];
  open: { kind: SkinWorkspaceKind; id: string | null } | null;
  onOpen: (kind: SkinWorkspaceKind, id: string) => void;
}

// Скачать файлы скина с перекраской; один файл на все экраны — одним файлом
async function downloadSkin(entry: SkinEntry) {
  const files = await loadSkinFiles(entry.id);
  const unique = new Map<string, string[]>();
  for (const s of SKIN_SCREENS) {
    const html = files[s];
    if (html) unique.set(html, [...(unique.get(html) ?? []), s]);
  }
  const slug = skinSlug(entry.name);
  for (const [html, screens] of unique) {
    const suffix = screens.length === 1 ? '.' + screens[0] : '';
    downloadFile(`skin-${slug}${suffix}.html`, applyEntryColors(html, entry));
  }
}

export default function SkinLibrary({ entries, assignments, persona, personas, open, onOpen }: SkinLibraryProps) {
  const { t } = useI18n();
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);

  const nameOf = (id: string) => personas.find((p) => p.id === id)?.name ?? id;
  const usersOf = (id: string) => Object.keys(assignments).filter((p) => assignments[p] === id);

  const saveName = (entry: SkinEntry) => {
    const name = renaming?.name.trim();
    setRenaming(null);
    if (name && name !== entry.name) updateSkin(entry.id, { name }).catch(() => {});
  };

  const remove = async (entry: SkinEntry) => {
    const users = usersOf(entry.id);
    const ok = await confirmDialog({
      title: t('skin.deleteTitle'),
      message: t(entry.builtin ? 'skin.deleteBuiltinConfirm' : 'skin.deleteConfirm', {
        name: skinTitle(entry, t),
        n: users.length,
      }),
      confirmLabel: t('skin.delete'),
      danger: true,
    });
    if (ok) deleteSkin(entry.id).catch(() => {});
  };

  return (
    <div className="skin-library">
      {entries.map((entry) => {
        const users = usersOf(entry.id);
        const here = assignments[persona.id] === entry.id;
        const broken = skinBroken(entry);
        const isOpen = (kind: SkinWorkspaceKind) => open?.kind === kind && open.id === entry.id;
        return (
          <div key={entry.id} className={`skin-card ${here ? 'skin-card--active' : ''}`}>
            <div className="skin-card-head">
              {renaming?.id === entry.id ? (
                <input
                  className="input skin-card-rename"
                  value={renaming.name}
                  maxLength={200}
                  autoFocus
                  onChange={(e) => setRenaming({ id: entry.id, name: e.target.value })}
                  onBlur={() => saveName(entry)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') saveName(entry);
                    if (e.key === 'Escape') setRenaming(null);
                  }}
                />
              ) : (
                <span className="skin-card-name">{skinTitle(entry, t)}</span>
              )}
              {here && <span className="badge badge--active">{t('skin.appliedHere', { name: persona.name })}</span>}
              {entry.builtin && <span className="badge badge--muted">{t('skin.builtin')}</span>}
              <span
                className={`badge ${entry.contract < SKIN_CONTRACT_VERSION ? 'skin-badge-old' : 'badge--muted'}`}
                title={
                  entry.contract < SKIN_CONTRACT_VERSION
                    ? t('skin.contractOld', { n: entry.contract, cur: SKIN_CONTRACT_VERSION })
                    : undefined
                }
              >
                {t('skin.contract', { n: entry.contract })}
              </span>
            </div>

            {(entry.author || entry.version) && (
              <div className="skin-card-meta">{[entry.author, entry.version && 'v' + entry.version].filter(Boolean).join(' · ')}</div>
            )}

            <div className="skin-card-screens">
              {SKIN_SCREENS.map((s) => (
                <span
                  key={s}
                  className={`skin-chip ${entry.screens[s] ? 'skin-chip--on' : ''} ${broken[s] ? 'skin-chip--broken' : ''}`}
                  title={broken[s] ?? (entry.sizes[s] ? formatSize(entry.sizes[s]!) : undefined)}
                >
                  {t(SCREEN_NAME_KEY[s])}
                </span>
              ))}
            </div>

            <div className="skin-card-users">
              {users.length ? t('skin.usedBy', { list: users.map(nameOf).join(', ') }) : t('skin.usedByNone')}
            </div>

            <div className="skin-card-actions">
              {here ? (
                <button type="button" className="btn btn--ghost" onClick={() => assignSkin(persona.id, null).catch(() => {})}>
                  {t('skin.unassign', { name: persona.name })}
                </button>
              ) : (
                <button type="button" className="btn btn--primary" onClick={() => assignSkin(persona.id, entry.id).catch(() => {})}>
                  {t('skin.assignTo', { name: persona.name })}
                </button>
              )}
              <button
                type="button"
                className={`btn btn--ghost ${isOpen('preview') ? 'skin-btn--open' : ''}`}
                onClick={() => onOpen('preview', entry.id)}
              >
                {t('skin.preview')}
              </button>
              <button
                type="button"
                className={`btn btn--ghost ${isOpen('colors') ? 'skin-btn--open' : ''}`}
                onClick={() => onOpen('colors', entry.id)}
              >
                {t('skin.colors')}
              </button>
              {!entry.builtin && (
                <>
                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => setRenaming({ id: entry.id, name: entry.name })}
                  >
                    {t('skin.rename')}
                  </button>
                  <button
                    type="button"
                    className={`btn btn--ghost ${isOpen('upload') ? 'skin-btn--open' : ''}`}
                    onClick={() => onOpen('upload', entry.id)}
                  >
                    {t('skin.updateFiles')}
                  </button>
                </>
              )}
              <button type="button" className="btn btn--ghost" onClick={() => void downloadSkin(entry)}>
                ⬇ {t('skin.download')}
              </button>
              <button type="button" className="btn btn--danger" onClick={() => void remove(entry)}>
                {t('skin.delete')}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
