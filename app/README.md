# 诚实日报助手

一个**本地优先、诚实、可审计**的电脑工作记录 + 日报工具。

实现「视觉 LLM 识别屏幕 → 活动会话聚合 → 日报回顾」的核心能力，
**不设任何授权/订阅/远程控制**：全本地、无联网验证、AI 直连官方（你自己的 key）、开源可审计、真永久。

## 核心特性

| 特性 | 说明 |
|---|---|
| 视觉 LLM 识别 | 截图交给视觉大模型（默认 DeepSeek-flash）理解工作内容 |
| 活动会话聚合 | 内容相同 → 合并延长记录，不重复烧 token |
| 日报生成 | 统计模板 + 可选 AI 分析（观察/证据/建议/小结），手动 + 定时自动 |
| 概览面板 | 段数 / 总时长 / 最长连续工作 / 最活跃时段 / 精力分布 |
| 工作热力图 | 最近 N 天 × 24 小时，跨小时按实际秒数分摊 |
| 成本透明度 | 显示每日真实 API 花费 —— 你的钱花在哪，明明白白 |
| 数据上报 | 可选，推送到你自己的服务端（默认关闭） |
| Skill 本地服务 | 127.0.0.1:8088，Bearer Token 鉴权，供其他 Agent 查询记录/日报 |
| 隐私 | 截图即用即弃、数据全本地、无授权、无联网验证 |

## 快速开始

```bash
# 1. 配置（复制示例，填你的 API Key）
cp config.example.json config.json
# 编辑 config.json，把 llm.api_key 填成你的 DeepSeek Key

# 2. 运行（需要 Python 3.11+）
pip install -r requirements.txt
python main.py
```

启动后：
- 后台每 **15 秒**抓一次屏并做像素比对（画面没变就跳过，**不消耗 token**）
- 画面变化时调用视觉模型识别，每 **120 秒**把结果聚合成「时间段」写进时间线
- 到 `report.scheduled_time`（默认 18:00）自动生成日报
- 面板跑在 `http://127.0.0.1:8088`

> 打开面板后可在「设置 → 测试连接」一键验证 Key、模型与网络是否可用。

### Windows 一键启动（桌面应用形态）

```bat
app\启动诚实日报助手.bat
```

以 Edge/Chrome 的**应用模式**打开无边框、无地址栏的独立窗口，看起来就是一个桌面应用。
**零额外依赖**，不需要安装任何新东西。

### 打包成 exe

```bat
app\打包.bat
```

产出 `app\dist\HonestDailyHelper.exe`（单文件）。首次运行会在同目录生成 `config.json`。

## 手动生成日报

```python
from main import DailyHelper
h = DailyHelper("config.json")
print(h.generate_report("2026-11-15", with_ai=True))
```

## Skill 服务接口

| 接口 | 说明 | 鉴权 |
|---|---|---|
| `GET /api/health` | 健康检查 | 无 |
| `GET /api/status` | 服务状态 + 今日记录数 | Bearer |
| `GET /api/entries?date=YYYY-MM-DD` | 按天查记录 | Bearer |
| `GET /api/overview?date=YYYY-MM-DD` | 概览统计 + 成本 | Bearer |
| `GET /api/heatmap?days=7` | 热力图网格（天 × 小时） | Bearer |
| `GET /api/report?date=YYYY-MM-DD` | 生成日报 | Bearer |
| `GET /api/settings` | 读取配置（密钥掩码） | Bearer |
| `POST /api/settings` | 更新配置 | Bearer |
| `POST /api/llm/test` | 连通性测试 | Bearer |
| `POST /api/entries/{id}/category` | 手动改分类 | Bearer |

鉴权：`Authorization: Bearer <token>`（token 在 config.json 的 `skill_server.token` 配置，留空则不鉴权）。

## 配置说明

