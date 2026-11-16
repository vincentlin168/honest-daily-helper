"""MCP 工具集（Spec-Driven）—— 供 AI Agent 只读查询工作记录。

Spec
----
把「诚实日报助手」的本地记录，以 MCP（Model Context Protocol）工具的形式
暴露给 AI Agent，让 Agent 能直接回答「我今天做了什么」「这周花了多少 API 费用」
这类问题，而不需要人手动截图查看。

为什么叫「诚实工具集」
----------------------
MCP 让 AI 直接读你的工作记录。这件事天然让人不安 ——
「我的屏幕内容会不会被 AI 拿走？」

所以本模块的设计原则是：**能读的都读，但读不到不该读的**。

1. **只读，且结构上只读** —— 见 ``ReadOnlyStore``：写方法在类型层面就不可达。
   靠约定或文档说「请不要调用写方法」是不可靠的。
2. **无副作用** —— 不调 LLM（不花钱）、不写文件、不改配置。
   日报只出**模板版**（``ai_analysis=None``）——
   AI 分析由主程序的定时任务负责，MCP 侧保持零网络调用。
3. **数据量可控** —— 所有列表工具都有限制 + 分页，
   并在返回值里用 ``truncated`` / ``total`` 明确告知是否被截断。
   不隐瞒，是本项目一贯的原则：Agent 拿到截断的数据时能明确知道
   「这不是全部」，而不是误以为看到了全部记录。
4. **不碰密钥** —— 配置类信息一律不暴露，只暴露「服务状态」这类必要信息。

工具分组
--------
记录查询：get_entries / get_entries_range / get_entry / get_categories
统计汇总：get_overview / get_daily_stats / get_top_categories /
          get_heatmap / get_interval_effectiveness
日报相关：get_daily_report / list_reports / get_report_body
成本诊断：get_token_usage / get_cost / get_errors / get_error_summary
导出归档：export_data / get_status

依赖：标准库 + 本项目的 store / report / overview / archive / exporter 模块。
"""

import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import exporter
from archive import ReportArchive
from overview import build_overview, compute_cost
from report import aggregate_stats, build_report
from store import RecordStore

# 单次返回的默认条数上限 —— 防止把 Agent 的上下文撑爆
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
MAX_DAYS = 90

# 日期必须严格是 YYYY-MM-DD
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ToolError(Exception):
    """工具执行失败（参数错误、库不存在等）。

    会被协议层转成 ``isError: true`` 的 tool result ——
    MCP 与普通 JSON-RPC 的关键差异：工具失败要让 Agent **看得见**并能自我纠正，
    而不是收到一个协议层错误。
    """


