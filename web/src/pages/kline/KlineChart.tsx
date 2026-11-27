import { useMemo, useState } from "react";
import type { MouseEvent } from "react";

import { extent, isComplete, ticks } from "./analysis";
import type { Slot } from "./analysis";

const W = 1000;
const H = 560;
const LEFT = 64;
const RIGHT = 12;
const PRICE_TOP = 16;
const PRICE_BOTTOM = 360;
const VOL_TOP = 384;
const VOL_BOTTOM = 490;
const AXIS_Y = 508;

const UP = "#cf1322"; // A 股习惯：红涨绿跌
const DOWN = "#389e0d";
const FLAT = "#8c8c8c";
const GRID = "#f0f0f0";
const TEXT = "#595959";
const SUSPEND = "rgba(140,140,140,0.18)";
const EX = "#d46b08";
const MISSING = "#722ed1";

function fmtPrice(v: number | null): string {
  return v === null ? "缺失" : v.toFixed(2);
}

function fmtVolume(v: number | null): string {
  if (v === null) {
    return "缺失";
  }
  if (v >= 1e8) {
    return `${(v / 1e8).toFixed(2)} 亿股`;
  }
  if (v >= 1e4) {
    return `${(v / 1e4).toFixed(2)} 万股`;
  }
  return `${v} 股`;
}

function fmtAxisVolume(v: number): string {
  if (v === 0) {
    return "0";
  }
  if (v >= 1e8) {
    return `${+(v / 1e8).toFixed(2)}亿`;
  }
  if (v >= 1e4) {
    return `${+(v / 1e4).toFixed(2)}万`;
  }
  return String(v);
}

function describe(slot: Slot): string {
  const tags: string[] = [];
  if (slot.suspended) {
    tags.push("停牌（参考日历有、这只证券没有日线）");
  }
  if (slot.exDate) {
    tags.push("除权");
  }
  const bar = slot.bar;
  if (bar === null) {
    return `${slot.date}　${tags.join("　")}`;
  }
  return [
    slot.date,
    `开 ${fmtPrice(bar.open)}`,
    `高 ${fmtPrice(bar.high)}`,
    `低 ${fmtPrice(bar.low)}`,
    `收 ${fmtPrice(bar.close)}`,
    `量 ${fmtVolume(bar.volume)}`,
    ...tags,
  ].join("　");
}

