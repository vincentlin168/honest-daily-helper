"""活动会话聚合逻辑（Spec-Driven）。

Spec
----
这是本项目的核心设计：识别结果与上一条相同时，**不新建记录，
而是延长上一条的 ended_at**，把连续相同的活动合并成一段，而非逐帧记录。

聚合规则：
1. 空库 → 新建记录。
2. 内容与最新一条相同（精确）或高度相似（语义相同但措辞不同）→ 合并。
3. 内容明显不同 → 新建记录。

关键：视觉模型对相似画面的输出措辞会有差异，纯精确匹配会导致"语义相同却重复建记录"，
故引入相似度阈值（difflib.SequenceMatcher），超过阈值即视为同一活动。
"""

from difflib import SequenceMatcher
from typing import Optional, Tuple

# 单条活动记录的最大时长（秒），防止一条记录无限延长
MAX_DURATION = 4 * 3600  # 4 小时

# 内容相似度阈值：超过即视为同一活动，合并
SIMILARITY_THRESHOLD = 0.55


def should_merge_content(new_activity: str, latest_activity: str,
                         threshold: float = SIMILARITY_THRESHOLD) -> bool:
    """判断新识别是否应合并到上一条（基于稳定的 activity 标签）。

    先精确匹配，再退化为相似度匹配（处理模型措辞差异）。
    activity 是短标签，比长 content 更适合做聚合判断。
    """
    new = (new_activity or "").strip()
    latest = (latest_activity or "").strip()
    if not new or not latest:
        return False
    if new == latest:
        return True
    ratio = SequenceMatcher(None, new, latest).ratio()
    return ratio >= threshold


def apply(
    store,
    activity: str,
    content: str,
    category: str,
    model: str = "",
    source: str = "screenshot",
    monitors: str = "",
    now: Optional[int] = None,
    max_duration: int = MAX_DURATION,
) -> Tuple[str, int]:
    """核心聚合逻辑：决定新建还是延长，返回 (action, entry_id)。

    :return: ('created', id) 或 ('merged', id)
    """
    from datetime import datetime
    if now is None:
        now = int(datetime.now().timestamp())

    latest = store.latest_entry()
    if latest and should_merge_content(activity, latest.get("activity", "")):
        store.extend_entry(latest["id"], now, max_duration)
        return ("merged", latest["id"])

    entry_id = store.insert_entry({
        "started_at": now,
        "ended_at": now,
        "activity": activity,
        "monitors": monitors,
        "content": content,
        "source": source,
        "model": model,
        "category": category,
    })
    return ("created", entry_id)
