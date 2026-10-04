import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Окно панели Tauri: свой порт (5173 — у веб-интерфейса web/)
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1430,
    strictPort: true,
    watch: { ignored: ['**/src-tauri/**'] },
  },
})
