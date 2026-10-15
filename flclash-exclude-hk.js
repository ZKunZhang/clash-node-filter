// FlClash：保存为覆写脚本，再给需要过滤的订阅选择此脚本。
function main(config) {
  const hongKong = "(?i)香港|香江|港区|港區|🇭🇰|hong[\\s_-]*kong|(?:^|[^a-z])hk(?:$|[^a-z])";
  if (!config || !Array.isArray(config["proxy-groups"])) {
    throw new Error("订阅没有 proxy-groups 策略组，无法应用香港过滤。");
  }
  for (const group of config["proxy-groups"]) {
    const existing = group["exclude-filter"] || "";
    if (typeof existing !== "string") {
      throw new Error("策略组的 exclude-filter 必须是字符串。");
    }
    // Mihomo 使用单个反引号分隔多个正则；保留订阅原有的排除条件。
    const filters = existing.split("`").filter(Boolean);
    if (!filters.includes(hongKong)) filters.push(hongKong);
    group["exclude-filter"] = filters.join("`");
    // 香港专用组可能被过滤空；此时拒绝连接，避免回退直连或香港节点。
    group["empty-fallback"] = "REJECT";
  }
  return config;
}
