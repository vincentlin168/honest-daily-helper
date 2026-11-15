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
| 数据上报 | 可选，推送到你自己的服务端（默认关闭） |
| Skill 本地服务 | 127.0.0.1:8088，Bearer Token 鉴权，供其他 Agent 查询记录/日报 |
| 隐私 | 截图即用即弃、数据全本地、无授权、无联网验证 |

## 快速开始

```bash
# 1. 配置（复制示例，填你的 API Key）
cp config.example.json config.json
# 编辑 config.json，把 llm.api_key 填成你的 DeepSeek Key

# 2. 运行（开发环境 C:/Python313 已装 Pillow + httpx）
python main.py
```

启动后：
- 后台定时截图 → 识别 → 记录（默认 120 秒间隔）
- 到 `report.scheduled_time`（默认 18:00）自动生成日报
- Skill 服务跑在 `http://127.0.0.1:8088`

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
| `GET /api/report?date=YYYY-MM-DD` | 生成日报 | Bearer |

鉴权：`Authorization: Bearer <token>`（token 在 config.json 的 skill_server.token 配置，留空则不鉴权）。

## 目录结构

```
app/
├── config.py         配置（JSON，零依赖）
├── capture.py        截屏 + 像素变更检测（省 token）
├── llm_client.py     视觉 LLM 客户端（DeepSeek-flash + 内置识别 prompt）
├── store.py          SQLite 存储（entries/llm_calls/skip_events）
├── aggregate.py      活动会话聚合
├── report.py         日报生成（统计 + 模板 + AI 分析）
├── upload.py         数据上报（可选）
├── skill_server.py   Skill 本地服务（http.server）
├── main.py           主编排
└── tests/            58 个单元/端到端测试
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

## 设计参考

- 识别 prompt / 数据模型 / 日报模板：本项目自行设计与实现
- 变更检测 / 视觉 LLM 客户端：参考 ScreenPipe 开源项目
- 日报混合模式：参考 Work_Review 开源项目
