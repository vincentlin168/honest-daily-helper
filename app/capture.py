"""屏幕捕获与内容变更检测（Spec-Driven）。

Spec
----
将屏幕画面实时转为截图，并用像素指纹做「变更检测」——画面没变就跳过，
这是省视觉模型 token 的关键设计。

设计要点：
1. **截屏用 Pillow ImageGrab**（Windows/macOS 可用，无需 mss 额外依赖）。
2. **blake2b 稳定指纹**：不用内置 hash()（受 PYTHONHASHSEED 随机化），用 blake2b 跨进程稳定。
3. **Screenshot 对象**：把图片数据 + 尺寸 + 体积绑在一起，便于成本归因。
4. **截图即用即弃**：默认不落盘（诚实版隐私原则），只保留文字识别结果。
5. **惰性截屏**：纯逻辑（区域预设换算、指纹）可在无图形环境测试，只有真正 grab 才需要屏幕。

依赖：Pillow。
"""

import base64
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PIL import Image

_HASH_SAMPLE_BYTES = 2 * 1024 * 1024  # 指纹只取前 2MB 像素

# 截图最长边上限：防止高分辨率/多显示器导致图片过大，触发视觉 API 拒绝
MAX_DIMENSION = 1600

# 区域预设：只截目标区域能显著降低图片体积与识别噪音
REGION_PRESETS = {
    "full": None,          # 主显示器全屏
    "left_half": "left",   # 左半屏
    "right_half": "right", # 右半屏
    "center": "center",    # 居中 1200x800
}


@dataclass
class Screenshot:
    """一次截屏的完整结果。"""

    data: bytes = b""
    filename: str = ""
    width: int = 0
    height: int = 0
    size_kb: int = 0

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("utf-8") if self.data else ""

    @property
    def is_empty(self) -> bool:
        return not self.data


class Capture:
    """屏幕捕获器（Pillow ImageGrab）。"""

    def __init__(self, save_dir: Optional[str] = None, quality: int = 60,
                 region: Optional[List[int]] = None):
        self.save_dir = save_dir
        self.quality = max(1, min(95, int(quality)))
        self.region = region
        self._last_hash: Optional[str] = None

    # ---------- 区域 ----------
    def resolve_region_preset(self, preset: str, monitor: Optional[dict] = None) -> Optional[List[int]]:
        """把预设名解析为具体坐标 [left, top, width, height]，无需真实截屏能力。"""
        if preset in (None, "", "full"):
            return None
        mon = monitor or {"left": 0, "top": 0, "width": 1920, "height": 1080}
        left, top = mon["left"], mon["top"]
        width, height = mon["width"], mon["height"]
        if preset == "left_half":
            return [left, top, width // 2, height]
        if preset == "right_half":
            return [left + width // 2, top, width - width // 2, height]
        if preset == "center":
            w, h = min(1200, width), min(800, height)
            return [left + (width - w) // 2, top + (height - h) // 2, w, h]
        return None

    def set_region_preset(self, preset: str) -> Optional[List[int]]:
        self.region = self.resolve_region_preset(preset)
        self.reset_hash()
        return self.region

    # ---------- 指纹 ----------
    @staticmethod
    def fingerprint(pil_img: "Image.Image") -> str:
        """计算画面指纹（稳定跨进程）。纯函数，可独立测试。"""
        raw = pil_img.tobytes()[:_HASH_SAMPLE_BYTES]
        return hashlib.blake2b(raw, digest_size=16).hexdigest()

    # ---------- 截屏 ----------
    def _grab_image(self) -> "Image.Image":
        """截取屏幕（或指定区域）。"""
        from PIL import ImageGrab
        if self.region and len(self.region) == 4:
            left, top, width, height = self.region
            bbox = (left, top, left + width, top + height)
            img = ImageGrab.grab(bbox=bbox)
        else:
            img = ImageGrab.grab()
        return img.convert("RGB")

    def has_display(self) -> bool:
        """探测当前环境是否具备截屏能力。"""
        try:
            from PIL import ImageGrab
            return ImageGrab.grab is not None
        except Exception:
            return False

    def grab(self, save_to_disk: bool = False) -> Screenshot:
        """截取屏幕。画面与上次一致且不强制落盘时，返回空 Screenshot（跳过识别）。"""
        pil_img = self._grab_image()

        # 缩放限制：最长边超过 MAX_DIMENSION 则等比缩小，控制图片体积
        if max(pil_img.size) > MAX_DIMENSION:
            ratio = MAX_DIMENSION / max(pil_img.size)
            new_size = (int(pil_img.width * ratio), int(pil_img.height * ratio))
            pil_img = pil_img.resize(new_size, Image.LANCZOS)

        if not save_to_disk:
            current = self.fingerprint(pil_img)
            if current == self._last_hash:
                return Screenshot()
            self._last_hash = current

        buffer = io.BytesIO()
        pil_img.save(buffer, format="JPEG", quality=self.quality)
        jpg_bytes = buffer.getvalue()

        shot = Screenshot(
            data=jpg_bytes,
            width=pil_img.width,
            height=pil_img.height,
            size_kb=len(jpg_bytes) // 1024,
        )
        if save_to_disk and self.save_dir:
            shot.filename = self._save_to_disk(jpg_bytes)
        return shot

    def _save_to_disk(self, jpg_bytes: bytes) -> str:
        directory = Path(self.save_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"screenshot_{stamp}.jpg"
        (directory / filename).write_bytes(jpg_bytes)
        return filename

    def reset_hash(self) -> None:
        self._last_hash = None
