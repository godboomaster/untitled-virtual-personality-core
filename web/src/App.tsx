import { useEffect, useState } from 'react';
import Sidebar from './components/Sidebar';
import Icon from './components/icons';
import DevLogPanel from './components/DevLogPanel';
import { DetroitBackground } from './effects/DetroitBackground';
import { useI18n, useMockData } from './i18n';
import { recheckCore, useApiOnline, useCoreHealth, useCoreLink } from './apiData';
import { useInbox, useInboxPolling } from './inboxStore';
import { ensureNotifyPermission, OPEN_CHAT_EVENT, OPEN_ROOM_EVENT, setUnreadTitle } from './notifications';
import { requestChatOverview, requestChatPersona, requestRoomPersona } from './chatNavStore';
import { setDevLogOpen, useDevLogOpen, useDevMode } from './devMode';
import { useNarrowScreen } from './useNarrowScreen';
import Start from './sections/Start';
import Home from './sections/Home';
import Chat from './sections/Chat';
import Room from './sections/Room';
import Avatar from './sections/Avatar';
import Personas from './sections/Personas';
import Skins from './sections/Skins';
import ApiKeys from './sections/ApiKeys';
import Services from './sections/Services';
import Connection from './sections/Connection';
import Providers from './sections/Providers';
import Player from './player/Player';
import ImageLightbox from './components/ImageLightbox';
import DialogHost from './components/DialogHost';
import PipHost from './PipHost';
import WidgetHeads from './portrait/widgetHeads';
import { getInitialTheme } from './useAppTheme';
import type { AppTheme } from './useAppTheme';
import { BACK_MENU, useBackHandler } from './backStack';
import { setThemePreference, useAppliedSkin, useEffectiveSkin } from './skinTheme';
import { getSkin } from '../skins';
import { listenBgNotificationTaps, syncBgInbox } from './bgInbox';
import { syncPhoneControl } from './phone/control';
import { usePhoneGemma } from './offline/phoneGemma';
import { syncWakeWord } from './wakeWord';
import { useOfflineMode, useOfflineSync } from './offline/state';
import { useKeysOffer } from './offline/useKeysOffer';
import { usePhoneServices, useServicesOffer } from './offline/useServicesOffer';
import { isStandalone } from './standalone';
import { useUiLookSync } from './uiLookSync';
import { isNativeApp } from './api';
import { useCoreRelocate } from './coreMove';

export type Section = 'start' | 'home' | 'chat' | 'room' | 'avatar' | 'personas' | 'skins' | 'services' | 'connection' | 'providers' | 'settings' | 'player';

// Телефон: «ядро на связи» подписью в шапке — столько после подключения (и
// по нажатию на индикатор), дальше место снова у переключателя темы
const LINK_OK_LABEL_MS = 4000;

type Theme = AppTheme;

