"""私人场景抑制的测试（SDD 验证）。

覆盖点
------
1. **提示词**：要求模型输出机器可读的 privacy 字段（三态）。
2. **解析**：``parse_recognition`` 正确解析并规范化 privacy。
3. **执行**：模型判定 private 时，程序**不写 entries**，且**不计入工作时长**。
4. **解除**：切回工作后能恢复记录（保底复核机制）。
5. **防漏**：private 混进 buffer 时也不得写入。

设计要点（为什么要"提示词 + 代码"两层）
----------------------------------------
只靠提示词不够：模型可能漏判或写错字段。只靠代码也不够：
代码不知道画面是不是私人场景。两层配合才可靠 ——
提示词负责判断，代码负责**强制执行**（模型说不记，就一定不写库）。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capture import Capture, Screenshot
from config import Config
from llm_client import (LLMClient, PRIVACY_PARTIAL, PRIVACY_PRIVATE,
                         PRIVACY_WORK, build_system_prompt, normalize_privacy)
from main import (DailyHelper, PRIVACY_MODE_DESENSITIZE, PRIVACY_MODE_EXCLUDE,
                  PRIVACY_MODE_OFF)
from store import RecordStore


class _FakeCapture(Capture):
    """可编程的假截屏器。"""

    def __init__(self, script):
        super().__init__(save_dir=None, quality=60)
        self.script = list(script)
        self.grabs = 0

    def grab(self, save_to_disk: bool = False) -> Screenshot:
        self.grabs += 1
        want_changed = self.script.pop(0) if self.script else False
        if want_changed:
            self.reset_hash()
            return Screenshot(data=b"\xff\xd8fake", width=100, height=100, size_kb=1)
        return Screenshot()


class _FakeLLM:
    """最小 LLM 替身：按脚本返回预设的模型输出（纯文本）。"""

    model = "test-model"

    def __init__(self, responses=None):
        # responses 是「模型输出的纯文本」列表
        self.responses = list(responses or [])
        self.calls = 0

    def chat_with_image(self, image_base64):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0), {"prompt_tokens": 10, "completion_tokens": 10}
        return ('{"activity": "写代码", "content": "写代码", "category": "代码开发"}',
                {"prompt_tokens": 10, "completion_tokens": 10})

    def parse_recognition(self, text):
        return LLMClient.parse_recognition(text)


def _helper(tmp):
    """造一个只连数据库、不连 LLM 的 DailyHelper（绕过 __init__）。"""
    helper = DailyHelper.__new__(DailyHelper)
    helper.store = RecordStore(str(Path(tmp) / "t.db"))
    helper.archive = None
    helper.uploader = None
    helper._last_report_date = None
    helper.capture = Capture(save_dir=None, quality=60)
    helper.llm = _FakeLLM()
    helper.config = Config(str(Path(tmp) / "config.json"))
    return helper


# ---------- 提示词 ----------

class TestPrivacyPrompt(unittest.TestCase):
    def test_prompt_requires_privacy_field(self):
        p = build_system_prompt()
        self.assertIn("privacy", p)

    def test_prompt_explains_three_states(self):
        p = build_system_prompt()
        for token in ("work", "partial", "private"):
            self.assertIn(token, p)

    def test_prompt_lists_private_scenarios(self):
        """提示词必须明确列出私人场景，否则模型无从判断。"""
        p = build_system_prompt()
        for kw in ("私人聊天", "娱乐", "购物", "朋友圈"):
            self.assertIn(kw, p)

    def test_prompt_says_private_is_discarded(self):
        """必须告诉模型 private 会被丢弃 —— 模型才有动力如实标注。"""
        p = build_system_prompt()
        self.assertIn("丢弃", p)


# ---------- privacy 规范化 ----------

class TestNormalizePrivacy(unittest.TestCase):
    def test_none_defaults_to_work(self):
        self.assertEqual(normalize_privacy(None), PRIVACY_WORK)

    def test_empty_defaults_to_work(self):
        self.assertEqual(normalize_privacy(""), PRIVACY_WORK)

    def test_exact_values(self):
        self.assertEqual(normalize_privacy("work"), PRIVACY_WORK)
        self.assertEqual(normalize_privacy("partial"), PRIVACY_PARTIAL)
        self.assertEqual(normalize_privacy("private"), PRIVACY_PRIVATE)

    def test_case_insensitive(self):
        self.assertEqual(normalize_privacy("PRIVATE"), PRIVACY_PRIVATE)
        self.assertEqual(normalize_privacy("Private"), PRIVACY_PRIVATE)

    def test_chinese_alias(self):
        self.assertEqual(normalize_privacy("私人"), PRIVACY_PRIVATE)

    def test_contains_private_is_strict(self):
        """从严：任何含 private 的写法都必须判为私人，不能漏判。"""
        self.assertEqual(normalize_privacy("private_chat"), PRIVACY_PRIVATE)
        self.assertEqual(normalize_privacy("这是私人内容"), PRIVACY_PRIVATE)

    def test_unknown_defaults_to_work(self):
        """未知值不得误杀工作记录。"""
        self.assertEqual(normalize_privacy("whatever"), PRIVACY_WORK)


# ---------- 解析 ----------

class TestParseRecognitionPrivacy(unittest.TestCase):
    def test_parses_private(self):
        r = LLMClient.parse_recognition(
            '{"activity": "私人活动", "content": "私人场景，不记录", '
            '"category": "其他", "privacy": "private"}')
        self.assertEqual(r["privacy"], PRIVACY_PRIVATE)

    def test_parses_partial(self):
        r = LLMClient.parse_recognition(
            '{"activity": "开会", "content": "讨论项目排期", '
            '"category": "项目管理", "privacy": "partial"}')
        self.assertEqual(r["privacy"], PRIVACY_PARTIAL)

    def test_missing_privacy_defaults_to_work(self):
        """老模型（不认识 privacy 字段）不能导致记录丢失。"""
        r = LLMClient.parse_recognition(
            '{"activity": "写代码", "content": "写代码", "category": "代码开发"}')
        self.assertEqual(r["privacy"], PRIVACY_WORK)

    def test_plain_text_fallback_has_privacy(self):
        r = LLMClient.parse_recognition("这不是 JSON")
        self.assertEqual(r["privacy"], PRIVACY_WORK)


# ---------- 执行：private 不写库 ----------

class TestPrivateNotStored(unittest.TestCase):
    """核心：模型判定 private 时，数据库里不能出现这条记录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _helper(self.tmp)

    def tearDown(self):
        self.helper.store.close()

    def test_private_recognition_not_written_to_entries(self):
        private_json = ('{"activity": "私人活动", "content": "私人场景，不记录", '
                        '"category": "其他", "privacy": "private"}')
        self.helper.llm = _FakeLLM([private_json])

        with patch.object(self.helper, "capture", _FakeCapture([True])):
            rec = self.helper.capture_and_recognize()

        self.assertIsNone(rec, "私人场景不应返回识别结果")
        entries = self.helper.store.entries_by_day("2026-01-01")
        self.assertEqual(len(entries), 0, "私人场景不得写入 entries")

    def test_private_records_skip_event_for_audit(self):
        """必须留痕 —— 「没记录」要可解释，否则用户以为程序漏记。"""
        private_json = ('{"activity": "私人活动", "content": "私人场景，不记录", '
                        '"category": "其他", "privacy": "private"}')
        self.helper.llm = _FakeLLM([private_json])

        with patch.object(self.helper, "capture", _FakeCapture([True])):
            self.helper.capture_and_recognize()

        with self.helper.store._lock:
            kinds = [r["kind"] for r in self.helper.store._conn.execute(
                "SELECT kind FROM skip_events").fetchall()]
        self.assertIn("private", kinds)

    def test_work_recognition_still_written(self):
        """回归：正常工作必须照常记录，不能被隐私逻辑误伤。"""
        work_json = ('{"activity": "写代码", "content": "写代码", '
                     '"category": "代码开发", "privacy": "work"}')
        self.helper.llm = _FakeLLM([work_json])

        with patch.object(self.helper, "capture", _FakeCapture([True])):
            rec = self.helper.capture_and_recognize()

        self.assertIsNotNone(rec)
        self.assertEqual(rec["privacy"], PRIVACY_WORK)


