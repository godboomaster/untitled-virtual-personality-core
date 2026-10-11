import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Воркер модели векторов (src/offline/embedWorker.ts) грузит onnxruntime-web
  // по требованию — нужен модульный воркер
  worker: { format: 'es' },
})
