"""Skill 本地服务 + Web UI 后端（Spec-Driven）。

Spec
----
提供本地 HTTP 服务（默认 127.0.0.1:8088），既是「Skill 服务」（供其他 Agent 查询），
也是「Web UI 后端」（浏览器可视化界面 + 设置面板）。

接口：
    GET  /                       Web UI 页面
    GET  /api/health             健康检查（无鉴权）
    GET  /api/status             服务状态
    GET  /api/entries?date=...   按天查记录
    GET  /api/report?date=...    生成日报
    GET  /api/settings           读取配置（密钥掩码）
    POST /api/settings           更新配置（截屏间隔/分类/模型等）
    POST /api/entries/{id}/category  手动改分类

设计要点：
1. 零依赖（标准库 http.server），只监听 127.0.0.1。
2. 可选 Bearer Token 鉴权。
3. 设置更新通过 settings_callback 回调，让主程序热更新模块。
4. **多线程处理**（ThreadingHTTPServer）：Web UI 切页签时会并发请求，
   单线程 HTTPServer 会让慢请求（如生成日报）阻塞其它请求，页面表现为「卡住」。
"""

import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

import exporter
from overview import build_overview


def _resource_path(name: str) -> Path:
    """资源路径：兼容 PyInstaller 打包（资源在 sys._MEIPASS）。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


class SkillHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器。"""

    protocol_version = "HTTP/1.1"

    store = None              # RecordStore
    token = ""                # 鉴权 token
    report_builder = None     # build_report 函数
    config = None             # Config 实例
    settings_callback = None  # 设置更新后的回调（热更新模块）
    llm_tester = None         # 连通性测试回调（返回 {ok, reply|error}）
    archive = None            # ReportArchive（日报留档）
    export_dir = "exports"    # 数据导出目录

    def _authorized(self) -> bool:
        if not self.token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}"

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- 导出辅助 ----------
    def _entries_for_export(self, date_str: str) -> list:
        """取待导出的记录。date 为空则导出全部（有上限）。"""
        if not self.store:
            return []
        if date_str:
            return self.store.entries_by_day(date_str)
        # 全部：取最近 90 天
        end = int(datetime.now().timestamp())
        start = end - 90 * 86400
        return self.store.entries_in_range(start, end)

    def _render_export(self, entries: list, date_str: str, fmt: str):
        """按格式渲染导出内容。不支持的格式返回 None。"""
        fmt = (fmt or "json").lower()
        if fmt == "json":
            meta = {"scope": date_str or "all", "tool": "诚实日报助手"}
            return exporter.export_json(entries, meta)
        if fmt == "csv":
            return exporter.export_csv(entries)
        if fmt in ("md", "markdown"):
            report_md = None
            arch = type(self).archive
            if date_str and arch:
                report_md = arch.load(date_str)
            return exporter.export_markdown(entries, date_str or None, report_md)
        return None

    # ---------- GET ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # 页面与健康检查无需鉴权
        if path == "/":
            html_path = _resource_path("web_ui.html")
            if html_path.exists():
                self._send_html(html_path.read_text(encoding="utf-8"))
            else:
                self._send_html("<h2>web_ui.html 未找到</h2>")
            return

        if path == "/api/health":
            self._send_json({"ok": True, "service": "honest-daily-helper"})
            return

        if not self._authorized():
            self._send_json({"ok": False, "error": "unauthorized"}, status=401)
            return

        if path == "/api/status":
            today = datetime.now().strftime("%Y-%m-%d")
            entries = self.store.entries_by_day(today) if self.store else []
            self._send_json({"ok": True, "date": today, "entries_count": len(entries)})
            return

        if path == "/api/entries":
            date = (query.get("date") or [datetime.now().strftime("%Y-%m-%d")])[0]
            entries = self.store.entries_by_day(date) if self.store else []
            self._send_json({"ok": True, "date": date, "entries": entries})
            return

        if path == "/api/report":
            date = (query.get("date") or [datetime.now().strftime("%Y-%m-%d")])[0]
            entries = self.store.entries_by_day(date) if self.store else []
            builder = type(self).report_builder
            report_md = builder(entries, date) if builder else ""
            self._send_json({"ok": True, "date": date, "report": report_md})
            return

        if path == "/api/settings":
            cfg = type(self).config
            self._send_json(cfg.to_public_dict() if cfg else {})
            return

        # 概览：今日统计卡片 + 成本透明度
        if path == "/api/overview":
            date = (query.get("date") or [datetime.now().strftime("%Y-%m-%d")])[0]
            entries = self.store.entries_by_day(date) if self.store else []
            usage = self.store.token_usage_by_day(date) if self.store else {}
            cfg = type(self).config
            price_in = float(cfg.get("llm", "price_per_1m_input", default=0) or 0) if cfg else 0.0
            price_out = float(cfg.get("llm", "price_per_1m_output", default=0) or 0) if cfg else 0.0
            data = build_overview(entries, usage, price_in, price_out)
            data["date"] = date
            self._send_json({"ok": True, **data})
            return

        # 热力图：最近 N 天 × 24 小时
        if path == "/api/heatmap":
            try:
                days = int((query.get("days") or [7])[0])
            except (TypeError, ValueError):
                days = 7
            grid = self.store.hourly_histogram(days) if self.store else []
            self._send_json({"ok": True, "days": days, "grid": grid})
            return

        # 抓屏间隔 / 汇总周期的实测效果
        if path == "/api/intervals":
            try:
                days = int((query.get("days") or [7])[0])
            except (TypeError, ValueError):
                days = 7
            data = self.store.interval_effectiveness(days) if self.store else {}
            cfg = type(self).config
            if cfg:
                data["configured_snap_interval"] = int(
                    cfg.get("capture", "snap_interval", default=15) or 15)
                data["configured_summary_interval"] = int(
                    cfg.get("capture", "interval", default=120) or 120)
            self._send_json({"ok": True, **data})
            return

        # 错误诊断：复用 llm_calls.error，不另建 error_logs 表
        if path == "/api/errors":
            try:
                days = int((query.get("days") or [7])[0])
            except (TypeError, ValueError):
                days = 7
            summary = self.store.error_summary(days) if self.store else {}
            self._send_json({"ok": True, **summary})
            return

        # 日报留档列表
        if path == "/api/archive":
            arch = type(self).archive
            items = arch.list_reports() if arch else []
            self._send_json({"ok": True, "reports": items})
            return

        # 读取某天的留档正文
        if path == "/api/archive/body":
            date = (query.get("date") or [""])[0]
            arch = type(self).archive
            body = arch.load(date) if arch else None
            if body is None:
                self._send_json({"ok": False, "error": "没有该日期的留档"}, status=404)
                return
            self._send_json({"ok": True, "date": date, "report": body})
            return

        # 导出预览（不写文件，仅返回内容，便于前端「复制到剪贴板」）
        if path == "/api/export/preview":
            date = (query.get("date") or [""])[0]
            fmt = (query.get("format") or ["json"])[0]
            entries = self._entries_for_export(date)
            content = self._render_export(entries, date, fmt)
            if content is None:
                self._send_json({"ok": False, "error": f"不支持的格式: {fmt}"}, status=400)
                return
            self._send_json({"ok": True, "format": fmt, "date": date,
                             "content": content, "count": len(entries)})
            return

        self._send_json({"ok": False, "error": f"unknown path: {path}"}, status=404)

    # ---------- POST ----------
    def     do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if not self._authorized():
            self._send_json({"ok": False, "error": "unauthorized"}, status=401)
            return

        # 连通性测试：/api/llm/test —— 验证 API Key / 模型 / 网络是否可用
        if path == "/api/llm/test":
            tester = type(self).llm_tester
            if tester is None:
                self._send_json({"ok": False, "error": "服务未就绪"}, status=503)
                return
            try:
                self._send_json(tester())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=200)
            return

        # 手动改分类：/api/entries/{id}/category
        if path.startswith("/api/entries/") and path.endswith("/category"):
            try:
                entry_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                self._send_json({"ok": False, "error": "invalid id"}, status=400)
                return
            body = self._read_json_body()
            category = body.get("category", "")
            if not category:
                self._send_json({"ok": False, "error": "category required"}, status=400)
                return
            ok = self.store.update_category(entry_id, category) if self.store else False
            self._send_json({"ok": ok})
            return

        # 编辑记录：/api/entries/{id} —— 改 activity / content / category
        if path.startswith("/api/entries/") and path.count("/") == 3:
            try:
                entry_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                self._send_json({"ok": False, "error": "invalid id"}, status=400)
                return
            body = self._read_json_body()
            fields = {k: body[k] for k in ("activity", "content", "category") if k in body}
            if not fields:
                self._send_json({"ok": False, "error": "没有可编辑字段"}, status=400)
                return
            ok = self.store.update_entry(entry_id, **fields) if self.store else False
            entry = self.store.get_entry(entry_id) if (ok and self.store) else None
            self._send_json({"ok": ok, "entry": entry})
            return

        # 删除记录：/api/entries/{id}/delete
        if path.startswith("/api/entries/") and path.endswith("/delete"):
            try:
                entry_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                self._send_json({"ok": False, "error": "invalid id"}, status=400)
                return
            ok = self.store.delete_entry(entry_id) if self.store else False
            self._send_json({"ok": ok})
            return

        # 导出到文件：/api/export
        if path == "/api/export":
            body = self._read_json_body()
            date = body.get("date", "") or ""
            fmt = body.get("format", "json") or "json"
            entries = self._entries_for_export(date)
            content = self._render_export(entries, date, fmt)
            if content is None:
                self._send_json({"ok": False, "error": f"不支持的格式: {fmt}"}, status=400)
                return
            root = Path(type(self).export_dir or "exports")
            root.mkdir(parents=True, exist_ok=True)
            stem = date or "全部记录"
            ext = {"json": "json", "csv": "csv", "md": "md", "markdown": "md"}.get(fmt.lower(), "txt")
            path = root / f"{stem}.{ext}"
            exporter.write_export(str(path), content)
            self._send_json({
                "ok": True,
                "path": str(path.resolve()),
                "format": fmt,
                "count": len(entries),
                "message": f"已导出 {len(entries)} 条记录到 {path.resolve()}",
            })
            return

        # 删除留档：/api/archive/delete
        if path == "/api/archive/delete":
            body = self._read_json_body()
            date = body.get("date", "") or ""
            arch = type(self).archive
            ok = arch.delete(date) if arch else False
            self._send_json({"ok": ok})
            return

        # 更新设置：/api/settings
        if path == "/api/settings":
            body = self._read_json_body()
            cfg = type(self).config
            if cfg is None:
                self._send_json({"ok": False, "error": "config not available"}, status=500)
                return
            cfg.update_from_dict(body)
            cfg.save()
            callback = type(self).settings_callback
            if callback:
                try:
                    callback()
                except Exception:
                    pass
            self._send_json({"ok": True, "message": "设置已保存并生效"})
            return

        self._send_json({"ok": False, "error": f"unknown path: {path}"}, status=404)

    def log_message(self, *args):
        pass  # 静默


class SkillServer:
    """Skill 本地服务。"""

    def __init__(self, store, token: str = "", port: int = 8088,
                 report_builder=None, config=None, settings_callback=None,
                 llm_tester=None, archive=None, export_dir: str = "exports"):
        self.store = store
        self.port = port
        SkillHandler.store = store
        SkillHandler.token = token or ""
        SkillHandler.report_builder = report_builder
        SkillHandler.config = config
        SkillHandler.settings_callback = settings_callback
        SkillHandler.llm_tester = llm_tester
        SkillHandler.archive = archive
        SkillHandler.export_dir = export_dir

    def run(self):
        # daemon_threads=True：请求线程随主进程退出，避免关不掉
        server = ThreadingHTTPServer(("127.0.0.1", self.port), SkillHandler)
        server.daemon_threads = True
        print(f"Skill 服务已启动: http://127.0.0.1:{self.port}")
        server.serve_forever()
