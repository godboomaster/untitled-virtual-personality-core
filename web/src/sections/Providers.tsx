import { useI18n } from '../i18n';
import PhoneKeysCard from '../offline/PhoneKeysCard';
import WebChatsCard from '../offline/WebChatsCard';
import OfflineCard from '../offline/OfflineCard';
import PageTabs from '../components/PageTabs';
import { isStandalone } from '../standalone';

/* «Провайдеры» — вкладка телефона без связи с ядром (в меню только тогда):
   кто может отвечать за персон — облачные модели по ключам (с
   компьютера и введённые на телефоне), веб-чаты без ключей, Gemma на
   телефоне. В каком порядке — у каждой персоны свой, в её досье →
   «Настройки» (PhoneChainCard). С ядром на связи отвечает ядро своими
   провайдерами, и вкладки нет. */

export default function Providers() {
  const { t } = useI18n();
  const standalone = isStandalone();
  return (
    <div className="section">
      <div className="section-header">
        <div>
          <h1 className="section-title">{t('nav.providers')}</h1>
          <p className="section-subtitle">{t(standalone ? 'providers.subtitleStandalone' : 'providers.subtitle')}</p>
        </div>
      </div>

      <p className="field-hint">{t('providers.orderHint')}</p>
      <PageTabs
        page="providers"
        tabs={[
          { id: 'keys', label: t('pageTabs.keys'), content: <PhoneKeysCard standalone={standalone} /> },
          { id: 'webchats', label: t('pageTabs.webchats'), content: <WebChatsCard /> },
          { id: 'gemma', label: 'Gemma', content: <OfflineCard /> },
        ]}
      />
    </div>
  );
}
