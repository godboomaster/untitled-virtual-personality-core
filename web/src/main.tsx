import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './mobile.css'
import App from './App.tsx'
import { I18nProvider } from './i18n'
import BootGate from './components/BootGate'
import { isNativeApp } from './api'
import { handleBack } from './backStack'

// Приложение на Android: «Назад» закрывает открытое (backStack), а с
// пустым стеком сворачивает приложение — не выходит из него, чтобы
// переписка и опрос фоновых сообщений пережили случайное нажатие
if (isNativeApp()) {
  void import('@capacitor/app').then(({ App: CapApp }) => {
    void CapApp.addListener('backButton', () => {
      if (!handleBack()) void CapApp.minimizeApp()
    })
  })
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <I18nProvider>
      <BootGate>
        <App />
      </BootGate>
    </I18nProvider>
  </StrictMode>,
)
