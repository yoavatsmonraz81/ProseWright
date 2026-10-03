import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The bundle is served by the Python server at /app, and committed to the repo
// so running the editor never needs node — only rebuilding it does.
export default defineConfig({
  plugins: [react()],
  base: '/app/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
sourcemap: false,
  },
  server: {
    port: 5273,
    // `npm run dev` talks to the real engine: every path the app calls is
    // proxied, so the dev server never needs its own copy of anything.
    proxy: Object.fromEntries(
      [
        '/health', '/status', '/layers', '/spine', '/scenes', '/log',
        '/manuscript', '/scene', '/policy', '/fonts', '/structure',
        '/attribution', '/interlude', '/edits', '/history', '/sync',
        '/proofread', '/canon', '/sweep', '/export',
      ].map((p) => [p, { target: 'http://127.0.0.1:8765', changeOrigin: true }]),
    ),
  },
})