export default function App() {
  const [section, setSection] = useState<Section>('home');
  // «Провайдеры» есть только у телефона без связи с ядром: связь вернулась —
  // из вкладки в «Настройки»
  const offlineMode = useOfflineMode();
  useEffect(() => {
    if (section === 'providers' && !offlineMode) setSection('settings');
  }, [section, offlineMode]);
  // Узкий экран (телефон): меню разделов — выдвижная панель по кнопке в шапке
  const [menuOpen, setMenuOpen] = useState(false);
  useBackHandler(menuOpen, BACK_MENU, () => setMenuOpen(false));
  const [theme, setTheme] = useState<Theme>(getInitialTheme);
  const { t, lang } = useI18n();
  // Тема оформления: схема, которая действует (скин с одной схемой — его),
  // и стоит ли скин уже на <html> — тогда стандартный фон не нужен
  const { skinId, scheme } = useEffectiveSkin();
  const skinApplied = useAppliedSkin() != null;
  const skin = getSkin(skinId);
  const schemeLocked = skin != null && skin.schemes.length === 1;
  const { personas } = useMockData();
  // false — бэкенд недоступен: интерфейс показывает моковые данные прототипа
  const apiOnline = useApiOnline();
  // Выбор скина — ядру: по нему рисуется портрет на рабочем столе
  useUiLookSync(apiOnline);
  // Шапка: связь с ядром по опросу /api/health; онлайн — незамороженные персоны
  const coreDown = useCoreHealth() === 'down';
  // Приложение на телефоне с ядром на компьютере открывается, не дожидаясь
  // его (BootGate): связь — подписью в шапке: ищу / на связи / нет связи
  const nativeCore = isNativeApp() && !isStandalone();
  const coreLink = useCoreLink();
  const [okLabel, setOkLabel] = useState(false);
  const [okLabelSeq, setOkLabelSeq] = useState(0);
  useEffect(() => {
    if (!nativeCore || coreLink !== 'ok') return;
    setOkLabel(true);
    const timer = window.setTimeout(() => setOkLabel(false), LINK_OK_LABEL_MS);
    return () => window.clearTimeout(timer);
  }, [nativeCore, coreLink, okLabelSeq]);
  const searching = nativeCore && coreLink === 'searching';
  const personasOnline = personas.filter((p) => p.status !== 'frozen').length;
  const devMode = useDevMode();
  const devLogOpen = useDevLogOpen();
  // Телефон: панель логов — полосой под шапкой, кнопка LOGS — в шапке
  const narrow = useNarrowScreen();
  // Единый поллер фоновых сообщений персон (напоминания, инициативы) —
  // работает на всех экранах, накапливает непрочитанные
  useInboxPolling(apiOnline, personas);
  // Телефон: ноутбук снова на связи — переписка без него уходит на него
  useOfflineSync();
  // Телефон: ключи, введённые без компьютера, — предложить перенести на ядро
  useKeysOffer();
  // Телефон: «Сервисы» без компьютера — режим и доставленное в чат; появился
  // компьютер — предложить перенести их туда
  usePhoneServices();
  useServicesOffer();
  // Телефон: Gemma считает служебные задачи ядра, если её выбрали на телефоне
  usePhoneGemma();
  // Телефон: нет связи — не сменился ли у ядра адрес (ищем его по id в сети)
  useCoreRelocate();
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

  // Ссылка ?chat=<персона> — чат с ней (клик по облачку портрета на рабочем
  // столе открывает веб так); из адреса параметр убираем, чтобы F5 не повторял
  useEffect(() => {
    const url = new URL(window.location.href);
    const personaId = url.searchParams.get('chat');
    if (!personaId) return;
    url.searchParams.delete('chat');
    window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash);
    window.dispatchEvent(new CustomEvent(OPEN_CHAT_EVENT, { detail: personaId }));
  }, []);

  // Тап по виджету комнаты на телефоне → комната этой персоны
  useEffect(() => {
    const openRoom = (e: Event) => {
      const personaId = (e as CustomEvent<string>).detail;
      if (!personaId) return;
      requestRoomPersona(personaId);
      setSection('room');
    };
    window.addEventListener(OPEN_ROOM_EVENT, openRoom);
    return () => window.removeEventListener(OPEN_ROOM_EVENT, openRoom);
  }, []);

  // Приложение на телефоне: тап уведомления фоновой службы — чат персоны
  // (после подписки выше: тап, которым приложение запустили, ждёт в плагине);
  // ядро ответило — служба перезапускается со свежими адресом и токеном
  useEffect(() => {
    syncBgInbox();
    // «Слушать по имени»: служба получает свежие адрес и токен
    syncWakeWord();
    // Управление экраном телефона — тоже
    syncPhoneControl();
    return listenBgNotificationTaps();
  }, []);

  // Запоминаем выбор; на <html> его ставит SkinHost — с поправкой на скин
  useEffect(() => {
    localStorage.setItem('vpc-theme', theme);
    setThemePreference(theme);
  }, [theme]);

  return (
    <div className="app">
      {!skinApplied && <DetroitBackground />}
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
            {/* Без связи логов нет, а место нужно подписи «нет связи» и заголовку */}
            {devMode && narrow && apiOnline && !coreDown && (
              <button
                type="button"
                className={`topbar-devlog${devLogOpen ? ' topbar-devlog--on' : ''}`}
                aria-pressed={devLogOpen}
                onClick={() => setDevLogOpen(!devLogOpen)}
              >
                LOGS
              </button>
            )}
            <div
              className={`theme-toggle${schemeLocked ? ' theme-toggle--locked' : ''}`}
              role="button"
              aria-label={t('topbar.themeToggle')}
              title={schemeLocked && skin ? t(`skinTheme.lockedScheme.${scheme}`, { name: skin.name[lang] }) : undefined}
            >
              {(['light', 'dark'] as const).map((s) => (
                <span
                  key={s}
                  className={`${scheme === s ? 'active' : ''}${schemeLocked && scheme !== s ? ' theme-toggle-off' : ''}`}
                  aria-disabled={schemeLocked && scheme !== s ? true : undefined}
                  onClick={() => {
                    if (!schemeLocked) setTheme(s);
                  }}
                >
                  {s === 'light' ? 'LIGHT' : 'DARK'}
                </span>
              ))}
            </div>
            <div
              className={`topbar-status${
                !apiOnline || (coreDown && !isStandalone() && !searching)
                  ? ' topbar-status--down'
                  : searching
                    ? ' topbar-status--searching'
                    : nativeCore && okLabel
                      ? ' topbar-status--label'
                      : ''
              }`}
              {...(nativeCore
                ? {
                    role: 'button',
                    tabIndex: 0,
                    // Нет связи — проверить сейчас; на связи — показать подпись
                    onClick: () => (coreLink === 'down' ? recheckCore() : setOkLabelSeq((n) => n + 1)),
                  }
                : {})}
            >
              <span className="status-led" />
              <span>
                {!apiOnline
                  ? t('topbar.mock')
                  : isStandalone()
                    ? t('topbar.standalone')
                    : searching
                      ? t('topbar.searching')
                      : coreDown
                        ? t('topbar.down')
                        : nativeCore && narrow
                          ? t('topbar.okShort')
                          : t('topbar.status', { n: personasOnline, total: personas.length })}
              </span>
              {!apiOnline && <span className="badge badge--muted">{t('topbar.mockBadge')}</span>}
            </div>
          </div>
        </header>
        {devMode && narrow && <DevLogPanel docked />}
        <div className="content-scroll" hidden={section === 'player'}>
          <div key={section} className="section-enter">
            {section === 'start' && <Start onNavigate={setSection} />}
            {section === 'home' && <Home onNavigate={setSection} />}
            {section === 'chat' && <Chat />}
            {section === 'room' && <Room />}
            {section === 'avatar' && <Avatar />}
            {section === 'personas' && <Personas />}
            {section === 'skins' && <Skins />}
            {section === 'services' && <Services />}
            {section === 'connection' && <Connection />}
            {section === 'providers' && offlineMode && <Providers />}
            {section === 'settings' && <ApiKeys onNavigate={setSection} />}
          </div>
        </div>
        <Player visible={section === 'player'} onOpen={() => setSection('player')} />
      </main>
      {devMode && !narrow && <DevLogPanel />}
      {/* Лайтбокс: клик по контентной картинке — крупный просмотр */}
      <ImageLightbox />
      {/* Подтверждения и сообщения на странице (dialogStore) вместо window.confirm/alert */}
      <DialogHost />
      {/* PiP-окно (комната или портрет): живёт над разделами, переживает уход из них */}
      <PipHost />
      {/* Телефон: листы головы для виджета «Живой портрет» у персон без своего портрета */}
      <WidgetHeads personas={personas} />
    </div>
  );
}
