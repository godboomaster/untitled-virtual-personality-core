import { useState } from 'react';
import type { CSSProperties } from 'react';
import type { Section } from '../App';
import { useI18n } from '../i18n';
import Icon from './icons';
import type { IconName } from './icons';

interface SidebarProps {
  current: Section;
  onSelect: (section: Section) => void;
}

const navItems: { id: Section; icon: IconName }[] = [
  { id: 'home', icon: 'home' },
  { id: 'chat', icon: 'chat' },
  { id: 'room', icon: 'room' },
  { id: 'personas', icon: 'personas' },
  { id: 'settings', icon: 'settings' },
];

export default function Sidebar({ current, onSelect }: SidebarProps) {
  const [collapsed, setCollapsed] = useState(false);
  const { t } = useI18n();
  const activeIndex = navItems.findIndex((item) => item.id === current);

  return (
    <aside className={`sidebar ${collapsed ? 'sidebar--collapsed' : ''}`}>
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
        <div className="sidebar-status-row sidebar-status-row--ok">
          <span className="status-led" /> <span className="sidebar-footer-text">SYSTEM ONLINE</span>
        </div>
        <div className="sidebar-status-row">
          <span className="sidebar-footer-text">VPC CORE · BUILD 0.1.0</span>
        </div>
        <div className="sidebar-status-row">
          <span className="sidebar-footer-text">{t('sidebar.footerPrototype')}</span>
        </div>
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
  );
}
