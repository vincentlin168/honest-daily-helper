"""主编排模块（Spec-Driven）。

Spec
----
把各模块串成完整可用的「诚实日报助手」：

    定时截屏 → 变更检测 → 视觉 LLM 识别 → 活动会话聚合 → SQLite 存储
                                            ↓
                              日报生成（手动 / 定时自动）
                                            ↓
                        数据上报（可选）+ Skill 本地服务

设计要点：
1. **诚实原则**：无授权、无联网验证、AI 直连官方（自己的 key）、数据全本地。
2. **定时循环**：threading + sleep，简单可靠。
3. **日报双触发**：手动调用 + 定时自动（配置 report.scheduled_time）。
4. **Skill 服务**：后台线程跑 http.server（127.0.0.1）。

依赖：见各模块（Pillow / httpx）。
"""

import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

# 让 print 实时输出（重定向到管道/文件时默认块缓冲，会导致看不到实时识别结果）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except Exception:
        pass

import aggregate
from archive import ReportArchive
from capture import Capture
from config import Config
from http_client import make_client
from logging_setup import get_logger
from llm_client import LLMClient
from report import build_report, build_ai_prompt
from skill_server import SkillServer
from store import RecordStore
from upload import ReportUploader

log = get_logger()


class DailyHelper:
    """诚实日报助手主类。"""

    def __init__(self, config_path: Optional[str] = None):
        self.config = Config(config_path)
        # store 只初始化一次（保持数据库连接稳定）
        self.store = RecordStore(self.config.get("storage", "db_path", default="records.db"))
        self._last_report_date: Optional[str] = None
        # 日报留档目录：优先配置，默认 app/outputs/reports
        archive_dir = self.config.get("storage", "archive_dir", default="outputs/reports")
        self.archive = ReportArchive(archive_dir)
        self.export_dir = self.config.get("storage", "export_dir", default="exports")
        self._init_capture_llm()

    def _init_capture_llm(self) -> None:
        """初始化/重建 capture、llm、uploader（设置热更新时调用）。"""
        cap_cfg = self.config.capture_config
        llm_cfg = self.config.llm_config

        save_dir = cap_cfg.get("save_dir") or None
        self.capture = Capture(
            save_dir=save_dir,
            quality=int(cap_cfg.get("quality", 60)),
        )
        preset = cap_cfg.get("region_preset")
        if preset:
            self.capture.set_region_preset(preset)

        self.llm = LLMClient(
            api_base=llm_cfg.get("api_base", ""),
            api_key=llm_cfg.get("api_key", ""),
            model=llm_cfg.get("model", "deepseek-flash"),
            system_prompt=llm_cfg.get("system_prompt") or None,
            categories=self.config.get("categories"),
            use_system_proxy=bool(llm_cfg.get("use_system_proxy", False)),
        )

        up_cfg = self.config.get("upload", default={})
        self.uploader = ReportUploader(
            url=up_cfg.get("url", ""),
            token=up_cfg.get("token", ""),
            use_system_proxy=bool(up_cfg.get("use_system_proxy", False)),
        )

    def _extend_current_activity(self) -> None:
        """把上一条活动记录延长到当前时刻。

        这是「不遗漏」的关键：屏幕像素不变时（读代码、看文档、思考），
        活动时间仍应计入。延长而不是新建，避免时间线碎片化。

        连续多次延长会受 ``aggregate.MAX_DURATION``（4 小时）封顶，
        保证单条记录不会无限延长。
        """
        latest = self.store.latest_entry()
        if not latest:
            return
        now = int(datetime.now().timestamp())
        try:
            self.store.extend_entry(latest["id"], now, aggregate.MAX_DURATION)
        except Exception as exc:
            log.warning("[延长记录失败] %s", exc)

    def _reload_settings(self) -> None:
        """设置更新后重建 capture/llm/uploader（不重建 store）。"""
        self._init_capture_llm()

    # ---------- 密集抓屏三层架构 ----------
    def capture_and_recognize(self) -> Optional[Dict[str, Any]]:
        """抓屏 + 识别（不聚合、不写 entries）。

        密集抓屏循环调用：画面没变返回 None，变化则识别并返回 recognition（含 _at 时间戳）。

        画面未变化时的关键处理
        ----------------------
        画面没变 **不代表没在工作** —— 恰恰相反，绝大多数时间是用户在读代码、
        看文档、写文字，屏幕像素可能长时间不变。

        所以画面未变时必须做两件事：
        1. **延长上一条活动记录**（ended_at 推到当前时刻），否则时间线会「断裂」；
        2. **记一条 skip_event**，留下「这段时间屏幕没动过」的痕迹，可回溯。

        少了第 1 步，实测会出现：9.5 小时的工作只记录了 4 小时，
        **58% 的工作时间在时间线上完全不可见**（见 tests/test_capture_interval.py）。
        """
        shot = self.capture.grab(save_to_disk=False)
        if shot.is_empty:
            # 画面未变：跳过识别（省 token），但要延长上一条记录 + 留痕
            self._extend_current_activity()
            self.store.insert_skip_event("unchanged")
            return None

        started = time.perf_counter()
        try:
            text, usage = self.llm.chat_with_image(shot.base64)
            rec = self.llm.parse_recognition(text)
            rec["_at"] = int(datetime.now().timestamp())
            latency_ms = int((time.perf_counter() - started) * 1000)
            self.store.insert_llm_call({
                "model": self.llm.model,
                "ok": True,
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "latency_ms": latency_ms,
                "images": 1,
            })
            return rec
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self.store.insert_llm_call({
                "model": self.llm.model,
                "ok": False,
                "error": str(exc),
                "latency_ms": latency_ms,
            })
            return {"_error": str(exc)}

    def summarize_buffer(self, buffer: list, summary_at: Optional[int] = None) -> Dict[str, int]:
        """把 buffer 里的识别结果聚合写入 store（相同 activity 自动合并成活动段）。

        时间语义
        --------
        每条识别记录了它**被识别的那一刻**（``rec["_at"]``）。
        但一段活动的时间范围应该是「本周期内第一次识别」到「汇总时刻」——
        中间即使画面一直不变（没产生新的识别），那段时间也是工作时间。

        若直接用 ``rec["_at"]`` 作 now，新建记录的 started_at 与 ended_at
        会落在同一刻 -> 时长 0 秒，整段时间从时间线上消失。

        所以这里取：
        - ``started_at`` = 上一条识别时刻（首条则用自身 _at）
        - ``now``         = 本条识别时刻
        - 最后一条的 now 额外延伸到 ``summary_at``（汇总时刻），
          这样「最后一次识别之后、画面一直没变」的那段尾巴也能计入。

        为避免相邻记录重叠，新建记录时若 ``started_at`` 早于
        「上一条活动段的结束时刻」，则把 ``started_at`` 推到该结束时刻 ——
        时间线应当首尾相接，不该有交集。

        :param summary_at: 本周期的汇总时刻（Unix 秒）。默认取当前时间。
        :return: {recognized, created, merged}
        """
        created = merged = 0
        valid = [rec for rec in buffer if "_error" not in rec and "_at" in rec]
        if not valid:
            return {"recognized": len(buffer), "created": 0, "merged": 0}
        if summary_at is None:
            summary_at = int(datetime.now().timestamp())
        prev_at: Optional[int] = None
        for idx, rec in enumerate(valid):
            at = int(rec["_at"])
            started = prev_at if prev_at is not None else at
            # 最后一条延伸到汇总时刻，覆盖「识别之后画面一直没变」的尾巴
            now = summary_at if (idx == len(valid) - 1 and summary_at > at) else at

            # 新建时避免与上一条记录重叠：起点推到上一条的结束时刻
            if started > now:
                started = now
            latest = self.store.latest_entry()
            if latest and latest.get("activity") != rec["activity"]:
                prev_end = int(latest.get("ended_at") or 0)
                if prev_end > started:
                    started = min(prev_end, now)

            action, _ = aggregate.apply(
                self.store,
                rec["activity"],
                rec["content"],
                rec["category"],
                model=self.llm.model,
                source="screenshot",
                now=now,
                started_at=started,
            )
            if action == "created":
                created += 1
            else:
                merged += 1
            prev_at = at
        return {"recognized": len(buffer), "created": created, "merged": merged}

    # ---------- 核心流程 ----------
    def recognize_once(self, force: bool = False) -> Dict[str, Any]:
        """一次完整流程：截屏 → 识别 → 聚合 → 存储。

        :param force: True 忽略变更检测强制识别（手动触发）
        """
        if force:
            self.capture.reset_hash()

        shot = self.capture.grab(save_to_disk=False)
        if shot.is_empty:
            # 画面没变：跳过识别（省 token），但延长上一条记录
            latest = self.store.latest_entry()
            if latest:
                now = int(datetime.now().timestamp())
                self.store.extend_entry(latest["id"], now, aggregate.MAX_DURATION)
            self.store.insert_skip_event("unchanged")
            return {"skipped": True, "reason": "画面未变化，跳过识别"}

        started = time.perf_counter()
        try:
            text, usage = self.llm.chat_with_image(shot.base64)
            rec = self.llm.parse_recognition(text)
            latency_ms = int((time.perf_counter() - started) * 1000)
            action, eid = aggregate.apply(
                self.store,
                rec["activity"],
                rec["content"],
                rec["category"],
                model=self.llm.model,
                source="screenshot",
            )
            self.store.insert_llm_call({
                "model": self.llm.model,
                "ok": True,
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "latency_ms": latency_ms,
                "images": 1,
            })
            return {"ok": True, "action": action, "entry_id": eid, "recognition": rec}
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self.store.insert_llm_call({
                "model": self.llm.model,
                "ok": False,
                "error": str(exc),
                "latency_ms": latency_ms,
            })
            return {"ok": False, "error": str(exc)}

    # ---------- 日报 ----------
    def generate_report(self, date: Optional[str] = None, with_ai: bool = False) -> str:
        """生成某天的日报。date 为 None 表示今天。"""
        date = date or datetime.now().strftime("%Y-%m-%d")
        entries = self.store.entries_by_day(date)

        ai_analysis = None
        if with_ai and entries:
            from report import aggregate_stats
            stats = aggregate_stats(entries)
            prompt = build_ai_prompt(stats, entries)
            try:
                # 纯文本调用（复用 llm_client 的 OpenAI 兼容路径）
                # trust_env=False：直连官方 API，不被系统代理静默改道
                llm_cfg = self.config.llm_config
                with make_client(timeout=120,
                                 use_system_proxy=self.config.get(
                                     "llm", "use_system_proxy", default=False)) as client:
                    resp = client.post(
                        f"{llm_cfg.get('api_base','').rstrip('/')}/chat/completions",
                        headers={"Authorization": f"Bearer {llm_cfg.get('api_key','')}",
                                 "Content-Type": "application/json"},
                        json={
                            "model": llm_cfg.get("model", "deepseek-flash"),
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.3,
                            "max_tokens": 1024,
                        },
                    )
                if resp.status_code == 200:
                    ai_analysis = resp.json()["choices"][0]["message"]["content"]
                else:
                    print(f"[日报 AI 分析失败] HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:
                print(f"[日报 AI 分析失败] {exc}")
                ai_analysis = None  # AI 失败回退纯模板

        report_md = build_report(entries, date, ai_analysis=ai_analysis)

        # 日报留档：每次生成都存一份到 outputs/reports/YYYY-MM-DD.md
        # （文件留档而非加表：日报是文档，用编辑器直接打开更自然）
        try:
            if self.archive:
                path = self.archive.save(date, report_md)
                if path:
                    log.info("[日报留档] %s", path)
        except Exception as exc:
            log.warning("[日报留档失败] %s", exc)

        # 可选：上报
        if self.uploader.enabled:
            self.uploader.upload_daily_report(date, report_md)

        return report_md

    # ---------- 运行 ----------
    def start(self) -> None:
        """启动 Skill 服务 + 定时截图循环（阻塞）。"""
        # Skill 本地服务
        skill_cfg = self.config.get("skill_server", default={})
        if skill_cfg.get("enabled", True):
            server = SkillServer(
                store=self.store,
                token=skill_cfg.get("token", ""),
                port=int(skill_cfg.get("port", 8088)),
                report_builder=build_report,
                config=self.config,
                settings_callback=self._reload_settings,
                llm_tester=self.llm.test_connection,
                archive=self.archive,
                export_dir=self.export_dir,
            )
            threading.Thread(target=server.run, daemon=True).start()

        # 三层架构循环：密集抓屏 → 变化识别 → 定期聚合
        log.info("诚实日报助手已启动")
        log.info("Web 界面: http://127.0.0.1:%s", skill_cfg.get('port', 8088))
        log.info("抓屏间隔 %ss / 汇总周期 %ss / 模型 %s",
                 self.config.get("capture", "snap_interval", default=15),
                 self.config.get("capture", "interval", default=120),
                 self.config.get("llm", "model", default="deepseek-flash"))

        buffer = []           # 攒本汇总周期内的识别结果
        last_summary = time.time()

        while True:
            snap_interval = int(self.config.get("capture", "snap_interval", default=15))
            summary_interval = int(self.config.get("capture", "interval", default=120))

            # 第一层：密集抓屏 + 变化才识别（省 token）
            try:
                rec = self.capture_and_recognize()
                if rec is not None:
                    if "_error" in rec:
                        log.error("[识别失败] %s", rec['_error'])
                    else:
                        buffer.append(rec)
                        log.info("[识别 %d] %s（%s）", len(buffer),
                                 rec['content'][:60], rec['category'])
                # 画面没变则静默跳过
            except Exception as exc:
                log.exception("[循环异常] %s", exc)

            # 第二层：到汇总周期，把 buffer 聚合成活动段写入
            if time.time() - last_summary >= summary_interval:
                if buffer:
                    # 传入汇总时刻：最后一段活动要延伸到此刻，
                    # 否则「最后一次识别之后画面一直没变」的那段尾巴会丢失。
                    stat = self.summarize_buffer(buffer, summary_at=int(time.time()))
                    log.info("[汇总] 本周期识别 %d 次 → 新建 %d 段 / 合并 %d 段",
                             stat['recognized'], stat['created'], stat['merged'])
                buffer = []
                last_summary = time.time()

            self._maybe_generate_report()
            time.sleep(snap_interval)

    def _maybe_generate_report(self) -> None:
        """到定时日报时间则自动生成。

        用「今日已生成」标记而非分钟字符串精确匹配，避免错过触发窗口
        （程序重启、snap_interval > 60、那一分钟卡顿）导致日报静默丢失。
        """
        trigger = self.config.get("report", "trigger", default="both")
        if trigger not in ("scheduled", "both"):
            return
        scheduled = self.config.get("report", "scheduled_time", default="18:00")
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        if self._last_report_date == today:
            return  # 今天已生成过

        if now.strftime("%H:%M") != scheduled:
            return

        try:
            report_md = self.generate_report()
            self._last_report_date = today
            log.info("===== 工作日报 =====\n%s", report_md)
        except Exception as exc:
            log.exception("[日报生成失败] %s", exc)


def main():
    import copy
    import json
    from pathlib import Path

    # 支持打包成 exe 后运行：config.json 在 exe 同目录，不存在则生成默认
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    config_path = exe_dir / "config.json"
    if not config_path.exists():
        example = Path(__file__).parent / "config.example.json"
        if example.exists():
            default = json.loads(example.read_text(encoding="utf-8"))
        else:
            # 兜底：用 config.DEFAULTS（单一事实来源，避免两处默认值漂移）
            from config import DEFAULTS
            default = copy.deepcopy(DEFAULTS)
        config_path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("已生成默认配置 %s，请填入 API Key 后重新运行", config_path)

    helper = DailyHelper(str(config_path))
    helper.start()


if __name__ == "__main__":
    main()
