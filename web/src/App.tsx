import { useState } from 'react';
import Sidebar from './components/Sidebar';
import { DetroitBackground } from './effects/DetroitBackground';
import { personas } from './mockData';
import Chat from './sections/Chat';
import Personas from './sections/Personas';
import Memory from './sections/Memory';
import Tasks from './sections/Tasks';
import Initiative from './sections/Initiative';
import Settings from './sections/Settings';

export type Section = 'chat' | 'personas' | 'memory' | 'tasks' | 'initiative' | 'settings';

const sectionTitles: Record<Section, string> = {
  chat: 'Чат',
  personas: 'Персоны',
  memory: 'Память',
  tasks: 'Напоминания и задачи',
  initiative: 'Инициатива',
  settings: 'Настройки',
};

export default function App() {
  const [section, setSection] = useState<Section>('chat');
  const activePersona = personas.find((p) => p.active);

  return (
    <div className="app">
      <DetroitBackground />
      <Sidebar current={section} onSelect={setSection} />
      <main className="content">
        <header className="topbar">
          <div className="topbar-title">{sectionTitles[section]}</div>
          <div className="topbar-status">
            <span className="status-led" />
            <span>
              ВСЕ СИСТЕМЫ В НОРМЕ{activePersona ? ` · ${activePersona.name}` : ''}
            </span>
          </div>
        </header>
        <div className="content-scroll">
          <div key={section} className="section-enter">
            {section === 'chat' && <Chat />}
            {section === 'personas' && <Personas />}
            {section === 'memory' && <Memory />}
            {section === 'tasks' && <Tasks />}
            {section === 'initiative' && <Initiative />}
            {section === 'settings' && <Settings />}
          </div>
        </div>
      </main>
    </div>
  );
}
