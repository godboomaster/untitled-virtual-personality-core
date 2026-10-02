import { useI18n } from '../i18n';
import SkinPanel from '../components/SkinPanel';

// Раздел «Скины»: библиотека внешнего вида персон, вынесена из «Персон»
// в свой раздел (SkinPanel несёт собственный заголовок/статус скина).
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

      <SkinPanel />
    </div>
  );
}
