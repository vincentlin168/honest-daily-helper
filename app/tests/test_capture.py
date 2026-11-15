"""capture 模块的测试（SDD 验证，标准库 unittest）。

覆盖点：区域预设换算、指纹稳定性、Screenshot 编码、无图形环境容错。
纯逻辑测试，不依赖真实屏幕。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from capture import Capture, Screenshot, REGION_PRESETS


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


if __name__ == "__main__":
    unittest.main()
