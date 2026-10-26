import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { lang } from '../i18n';
import PortraitOverlay from './PortraitOverlay';
import './portrait.css';

document.documentElement.lang = lang;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <PortraitOverlay />
  </StrictMode>,
);
