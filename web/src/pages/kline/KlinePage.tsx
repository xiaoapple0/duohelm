import { Alert, Button, Card, Descriptions, Input, Radio, Select, Space, Spin, Tag } from "antd";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../../api/client";
import { dataApi } from "../../api/data";
import type { AdjustMode, BarsOut, Security } from "../../api/data";
import {
  CALENDAR_SYMBOL,
  buildSlots,
  exDates,
  otherAdjust,
  gapDates,
} from "./analysis";
import type { Known, Slot } from "./analysis";
import { addDays, isIsoDate, shanghaiToday } from "./dates";
import KlineChart from "./KlineChart";

interface Params {
  symbol: string;
  start: string;
  end: string;
  adjust: AdjustMode;
}

type View =
  | { kind: "idle" }
  | { kind: "loading" }
  /** 没取到数据（404、网络错误、服务器错误）：结果是“未知”，不等于没有数据 */
  | { kind: "unknown"; message: string }
  /** 参数不合法（422） */
  | { kind: "invalid"; message: string }
  /** 200 且 empty：证券存在但区间内没有日线 */
  | { kind: "empty"; result: BarsOut }
  | {
      kind: "ok";
      result: BarsOut;
      slots: Slot[];
      suspension: Known<number>;
      /** 没有退市日的证券：这一天起（最后一根日线之后）是否停牌未知，没有标注 */
      tailFrom: string | null;
      ex: Known<number>;
    };

const ADJUST_LABELS: Record<AdjustMode, string> = {
  none: "不复权",
  hfq: "后复权",
  qfq: "前复权",
};

function errorView(err: unknown): View {
  if (err instanceof ApiError && err.status === 422) {
    return { kind: "invalid", message: err.message };
  }
  const message = err instanceof Error ? err.message : String(err);
  return { kind: "unknown", message };
}

async function known<T>(promise: Promise<T>): Promise<Known<T>> {
  try {
    return { known: true, value: await promise };
  } catch (err) {
    return { known: false, reason: err instanceof Error ? err.message : String(err) };
  }
}

async function load(p: Params): Promise<View> {
  const result = await dataApi.bars(p.symbol, { start: p.start, end: p.end, adjust: p.adjust });
  if (result.empty) {
    return { kind: "empty", result };
  }
  // 之后的请求固定读同一个快照，不会混合两个快照
  const snapshot = result.snapshot;
  const range = { snapshot, start: p.start, end: p.end };
  const [security, calendar, other] = await Promise.all([
    known(dataApi.securities({ snapshot, symbol: p.symbol })),
    p.symbol === CALENDAR_SYMBOL
      ? Promise.resolve<Known<BarsOut>>({ known: false, reason: "所选证券就是参考指数" })
      : known(dataApi.bars(CALENDAR_SYMBOL, { ...range, adjust: "none" })),
    known(dataApi.bars(p.symbol, { ...range, adjust: otherAdjust(p.adjust) })),
  ]);

  const sec: Security | undefined = security.known ? security.value.items[0] : undefined;

  let suspended: string[] = [];
  let suspension: Known<number>;
  let tailFrom: string | null = null;
  if (p.symbol === CALENDAR_SYMBOL) {
    suspension = { known: true, value: 0 };
  } else if (!security.known || sec === undefined) {
    suspension = {
      known: false,
      reason: security.known
        ? "证券列表里没有这只证券的信息，不知道上市日和退市日"
        : `证券信息读取失败：${security.reason}`,
    };
  } else if (!calendar.known) {
    suspension = { known: false, reason: `参考日历（${CALENDAR_SYMBOL}）读取失败：${calendar.reason}` };
  } else if (calendar.value.empty) {
    suspension = { known: false, reason: `参考日历（${CALENDAR_SYMBOL}）在区间内没有日线` };
  } else {
    const gaps = gapDates(
      result,
      sec,
      calendar.value.bars.map((b) => b.date),
    );
    suspended = gaps.dates;
    tailFrom = gaps.unknownFrom;
    suspension = { known: true, value: suspended.length };
  }

  let ex: string[] = [];
  let exInfo: Known<number>;
  if (!other.known) {
    exInfo = { known: false, reason: other.reason };
  } else {
    ex = exDates(result.bars, other.value.bars);
    exInfo = { known: true, value: ex.length };
  }

  return {
    kind: "ok",
    result,
    slots: buildSlots(result.bars, suspended, ex),
    suspension,
    tailFrom,
    ex: exInfo,
  };
}

