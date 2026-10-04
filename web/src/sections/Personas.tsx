import { useEffect, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { useI18n, useMockData } from '../i18n';
import { api } from '../api';
import { refetchPersonas, useApiOnline } from '../apiData';
import { consumePersonaCreateRequest, usePersonaCreateRequest } from '../personaCreateStore';
import type { PersonaCreatePrefill } from '../personaCreateStore';
import PersonaCreateModal from '../components/PersonaCreateModal';
import PersonaYamlModal from '../components/PersonaYamlModal';
import InfoButton from '../components/InfoButton';
import Icon from '../components/icons';
import PersonaColorPicker from '../components/PersonaColorPicker';
import { alertDialog, confirmDialog } from '../dialogStore';
import { clearPersonaAvatar, fileToAvatarDataUrl, setPersonaAvatar, usePersonaAvatars } from '../avatarStore';

export default function Personas() {
  const { t } = useI18n();
  // Список персон: при живом бэкенде — реальный (через useMockData → useApiPersonas)
  const { personas } = useMockData();
  const online = useApiOnline();
  const avatars = usePersonaAvatars();
  const [createOpen, setCreateOpen] = useState(false);
  const [prefill, setPrefill] = useState<PersonaCreatePrefill | undefined>(undefined);
  const [yamlEditId, setYamlEditId] = useState<string | null>(null); // персона в YAML-редакторе
  const [avatarEditId, setAvatarEditId] = useState<string | null>(null); // персона, которой меняем аватар

  // Загрузка аватара: скрытый file-input один на секцию, id персоны — в state
  const avatarInputRef = useRef<HTMLInputElement>(null);
  const pickAvatar = (id: string) => {
    setAvatarEditId(id);
    avatarInputRef.current?.click();
  };
  const onAvatarFile = (file: File | undefined) => {
    const id = avatarEditId;
    setAvatarEditId(null);
    if (!id || !file) return;
    fileToAvatarDataUrl(file)
      .catch(() => {
        void alertDialog({ title: t('personas.avatarErrorTitle'), message: t('personas.avatarError') });
        return null;
      })
      .then((dataUrl) => {
        if (dataUrl) return setPersonaAvatar(id, dataUrl);
      })
      .catch((e) =>
        alertDialog({ title: t('personas.avatarErrorTitle'), message: e instanceof Error ? e.message : String(e) }),
      );
  };

  // Запрос на создание из Home («+ Инициализировать персону», панель быстрого создания)
  const request = usePersonaCreateRequest();
  useEffect(() => {
    if (request) {
      setPrefill(request);
      setCreateOpen(true);
      consumePersonaCreateRequest();
    }
  }, [request]);

  const duplicate = (id: string) => {
    api.duplicatePersona(id)
      .then(() => refetchPersonas())
      .catch(() => {});
  };

  // Заморозка/разморозка: features.muted в YAML персоны (применяется на живую)
  const toggleMute = (id: string, muted: boolean) => {
    api.updatePersonaConfig(id, { features: { muted } })
      .then(() => refetchPersonas())
      .catch(() => {});
  };

  // Цвет метки персоны: YAML → перечитать список (карточка, календарь, главная)
  const setColor = async (id: string, color: string | null) => {
    await api.setPersonaColor(id, color);
    await refetchPersonas();
  };

  // Своя копия встроенной персоны удаляется — остаётся встроенная (сброс)
  const remove = async (id: string, name: string, reset: boolean) => {
    const ok = await confirmDialog({
      title: t(reset ? 'personas.resetTitle' : 'personas.deleteTitle', { name }),
      message: t(reset ? 'personas.resetConfirm' : 'personas.deleteConfirm'),
      confirmLabel: reset ? t('personas.reset') : t('common.delete'),
      danger: true,
    });
    if (!ok) return;
    api.deletePersona(id)
      .then(() => refetchPersonas())
      .catch(() => {});
  };

  return (
    <div className="section">
      <div className="section-header">
        <div>
          <h1 className="section-title">
            {t('nav.personas')}
            <InfoButton helpKey="persona.yaml" />
          </h1>
          <p className="section-subtitle">{t('personas.subtitle')}</p>
        </div>
        <button
          className="btn btn--primary"
          onClick={() => {
            setPrefill(undefined);
            setCreateOpen(true);
          }}
        >
          {t('personas.create')}
        </button>
      </div>

      <div className="persona-grid">
        {personas.map((p, i) => (
          <div
            key={p.id}
            className={'card persona-card stagger-item' + (p.muted ? ' persona-card--muted' : '')}
            // --persona — цвет метки персоны из YAML: уголки карточки и буква-аватар
            style={{ animationDelay: `${i * 50}ms`, ...(p.color ? { '--persona': p.color } : {}) } as CSSProperties}
          >
            {/* Угловые скобки HUD в цвете персоны */}
            <span className="corner tl" />
            <span className="corner tr" />
            <span className="corner bl" />
            <span className="corner br" />

            <div className="persona-card-head">
              {/* Аватар: своя картинка или буква; клик — загрузить/заменить */}
              <div className="persona-avatar-wrap">
                <button
                  type="button"
                  className="persona-avatar-btn"
                  title={t('personas.setAvatar')}
                  onClick={() => pickAvatar(p.id)}
                >
                  {avatars[p.id] ? <img src={avatars[p.id]} alt={p.name} /> : <span>{p.name.charAt(0)}</span>}
                  <span className="persona-avatar-overlay">
                    <Icon name="camera" size={18} />
                  </span>
                </button>
                {avatars[p.id] && (
                  <button
                    type="button"
                    className="persona-avatar-remove"
                    title={t('personas.removeAvatar')}
                    aria-label={t('personas.removeAvatar')}
                    onClick={() =>
                      clearPersonaAvatar(p.id).catch((e) =>
                        alertDialog({ title: t('personas.avatarErrorTitle'), message: e instanceof Error ? e.message : String(e) }),
                      )
                    }
                  >
                    <Icon name="close" size={10} />
                  </button>
                )}
              </div>
              <div className="persona-card-title">
                <div className="persona-card-name">{p.name}</div>
                <div className="persona-card-meta">
                  <span>@{p.id}</span>
                  {p.lastReply !== '—' && <span>{t('personas.lastReply', { t: p.lastReply })}</span>}
                </div>
              </div>
              <span className={'persona-status' + (p.muted ? ' persona-status--frozen' : '')}>
                {p.muted ? <Icon name="snowflake" size={11} /> : <span className="persona-status-dot" />}
                {p.muted ? t('personas.statusFrozen') : t('personas.statusActive')}
              </span>
            </div>

            <p className="persona-card-desc" title={p.description}>
              {p.description || t('personas.noDescription')}
            </p>

            <div className="persona-card-block">
              <div className="persona-card-label">
                {'// '}
                {t('personas.features')} · {p.features.length}
                <InfoButton helpKey="persona.features" />
              </div>
              <div className="persona-chips">
                {p.features.length ? (
                  p.features.map((f) => (
                    <span key={f} className="persona-chip">
                      {t(`fb.${f}`)}
                    </span>
                  ))
                ) : (
                  <span className="persona-chip persona-chip--empty">{t('personas.noFeatures')}</span>
                )}
              </div>
            </div>

            {/* Параметры генерации — полоса телеметрии */}
            <div className="persona-telemetry">
              <div className="persona-tele-cell">
                <span className="persona-tele-label">temp</span>
                <span className="persona-tele-value">{p.temperature}</span>
              </div>
              <div className="persona-tele-cell">
                <span className="persona-tele-label">tokens</span>
                <span className="persona-tele-value">{p.maxTokens}</span>
              </div>
              <div className="persona-tele-cell">
                <span className="persona-tele-label">
                  top_p
                  <InfoButton helpKey="persona.genParams" />
                </span>
                <span className="persona-tele-value">{p.topP}</span>
              </div>
            </div>

            {/* Действия с персоной — только при живом бэкенде (в мок-режиме нечего менять) */}
            {online && (
              <div className="persona-card-actions">
                <button className="btn btn--ghost persona-edit-btn" onClick={() => setYamlEditId(p.id)}>
                  <Icon name="pencil" size={13} />
                  {t('common.edit')}
                </button>
                <div className="persona-icon-actions">
                  <PersonaColorPicker color={p.color} onChange={(c) => setColor(p.id, c)} />
                  <button
                    className="persona-icon-btn"
                    title={t('personas.duplicate')}
                    aria-label={t('personas.duplicate')}
                    onClick={() => duplicate(p.id)}
                  >
                    <Icon name="copy" size={15} />
                  </button>
                  <button
                    className={'persona-icon-btn' + (p.muted ? ' persona-icon-btn--on' : '')}
                    title={p.muted ? t('personas.unmute') : t('personas.mute')}
                    aria-label={p.muted ? t('personas.unmute') : t('personas.mute')}
                    aria-pressed={!!p.muted}
                    onClick={() => toggleMute(p.id, !p.muted)}
                  >
                    <Icon name="snowflake" size={15} />
                  </button>
                  {/* Встроенную персону не удалить; свою копию встроенной — сбросить */}
                  {(!p.builtin || p.customized) && (
                    <button
                      className="persona-icon-btn persona-icon-btn--danger"
                      title={p.builtin ? t('personas.reset') : t('common.delete')}
                      aria-label={p.builtin ? t('personas.reset') : t('common.delete')}
                      onClick={() => remove(p.id, p.name, !!p.builtin)}
                    >
                      <Icon name={p.builtin ? 'reset' : 'trash'} size={15} />
                    </button>
                  )}
                  <InfoButton helpKey="persona.actions" />
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Скрытый выбор файла для аватара персоны (id — в avatarEditId) */}
      <input
        ref={avatarInputRef}
        type="file"
        accept="image/*"
        style={{ display: 'none' }}
        onChange={(e) => {
          onAvatarFile(e.target.files?.[0]);
          e.target.value = ''; // тот же файл можно выбрать повторно
        }}
      />

      {/* Модалка создания персоны (также открывается сигналом из Home) */}
      {createOpen && (
        <PersonaCreateModal
          initial={prefill}
          onClose={() => setCreateOpen(false)}
          onCreate={() => {
            refetchPersonas();
            setCreateOpen(false);
          }}
        />
      )}

      {/* YAML-редактор существующей персоны (кнопка «Редактировать») */}
      {yamlEditId && (
        <PersonaYamlModal
          personaId={yamlEditId}
          onClose={() => setYamlEditId(null)}
          onRenamed={(newId) => setYamlEditId(newId)}
        />
      )}
    </div>
  );
}
