import { useEffect, useState } from 'react';
import type { CSSProperties } from 'react';
import type { Section } from '../App';
import { useI18n } from '../i18n';
import { useApiOnline, useCoreHealth } from '../apiData';
import Icon from './icons';
import type { IconName } from './icons';

interface SidebarProps {
  current: Section;
  onSelect: (section: Section) => void;
  // Узкий экран: панель выдвигается поверх контента (open) и закрывается
  // тапом мимо неё или Esc; на широком экране оба пропа ни на что не влияют
  open?: boolean;
  onClose?: () => void;
}

const navItems: { id: Section; icon: IconName }[] = [
  { id: 'start', icon: 'start' },
  { id: 'home', icon: 'home' },
  { id: 'chat', icon: 'chat' },
  { id: 'room', icon: 'room' },
  { id: 'personas', icon: 'personas' },
  { id: 'skins', icon: 'skins' },
  { id: 'settings', icon: 'settings' },
];

export default function Sidebar({ current, onSelect, open = false, onClose }: SidebarProps) {
  const [collapsed, setCollapsed] = useState(false);
  const { t } = useI18n();
  const apiOnline = useApiOnline();
  // Связь с ядром: есть, пока опрос /api/health не сказал обратное
  const coreHealth = useCoreHealth();
  const coreOk = apiOnline && coreHealth !== 'down';
  const activeIndex = navItems.findIndex((item) => item.id === current);

  useEffect(() => {
    if (!open || !onClose) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  return (
    <>
    {open && <div className="sidebar-backdrop" onClick={onClose} aria-hidden="true" />}
    <aside className={`sidebar ${collapsed ? 'sidebar--collapsed' : ''}${open ? ' sidebar--open' : ''}`}>
      <div className="sidebar-logo">
        <span className="sidebar-logo-icon">◉</span>
        <div className="sidebar-logo-text">
          <div className="sidebar-logo-title">Virtual Persona</div>
          <div className="sidebar-logo-sub">Core · web</div>
        </div>
      </div>
      <nav className="sidebar-nav" style={{ '--active-index': activeIndex } as CSSProperties}>
        <span className="nav-indicator" aria-hidden="true" />
        {navItems.map((item) => (
          <button
            key={item.id}
            className={`nav-item ${current === item.id ? 'nav-item--active' : ''}`}
            data-label={t(`nav.${item.id}`)}
            onClick={() => onSelect(item.id)}
          >
            <span className="nav-item-icon">
              <Icon name={item.icon} size={17} />
            </span>
            <span className="nav-item-label">{t(`nav.${item.id}`)}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-footer">
        <div className={`sidebar-status-row ${coreOk ? 'sidebar-status-row--ok' : 'sidebar-status-row--down'}`}>
          <span className="status-led" /> <span className="sidebar-footer-text">{coreOk ? 'SYSTEM ONLINE' : 'SYSTEM OFFLINE'}</span>
        </div>
        <div className="sidebar-status-row">
          <span className="sidebar-footer-text">VPC CORE · BUILD 0.1.0</span>
        </div>
        {/* Моковые данные — только без ядра (прототипный режим) */}
        {!apiOnline && (
          <div className="sidebar-status-row">
            <span className="sidebar-footer-text">{t('sidebar.footerPrototype')}</span>
          </div>
        )}
      </div>
      <button
        type="button"
        className="sidebar-toggle"
        onClick={() => setCollapsed((v) => !v)}
        title={collapsed ? t('sidebar.expand') : t('sidebar.collapse')}
      >
        {collapsed ? '»' : '«'}
      </button>
    </aside>
    </>
  );
}
