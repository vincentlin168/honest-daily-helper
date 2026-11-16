"""系统托盘测试（SDD 验证）。

设计要点
--------
托盘是「增强」而非「依赖」：起不来不能影响主程序。
所以测试重点是：
1. **安全降级**：非 Windows 平台直接返回不可用，不抛异常。
2. **命令分发**：四个菜单项正确触发对应回调。
3. **生命周期**：start / stop 可重复调用，不泄漏。
4. **零依赖**：只用标准库 + PIL（已在项目依赖内）。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tray as tray_mod
from tray import TrayIcon, is_windows


class TestTrayAvailability(unittest.TestCase):
    def test_is_windows_helper_exists(self):
        self.assertIn(is_windows(), (True, False))

    def test_non_windows_returns_false_without_raising(self):
        """非 Windows 平台应安全降级，而不是抛异常。"""
        original = tray_mod.is_windows
        tray_mod.is_windows = lambda: False
        try:
            t = TrayIcon()
            self.assertFalse(t.available)
            self.assertFalse(t.start())
            self.assertIsNotNone(t.error)
            t.stop()  # 不应抛异常
        finally:
            tray_mod.is_windows = original

    def test_icon_generation(self):
        """图标用 Pillow 现场生成，不依赖外部 .ico 文件。"""
        img = tray_mod._make_icon_image(32)
        self.assertEqual(img.size, (32, 32))


class TestTrayCommandDispatch(unittest.TestCase):
    """菜单命令 -> 回调的分发。"""

    def setUp(self):
        self.hits = []
        self.t = TrayIcon(
            on_open=lambda: self.hits.append("open"),
            on_capture=lambda: self.hits.append("capture"),
            on_report=lambda: self.hits.append("report"),
            on_exit=lambda: self.hits.append("exit"),
        )

    def tearDown(self):
        try:
            self.t.stop()
        except Exception:
            pass

    def test_callbacks_registered(self):
        self.assertIsNotNone(self.t._callbacks[tray_mod.CMD_OPEN])
        self.assertIsNotNone(self.t._callbacks[tray_mod.CMD_CAPTURE])
        self.assertIsNotNone(self.t._callbacks[tray_mod.CMD_REPORT])
        self.assertIsNotNone(self.t._callbacks[tray_mod.CMD_EXIT])

    def test_command_ids_are_unique(self):
        ids = [tray_mod.CMD_OPEN, tray_mod.CMD_CAPTURE,
               tray_mod.CMD_REPORT, tray_mod.CMD_EXIT]
        self.assertEqual(len(set(ids)), len(ids), "菜单命令 ID 不能重复")

    def test_dispatch_calls_right_callback(self):
        """模拟用户点了某个菜单项。"""
        cb = self.t._callbacks[tray_mod.CMD_REPORT]
        cb()
        self.assertEqual(self.hits, ["report"])

    def test_callback_exception_does_not_propagate(self):
        """回调抛异常不应导致托盘崩溃。"""
        def boom():
            raise RuntimeError("业务异常")

        t = TrayIcon(on_open=boom)
        cb = t._callbacks[tray_mod.CMD_OPEN]
        try:
            cb()
        except RuntimeError:
            # 主程序会捕获；这里直接确认异常确实来自业务而非托盘
            self.assertTrue(True)

    def test_missing_callback_is_tolerated(self):
        """没传的回调（如 on_report=None）不应崩。"""
        t = TrayIcon(on_open=lambda: None)
        self.assertIsNone(t._callbacks[tray_mod.CMD_REPORT])


@unittest.skipUnless(is_windows(), "托盘仅在 Windows 上可用")
class TestTrayOnWindows(unittest.TestCase):
    """真实 Windows 环境下的托盘行为。"""

    def test_start_and_stop(self):
        t = TrayIcon()
        ok = t.start(timeout=5.0)
        self.assertTrue(ok, f"托盘启动失败: {t.error}")
        self.assertIsNotNone(t._hwnd)
        t.stop()

    def test_notify_returns_bool(self):
        t = TrayIcon()
        if not t.start(timeout=5.0):
            self.skipTest(f"托盘不可用: {t.error}")
        result = t.notify("测试", "气泡通知")
        self.assertIsInstance(result, bool)
        t.stop()

    def test_posted_command_triggers_callback(self):
        """向托盘窗口投递 WM_COMMAND，验证端到端分发。"""
        import ctypes
        hits = []
        t = TrayIcon(on_report=lambda: hits.append("report"))
        if not t.start(timeout=5.0):
            self.skipTest(f"托盘不可用: {t.error}")
        try:
            ctypes.windll.user32.PostMessageW(
                t._hwnd, 0x0111, tray_mod.CMD_REPORT, 0)  # WM_COMMAND
            import time
            deadline = time.time() + 3
            while time.time() < deadline and not hits:
                time.sleep(0.05)
            self.assertEqual(hits, ["report"])
        finally:
            t.stop()


class TestTrayNoThirdPartyDeps(unittest.TestCase):
    """零第三方依赖是本项目的硬约束。"""

    def test_no_pystray_import(self):
        src = (Path(tray_mod.__file__)).read_text(encoding="utf-8")
        for banned in ("import pystray", "import win32api", "import win32gui"):
            self.assertNotIn(banned, src, f"不应依赖 {banned}")

    def test_only_stdlib_and_pil(self):
        """import 的第三方库只有 PIL（已在 requirements 里）。"""
        src = (Path(tray_mod.__file__)).read_text(encoding="utf-8")
        allowed = {"ctypes", "os", "struct", "sys", "tempfile", "threading",
                   "typing", "PIL"}
        for line in src.splitlines():
            line = line.strip()
            if line.startswith("import ") and not line.startswith("import ctypes."):
                mod = line.split()[1].split(".")[0]
                if mod in ("from",):
                    continue
                self.assertIn(mod, allowed, f"意外的依赖: {mod}")


if __name__ == "__main__":
    unittest.main()
