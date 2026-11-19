import type { ReactNode } from 'react';
import { getSkinParts } from '../../skins';
import { HeadLookContext } from '../portrait/headLook';
import { headLookFor } from '../skinHeadLook';
import { SkinPartsContext } from '../skinParts';
import { useAppliedLook } from '../skinTheme';

/* Свой облик применённого скина для компонентов: рисовка головы персоны
   (PersonaAvatar, голосовой режим, фигура в комнате) и свои части интерфейса
   (окно задачи). Скин встаёт на <html> только после загрузки своих файлов,
   поэтому к этому моменту всё уже есть. */
export default function SkinLookProvider({ children }: { children: ReactNode }) {
  const { skinId, scheme } = useAppliedLook();
  return (
    <HeadLookContext.Provider value={headLookFor(skinId, scheme)}>
      <SkinPartsContext.Provider value={getSkinParts(skinId) ?? null}>{children}</SkinPartsContext.Provider>
    </HeadLookContext.Provider>
  );
}
