"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const tool = require("./filter");
const yaml = require("./vendor/js-yaml.min");
const source = (nodes, label = "test") => ({
  label,
  text: yaml.dump({ proxies: nodes }),
});
const http = (name, extra = {}) => ({
  name,
  type: "http",
  server: "example.com",
  port: 443,
  ...extra,
});

test("标准 YAML 合并锚点保留继承的协议参数", () => {
  const text =
    "defaults: &base\n  type: http\n  server: example.com\n  port: 443\nproxies:\n  - <<: *base\n    name: 日本\n";
  const result = tool.generate(tool.combine([{ label: "anchors", text }]));
  const node = result.config.proxies[0];
  assert.equal(node.type, "http");
  assert.equal(node.server, "example.com");
  assert.equal(node.port, 443);
  assert.equal(Object.hasOwn(node, "<<"), false);
});

test("检测失败和未检测节点不能导出；失败依赖被传递排除", () => {
  const records = tool.combine([
    source([
      http("日本"),
      http("美国"),
      http("链式", { "dialer-proxy": "美国" }),
      http("未检测"),
    ]),
  ]);
  const health = records.slice(0, 3).map((r, i) => ({
    name: r.id,
    status: i === 1 ? "timeout" : "ok",
    delay: i === 1 ? null : 100,
    reason: i === 1 ? "节点连接超时" : "连接成功",
  }));
  const result = tool.generate(records, {
    health,
    requireHealthy: true,
    checkedAt: "2026-10-12T00:00:00Z",
  });
  assert.deepEqual(
    result.kept.map((r) => r.originalName),
    ["日本"],
  );
  assert.equal(result.reasons.get(records[1].id), "节点连接超时");
  assert.equal(result.reasons.get(records[2].id), "依赖已排除节点");
  assert.equal(result.reasons.get(records[3].id), "未检测");
  assert.match(result.yaml, /导出前连通性检测/);
  assert.throws(
    () => tool.generate(records, { health: [], requireHealthy: true }),
    /没有可用/,
  );
});

