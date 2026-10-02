import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { I18nProvider } from './i18n'
import BootGate from './components/BootGate'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <I18nProvider>
      <BootGate>
        <App />
      </BootGate>
    </I18nProvider>
  </StrictMode>,
)
