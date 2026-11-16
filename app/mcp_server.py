"""MCP stdio 服务器（Spec-Driven）—— 让 AI Agent 通过 MCP 协议查询工作记录。

Spec
----
把 :mod:`mcp_tools` 的只读工具集，通过 MCP（Model Context Protocol）的
**stdio 传输**暴露给 AI Agent。Agent 侧只需在 ``mcpServers`` 里配一条命令，
即可直接调用「查记录」「看日报」「算花费」等工具。

为什么是独立进程
----------------
MCP 的 stdio 传输要求进程**独占标准输入输出** —— 所有 JSON-RPC 消息
都走 stdin/stdout。而诚实日报助手的主进程正在跑抓屏循环：
它必须独占屏幕、日志和 SQLite 连接。两者的生命周期本就不同。

所以 MCP server 是**按需启动的独立进程**：Agent 需要查数据时拉起它，
用完退出。

好处是彻底隔离 —— MCP 进程崩了不影响抓屏；抓屏跑一天也不占 MCP 的资源。

为什么不用 HTTP
--------------
HTTP 版本需要 SSE 支持（Streamable HTTP transport），而本项目已有的是
「简单 JSON 响应」的 HTTP 服务。stdio 是 MCP 的**原始标准传输**，
实现最直接，且天然有进程级隔离 ——
**能启动这个进程的人，就是授权边界**（本地场景下这比 Bearer Token 更实在）。

stdout 是协议通道，绝对不能被污染
---------------------------------
这是实现 stdio 传输时**最容易踩的坑**：协议里每一条响应都是一行 JSON，
任何多出来的 ``print()``、日志、警告都会让 Agent 的解析器直接失败。

所以本模块做了三件事：
1. 启动时立刻把 ``sys.stdout`` 重定向到 ``sys.stderr``；
2. 协议输出走独立的 ``sys.__stdout__``，不经过 ``sys.stdout``；
3. 只往 stderr 写日志（不与主程序共用日志文件 —— 两个进程同时轮转
   同一个文件会冲突）。

编码必须是 UTF-8
---------------
Windows 上 ``sys.__stdout__`` / ``sys.__stdin__`` 的编码是**系统 ANSI 代码页**
（简体中文环境是 GBK / cp936），而 MCP 协议规定用 UTF-8。

不显式指定编码的后果：
* stdout：中文按 GBK 写出去，Agent 按 UTF-8 解析直接失败；
* stdin：Agent 发来的 UTF-8 中文被按 GBK 读成乱码，``json.loads`` 报错。

这个坑很隐蔽 —— **纯 ASCII 请求一切正常，只有出现中文才炸**。
所以这里显式用 UTF-8 重新包装输入输出流。

依赖：标准库（json / sys / logging）+ 本项目的 mcp_tools 模块。
"""

import io
import json
import logging
import sys
from typing import Any, Dict, Optional, TextIO

import mcp_tools
from mcp_tools import ToolContext, ToolError

# MCP 协议版本。客户端在 initialize 里报自己的版本，
# 我们宽松协商：能对上就对上，对不上也继续（而不是直接握手失败）。
PROTOCOL_VERSION = "2024-11-05"

SERVER_NAME = "honest-daily-helper"
SERVER_VERSION = "2.2.0"

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

log = logging.getLogger("honest.mcp")