| 键 | 默认值 | 说明 |
|---|---|---|
| `llm.api_base` | `https://api.deepseek.com/v1` | 任意 OpenAI 兼容接口 |
| `llm.api_key` | 空 | 你的 Key，只存在本机 |
| `llm.auth_type` | 空（自动） | `bearer` / `basic` / `none`；留空则自动判定 |
| `llm.api_username` | 空 | Basic 认证用户名（也可用 URL 内嵌 `user:pass@`） |
| `llm.api_password` | 空 | Basic 认证密码，只存在本机 |
| `llm.model` | `deepseek-flash` | 需支持视觉输入 |
| `llm.use_system_proxy` | `false` | 默认直连，不被系统代理改道 |
| `llm.price_per_1m_input` | `0` | 每百万输入 token 价格，用于成本显示 |
| `llm.price_per_1m_output` | `0` | 每百万输出 token 价格 |
| `capture.snap_interval` | `15` | 抓屏间隔（秒），只做像素比对，不花 token |
| `capture.interval` | `120` | 汇总写入周期（秒） |
| `capture.region_preset` | `full` | `full` / `left_half` / `right_half` / `center` |
| `report.scheduled_time` | `18:00` | 定时生成日报的时间 |
| `upload.enabled` | `false` | 数据上报默认关闭 |

> 未填写单价时，概览页只显示 token 用量，**不会替你编造金额** —— 价格由你自己定。

### 接入带密码的接口（URL + 密码）

除了最常见的 Bearer（API Key），本项目也支持 **Basic 认证**（用户名 + 密码）。
三种写法任选其一，效果完全相同：

1. **在设置页填写**：认证方式选「Basic」，填用户名和密码
2. **URL 内嵌凭据**：API 地址填 `https://用户名:密码@主机/v1`
   （凭据会被自动剥离，不会随 URL 出现在日志里）
3. **代码调用**：`LLMClient(api_base, api_key, model, auth_type="basic", api_username=..., api_password=...)`

留空「认证方式」时自动判定：URL 里内嵌了 `user:pass@` 或填了用户名密码 → Basic；
否则有 API Key → Bearer；都没有 → 不带认证头。

> 密码只保存在本机 `config.json`，面板显示时做掩码处理（`sup...cret`）。

## 目录结构

```
诚实日报助手/
├── .github/workflows/ci.yml   CI（自动跑测试）
├── .gitignore
├── CHANGELOG.md
├── LICENSE                    MIT
├── pyproject.toml             项目元数据
├── 开发计划.md                开发路线与阶段验收标准
└── app/
    ├── config.py              配置（JSON，零依赖）
    ├── capture.py             截屏 + 像素变更检测（省 token）
    ├── llm_client.py          视觉 LLM 客户端（DeepSeek-flash + 内置识别 prompt）
    ├── http_client.py         统一 HTTP 客户端（代理策略单一事实来源）
    ├── logging_setup.py       控制台 + 文件双写日志
    ├── store.py               SQLite 存储（entries / llm_calls / skip_events）
    ├── aggregate.py           活动会话聚合
    ├── overview.py            概览统计 + 成本换算
    ├── report.py              日报生成（统计 + 模板 + AI 分析）
    ├── upload.py              数据上报（可选）
    ├── skill_server.py        Skill 本地服务 + Web UI 后端
    ├── main.py                主编排
    ├── web_ui.html            Web 界面（概览 / 时间线 / 日报 / 设置）
    ├── 启动诚实日报助手.bat     Edge 应用模式启动器
    ├── 打包.bat                一键打包 exe
    └── tests/                 112 个单元/端到端测试
```

## 运行测试

```bash
cd app
python -m unittest discover -s tests -v
```

## 设计原则（诚实）

1. **全本地**：数据只存本机 SQLite。
2. **无授权**：没有试用次数、订阅、联网验证、远程吊销。
3. **AI 直连官方**：用你自己的 API Key，成本透明，无第三方中转。
4. **截图即用即弃**：默认不落盘，只存文字识别结果。
5. **开源可审计**：每一行代码你都看得见、改得动。
6. **失败可见**：出错会写日志、会在面板上显示，不静默吞掉。

## 设计参考

- 变更检测 / 视觉 LLM 客户端：参考 ScreenPipe 开源项目
- 日报混合模式：参考 Work_Review 开源项目
- 识别 prompt / 数据模型 / 日报模板 / 概览与热力图：本项目自行设计与实现
