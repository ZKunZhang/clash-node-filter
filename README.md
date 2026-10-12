# clash-node-filter

一个在本机运行的 HTML 操作工具，将多个 Clash / Mihomo 订阅、本地 YAML 文件和粘贴内容整合为一份配置。默认按节点名称排除香港与流量、到期等订阅提示项；导出前可实际检测连通性，失败节点自动剔除，无需逐个勾选。

## 启动

需要 Python 3.9 或更高版本，不需要安装 Python 依赖。

```bash
git clone git@github.com:ZKunZhang/clash-node-filter.git
cd clash-node-filter
python3 app.py
```

macOS 也可以双击 `启动工具.command`。Windows 可用 `py app.py`。

页面只监听 `127.0.0.1`，默认使用随机端口。关闭终端或按 Ctrl+C 停止。需要指定端口时使用 `python3 app.py --port 8765`；不自动打开浏览器时使用 `python3 app.py --no-browser`。

直接打开 `index.html` 可以离线导入、粘贴和整合 YAML，但不会实际检测节点。这种模式会明确标记为“未检测”，仍可能包含超时节点。读取在线订阅、检测节点和保存本地设置需通过 Python 启动。

## 安装检测内核

Git 仓库不附带 Mihomo 可执行文件。需要检测节点时，从 [Mihomo 官方发布页](https://github.com/MetaCubeX/mihomo/releases) 下载与操作系统和 CPU 架构对应的版本，解压后放到：

- macOS / Linux：`vendor/mihomo`，并执行 `chmod +x vendor/mihomo`。
- Windows：`vendor/mihomo.exe`。

也可以将 `mihomo` 放入系统 PATH。未安装内核时，整合和预览仍可使用，在线页面检测导出会提示缺少内核。使用支持节点协议的版本；AnyTLS、VLESS、Hysteria2 等不一定被旧 Clash 内核支持。

版本元数据记录开发时验证的内核来源；它不表示已经为当前平台安装内核。第三方许可见 [THIRD_PARTY.md](THIRD_PARTY.md)。

## 使用

1. 输入订阅链接，每行一个，点击“读取并整合订阅”。也可以上传多个 YAML 文件，或粘贴文件内容后点击“添加文件内容”。这几种来源可以一起整合。
2. 如需额外排除节点，在“名称关键词”中填写关键词；如需直连其他网站，填写纯域名，不带协议、路径或通配符。
3. 点击“检测节点并下载 YAML”。数百个节点可能需要数分钟，页面会显示进度。
4. 将下载的 `clash-filtered.yaml` 导入 Mihomo 客户端，启用**规则模式**，在“节点选择”中选择“自动选择（排除香港）”。客户端若恢复了之前的手选状态，需要重新选择自动组。

配置也会写入 `output/clash-filtered.yaml`，检测记录写入 `output/health-report.local.json`。导出后可以关闭本工具，之后的定期检测和自动切换由客户端执行。

## 过滤、检测与分流

- 香港过滤依据名称中的中文、HK、Hong Kong 或旗帜标记，无法确认真实出口地区。
- 导出前通过独立 Mihomo 内核访问 `https://www.gstatic.com/generate_204`，要求 HTTPS 返回 204。单次超时 5 秒，失败重试一次，8 路并发。两次都失败的节点自动剔除；缺少检测结果的节点不进入已检测配置。
- 检测使用随机本地控制端口、随机认证信息和独立临时目录，不读取或修改当前客户端配置、系统代理或 TUN，检测结束后退出。
- 导出配置的自动组默认每 180 秒检测一次，切换容差 50 毫秒，测试地址和预期状态与导出前检测一致。客户端自动组切换节点，不会删除后来失败的节点。
- 默认钉钉、小红书、B站、飞书、豆包及部分资源域名直连；ChatGPT、YouTube、V2EX、Notion 及部分资源域名走“节点选择”。额外直连域名优先，其他国内 IP 直连，其余代理。具体清单见 `filter.js`。
- 节点的协议参数会保留，名称和已验证的链式引用会重命名。依赖已排除节点的链路也会排除。

检测通过只代表当时能访问测试地址，不保证之后不会超时，也不保证其他网站畅通。这不测带宽、验证码、出口信誉或 IP 漂移；低延迟不保证高峰期速度。已建立的连接未必随自动选择迁移。

此工具重新生成策略组、DNS 和规则，不继承原订阅的 TUN、控制接口或其他全局配置。`GEOIP,CN` 需要客户端可用的 GeoIP 数据。节点若引用外部证书或本地文件，也需保留这些依赖并调整路径。

仅支持内嵌 `proxies` 的 Clash YAML，不提供第三方远程转换，不自动拉取原订阅的 provider。任一订阅读取失败时会停止整合，修正或移除链接后重试。

## 本地配置与隐私

页面可以保存链接与过滤参数：

- `subscriptions.local.json`：真实订阅链接；仓库提供空的 `subscriptions.example.json` 作为格式示例。
- `preferences.local.json`：个人排除关键词、额外直连域名和检测参数。
- `inputs/*.yaml`：每次启动自动加载的本地节点文件。
- `output/`：订阅快照、生成配置、检测报告和备份。

上传或粘贴的内容只在本次页面内保留，刷新后需重新添加。生成的 YAML 是快照，不会自动更新订阅；节点变化后需重新读取、检测和导出。

订阅直接在本机与订阅服务商之间传输，不发给第三方转换服务。保存的订阅及导出配置可能包含 token、账号、密码、UUID、服务器地址等敏感内容；检测记录也可能暴露节点名称与使用时间。

`.gitignore` 排除了私人配置、输入输出、YAML、压缩包、日志、密钥、内核二进制和本地缓存。**不要上传这些私人文件或包含它们的压缩包，也不要用 `git add -f` 绕过忽略规则。** 忽略规则不会清除已经进入 Git 历史的文件。

## 校验

开发校验需要 Node.js 18 或更高版本，以及 Python 3.9 或更高版本：

```bash
node --test test-filter.js
python3 -m unittest test_app.py test_probe.py
```

这些测试使用模拟数据验证过滤、引用、接口和检测清理；不会访问真实订阅，也不需要安装 Mihomo。实际节点可用性仍需在使用机器上检测。

## 许可与行为依据

本工具原创代码使用 [MIT License](LICENSE)。第三方组件见 [THIRD_PARTY.md](THIRD_PARTY.md)。

行为依据：[Mihomo 自动选择组](https://wiki.metacubex.one/en/config/proxy-groups/url-test/)、[组检测参数](https://wiki.metacubex.one/en/config/proxy-groups/)、[规则顺序与域名后缀](https://wiki.metacubex.one/en/config/rules/)。
