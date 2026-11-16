"""历史数据归档（Spec-Driven）。

Spec
----
v2 修复了两个会扭曲统计的缺陷：

1. **静态屏幕遗漏** —— 画面没变时旧版直接跳过，既不延长记录也不留痕，
   58% 的工作时间从时间线上消失了。
2. **零秒条目** —— 旧版 ``summarize_buffer`` 把 ``started_at`` 和 ``ended_at``
   都写成同一时刻，导致 70% 的条目时长为 0。

修复之前积累的数据带着这些缺陷。如果直接留着：
- 概览页的「总时长」被低估（0 秒条目不贡献时长）
- 覆盖率统计偏低（分母是真实跨度，分子少了大量归零的时间）
- 时间线被 0 秒条目切得很碎，看不清实际做了什么

所以提供归档能力：**把有缺陷的历史记录移到归档表，让实时统计只反映
修复后的真实数据，同时一条都不丢。**

设计要点
--------
1. **不伪造时长**。我们不知道那些 gap 里用户到底在干什么 ——
   猜测出来的时长是编的数据，与「诚实」背道而驰。
   归档只是「挪走 + 标注原因」，不改动任何时间戳。
2. **可逆**。归档表保留全部字段，随时可以搬回来。
3. **可导出**。归档数据也能导出成 JSON/Markdown/CSV ——
   用户的数据属于用户，移走不等于失去控制权。
4. **默认不动手**。归档是显式操作，绝不在启动时偷偷搬走用户数据。

判定「有缺陷的旧记录」
---------------------
- ``ended_at <= started_at``（时长为 0 或负）—— 旧版零秒条目
- 早于 v2 修复上线时间、且时长为 0 的记录

判定依据放在函数参数里（``before_ts``），不写死时间戳 ——
这样在别的机器 / 别的数据集上也能复用。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 归档原因
REASON_ZERO_DURATION = "zero_duration"      # 零秒/负时长（旧版缺陷）
REASON_MANUAL = "manual"                    # 用户手动归档

_ARCHIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS archived_entries (
    id            INTEGER PRIMARY KEY,
    started_at    INTEGER NOT NULL,
    ended_at      INTEGER NOT NULL,
    activity      TEXT    NOT NULL DEFAULT '',
    monitors      TEXT    NOT NULL DEFAULT '',
    content       TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    category      TEXT    NOT NULL DEFAULT '',
    archived_at   INTEGER NOT NULL,
    archive_reason TEXT   NOT NULL DEFAULT '',
    original_table TEXT   NOT NULL DEFAULT 'entries'
);
CREATE INDEX IF NOT EXISTS idx_archived_started ON archived_entries (started_at DESC);
"""


def _is_defective(row: Dict[str, Any]) -> bool:
    """这条记录是否属于「旧版缺陷产物」。

    判据：时长为 0 或负数。修复后写入的记录时长必然 > 0
    （除非同一秒内开始又结束，那也是合法的瞬时操作）。
    """
    return int(row["ended_at"]) - int(row["started_at"]) <= 0


def scan_defective(store, before_ts: Optional[int] = None) -> Dict[str, Any]:
    """扫描有缺陷的历史记录（不修改任何数据）。

    :param store: RecordStore 实例
    :param before_ts: 只看这个时间点之前的记录；None 表示全部
    """
    conn = store._conn  # noqa: SLF001 —— 同模块内访问底层连接
    sql = "SELECT id, started_at, ended_at, activity, category FROM entries"
    params: List[Any] = []
    if before_ts is not None:
        sql += " WHERE started_at < ?"
        params.append(int(before_ts))
    sql += " ORDER BY started_at"

    with store._lock:
        rows = conn.execute(sql, params).fetchall()

    defective = [dict(r) for r in rows if _is_defective(dict(r))]
    total = len(rows)
    zero = len(defective)
    return {
        "total_scanned": total,
        "defective": zero,
        "healthy": total - zero,
        "defective_pct": (zero / total * 100) if total else 0.0,
        "sample": defective[:5],
    }


