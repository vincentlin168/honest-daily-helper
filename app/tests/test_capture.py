"""capture 模块的测试（SDD 验证，标准库 unittest）。

覆盖点：区域预设换算、指纹稳定性、Screenshot 编码、无图形环境容错。
纯逻辑测试，不依赖真实屏幕。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from capture import Capture, Screenshot


def make_image(w=64, h=64, color=(255, 0, 0)):
    return Image.new("RGB", (w, h), color)


class TestRegionPreset(unittest.TestCase):
    def setUp(self):
        self.cap = Capture()

    def test_full_is_none(self):
        self.assertIsNone(self.cap.resolve_region_preset("full"))

    def test_left_half(self):
        mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        r = self.cap.resolve_region_preset("left_half", mon)
        self.assertEqual(r, [0, 0, 960, 1080])

    def test_right_half(self):
        mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        r = self.cap.resolve_region_preset("right_half", mon)
        self.assertEqual(r, [960, 0, 960, 1080])

    def test_center(self):
        mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        r = self.cap.resolve_region_preset("center", mon)
        self.assertEqual(r, [360, 140, 1200, 800])


class TestFingerprint(unittest.TestCase):
    def test_same_image_same_fingerprint(self):
        img1 = make_image(color=(10, 20, 30))
        img2 = make_image(color=(10, 20, 30))
        self.assertEqual(Capture.fingerprint(img1), Capture.fingerprint(img2))

    def test_diff_image_diff_fingerprint(self):
        img1 = make_image(color=(10, 20, 30))
        img2 = make_image(color=(40, 50, 60))
        self.assertNotEqual(Capture.fingerprint(img1), Capture.fingerprint(img2))


class TestScreenshot(unittest.TestCase):
    def test_empty(self):
        s = Screenshot()
        self.assertTrue(s.is_empty)
        self.assertEqual(s.base64, "")

    def test_base64(self):
        import base64
        data = b"\x89PNG-test"
        s = Screenshot(data=data)
        self.assertEqual(s.base64, base64.b64encode(data).decode("utf-8"))


class TestDisplayDetection(unittest.TestCase):
    def test_has_display_returns_bool(self):
        # 无图形环境下应返回 False 而不抛异常（mss 惰性初始化）
        cap = Capture()
        result = cap.has_display()
        self.assertIn(result, (True, False))


class TestChangeDetection(unittest.TestCase):
    """变更检测：用假截图源驱动，不依赖真实屏幕。"""

    class _FakeCapture(Capture):
        def __init__(self, images):
            super().__init__()
            self._images = list(images)
            self._idx = 0

        def _grab_image(self):
            img = self._images[self._idx % len(self._images)]
            self._idx += 1
            return img

    def test_unchanged_screen_returns_empty(self):
        img = make_image(color=(10, 20, 30))
        cap = self._FakeCapture([img, img])
        self.assertFalse(cap.grab().is_empty)          # 第一次：有画面
        self.assertTrue(cap.grab().is_empty)           # 第二次：画面未变 → 跳过

    def test_changed_screen_returns_data(self):
        cap = self._FakeCapture([
            make_image(color=(10, 20, 30)),
            make_image(color=(200, 30, 40)),
        ])
        self.assertFalse(cap.grab().is_empty)
        self.assertFalse(cap.grab().is_empty)          # 画面变了 → 继续识别

    def test_save_to_disk_still_updates_fingerprint(self):
        """回归：save_to_disk=True 时也必须更新指纹。

        旧实现只在 save_to_disk=False 分支更新 _last_hash，
        导致落盘路径下指纹状态陈旧，后续变更检测不可靠。
        """
        img = make_image(color=(10, 20, 30))
        cap = self._FakeCapture([img, img])
        cap.grab(save_to_disk=True)                    # 落盘，并应更新指纹
        self.assertTrue(cap.grab(save_to_disk=False).is_empty)  # 画面未变 → 跳过


if __name__ == "__main__":
    unittest.main()
