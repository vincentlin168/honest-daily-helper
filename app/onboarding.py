"""首次运行引导（Spec-Driven）。

Spec
----
新用户第一次打开 Web 界面时，不应该面对一堆空白的输入框不知道填什么。
引导向导做三件事：

1. **判断是否需要引导**：API Key 没配 -> 需要；已配好 -> 跳过。
2. **分步收集必要信息**：模型连接 -> 抓屏区域 -> 分类偏好 -> 完成。
3. **每一步都能验证**：连不上就直接报错，不让用户走到最后才发现 Key 是错的。

设计要点
--------
1. **零依赖**：只用标准库。向导的每一步都能独立测试，不需要起 HTTP 服务。
2. **状态可持久化**：完成标记写在 config.json 的 ``onboarding.completed_at``，
   下次启动不再打扰用户。
3. **不代替用户做决定**：模型地址/名称给出常见选项，但都允许手填；
   分类默认给一套通用分类，用户可以改。
4. **诚实**：每一步都告诉用户「这一步在做什么、失败了会怎样」，
   不做「一键搞定」的假承诺。

配置结构：
    onboarding:  completed(bool) / completed_at(str) / skipped(bool)
"""

import re
from datetime import datetime
from typing import Any, Dict, List

# 常见视觉模型服务，用户可以直接选，也可以手填其它地址
KNOWN_PROVIDERS: List[Dict[str, str]] = [
    {
        "label": "DeepSeek（推荐 · 免费档支持看图）",
        "api_base": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
    },
    {
        "label": "OpenAI 兼容接口（任何兼容 OpenAI 格式的服务）",
        "api_base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    {
        "label": "本地 Ollama（离线，模型需自备）",
        "api_base": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5vl",
    },
]

DEFAULT_CATEGORIES = [
    "产品设计", "代码开发", "运营分析", "数据复盘",
    "客户支持", "项目管理", "日报整理", "资料研究", "其他",
]

# 区域预设说明（用户选「只截哪部分」时看）
REGION_OPTIONS: List[Dict[str, str]] = [
    {"value": "full", "label": "全屏", "hint": "最保险，但图片大、识别慢、token 贵"},
    {"value": "left_half", "label": "左半屏", "hint": "适合主窗口靠左的人"},
    {"value": "right_half", "label": "右半屏", "hint": "适合主窗口靠右的人"},
    {"value": "center", "label": "居中 1200x800", "hint": "适合双屏或窗口居中的人"},
]

# API Key 基本形状校验（只查明显不合理的，不查具体厂商规则）
_KEY_SUSPICIOUS = re.compile(r"^(your|placeholder|xxx|todo|changeme)", re.I)


