"""SQLite 存储模块（Spec-Driven）。

Spec
----
持久化「诚实日报助手」的工作记录，schema 采用「活动会话」粒度（started_at/ended_at 表示时间段）。

数据模型（3 张核心表）：
    entries     —— 工作活动记录（"活动会话"粒度，started_at/ended_at 表示时间段）
    llm_calls   —— LLM 调用记录（token 用量、耗时、费用归因）
    skip_events —— 跳过事件（画面未变/闲置，去重的痕迹）

设计要点：
1. **零依赖**：sqlite3 标准库。
2. **entries 用时间段**：started_at + ended_at 表示一段活动，配合聚合逻辑
   （内容相同 → 延长 ended_at）而非逐帧记录。
3. **线程安全**：加锁 + check_same_thread=False。
"""

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from report import distribute_to_hours

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL,
    ended_at   INTEGER NOT NULL,
    activity   TEXT    NOT NULL DEFAULT '',
    monitors   TEXT    NOT NULL DEFAULT '',
    content    TEXT    NOT NULL DEFAULT '',
    source     TEXT    NOT NULL DEFAULT 'screenshot',
    model      TEXT    NOT NULL DEFAULT '',
    category   TEXT    NOT NULL DEFAULT '其他'
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    at               INTEGER NOT NULL,
    feature          TEXT    NOT NULL DEFAULT 'recognition',
    model            TEXT    NOT NULL DEFAULT '',
    ok               INTEGER NOT NULL DEFAULT 1,
    http_status      INTEGER NOT NULL DEFAULT 0,
    error            TEXT    NOT NULL DEFAULT '',
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    estimated        REAL    NOT NULL DEFAULT 0,
    latency_ms       INTEGER NOT NULL DEFAULT 0,
    images           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS skip_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       INTEGER NOT NULL,
    kind     TEXT    NOT NULL DEFAULT 'unchanged',
    monitors TEXT    NOT NULL DEFAULT ''
);
"""

_INDEX = """
CREATE INDEX IF NOT EXISTS idx_entries_started ON entries (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_calls_at ON llm_calls (at DESC);
"""


class RecordStore:
    """SQLite 记录仓库。"""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.executescript(_INDEX)
            self._conn.commit()

    # ---------- entries ----------
    def insert_entry(self, entry: Dict[str, Any]) -> int:
        """插入一条活动记录，返回 id。"""
        now = int(datetime.now().timestamp())
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO entries (started_at, ended_at, activity, monitors, content, source, model, category)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    int(entry.get("started_at", now)),
                    int(entry.get("ended_at", now)),
                    entry.get("activity", ""),
                    entry.get("monitors", ""),
                    entry.get("content", ""),
                    entry.get("source", "screenshot"),
                    entry.get("model", ""),
                    entry.get("category", "其他"),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def extend_entry(self, entry_id: int, now: int, max_duration: int) -> bool:
        """延长记录的 ended_at（活动会话聚合的核心）。"""
        with self._lock:
            cur = self._conn.execute(
                """UPDATE entries SET ended_at = MIN(MAX(ended_at, ?), started_at + ?)
                   WHERE id = ?""",
                (int(now), int(max_duration), int(entry_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_category(self, entry_id: int, category: str) -> bool:
        """手动修改一条记录的分类。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE entries SET category = ? WHERE id = ?",
                (category, int(entry_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def latest_entry(self) -> Optional[Dict[str, Any]]:
        """取最新一条记录（用于聚合判断「内容是否与上一条相同」）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM entries ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._to_dict(row) if row else None

    def entries_by_day(self, date_str: str) -> List[Dict[str, Any]]:
        """按天取记录，date_str 如 '2026-11-15'（本地日期）。"""
        day_start = int(datetime.fromisoformat(date_str).timestamp())
        day_end = day_start + 86400
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM entries WHERE started_at >= ? AND started_at < ? ORDER BY started_at",
                (day_start, day_end),
            ).fetchall()
        return [self._to_dict(r) for r in rows]

    def entries_in_range(self, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
        """取时间区间内的记录（按 started_at 过滤，用于多天热力图）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM entries WHERE started_at >= ? AND started_at < ? ORDER BY started_at",
                (int(start_ts), int(end_ts)),
            ).fetchall()
        return [self._to_dict(r) for r in rows]

    # ---------- 热力图 ----------
    def hourly_histogram(self, days: int = 7) -> List[Dict[str, Any]]:
        """按「天 × 小时」聚合时长，用于热力图。

        返回形如 ``[{"date": "2026-11-10", "weekday": 0, "hours": [秒, ...24项]}, ...]``
        每天 24 个槽位，值为该小时内的活动秒数。

        跨小时的活动会按实际落在各小时内的秒数分摊（复用 report.distribute_to_hours），
        跨天的活动按自然日边界切分。
        """
        days = max(1, min(90, int(days)))
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        first_day = today - timedelta(days=days - 1)
        start_ts = int(first_day.timestamp())
        end_ts = int((today + timedelta(days=1)).timestamp())

        entries = self.entries_in_range(start_ts, end_ts)

        # date -> [24 个小时的秒数]
        grid: Dict[str, List[int]] = {}
        for entry in entries:
            seg_start = int(entry["started_at"])
            seg_end = int(entry["ended_at"])
            if seg_end <= seg_start:
                continue
            # 逐自然日切分
            cursor = seg_start
            while cursor < seg_end:
                day = datetime.fromtimestamp(cursor)
                day_key = day.strftime("%Y-%m-%d")
                next_midnight = int((day + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0).timestamp())
                slice_end = min(seg_end, next_midnight)
                hours = grid.setdefault(day_key, [0] * 24)
                # distribute_to_hours 直接按「一天中的第几小时」(0-23) 累加
                distribute_to_hours(cursor, slice_end, slice_end - cursor, hours)
                cursor = slice_end

        result = []
        for offset in range(days):
            day = first_day + timedelta(days=offset)
            key = day.strftime("%Y-%m-%d")
            result.append({
                "date": key,
                "weekday": day.weekday(),   # 0=周一
                "hours": grid.get(key, [0] * 24),
            })
        return result

    # ---------- 成本 ----------
    def token_usage_by_day(self, date_str: str) -> Dict[str, int]:
        """某天的 token 用量汇总（含失败调用的消耗）。"""
        day_start = int(datetime.fromisoformat(date_str).timestamp())
        day_end = day_start + 86400
        with self._lock:
            row = self._conn.execute(
                """SELECT COALESCE(SUM(input_tokens),0) AS ti,
                          COALESCE(SUM(output_tokens),0) AS tout,
                          COUNT(*) AS calls,
                          COALESCE(SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END),0) AS failures
                     FROM llm_calls WHERE at >= ? AND at < ?""",
                (day_start, day_end),
            ).fetchone()
        return {
            "input_tokens": int(row["ti"]),
            "output_tokens": int(row["tout"]),
            "total_tokens": int(row["ti"]) + int(row["tout"]),
            "calls": int(row["calls"]),
            "failures": int(row["failures"]),
        }

    def token_usage_range(self, start_ts: int, end_ts: int) -> Dict[str, int]:
        """时间区间内的 token 用量汇总。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT COALESCE(SUM(input_tokens),0) AS ti,
                          COALESCE(SUM(output_tokens),0) AS tout,
                          COUNT(*) AS calls
                     FROM llm_calls WHERE at >= ? AND at < ?""",
                (int(start_ts), int(end_ts)),
            ).fetchone()
        return {
            "input_tokens": int(row["ti"]),
            "output_tokens": int(row["tout"]),
            "total_tokens": int(row["ti"]) + int(row["tout"]),
            "calls": int(row["calls"]),
        }

    # ---------- llm_calls ----------
    def insert_llm_call(self, call: Dict[str, Any]) -> None:
        now = int(datetime.now().timestamp())
        with self._lock:
            self._conn.execute(
                """INSERT INTO llm_calls (at, feature, model, ok, http_status, error,
                                          input_tokens, output_tokens, estimated, latency_ms, images)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(call.get("at", now)),
                    call.get("feature", "recognition"),
                    call.get("model", ""),
                    1 if call.get("ok", True) else 0,
                    int(call.get("http_status", 0)),
                    call.get("error", ""),
                    int(call.get("input_tokens", 0)),
                    int(call.get("output_tokens", 0)),
                    float(call.get("estimated", 0)),
                    int(call.get("latency_ms", 0)),
                    int(call.get("images", 1)),
                ),
            )
            self._conn.commit()

    # ---------- skip_events ----------
    def insert_skip_event(self, kind: str, at: Optional[int] = None) -> None:
        now = at or int(datetime.now().timestamp())
        with self._lock:
            self._conn.execute(
                "INSERT INTO skip_events (at, kind, monitors) VALUES (?,?,?)",
                (int(now), kind, ""),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {k: row[k] for k in row.keys()}
