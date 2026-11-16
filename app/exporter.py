"""数据导出模块（Spec-Driven）。

Spec
----
把用户自己的数据导出成可携带的格式。

为什么这是「诚实」的一部分
------------------------
闭源产品的典型做法：数据存在你机器上，但**导不出来**。
于是你被锁住了 —— 想换工具、想自己分析、想备份，都得继续用这个软件。

诚实的做法：**你的数据是你的**。一键导出成开放格式（JSON / Markdown / CSV），
不带任何厂商格式、不加密、不联网。导出的文件用任何编辑器都能打开。

导出三种格式
------------
1. **JSON** —— 完整结构化数据，供程序分析
2. **Markdown** —— 人类可读的时间线 + 日报，供归档或分享
3. **CSV** —— 表格，供 Excel / 表格软件

隐私约束
--------
- 导出内容**只含你本机的记录**，不含 API Key、不含 token。
- 不上传任何地方，纯本地写文件。

依赖：标准库（json / csv / pathlib）。
"""

import csv
import io
import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from report import format_duration


def _entry_to_row(entry: Dict[str, Any]) -> Dict[str, Any]:
    """把一条 entry 转成导出用的扁平结构。"""
    started = int(entry.get("started_at", 0))
    ended = int(entry.get("ended_at", 0))
    duration = max(0, ended - started)
    return {
        "id": entry.get("id"),
        "date": datetime.fromtimestamp(started).strftime("%Y-%m-%d") if started else "",
        "start": datetime.fromtimestamp(started).strftime("%H:%M:%S") if started else "",
        "end": datetime.fromtimestamp(ended).strftime("%H:%M:%S") if ended else "",
        "duration_seconds": duration,
        "duration_text": format_duration(duration),
        "activity": entry.get("activity", ""),
        "category": entry.get("category", ""),
        "content": entry.get("content", ""),
        "source": entry.get("source", ""),
        "model": entry.get("model", ""),
    }


def export_json(entries: Iterable[Dict[str, Any]],
                meta: Optional[Dict[str, Any]] = None) -> str:
    """导出为 JSON 字符串。

    :param entries: 记录列表
    :param meta: 附加元信息（导出时间、数据来源说明等）
    """
    items = [_entry_to_row(e) for e in entries]
    payload = {
        "format": "honest-daily-helper/export",
        "version": 2,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(items),
        "meta": meta or {},
        "entries": items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def export_csv(entries: Iterable[Dict[str, Any]]) -> str:
    """导出为 CSV 字符串（UTF-8 BOM，Excel 直接打开不乱码）。"""
    items = [_entry_to_row(e) for e in entries]
    if not items:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(items[0].keys()))
    writer.writeheader()
    writer.writerows(items)
    # Excel 靠 BOM 判断 UTF-8
    return "\ufeff" + buf.getvalue()


def export_markdown(entries: Iterable[Dict[str, Any]],
                    date_str: Optional[str] = None,
                    report_md: Optional[str] = None) -> str:
    """导出为 Markdown（人类可读的完整快照）。

    :param entries: 记录列表
    :param date_str: 导出日期（None 表示跨多天）
    :param report_md: 可选的日报正文，追加在时间线之后
    """
    items = [_entry_to_row(e) for e in entries]
    lines: List[str] = []

    title = f"# 工作记录 · {date_str}" if date_str else "# 工作记录"
    lines.append(title)
    lines.append("")
    lines.append(f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"记录条数：{len(items)}")
    if items:
        total = sum(i["duration_seconds"] for i in items)
        lines.append(f"总时长：{format_duration(total)}")
    lines.append("")
    lines.append("> 本文件由诚实日报助手导出，数据完全属于你自己，可自由使用。")
    lines.append("")

    if report_md:
        lines.append("## 日报")
        lines.append("")
        lines.append(report_md)
        lines.append("")

    lines.append("## 时间线")
    lines.append("")
    if not items:
        lines.append("_没有记录_")
        lines.append("")
    else:
        lines.append("| 时间 | 时长 | 分类 | 内容 |")
        lines.append("|---|---|---|---|")
        for item in items:
            time_range = f"{item['start']} - {item['end']}"
            content = (item["content"] or "").replace("|", "\\|").replace("\n", " ")
            if len(content) > 80:
                content = content[:80] + "…"
            lines.append(
                f"| {time_range} | {item['duration_text']} | {item['category']} | {content} |"
            )
        lines.append("")

    return "\n".join(lines)


def write_export(path: str, content: str) -> str:
    """把导出内容写入文件，返回绝对路径。

    自动按扩展名选择编码：``.csv`` 用 utf-8-sig（Excel 友好），
    其余用 utf-8。
    """
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    encoding = "utf-8-sig" if p.suffix.lower() == ".csv" else "utf-8"
    p.write_text(content, encoding=encoding)
    return str(p.resolve())
