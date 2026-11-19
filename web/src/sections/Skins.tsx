import { useI18n } from '../i18n';
import SkinPanel from '../components/SkinPanel';
import SkinThemeGallery from '../components/SkinThemeGallery';
import PageTabs from '../components/PageTabs';

// Раздел «Скины»: темы оформления всего приложения или одной персоны
// (web/skins) и библиотека HTML-скинов персон (SkinPanel несёт собственный
// заголовок/статус скина).
export default function Skins() {
  const { t } = useI18n();

  return (
    <div className="section">
      <div className="section-header">
        <div>
          <h1 className="section-title">{t('nav.skins')}</h1>
          <p className="section-subtitle">{t('skins.subtitle')}</p>
        </div>
      </div>

      <PageTabs
        page="skins"
        tabs={[
          { id: 'themes', label: t('pageTabs.themes'), content: <SkinThemeGallery /> },
          { id: 'skins', label: t('pageTabs.personaSkins'), content: <SkinPanel /> },
        ]}
      />
    </div>
  );
}