function SecuritySelect({
  value,
  onChange,
}: {
  value: string | undefined;
  onChange: (symbol: string) => void;
}) {
  const [options, setOptions] = useState<Security[]>([]);
  const [searching, setSearching] = useState(false);
  const [failed, setFailed] = useState<string>();
  const seq = useRef(0);
  const timer = useRef<number | undefined>(undefined);

  const search = useCallback((text: string) => {
    const id = ++seq.current;
    const q = text.trim();
    setSearching(true);
    // 名称前缀和代码精确匹配各查一次，合并去重
    const requests = q
      ? [
          dataApi.securities({ name_prefix: q, limit: 50 }),
          dataApi.securities({ symbol: q.toUpperCase(), limit: 50 }),
        ]
      : [dataApi.securities({ limit: 50 })];
    Promise.all(requests)
      .then((pages) => {
        if (id !== seq.current) {
          return;
        }
        const seen = new Map<string, Security>();
        for (const page of pages) {
          for (const item of page.items) {
            seen.set(item.symbol, item);
          }
        }
        setOptions([...seen.values()]);
        setFailed(undefined);
      })
      .catch((err: unknown) => {
        if (id === seq.current) {
          setFailed(err instanceof Error ? err.message : String(err));
        }
      })
      .finally(() => {
        if (id === seq.current) {
          setSearching(false);
        }
      });
  }, []);

  useEffect(() => {
    search("");
    return () => window.clearTimeout(timer.current);
  }, [search]);

  return (
    <Select
      showSearch
      style={{ width: 260 }}
      placeholder="输入名称前缀或代码，如 600000.SH"
      filterOption={false}
      loading={searching}
      value={value}
      onSearch={(text) => {
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => search(text), 300);
      }}
      onChange={onChange}
      notFoundContent={failed ? `证券列表未知：${failed}` : searching ? <Spin size="small" /> : "没有匹配的证券"}
      options={options.map((s) => ({
        value: s.symbol,
        label: `${s.name ?? "（无名称）"}　${s.symbol}`,
      }))}
    />
  );
}

function KnownTag({ label, info, unit }: { label: string; info: Known<number>; unit: string }) {
  return info.known ? (
    <Tag>{`${label}：${info.value} ${unit}`}</Tag>
  ) : (
    <Tag color="warning" title={info.reason}>{`${label}：未知`}</Tag>
  );
}

