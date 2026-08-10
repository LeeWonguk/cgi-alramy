import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// 빌드 결과는 Flask가 그대로 서빙한다 (web/app.py의 STATIC_DIR).
// 개발 중에는 vite dev(5173)가 /api를 Flask(8787)로 넘긴다.
export default defineConfig({
  plugins: [svelte()],
  build: {
    outDir: '../web/static',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8787',
    },
  },
})
