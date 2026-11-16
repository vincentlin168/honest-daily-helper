"""视觉 LLM 识别客户端（Spec-Driven）。

Spec
----
把截图发给视觉大模型（DeepSeek-flash 等 OpenAI 兼容接口），让模型理解画面并输出
「工作内容 + 分类」，这是「诚实日报助手」的核心识别层。

设计要点：
1. **单一抽象**：任意 OpenAI `/chat/completions` 兼容服务都能接入，换模型只改配置。
2. **图片 base64 内联**：截图以 `data:image/jpeg;base64,...` 传入，不出本机依赖图床。
3. **结构化输出**：system prompt 要求模型返回 JSON（content + category），程序直接消费。
4. **prompt 源自实践**：识别 prompt 为日报导向 + 脱敏设计，已提炼为内置模板。
   是「日报导向 + 脱敏」的设计。
5. **错误可诊断**：空内容、纯文本模型（没看到图）给出明确提示。
6. **多认证方式**：支持 Bearer（API Key）、Basic（用户名 + 密码）、无鉴权三种，
   并可从 URL 内嵌的 ``user:pass@`` 自动识别。认证头由
   ``_request_headers()`` 单一生成，图片识别 / 连通性测试 / 日报 AI 分析
   三条路径共用，杜绝「一处硬编码 Bearer、另一处漏支持」的老问题。

认证方式
--------
============  ==========================================  ==================
配置          含义                                        生成的请求头
============  ==========================================  ==================
``bearer``    用 ``api_key`` 作 Bearer Token              ``Authorization: Bearer <key>``
``basic``     用 ``api_username`` / ``api_password``      ``Authorization: Basic <base64>``
``none``      不带任何认证头                              （无）
``""``（自动）  URL 内嵌 ``user:pass@`` 或填了用户名密码 -> Basic；            见下
              否则有 ``api_key`` -> Bearer；都没有 -> 无
============  ==========================================  ==================

**URL 内嵌凭据**：很多私有化部署 / 中转网关的文档给的就是
``https://user:pass@host/v1`` 这种形式。``_split_url_credentials()``
会把它拆成 (清洗后的 URL, 用户名, 密码)，使凭据**不会残留在 api_base 里**
—— 否则密码会随 URL 出现在日志、错误信息或调用栈里。

依赖：httpx。
"""

import base64
import json
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx

from http_client import make_client

# 默认工作方向分类（用户可在设置里自定义）
DEFAULT_CATEGORIES = [
    "产品设计", "代码开发", "运营分析", "数据复盘",
    "客户支持", "项目管理", "日报整理", "资料研究", "其他",
]

# 认证方式取值
AUTH_AUTO = ""       # 自动判定（默认）
AUTH_BEARER = "bearer"
AUTH_BASIC = "basic"
AUTH_NONE = "none"


def build_system_prompt(categories: Optional[list] = None) -> str:
    """生成识别 prompt（日报导向 + 脱敏设计，分类列表可动态注入）。"""
    cats = categories or DEFAULT_CATEGORIES
    cat_str = "、".join(cats)
    return f"""根据当前电脑屏幕截图，记录此刻正在进行的工作活动。

核心目标：
1. 尽可能记录有日报价值的工作内容
2. 保留工作主题、任务名称、项目方向、需求内容、技术问题、交付物、进展、待办
3. 保护隐私，脱敏身份信息和敏感字段，工作任务本身要尽量保留

你必须只输出一个 JSON 对象，不要输出任何其他文字，格式如下：
{{"activity": "简短活动标签", "content": "当前具体工作任务描述", "category": "工作方向分类"}}

其中 activity 是一个**稳定的活动主题标签**（概括当前工作主题，不超过 15 字，
例如「开发日报助手工具」「撰写项目方案」，不要包含易变的细节动作），
用于把连续相同的工作聚合成一段。

工作方向分类（category 必须是以下之一）：
{cat_str}

你应该记录：
1. 当前正在做的具体工作任务
2. 当前正在阅读、编辑、核对、调试、整理、沟通的内容主题
3. 当前任务属于哪个工作方向
4. 能用于日报的具体进展
5. 聊天工具里处理工作，只提取工作事项和任务含义，不复述聊天原文

你不应该记录（脱敏要求）：
1. 不描述桌面壁纸、系统状态栏、时间、电量、天气等无关信息
2. 不输出联系人昵称、群名、账号名
3. 不输出手机号、邮箱、身份证、银行卡、地址、验证码、密码、Token、API Key、Cookie
4. 不输出客户/员工个人身份、薪酬绩效、敏感财务明细
5. 私人闲聊只输出「当前包含私人沟通内容，已脱敏」"""


