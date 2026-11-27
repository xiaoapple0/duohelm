import { describe, expect, it } from "vitest";

import { query } from "./client";

describe("query", () => {
  it("没有参数时返回空串，不带问号", () => {
    expect(query({})).toBe("");
  });

  it("undefined 的参数不写进查询串", () => {
    expect(query({ snapshot: undefined, limit: 50 })).toBe("?limit=50");
    expect(query({ snapshot: undefined })).toBe("");
  });

  it("数字按十进制文本写入，0 不会被当成没有", () => {
    expect(query({ limit: 200, offset: 0 })).toBe("?limit=200&offset=0");
  });

  it("保持参数的书写顺序", () => {
    expect(query({ start: "2024-01-02", end: "2024-01-09", adjust: "qfq" })).toBe(
      "?start=2024-01-02&end=2024-01-09&adjust=qfq",
    );
  });

  it("特殊字符按表单规则转义", () => {
    expect(query({ name_prefix: "平安 银行&1" })).toBe("?name_prefix=%E5%B9%B3%E5%AE%89+%E9%93%B6%E8%A1%8C%261");
  });

  it("空字符串是有效值，照样写入", () => {
    expect(query({ kind: "" })).toBe("?kind=");
  });
});
