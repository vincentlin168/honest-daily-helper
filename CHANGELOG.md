# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
