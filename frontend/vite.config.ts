import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 開発サーバー（npm run dev）はバックエンド（既定 8787）へ /api を渡す。本番は build した dist をバックエンドが配る。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": { target: "http://127.0.0.1:8787", changeOrigin: false } } },
  build: { chunkSizeWarningLimit: 600 },
});
