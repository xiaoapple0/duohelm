// 4 个页面的冒烟测试：接口全部由 page.route 拦截，不启动后端，不访问外网。
// 断言：关键元素出现、没有预期之外的请求、控制台没有 error。
import { expect, test } from "@playwright/test";
import type { Page } from "@playwright/test";

import { mockApi } from "./fixtures";

/**
 * 控制台里已知、与本测试无关的 error：antd 5 与 React 19 的兼容提示
 * （"[antd: compatible] antd v5 support React is 16 ~ 18"），main 上已有，只在开发构建里出现。
 */
const KNOWN_CONSOLE_ERRORS = [/\[antd: compatible\]/];

interface Watch {
  errors: string[];
  unexpected: string[];
}

async function watch(page: Page): Promise<Watch> {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error" && !KNOWN_CONSOLE_ERRORS.some((re) => re.test(msg.text()))) {
      errors.push(msg.text());
    }
  });
  page.on("pageerror", (err) => errors.push(`未捕获的异常：${err.message}`));
  const { unexpected } = await mockApi(page);
  return { errors, unexpected };
}

function expectClean(w: Watch) {
  expect(w.errors, "控制台 error").toEqual([]);
  expect(w.unexpected, "没有预期的请求").toEqual([]);
}

test("K 线查看：选证券后画出 K 线，停牌日有标注", async ({ page }) => {
  const w = await watch(page);
  await page.goto("/kline");
  await expect(page.getByText("K 线查看", { exact: true }).first()).toBeVisible();

  await page.getByRole("combobox").click();
  await page.getByText("浦发银行　600000.SH").click();
  await page.getByRole("button", { name: /查\s*看/ }).click();

  // 5 根日线；6 个交易日里缺 1 天（2024-01-04），标成“停牌或缺数据”
  await expect(page.getByText("5 根")).toBeVisible();
  await expect(page.getByText("停牌或缺数据：1 天")).toBeVisible();
  await expect(page.getByRole("img", { name: "K 线和成交量" })).toBeVisible();
  expectClean(w);
});

test("覆盖情况：汇总和表格", async ({ page }) => {
  const w = await watch(page);
  await page.goto("/coverage");
  await expect(page.getByRole("heading", { name: "数据覆盖情况" })).toBeVisible();
  await expect(page.getByText("按交易所")).toBeVisible();
  await expect(page.getByText("按在市/退市")).toBeVisible();
  await expect(page.getByRole("cell", { name: "浦发银行" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "已退市（2010-01-08）" })).toBeVisible();
  expectClean(w);
});

test("任务中心：任务列表", async ({ page }) => {
  const w = await watch(page);
  await page.goto("/tasks");
  await expect(page.getByText("任务中心", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "data.sync" })).toHaveCount(2);
  await expect(page.getByText("还没有任务")).toHaveCount(0);
  expectClean(w);
});

test("插件：可用和清单无效的插件都显示出来", async ({ page }) => {
  const w = await watch(page);
  await page.goto("/plugins");
  await expect(page.getByRole("cell", { name: "xtdata 行情源" })).toBeVisible();
  await expect(page.getByText("可用", { exact: true })).toBeVisible();
  await expect(page.getByText("清单无效")).toBeVisible();
  await expect(page.getByText("清单缺少 id 字段")).toBeVisible();
  expectClean(w);
});
