import type { SkinManifest } from '../types';

const manifest: SkinManifest = {
  id: 'pixel',
  order: 8,
  name: { ru: 'Пиксель-арт', en: 'Pixel Art' },
  tagline: {
    ru: '16-битная RPG: окна диалогов, слоты инвентаря и полоски HP',
    en: '16-bit RPG: dialogue windows, inventory slots and HP bars',
  },
  schemes: ['light', 'dark'],
};

export default manifest;
