import { useRef, useState } from 'react';
import type { InventoryItem } from '../mockData';
import { useI18n } from '../i18n';
import Icon from '../components/icons';
import ItemPlacer from '../components/ItemPlacer';
import { ICON_CHOICES, itemIcon } from '../components/iconChoices';

/* Карточка инвентаря комнаты: сетка предметов + редактор предмета
   (название, описание, вид — иконка/свой ассет, метка на фоне, «место в
   комнате») + отдельный редактор размещения на фоне (ItemPlacer).
   Источник и запись предметов — родитель (useRoomItems: бэкенд или
   локальный стор). */

export default function InventoryCard({
  items,
  setItems,
  roomBgUrl,
  onOpenEditor,
}: {
  items: InventoryItem[];
  setItems: (next: InventoryItem[]) => void;
  roomBgUrl?: string;
  onOpenEditor: () => void;
}) {
  const { t } = useI18n();
  // Редактор предмета: черновик + признак «новый предмет» (против правки существующего)
  const [itemDraft, setItemDraft] = useState<InventoryItem | null>(null);
  const [itemIsNew, setItemIsNew] = useState(false);
  // Скрытый file input ассета предмета; отдельный редактор размещения на фоне
  const itemImageInputRef = useRef<HTMLInputElement>(null);
  const [placerOpen, setPlacerOpen] = useState(false);

  // Редактор предмета: открытие (новый / правка), сохранение, удаление
  const openNewItem = () => {
    setItemDraft({ id: Date.now(), icon: 'book', name: '', description: '', tag: 'gift' });
    setItemIsNew(true);
  };
  const openEditItem = (item: InventoryItem) => {
    setItemDraft({ ...item });
    setItemIsNew(false);
  };
  const saveItem = () => {
    if (!itemDraft || !itemDraft.name.trim()) return;
    const draft = { ...itemDraft, name: itemDraft.name.trim() };
    setItems(itemIsNew ? [...items, draft] : items.map((i) => (i.id === draft.id ? draft : i)));
    setItemDraft(null);
  };
  const deleteItem = (id: number) => {
    setItems(items.filter((i) => i.id !== id));
    if (itemDraft?.id === id) setItemDraft(null);
  };
  // Загрузка собственного ассета предмета (dataURL заменяет иконку)
  const pickItemImage = (f: File) => {
    if (!f.type.startsWith('image/')) return;
    const reader = new FileReader();
    reader.onload = () => setItemDraft((d) => (d ? { ...d, image: String(reader.result) } : d));
    reader.readAsDataURL(f);
  };

  return (
    <div className="card stagger-item">
      <div className="card-title-row">
        <h3 className="card-title">{t('room.inventory')}</h3>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <button type="button" className="btn btn--ghost" onClick={onOpenEditor}>
            <Icon name="pin" size={13} /> {t('editor.blockTitle')}
          </button>
          <span className="badge">{t('room.itemsBadge', { n: items.length })}</span>
        </span>
      </div>
      <div className="room-inventory-grid">
        {items.map((item) => (
          <div key={item.id} className={`room-item ${itemDraft?.id === item.id ? 'room-item--editing' : ''}`}>
            <div className="room-item-icon">
              {item.image
                ? <img className="room-item-img" src={item.image} alt={item.name} />
                : <Icon name={itemIcon(item.icon)} size={20} />}
              {item.marker && (
                <span className="room-item-pin" title={t('room.markerTitle')}>
                  <Icon name="pin" size={10} />
                </span>
              )}
            </div>
            <div className="room-item-name">{item.name}</div>
            <div className="room-item-desc">{item.description}</div>
            <div className="room-item-foot">
              <span className="badge">{t(`inv.tag.${item.tag}`)}</span>
              <span className="room-item-actions">
                <button type="button" className="btn btn--icon" title={t('room.editItem')} aria-label={t('room.editItem')} onClick={() => openEditItem(item)}>
                  <Icon name="pencil" size={12} />
                </button>
                <button type="button" className="btn btn--icon" title={t('room.deleteItem')} aria-label={t('room.deleteItem')} onClick={() => deleteItem(item.id)}>
                  <Icon name="trash" size={12} />
                </button>
              </span>
            </div>
          </div>
        ))}
      </div>

      {/* Редактор предмета: название, описание, вид (иконка/свой ассет),
          метка на фоне комнаты. Открыт — заменяет кнопку добавления */}
      {itemDraft ? (
        <div className="room-item-editor">
          <div className="field-label">{itemIsNew ? t('room.newItem') : t('room.editItem')}</div>
          <div className="room-item-editor-grid">
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="field-label" htmlFor="room-item-name">{t('room.itemName')}</label>
              <input
                id="room-item-name"
                className="input"
                placeholder={t('room.itemNamePh')}
                value={itemDraft.name}
                onChange={(e) => setItemDraft({ ...itemDraft, name: e.target.value })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label className="field-label" htmlFor="room-item-desc">{t('room.itemDesc')}</label>
              <input
                id="room-item-desc"
                className="input"
                value={itemDraft.description}
                onChange={(e) => setItemDraft({ ...itemDraft, description: e.target.value })}
              />
            </div>
          </div>

          <div className="field-label">{t('room.itemLook')}</div>
          <div className="room-options">
            {ICON_CHOICES.map((name) => (
              <button
                key={name}
                type="button"
                className={`room-option ${!itemDraft.image && itemDraft.icon === name ? 'room-option--selected' : ''}`}
                aria-label={name}
                aria-pressed={!itemDraft.image && itemDraft.icon === name}
                onClick={() => setItemDraft({ ...itemDraft, icon: name, image: undefined })}
              >
                <Icon name={name} size={15} />
              </button>
            ))}
            <button
              type="button"
              className={`room-option room-option--upload ${itemDraft.image ? 'room-option--selected' : ''}`}
              title={t('room.uploadImage')}
              onClick={() => itemImageInputRef.current?.click()}
            >
              {itemDraft.image
                ? <img className="room-item-img" src={itemDraft.image} alt={t('room.uploadImage')} />
                : <><Icon name="photo" size={15} /> {t('room.uploadImage')}</>}
            </button>
            <input
              ref={itemImageInputRef}
              type="file"
              accept="image/*"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) pickItemImage(f);
                e.target.value = '';
              }}
            />
          </div>

          <div className="field-label">{t('room.markerTitle')}</div>
          <div className="room-item-editor-note">
            <button type="button" className="btn btn--ghost" onClick={() => setPlacerOpen(true)}>
              <Icon name="pin" size={13} /> {t('room.placeBtn')}
            </button>
            {itemDraft.marker ? (
              <>
                <span className="ctx-note">{t('room.markerSet')}</span>
                <button type="button" className="btn btn--ghost" onClick={() => setItemDraft({ ...itemDraft, marker: undefined })}>
                  {t('room.markerDel')}
                </button>
              </>
            ) : (
              <span className="ctx-note">{itemDraft.zone ? t('room.markerAuto') : t('room.markerNone')}</span>
            )}
          </div>

          {/* Место в комнате: предмет на сцене можно сделать точкой,
              к которой персона подходит */}
          <div className="field-label">{t('room.placeInRoom')}</div>
          <div className="room-item-editor-note">
            <button
              type="button"
              className={`btn btn--ghost ${itemDraft.spot ? 'room-spot-toggle--on' : ''}`}
              disabled={!itemDraft.marker && !itemDraft.zone}
              onClick={() => setItemDraft({ ...itemDraft, spot: !itemDraft.spot })}
            >
              <Icon name="personas" size={13} /> {itemDraft.spot ? t('room.spotOn') : t('room.spotOff')}
            </button>
            {!itemDraft.marker && !itemDraft.zone && <span className="ctx-note">{t('room.spotNeedMarker')}</span>}
          </div>

          <div className="room-item-editor-actions">
            <button type="button" className="btn btn--ghost" onClick={() => setItemDraft(null)}>
              {t('common.cancel')}
            </button>
            <button type="button" className="btn btn--primary" disabled={!itemDraft.name.trim()} onClick={saveItem}>
              {itemIsNew ? t('common.add') : t('common.apply')}
            </button>
          </div>
        </div>
      ) : (
        <button type="button" className="btn btn--ghost room-add-btn" onClick={openNewItem}>
          + {t('room.addItem')}
        </button>
      )}

      {/* Отдельный редактор размещения предмета на фоне комнаты */}
      {placerOpen && itemDraft && (
        <ItemPlacer
          name={itemDraft.name}
          icon={itemIcon(itemDraft.icon)}
          image={itemDraft.image}
          bg={roomBgUrl}
          initialMarker={itemDraft.marker}
          initialSize={itemDraft.size ?? 6}
          onCancel={() => setPlacerOpen(false)}
          onApply={(marker, size) => {
            setItemDraft({ ...itemDraft, marker: marker ?? undefined, size });
            setPlacerOpen(false);
          }}
        />
      )}
    </div>
  );
}
