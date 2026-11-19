import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Окно панели Tauri: свой порт (5173 — у веб-интерфейса web/). Две страницы:
// панель (index.html) и портрет на рабочем столе (portrait.html) — его движок
// берётся из веба (web/src/portrait/LivePortrait.tsx, голова фигуры — из
// web/src/room, рисовка скина — из web/skins), React — свой: одна копия
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  resolve: { dedupe: ['react', 'react-dom'] },
  server: {
    port: 1430,
    strictPort: true,
    watch: { ignored: ['**/src-tauri/**'] },
    // web/skins — рисовка головы скина (avatars.ts и <id>/avatar.tsx)
    fs: { allow: ['.', '../web/src/portrait', '../web/src/room', '../web/skins', '../../vpc-robot/desktop'] },
  },
  build: {
    rolldownOptions: { input: { main: 'index.html', portrait: 'portrait.html' } },
  },
})
