import { useState } from 'react';
import type { CSSProperties } from 'react';
import type { Section } from '../App';

interface SidebarProps {
  current: Section;
  onSelect: (section: Section) => void;
}

const navItems: { id: Section; label: string; icon: string }[] = [
  { id: 'chat', label: 'Чат', icon: '◱' },
  { id: 'personas', label: 'Персоны', icon: '◉' },
  { id: 'memory', label: 'Память', icon: '▤' },
  { id: 'tasks', label: 'Напоминания и задачи', icon: '◔' },
  { id: 'initiative', label: 'Инициатива', icon: '✦' },
  { id: 'settings', label: 'Настройки', icon: '⚙' },
];

export default function Sidebar({ current, onSelect }: SidebarProps) {
  const [collapsed, setCollapsed] = useState(false);
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
            data-label={item.label}
            onClick={() => onSelect(item.id)}
          >
            <span className="nav-item-icon">{item.icon}</span>
            <span className="nav-item-label">{item.label}</span>
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
          <span className="sidebar-footer-text">UI-прототип · моковые данные</span>
        </div>
      </div>
      <button
        type="button"
        className="sidebar-toggle"
        onClick={() => setCollapsed((v) => !v)}
        title={collapsed ? 'Развернуть меню' : 'Свернуть меню'}
      >
        {collapsed ? '»' : '«'}
      </button>
    </aside>
  );
}
