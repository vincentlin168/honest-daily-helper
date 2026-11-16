"""日报生成模块（Spec-Driven）。

Spec
----
基于一天的活动记录（entries）生成工作日报，采用「混合模式」：
统计模板（本地生成，永远可用）+ 可选 LLM 分析（AI 挂了自动回退）。

设计要点（日报模板 + AI 混合模式）：
1. **统计聚合**：段数、跨度、分类占比、集中时段、最长空档（纯本地计算）。
2. **模板话术**：用自然语言模板把统计转成可读日报。
3. **可选 AI 分析**：把统计喂给 LLM 生成「观察/证据/建议/小结」四段，失败回退本地模板。
4. **多语言中文优先**。

数据来源：store.entries_by_day(date) 返回的 entries 列表。
"""

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional


def format_duration(seconds: int) -> str:
    """秒 → 「X 小时 Y 分钟」/「Y 分钟」。"""
    seconds = max(0, int(seconds))
    h, m = divmod(seconds, 3600)
    m, _ = divmod(m, 60)
    if h > 0:
        return f"{h} 小时 {m} 分钟"
    return f"{m} 分钟"


def format_clock(ts: int) -> str:
    """Unix 时间戳 → HH:MM。"""
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def distribute_to_hours(start_ts: int, end_ts: int, duration: int,
                        hourly: Dict[int, int]) -> None:
    """把一段活动的时长按自然小时切片分摊到 hourly。

    跨小时的活动（如 10:50 - 11:10）会按实际落在各小时内的秒数分摊，
    而不是整段记入开始小时，避免「投入最集中的时段」统计偏移。

    同时被 report.aggregate_stats 与 store.hourly_histogram（热力图）复用，
    故为公开函数。
    """
    if duration <= 0:
        return
    cursor = start_ts
    end = end_ts
    while cursor < end:
        hour = datetime.fromtimestamp(cursor).hour
        # 当前小时内的下一个整点
        hour_end = (cursor // 3600 + 1) * 3600
        slice_end = min(end, hour_end)
        hourly[hour] += slice_end - cursor
        cursor = slice_end


# 向后兼容别名（保留旧私有名，避免破坏既有调用）
_distribute_to_hours = distribute_to_hours


def aggregate_stats(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """聚合一天的 entries 成统计。"""
    if not entries:
        return {
            "total_segments": 0,
            "total_duration": 0,
            "start_time": None,
            "end_time": None,
            "category_durations": {},
            "hourly": {},
            "max_gap": 0,
        }

    total_duration = 0
    category_durations: Dict[str, int] = defaultdict(int)
    hourly: Dict[int, int] = defaultdict(int)
    start_time = min(e["started_at"] for e in entries)
    end_time = max(e["ended_at"] for e in entries)

    for e in entries:
        dur = max(0, e["ended_at"] - e["started_at"])
        total_duration += dur
        category_durations[e.get("category", "其他")] += dur
        # 跨小时的活动按小时切片分摊，否则整段会记到开始小时，导致「集中时段」偏移
        distribute_to_hours(e["started_at"], e["ended_at"], dur, hourly)

    # 最长空档：相邻记录之间的间隔
    max_gap = 0
    sorted_entries = sorted(entries, key=lambda e: e["started_at"])
    for i in range(1, len(sorted_entries)):
        gap = sorted_entries[i]["started_at"] - sorted_entries[i - 1]["ended_at"]
        max_gap = max(max_gap, gap)

    # 分类占比（时长降序）
    category_sorted = sorted(category_durations.items(), key=lambda x: -x[1])
    # 集中时段（时长最多的小时）
    top_hour = max(hourly.items(), key=lambda x: x[1]) if hourly else (None, 0)

    return {
        "total_segments": len(entries),
        "total_duration": total_duration,
        "start_time": start_time,
        "end_time": end_time,
        "category_durations": dict(category_sorted),
        "hourly": dict(hourly),
        "max_gap": max_gap,
        "top_hour": top_hour[0],
        "top_hour_duration": top_hour[1],
    }


def build_report(entries: List[Dict[str, Any]], date: str,
                 ai_analysis: Optional[str] = None) -> str:
    """生成日报 Markdown（统计模板 + 时间线 + 可选 AI 分析）。"""
    stats = aggregate_stats(entries)
    lines = [f"# 工作日报", "", f"**日期：{date}**", ""]

    if stats["total_segments"] == 0:
        lines.append("今天还没有工作记录。")
        return "\n".join(lines)

    # 概览
    lines.append("## 概览")
    lines.append("")
    lines.append(f"- 活动段数：{stats['total_segments']} 段")
    lines.append(f"- 工作跨度：{format_clock(stats['start_time'])} - {format_clock(stats['end_time'])}"
                 f"（约 {format_duration(stats['total_duration'])}）")
    top_cat, top_cat_dur = next(iter(stats["category_durations"].items()), ("其他", 0))
    if stats["total_duration"] > 0:
        pct = top_cat_dur / stats["total_duration"] * 100
        lines.append(f"- 主要精力：{top_cat}（占 {pct:.0f}%）")
    if stats["top_hour"] is not None:
        lines.append(f"- 投入最集中的时段：{stats['top_hour']}:00 - {stats['top_hour'] + 1}:00")
    if stats["max_gap"] > 0:
        lines.append(f"- 最长空档：{format_duration(stats['max_gap'])}")
    lines.append("")

    # 分类占比
    lines.append("## 分类占比")
    lines.append("")
    lines.append("| 分类 | 时长 | 占比 |")
    lines.append("|---|---|---|")
    for cat, dur in stats["category_durations"].items():
        pct = dur / stats["total_duration"] * 100 if stats["total_duration"] > 0 else 0
        lines.append(f"| {cat} | {format_duration(dur)} | {pct:.0f}% |")
    lines.append("")

    # 活动时间线
    lines.append("## 活动时间线")
    lines.append("")
    for e in sorted(entries, key=lambda x: x["started_at"]):
        t = f"{format_clock(e['started_at'])} - {format_clock(e['ended_at'])}"
        lines.append(f"- {t}  {e['content']}（{e.get('category', '其他')}）")
    lines.append("")

    # AI 分析（可选）
    if ai_analysis:
        lines.append("## AI 分析")
        lines.append("")
        lines.append(ai_analysis)
        lines.append("")

    return "\n".join(lines)


def build_ai_prompt(stats: Dict[str, Any], entries: List[Dict[str, Any]]) -> str:
    """构造日报 AI 分析的 prompt（参考 Work_Review 的四段结构）。"""
    timeline = "\n".join(
        f"- {format_clock(e['started_at'])}-{format_clock(e['ended_at'])} {e['content']}（{e.get('category')}）"
        for e in entries
    )
    cats = "、".join(f"{c}({format_duration(d)})" for c, d in stats["category_durations"].items())
    return f"""请基于以下工作记录，生成日报分析，严格按四个标题输出：

### 观察
用 1 段总结今天最明显的工作模式。

### 证据
列出 2-4 条来自时长、分类、时段分布的具体依据。

### 建议
给出 1-2 条可执行建议。

### 小结
用 1 句话收束今天的工作状态。

【今日统计】
活动段数：{stats['total_segments']}
总时长：{format_duration(stats['total_duration'])}
分类：{cats}

【活动时间线】
{timeline}
"""
