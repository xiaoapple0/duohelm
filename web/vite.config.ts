import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// 开发时把 /api 代理到本机后端（duohelm serve 默认端口 8765）。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
});
