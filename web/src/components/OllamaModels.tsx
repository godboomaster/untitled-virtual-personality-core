import { useI18n } from '../i18n';

/* Сравнение локальных моделей Ollama в шаге «Провайдеры: Ollama» обучения:
   карточки по семействам — команда скачивания (копируется кликом), скорость и
   «ум» точками (ориентир по размеру модели), размер скачивания, контекст,
   что модель понимает, плюс и минус. Размеры — с ollama.com/library на
   октябрь 2026; mac — своя сборка для Apple Silicon (MLX), если она по размеру
   отличается (у Gemma 4 12B она того же размера, у Gemma 3 её нет). */

type Input = 'text' | 'image' | 'audio';

interface Model {
  tag: string;
  name: string;
  size: number; // ГБ, обычная сборка (GGUF): Windows, Linux — и Mac, если размер тот же
  mac?: number; // ГБ, сборка для Mac (MLX), если её размер другой
  ctx: string;
  inputs: Input[];
  speed: number; // 1..5
  smarts: number; // 1..5
  key: string; // суффикс ключей i18n плюса и минуса
  recommended?: boolean;
}

interface Family {
  titleKey: string;
  models: Model[];
}

const FAMILIES: Family[] = [
  {
    titleKey: 'guide.mGemma',
    models: [
      { tag: 'gemma4:e2b', name: 'Gemma 4 · E2B', size: 4.6, mac: 7.5, ctx: '128K', inputs: ['text', 'image', 'audio'], speed: 5, smarts: 2, key: 'gemmaE2b', recommended: true },
      { tag: 'gemma4:e4b', name: 'Gemma 4 · E4B', size: 6.6, mac: 9.5, ctx: '128K', inputs: ['text', 'image', 'audio'], speed: 4, smarts: 3, key: 'gemmaE4b' },
      { tag: 'gemma4:12b', name: 'Gemma 4 · 12B', size: 7.7, ctx: '256K', inputs: ['text', 'image'], speed: 2, smarts: 5, key: 'gemma12b' },
    ],
  },
  {
    titleKey: 'guide.mGemma3',
    models: [
      { tag: 'gemma3:1b', name: 'Gemma 3 · 1B', size: 0.8, ctx: '32K', inputs: ['text'], speed: 5, smarts: 1, key: 'gemma3_1b' },
      { tag: 'gemma3:4b', name: 'Gemma 3 · 4B', size: 3.3, ctx: '128K', inputs: ['text', 'image'], speed: 4, smarts: 2, key: 'gemma3_4b' },
      { tag: 'gemma3:12b', name: 'Gemma 3 · 12B', size: 8.1, ctx: '128K', inputs: ['text', 'image'], speed: 2, smarts: 4, key: 'gemma3_12b' },
    ],
  },
  {
    titleKey: 'guide.mQwen',
    models: [
      { tag: 'qwen3.5:2b', name: 'Qwen 3.5 · 2B', size: 2.7, mac: 3.1, ctx: '256K', inputs: ['text', 'image'], speed: 5, smarts: 2, key: 'qwen2b' },
      { tag: 'qwen3.5:4b', name: 'Qwen 3.5 · 4B', size: 3.4, mac: 4.0, ctx: '256K', inputs: ['text', 'image'], speed: 4, smarts: 3, key: 'qwen4b' },
      { tag: 'qwen3.5:9b', name: 'Qwen 3.5 · 9B', size: 6.6, mac: 8.9, ctx: '256K', inputs: ['text', 'image'], speed: 3, smarts: 4, key: 'qwen9b' },
    ],
  },
];

function Dots({ value, label }: { value: number; label: string }) {
  return (
    <span className="ollama-model-dots" role="img" aria-label={`${label}: ${value} / 5`}>
      {[1, 2, 3, 4, 5].map((i) => (
        <i key={i} className={i <= value ? 'is-on' : ''} />
      ))}
    </span>
  );
}

interface Props {
  uid: string; // префикс id для отметки «скопировано»
  copied: string | null;
  onCopy: (uid: string, text: string) => void;
}

export default function OllamaModels({ uid, copied, onCopy }: Props) {
  const { t, lang } = useI18n();
  const gb = (n: number) => t('guide.mGb', { n: n.toLocaleString(lang === 'en' ? 'en-US' : 'ru-RU') });

  return (
    <div className="ollama-models">
      {FAMILIES.map((fam) => (
        <section key={fam.titleKey} className="ollama-models-family">
          <h5 className="ollama-models-head">{t(fam.titleKey)}</h5>
          <div className="ollama-models-grid">
            {fam.models.map((m) => {
              const chipUid = `${uid}#${m.tag}`;
              const cmd = `ollama pull ${m.tag}`;
              return (
                <article key={m.tag} className={`ollama-model${m.recommended ? ' is-recommended' : ''}`}>
                  <div className="ollama-model-top">
                    <span className="ollama-model-name">{m.name}</span>
                    {m.recommended && <span className="badge badge--active">{t('guide.mRecommended')}</span>}
                  </div>

                  <button
                    type="button"
                    className={`start-guide-code ollama-model-cmd${copied === chipUid ? ' is-copied' : ''}`}
                    title={t('guide.copy')}
                    onClick={() => onCopy(chipUid, cmd)}
                  >
                    <code>{cmd}</code>
                    {copied === chipUid && <span className="start-guide-code-flash">{t('guide.copied')}</span>}
                  </button>

                  <dl className="ollama-model-meters">
                    <dt>{t('guide.mSpeed')}</dt>
                    <dd><Dots value={m.speed} label={t('guide.mSpeed')} /></dd>
                    <dt>{t('guide.mSmarts')}</dt>
                    <dd><Dots value={m.smarts} label={t('guide.mSmarts')} /></dd>
                  </dl>

                  {/* Размер скачивания по платформам (своя сборка для Mac — отдельной
                      строкой, иначе один на все) и контекст: подпись — значение */}
                  <dl className="ollama-model-specs">
                    {m.mac ? (
                      <>
                        <dt>{t('guide.mPc')}</dt>
                        <dd>{gb(m.size)}</dd>
                        <dt>{t('guide.mMac')}</dt>
                        <dd>{gb(m.mac)}</dd>
                      </>
                    ) : (
                      <>
                        <dt>{t('guide.mAll')}</dt>
                        <dd>{gb(m.size)}</dd>
                      </>
                    )}
                    <dt>{t('guide.mCtx')}</dt>
                    <dd>{m.ctx}</dd>
                  </dl>
                  <div className="ollama-model-inputs">
                    {m.inputs.map((inp) => (
                      <span key={inp}>{t(`guide.mIn.${inp}`)}</span>
                    ))}
                  </div>

                  <p className="ollama-model-pro">{t(`guide.mPro.${m.key}`)}</p>
                  <p className="ollama-model-con">{t(`guide.mCon.${m.key}`)}</p>
                </article>
              );
            })}
          </div>
        </section>
      ))}
      <p className="ollama-models-legend">{t('guide.mLegend')}</p>
    </div>
  );
}
