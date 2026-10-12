"use strict";
const $ = (id) => document.getElementById(id);
const online =
  location.protocol === "http:" && location.hostname === "127.0.0.1";
const token = document.querySelector('meta[name="session-token"]').content;
let remoteSources = [];
let fileSources = [];
let busy = false;
let urlsAtFetch = "";
let healthReport = null;
const options = () => ({
  keywords: $("keywords").value,
  interval: Number($("interval").value),
  tolerance: Number($("tolerance").value),
  directDomains: $("direct-domains").value,
});
const urls = () =>
  $("urls")
    .value.split(/\r?\n/)
    .map((s) => s.trim())
    .filter(Boolean);
function status(text, error = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", error);
}
async function api(path, data) {
  const response = await fetch(path, {
    method: data ? "POST" : "GET",
    headers: {
      "X-Session-Token": token,
      ...(data ? { "Content-Type": "application/json" } : {}),
    },
    body: data ? JSON.stringify(data) : undefined,
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "本地请求失败。");
  return result;
}
function records() {
  return NodeFilter.combine([...remoteSources, ...fileSources]);
}
function preview() {
  $("sources").textContent =
    `已读取 ${remoteSources.length} 个订阅、${fileSources.length} 个文件。`;
  $("nodes").replaceChildren();
  $("summary").textContent = "";
  if (!remoteSources.length && !fileSources.length) return;
  const combined = records();
  const healthOptions = healthReport
    ? { health: healthReport.results, requireHealthy: true }
    : {};
  const result = NodeFilter.filter(combined, {
    ...options(),
    ...healthOptions,
  });
  $("summary").textContent =
    `共 ${combined.length} 个节点，${healthReport ? "检测后保留" : "未测候选"} ${result.kept.length} 个，自动排除 ${result.reasons.size} 个。`;
  const fragment = document.createDocumentFragment();
  for (const record of combined) {
    const row = document.createElement("tr");
    const measured = healthReport?.results.find((r) => r.name === record.id);
    for (const text of [
      record.originalName,
      record.source,
      result.reasons.get(record.id) ||
        (measured ? `检测通过（${measured.delay} 毫秒）` : "待检测候选"),
    ]) {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.append(cell);
    }
    fragment.append(row);
  }
  $("nodes").append(fragment);
}
async function run(action) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button, input, textarea").forEach((el) => {
    el.disabled = true;
  });
  try {
    await action();
  } catch (error) {
    status(error.message, true);
  } finally {
    busy = false;
    document.querySelectorAll("button, input, textarea").forEach((el) => {
      el.disabled = false;
    });
  }
}
$("fetch").onclick = () =>
  run(async () => {
    if (!online)
      throw new Error(
        "在线读取订阅请双击「启动工具.command」；直接打开 HTML 时可上传或粘贴 YAML。",
      );
    if (!urls().length) throw new Error("请填写订阅链接。");
    remoteSources = [];
    healthReport = null;
    urlsAtFetch = "";
    preview();
    status("正在下载订阅，通常需要 10–40 秒…");
    const result = await api("/api/fetch", { urls: urls() });
    const failed = result.sources.filter((s) => s.error);
    if (failed.length)
      throw new Error(
        failed.map((s) => `${s.label}：${s.error}`).join("\n") +
          "\n请修正或移除失败链接后重新读取。",
      );
    NodeFilter.combine(result.sources);
    remoteSources = result.sources;
    urlsAtFetch = $("urls").value.trim();
    preview();
    status("订阅已整合，可以生成配置。");
  });
$("files").onchange = () =>
  run(async () => {
    const additions = [];
    for (const file of $("files").files) {
      if (file.size > 8 * 1024 * 1024)
        throw new Error("单个文件不能超过 8 MB。");
      const source = { label: file.name, text: await file.text() };
      NodeFilter.combine([source]);
      additions.push(source);
    }
    fileSources.push(...additions);
    healthReport = null;
    $("files").value = "";
    preview();
    status("本地文件已添加。");
  });