export default function KlinePage() {
  const [today] = useState(() => shanghaiToday());
  const [symbol, setSymbol] = useState<string>();
  const [start, setStart] = useState(() => addDays(today, -365));
  const [end, setEnd] = useState(today);
  const [adjust, setAdjust] = useState<AdjustMode>("none");
  const [formError, setFormError] = useState<string>();
  const [view, setView] = useState<View>({ kind: "idle" });
  const run = useRef(0);

  const submit = useCallback((p: Params) => {
    const id = ++run.current;
    setView({ kind: "loading" });
    load(p)
      .then((v) => {
        if (id === run.current) {
          setView(v);
        }
      })
      .catch((err: unknown) => {
        if (id === run.current) {
          setView(errorView(err));
        }
      });
  }, []);

  const onSubmit = () => {
    if (!symbol) {
      setFormError("请先选择证券");
      return;
    }
    if (!isIsoDate(start) || !isIsoDate(end)) {
      setFormError("起止日期必须是有效的日期");
      return;
    }
    if (start > end) {
      setFormError("起始日期晚于结束日期");
      return;
    }
    setFormError(undefined);
    submit({ symbol, start, end, adjust });
  };

  return (
    <Card title="K 线查看">
      <Space wrap style={{ marginBottom: 16 }}>
        <SecuritySelect value={symbol} onChange={setSymbol} />
        <Input
          type="date"
          value={start}
          onChange={(e) => setStart(e.target.value)}
          aria-label="起始日期"
          style={{ width: 150 }}
        />
        <span>至</span>
        <Input
          type="date"
          value={end}
          onChange={(e) => setEnd(e.target.value)}
          aria-label="结束日期"
          style={{ width: 150 }}
        />
        <Radio.Group
          value={adjust}
          onChange={(e) => setAdjust(e.target.value as AdjustMode)}
          optionType="button"
          options={(Object.keys(ADJUST_LABELS) as AdjustMode[]).map((m) => ({
            value: m,
            label: ADJUST_LABELS[m],
          }))}
        />
        <Button type="primary" onClick={onSubmit} loading={view.kind === "loading"}>
          查看
        </Button>
      </Space>
      <div style={{ color: "#8c8c8c", fontSize: 12, marginBottom: 12 }}>
        日期是北京时间的交易日。前复权以所选区间的最后一根日线为基准。
      </div>

      {formError && <Alert type="warning" showIcon message={formError} style={{ marginBottom: 12 }} />}

      {view.kind === "idle" && <Alert type="info" showIcon message="选择证券后点“查看”。" />}
      {view.kind === "loading" && <Spin />}
      {view.kind === "unknown" && (
        <Alert
          type="error"
          showIcon
          message="未知：没有取到数据"
          description={`${view.message}。这不表示这段时间没有行情，只是没读到。`}
        />
      )}
      {view.kind === "invalid" && (
        <Alert type="error" showIcon message="请求参数不合法" description={view.message} />
      )}
      {view.kind === "empty" && (
        <Alert
          type="info"
          showIcon
          message="没有数据：这只证券在所选区间内没有日线"
          description={
            <>
              可能是区间在上市前、退市后，或整段停牌。
              {view.result.last_bar_date
                ? `这只证券在快照里的最后一根日线是 ${view.result.last_bar_date}。`
                : "这只证券在快照里没有任何日线。"}
              {view.result.last_bar_date && (
                <Button
                  type="link"
                  onClick={() => {
                    const last = view.result.last_bar_date;
                    if (last && symbol) {
                      const nextStart = addDays(last, -365);
                      setStart(nextStart);
                      setEnd(last);
                      submit({ symbol, start: nextStart, end: last, adjust });
                    }
                  }}
                >
                  跳到最后一根日线
                </Button>
              )}
            </>
          }
        />
      )}
      {view.kind === "ok" && (
        <>
          <Descriptions size="small" column={2} style={{ marginBottom: 8 }}>
            <Descriptions.Item label="证券">{view.result.symbol}</Descriptions.Item>
            <Descriptions.Item label="复权">
              {ADJUST_LABELS[view.result.adjust]}
              {view.result.base_date ? `（基准日 ${view.result.base_date}）` : ""}
            </Descriptions.Item>
            <Descriptions.Item label="日线">{view.result.bars.length} 根</Descriptions.Item>
            <Descriptions.Item label="快照">{view.result.snapshot.slice(0, 12)}</Descriptions.Item>
          </Descriptions>
          <Space wrap style={{ marginBottom: 8 }}>
            <KnownTag label="停牌或缺数据" info={view.suspension} unit="天" />
            <KnownTag label="除权" info={view.ex} unit="次" />
          </Space>
          {!view.suspension.known && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message="停牌或缺数据的日子未知，图上没有标注"
              description={view.suspension.reason}
            />
          )}
          {view.tailFrom && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message={`${view.tailFrom} 起是否停牌未知，图上没有标注`}
              description="这只证券没有退市日，最后一根日线之后到区间结束的日子，可能在停牌，也可能数据还没同步，分不出来。"
            />
          )}
          {!view.ex.known && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message="除权日未知，图上没有标注除权"
              description={view.ex.reason}
            />
          )}
          <KlineChart slots={view.slots} />
        </>
      )}
    </Card>
  );
}
