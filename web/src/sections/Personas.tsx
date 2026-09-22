import { useEffect, useRef, useState } from 'react';
import { useI18n, useMockData } from '../i18n';
import { api } from '../api';
import { refetchPersonas, useApiOnline } from '../apiData';
import { consumePersonaCreateRequest, usePersonaCreateRequest } from '../personaCreateStore';
import type { PersonaCreatePrefill } from '../personaCreateStore';
import PersonaCreateModal from '../components/PersonaCreateModal';
import PersonaYamlModal from '../components/PersonaYamlModal';
import InfoButton from '../components/InfoButton';
import SkinPanel from '../components/SkinPanel';
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
      .then((dataUrl) => setPersonaAvatar(id, dataUrl))
      .catch(() => window.alert(t('personas.avatarError')));
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

  const remove = (id: string, name: string) => {
    if (!window.confirm(t('personas.deleteConfirm', { name }))) return;
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
            className="card persona-card stagger-item"
            style={{ animationDelay: `${i * 50}ms` }}
          >
            <div className="persona-card-head">
              {/* Аватар: своя картинка или буква; клик — загрузить/заменить */}
              <div className="persona-avatar-wrap">
                <button
                  type="button"
                  className="avatar avatar--large persona-avatar-btn"
                  title={t('personas.setAvatar')}
                  onClick={() => pickAvatar(p.id)}
                >
                  {avatars[p.id] ? (
                    <img src={avatars[p.id]} alt={p.name} />
                  ) : (
                    p.name.charAt(0)
                  )}
                </button>
                {avatars[p.id] && (
                  <button
                    type="button"
                    className="persona-avatar-remove"
                    title={t('personas.removeAvatar')}
                    onClick={() => clearPersonaAvatar(p.id)}
                  >
                    ×
                  </button>
                )}
              </div>
              <div>
                <div className="persona-card-name">{p.name}</div>
                <div className="persona-card-model">{p.model}</div>
              </div>
            </div>
            <p className="persona-card-desc">{p.description}</p>
            <div className="badge-row">
              {p.features.map((f) => (
                <span key={f} className="badge">
                  {t(`fb.${f}`)}
                </span>
              ))}
              <InfoButton helpKey="persona.features" />
            </div>
            <div className="persona-card-params">
              temp {p.temperature} · tokens {p.maxTokens} · top_p {p.topP}
              <InfoButton helpKey="persona.genParams" />
            </div>
            {/* Все персоны всегда активны; частота инициативы зависит от давности ответа */}
            <div className="persona-card-params">
              {t('personas.lastReplyLine', { t: p.lastReply })}
            </div>
            {/* Действия с персоной — только при живом бэкенде (в мок-режиме нечего менять) */}
            {online && (
              <div className="persona-card-actions">
                <button className="btn btn--ghost" onClick={() => setYamlEditId(p.id)}>
                  {t('common.edit')}
                </button>
                <button className="btn btn--ghost" onClick={() => duplicate(p.id)}>
                  {t('personas.duplicate')}
                </button>
                <button className="btn btn--ghost" onClick={() => toggleMute(p.id, !p.muted)}>
                  {p.muted ? t('personas.unmute') : t('personas.mute')}
                </button>
                <button className="btn btn--danger" onClick={() => remove(p.id, p.name)}>
                  {t('common.delete')}
                </button>
                <InfoButton helpKey="persona.actions" />
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

      {/* Скины персон: шаблон → нейросеть → загрузка → предпросмотр */}
      <SkinPanel />

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
        <PersonaYamlModal personaId={yamlEditId} onClose={() => setYamlEditId(null)} />
      )}
    </div>
  );
}