$("add-content").onclick = () =>
  run(async () => {
    const text = $("content").value.trim();
    if (!text) throw new Error("请先粘贴 YAML 内容。");
    if (new TextEncoder().encode(text).length > 8 * 1024 * 1024)
      throw new Error("内容不能超过 8 MB。");
    const label = $("source-name").value.trim();
    if (!label) throw new Error("请填写粘贴内容的机场名称，用作导出节点的前缀。");
    const source = { label, text };
    NodeFilter.combine([source]);
    fileSources.push(source);
    healthReport = null;
    $("content").value = "";
    preview();
    status("文件内容已添加。");
  });
$("clear-files").onclick = () => {
  fileSources = [];
  healthReport = null;
  preview();
  status("已清空本地文件；订阅内容仍保留。");
};
$("urls").oninput = () => {
  remoteSources = [];
  urlsAtFetch = "";
  healthReport = null;
  preview();
};
for (const id of ["keywords", "interval", "tolerance", "direct-domains"])
  $(id).oninput = () => {
    try {
      healthReport = null;
      preview();
    } catch (error) {
      status(error.message, true);
    }
  };
$("save-settings").onclick = () =>
  run(async () => {
    if (!online) throw new Error("保存设置请通过「启动工具.command」打开。");
    await api("/api/save", { urls: urls(), preferences: options() });
    status(
      "订阅链接和过滤设置已保存到本地。上传或粘贴的文件内容仅在本次页面内保留。",
    );
  });
$("export").onclick = () =>
  run(async () => {
    if (
      urls().length &&
      (!remoteSources.length || urlsAtFetch !== $("urls").value.trim())
    )
      throw new Error(
        "订阅链接尚未读取或已修改，请先点击「读取并整合订阅」。仅处理文件时请清空链接。",
      );
    const snapshot = records();
    const exportOptions = options();
    let result = NodeFilter.generate(snapshot, exportOptions);
    if (online) {
      healthReport = null;
      status(
        `开始检测 ${result.kept.length} 个候选节点，通过后还会复测；可能需要数分钟…`,
      );
      const started = await api("/api/probe", {
        proxies: result.config.proxies,
        dns: result.config.dns,
      });
      let job;
      do {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        job = await api(`/api/probe?job=${encodeURIComponent(started.job)}`);
        status(`正在检测：${job.done} / ${job.total}，请等待…`);
      } while (job.state === "running");
      if (job.state !== "complete")
        throw new Error(job.error || "检测失败，未更新配置。");
      healthReport = job.report;
      preview();
      if (!healthReport.results.some((r) => r.status === "ok"))
        throw new Error("没有节点通过本次检测，未更新配置。请检查网络后重试。");
      result = NodeFilter.generate(snapshot, {
        ...exportOptions,
        health: healthReport.results,
        requireHealthy: true,
        checkedAt: healthReport.checkedAt,
      });
      await api("/api/export", { yaml: result.yaml, report: healthReport });
    }
    const blob = new Blob([result.yaml], {
      type: "application/yaml;charset=utf-8",
    });
    const link = document.createElement("a");
    const url = URL.createObjectURL(blob);
    link.href = url;
    link.download = "clash-filtered.yaml";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status(
      online
        ? `已检测并导出 ${result.kept.length} 个节点：output/clash-filtered.yaml。仅表示检测时连通；之后仍可能超时。`
        : "已下载未检测的整合配置，可能包含超时节点。实际检测请用启动工具打开页面。",
    );
    preview();
  });
async function initialize() {
  if (!online) {
    $("export").textContent = "仅整合并下载（未检测）";
    status(
      "离线模式：上传或粘贴 YAML 后即可生成。读取链接请用「启动工具.command」。",
    );
    return;
  }
  const data = await api("/api/settings");
  $("urls").value = data.urls.join("\n");
  const prefs = data.preferences;
  $("keywords").value = prefs.keywords || "";
  $("interval").value = prefs.interval ?? 120;
  $("tolerance").value = prefs.tolerance ?? 30;
  $("direct-domains").value = prefs.directDomains || "";
  if (data.sources.length) NodeFilter.combine(data.sources);
  fileSources = data.sources;
  preview();
  status("准备就绪。读取订阅后，生成整合配置。");
}
run(initialize);
