import { defineConfig, devices } from "@playwright/test";

// 冒烟测试：接口由测试里的 page.route 拦截，不需要后端。
// 端口避开开发服务器（5173）、后端（8765）和常见的 5174。
const PORT = 4183;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // 测的是生产构建，和用户实际拿到的一致
    command: `npm run build && npx vite preview --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: false,
    timeout: 180_000,
  },
});
