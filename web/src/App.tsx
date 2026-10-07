import { useEffect, useState } from 'react';
import Sidebar from './components/Sidebar';
import Icon from './components/icons';
import DevLogPanel from './components/DevLogPanel';
import { DetroitBackground } from './effects/DetroitBackground';
import { useI18n, useMockData } from './i18n';
import { useApiOnline, useCoreHealth } from './apiData';
import { useInbox, useInboxPolling } from './inboxStore';
import { ensureNotifyPermission, OPEN_CHAT_EVENT, setUnreadTitle } from './notifications';
import { requestChatOverview, requestChatPersona } from './chatNavStore';
import { useDevMode } from './devMode';
import Start from './sections/Start';
import Home from './sections/Home';
import Chat from './sections/Chat';
import Room from './sections/Room';
import Personas from './sections/Personas';
import Skins from './sections/Skins';
import ApiKeys from './sections/ApiKeys';
import ImageLightbox from './components/ImageLightbox';
import DialogHost from './components/DialogHost';
import RoomPipHost from './room/RoomPip';
import { getInitialTheme } from './useAppTheme';
import type { AppTheme } from './useAppTheme';
import { BACK_MENU, useBackHandler } from './backStack';
import { syncSystemBars } from './nativeBars';
import { listenBgNotificationTaps, syncBgInbox } from './bgInbox';

export type Section = 'start' | 'home' | 'chat' | 'room' | 'personas' | 'skins' | 'settings';

type Theme = AppTheme;

export default function App() {
  const [section, setSection] = useState<Section>('home');
  // Узкий экран (телефон): меню разделов — выдвижная панель по кнопке в шапке
  const [menuOpen, setMenuOpen] = useState(false);
  useBackHandler(menuOpen, BACK_MENU, () => setMenuOpen(false));
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const { t } = useI18n();
  const { personas } = useMockData();
  // false — бэкенд недоступен: интерфейс показывает моковые данные прототипа
  const apiOnline = useApiOnline();
  // Шапка: связь с ядром по опросу /api/health; онлайн — незамороженные персоны
  const coreDown = useCoreHealth() === 'down';
  const personasOnline = personas.filter((p) => p.status !== 'frozen').length;
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

  // Приложение на телефоне: тап уведомления фоновой службы — чат персоны
  // (после подписки выше: тап, которым приложение запустили, ждёт в плагине);
  // ядро ответило — служба перезапускается со свежими адресом и токеном
  useEffect(() => {
    syncBgInbox();
    return listenBgNotificationTaps();
  }, []);

  // Применяем тему к <html> и запоминаем выбор
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('vpc-theme', theme);
    syncSystemBars(theme);
  }, [theme]);

  return (
    <div className="app">
      <DetroitBackground />
      <Sidebar
        current={section}
        open={menuOpen}
        onClose={() => setMenuOpen(false)}
        onSelect={(next) => {
          // Повторный клик по «Чат» внутри открытого чата — к странице всех чатов
          if (next === 'chat' && section === 'chat') requestChatOverview();
          setSection(next);
          setMenuOpen(false);
        }}
      />
      <main className="content">
        <header className="topbar">
          <button
            type="button"
            className="topbar-menu"
            aria-label={t('topbar.menu')}
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen(true)}
          >
            <Icon name="menu" size={20} />
            {totalUnread > 0 && <span className="topbar-menu-dot" aria-hidden="true" />}
          </button>
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
            <div className={`topbar-status${!apiOnline || coreDown ? ' topbar-status--down' : ''}`}>
              <span className="status-led" />
              <span>
                {!apiOnline
                  ? t('topbar.mock')
                  : coreDown
                    ? t('topbar.down')
                    : t('topbar.status', { n: personasOnline, total: personas.length })}
              </span>
              {!apiOnline && <span className="badge badge--muted">MOCK</span>}
            </div>
          </div>
        </header>
        <div className="content-scroll">
          <div key={section} className="section-enter">
            {section === 'start' && <Start onNavigate={setSection} />}
            {section === 'home' && <Home onNavigate={setSection} />}
            {section === 'chat' && <Chat />}
            {section === 'room' && <Room />}
            {section === 'personas' && <Personas />}
            {section === 'skins' && <Skins />}
            {section === 'settings' && <ApiKeys />}
          </div>
        </div>
      </main>
      {devMode && <DevLogPanel />}
      {/* Лайтбокс: клик по контентной картинке — крупный просмотр */}
      <ImageLightbox />
      {/* Подтверждения и сообщения на странице (dialogStore) вместо window.confirm/alert */}
      <DialogHost />
      {/* PiP-окно комнаты: живёт над разделами, переживает уход из «Комнаты» */}
      <RoomPipHost />
    </div>
  );
}
