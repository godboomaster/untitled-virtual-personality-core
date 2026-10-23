import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Воркер модели векторов (src/offline/embedWorker.ts) грузит onnxruntime-web
  // по требованию — нужен модульный воркер
  worker: { format: 'es' },
  build: {
    // Шрифты скинов (web/skins/*/fonts) — всегда отдельными файлами: мелкие
    // иначе встраивались бы в общий CSS и грузились при любом запуске
    assetsInlineLimit: (file) => (file.endsWith('.woff2') ? false : undefined),
  },
})