# ---------- 执行：private 不计入工作时长 ----------

class TestPrivateNotCountedAsWorkTime(unittest.TestCase):
    """私人时段不能被算成工作时长。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _helper(self.tmp)

    def tearDown(self):
        self.helper.store.close()

    def test_unchanged_screen_does_not_extend_work_entry(self):
        """私人模式下「画面未变」不能延长上一条工作记录。"""
        import time
        now = int(time.time())
        eid = self.helper.store.insert_entry({
            "started_at": now - 100, "ended_at": now - 100,
            "activity": "写代码", "content": "写代码", "category": "代码开发",
        })
        before = self.helper.store.get_entry(eid)["ended_at"]

        # 先进入私人模式
        private_json = ('{"activity": "私人活动", "content": "私人场景，不记录", '
                        '"category": "其他", "privacy": "private"}')
        self.helper.llm = _FakeLLM([private_json])
        with patch.object(self.helper, "capture", _FakeCapture([True])):
            self.helper.capture_and_recognize()
        self.assertTrue(self.helper._private_mode)

        # 私人模式下画面未变 -> 不得延长工作记录
        with patch.object(self.helper, "capture", _FakeCapture([False])):
            with patch.object(self.helper, "llm"):
                self.helper.capture_and_recognize()

        after = self.helper.store.get_entry(eid)["ended_at"]
        self.assertEqual(after, before, "私人时段不应延长工作记录")


# ---------- 解除私人模式 ----------

class TestPrivateModeReleased(unittest.TestCase):
    """切回工作后要能恢复记录，否则会漏记工作时间。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _helper(self.tmp)

    def tearDown(self):
        self.helper.store.close()

    def test_work_after_private_clears_flag(self):
        work_json = ('{"activity": "写代码", "content": "写代码", '
                     '"category": "代码开发", "privacy": "work"}')
        self.helper.llm = _FakeLLM([work_json])

        with patch.object(self.helper, "capture", _FakeCapture([True])):
            self.helper.capture_and_recognize()

        self.assertFalse(self.helper._private_mode)

    def test_private_recheck_due_after_interval(self):
        """私人模式下的保底复核：超时后应触发强制识别。"""
        import time
        self.helper._private_mode = True
        self.helper._last_private_check_ts = int(time.time()) - 999
        self.assertTrue(self.helper._private_recheck_due())

    def test_private_recheck_not_due_within_interval(self):
        import time
        self.helper._private_mode = True
        self.helper._last_private_check_ts = int(time.time())
        self.assertFalse(self.helper._private_recheck_due())


