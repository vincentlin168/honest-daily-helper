# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **MCP 工具集（只读）** —— 让 AI Agent 通过 MCP（Model Context Protocol）
  直接查询工作记录、日报与 API 花费。新增 `app/mcp_tools.py`（18 个只读工具）
  与 `app/mcp_server.py`（stdio 传输，JSON-RPC 2.0）。
  - **结构上只读** —— 写方法在类型层面不可达（`ReadOnlyStore` 白名单转发），
    不是靠文档约定「请不要调用写方法」。
  - **无副作用** —— 不写库、不写文件、**不调 LLM**（因此不花钱）。
    `get_daily_report` 只出模板版日报；带 AI 分析的版本用 `get_report_body`
    读主程序已留档的日报。
  - **截断诚实告知** —— 列表类工具默认最多 200 条，返回里的 `truncated` 字段
    为 `true` 表示还有更多，不会让 Agent 误以为看到的就是全部。
  - **不碰密钥** —— 配置类信息一律不暴露。
  - 授权边界：stdio 是进程级隔离 —— 能启动该进程的人就是授权边界。

### 修复

- **MCP stdio 的中文编码陷阱** —— Windows 上 `sys.__stdout__` / `sys.__stdin__`
  的编码是系统 ANSI 代码页（简体中文环境为 GBK / cp936），而 MCP 协议规定用 UTF-8。
  不显式指定编码时，中文工具描述与返回内容会被按 GBK 写出去，Agent 按 UTF-8
  解析直接失败（表现为「每次调用都报 JSON 解析错误」）。
  这个坑很隐蔽：纯 ASCII 请求一切正常，只有出现中文才炸。
  已在 `mcp_server.py` 中显式用 UTF-8 包装输入输出流，并补中文往返的回归测试。
- **托盘窗口类名冲突（CI 随机失败）** —— `tray.py` 用 `f"HHDTray_{pid}_{id(self)}"`
  生成窗口类名，而 CPython 的 `id()` 是内存地址、对象回收后会被复用，
  新实例可能撞上「已注册未注销」的同名类，`RegisterClassW` 报 1410
  （ERROR_CLASS_ALREADY_EXISTS）。表现为随机失败：同一份代码 3.12/3.13 通过、
  3.11 失败。改用进程内单调计数器生成类名，并在撞名时自动换名重试。
- **「测试连接」假阴性（凭据热更新后仍报 401）** —— `main.py` 启动时把
  `llm_tester=self.llm.test_connection` 直接绑给 `SkillServer`，该绑定固定住了
  **服务启动那一刻**的 `LLMClient` 实例。用户在设置页改掉 api_key / api_base 后，
  识别循环走被 `_reload_settings()` 重建过的 `self.llm`（用新凭据，正常），
  而「测试连接」仍走旧实例（用旧凭据，报 401）。
  表现为 Key 明明改对了、测试连接却仍说连不上，只能靠重启进程解决。
  改为 `self._make_llm_tester()`，返回一个每次调用都取当前实例的可调用对象；
  凭据变更后测试连接立即反映真实状态，**无需重启**。

### 新增

- **Basic 认证（URL + 密码）** —— 除 Bearer（API Key）外，支持用户名 + 密码的
  Basic 认证，以及从 URL 内嵌的 `https://user:pass@host/v1` 自动识别。
  三种写法等价：设置页选「Basic」填用户名密码 / 地址里内嵌凭据 / 代码传
  `auth_type="basic"`。认证方式留空时自动判定（有 URL 内嵌凭据或用户名密码 → Basic；
  有 Key → Bearer；都没有 → 不带认证头）。
- **认证逻辑单一事实来源** —— 新增 `LLMClient._request_headers()`，
  图片识别 / 连通性测试 / 日报 AI 分析三条路径共用。
  顺手消灭了 `main.py` 日报 AI 分析里硬编码的 `Authorization: Bearer`（Basic 部署会在此失败）。