def archive_defective(store, before_ts: Optional[int] = None,
                      dry_run: bool = False) -> Dict[str, Any]:
    """把有缺陷的历史记录移到归档表。

    :param before_ts: 只归档这个时间点之前的记录
    :param dry_run: True 时只统计不移动（默认应先跑一次 dry_run 看清影响）
    """
    scan = scan_defective(store, before_ts)
    if dry_run:
        return {**scan, "archived": 0, "dry_run": True}

    conn = store._conn  # noqa: SLF001
    now = int(datetime.now().timestamp())
    archived = 0

    with store._lock:
        with conn:  # 事务：要么全成功要么全回滚
            conn.executescript(_ARCHIVE_SCHEMA)
            rows = conn.execute(
                "SELECT * FROM entries ORDER BY started_at").fetchall()
            to_move = [dict(r) for r in rows if _is_defective(dict(r))]
            if before_ts is not None:
                to_move = [r for r in to_move if int(r["started_at"]) < int(before_ts)]

            for row in to_move:
                conn.execute(
                    """INSERT INTO archived_entries
                       (id, started_at, ended_at, activity, monitors, content,
                        source, model, category, archived_at, archive_reason, original_table)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        int(row["id"]), int(row["started_at"]), int(row["ended_at"]),
                        row["activity"], row["monitors"], row["content"],
                        row["source"], row["model"], row["category"],
                        now, REASON_ZERO_DURATION, "entries",
                    ))
                conn.execute("DELETE FROM entries WHERE id = ?", (int(row["id"]),))
                archived += 1

    return {
        "total_scanned": scan["total_scanned"],
        "defective": scan["defective"],
        "healthy": scan["healthy"],
        "defective_pct": scan["defective_pct"],
        "archived": archived,
        "dry_run": False,
    }


def restore_archived(store, ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """把归档记录搬回 entries 表。

    :param ids: 只恢复这些 id；None 表示全部恢复
    """
    conn = store._conn  # noqa: SLF001
    with store._lock:
        with conn:
            conn.executescript(_ARCHIVE_SCHEMA)
            sql = "SELECT * FROM archived_entries"
            params: List[Any] = []
            if ids:
                sql += f" WHERE id IN ({','.join('?' * len(ids))})"
                params.extend(int(i) for i in ids)
            sql += " ORDER BY started_at"
            rows = conn.execute(sql, params).fetchall()

            restored = 0
            skipped = 0
            for row in rows:
                exists = conn.execute(
                    "SELECT 1 FROM entries WHERE id = ?", (int(row["id"]),)).fetchone()
                if exists:
                    skipped += 1
                    continue
                conn.execute(
                    """INSERT INTO entries
                       (id, started_at, ended_at, activity, monitors, content,
                        source, model, category)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        int(row["id"]), int(row["started_at"]), int(row["ended_at"]),
                        row["activity"], row["monitors"], row["content"],
                        row["source"], row["model"], row["category"],
                    ))
                conn.execute("DELETE FROM archived_entries WHERE id = ?", (int(row["id"]),))
                restored += 1

    return {"restored": restored, "skipped": skipped}


def list_archived(store, limit: int = 200, offset: int = 0) -> Dict[str, Any]:
    """分页列出归档记录。"""
    conn = store._conn  # noqa: SLF001
    with store._lock:
        conn.executescript(_ARCHIVE_SCHEMA)
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM archived_entries").fetchone()["c"]
        rows = conn.execute(
            "SELECT * FROM archived_entries ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset))).fetchall()
    return {
        "total": total,
        "entries": [dict(r) for r in rows],
    }


def export_archived(store, out_path: str) -> Dict[str, Any]:
    """把归档数据导出成 JSON 文件（用户的数据始终可取回）。"""
    conn = store._conn  # noqa: SLF001
    with store._lock:
        conn.executescript(_ARCHIVE_SCHEMA)
        rows = conn.execute(
            "SELECT * FROM archived_entries ORDER BY started_at").fetchall()
    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(rows),
        "entries": [dict(r) for r in rows],
    }
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"path": str(target), "count": len(rows)}
