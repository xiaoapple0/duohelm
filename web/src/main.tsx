import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";

const root = document.getElementById("root");
if (!root) {
  throw new Error("找不到 #root 元素");
}

createRoot(root).render(
  <StrictMode>
    <ConfigProvider locale={zhCN}>
      {/* 提供 App.useApp()，让 message 等提示能读到主题和语言配置 */}
      <AntApp>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AntApp>
    </ConfigProvider>
  </StrictMode>,
);
