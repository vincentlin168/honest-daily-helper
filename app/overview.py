"""概览统计与成本透明度模块（Spec-Driven）。

Spec
----
把一天的记录汇总成「概览卡片」数据，并算出**真实的 API 花费**。

为什么要有成本透明度
--------------------
这是诚实版相对闭源产品的独有优势：**你花了多少钱，一目了然**。
闭源产品只告诉你「识别了 N 次」，不告诉你这些调用花了多少钱。

诚实的做法：记录每次调用的 token 用量（``llm_calls`` 表已有），
再按你自己填的单价换算成金额。单价由用户配置（``llm.price_per_1m_input`` /
``price_per_1m_output``），因为不同厂商、不同模型、不同时间的价格都不同，
**软件不该替你假定**。未填单价时只显示 token 数，不编造金额。

设计要点
--------
1. **最长连续工作段**：相邻记录之间的空档若小于阈值则视为「连续工作」。
2. **最活跃时段**：时长最集中的小时。
3. **分类 Top N**：按时长降序。
4. **成本可核对**：金额 = (输入 token/1e6 × 输入单价) + (输出 token/1e6 × 输出单价)，
   用 Decimal 而非 float，避免浮点误差在金额上累积。
5. **纯函数优先**：``build_overview(entries, usage, prices)`` 不碰数据库，便于测试。

依赖：标准库（decimal / datetime）。
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from report import aggregate_stats, format_clock, format_duration

# 相邻两段记录的间隔小于这个秒数，视为「连续工作」（同一段时间内没离开）
CONTINUITY_GAP_SECONDS = 5 * 60


def _longest_continuous_work(entries: List[Dict[str, Any]],
                             gap_threshold: int = CONTINUITY_GAP_SECONDS) -> Tuple[int, int]:
    """最长连续工作时长（秒）与段数。

    连续 = 相邻记录之间的空档 < gap_threshold。
    """
    if not entries:
        return (0, 0)
    ordered = sorted(entries, key=lambda e: e["started_at"])
    best_duration = 0
    best_count = 0
    cur_start = ordered[0]["started_at"]
    cur_end = ordered[0]["ended_at"]
    cur_count = 1
    for entry in ordered[1:]:
        gap = entry["started_at"] - cur_end
        if gap <= gap_threshold:
            cur_end = max(cur_end, entry["ended_at"])
            cur_count += 1
        else:
            best_duration = max(best_duration, cur_end - cur_start)
            best_count = max(best_count, cur_count)
            cur_start = entry["started_at"]
            cur_end = entry["ended_at"]
            cur_count = 1
    best_duration = max(best_duration, cur_end - cur_start)
    best_count = max(best_count, cur_count)
    return (best_duration, best_count)


def compute_cost(usage: Dict[str, Any],
                 price_input: float = 0.0,
                 price_output: float = 0.0) -> Dict[str, Any]:
    """按 token 用量与单价算出花费。

    :param usage: ``{"input_tokens":.., "output_tokens":..}``（其它字段忽略）
    :param price_input: 每百万输入 token 的价格（元）
    :param price_output: 每百万输出 token 的价格（元）
    :return: {"cost": float|None, "has_price": bool, "input_tokens":.., "output_tokens":..}
    """
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    has_price = bool(price_input or price_output)

    if not has_price:
        return {
            "cost": None,
            "has_price": False,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    # Decimal 避免浮点误差在金额上累积
    cost = (Decimal(input_tokens) * Decimal(str(price_input))
            + Decimal(output_tokens) * Decimal(str(price_output))) / Decimal(1000000)
    return {
        "cost": float(cost),
        "has_price": True,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def build_overview(entries: List[Dict[str, Any]],
                   usage: Optional[Dict[str, Any]] = None,
                   price_input: float = 0.0,
                   price_output: float = 0.0,
                   top_categories: int = 3) -> Dict[str, Any]:
    """构造概览数据（纯函数，不访问数据库）。

    :param entries: 当天的 entries 列表
    :param usage: token 用量（来自 store.token_usage_by_day）
    :param price_input: 每百万输入 token 价格
    :param price_output: 每百万输出 token 价格
    :param top_categories: 分类榜取前 N
    """
    usage = usage or {}
    stats = aggregate_stats(entries)

    continuous_duration, continuous_count = _longest_continuous_work(entries)

    categories = list(stats["category_durations"].items())
    top = categories[:max(1, int(top_categories))]

    # 最活跃时段：时长最多的那个小时
    top_hour = stats.get("top_hour")
    peak_window = None
    if top_hour is not None:
        peak_window = f"{top_hour:02d}:00 - {(top_hour + 1) % 24:02d}:00"

    cost = compute_cost(usage, price_input, price_output)

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total_segments": stats["total_segments"],
        "total_duration": stats["total_duration"],
        "total_duration_text": format_duration(stats["total_duration"]),
        "start_time": format_clock(stats["start_time"]) if stats.get("start_time") else None,
        "end_time": format_clock(stats["end_time"]) if stats.get("end_time") else None,
        "longest_continuous": continuous_duration,
        "longest_continuous_text": format_duration(continuous_duration),
        "longest_continuous_count": continuous_count,
        "max_gap": stats["max_gap"],
        "max_gap_text": format_duration(stats["max_gap"]),
        "peak_hour": top_hour,
        "peak_window": peak_window,
        "top_categories": [{"name": name, "duration": dur, "text": format_duration(dur)}
                           for name, dur in top],
        "category_count": len(categories),
        "cost": cost,
        "llm_calls": int(usage.get("calls", 0)),
        "llm_failures": int(usage.get("failures", 0)),
    }