# --------------------------------------------------------------------------
# 协议层（与 stdio 解耦，便于测试）
# --------------------------------------------------------------------------
class MCPProtocol:
    """处理 MCP 的 JSON-RPC 消息。

    与 I/O 完全解耦：``handle()`` 输入一个已解析的 dict，
    返回要发回的 dict（或 None 表示这是通知、不该回响应）。
    这样协议逻辑可以脱离开子进程直接测。
    """

    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self.initialized = False
        self.client_info: Dict[str, Any] = {}

    # ---------- 主入口 ----------
    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        """处理一条消息。

        :param message: 已解析的 JSON-RPC 消息（dict）
        :return: 响应 dict；通知（无 id）返回 None
        """
        if not isinstance(message, dict):
            return _error_response(None, INVALID_REQUEST,
                                   "Invalid Request: 消息必须是 JSON 对象")

        msg_id = message.get("id")
        method = message.get("method")

        if not isinstance(method, str) or not method:
            return _error_response(msg_id, INVALID_REQUEST,
                                   "Invalid Request: 缺少 method 字段")

        # 通知（没有 id）不该有响应
        is_notification = "id" not in message

        try:
            result = self._dispatch(method, message.get("params") or {})
        except KeyError:
            if is_notification:
                return None
            return _error_response(msg_id, METHOD_NOT_FOUND,
                                   f"Method not found: {method}")
        except ToolError as exc:
            # 工具的参数错误 —— 转成 isError 结果，让 Agent 能自我纠正
            if is_notification:
                return None
            return _tool_error_response(msg_id, str(exc))
        except Exception as exc:  # noqa: BLE001 —— 兜底，不能让进程崩
            log.exception("处理 %s 时异常", method)
            if is_notification:
                return None
            return _error_response(msg_id, INTERNAL_ERROR,
                                   f"Internal error: {type(exc).__name__}: {exc}")

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    # ---------- 方法分发 ----------
    def _dispatch(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method in ("notifications/initialized", "initialized"):
            self.initialized = True
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": mcp_tools.list_tools()}
        if method == "tools/call":
            return self._tools_call(params)
        # 资源与提示词是 MCP 的可选能力 —— 本项目不提供，
        # 但要**明确返回空列表**而不是「方法不存在」：
        # 有些客户端会固定探测这两个方法，报错会让它们以为服务坏了。
        if method == "resources/list":
            return {"resources": []}
        if method == "prompts/list":
            return {"prompts": []}
        raise KeyError(method)

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """握手：协商协议版本，声明能力。"""
        requested = params.get("protocolVersion")
        # 宽松协商：客户端报什么版本就回什么版本。
        # 严格校验版本会让新旧客户端直接握手失败 —— 对本地工具来说，
        # 继续跑比拒绝服务更划算。
        version = requested if isinstance(requested, str) and requested else PROTOCOL_VERSION
        self.client_info = params.get("clientInfo") or {}
        log.info("MCP 客户端已连接: %s (协议 %s)",
                 self.client_info.get("name", "?"), version)
        return {
            "protocolVersion": version,
            "capabilities": {
                # 只声明 tools —— 本项目不提供 resources / prompts
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
            "instructions": (
                "本工具集是**只读**的：可以查工作记录、看日报、算 API 花费。\n"
                "不会写库、不写文件、不调 LLM（因此日报只有模板版，无 AI 分析）。\n"
                "列表类工具默认最多返回 200 条 —— 返回里的 truncated 字段为 true "
                "表示还有更多，用 offset 继续翻页。"
            ),
        }

    def _tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """调用工具。

        注意：工具执行失败**不抛 JSON-RPC error**，而是返回
        ``isError: true`` 的结果。这是 MCP 与通用 JSON-RPC 的关键差异 ——
        工具失败要让 Agent **看得见并能自我纠正**，
        而不是收到一个它无法理解的协议层错误。
        """
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ToolError("tools/call 缺少 name 参数")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ToolError(
                f"arguments 必须是对象，收到 {type(arguments).__name__}")

        try:
            payload = mcp_tools.call_tool(self.ctx, name, arguments)
        except KeyError:
            raise ToolError(
                f"没有名为 {name!r} 的工具。可用工具："
                f"{', '.join(mcp_tools.tool_names())}"
            ) from None

        return {
            "content": [{"type": "text", "text": _format_result(payload)}],
            "structuredContent": payload,
            "isError": False,
        }


def _format_result(payload: Dict[str, Any]) -> str:
    """把结果信封格式化成给 Agent 看的文本。

    用 JSON 而不是 repr —— Agent 读 JSON 更准，也便于它二次解析。
    ``ensure_ascii=False`` 让中文保持可读（不是 \\uXXXX 转义）。
    """
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _error_response(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }


def _tool_error_response(msg_id: Any, message: str) -> Dict[str, Any]:
    """工具级失败 —— 走 result + isError，不是 JSON-RPC error。"""
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        },
    }


# --------------------------------------------------------------------------
# stdio 传输层
# --------------------------------------------------------------------------
def serve(stdin: TextIO, stdout: TextIO, protocol: MCPProtocol) -> int:
    """stdio 主循环：读一行 JSON，处理，写一行 JSON。

    :return: 退出码（0 = 正常结束）
    """
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            # 解析失败必须回 id=null 的错误 —— 客户端无从知道是哪条消息坏了
            response: Optional[Dict[str, Any]] = _error_response(
                None, PARSE_ERROR, f"Parse error: {exc}")
        else:
            response = protocol.handle(message)

        if response is None:
            continue  # 通知，不回响应
        stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        stdout.flush()  # 必须 flush —— 管道是缓冲的，不 flush 客户端会一直等
    return 0


def protect_stdout() -> TextIO:
    """保护协议通道：把 ``sys.stdout`` 重定向到 stderr。

    :return: 协议专用的输出流（UTF-8 编码）
    """
    protocol_out = _utf8_stdout()
    try:
        sys.stdout = sys.stderr
    except Exception:
        pass
    return protocol_out


