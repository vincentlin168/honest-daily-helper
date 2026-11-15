"""统一 HTTP 客户端工厂（Spec-Driven）。

Spec
----
全项目所有出网请求（视觉 LLM 识别、日报 AI 分析、数据上报）统一从这里创建客户端，
解决同一类反复出现的故障：**系统代理把本机/直连请求劫持走**。

问题背景
--------
httpx 默认 ``trust_env=True``，会读取 Windows 注册表的 Internet Settings 代理
（``HKCU\\...\\Internet Settings\\ProxyEnable`` / ``ProxyServer``）。
当机器上开着 Clash/V2Ray 等系统代理时：

* ``upload.py`` 上报到 ``127.0.0.1:9`` 被代理转发 → 收到 **502**（而非预期的连接失败）
* ``main.py`` 的日报 AI 分析直连官方 API 被代理转发 → 静默失败回退本地模板
* 本地 Skill 服务（``127.0.0.1:8088``）被代理转发 → Web UI 整页加载失败

设计要点
--------
1. **默认不信任环境代理**（``trust_env=False``）：本项目主张「AI 直连官方、链路透明」，
   不应该被一个来路不明的系统代理静默改道。故障要**显式**发生，而不是变成 502。
2. **仍可显式开启**：确有需要走代理访问 API 的用户，可在配置里打开
   ``llm.use_system_proxy``（``true`` 恢复 httpx 默认行为）。
3. **单一事实来源**：超时、重试、代理策略只在这里定义，避免各模块各写一套。

配置项
------
    llm.use_system_proxy      视觉 LLM / 日报 AI 分析是否走系统代理（默认 false）
    upload.use_system_proxy   数据上报是否走系统代理（默认 false）

依赖：httpx。
"""

from typing import Any, Optional

import httpx

# 默认超时：视觉 LLM 延迟远高于普通 HTTP 调用，由调用方按场景覆盖
DEFAULT_TIMEOUT = 30.0

# 长耗时场景（视觉识别、日报生成）专用
LONG_TIMEOUT = 120.0


def make_client(timeout: float = DEFAULT_TIMEOUT,
                use_system_proxy: bool = False,
                **kwargs: Any) -> httpx.Client:
    """创建统一配置的 httpx 客户端。

    :param timeout: 超时秒数。
    :param use_system_proxy: 是否读取系统/环境代理。默认 False（直连，链路透明）。
    :param kwargs: 透传给 httpx.Client（如 headers、limits）。
    """
    return httpx.Client(timeout=timeout, trust_env=use_system_proxy, **kwargs)


def client_from_config(config: Any, timeout: float = DEFAULT_TIMEOUT,
                       section: str = "llm") -> httpx.Client:
    """按配置里的 ``<section>.use_system_proxy`` 创建客户端。

    :param config: Config 实例（有 ``get(*keys)`` 接口）。
    :param timeout: 超时秒数。
    :param section: 配置段名（``llm`` / ``upload``）。
    """
    use_proxy = bool(config.get(section, "use_system_proxy", default=False))
    return make_client(timeout=timeout, use_system_proxy=use_proxy)
