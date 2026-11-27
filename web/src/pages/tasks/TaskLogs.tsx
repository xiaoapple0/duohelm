import { Empty, Tag, Typography } from "antd";
import { useLayoutEffect, useRef } from "react";

import type { LogLevel, TaskLog } from "../../api/client";
import { formatTime } from "./format";
import { TEXT } from "./text";

const LEVEL_COLOR: Record<LogLevel, string> = {
  debug: "default",
  info: "blue",
  warning: "orange",
  error: "red",
};

/** 参数、结果等 JSON 文本块的样式。 */
export const preStyle = {
  margin: 0,
  padding: 8,
  maxHeight: 240,
  overflow: "auto",
  background: "rgba(0, 0, 0, 0.04)",
  borderRadius: 6,
  fontSize: 12,
  whiteSpace: "pre-wrap",
  wordBreak: "break-all",
} as const;

function groupByAttempt(logs: TaskLog[]): [number, TaskLog[]][] {
  const groups = new Map<number, TaskLog[]>();
  for (const log of logs) {
    const list = groups.get(log.attempt);
    if (list) {
      list.push(log);
    } else {
      groups.set(log.attempt, [log]);
    }
  }
  return [...groups.entries()];
}

interface Props {
  logs: TaskLog[];
  /** 任务还没结束：有新日志时滚到最新一行 */
  following: boolean;
}

/** 任务日志，按执行次数分组。任务中心的详情抽屉和数据同步页共用。 */
export default function TaskLogs({ logs, following }: Props) {
  const box = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = box.current;
    if (el && following) {
      el.scrollTop = el.scrollHeight;
    }
  }, [logs, following]);

  return (
    <div ref={box} style={{ ...preStyle, maxHeight: 360, whiteSpace: "normal" }}>
      {logs.length === 0 ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={TEXT.drawer.noLogs} />
      ) : (
        groupByAttempt(logs).map(([attempt, items]) => (
          <div key={attempt} style={{ marginBottom: 12 }}>
            <Typography.Text strong>{TEXT.drawer.attempt(attempt)}</Typography.Text>
            {items.map((log) => (
              <div
                key={log.id}
                style={{ fontFamily: "monospace", whiteSpace: "pre-wrap", marginTop: 2 }}
              >
                <Typography.Text type="secondary">{formatTime(log.ts)}</Typography.Text>{" "}
                <Tag color={LEVEL_COLOR[log.level]} style={{ marginInlineEnd: 4 }}>
                  {TEXT.level[log.level]}
                </Tag>
                {log.message}
              </div>
            ))}
          </div>
        ))
      )}
    </div>
  );
}
