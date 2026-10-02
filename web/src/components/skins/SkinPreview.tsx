/* Предпросмотр файлов скина на моковых данных персоны: вкладки экранов +
   SkinFrame. Снапшоты — те же билдеры, что у боевых экранов, с окружением
   приложения (тема, язык, подписи — usePreviewStates). Перекраска
   (colors/hueShift) применяется с задержкой: iframe перезагружается на
   каждое изменение, а пикер цвета шлёт их десятками в секунду. */

import { useEffect, useMemo, useState } from 'react';
import type { SkinScreen } from '../../skins/engine';
import type { SkinStatePayload } from '../../skins/payloads';
import { applyColorOverrides } from '../../skins/recolor';
import { SKIN_SCREENS } from '../../skins/skinStore';
import type { PersonaSkins } from '../../skins/skinStore';
import type { Persona } from '../../mockData';
import { useI18n } from '../../i18n';
import SkinFrame from '../SkinFrame';
import { SCREEN_NAME_KEY } from './skinUi';
import { usePreviewStates } from './usePreviewStates';

const RECOLOR_DELAY_MS = 200;

// Значение, догоняющее исходное с задержкой
function useDebounced<T>(value: T, delay: number): T {
  const [out, setOut] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setOut(value), delay);
    return () => clearTimeout(id);
  }, [value, delay]);
  return out;
}

interface SkinPreviewProps {
  files: PersonaSkins;
  persona: Persona;
  colors?: Record<string, string>;
  hueShift?: number;
  // Готовые снапшоты (родитель уже держит usePreviewStates) — без второго хука
  states?: Record<SkinScreen, SkinStatePayload>;
}

export default function SkinPreview({ files, persona, colors, hueShift = 0, states }: SkinPreviewProps) {
  if (states) return <PreviewBody files={files} colors={colors} hueShift={hueShift} states={states} />;
  return <OwnStatesPreview files={files} persona={persona} colors={colors} hueShift={hueShift} />;
}

function OwnStatesPreview({ persona, ...rest }: Omit<SkinPreviewProps, 'states'>) {
  const states = usePreviewStates(persona);
  return <PreviewBody {...rest} states={states} />;
}

function PreviewBody({
  files,
  colors,
  hueShift = 0,
  states,
}: Omit<SkinPreviewProps, 'persona' | 'states'> & { states: Record<SkinScreen, SkinStatePayload> }) {
  const { t } = useI18n();
  const available = SKIN_SCREENS.filter((s) => files[s]);
  const [picked, setPicked] = useState<SkinScreen>(available[0] ?? 'chat');
  const screen = available.includes(picked) ? picked : (available[0] ?? 'chat');
  const state = states[screen];

  const recolor = useDebounced(useMemo(() => ({ colors: colors ?? {}, hueShift }), [colors, hueShift]), RECOLOR_DELAY_MS);
  const html = files[screen];
  const skin = useMemo(
    () => (html ? applyColorOverrides(html, recolor.colors, recolor.hueShift) : null),
    [html, recolor],
  );

  if (!skin) return null;
  return (
    <div className="skin-preview">
      {available.length > 1 && (
        <div className="tabs">
          {available.map((s) => (
            <button
              key={s}
              type="button"
              className={`tab ${screen === s ? 'tab--active' : ''}`}
              onClick={() => setPicked(s)}
            >
              {t(SCREEN_NAME_KEY[s])}
            </button>
          ))}
        </div>
      )}
      <SkinFrame skin={skin} screen={screen} state={state} className="skin-frame skin-frame--preview" title="skin preview" />
    </div>
  );
}
