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
from capture import Capture
from config import Config
from llm_client import LLMClient
from report import build_report, build_ai_prompt
from skill_server import SkillServer
from store import RecordStore
from upload import ReportUploader


class DailyHelper:
    """诚实日报助手主类。"""

    def __init__(self, config_path: Optional[str] = None):
        self.config = Config(config_path)
        # store 只初始化一次（保持数据库连接稳定）
        self.store = RecordStore(self.config.get("storage", "db_path", default="records.db"))
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
        )

        up_cfg = self.config.get("upload", default={})
        self.uploader = ReportUploader(
            url=up_cfg.get("url", ""),
            token=up_cfg.get("token", ""),
        )

    def _reload_settings(self) -> None:
        """设置更新后重建 capture/llm/uploader（不重建 store）。"""
        self._init_capture_llm()

    # ---------- 密集抓屏三层架构 ----------
    def capture_and_recognize(self) -> Optional[Dict[str, Any]]:
        """抓屏 + 识别（不聚合、不写 entries）。

        密集抓屏循环调用：画面没变返回 None，变化则识别并返回 recognition（含 _at 时间戳）。
        """
        shot = self.capture.grab(save_to_disk=False)
        if shot.is_empty:
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

    def summarize_buffer(self, buffer: list) -> Dict[str, int]:
        """把 buffer 里的识别结果聚合写入 store（相同 activity 自动合并成活动段）。

        :return: {recognized, created, merged}
        """
        created = merged = 0
        for rec in buffer:
            if "_error" in rec:
                continue
            action, _ = aggregate.apply(
                self.store,
                rec["activity"],
                rec["content"],
                rec["category"],
                model=self.llm.model,
                source="screenshot",
                now=rec["_at"],
            )
            if action == "created":
                created += 1
            else:
                merged += 1
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
                # 纯文本调用（复用 llm_client 的 OpenAI 兼容路径，简化用 httpx）
                import httpx
                llm_cfg = self.config.llm_config
                with httpx.Client(timeout=120) as client:
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
            except Exception:
                ai_analysis = None  # AI 失败回退纯模板

        report_md = build_report(entries, date, ai_analysis=ai_analysis)

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
            )
            threading.Thread(target=server.run, daemon=True).start()

        # 三层架构循环：密集抓屏 → 变化识别 → 定期聚合
        print("诚实日报助手已启动")
        print(f"Web 界面: http://127.0.0.1:{skill_cfg.get('port', 8088)}")

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
                        print(f"[识别失败] {rec['_error']}")
                    else:
                        buffer.append(rec)
                        print(f"[识别 {len(buffer)}] {rec['content'][:60]}（{rec['category']}）")
                # 画面没变则静默跳过
            except Exception as exc:
                print(f"[循环异常] {exc}")

            # 第二层：到汇总周期，把 buffer 聚合成活动段写入
            if time.time() - last_summary >= summary_interval:
                if buffer:
                    stat = self.summarize_buffer(buffer)
                    print(f"[汇总] 本周期识别 {stat['recognized']} 次 → 新建 {stat['created']} 段 / 合并 {stat['merged']} 段")
                buffer = []
                last_summary = time.time()

            self._maybe_generate_report()
            time.sleep(snap_interval)

    def _maybe_generate_report(self) -> None:
        """到定时日报时间则自动生成。"""
        trigger = self.config.get("report", "trigger", default="both")
        if trigger not in ("scheduled", "both"):
            return
        scheduled = self.config.get("report", "scheduled_time", default="18:00")
        if datetime.now().strftime("%H:%M") == scheduled:
            try:
                report_md = self.generate_report()
                print(f"\n===== 工作日报 =====\n{report_md}\n")
            except Exception as exc:
                print(f"[日报生成失败] {exc}")


def main():
    import sys
    import json
    from pathlib import Path

    # 支持打包成 exe 后运行：config.json 在 exe 同目录，不存在则生成默认
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    config_path = exe_dir / "config.json"
    if not config_path.exists():
        example = Path(__file__).parent / "config.example.json"
        if example.exists():
            default = json.loads(example.read_text(encoding="utf-8"))
            config_path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已生成默认配置 {config_path}，请填入 API Key 后重新运行")
        else:
            config_path.write_text(json.dumps({
                "llm": {"api_base": "https://api.deepseek.com/v1", "api_key": "", "model": "deepseek-flash"},
                "capture": {"interval": 120, "quality": 60, "save_dir": "", "region_preset": "full"},
                "storage": {"db_path": "records.db"},
                "report": {"trigger": "both", "scheduled_time": "18:00"},
                "upload": {"enabled": False, "url": "", "token": ""},
                "skill_server": {"enabled": True, "port": 8088, "token": ""},
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    helper = DailyHelper(str(config_path))
    helper.start()


if __name__ == "__main__":
    main()