# ---------- 防漏：buffer 里的 private ----------

class TestPrivateFilteredFromBuffer(unittest.TestCase):
    """即使 private 混进了 buffer，汇总时也必须过滤掉。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _helper(self.tmp)

    def tearDown(self):
        self.helper.store.close()

    def test_summarize_filters_private(self):
        now = int(__import__("time").time())
        buffer = [
            {"activity": "私人活动", "content": "私人场景，不记录",
             "category": "其他", "privacy": PRIVACY_PRIVATE, "_at": now},
            {"activity": "写代码", "content": "写代码",
             "category": "代码开发", "privacy": PRIVACY_WORK, "_at": now + 1},
        ]
        stat = self.helper.summarize_buffer(buffer, summary_at=now + 2)

        self.assertEqual(stat["created"], 1, "只应写入 1 条工作记录")
        entries = self.helper.store.entries_by_day(
            __import__("datetime").datetime.fromtimestamp(now).strftime("%Y-%m-%d"))
        for e in entries:
            self.assertNotEqual(e["activity"], "私人活动",
                                "私人场景不得出现在 entries 里")


# ---------- 处理方式：exclude / desensitize / off ----------

class TestPrivacyModeSwitch(unittest.TestCase):
    """用户可选择私人场景的处理方式。"""

    PRIVATE_JSON = ('{"activity": "私人活动", "content": "私人场景，不记录", '
                    '"category": "其他", "privacy": "private"}')

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _helper(self.tmp)

    def tearDown(self):
        self.helper.store.close()

    def _run(self):
        self.helper.llm = _FakeLLM([self.PRIVATE_JSON])
        with patch.object(self.helper, "capture", _FakeCapture([True])):
            return self.helper.capture_and_recognize()

    def test_default_is_exclude(self):
        """默认必须是最严格的 exclude。"""
        self.assertEqual(self.helper._privacy_mode(), PRIVACY_MODE_EXCLUDE)

    def test_exclude_writes_nothing(self):
        self.helper.config.set("privacy", "mode", PRIVACY_MODE_EXCLUDE)
        rec = self._run()
        self.assertIsNone(rec)
        self.assertEqual(len(self.helper.store.entries_by_day("2026-01-01")), 0)

    def test_desensitize_writes_placeholder(self):
        """仅脱敏模式：写一条占位记录，但内容必须是脱敏后的，不能带原始内容。"""
        self.helper.config.set("privacy", "mode", PRIVACY_MODE_DESENSITIZE)
        rec = self._run()
        self.assertIsNotNone(rec)
        self.assertEqual(rec["content"], "私人场景，已脱敏")
        self.assertEqual(rec["activity"], "私人场景")

    def test_off_keeps_original_content(self):
        """不过滤模式：保留模型原始输出。"""
        self.helper.config.set("privacy", "mode", PRIVACY_MODE_OFF)
        rec = self._run()
        self.assertIsNotNone(rec)
        self.assertEqual(rec["activity"], "私人活动")

    def test_invalid_mode_falls_back_to_exclude(self):
        """非法配置必须回退到最严格模式，不能放行。"""
        self.helper.config.set("privacy", "mode", "bogus")
        self.assertEqual(self.helper._privacy_mode(), PRIVACY_MODE_EXCLUDE)


# ---------- 额外屏蔽关键词 ----------

class TestExtraKeywords(unittest.TestCase):
    """用户自定义屏蔽关键词是模型判定之外的第二道保险。"""

    WORK_JSON = ('{"activity": "刷抖音", "content": "刷短视频消遣", '
                 '"category": "其他", "privacy": "work"}')

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.helper = _helper(self.tmp)

    def tearDown(self):
        self.helper.store.close()

    def test_keyword_hit_is_excluded(self):
        """模型说是 work，但命中关键词 -> 仍按私人处理。"""
        self.helper.config.set("privacy", "extra_keywords", ["抖音"])
        self.helper.llm = _FakeLLM([self.WORK_JSON])
        with patch.object(self.helper, "capture", _FakeCapture([True])):
            rec = self.helper.capture_and_recognize()

        self.assertIsNone(rec, "命中屏蔽关键词的记录不应写入")
        self.assertEqual(len(self.helper.store.entries_by_day("2026-01-01")), 0)

    def test_no_keyword_hit_is_recorded(self):
        """不命中则正常记录。"""
        self.helper.config.set("privacy", "extra_keywords", ["游戏"])
        self.helper.llm = _FakeLLM([self.WORK_JSON])
        with patch.object(self.helper, "capture", _FakeCapture([True])):
            rec = self.helper.capture_and_recognize()

        self.assertIsNotNone(rec)

    def test_empty_keywords_allows_all(self):
        self.helper.config.set("privacy", "extra_keywords", [])
        self.assertEqual(self.helper._privacy_extra_keywords(), [])


if __name__ == "__main__":
    unittest.main()
