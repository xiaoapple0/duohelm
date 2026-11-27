import { useEffect, useMemo, useRef } from "react";
import { BarChart, CandlestickChart, ScatterChart } from "echarts/charts";
import {
  GridComponent,
  MarkLineComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

import { extent, isComplete } from "./analysis";
import type { Slot } from "./analysis";
import { TEXT as T } from "./text";

echarts.use([
  BarChart,
  CandlestickChart,
  ScatterChart,
  GridComponent,
  MarkLineComponent,
  TooltipComponent,
  CanvasRenderer,
]);

const UP = "#cf1322"; // A 股习惯：红涨绿跌
const DOWN = "#389e0d";
const FLAT = "#8c8c8c";
const GRID = "#f0f0f0";
const TEXT = "#595959";
const SUSPEND = "rgba(140,140,140,0.18)";
const EX = "#d46b08";
const MISSING = "#722ed1";

function fmtPrice(v: number | null): string {
  return v === null ? T.chart.missing : v.toFixed(2);
}

function fmtVolume(v: number | null): string {
  if (v === null) {
    return T.chart.missing;
  }
  if (v >= 1e8) {
    return `${(v / 1e8).toFixed(2)} ${T.chart.volumeBillion}`;
  }
  if (v >= 1e4) {
    return `${(v / 1e4).toFixed(2)} ${T.chart.volumeTenThousand}`;
  }
  return `${v} ${T.chart.volumeShares}`;
}

function fmtAxisVolume(v: number): string {
  if (v === 0) {
    return "0";
  }
  if (v >= 1e8) {
    return `${+(v / 1e8).toFixed(2)}${T.chart.axisBillion}`;
  }
  if (v >= 1e4) {
    return `${+(v / 1e4).toFixed(2)}${T.chart.axisTenThousand}`;
  }
  return String(v);
}

function describe(slot: Slot): string[] {
  const lines = [slot.date];
  const bar = slot.bar;
  if (bar !== null) {
    lines.push(
      `${T.chart.open} ${fmtPrice(bar.open)}　${T.chart.high} ${fmtPrice(bar.high)}`,
      `${T.chart.low} ${fmtPrice(bar.low)}　${T.chart.close} ${fmtPrice(bar.close)}`,
      `${T.chart.volume} ${fmtVolume(bar.volume)}`,
    );
  }
  if (slot.suspended) {
    lines.push(T.chart.suspendedTip);
  }
  if (slot.exDate) {
    lines.push(T.chart.exTip);
  }
  return lines;
}

/**
 * 停牌或缺数据的背景：在一个 grid 里画只在这些日子有值的整格宽度的柱，连续的日子连成一片。
 * 值取该坐标轴已有范围的上限（价格区用价格上限，成交量区用成交量最大值），不改变坐标范围；
 * 没有上限（整段没有数据）时不画。不参与 tooltip。
 */
function suspendBackground(slots: Slot[], grid: 0 | 1, top: number | undefined) {
  if (top === undefined || !slots.some((s) => s.suspended)) {
    return [];
  }
  return [
    {
      type: "bar" as const,
      xAxisIndex: grid,
      yAxisIndex: grid,
      silent: true,
      z: 1,
      barWidth: "100%",
      barGap: "-100%",
      barCategoryGap: "0%",
      tooltip: { show: false },
      itemStyle: { color: SUSPEND },
      data: slots.map((s) => (s.suspended ? top : "-")),
    },
  ];
}

/**
 * 缺失（null）一律不画：蜡烛和成交量柱缺值时用 "-"，不用 0 补。
 * 日期按接口返回的字符串显示，横轴是类目轴，不做时区换算。
 */
function buildOption(slots: Slot[]): EChartsCoreOption {
  const dates = slots.map((s) => s.date);
  const bars = slots.flatMap((s) => (s.bar ? [s.bar] : []));
  const prices = extent(bars.flatMap((b) => [b.open, b.high, b.low, b.close]));
  const pad = prices ? Math.max((prices.max - prices.min) * 0.05, prices.max * 1e-3) : 0;
  const priceMin = prices ? prices.min - pad : undefined;
  const priceMax = prices ? prices.max + pad : undefined;
  const volMax = extent(bars.map((b) => b.volume))?.max;
  const colorOf = (s: Slot): string => {
    const b = s.bar;
    if (b === null || !isComplete(b)) {
      return FLAT;
    }
    return b.close > b.open ? UP : b.close < b.open ? DOWN : FLAT;
  };

  const axisLabel = { color: TEXT, fontSize: 11 };
  const xAxis = (gridIndex: number, show: boolean) => ({
    type: "category" as const,
    gridIndex,
    data: dates,
    boundaryGap: true,
    axisLine: { lineStyle: { color: "#d9d9d9" } },
    axisTick: { show: false },
    axisLabel: { ...axisLabel, show },
    splitLine: { show: false },
  });

  return {
    animation: false,
    textStyle: { color: TEXT },
    grid: [
      { left: 64, right: 12, top: 28, height: 330 },
      { left: 64, right: 12, top: 384, height: 106 },
    ],
    xAxis: [xAxis(0, false), xAxis(1, true)],
    yAxis: [
      {
        type: "value",
        gridIndex: 0,
        scale: true,
        min: priceMin,
        max: priceMax,
        name: T.chart.priceAxis,
        nameTextStyle: { color: TEXT, align: "left", fontSize: 11 },
        axisLabel: { ...axisLabel, formatter: (v: number) => String(+v.toFixed(2)) },
        splitLine: { lineStyle: { color: GRID } },
      },
      {
        type: "value",
        gridIndex: 1,
        name: T.chart.volumeAxis,
        nameTextStyle: { color: TEXT, align: "left", fontSize: 11 },
        splitNumber: 3,
        axisLabel: { ...axisLabel, formatter: (v: number) => fmtAxisVolume(v) },
        splitLine: { lineStyle: { color: GRID } },
      },
    ],
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", link: [{ xAxisIndex: "all" }] },
      formatter: (params: unknown) => {
        const list = Array.isArray(params) ? (params as { dataIndex: number }[]) : [];
        const slot = list[0] ? slots[list[0].dataIndex] : undefined;
        return slot ? describe(slot).join("<br/>") : "";
      },
    },
    series: [
      ...suspendBackground(slots, 0, priceMax),
      ...suspendBackground(slots, 1, volMax),
      {
        name: "K",
        type: "candlestick",
        xAxisIndex: 0,
        yAxisIndex: 0,
        // [open, close, low, high]；价格缺任何一项就不画蜡烛
        data: slots.map((s) =>
          s.bar && isComplete(s.bar) ? [s.bar.open, s.bar.close, s.bar.low, s.bar.high] : "-",
        ),
        itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: EX, type: "dashed" },
          label: { formatter: T.chart.exMark, color: EX, position: "insideEndTop" },
          data: slots.filter((s) => s.exDate).map((s) => ({ xAxis: s.date })),
        },
      },
      {
        // 价格缺失的日子：在价格区底部标出，不补值
        name: T.chart.legendMissing,
        type: "scatter",
        xAxisIndex: 0,
        yAxisIndex: 0,
        symbol: "rect",
        symbolSize: [6, 6],
        itemStyle: { color: MISSING },
        tooltip: { show: false },
        data:
          priceMin === undefined
            ? []
            : slots.flatMap((s) => (s.bar && !isComplete(s.bar) ? [[s.date, priceMin]] : [])),
      },
      {
        name: T.chart.volume,
        type: "bar",
        xAxisIndex: 1,
        yAxisIndex: 1,
        barWidth: "70%",
        barGap: "-100%",
        data: slots.map((s) =>
          s.bar && s.bar.volume !== null
            ? { value: s.bar.volume, itemStyle: { color: colorOf(s), opacity: 0.7 } }
            : "-",
        ),
      },
    ],
  };
}