# --------------------------------------------------------------------------
# 只读视图
# --------------------------------------------------------------------------
class ReadOnlyStore:
    """``RecordStore`` 的只读视图 —— 只转发白名单方法。

    为什么用包装而不是直接传 ``RecordStore``？
    因为「只读」必须是**结构性的**：调用方拿到的对象上根本不存在
    ``insert_entry`` / ``delete_entry`` / ``update_entry`` 这些方法。
    靠文档约定「请不要调用写方法」是不可靠的 ——
    Agent 会读错文档、会写错代码、会「顺手优化一下」。

    这也是本项目「诚实」理念在接口层的体现：
    **能力边界要写在类型里，而不是写在注释里。**
    """

    def __init__(self, store: RecordStore):
        self._store = store

    # ---------- entries（只读）----------
    def get_entry(self, entry_id: int) -> Optional[Dict[str, Any]]:
        return self._store.get_entry(entry_id)

    def entries_by_day(self, date_str: str) -> List[Dict[str, Any]]:
        return self._store.entries_by_day(date_str)

    def entries_in_range(self, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
        return self._store.entries_in_range(start_ts, end_ts)

    def entries_around(self, ts: int, window: int = 86400) -> List[Dict[str, Any]]:
        return self._store.entries_around(ts, window)

    def categories_in_use(self, date_str: Optional[str] = None) -> List[str]:
        return self._store.categories_in_use(date_str)

    def latest_entry(self) -> Optional[Dict[str, Any]]:
        return self._store.latest_entry()

    # ---------- 统计（只读）----------
    def hourly_histogram(self, days: int = 7) -> List[Dict[str, Any]]:
        return self._store.hourly_histogram(days)

    def token_usage_by_day(self, date_str: str) -> Dict[str, int]:
        return self._store.token_usage_by_day(date_str)

    def token_usage_range(self, start_ts: int, end_ts: int) -> Dict[str, int]:
        return self._store.token_usage_range(start_ts, end_ts)

    def interval_effectiveness(self, days: int = 7) -> Dict[str, Any]:
        return self._store.interval_effectiveness(days)

    # ---------- 诊断（只读）----------
    def recent_errors(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._store.recent_errors(limit)

    def error_summary(self, days: int = 7) -> Dict[str, Any]:
        return self._store.error_summary(days)

    # ---------- 只读元信息 ----------
    @property
    def db_path(self) -> str:
        return str(self._store.db_path)


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------
def _validate_date(date_str: str, field: str = "date") -> str:
    """校验 YYYY-MM-DD 日期。

    ``store`` 用 ``datetime.fromisoformat`` 解析，非法输入会抛 ValueError。
    但 ``fromisoformat`` 在 Python 3.11+ 接受紧凑格式（``20261116``）、
    也接受 ``2026-11-16T00:00`` 这种带时间的形式 —— 那些都不是「某一天」，
    混进来会让按天查询的语义变得含糊。

    所以这里先用正则严格限定 ``YYYY-MM-DD``，再交给 fromisoformat 兜底校验。
    把异常转成可读的 ToolError，让 Agent 能自我纠正。
    """
    if not isinstance(date_str, str) or not date_str.strip():
        raise ToolError(f"参数 {field} 缺失或为空，应为 YYYY-MM-DD 格式的日期")
    candidate = date_str.strip()
    if not _DATE_RE.fullmatch(candidate):
        raise ToolError(
            f"参数 {field} 格式非法：{candidate!r}，应为 YYYY-MM-DD"
            f"（例如 2026-11-16）"
        )
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        raise ToolError(
            f"参数 {field} 不是有效日期：{candidate!r}"
        ) from None
    return candidate


def _clamp_limit(limit: Optional[int]) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise ToolError(f"参数 limit 必须是整数，收到 {limit!r}") from None
    if value < 1:
        return 1
    return min(value, MAX_LIMIT)


def _clamp_days(days: Optional[int], default: int = 7) -> int:
    if days is None:
        return default
    try:
        value = int(days)
    except (TypeError, ValueError):
        raise ToolError(f"参数 days 必须是整数，收到 {days!r}") from None
    return max(1, min(MAX_DAYS, value))


def _paginate(items: List[Any], limit: int, offset: int = 0) -> Dict[str, Any]:
    """分页并诚实标注是否被截断。

    返回 ``{"items": [...], "total": N, "returned": M, "truncated": bool}``。

    ``truncated=True`` 意味着「还有更多没给你」——
    Agent 必须知道这一点，否则会误以为看到的就是全部。
    """
    total = len(items)
    start = max(0, int(offset or 0))
    window = items[start:start + limit]
    returned = len(window)
    return {
        "items": window,
        "total": total,
        "returned": returned,
        "offset": start,
        "truncated": start + returned < total,
    }


def _ok(data: Any, **extra: Any) -> Dict[str, Any]:
    """统一成功信封。"""
    payload: Dict[str, Any] = {"ok": True, "data": data}
    payload.update(extra)
    return payload


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# 工具上下文
# --------------------------------------------------------------------------
class ToolContext:
    """工具执行所需的上下文（只读资源）。"""

    def __init__(self, store: RecordStore, archive: Optional[ReportArchive] = None,
                 db_path: Optional[str] = None,
                 price_input: float = 0.0, price_output: float = 0.0):
        self.store = ReadOnlyStore(store)
        self.archive = archive or ReportArchive()
        self.db_path = db_path or store.db_path
        self.price_input = float(price_input or 0.0)
        self.price_output = float(price_output or 0.0)


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------
def _tool_get_entries(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    date = _validate_date(args.get("date") or _today())
    limit = _clamp_limit(args.get("limit"))
    offset = int(args.get("offset") or 0)
    entries = ctx.store.entries_by_day(date)
    page = _paginate(entries, limit, offset)
    return _ok(page, date=date)


def _tool_get_entries_range(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    start = _validate_date(args.get("start_date"), "start_date")
    end = _validate_date(args.get("end_date"), "end_date")
    if start > end:
        raise ToolError(f"start_date（{start}）不能晚于 end_date（{end}）")
    limit = _clamp_limit(args.get("limit"))
    offset = int(args.get("offset") or 0)

    start_ts = int(datetime.fromisoformat(start).timestamp())
    end_ts = int(datetime.fromisoformat(end).timestamp()) + 86400
    entries = ctx.store.entries_in_range(start_ts, end_ts)
    page = _paginate(entries, limit, offset)
    return _ok(page, start_date=start, end_date=end)


def _tool_get_entry(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    raw = args.get("id")
    try:
        entry_id = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"参数 id 必须是整数，收到 {raw!r}") from None
    entry = ctx.store.get_entry(entry_id)
    if entry is None:
        return _ok(None, found=False, message=f"没有 id={entry_id} 的记录")
    return _ok(entry, found=True)


def _tool_get_categories(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    date = args.get("date")
    if date:
        date = _validate_date(date)
    cats = ctx.store.categories_in_use(date)
    return _ok({"categories": cats, "date": date or "all"})


def _tool_get_overview(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    date = _validate_date(args.get("date") or _today())
    entries = ctx.store.entries_by_day(date)
    usage = ctx.store.token_usage_by_day(date)
    top = args.get("top_categories")
    top_n = 3 if top is None else max(1, min(MAX_LIMIT, int(top)))
    overview = build_overview(
        entries, usage,
        price_input=ctx.price_input,
        price_output=ctx.price_output,
        top_categories=top_n,
    )
    return _ok(overview, date=date)


def _tool_get_daily_stats(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    date = _validate_date(args.get("date") or _today())
    entries = ctx.store.entries_by_day(date)
    return _ok(aggregate_stats(entries), date=date)


def _tool_get_top_categories(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    date = _validate_date(args.get("date") or _today())
    raw_top = args.get("top")
    top = 3 if raw_top is None else max(1, min(MAX_LIMIT, int(raw_top)))
    entries = ctx.store.entries_by_day(date)
    overview = build_overview(entries, top_categories=top)
    cats = overview.get("top_categories", [])
    return _ok({"top_categories": cats, "date": date}, top=top)


def _tool_get_heatmap(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    days = _clamp_days(args.get("days"))
    return _ok({"heatmap": ctx.store.hourly_histogram(days)}, days=days)


def _tool_get_interval_effectiveness(ctx: ToolContext,
                                     args: Dict[str, Any]) -> Dict[str, Any]:
    days = _clamp_days(args.get("days"))
    return _ok(ctx.store.interval_effectiveness(days), days=days)


def _tool_get_daily_report(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """生成**模板版**日报（无 AI 分析）。

    AI 分析需要调 LLM —— 会花钱、有网络副作用。
    MCP 侧坚持「无副作用」，所以这里固定 ``ai_analysis=None``；
    AI 分析由主程序的定时任务负责。
    """
    date = _validate_date(args.get("date") or _today())
    entries = ctx.store.entries_by_day(date)
    report_md = build_report(entries, date, ai_analysis=None)
    return _ok({
        "date": date,
        "report_markdown": report_md,
        "ai_analysis": None,
        "note": "这是模板版日报（不含 AI 分析）。AI 分析由主程序定时任务生成。",
    }, date=date)


def _tool_list_reports(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    limit = _clamp_limit(args.get("limit"))
    items = ctx.archive.list_reports(limit)
    return _ok({"reports": items, "count": len(items)})


def _tool_get_report_body(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    date = _validate_date(args.get("date"))
    body = ctx.archive.load(date)
    if body is None:
        return _ok(None, found=False,
                   message=f"{date} 没有留档日报（可能尚未生成或已被删除）")
    return _ok({"date": date, "content": body}, found=True)


def _tool_get_token_usage(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    date = args.get("date")
    if date:
        date = _validate_date(date)
        usage = ctx.store.token_usage_by_day(date)
        scope = {"scope": "day", "date": date}
    else:
        days = _clamp_days(args.get("days"))
        end = int(datetime.now().timestamp())
        start = end - days * 86400
        usage = ctx.store.token_usage_range(start, end)
        scope = {"scope": "range", "days": days}
    return _ok(usage, **scope)


def _tool_get_cost(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    date = _validate_date(args.get("date") or _today())
    usage = ctx.store.token_usage_by_day(date)
    price_in = float(args.get("price_per_1m_input", ctx.price_input) or 0.0)
    price_out = float(args.get("price_per_1m_output", ctx.price_output) or 0.0)
    cost = compute_cost(usage, price_in, price_out)
    return _ok(cost, date=date,
               note=("未配置单价，只显示 token 数" if not cost.get("has_price")
                     else "金额按你填的单价换算"))


def _tool_get_errors(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    limit = _clamp_limit(args.get("limit"))
    errors = ctx.store.recent_errors(limit)
    return _ok({"errors": errors, "count": len(errors)})


def _tool_get_error_summary(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    days = _clamp_days(args.get("days"))
    return _ok(ctx.store.error_summary(days), days=days)


def _tool_export_data(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    """导出为内容字符串 —— **不写文件**。

    只读原则：写文件是副作用。Agent 拿到内容后自行决定怎么存。
    """
    fmt = str(args.get("format") or "json").lower()
    date = args.get("date")
    if date:
        date = _validate_date(date)
        entries = ctx.store.entries_by_day(date)
    else:
        end = int(datetime.now().timestamp())
        start = end - 90 * 86400
        entries = ctx.store.entries_in_range(start, end)

    if fmt == "json":
        meta = {"scope": date or "last_90_days", "tool": "诚实日报助手 MCP"}
        content = exporter.export_json(entries, meta)
    elif fmt == "csv":
        content = exporter.export_csv(entries)
    elif fmt in ("md", "markdown"):
        report_md = None
        if date:
            report_md = ctx.archive.load(date)
        content = exporter.export_markdown(entries, date, report_md)
    else:
        raise ToolError(
            f"不支持的导出格式 {fmt!r}，可用：json / csv / md(markdown)"
        )
    return _ok({
        "format": fmt,
        "scope": date or "last_90_days",
        "entry_count": len(entries),
        "content": content,
    })


def _tool_get_status(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    today = _today()
    entries = ctx.store.entries_by_day(today)
    latest = ctx.store.latest_entry()
    return _ok({
        "today": today,
        "today_entry_count": len(entries),
        "latest_entry_at": (latest or {}).get("started_at"),
        "db_path": ctx.db_path,
        "read_only": True,
        "note": "MCP 工具集是只读的：不写库、不写文件、不调 LLM。",
    })


# --------------------------------------------------------------------------
# 工具注册表
# --------------------------------------------------------------------------
class Tool:
    """一个工具的定义（名字、描述、schema、实现）。"""

    def __init__(self, name: str, description: str, schema: Dict[str, Any],
                 handler: Callable[[ToolContext, Dict[str, Any]], Dict[str, Any]]):
        self.name = name
        self.description = description
        self.schema = schema
        self.handler = handler

    def to_mcp(self) -> Dict[str, Any]:
        """转成 MCP ``tools/list`` 里的格式。"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


_DATE_PROP = {
    "type": "string",
    "description": "日期，格式 YYYY-MM-DD（本地日期）。省略则用今天。",
}
_LIMIT_PROP = {
    "type": "integer",
    "description": f"最多返回多少条，默认 {DEFAULT_LIMIT}，上限 {MAX_LIMIT}。",
    "minimum": 1,
    "maximum": MAX_LIMIT,
}
_OFFSET_PROP = {
    "type": "integer",
    "description": "跳过前多少条，用于分页。默认 0。",
    "minimum": 0,
}
_DAYS_PROP = {
    "type": "integer",
    "description": f"统计最近多少天，默认 7，上限 {MAX_DAYS}。",
    "minimum": 1,
    "maximum": MAX_DAYS,
}

# 说明文案的统一风格：**讲清楚什么时候用、有什么限制**。
# MCP 的 description 是 Agent 选工具的唯一依据，写得含糊就会误用。
TOOLS: List[Tool] = [
    # ---------- 记录查询 ----------
    Tool(
        "get_entries",
        "获取指定日期的工作活动记录（按时间段聚合，不是逐帧截图）。\n\n"
        "用途：想知道某天做了什么、每段时间在做什么。\n\n"
        "注意：结果默认最多 200 条，超出会被截断——"
        "返回的 truncated 字段为 true 时说明还有更多，用 offset 继续翻页。",
        _schema({"date": _DATE_PROP, "limit": _LIMIT_PROP, "offset": _OFFSET_PROP}),
        _tool_get_entries,
    ),
    Tool(
        "get_entries_range",
        "获取一段日期范围内的全部工作活动记录。\n\n"
        "用途：跨天分析（如「这一周我都在干什么」）。\n\n"
        "注意：范围可能很长，默认最多 200 条，用 offset 分页。",
        _schema({
            "start_date": {"type": "string", "description": "起始日期 YYYY-MM-DD。"},
            "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD。"},
            "limit": _LIMIT_PROP,
            "offset": _OFFSET_PROP,
        }, ["start_date", "end_date"]),
        _tool_get_entries_range,
    ),
    Tool(
        "get_entry",
        "按 id 获取单条工作记录。\n\n"
        "用途：从 get_entries 拿到 id 后看某一段的完整内容。\n\n"
        "注意：id 不存在时 found 为 false，不是报错。",
        _schema({"id": {"type": "integer", "description": "记录 id。"}}, ["id"]),
        _tool_get_entry,
    ),
    Tool(
        "get_categories",
        "获取已出现过的分类列表。\n\n"
        "用途：知道能按哪些维度筛数据（如「开发」「写作」「会议」）。\n\n"
        "注意：省略 date 则返回所有历史用过的分类。",
        _schema({"date": _DATE_PROP}),
        _tool_get_categories,
    ),

    # ---------- 统计汇总 ----------
    Tool(
        "get_overview",
        "获取某天的概览：总时长、最长连续工作段、分类占比、API 花费。\n\n"
        "用途：回答「今天干了多久活、花了多少钱」。\n\n"
        "注意：花费需要你在设置里填过单价，否则只给 token 数不给金额。",
        _schema({"date": _DATE_PROP, "top_categories": _LIMIT_PROP}),
        _tool_get_overview,
    ),
    Tool(
        "get_daily_stats",
        "获取某天的纯统计数字：段数、总时长、起止时间、分类分布、最长空档。\n\n"
        "用途：需要原始数字而不是人话总结时用这个（比 get_overview 更细）。",
        _schema({"date": _DATE_PROP}),
        _tool_get_daily_stats,
    ),
    Tool(
        "get_top_categories",
        "获取某天按时长排序的分类 Top N。\n\n"
        "用途：回答「今天主要在做什么」。",
        _schema({
            "date": _DATE_PROP,
            "top": {"type": "integer", "description": "取前几类，默认 3。", "minimum": 1},
        }),
        _tool_get_top_categories,
    ),
    Tool(
        "get_heatmap",
        "获取最近 N 天的「天 × 小时」活动时长热力图。\n\n"
        "用途：看出自己在什么时段最活跃、有没有熬夜或周末加班。\n\n"
        "注意：返回的是原始秒数，需要自己换算成小时。",
        _schema({"days": _DAYS_PROP}),
        _tool_get_heatmap,
    ),
    Tool(
        "get_interval_effectiveness",
        "实测「抓屏间隔 / 汇总周期」这两个设置到底有没有用。\n\n"
        "用途：想知道当前配置是否真的在工作，而不是拍脑袋设的参数。\n\n"
        "内容：实际识别间隔分布、时间线覆盖率、聚合压缩比。",
        _schema({"days": _DAYS_PROP}),
        _tool_get_interval_effectiveness,
    ),

    # ---------- 日报相关 ----------
    Tool(
        "get_daily_report",
        "生成某天的**模板版**日报（Markdown 文本）。\n\n"
        "用途：拿到可直接阅读的日报。\n\n"
        "注意：不含 AI 分析（AI 分析要调 LLM、会花钱，"
        "由主程序的定时任务负责）。要带 AI 分析的版本请用 get_report_body "
        "读主程序已留档的日报。",
        _schema({"date": _DATE_PROP}),
        _tool_get_daily_report,
    ),
    Tool(
        "list_reports",
        "列出已留档的日报（按日期倒序）。\n\n"
        "用途：知道哪些天的日报已经生成过。",
        _schema({"limit": _LIMIT_PROP}),
        _tool_list_reports,
    ),
    Tool(
        "get_report_body",
        "读取某天已留档日报的完整正文（含 AI 分析）。\n\n"
        "用途：拿主程序定时生成的高质量日报（含 AI 四段分析）。\n\n"
        "注意：该日期没有留档时 found 为 false。",
        _schema({"date": {
            "type": "string", "description": "日期 YYYY-MM-DD。"}}, ["date"]),
        _tool_get_report_body,
    ),

    # ---------- 成本与诊断 ----------
    Tool(
        "get_token_usage",
        "获取 token 用量与调用次数。\n\n"
        "用途：知道花了多少 token、失败了几次。\n\n"
        "注意：给了 date 则查当天，否则查最近 N 天。",
        _schema({"date": _DATE_PROP, "days": _DAYS_PROP}),
        _tool_get_token_usage,
    ),
    Tool(
        "get_cost",
        "按你填的单价算出某天的真实花费。\n\n"
        "用途：回答「今天这个工具花了多少钱」。\n\n"
        "注意：单价来自你的设置；也可以用参数临时指定。"
        "没填单价时只给 token 数，不会编造金额。",
        _schema({
            "date": _DATE_PROP,
            "price_per_1m_input": {
                "type": "number", "description": "每百万输入 token 单价（元）。"},
            "price_per_1m_output": {
                "type": "number", "description": "每百万输出 token 单价（元）。"},
        }),
        _tool_get_cost,
    ),
    Tool(
        "get_errors",
        "获取最近的失败调用记录（错误类型、时间、HTTP 状态）。\n\n"
        "用途：排查「为什么识别不出来了」。\n\n"
        "注意：包含错误原文，可能含接口地址等信息。",
        _schema({"limit": _LIMIT_PROP}),
        _tool_get_errors,
    ),
    Tool(
        "get_error_summary",
        "按错误类型聚合的失败统计（如 401 多少次、超时多少次）。\n\n"
        "用途：一眼看出主要故障源。",
        _schema({"days": _DAYS_PROP}),
        _tool_get_error_summary,
    ),

    # ---------- 导出与状态 ----------
    Tool(
        "export_data",
        "把工作记录导出为指定格式的**内容字符串**。\n\n"
        "用途：拿到可携带的数据（JSON 给程序分析 / CSV 给 Excel / Markdown 给人看）。\n\n"
        "注意：只返回内容，**不写文件**（写文件是副作用）。"
        "你自己决定存到哪。省略 date 则导出最近 90 天。",
        _schema({
            "format": {
                "type": "string",
                "description": "导出格式：json / csv / md。默认 json。",
                "enum": ["json", "csv", "md", "markdown"],
            },
            "date": _DATE_PROP,
        }),
        _tool_export_data,
    ),
    Tool(
        "get_status",
        "获取助手当前状态：今天的记录数、最后一条记录时间、数据库位置。\n\n"
        "用途：确认助手是否在正常工作、数据是否存在。",
        _schema({}),
        _tool_get_status,
    ),
]

TOOL_MAP: Dict[str, Tool] = {t.name: t for t in TOOLS}


def list_tools() -> List[Dict[str, Any]]:
    """返回 MCP ``tools/list`` 用的工具清单。"""
    return [t.to_mcp() for t in TOOLS]


def call_tool(ctx: ToolContext, name: str,
              arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """调用一个工具。

    :raises ToolError: 参数非法或工具执行失败（由协议层转成 isError 结果）
    :raises KeyError: 工具不存在（由协议层转成 JSON-RPC error）
    """
    tool = TOOL_MAP.get(name)
    if tool is None:
        raise KeyError(name)
    args = arguments or {}
    if not isinstance(args, dict):
        raise ToolError(f"工具 {name} 的 arguments 必须是对象，收到 {type(args).__name__}")
    return tool.handler(ctx, args)


def tool_names() -> List[str]:
    return [t.name for t in TOOLS]
