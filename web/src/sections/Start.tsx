import type { Section } from '../App';
import { useI18n } from '../i18n';
import StartGuide from '../components/StartGuide';
import StartControl from '../components/StartControl';

interface StartProps {
  onNavigate: (s: Section) => void;
}

// Страница «Старт»: как пользоваться уже запущенными ботами — обучение
// (консоль и спецфункции), режим управления и в конце команды Telegram.
// Установка ядра — не здесь
export default function Start({ onNavigate }: StartProps) {
  const { t } = useI18n();

  return (
    <div className="section home">
      <div className="home-hero bracketed">
        <div className="corner tl plus" />
        <div className="corner tr" />
        <div className="corner bl" />
        <div className="corner br plus" />
        <div className="home-readout top-right">
          TRACKS <span className="val">03</span>
          <br />
          MODE <span className="live-tag">BRIEFING</span>
        </div>
        <div className="home-eyebrow">{t('start.eyebrow')}</div>
        <h1 className="home-title start-title glitch" data-text={t('start.title')}>
          {t('start.title')}
        </h1>
        <p className="home-lead">{t('start.lead')}</p>
        <div className="home-cta-row">
          <button
            className="btn btn--primary"
            onClick={() => document.getElementById('start-control')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
          >
            {t('start.ctaControl')}
          </button>
          <button
            className="btn btn--ghost"
            onClick={() => document.getElementById('start-telegram')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
          >
            {t('start.ctaTelegram')}
          </button>
          <button className="btn btn--ghost" onClick={() => onNavigate('home')}>
            {t('start.ctaHome')}
          </button>
        </div>
      </div>

      <div className="chrome-bar" />

      {/* Обучение: консоль (персона, провайдеры, разговор) и спецфункции */}
      <StartGuide
        onNavigate={onNavigate}
        trackIds={['console', 'features']}
        titleKey="guide.title"
        num="01 / BRIEFING"
        leadKey="guide.lead"
        allDoneKey="guide.allDone"
      />

      {/* Режим управления: как включить, войти, командовать и выйти */}
      <StartControl onNavigate={onNavigate} />

      {/* Telegram — в самом конце: слэш-команды, личка и группы */}
      <StartGuide
        id="start-telegram"
        onNavigate={onNavigate}
        trackIds={['telegram']}
        titleKey="guide.tgTitle"
        num="03 / TELEGRAM"
        leadKey="guide.tgLead"
        allDoneKey="guide.tgAllDone"
      />
    </div>
  );
}