export default function KlineChart({ slots }: { slots: Slot[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const chart = useRef<echarts.ECharts | null>(null);
  const option = useMemo(() => buildOption(slots), [slots]);

  // 项目目前没有暗色模式，不随主题切换；加入暗色模式时在这里按主题 init
  useEffect(() => {
    const el = ref.current;
    if (!el) {
      return;
    }
    const instance = echarts.init(el);
    chart.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(el);
    return () => {
      observer.disconnect();
      instance.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    chart.current?.setOption(option, true);
  }, [option]);

  const swatch = { display: "inline-block", width: 10, height: 10, marginRight: 4 };
  return (
    <div>
      <div
        ref={ref}
        role="img"
        aria-label={T.chart.ariaLabel}
        style={{ width: "100%", height: 520 }}
      />
      <div style={{ display: "flex", gap: 16, fontSize: 11, color: TEXT, marginTop: 4 }}>
        <span>
          <i style={{ ...swatch, background: SUSPEND, border: "1px solid #8c8c8c" }} />
          {T.chart.legendSuspended}
        </span>
        <span style={{ color: EX }}>┆ {T.chart.legendEx}</span>
        <span>
          <i style={{ ...swatch, background: MISSING }} />
          {T.chart.legendMissing}
        </span>
      </div>
    </div>
  );
}