class Onboarding:
    """引导向导的状态与步骤定义。"""

    STEPS: List[Dict[str, str]] = [
        {
            "id": "model",
            "title": "连接视觉模型",
            "desc": "助手需要能「看懂截图」的模型。没有这一步，抓到的屏只是一堆像素。",
        },
        {
            "id": "capture",
            "title": "选择抓屏范围",
            "desc": "只截你在用的那块屏幕，能省 token，也能减少识别噪音。",
        },
        {
            "id": "categories",
            "title": "确认工作方向",
            "desc": "识别时模型会从这些分类里挑最贴近的一个。之后在时间线里也能随时改。",
        },
        {
            "id": "done",
            "title": "开始使用",
            "desc": "助手会每隔几秒抓一次屏，识别出你在做什么，写进时间线。",
        },
    ]

    def __init__(self, config=None):
        self.config = config

    # ---------- 是否需要引导 ----------
    def is_completed(self) -> bool:
        """是否已完成过引导。

        判据不是「有没有 onboarding.completed_at 这个字段」，
        而是**实质条件**：API Key 已经填好（且不是占位符）。
        这样即使用户手删了标记，只要 Key 配好了也不会被重复打扰。
        """
        if self.config is None:
            return False
        key = str(self.config.get("llm", "api_key", default="") or "").strip()
        if not key or _KEY_SUSPICIOUS.match(key):
            return False
        if key == "your-api-key-here":
            return False
        return True

    def mark_completed(self) -> None:
        """标记引导已完成。"""
        if self.config is None:
            return
        self.config.set("onboarding", "completed", True)
        self.config.set("onboarding", "completed_at",
                         datetime.now().isoformat(timespec="seconds"))
        self.config.set("onboarding", "skipped", False)
        self.config.save()

    def mark_skipped(self) -> None:
        """用户主动跳过引导（下次仍会提示，但不强拦）。"""
        if self.config is None:
            return
        self.config.set("onboarding", "completed", False)
        self.config.set("onboarding", "skipped", True)
        self.config.set("onboarding", "skipped_at",
                         datetime.now().isoformat(timespec="seconds"))
        self.config.save()

    # ---------- 步骤 ----------
    def steps(self) -> List[Dict[str, Any]]:
        """返回向导步骤定义（供前端渲染）。"""
        out = []
        for idx, step in enumerate(self.STEPS):
            out.append({
                "index": idx,
                "id": step["id"],
                "title": step["title"],
                "desc": step["desc"],
            })
        return out

    def providers(self) -> List[Dict[str, str]]:
        """可选的模型服务。"""
        return list(KNOWN_PROVIDERS)

    def region_options(self) -> List[Dict[str, str]]:
        """可选的抓屏区域。"""
        return list(REGION_OPTIONS)

    # ---------- 校验 ----------
    def validate_model(self, api_base: str, model: str, api_key: str) -> Dict[str, Any]:
        """校验模型配置的基本合理性（不做网络请求）。

        网络连通性由 ``POST /api/llm/test`` 单独验证 ——
        这里只挡「明显填错」的情况，给出即时反馈，不用等用户点下一步。
        """
        problems: List[str] = []
        base = (api_base or "").strip()
        name = (model or "").strip()
        key = (api_key or "").strip()

        if not base:
            problems.append("API 地址不能为空")
        elif not base.startswith(("http://", "https://")):
            problems.append("API 地址应以 http:// 或 https:// 开头")
        if not name:
            problems.append("模型名不能为空")
        if not key:
            problems.append("API Key 不能为空")
        elif _KEY_SUSPICIOUS.match(key):
            problems.append("API Key 看起来还是示例值，请填入你真实的 Key")

        return {
            "ok": not problems,
            "problems": problems,
            "message": "配置看起来没问题，点「测试连接」验证一下" if not problems else "",
        }

    def validate_capture(self, region_preset: str, snap_interval: int) -> Dict[str, Any]:
        """校验抓屏设置。"""
        problems: List[str] = []
        valid_regions = {r["value"] for r in REGION_OPTIONS}
        if region_preset not in valid_regions:
            problems.append("未知的抓屏区域")
        try:
            interval = int(snap_interval)
        except (TypeError, ValueError):
            problems.append("抓屏间隔必须是数字")
            interval = 15
        if interval < 3:
            problems.append("抓屏间隔至少 3 秒（再短没有实际意义）")
        if interval > 300:
            problems.append("抓屏间隔最多 300 秒")
        return {
            "ok": not problems,
            "problems": problems,
            "message": "" if problems else "抓屏设置没问题",
        }

    def validate_categories(self, categories: List[str]) -> Dict[str, Any]:
        """校验分类列表。"""
        clean = [c.strip() for c in (categories or []) if c and c.strip()]
        # 去重但保持顺序
        seen = set()
        unique = []
        for c in clean:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        problems: List[str] = []
        if not unique:
            problems.append("至少保留一个分类，否则模型无处可归")
        if len(unique) > 30:
            problems.append("分类太多（超过 30 个），模型会难以选择")
        return {
            "ok": not problems,
            "problems": problems,
            "categories": unique,
            "message": "" if problems else f"已确认 {len(unique)} 个工作方向",
        }