- **URL 内嵌凭据自动剥离** —— `_split_url_credentials()` 把 `user:pass@` 从 URL 拆出，
  凭据不再残留在 `api_base` 里，不会随 URL 出现在日志或错误信息中。
- 设置项新增 `llm.auth_type`、`llm.api_username`、`llm.api_password`；
  面板「模型设置」新增认证方式下拉与用户名/密码输入框，引导向导同步支持。
- `api_password` 与 `api_key` 同等保护：面板回显掩码，且掩码值回传不覆盖真实密码。

### 变更

- 日报 AI 分析改用 `LLMClient.chat_text()`，与识别路径共用同一套认证与解析逻辑。

## [2.0.0] - 2026-11-16

从「能跑」到「可交付」：修掉 6 处真实缺陷，补上数据可视化与成本透明度，
建立 CI 与打包链路。

### 修复

- **系统代理劫持链路** —— httpx 默认 `trust_env=True` 会读取 Windows 注册表的系统代理。
  开着 Clash/V2Ray 的机器上：数据上报收到 502、日报 AI 分析静默失败回退模板、
  本地面板请求被代理转发。新增 `http_client.py` 统一客户端工厂，默认 `trust_env=False`
  （可通过 `llm.use_system_proxy` / `upload.use_system_proxy` 显式开启）。
- **连通性测试间歇性假失败** —— `test_connection()` 用 `max_tokens=20`，而 deepseek-flash
  这类推理模型会先输出 `reasoning_content`，预算被思维链吃光导致 `content` 为空，
  被误判成「Key 额度问题」。改为 512 token 预算 + 优先取 `content`、回退 `reasoning_content`。
- **HTTP 服务单线程阻塞** —— `HTTPServer` → `ThreadingHTTPServer`，面板切页签不再互相等待。
- **面板上的「截屏间隔」是假设置** —— 实际抓屏间隔是 `capture.snap_interval`（默认 15s），
  面板却只暴露 `capture.interval`（汇总周期，默认 120s）。已拆为两个独立字段并各自说明。
- **打包漏资源** —— `web_ui.html` 与 `config.example.json` 均已打进 exe；
  兜底配置改用 `config.DEFAULTS` 单一事实来源，消除双份默认值漂移。
- **窗口模式下静默黑盒** —— 新增 `logging_setup.py`，控制台与文件双写日志。

### 新增

- **概览面板** —— 活动段数、总时长、最长连续工作、最活跃时段、识别调用数、
  精力分布 Top N。
- **工作热力图** —— 最近 N 天 × 24 小时；跨小时活动按实际秒数分摊，不整段记入开始小时。
- **成本透明度** —— 按用户填写的单价换算每日真实花费（`Decimal` 精度）。
  未填单价时只显示 token 数、不编造金额。
- **`POST /api/llm/test`** —— 连通性测试接口 + 面板「测试连接」按钮。
- **`GET /api/overview`、`GET /api/heatmap`** —— 新数据接口。
- **CI** —— GitHub Actions 在 Python 3.11/3.12/3.13 上跑测试与静态检查。
- **`pyproject.toml`、`LICENSE`(MIT)、`CHANGELOG.md`** —— 项目元数据与许可证。
- **`启动诚实日报助手.bat`** —— 以 Edge/Chrome 应用模式打开无边框面板，零额外依赖。
- **`打包.bat`** —— 一键产出单文件 exe。

### 变更

- 设置项新增 `llm.use_system_proxy`、`llm.price_per_1m_input`、`llm.price_per_1m_output`、
  `upload.use_system_proxy`。
- 面板新增「概览」页签作为默认首页；设置页新增「成本透明度」区。

## [1.0.0]

首个可用版本：视觉 LLM 识别屏幕 → 活动会话聚合 → 日报生成，
全本地、无授权、无联网验证、截图即用即弃。
