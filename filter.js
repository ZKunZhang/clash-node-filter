(function (root) {
  "use strict";
  const AUTO = "自动选择（排除香港）";
  const BALANCE = "机场均衡（排除香港）";
  const SELECT = "节点选择";
  const DNS_CONFIG = {
    enable: true,
    "enhanced-mode": "fake-ip",
    nameserver: ["223.5.5.5", "119.29.29.29"],
  };
  const HK =
    /香港|香江|港区|港區|🇭🇰|hong[\s_-]*kong|(?:^|[^a-z])hk(?:$|[^a-z])/i;
  const NOTICE =
    /剩余流量|剩餘流量|到期|重置|套餐|官网|官網|网址|網址|公告|建议|建議|traffic|expire|reset/i;
  const DIRECT_DOMAINS = [
    "dingtalk.com",
    "dingding.com",
    "dingtalkapps.com",
    "g.alicdn.com",
    "gw.alicdn.com",
    "img.alicdn.com",
    "o.alicdn.com",
    "cloud.video.taobao.com",
    "xiaohongshu.com",
    "xhscdn.com",
    "xhslink.com",
    "bilibili.com",
    "bilibili.tv",
    "b23.tv",
    "hdslb.com",
    "bilivideo.com",
    "biliimg.com",
    "acgvideo.com",
    "feishu.cn",
    "feishu.net",
    "feishucdn.com",
    "larksuite.com",
    "larkoffice.com",
    "doubao.com",
    "doubao.cn",
    "lf3-short.ibytedapm.com",
  ];
  const PROXY_DOMAINS = [
    "chatgpt.com",
    "openai.com",
    "oaistatic.com",
    "oaiusercontent.com",
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "googlevideo.com",
    "ytimg.com",
    "ggpht.com",
    "youtubei.googleapis.com",
    "v2ex.com",
    "notion.so",
    "notion.site",
    "notion.com",
    "notionusercontent.com",
  ];
  function parser() {
    return typeof module !== "undefined" && module.exports
      ? require("./vendor/js-yaml.min.js")
      : root.jsyaml;
  }
  function parseSource(text, label) {
    let data;
    try {
      const yaml = parser();
      data = yaml.load(text, {
        schema: yaml.JSON_SCHEMA.withTags([yaml.mergeTag]),
      });
    } catch {
      throw new Error(`${label}：YAML 格式无效，请导入 Clash / Mihomo 格式。`);
    }
    if (!data || !Array.isArray(data.proxies) || !data.proxies.length) {
      throw new Error(
        `${label}：没有内嵌 proxies 节点；不支持仅含 provider 或 Base64 链接的订阅，请导出 Clash YAML。`,
      );
    }
    const names = new Set();
    return data.proxies.map((node) => {
      if (
        !node ||
        typeof node.name !== "string" ||
        !node.name.trim() ||
        typeof node.type !== "string"
      ) {
        throw new Error(`${label}：节点缺少 name / type。`);
      }
      if (names.has(node.name))
        throw new Error(`${label}：节点名称重复，无法可靠处理引用。`);
      names.add(node.name);
      return JSON.parse(JSON.stringify(node));
    });
  }
  function combine(sources) {
    if (!sources.length) throw new Error("请先读取订阅或导入 YAML。");
    const usedNames = new Set();
    return sources.flatMap((source) => {
      const nodes = parseSource(source.text, source.label);
      const label = String(source.label || "未命名机场")
        .trim()
        .replace(/\.(?:ya?ml|txt)$/i, "") || "未命名机场";
      const refs = new Map(
        nodes.map((n) => {
          const base = `${label} · ${n.name}`;
          let name = base;
          let suffix = 2;
          while (usedNames.has(name)) name = `${base} (${suffix++})`;
          usedNames.add(name);
          return [n.name, name];
        }),
      );
      return nodes.map((node) => {
        const originalName = node.name;
        const reference = node["dialer-proxy"];
        if (
          reference &&
          !["DIRECT", "REJECT"].includes(reference) &&
          !refs.has(reference)
        ) {
          throw new Error(
            `${source.label}：节点引用了缺失节点或原订阅策略组，无法合并。`,
          );
        }
        if (refs.has(reference)) node["dialer-proxy"] = refs.get(reference);
        node.name = refs.get(originalName);
        return { id: node.name, originalName, source: source.label, airport: label, node };
      });
    });
  }
  function filter(records, options = {}) {
    const excluded = new Set(options.excluded || []);
    const words = String(options.keywords || "")
      .split(/[,，\n]/)
      .map((x) => x.trim().toLowerCase())
      .filter(Boolean);
    const reasons = new Map();
    const health = new Map((options.health || []).map((r) => [r.name, r]));
    for (const r of records) {
      if (HK.test(r.originalName)) reasons.set(r.id, "香港名称");
      else if (NOTICE.test(r.originalName)) reasons.set(r.id, "订阅提示");
      else if (words.some((w) => r.originalName.toLowerCase().includes(w)))
        reasons.set(r.id, "关键词");
      else if (excluded.has(r.id)) reasons.set(r.id, "手动排除");
      else if (health.has(r.id) && health.get(r.id).status !== "ok")
        reasons.set(r.id, health.get(r.id).reason || "连通性检测失败");
      else if (options.requireHealthy && !health.has(r.id))
        reasons.set(r.id, "未检测");
    }
    const byId = new Map(records.map((r) => [r.id, r]));
    const done = new Set();
    function visit(id, stack = new Set()) {
      if (stack.has(id))
        throw new Error("节点存在循环 dialer-proxy 引用，无法导出。");
      if (done.has(id)) return;
      const r = byId.get(id);
      const ref = r.node["dialer-proxy"];
      if (ref && !["DIRECT", "REJECT"].includes(ref)) {
        if (!byId.has(ref)) throw new Error("节点引用缺失，无法导出。");
        visit(ref, new Set([...stack, id]));
        if (reasons.has(ref)) reasons.set(id, "依赖已排除节点");
      }
      done.add(id);
    }
    records.forEach((r) => visit(r.id));
    return { kept: records.filter((r) => !reasons.has(r.id)), reasons };
  }
  function generate(records, options = {}) {
    const result = filter(records, options);
    if (!result.kept.length)
      throw new Error("过滤后没有可用候选节点，请调整排除规则。");
    const interval = Number(options.interval ?? 120);
    const tolerance = Number(options.tolerance ?? 30);
    if (!Number.isInteger(interval) || interval < 30 || interval > 3600)
      throw new Error("测速间隔需为 30–3600 秒的整数。");
    if (!Number.isInteger(tolerance) || tolerance < 0 || tolerance > 1000)
      throw new Error("切换容差需为 0–1000 毫秒的整数。");
    const directDomains = [
      ...new Set([
        ...DIRECT_DOMAINS,
        ...String(options.directDomains || "")
          .split(/[,，\s]+/)
          .filter(Boolean),
      ]),
    ];
    if (
      directDomains.some(
        (d) => !/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/i.test(d),
      )
    )
      throw new Error("额外直连域名请填纯域名，不带 https://、路径或通配符。");
    const names = result.kept.map((r) => r.node.name);
    const health = new Map((options.health || []).map((r) => [r.name, r]));
    const delay = (record) => {
      const value = health.get(record.id)?.delay;
      return typeof value === "number" && Number.isFinite(value) && value > 0
        ? value
        : Infinity;
    };
    const sorted = [...result.kept].sort((a, b) => {
      const left = delay(a), right = delay(b);
      return left === right ? 0 : left < right ? -1 : 1;
    });
    const checkOptions = {
      url: "https://www.gstatic.com/generate_204",
      interval,
      lazy: false,
      timeout: 5000,
      "max-failed-times": 2,
      "expected-status": 204,
    };
    const automaticGroup = (name, proxies) => ({
      name, type: "url-test", proxies, ...checkOptions, tolerance,
    });
    // One seat per normalized source name, regardless of its node count.
    const airports = new Map();
    for (const r of sorted) {
      if (!airports.has(r.airport)) airports.set(r.airport, []);
      airports.get(r.airport).push(r.node.name);
    }
    const usedNames = new Set([SELECT, AUTO, BALANCE, "DIRECT", "REJECT", ...names]);
    const airportGroups = [...airports].map(([airport, proxies]) => {
      const base = `${airport} · 自动选择`;
      let name = base;
      let suffix = 2;
      while (usedNames.has(name)) name = `${base} (${suffix++})`;
      usedNames.add(name);
      return automaticGroup(name, proxies);
    });
    const config = {
      "mixed-port": 7890,
      "allow-lan": false,
      mode: "rule",
      "log-level": "info",
      dns: { ...DNS_CONFIG, nameserver: [...DNS_CONFIG.nameserver] },
      proxies: result.kept.map((r) => r.node),
      "proxy-groups": [
        { name: SELECT, type: "select", proxies: [BALANCE, AUTO, ...names] },
        automaticGroup(AUTO, sorted.map((r) => r.node.name)),
        {
          name: BALANCE,
          type: "load-balance",
          strategy: "round-robin",
          proxies: airportGroups.map((g) => g.name),
          ...checkOptions,
        },
        ...airportGroups,
      ],
      rules: [
        ...directDomains.map((d) => `DOMAIN-SUFFIX,${d.toLowerCase()},DIRECT`),
        ...PROXY_DOMAINS.map((d) => `DOMAIN-SUFFIX,${d},${SELECT}`),
        "DOMAIN,localhost,DIRECT",
        "DOMAIN-SUFFIX,local,DIRECT",
        "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
        "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
        "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
        "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
        "IP-CIDR6,::1/128,DIRECT,no-resolve",
        "IP-CIDR6,fc00::/7,DIRECT,no-resolve",
        "GEOIP,CN,DIRECT",
        `MATCH,${SELECT}`,
      ],
    };
    return {
      ...result,
      config,
      yaml:
        (options.health
          ? `# 导出前连通性检测：${options.checkedAt || ""}；HTTPS 204 检测通过。\n`
          : "# 未实测节点连通性，可能包含超时节点。\n") +
        "# 本地生成：按节点名称排除香港；后六类服务优先直连。\n" +
        parser().dump(config, { noRefs: true, lineWidth: -1 }),
    };
  }
  const api = {
    AUTO,
    BALANCE,
    SELECT,
    DNS_CONFIG,
    DIRECT_DOMAINS,
    PROXY_DOMAINS,
    combine,
    filter,
    generate,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.NodeFilter = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
