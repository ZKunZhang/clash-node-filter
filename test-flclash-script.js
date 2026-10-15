"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(`${__dirname}/flclash-exclude-hk.js`, "utf8"), context);
const apply = (config) => context.main(config);

test("覆写保留原订阅规则、节点参数、provider 与策略选择，仅增加排除和空组处理", () => {
  const config = {
    proxies: [{ name: "香港", type: "ss", server: "example.com", password: "test" }],
    "proxy-providers": { airport: { type: "http", url: "https://example.com/sub" } },
    "proxy-groups": [
      { name: "自动", type: "url-test", proxies: ["香港", "日本"], tolerance: 80 },
      { name: "选择", type: "select", use: ["airport"], "exclude-filter": "到期`剩余流量" },
    ],
    rules: ["DOMAIN-SUFFIX,example.cn,DIRECT", "MATCH,选择"],
    dns: { enable: true, nameserver: ["223.5.5.5"] },
  };
  const before = structuredClone(config);
  assert.equal(apply(config), config);
  for (const key of ["proxies", "proxy-providers", "rules", "dns"]) {
    assert.deepEqual(config[key], before[key]);
  }
  config["proxy-groups"].forEach((group, i) => {
    const { ["exclude-filter"]: excluded, ["empty-fallback"]: fallback, ...rest } = group;
    const { ["exclude-filter"]: old, ...original } = before["proxy-groups"][i];
    assert.deepEqual(rest, original);
    assert.equal(fallback, "REJECT");
    if (old) assert.ok(excluded.startsWith(`${old}\``));
  });
  const once = structuredClone(config);
  apply(config);
  assert.deepEqual(config, once);
});

test("原有空组回退不会绕过香港过滤", () => {
  const config = { "proxy-groups": [
    { name: "香港组", type: "select", proxies: ["香港"], "empty-fallback": "香港" },
  ] };
  assert.equal(apply(config)["proxy-groups"][0]["empty-fallback"], "REJECT");
});

test("缺少策略组或无效排除规则会明确报错", () => {
  assert.throws(() => apply({ proxies: [] }), /没有 proxy-groups/);
  assert.throws(() => apply({ "proxy-groups": [{ "exclude-filter": ["香港"] }] }), /字符串/);
});
