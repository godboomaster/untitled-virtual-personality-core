import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

// Бандл команд телефону для голосовой службы (WakeWordService.java): тот же
// src/phone/actions.ts в JS-движке без WebView. Плагин и ../api подменены
// (src/phone/voice/), результат — assets/phone-voice.js, глобальный vpcPhone.
// Собирается в npm run apk; в git не попадает
const here = (p: string) => fileURLToPath(new URL(p, import.meta.url));

export default defineConfig({
  resolve: {
    alias: [
      { find: '@capacitor/core', replacement: here('./src/phone/voice/capacitorShim.ts') },
      { find: /^\.\.\/api$/, replacement: here('./src/phone/voice/apiShim.ts') },
    ],
  },
  build: {
    outDir: here('./android/app/src/main/assets'),
    // В assets лежат и веб (public/), и модель голосовой активации — не чистить
    emptyOutDir: false,
    copyPublicDir: false,
    minify: false,
    target: 'es2020',
    lib: {
      entry: here('./src/phone/voice/entry.ts'),
      name: 'vpcPhone',
      formats: ['iife'],
      fileName: () => 'phone-voice.js',
    },
  },
});