def _utf8_stdout() -> TextIO:
    """取一个**强制 UTF-8** 的协议输出流。

    为什么不能直接用 ``sys.__stdout__``
    --------------------------------
    Windows 上 ``sys.__stdout__`` 的编码是**系统 ANSI 代码页**（简体中文
    环境是 GBK / cp936），不是 UTF-8。而协议里的中文（工具描述、instructions、
    返回内容）会被按 GBK 编码写出去 —— Agent 按 UTF-8 解析时直接失败，
    表现为「每次调用都报 JSON 解析错误」。

    这个坑很隐蔽：ASCII 内容完全正常，只有出现中文才炸。
    所以这里显式用 UTF-8 重新包装输出流，并让 errors="replace" 兜底
    （宁可替换成 �，也不能让整个进程因编码错误崩掉）。

    实现要点：``sys.__stdout__`` 是文本流，要重新编码必须拿它底层的
    二进制流 ``sys.__stdout__.buffer``。
    """
    try:
        raw = sys.__stdout__.buffer
    except Exception:
        return sys.__stdout__
    try:
        return io.TextIOWrapper(raw, encoding="utf-8", errors="replace",
                                line_buffering=True)
    except Exception:
        return sys.__stdout__


def _utf8_stdin() -> TextIO:
    """取一个**强制 UTF-8** 的输入流。

    与 stdout 同理：Windows 上 ``sys.stdin`` 的编码是系统 ANSI 代码页
    （cp936），而 Agent 发来的 JSON 一定是 UTF-8。中文会被读成乱码，
    导致 ``json.loads`` 直接失败。

    这是 stdio 传输的另一个隐蔽坑 —— 纯 ASCII 请求一切正常，
    只有请求里带中文才炸。

    实现要点：``sys.__stdin__`` 已经是**文本流**，不能直接再包一层
    ``TextIOWrapper``（那会要求它返回 bytes，报
    "underlying read() should have returned a bytes-like object"）。
    要重新编码必须拿到它底层的二进制流 ``sys.__stdin__.buffer``。
    """
    try:
        raw = sys.__stdin__.buffer
    except Exception:
        return sys.__stdin
    try:
        return io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
    except Exception:
        return sys.__stdin


def setup_logging() -> None:
    """只往 stderr 写日志。

    刻意**不**用 ``logging_setup.setup_logging()``：那个模块会往
    ``sys.stdout`` 挂 handler（会污染协议通道），且与主程序共用
    ``honest-helper.log`` —— 两个进程同时轮转同一个文件会冲突。
    """
    logger = logging.getLogger("honest.mcp")
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "[MCP] %(levelname)s %(message)s"))
        logger.addHandler(handler)


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def build_context(config_path: Optional[str] = None,
                  db_path: Optional[str] = None) -> ToolContext:
    """构造只读上下文。

    配置解析与主程序保持一致（``Config`` 默认读 ``config.json``），
    这样 MCP 和主程序看的是同一个库、同一套单价。
    """
    from config import Config

    cfg = Config(config_path)
    resolved_db = db_path or cfg.get("storage", "db_path", default="records.db")
    archive_dir = cfg.get("storage", "archive_dir", default="outputs/reports")

    from archive import ReportArchive
    from store import RecordStore

    store = RecordStore(resolved_db)
    return ToolContext(
        store,
        ReportArchive(archive_dir),
        db_path=resolved_db,
        price_input=float(cfg.get("llm", "price_per_1m_input", default=0.0) or 0.0),
        price_output=float(cfg.get("llm", "price_per_1m_output", default=0.0) or 0.0),
    )


def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="诚实日报助手 MCP 服务器（stdio 传输，只读）")
    parser.add_argument("--config", default=None,
                        help="配置文件路径（默认 ./config.json）")
    parser.add_argument("--db", default=None,
                        help="数据库路径（默认取配置里的 storage.db_path）")
    args = parser.parse_args(argv)

    # 顺序很重要：先保护 stdout，再做任何可能输出到 stdout 的事
    protocol_out = protect_stdout()
    setup_logging()

    try:
        ctx = build_context(args.config, args.db)
    except Exception as exc:
        print(f"[MCP] 无法初始化数据源: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        print("[MCP] 请确认诚实日报助手已运行过，或用 --db 指定数据库路径。",
              file=sys.stderr)
        return 1

    protocol = MCPProtocol(ctx)
    try:
        return serve(_utf8_stdin(), protocol_out, protocol)
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            ctx.store._store.close()  # noqa: SLF001 —— 关闭底层连接
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
