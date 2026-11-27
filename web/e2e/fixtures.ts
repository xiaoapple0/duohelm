// 冒烟测试用的固定接口数据，形状对应 src/api 里的类型。日期都是北京时间的交易日。
import type { Page, Route } from "@playwright/test";

export const SNAPSHOT = "a1b2c3d4e5f60718293a4b5c6d7e8f90";

const SECURITY = {
  symbol: "600000.SH",
  name: "浦发银行",
  exchange: "SH",
  kind: "stock",
  list_date: "1999-11-10",
  delist_date: null,
};

interface BarData {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  amount: number | null;
}

const bar = (date: string, close: number | null, volume: number | null): BarData => ({
  date,
  open: close,
  high: close,
  low: close,
  close,
  volume,
  amount: null,
});

// 参考指数 000001.SH 在 2024-01-02 至 01-09 每个交易日都有日线（6 天）。
const CALENDAR_BARS = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"].map(
  (d) => bar(d, 2900, 3e8),
);

// 浦发银行缺 2024-01-04（停牌），2024-01-08 的成交量缺失（null，不是 0）。
export const STOCK_BARS: BarData[] = [
  bar("2024-01-02", 6.5, 1.2e7),
  bar("2024-01-03", 6.6, 1.1e7),
  bar("2024-01-05", 6.55, 9e6),
  bar("2024-01-08", 6.6, null),
  bar("2024-01-09", 6.7, 1.3e7),
];

const barsOut = (symbol: string, adjust: string, list: BarData[]) => ({
  snapshot: SNAPSHOT,
  symbol,
  adjust,
  base_date: adjust === "qfq" ? "2024-01-09" : null,
  last_bar_date: list.at(-1)?.date ?? null,
  empty: list.length === 0,
  bars: list,
});

const coverageRow = (symbol: string, name: string, exchange: string, delist: string | null) => ({
  symbol,
  name,
  kind: "stock",
  exchange,
  delist_date: delist,
  first_date: "2024-01-02",
  last_date: "2024-01-09",
  bar_count: 5,
  expected_days: 6,
  missing_days: 1,
  off_calendar_bars: 0,
});

const COVERAGE_ITEMS = [
  coverageRow("600000.SH", "浦发银行", "SH", null),
  coverageRow("000002.SZ", "万科A", "SZ", null),
  coverageRow("600001.SH", "邯郸钢铁", "SH", "2010-01-08"),
];

const group = (exchange: string, status: "listed" | "delisted", securities: number) => ({
  exchange,
  kind: "stock",
  status,
  securities,
  with_bars: securities,
  no_bars: 0,
  bar_count: securities * 5,
  with_missing: securities,
  missing_days: securities,
  off_calendar_bars: 0,
});

const task = (id: number, status: string, error: string | null) => ({
  id,
  type: "data.sync",
  params: {},
  status,
  progress: null,
  progress_message: null,
  attempt: 1,
  max_attempts: 1,
  run_after: "2024-01-09T10:00:00+08:00",
  cancel_requested: false,
  error,
  result: null,
  dedupe_key: null,
  created_at: "2024-01-09T10:00:00+08:00",
  started_at: "2024-01-09T10:00:01+08:00",
  finished_at: "2024-01-09T10:00:30+08:00",
});

const TASKS = [task(7, "succeeded", null), task(8, "failed", "数据源连接失败")];

const PLUGINS = [
  {
    id: "xtdata",
    status: "ready",
    name: "xtdata 行情源",
    version: "0.1.0",
    kind: "datasource",
    path: "plugins/xtdata",
    problems: [],
    network: null,
    endpoints: [],
  },
  {
    id: "broken",
    status: "invalid",
    name: null,
    version: null,
    kind: null,
    path: "plugins/broken",
    problems: ["清单缺少 id 字段"],
    network: null,
    endpoints: [],
  },
];

const json = (route: Route, body: unknown) => route.fulfill({ json: body });

/**
 * 拦截 /api/**，返回固定数据；不启动后端。
 * 没有预期的请求（没列出的路径）返回 404 并记下来，测试结束时断言为空；
 * 访问 127.0.0.1 以外的地址一律拦下并记下来，测试不访问外网。
 */
export async function mockApi(page: Page): Promise<{ unexpected: string[] }> {
  const unexpected: string[] = [];

  await page.route(
    (url) => url.hostname !== "127.0.0.1",
    (route) => {
      unexpected.push(`外部请求：${route.request().url()}`);
      return route.abort();
    },
  );

  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    if (path === "/api/data/snapshots") {
      return json(route, {
        current: SNAPSHOT,
        snapshots: [
          {
            id: SNAPSHOT,
            parent: null,
            created_at: "2024-01-09T08:00:00+00:00",
            task_id: 7,
            note: null,
            stats: {},
            is_current: true,
          },
        ],
      });
    }
    if (path === "/api/data/securities") {
      return json(route, { snapshot: SNAPSHOT, total: 1, items: [SECURITY] });
    }
    const barsMatch = /^\/api\/data\/securities\/([^/]+)\/bars$/.exec(path);
    if (barsMatch) {
      const symbol = decodeURIComponent(barsMatch[1] ?? "");
      const adjust = url.searchParams.get("adjust") ?? "none";
      if (symbol === "000001.SH") {
        return json(route, barsOut(symbol, adjust, CALENDAR_BARS));
      }
      if (symbol === "600000.SH") {
        return json(route, barsOut(symbol, adjust, STOCK_BARS));
      }
    }
    if (path === "/api/data/coverage/summary") {
      return json(route, {
        snapshot: SNAPSHOT,
        as_of: "2024-01-09",
        total: 3,
        groups: [group("SH", "listed", 1), group("SZ", "listed", 1), group("SH", "delisted", 1)],
      });
    }
    if (path === "/api/data/coverage") {
      return json(route, { snapshot: SNAPSHOT, total: COVERAGE_ITEMS.length, items: COVERAGE_ITEMS });
    }
    if (path === "/api/tasks") {
      return json(route, TASKS);
    }
    if (path === "/api/task-types") {
      return json(route, [{ name: "data.sync", description: "同步行情", max_attempts: 1, params_schema: {} }]);
    }
    if (path === "/api/plugins") {
      return json(route, PLUGINS);
    }

    unexpected.push(`没有预期的接口请求：${route.request().method()} ${path}${url.search}`);
    return route.fulfill({ status: 404, json: { detail: "测试里没有预期的请求" } });
  });

  return { unexpected };
}