export default function KlineChart({ slots }: { slots: Slot[] }) {
  const [hover, setHover] = useState<number | null>(null);

  const layout = useMemo(() => {
    const bars = slots.flatMap((s) => (s.bar ? [s.bar] : []));
    const prices = extent(bars.flatMap((b) => [b.open, b.high, b.low, b.close]));
    const volumes = extent(bars.map((b) => b.volume));
    const pad = prices ? Math.max((prices.max - prices.min) * 0.05, prices.max * 1e-3) : 0;
    return {
      price: prices ? { min: prices.min - pad, max: prices.max + pad } : null,
      volMax: volumes && volumes.max > 0 ? volumes.max : null,
    };
  }, [slots]);

  const n = slots.length;
  const slotW = (W - LEFT - RIGHT) / Math.max(n, 1);
  const bodyW = Math.max(slotW * 0.7, 1);
  const xOf = (i: number) => LEFT + slotW * (i + 0.5);
  const yPrice = (v: number) => {
    const p = layout.price;
    if (!p) {
      return PRICE_BOTTOM;
    }
    return PRICE_BOTTOM - ((v - p.min) / (p.max - p.min || 1)) * (PRICE_BOTTOM - PRICE_TOP);
  };
  const yVol = (v: number) =>
    VOL_BOTTOM - (v / (layout.volMax ?? 1)) * (VOL_BOTTOM - VOL_TOP);

  const priceTicks = layout.price ? ticks(layout.price.min, layout.price.max, 6) : [];
  const volTicks = layout.volMax ? ticks(0, layout.volMax, 3) : [];
  const labelEvery = Math.max(Math.ceil(n / 8), 1);

  const onMove = (e: MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width === 0) {
      return;
    }
    const x = ((e.clientX - rect.left) / rect.width) * W;
    const i = Math.floor((x - LEFT) / slotW);
    setHover(i >= 0 && i < n ? i : null);
  };

  const active = hover !== null ? slots[hover] : undefined;

  return (
    <div>
      <div style={{ minHeight: 24, fontSize: 13, color: TEXT, marginBottom: 4 }}>
        {active ? describe(active) : "把鼠标移到图上查看每天的数据"}
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-label="K 线和成交量"
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
        style={{ display: "block", maxWidth: "100%" }}
      >
        {priceTicks.map((t) => (
          <g key={`p${t}`}>
            <line x1={LEFT} x2={W - RIGHT} y1={yPrice(t)} y2={yPrice(t)} stroke={GRID} />
            <text x={LEFT - 6} y={yPrice(t) + 4} fontSize={11} textAnchor="end" fill={TEXT}>
              {t}
            </text>
          </g>
        ))}
        {volTicks.map((t) => (
          <g key={`v${t}`}>
            <line x1={LEFT} x2={W - RIGHT} y1={yVol(t)} y2={yVol(t)} stroke={GRID} />
            <text x={LEFT - 6} y={yVol(t) + 4} fontSize={11} textAnchor="end" fill={TEXT}>
              {fmtAxisVolume(t)}
            </text>
          </g>
        ))}

        {slots.map((s, i) => {
          const x = xOf(i);
          return (
            <g key={s.date}>
              {s.suspended && (
                <rect
                  x={x - slotW / 2}
                  y={PRICE_TOP}
                  width={Math.max(slotW, 1)}
                  height={VOL_BOTTOM - PRICE_TOP}
                  fill={SUSPEND}
                />
              )}
              {s.exDate && (
                <g>
                  <line
                    x1={x}
                    x2={x}
                    y1={PRICE_TOP}
                    y2={VOL_BOTTOM}
                    stroke={EX}
                    strokeDasharray="4 3"
                  />
                  <text x={x} y={PRICE_TOP + 10} fontSize={11} textAnchor="middle" fill={EX}>
                    除
                  </text>
                </g>
              )}
              {s.bar && <BarMarks slot={s} x={x} bodyW={bodyW} yPrice={yPrice} yVol={yVol} />}
            </g>
          );
        })}

        {slots.map((s, i) =>
          i % labelEvery === 0 ? (
            <text key={`x${s.date}`} x={xOf(i)} y={AXIS_Y} fontSize={11} textAnchor="middle" fill={TEXT}>
              {s.date}
            </text>
          ) : null,
        )}

        {hover !== null && (
          <line
            x1={xOf(hover)}
            x2={xOf(hover)}
            y1={PRICE_TOP}
            y2={VOL_BOTTOM}
            stroke="#8c8c8c"
            strokeWidth={0.5}
          />
        )}

        <line x1={LEFT} x2={LEFT} y1={PRICE_TOP} y2={VOL_BOTTOM} stroke="#d9d9d9" />
        <text x={LEFT} y={PRICE_TOP - 4} fontSize={11} fill={TEXT}>
          价格
        </text>
        <text x={LEFT} y={VOL_TOP - 4} fontSize={11} fill={TEXT}>
          成交量（股，不复权）
        </text>

        {/* 图例 */}
        <g fontSize={11} fill={TEXT} transform={`translate(${LEFT}, ${H - 18})`}>
          <rect x={0} y={-9} width={10} height={10} fill={SUSPEND} stroke="#8c8c8c" />
          <text x={14} y={0}>停牌</text>
          <line x1={60} x2={60} y1={-10} y2={2} stroke={EX} strokeDasharray="3 2" />
          <text x={66} y={0}>除权日</text>
          <rect x={130} y={-9} width={10} height={10} fill={MISSING} />
          <text x={144} y={0}>价格缺失</text>
        </g>
      </svg>
    </div>
  );
}

function BarMarks({
  slot,
  x,
  bodyW,
  yPrice,
  yVol,
}: {
  slot: Slot;
  x: number;
  bodyW: number;
  yPrice: (v: number) => number;
  yVol: (v: number) => number;
}) {
  const bar = slot.bar;
  if (bar === null) {
    return null;
  }
  const complete = isComplete(bar);
  // 红涨绿跌；收盘价缺失时不判断涨跌，成交量柱用灰色
  const color = complete ? (bar.close > bar.open ? UP : bar.close < bar.open ? DOWN : FLAT) : FLAT;
  return (
    <g>
      {complete ? (
        <g stroke={color} fill={color}>
          <line x1={x} x2={x} y1={yPrice(bar.high)} y2={yPrice(bar.low)} />
          <rect
            x={x - bodyW / 2}
            y={Math.min(yPrice(bar.open), yPrice(bar.close))}
            width={bodyW}
            height={Math.max(Math.abs(yPrice(bar.open) - yPrice(bar.close)), 1)}
          />
        </g>
      ) : (
        // 缺任何一个价格就不画蜡烛，只在底部标出，不用 0 或其他值补
        <rect
          x={x - Math.max(bodyW / 2, 1)}
          y={PRICE_BOTTOM - 6}
          width={Math.max(bodyW, 2)}
          height={6}
          fill={MISSING}
        />
      )}
      {bar.volume !== null && (
        <rect
          x={x - bodyW / 2}
          y={yVol(bar.volume)}
          width={bodyW}
          height={Math.max(VOL_BOTTOM - yVol(bar.volume), 0)}
          fill={color}
          opacity={0.7}
        />
      )}
    </g>
  );
}