def _split_url_credentials(api_base: str) -> Tuple[str, str, str]:
    """把 URL 里内嵌的 ``user:pass@`` 拆出来。

    支持 ``https://user:password@host/v1`` 这种「URL + 密码」的写法 —— 很多
    私有化部署 / 中转网关的文档给的就是这种形式。

    :return: (清洗后的 URL, 用户名, 密码)
    """
    raw = (api_base or "").strip()
    if not raw:
        return "", "", ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.rstrip("/"), "", ""

    if "@" not in parts.netloc:
        return raw.rstrip("/"), "", ""

    # 只切 netloc 里 @ 之前的部分，保留 host:port
    userinfo, _, hostport = parts.netloc.rpartition("@")
    username, _, password = userinfo.partition(":")
    clean_netloc = hostport
    clean = urlunsplit((parts.scheme, clean_netloc, parts.path,
                        parts.query, parts.fragment))
    return clean.rstrip("/"), username, password


class LLMClient:
    """视觉大模型客户端（OpenAI 兼容协议）。"""
    REQUEST_TIMEOUT = 120.0  # 视觉模型延迟高于纯文本，放宽超时

    # 图片识别的 token 预算。
    # 实测 deepseek-flash 的思维链常消耗 700-1100 token；原 1024 会让预算被
    # 思维链吃光 -> content 为空、finish_reason='length'，表现为「识别失败」。
    # 2048 实测稳定（思维链 736 token 时 finish_reason='stop'）。
    MAX_TOKENS = 2048

    # 连通性测试的 token 预算（纯文本，思维链同样要占预算）
    MAX_TOKENS_TEXT = 512

    def __init__(self, api_base: str, api_key: str, model: str,
                 system_prompt: Optional[str] = None,
                 categories: Optional[list] = None,
                 use_system_proxy: bool = False,
                 auth_type: str = AUTH_AUTO,
                 api_username: str = "",
                 api_password: str = ""):
        # URL 里可能内嵌 user:pass@ —— 先剥离出来，避免凭据随 URL 出现在日志里
        self.api_base, embedded_user, embedded_pass = _split_url_credentials(api_base)
        self.api_key = api_key or ""
        self.model = model or "deepseek-flash"
        self.use_system_proxy = bool(use_system_proxy)

        # basic 认证的用户名/密码：显式配置优先于 URL 内嵌
        self.api_username = api_username or embedded_user or ""
        self.api_password = api_password or embedded_pass or ""
        self.auth_type = (auth_type or AUTH_AUTO).strip().lower()

        # 自定义 system_prompt 优先，否则按分类动态生成
        self.system_prompt = system_prompt or build_system_prompt(categories)

    # ---------- 认证 ----------
    def _resolved_auth_type(self) -> str:
        """确定实际使用的认证方式。

        显式指定 > URL 内嵌凭据 > 有 api_key 时用 Bearer > 否则不带认证头。
        """
        if self.auth_type in (AUTH_BEARER, AUTH_BASIC, AUTH_NONE):
            return self.auth_type
        # 自动：有 user:pass@ -> basic；否则有 api_key -> bearer；都没有 -> none
        if self.api_username or self.api_password:
            return AUTH_BASIC
        if self.api_key:
            return AUTH_BEARER
        return AUTH_NONE

    def _auth_headers(self) -> Dict[str, str]:
        """构造认证请求头。"""
        mode = self._resolved_auth_type()
        if mode == AUTH_BASIC:
            raw = f"{self.api_username}:{self.api_password}".encode("utf-8")
            return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        if mode == AUTH_BEARER and self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    def _request_headers(self) -> Dict[str, str]:
        """完整请求头（认证 + JSON）。"""
        headers = {"Content-Type": "application/json"}
        headers.update(self._auth_headers())
        return headers

    def _build_messages(self, image_base64: str) -> list:
        """构造 OpenAI 多模态消息体。图片只能放 user 消息的 content 数组。"""
        if not image_base64:
            raise ValueError("截图为空，未发送请求")
        return [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请识别这张截图中的工作内容，输出 JSON。"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_base64}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ]

    def chat_with_image(self, image_base64: str) -> Tuple[str, Dict[str, int]]:
        """发送截图给模型，返回 (文本结果, usage)。空内容自动重试一次。"""
        if not image_base64:
            raise ValueError("截图为空，未发送请求")

        last_error = None
        for attempt in range(2):  # 空内容时重试一次
            payload = {
                "model": self.model,
                "messages": self._build_messages(image_base64),
                "temperature": 0.2,
                "max_tokens": self.MAX_TOKENS,
            }
            try:
                with make_client(timeout=self.REQUEST_TIMEOUT,
                                 use_system_proxy=self.use_system_proxy) as client:
                    response = client.post(
                        f"{self.api_base}/chat/completions",
                        headers=self._request_headers(),
                        json=payload,
                    )
                if response.status_code != 200:
                    raise RuntimeError(f"模型服务返回错误 ({response.status_code}): {response.text[:300]}")
                data = response.json()
                text = self._extract_text(data)
                return text, self._extract_usage(data)
            except RuntimeError as exc:
                last_error = exc
                # 空内容/格式异常可能是偶发，重试一次
                continue
        raise last_error if last_error else RuntimeError("模型调用失败")

    # ---------- 解析 ----------
    @staticmethod
    def _extract_text(payload: Any) -> str:
        """从响应里取出模型输出。

        优先 ``content``；若为空则回退 ``reasoning_content``（推理模型的思维链）。

        为什么需要回退
        --------------
        deepseek-flash 这类推理模型会先输出思维链。实测：思维链常消耗 700-1100
        token，若 ``max_tokens`` 给得太小（如 1024），预算会被思维链吃光 →
        ``content`` 为空、``finish_reason='length'`` → 被误判成「模型不支持图片」
        或「Key 有问题」。

        这与 ``test_connection`` 是同一个 bug 家族：纯文本路径早已修好，
        图片路径当时漏了。图片识别失败率约 3.5%（650 次调用中 23 次）。
        """
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("模型响应格式异常，未取到内容")

        for field in ("content", "reasoning_content"):
            raw = message.get(field)
            if isinstance(raw, list):
                raw = "".join(item.get("text", "") for item in raw if isinstance(item, dict))
            if isinstance(raw, str) and raw.strip():
                return raw.strip()

        # 两个字段都空 —— 再看一眼 finish_reason，给出可诊断的原因
        finish = ""
        try:
            finish = payload["choices"][0].get("finish_reason", "")
        except Exception:
            pass
        if finish == "length":
            raise RuntimeError(
                "模型输出被 max_tokens 截断（思维链吃光了预算）。"
                "请调大 max_tokens 后重试"
            )
        raise RuntimeError("模型返回了空内容（可能是纯文本模型无法处理图片，或 Key/额度问题）")

    @staticmethod
    def _extract_usage(payload: Any) -> Dict[str, int]:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return {}
        def pick(*names):
            for k in names:
                v = usage.get(k)
                if isinstance(v, (int, float)):
                    return int(v)
            return 0
        prompt = pick("prompt_tokens", "input_tokens")
        completion = pick("completion_tokens", "output_tokens")
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": pick("total_tokens") or (prompt + completion),
        }

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """剥离 markdown 代码块围栏（```json ... ``` 或 ``` ... ```）。

        注意：不能简单用 ``^```(?:json)?\\s*`` —— 该正则在多行模式下 ``\\s*`` 会贪婪吃掉
        换行，且当围栏内 JSON 非法时无法回退。这里改为定位围栏起止标记后精确切片。
        """
        s = text.strip()
        fence = re.match(r"^```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n?(.*?)\r?\n?[ \t]*```$",
                         s, flags=re.DOTALL)
        if fence:
            return fence.group(2).strip()
        # 未闭合的围栏（模型输出被截断）：剥掉开头围栏行
        s2 = re.sub(r"^```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n?", "", s)
        return s2.strip()

    @staticmethod
    def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
        """从任意文本中提取第一个合法 JSON 对象。

        依次尝试：整体解析 → 括号配平扫描（不依赖正则贪婪匹配）。
        括号配平能正确跳过字符串字面量里的 ``{`` ``}`` 与转义引号。
        """
        candidates = []
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, TypeError):
            pass

        # 括号配平扫描：找到第一个能配平的 {...}
        start = text.find("{")
        while start != -1:
            depth = 0
            in_string = False
            escaped = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_string:
                    if escaped:
                        escaped = False
                    elif ch == "\\":
                        escaped = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[start:i + 1])
                        break
            start = text.find("{", start + 1)

        for cand in candidates:
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _repair_json(text: str) -> Optional[Dict[str, Any]]:
        """修复 JSON 中字符串字面量里的裸换行/裸回车（模型常见错误）。

        逐字符扫描并跟踪「是否在字符串内」，只把字符串内部的裸控制字符转义，
        结构性的换行（键值之间的换行）保持原样。
        """
        out = []
        in_string = False
        escaped = False
        changed = False
        for ch in text:
            if in_string:
                if escaped:
                    out.append(ch)
                    escaped = False
                    continue
                if ch == "\\":
                    out.append(ch)
                    escaped = True
                    continue
                if ch == '"':
                    out.append(ch)
                    in_string = False
                    continue
                if ch == "\n":
                    out.append("\\n")
                    changed = True
                    continue
                if ch == "\r":
                    out.append("\\r")
                    changed = True
                    continue
                out.append(ch)
                continue
            # 字符串外
            if ch == '"':
                in_string = True
            out.append(ch)

        if not changed:
            return None
        try:
            obj = json.loads("".join(out))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def parse_recognition(text: str) -> Dict[str, str]:
        """从模型输出中解析 activity + content + category（容错处理各种 JSON 形态）。"""
        raw = (text or "").strip()
        fallback = {
            "activity": raw[:15],
            "content": raw,
            "category": "其他",
        }
        if not raw:
            return fallback

        cleaned = LLMClient._strip_code_fence(raw)
        obj = LLMClient._extract_json_object(cleaned)
        if obj is None:
            obj = LLMClient._repair_json(cleaned)
        if obj is None:
            # 最后再试一次原文（围栏内可能还有嵌套 JSON）
            obj = LLMClient._extract_json_object(raw)
        if obj is None:
            return fallback

        content = obj.get("content") or obj.get("描述") or ""
        category = obj.get("category") or obj.get("分类") or "其他"
        activity = obj.get("activity") or obj.get("活动") or ""
        if not activity:
            activity = content[:15]  # fallback：用 content 前 15 字
        return {
            "activity": str(activity).strip(),
            "content": str(content).strip(),
            "category": str(category).strip(),
        }

    def chat_text(self, prompt: str, max_tokens: Optional[int] = None,
                  temperature: float = 0.3) -> str:
        """纯文本对话（不带图片），返回模型输出文本。

        供日报 AI 分析复用 —— 走同一套认证逻辑（Bearer / Basic / URL 内嵌凭据），
        避免在别处再写一份硬编码 ``Authorization: Bearer`` 而漏掉其它认证方式。
        """
        with make_client(timeout=self.REQUEST_TIMEOUT,
                         use_system_proxy=self.use_system_proxy) as client:
            response = client.post(
                f"{self.api_base}/chat/completions",
                headers=self._request_headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens or self.MAX_TOKENS_TEXT,
                },
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"模型服务返回错误 ({response.status_code}): {response.text[:200]}")
        return self._extract_text(response.json())

    def test_connection(self) -> Dict[str, Any]:
        """用纯文本探测消息测试连通性（不消耗图片 token）。

        注意：``max_tokens`` 必须给足。有些模型（如 deepseek-flash）会先输出
        ``reasoning_content``（思维链），若预算太小会被思维链吃光，导致
        ``content`` 为空、``finish_reason='length'`` —— 被误判成「连接失败」。
        """
        try:
            with make_client(timeout=15.0, use_system_proxy=self.use_system_proxy) as client:
                response = client.post(
                    f"{self.api_base}/chat/completions",
                    headers=self._request_headers(),
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": "请只回复四个字：连接成功"}],
                        "max_tokens": self.MAX_TOKENS_TEXT,
                    },
                )
            if response.status_code == 200:
                payload = response.json()
                text = self._extract_reply(payload)
                if text:
                    return {"ok": True, "reply": text}
                return {"ok": False, "error": "模型返回空内容（可能被 max_tokens 截断，可调大后重试）"}
            return {"ok": False, "error": f"[{response.status_code}] {response.text[:200]}"}
        except httpx.TimeoutException:
            return {"ok": False, "error": "连接超时"}
        except httpx.RequestError as exc:
            return {"ok": False, "error": f"连接失败：{exc}"}

    @staticmethod
    def _extract_reply(payload: Any) -> str:
        """从响应里取出可读文本。

        优先 ``content``；若为空则回退 ``reasoning_content``（推理模型的思维链）。
        """
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return ""
        for field in ("content", "reasoning_content"):
            raw = message.get(field)
            if isinstance(raw, list):
                raw = "".join(item.get("text", "") for item in raw if isinstance(item, dict))
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        return ""
