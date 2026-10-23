import { useI18n } from '../i18n';
import SkinPanel from '../components/SkinPanel';
import SkinThemeGallery from '../components/SkinThemeGallery';

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

      <SkinThemeGallery />

      <SkinPanel />
    </div>
  );
}
