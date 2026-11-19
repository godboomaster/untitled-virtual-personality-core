import { isNativeApp } from '../api';
import { useI18n } from '../i18n';
import ConnectionCard, { BackgroundCard } from '../link/ConnectionCard';
import LinkSettings from '../link/LinkSettings';
import NetworkCores from '../link/NetworkCores';
import PageTabs from '../components/PageTabs';

/* «Подключение». На телефоне: ядро (связь сейчас, адрес, смена сервера),
   ядра в этой сети («кто тут ядро?»), что работает при закрытом приложении.
   На компьютере: телефоны — QR-код для нового, сопряжённые, токен API. */

export default function Connection() {
  const { t } = useI18n();
  const native = isNativeApp();
  return (
    <div className="section">
      <div className="section-header">
        <div>
          <h1 className="section-title">{t('nav.connection')}</h1>
          <p className="section-subtitle">{t(native ? 'connection.subtitlePhone' : 'connection.subtitleCore')}</p>
        </div>
      </div>
      {native ? (
        <PageTabs
          page="connection"
          tabs={[
            { id: 'core', label: t('pageTabs.core'), content: <ConnectionCard /> },
            { id: 'network', label: t('pageTabs.network'), content: <NetworkCores /> },
            { id: 'background', label: t('pageTabs.background'), content: <BackgroundCard /> },
          ]}
        />
      ) : (
        <LinkSettings />
      )}
    </div>
  );
}
