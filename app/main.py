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

import os
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional

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
from logging_setup import get_logger
from llm_client import LLMClient
from llm_client import PRIVACY_PRIVATE, PRIVACY_WORK
from onboarding import Onboarding
from report import build_report, build_ai_prompt
from skill_server import SkillServer
from store import RecordStore
from upload import ReportUploader

log = get_logger()

# 私人模式下的保底复核间隔（秒）：
# 识别出私人场景后，即使画面一直不变，也每隔这么久强制问一次模型
# 「现在还是私人场景吗」，以便用户切回工作后能及时恢复记录。
# 取 60 秒是权衡：太短会白白烧 token，太长会漏记切回工作后的时间。
PRIVATE_RECHECK_INTERVAL = 60

# 私人场景处理方式（配置 privacy.mode）
PRIVACY_MODE_EXCLUDE = "exclude"          # 完全排除：不写库、不计入工作时长
PRIVACY_MODE_DESENSITIZE = "desensitize"  # 仅脱敏：写一条占位记录
PRIVACY_MODE_OFF = "off"                  # 不过滤


class DailyHelper:
    """诚实日报助手主类。"""

    # 类级默认值：让 ``DailyHelper.__new__(DailyHelper)`` 这种
    # 绕过 __init__ 构造的用法（测试里常见）也能安全访问这些属性。
    # 隐私相关状态在这里兜底为「正常工作模式」—— 最保守的选择。
    _private_mode = False
    _last_private_check_ts: Optional[int] = None

    def __init__(self, config_path: Optional[str] = None):
        self.config = Config(config_path)
        # store 只初始化一次（保持数据库连接稳定）
        self.store = RecordStore(self.config.get("storage", "db_path", default="records.db"))
        self._last_report_date: Optional[str] = None
        # 日报留档目录：优先配置，默认 app/outputs/reports
        archive_dir = self.config.get("storage", "archive_dir", default="outputs/reports")
        self.archive = ReportArchive(archive_dir)
        self.export_dir = self.config.get("storage", "export_dir", default="exports")
        # 首次运行引导
        self.onboarding = Onboarding(self.config)
        self.tray = None
        # 私人模式状态：一旦模型判定为 private，后续「画面未变」时
        # **不能**延长上一条工作记录 —— 否则私人时段仍会被算成工作时长。
        # 详见 _extend_current_activity 的 docstring。
        self._private_mode = False
        self._last_private_check_ts: Optional[int] = None
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
            auth_type=llm_cfg.get("auth_type", "") or "",
            api_username=llm_cfg.get("api_username", "") or "",
            api_password=llm_cfg.get("api_password", "") or "",
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

        私人模式下的例外
        ----------------
        模型判定当前是私人场景（``privacy="private"``）后，这段时间
        **不应该**计入工作时长。如果这里照常延长上一条工作记录，
        那么「切出去聊 10 分钟私人微信」会被算成 10 分钟工作 ——
        恰恰违背「诚实不记录私人信息」的初衷。

        所以私人模式下只记一条 skip_event 留痕，**不延长**任何工作记录。
        """
        if self._private_mode:
            # 私人时段：不碰工作记录，只留痕（可回溯「这段没记录是因为私人场景」）
            self.store.insert_skip_event("private")
            return
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

    def _make_llm_tester(self) -> Callable[[], Dict[str, Any]]:
        """构造「连通性测试」回调。

        必须返回一个**每次调用都取当前实例**的可调用对象，而不是直接绑定
        ``self.llm.test_connection``。

        为什么
        ------
        直接绑定绑定方法会把「服务启动那一刻」的 LLMClient 实例写死。
        用户在设置页改掉 api_key / api_base 后：

        * 识别循环走 ``self.llm``（已被 ``_reload_settings()`` 重建）-> 用新凭据，正常
        * 「测试连接」走旧实例 -> 用旧凭据，报 401

        表现为**假阴性**：Key 明明改对了，测试连接却仍说连不上，
        用户只能靠重启进程解决。lambda 每次求值都读当前的 ``self.llm``，
        因此凭据热更新后测试连接立即反映真实状态。
        """
        return lambda: self.llm.test_connection()

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

        私人场景的处理
        --------------
        模型判定为 ``privacy="private"`` 时，这里**直接丢弃**该识别结果
        （返回 ``None``），并置上 ``self._private_mode`` ——
        之后「画面未变」也不会延长上一条工作记录，
        这样私人时段既不留下记录，也不计入工作时长。

        丢弃动作仍然写一条 ``skip_event(kind="private")`` 留痕：
        「这段时间没记录」必须可解释，否则用户会以为程序漏记了。

        私人模式如何解除
        ----------------
        识别出私人场景后，画面若一直不变就不会再触发识别，
        「私人模式」也就永远解不开 —— 切回工作后那段工作时间会漏记。

        所以私人模式下有一个**保底强制识别**机制：
        每隔 ``PRIVATE_RECHECK_INTERVAL`` 秒（默认 60 秒），
        即使画面未变也强制送一次模型确认。
        这样最多损失 1 分钟的私人模式判定，
        换来「切回工作立刻恢复记录」。
        """
        shot = self.capture.grab(save_to_disk=False)
        if shot.is_empty:
            # 画面未变：跳过识别（省 token），但要延长上一条记录 + 留痕
            if self._private_mode and self._private_recheck_due():
                # 私人模式保底复核：画面虽未变，但距上次确认已过一段时间，
                # 强制问一次模型「现在还是私人场景吗」，以便及时解除私人模式。
                # reset_hash 让下一次 grab 一定返回画面（绕过变更检测）。
                self.capture.reset_hash()
                return self._recognize_shot(self.capture.grab(save_to_disk=False))
            self._extend_current_activity()
            if not self._private_mode:
                self.store.insert_skip_event("unchanged")
            return None

        return self._recognize_shot(shot)

    def _private_recheck_due(self) -> bool:
        """私人模式下的保底复核：距上次确认是否已超过间隔。"""
        if self._last_private_check_ts is None:
            return True
        return (int(datetime.now().timestamp()) - self._last_private_check_ts) >= PRIVATE_RECHECK_INTERVAL

    def _privacy_mode(self) -> str:
        """读取隐私处理方式：exclude / desensitize / off。

        没有 config 的场景（测试用 ``__new__`` 构造）默认用最严格的 exclude。
        """
        if self.config is None:
            return PRIVACY_MODE_EXCLUDE
        mode = self.config.get("privacy", "mode", default=PRIVACY_MODE_EXCLUDE)
        mode = str(mode or "").strip().lower()
        if mode not in (PRIVACY_MODE_EXCLUDE, PRIVACY_MODE_DESENSITIZE, PRIVACY_MODE_OFF):
            return PRIVACY_MODE_EXCLUDE
        return mode

    def _privacy_extra_keywords(self) -> list:
        """用户配置的额外屏蔽关键词。"""
        if self.config is None:
            return []
        kws = self.config.get("privacy", "extra_keywords", default=[]) or []
        if isinstance(kws, str):
            kws = [line.strip() for line in kws.splitlines()]
        return [str(k).strip() for k in kws if str(k).strip()]

    def _hits_extra_keyword(self, rec: Dict[str, Any]) -> bool:
        """识别结果是否命中用户配置的屏蔽关键词。

        关键词在 **activity + content** 上匹配（模型输出的描述里会提到
        画面中的应用/场景）。命中即按私人场景处理。

        这是模型判定之外的**第二道保险**：用户可以自己指定
        「看到 XXX 就别记」，不必完全依赖模型对场景的判断。
        """
        kws = self._privacy_extra_keywords()
        if not kws:
            return False
        haystack = f"{rec.get('activity', '')} {rec.get('content', '')}"
        return any(kw in haystack for kw in kws)

    def _recognize_shot(self, shot) -> Optional[Dict[str, Any]]:
        """把一张截图送去识别，并按 privacy 字段决定「记录 / 丢弃」。

        这是识别逻辑的唯一出口 —— 密集抓屏循环与私人模式保底复核都走这里，
        保证两条路径的隐私处理**完全一致**（不会出现一条记录私人、一条不记录）。

        :return: 识别结果 dict（带 _at）；私人场景返回 None；出错返回 {"_error": ...}
        """
        if shot is None or shot.is_empty:
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

            # 私人场景：按配置决定「完全排除」「仅脱敏」还是「不处理」
            mode = self._privacy_mode()
            is_private = (rec.get("privacy") == PRIVACY_PRIVATE
                          or self._hits_extra_keyword(rec))
            if is_private and mode != PRIVACY_MODE_OFF:
                self._last_private_check_ts = int(datetime.now().timestamp())
                if mode == PRIVACY_MODE_EXCLUDE:
                    self._private_mode = True
                    self.store.insert_skip_event("private")
                    log.info("[私人场景] 已忽略，不写入时间线")
                    return None
                # desensitize：写一条脱敏占位记录（私人时段仍计入工作时长）
                log.info("[私人场景] 已脱敏，写入占位记录")
                rec = {
                    "activity": "私人场景",
                    "content": "私人场景，已脱敏",
                    "category": "其他",
                    "privacy": PRIVACY_WORK,  # 占位记录本身要能正常写入
                    "_at": rec.get("_at"),
                }

            # 回到工作场景：解除私人模式，之后的画面未变可以正常延长工作记录
            self._private_mode = False
            self._last_private_check_ts = int(datetime.now().timestamp())
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
        # 过滤掉私人场景：即使某条 private 混进了 buffer，也不能写库
        valid = [rec for rec in buffer
                 if "_error" not in rec and "_at" in rec
                 and rec.get("privacy") != PRIVACY_PRIVATE]
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

        手动触发路径（托盘「立即识别」）。识别与隐私判定走
        ``_recognize_shot``，与自动循环**共用同一套逻辑**。

        :param force: True 忽略变更检测强制识别（手动触发）
        """
        if force:
            self.capture.reset_hash()

        shot = self.capture.grab(save_to_disk=False)
        if shot.is_empty:
            # 画面没变：跳过识别（省 token），但延长上一条记录
            if self._private_mode:
                # 私人时段：不延长工作记录
                self.store.insert_skip_event("private")
            else:
                latest = self.store.latest_entry()
                if latest:
                    now = int(datetime.now().timestamp())
                    self.store.extend_entry(latest["id"], now, aggregate.MAX_DURATION)
                self.store.insert_skip_event("unchanged")
            return {"skipped": True, "reason": "画面未变化，跳过识别"}

        rec = self._recognize_shot(shot)
        if rec is None:
            # 私人场景：已丢弃，未写入任何记录
            return {"ok": True, "action": "skipped_private", "entry_id": None,
                    "reason": "私人场景，未写入时间线"}
        if "_error" in rec:
            return {"ok": False, "error": rec["_error"]}

        action, eid = aggregate.apply(
            self.store,
            rec["activity"],
            rec["content"],
            rec["category"],
            model=self.llm.model,
            source="screenshot",
        )
        return {"ok": True, "action": action, "entry_id": eid, "recognition": rec}

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
                # 认证方式（Bearer / Basic / URL 内嵌 user:pass）由 LLMClient 统一处理
                ai_analysis = self.llm.chat_text(prompt, max_tokens=1024)
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
    def _open_panel(self) -> None:
        """打开 Web 面板（托盘菜单「打开面板」）。"""
        url = f"http://127.0.0.1:{self.config.get('skill_server', 'port', default=8088)}"
        try:
            os.startfile(url)  # noqa: S606  (Windows 官方 API)
        except Exception as exc:
            log.warning("[打开面板失败] %s", exc)

    def _capture_now(self) -> None:
        """立即识别一次（托盘菜单「立即识别」）。"""
        try:
            rec = self.capture_and_recognize()
            if rec is None:
                log.info("[手动识别] 画面未变化，未消耗 token")
            elif "_error" in rec:
                log.error("[手动识别失败] %s", rec["_error"])
            else:
                log.info("[手动识别] %s（%s）", rec.get("content", "")[:60],
                         rec.get("category", ""))
        except Exception as exc:
            log.error("[手动识别异常] %s", exc)

    def _generate_report_now(self) -> None:
        """立即生成今日日报（托盘菜单「生成日报」）。"""
        try:
            md = self.generate_report()
            log.info("===== 工作日报 =====\n%s", md)
        except Exception as exc:
            log.error("[日报生成失败] %s", exc)

    def _start_tray(self) -> None:
        """启动系统托盘图标（失败不影响主流程）。"""
        if not self.config.get("tray", "enabled", default=True):
            return
        try:
            from tray import TrayIcon
        except Exception as exc:
            log.warning("[托盘不可用] %s", exc)
            return
        self.tray = TrayIcon(
            title="诚实日报助手",
            on_open=self._open_panel,
            on_capture=self._capture_now,
            on_report=self._generate_report_now,
            on_exit=self._exit_requested,
        )
        if self.tray.start():
            log.info("系统托盘已启动（右键图标：打开面板 / 立即识别 / 生成日报 / 退出）")
        else:
            log.warning("[托盘启动失败] %s", self.tray.error)
            self.tray = None

    def _exit_requested(self) -> None:
        """托盘菜单「退出」：停掉托盘并结束进程。"""
        log.info("收到退出指令，正在停止…")
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        # 结束主循环 + 进程
        os._exit(0)

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
                # 用 _make_llm_tester() 而非直接绑定 self.llm.test_connection：
                # 直接绑定会把「启动那一刻」的 LLMClient 实例写死，设置页改了
                # api_key/api_base 后，识别循环用的是新客户端、而「测试连接」
                # 仍在用旧客户端的旧凭据 —— 表现为假阴性（Key 明明改对了，
                # 测试连接却仍报 401）。详见 _make_llm_tester 的 docstring。
                llm_tester=self._make_llm_tester(),
                archive=self.archive,
                export_dir=self.export_dir,
                onboarding=self.onboarding,
            )
            threading.Thread(target=server.run, daemon=True).start()

        # 系统托盘
        self._start_tray()

        # 三层架构循环：密集抓屏 → 变化识别 → 定期聚合
        log.info("诚实日报助手已启动")
        log.info("Web 界面: http://127.0.0.1:%s", skill_cfg.get('port', 8088))
        log.info("抓屏间隔 %ss / 汇总周期 %ss / 模型 %s",
                 self.config.get("capture", "snap_interval", default=15),
                 self.config.get("capture", "interval", default=120),
                 self.config.get("llm", "model", default="deepseek-flash"))

        # 首次运行引导提示
        if not self.onboarding.is_completed():
            log.info("首次运行：请打开 http://127.0.0.1:%s 完成引导配置（填 API Key 等）",
                     skill_cfg.get('port', 8088))

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