test("自动排除中英文香港、旗帜和订阅提示，不误排其他名称", () => {
  const names = [
    "香港 01",
    "🇭🇰 专线",
    "HK01",
    "Hong-Kong 02",
    "hk_03",
    "剩余流量：10 GB",
    "日本 01",
    "US-San Jose",
    "Frankfurt",
  ];
  const result = tool.generate(
    tool.combine([source(names.map((n) => http(n)))]),
  );
  assert.deepEqual(
    result.kept.map((r) => r.originalName),
    ["日本 01", "US-San Jose", "Frankfurt"],
  );
  assert.equal(result.config["proxy-groups"][1].proxies.length, 3);
});
test("跨订阅重名节点保持唯一且保留完整协议参数", () => {
  const original = {
    name: "日本",
    type: "vless",
    server: "example.com",
    port: 443,
    uuid: "test",
    tls: true,
    "reality-opts": { "public-key": "test", "short-id": "0011" },
    "ws-opts": { path: "/tunnel", headers: { Host: "example.com" } },
    "client-fingerprint": "chrome",
  };
  const result = tool.generate(
    tool.combine([source([original], "a"), source([original], "b")]),
  );
  const roundtrip = yaml.load(result.yaml);
  assert.deepEqual(roundtrip.proxies.map((p) => p.name), ["a · 日本", "b · 日本"]);
  for (const p of roundtrip.proxies)
    assert.deepEqual({ ...p, name: "日本" }, original);
  assert.equal(result.config["proxy-groups"][0].proxies[0], tool.AUTO);
});
test("机场文件名作为前缀；同名来源和节点冲突仍保留正确引用与检测结果", () => {
  const records = tool.combine([
    source([http("日本"), http("日本 (2)")], "机场甲.yaml"),
    source([
      http("日本"),
      http("美国", { "dialer-proxy": "日本" }),
    ], "机场甲.yml"),
  ]);
  assert.deepEqual(records.map((r) => r.id), [
    "机场甲 · 日本",
    "机场甲 · 日本 (2)",
    "机场甲 · 日本 (3)",
    "机场甲 · 美国",
  ]);
  const health = records.map((r) => ({ name: r.id, status: "ok" }));
  const config = yaml.load(tool.generate(records, { health, requireHealthy: true }).yaml);
  assert.equal(config.proxies[3]["dialer-proxy"], "机场甲 · 日本 (3)");
  assert.deepEqual(config["proxy-groups"][1].proxies, records.map((r) => r.id));
  assert.equal(records[3].originalName, "美国");
});
test("香港链式依赖被传递排除；正常引用重命名后保留", () => {
  const nodes = [
    http("香港"),
    http("B", { "dialer-proxy": "香港" }),
    http("A", { "dialer-proxy": "B" }),
    http("日本"),
    http("C", { "dialer-proxy": "日本" }),
  ];
  const result = tool.generate(tool.combine([source(nodes)]));
  assert.deepEqual(
    result.kept.map((r) => r.originalName),
    ["日本", "C"],
  );
  assert.equal(result.kept[1].node["dialer-proxy"], result.kept[0].node.name);
});
test("自动组按有效检测延迟排序，保留同延迟顺序与原节点和引用", () => {
  const records = tool.combine([source([
    http("慢"), http("快"), http("同速", { "dialer-proxy": "慢" }),
    http("未提供延迟"), http("无效延迟"), http("无限延迟"),
  ], "机场")]);
  const delays = [2200, 400, 400, undefined, true, Infinity];
  const health = records.map((r, i) => ({ name: r.id, status: "ok", delay: delays[i] }));
  const result = tool.generate(records, { health, requireHealthy: true });
  assert.deepEqual(result.config["proxy-groups"][1].proxies, [
    records[1].id, records[2].id, records[0].id,
    records[3].id, records[4].id, records[5].id,
  ]);
  assert.deepEqual(result.config.proxies.map((p) => p.name), records.map((r) => r.id));
  assert.equal(result.config.proxies[2]["dialer-proxy"], records[0].id);
  assert.deepEqual(result.config["proxy-groups"][0].proxies, [tool.AUTO, ...records.map((r) => r.id)]);
  const group = result.config["proxy-groups"][1];
  assert.equal(group.interval, 120);
  assert.equal(group.tolerance, 30);
  assert.equal(group.timeout, 5000);
  assert.equal(group["max-failed-times"], 2);
  assert.equal(group.lazy, false);
});
test("关键词过滤也应用到依赖，并拒绝空结果", () => {
  const records = tool.combine([
    source([http("日本"), http("C", { "dialer-proxy": "日本" })]),
  ]);
  assert.throws(() => tool.generate(records, { keywords: "日本" }), /没有可用/);
});
test("拒绝循环、缺失引用、源内重名、不支持格式和参数错误", () => {
  assert.throws(() => tool.combine([source([http("A"), http("A")])]), /重复/);
  assert.throws(
    () => tool.combine([source([http("A", { "dialer-proxy": "missing" })])]),
    /缺失/,
  );
  const cycle = tool.combine([
    source([
      http("A", { "dialer-proxy": "B" }),
      http("B", { "dialer-proxy": "A" }),
    ]),
  ]);
  assert.throws(() => tool.generate(cycle), /循环/);
  assert.throws(
    () => tool.combine([{ label: "bad", text: "dmxlc3M6Ly8=" }]),
    /没有内嵌/,
  );
  const records = tool.combine([source([http("日本")])]);
  assert.throws(() => tool.generate(records, { interval: 0 }), /间隔/);
  assert.throws(() => tool.generate(records, { tolerance: -1 }), /容差/);
  assert.throws(
    () => tool.generate(records, { directDomains: "https://example.com" }),
    /纯域名/,
  );
});
test("默认国内服务和额外域名直连，前四类显式代理，规则优先于 GEOIP / MATCH", () => {
  const result = tool.generate(tool.combine([source([http("日本")])]), {
    directDomains: "EXAMPLE.com",
  });
  const rules = result.config.rules;
  for (const domain of [
    "dingtalk.com",
    "g.alicdn.com",
    "cloud.video.taobao.com",
    "xiaohongshu.com",
    "bilibili.com",
    "feishu.cn",
    "doubao.com",
    "lf3-short.ibytedapm.com",
    "example.com",
  ])
    assert.ok(rules.includes(`DOMAIN-SUFFIX,${domain},DIRECT`));
  for (const domain of ["chatgpt.com", "youtube.com", "v2ex.com", "notion.so"])
    assert.ok(rules.includes(`DOMAIN-SUFFIX,${domain},${tool.SELECT}`));
  assert.equal(rules.at(-2), "GEOIP,CN,DIRECT");
  assert.equal(rules.at(-1), `MATCH,${tool.SELECT}`);
});
