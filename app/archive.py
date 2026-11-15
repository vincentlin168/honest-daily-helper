"""日报留档模块（Spec-Driven）。

Spec
----
把生成过的日报留存在本地文件里，形成历史。

为什么用文件而不是加一张 report_logs 表
------------------------------------
加表也能实现，但日报是**文档**而不是**结构化查询对象**：
- 你想直接用记事本 / 编辑器打开看
- 你想拷进笔记软件、邮件、Git
- 归档本身就该是「文件」

所以落成 ``outputs/reports/YYYY-MM-DD.md``，一眼可见、可直接使用。
数据库留给你需要查询的数据，文件留给需要阅读的文档。

留档时机
--------
日报**每次生成都留档**，即使内容没变也留（带时间戳区分）。
这符合「诚实」的原则：不替你决定什么重要，只如实保存。

依赖：标准库（pathlib / datetime）。
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 允许出现在日期文件名里的字符（防路径穿越）
_SAFE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _safe_date(date_str: str) -> Optional[str]:
    """校验日期字符串是否是 YYYY-MM-DD；非法则返回 None。

    防的是路径穿越：用户可以请求 ``date=../../etc/passwd``。
    """
    if not date_str or not _SAFE_DATE.match(date_str):
        return None
    return date_str


class ReportArchive:
    """日报文件留档。"""

    def __init__(self, root: str = "outputs/reports"):
        self.root = Path(root)

    # ---------- 写 ----------
    def save(self, date_str: str, report_md: str) -> Optional[str]:
        """保存日报，返回文件绝对路径。日期非法则返回 None。

        文件名 ``YYYY-MM-DD.md``；同一天重复生成会覆盖（保持一天一份，
        避免「一天 20 个文件」这种噪声）。需要保留历史版本时用 ``save_versioned``。
        """
        safe = _safe_date(date_str)
        if not safe:
            return None
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{safe}.md"
        path.write_text(report_md, encoding="utf-8")
        return str(path.resolve())

    def save_versioned(self, date_str: str, report_md: str) -> Optional[str]:
        """保存带时间戳的版本（``YYYY-MM-DD_HHMMSS.md``），不覆盖已有文件。"""
        safe = _safe_date(date_str)
        if not safe:
            return None
        self.root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%H%M%S")
        path = self.root / f"{safe}_{stamp}.md"
        path.write_text(report_md, encoding="utf-8")
        return str(path.resolve())

    # ---------- 读 ----------
    def list_reports(self, limit: int = 60) -> List[Dict[str, Any]]:
        """列出留档的日报，按日期倒序。

        :return: [{"date": "2026-11-16", "path": "...", "size": 1234, "saved_at": "..."}]
        """
        if not self.root.exists():
            return []
        items: List[Dict[str, Any]] = []
        for path in self.root.glob("*.md"):
            try:
                stat = path.stat()
            except OSError:
                continue
            date_part = path.stem.split("_")[0]
            items.append({
                "date": date_part,
                "file": path.name,
                "path": str(path.resolve()),
                "size": stat.st_size,
                "saved_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "versioned": "_" in path.stem,
            })
        items.sort(key=lambda x: (x["date"], x["saved_at"]), reverse=True)
        return items[:max(1, int(limit))]

    def load(self, date_str: str) -> Optional[str]:
        """按日期读取留档的日报正文；不存在返回 None。"""
        safe = _safe_date(date_str)
        if not safe:
            return None
        path = self.root / f"{safe}.md"
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def delete(self, date_str: str) -> bool:
        """删除某天的留档。"""
        safe = _safe_date(date_str)
        if not safe:
            return False
        path = self.root / f"{safe}.md"
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError:
            return False
