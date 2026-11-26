import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发时把 /api 代理到本机后端（duohelm serve 默认端口 8765）。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    // 端口固定：后端只接受本机这个端口（DUOHELM_WEB_PORT，默认 5173）的 Host 和 Origin，
    // 端口被占用时报错，不自动换成别的端口
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
});
