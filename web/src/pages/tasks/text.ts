// 任务中心页面的全部界面文字。将来做多语言时，从这里替换。

import type { LogLevel, TaskStatus } from "../../api/client";

export const TEXT = {
  title: "任务中心",
  refresh: "刷新",
  create: "新建任务",
  emptyText: "还没有任务",
  loadFailed: "加载任务失败",
  showingLatest: (n: number) => `显示最近 ${n} 个任务`,

  filter: {
    status: "状态",
    type: "类型",
    all: "全部",
  },

  column: {
    id: "ID",
    type: "类型",
    status: "状态",
    progress: "进度",
    attempt: "第几次执行",
    createdAt: "创建时间",
    duration: "耗时",
    durationHint: "本次执行从开始到结束（运行中则到现在）的时间",
    actions: "操作",
  },

  status: {
    queued: "排队中",
    running: "运行中",
    succeeded: "成功",
    failed: "失败",
    cancelled: "已取消",
  } satisfies Record<TaskStatus, string>,
  cancelling: "取消中",
  waitingRetry: (time: string) => `将于 ${time} 重试`,

  level: {
    debug: "调试",
    info: "信息",
    warning: "警告",
    error: "错误",
  } satisfies Record<LogLevel, string>,

  action: {
    detail: "详情",
    cancel: "取消",
    retry: "重试",
    cancelConfirmTitle: "确认取消这个任务？",
    cancelConfirmQueued: "任务还在排队，会立即取消。",
    cancelConfirmRunning: "任务正在运行，会在下一个检查点停止，已经完成的部分不会回滚。",
    cancelConfirmOk: "取消任务",
    cancelConfirmBack: "不取消",
    cancelled: (id: number) => `已请求取消任务 ${id}`,
    retried: (id: number) => `任务 ${id} 已重新排队`,
    failed: (reason: string) => `操作失败：${reason}`,
  },

  drawer: {
    title: (id: number, type: string) => `任务 ${id} · ${type}`,
    basics: "概况",
    params: "参数",
    error: "错误",
    result: "结果",
    logs: "日志",
    noLogs: "暂无日志",
    attempt: (n: number) => `第 ${n} 次执行`,
    createdAt: "创建时间",
    startedAt: "本次开始",
    finishedAt: "结束时间",
    dedupeKey: "去重键",
    none: "无",
    loadFailed: "加载任务详情失败",
  },

  createModal: {
    title: "新建任务",
    type: "任务类型",
    typeRequired: "请选择任务类型",
    params: "参数（JSON）",
    paramsHint: "留空表示使用默认值",
    paramsInvalid: "参数不是合法的 JSON 对象",
    submit: "创建",
    created: (id: number) => `已创建任务 ${id}`,
    noTypes: "没有可用的任务类型",
  },

  duration: {
    seconds: (s: number) => `${s} 秒`,
    minutes: (m: number, s: number) => `${m} 分 ${s} 秒`,
    hours: (h: number, m: number) => `${h} 小时 ${m} 分`,
  },
} as const;
