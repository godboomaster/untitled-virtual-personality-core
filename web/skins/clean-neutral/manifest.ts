import type { SkinManifest } from '../types';

const manifest: SkinManifest = {
  id: 'clean-neutral',
  order: 1,
  name: { ru: 'Чистый нейтральный', en: 'Clean Neutral' },
  tagline: {
    ru: 'Тёплая бумага и шалфей, ночью — графитовые сумерки',
    en: 'Warm paper and sage; graphite dusk at night',
  },
  schemes: ['light', 'dark'],
};

export default manifest;
