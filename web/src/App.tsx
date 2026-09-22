import { useEffect, useState } from 'react';
import Sidebar from './components/Sidebar';
import DevLogPanel from './components/DevLogPanel';
import { DetroitBackground } from './effects/DetroitBackground';
import { useI18n, useMockData } from './i18n';
import { useApiOnline } from './apiData';
import { useInbox, useInboxPolling } from './inboxStore';
import { ensureNotifyPermission, OPEN_CHAT_EVENT, setUnreadTitle } from './notifications';
import { requestChatPersona } from './chatNavStore';
import { useDevMode } from './devMode';
import Home from './sections/Home';
import Chat from './sections/Chat';
import Room from './sections/Room';
import Personas from './sections/Personas';
import ApiKeys from './sections/ApiKeys';
import ImageLightbox from './components/ImageLightbox';

export type Section = 'home' | 'chat' | 'room' | 'personas' | 'settings';

type Theme = 'light' | 'dark';

// Начальная тема: сохранённый выбор или системное предпочтение
function getInitialTheme(): Theme {
  const saved = localStorage.getItem('vpc-theme');
  if (saved === 'light' || saved === 'dark') return saved;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export default function App() {
  const [section, setSection] = useState<Section>('home');
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const { t } = useI18n();
  const { personas } = useMockData();
  // false — бэкенд недоступен: интерфейс показывает моковые данные прототипа
  const apiOnline = useApiOnline();
  const devMode = useDevMode();
  // Единый поллер фоновых сообщений персон (напоминания, инициативы) —
  // работает на всех экранах, накапливает непрочитанные
  useInboxPolling(apiOnline, personas);
  // Присутствие (гейт фона) репортит экран чата — там известно, ЧЕЙ чат
  // открыт; ключ отметки — (персона, чат), см. presence.ts
  const { unread } = useInbox();

  // Разрешение системных уведомлений спрашиваем по первому клику —
  // браузеры требуют пользовательский жест, повторно не беспокоим
  useEffect(() => {
    const ask = () => {
      ensureNotifyPermission();
      window.removeEventListener('pointerdown', ask);
    };
    window.addEventListener('pointerdown', ask);
    return () => window.removeEventListener('pointerdown', ask);
  }, []);

  // Счётчик непрочитанных в заголовке вкладки: «(2) Virtual Persona Core»
  const totalUnread = Object.values(unread).reduce((sum, n) => sum + n, 0);
  useEffect(() => {
    setUnreadTitle(totalUnread);
  }, [totalUnread]);

  // Клик по системному уведомлению → фокус окна и чат с этой персоной
  useEffect(() => {
    const openChat = (e: Event) => {
      const personaId = (e as CustomEvent<string>).detail;
      if (!personaId) return;
      requestChatPersona(personaId);
      setSection('chat');
    };
    window.addEventListener(OPEN_CHAT_EVENT, openChat);
    return () => window.removeEventListener(OPEN_CHAT_EVENT, openChat);
  }, []);

  // Применяем тему к <html> и запоминаем выбор
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('vpc-theme', theme);
  }, [theme]);

  return (
    <div className="app">
      <DetroitBackground />
      <Sidebar current={section} onSelect={setSection} />
      <main className="content">
        <header className="topbar">
          <div className="topbar-title">{t(`nav.${section}`)}</div>
          <div className="topbar-right">
            <div className="theme-toggle" role="button" aria-label={t('topbar.themeToggle')}>
              <span className={theme === 'light' ? 'active' : ''} onClick={() => setTheme('light')}>
                LIGHT
              </span>
              <span className={theme === 'dark' ? 'active' : ''} onClick={() => setTheme('dark')}>
                DARK
              </span>
            </div>
            <div className="topbar-status">
              <span className="status-led" />
              <span>{t('topbar.status', { n: personas.length })}</span>
              {!apiOnline && <span className="badge badge--muted">MOCK</span>}
            </div>
          </div>
        </header>
        <div className="content-scroll">
          <div key={section} className="section-enter">
            {section === 'home' && <Home onNavigate={setSection} />}
            {section === 'chat' && <Chat />}
            {section === 'room' && <Room />}
            {section === 'personas' && <Personas />}
            {section === 'settings' && <ApiKeys />}
          </div>
        </div>
      </main>
      {devMode && <DevLogPanel />}
      {/* Лайтбокс: клик по контентной картинке — крупный просмотр */}
      <ImageLightbox />
    </div>
  );
}
