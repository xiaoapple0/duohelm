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
import { TEXT } from "./text";

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
      ? Promise.resolve<Known<BarsOut>>({ known: false, reason: TEXT.reason.isCalendar })
      : known(dataApi.bars(CALENDAR_SYMBOL, { ...range, adjust: "none" })),
    known(dataApi.bars(p.symbol, { ...range, adjust: otherAdjust(p.adjust) })),
  ]);

  const sec: Security | undefined = security.known ? security.value.items[0] : undefined;

  let suspended: string[] = [];
  let suspension: Known<number>;
  let tailFrom: string | null = null;
  if (p.symbol === CALENDAR_SYMBOL) {
    // 没有参考日历，无法发现它自己缺的日子：未知，不是 0
    suspension = { known: false, reason: TEXT.reason.isCalendar };
  } else if (!security.known || sec === undefined) {
    suspension = {
      known: false,
      reason: security.known ? TEXT.reason.noSecurity : TEXT.reason.securityFailed(security.reason),
    };
  } else if (!calendar.known) {
    suspension = { known: false, reason: TEXT.reason.calendarFailed(CALENDAR_SYMBOL, calendar.reason) };
  } else if (calendar.value.empty) {
    suspension = { known: false, reason: TEXT.reason.calendarEmpty(CALENDAR_SYMBOL) };
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
      placeholder={TEXT.select.placeholder}
      filterOption={false}
      loading={searching}
      value={value}
      onSearch={(text) => {
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => search(text), 300);
      }}
      onChange={onChange}
      notFoundContent={failed ? TEXT.select.listUnknown(failed) : searching ? <Spin size="small" /> : TEXT.select.noMatch}
      options={options.map((s) => ({
        value: s.symbol,
        label: `${s.name ?? TEXT.select.noName}　${s.symbol}`,
      }))}
    />
  );
}

function KnownTag({ label, info, unit }: { label: string; info: Known<number>; unit: string }) {
  return info.known ? (
    <Tag>{TEXT.tag.known(label, info.value, unit)}</Tag>
  ) : (
    <Tag color="warning" title={info.reason}>{TEXT.tag.unknown(label)}</Tag>
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
      setFormError(TEXT.form.needSymbol);
      return;
    }
    if (!isIsoDate(start) || !isIsoDate(end)) {
      setFormError(TEXT.form.badDate);
      return;
    }
    if (start > end) {
      setFormError(TEXT.form.startAfterEnd);
      return;
    }
    setFormError(undefined);
    submit({ symbol, start, end, adjust });
  };

  return (
    <Card title={TEXT.title}>
      <Space wrap style={{ marginBottom: 16 }}>
        <SecuritySelect value={symbol} onChange={setSymbol} />
        <Input
          type="date"
          value={start}
          onChange={(e) => setStart(e.target.value)}
          aria-label={TEXT.form.startLabel}
          style={{ width: 150 }}
        />
        <span>{TEXT.form.to}</span>
        <Input
          type="date"
          value={end}
          onChange={(e) => setEnd(e.target.value)}
          aria-label={TEXT.form.endLabel}
          style={{ width: 150 }}
        />
        <Radio.Group
          value={adjust}
          onChange={(e) => setAdjust(e.target.value as AdjustMode)}
          optionType="button"
          options={(Object.keys(TEXT.adjust) as AdjustMode[]).map((m) => ({
            value: m,
            label: TEXT.adjust[m],
          }))}
        />
        <Button type="primary" onClick={onSubmit} loading={view.kind === "loading"}>
          {TEXT.submit}
        </Button>
      </Space>
      <div style={{ color: "#8c8c8c", fontSize: 12, marginBottom: 12 }}>
        {TEXT.timeNote}
      </div>

      {formError && <Alert type="warning" showIcon message={formError} style={{ marginBottom: 12 }} />}

      {view.kind === "idle" && <Alert type="info" showIcon message={TEXT.idle} />}
      {view.kind === "loading" && <Spin />}
      {view.kind === "unknown" && (
        <Alert
          type="error"
          showIcon
          message={TEXT.unknown.title}
          description={TEXT.unknown.description(view.message)}
        />
      )}
      {view.kind === "invalid" && (
        <Alert type="error" showIcon message={TEXT.invalid.title} description={view.message} />
      )}
      {view.kind === "empty" && (
        <Alert
          type="info"
          showIcon
          message={TEXT.empty.title}
          description={
            <>
              {TEXT.empty.reasons}
              {view.result.last_bar_date
                ? TEXT.empty.lastBar(view.result.last_bar_date)
                : TEXT.empty.noBar}
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
                  {TEXT.empty.jump}
                </Button>
              )}
            </>
          }
        />
      )}
      {view.kind === "ok" && (
        <>
          <Descriptions size="small" column={2} style={{ marginBottom: 8 }}>
            <Descriptions.Item label={TEXT.summary.symbol}>{view.result.symbol}</Descriptions.Item>
            <Descriptions.Item label={TEXT.summary.adjust}>
              {TEXT.adjust[view.result.adjust]}
              {view.result.base_date ? TEXT.summary.baseDate(view.result.base_date) : ""}
            </Descriptions.Item>
            <Descriptions.Item label={TEXT.summary.bars}>{TEXT.summary.barsUnit(view.result.bars.length)}</Descriptions.Item>
            <Descriptions.Item label={TEXT.summary.snapshot}>{view.result.snapshot.slice(0, 12)}</Descriptions.Item>
          </Descriptions>
          <Space wrap style={{ marginBottom: 8 }}>
            <KnownTag label={TEXT.tag.suspension} info={view.suspension} unit={TEXT.tag.suspensionUnit} />
            <KnownTag label={TEXT.tag.ex} info={view.ex} unit={TEXT.tag.exUnit} />
          </Space>
          {!view.suspension.known && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message={TEXT.warn.suspensionTitle}
              description={view.suspension.reason}
            />
          )}
          {view.tailFrom && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message={TEXT.warn.tailTitle(view.tailFrom)}
              description={TEXT.warn.tailDescription}
            />
          )}
          {!view.ex.known && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message={TEXT.warn.exTitle}
              description={view.ex.reason}
            />
          )}
          <KlineChart slots={view.slots} />
        </>
      )}
    </Card>
  );
}
