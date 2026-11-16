"""配置模块（Spec-Driven）。

Spec
----
本模块负责「诚实日报助手」的全部可配置项，用零依赖的 JSON 存储。

设计要点：
1. **零额外依赖**：用标准库 json，不引入 yaml/toml，保持"开箱即跑"。
2. **默认值内置**：配置文件缺失时用 DEFAULTS，保证 clone 下来不填任何东西也能跑。
3. **密钥永不明文回显**：`to_public_dict()` 是唯一允许暴露给外部（HTTP/日志）的出口，
   会把 api_key / token 掩码成 `sk-a...wxyz` 形式。
4. **点路径读取**：`get("llm", "model")` 风格，缺失时返回 default。

配置结构（与 DEFAULTS 对齐）：
    llm:            api_base / api_key / model / system_prompt
                    use_system_proxy / price_per_1m_input / price_per_1m_output
    capture:        snap_interval / interval / quality / save_dir / region_preset
    storage:        db_path / archive_dir / export_dir
    report:         trigger(manual|scheduled|both) / scheduled_time
    upload:         enabled / url / token / use_system_proxy
    skill_server:   enabled / port / token
"""

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

PLACEHOLDER_KEY = "your-api-key-here"

DEFAULTS: Dict[str, Any] = {
    "llm": {
        "api_base": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-flash",  # 用户拍板：默认 DeepSeek-flash（免费档，视觉可用）
        "system_prompt": "",         # 留空则用 llm_client 内置识别 prompt
        "use_system_proxy": False,   # 默认直连官方，不被系统代理静默改道
        # 价格（每百万 token），用于成本透明度展示；填 0 则不显示金额
        "price_per_1m_input": 0.0,
        "price_per_1m_output": 0.0,
    },
    "capture": {
        "snap_interval": 15,         # 抓屏间隔（秒，密集抓，不漏记）
        "interval": 120,             # 汇总周期（秒，把识别结果聚合写入时间线）
        "quality": 60,               # JPEG 质量 1-95
        "save_dir": "",              # 留空则不落盘（截图即用即弃，诚实版默认）
        "region_preset": "full",     # full / left_half / right_half / center
    },
    "storage": {
        "db_path": "records.db",
        "archive_dir": "outputs/reports",  # 日报留档目录（Markdown 文件）
        "export_dir": "exports",           # 数据导出目录
    },
    "report": {
        "trigger": "both",           # manual / scheduled / both（用户拍板：都要）
        "scheduled_time": "18:00",   # 定时自动生成日报的时间
    },
    "upload": {
        "enabled": False,            # 数据上报（默认关，仅推用户自己的服务端）
        "url": "",
        "token": "",
        "use_system_proxy": False,   # 默认直连，不被系统代理静默改道
    },
    "skill_server": {
        "enabled": True,             # Skill 本地服务（首版要做）
        "port": 8088,                # 本地 Skill 服务端口
        "token": "",                 # 鉴权 token，留空则不鉴权
    },
    "tray": {
        "enabled": True,             # Windows 系统托盘常驻（右键：打开面板/立即识别/生成日报/退出）
    },
    "onboarding": {
        "completed": False,          # 首次运行引导是否已完成
        "completed_at": "",          # 完成时间（ISO 格式）
        "skipped": False,            # 用户是否主动跳过
    },
    "categories": [
        "产品设计", "代码开发", "运营分析", "数据复盘",
        "客户支持", "项目管理", "日报整理", "资料研究", "其他",
    ],
}


def mask_secret(value: Optional[str], keep: int = 4) -> str:
    """把密钥掩码成 `sk-a...wxyz` 形式，唯一允许离开进程的形态。

    - 空值/占位符原样返回
    - 长度不足 2*keep+1 则全部打码
    """
    if not value:
        return ""
    if value == PLACEHOLDER_KEY:
        return value
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}...{value[-keep:]}"


class Config:
    """JSON 配置的读写封装。"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(config_path) if config_path else Path("config.json")
        self._data: Dict[str, Any] = {}
        if self.config_path.exists():
            self.load()
        else:
            self._data = copy.deepcopy(DEFAULTS)

    # ---------- 读写 ----------
    def load(self) -> None:
        with open(self.config_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError(f"配置文件格式错误，应为 JSON 对象：{self.config_path}")
        # 用默认值兜底缺失的顶层/嵌套键
        merged = copy.deepcopy(DEFAULTS)
        for key, value in loaded.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        self._data = merged

    def save(self, path: Optional[str] = None) -> None:
        """保存配置。

        覆盖前先备份上一份（``<name>.bak``），只保留最近一份。
        这是为了防止「一次误写就把用户配置弄丢且无法恢复」——
        比如引导流程写坏了 Key。
        """
        target = Path(path) if path else self.config_path
        try:
            if target.exists():
                backup = target.with_suffix(target.suffix + ".bak")
                shutil.copy2(target, backup)
        except Exception:
            # 备份失败不该阻断保存
            pass
        with open(target, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def get(self, *keys: str, default=None):
        """按路径读取，如 config.get("llm", "model", default="deepseek-flash")。"""
        val: Any = self._data
        for k in keys:
            if not isinstance(val, dict):
                return default
            val = val.get(k)
            if val is None:
                return default
        return val

    def set(self, *keys_and_value: Any) -> None:
        """按路径写入，最后一个参数是值，前面是键路径。"""
        if len(keys_and_value) < 2:
            raise ValueError("set() 至少需要「键路径 + 值」两个参数")
        keys, value = keys_and_value[:-1], keys_and_value[-1]
        d = self._data
        for k in keys[:-1]:
            if k not in d or not isinstance(d[k], dict):
                d[k] = {}
            d = d[k]
        d[keys[-1]] = value

    def update_from_dict(self, data: Dict[str, Any]) -> None:
        """用外部字典覆盖配置（逐段合并，忽略掩码形态的密钥）。"""
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(self._data.get(key), dict):
                merged = dict(self._data[key])
                merged.update(value)
                # 掩码形态的 api_key/token 不应覆盖真实值
                for secret in ("api_key", "token"):
                    if secret in merged and isinstance(merged[secret], str) and "..." in merged[secret]:
                        merged[secret] = self._data[key].get(secret, "")
                self._data[key] = merged
            else:
                self._data[key] = value

    # ---------- 派生视图 ----------
    @property
    def llm_config(self) -> Dict[str, Any]:
        return self._data.get("llm", {})

    @property
    def capture_config(self) -> Dict[str, Any]:
        return self._data.get("capture", {})

    # ---------- 输出 ----------
    def to_dict(self) -> Dict[str, Any]:
        """返回含明文密钥的完整配置，仅限进程内部使用。"""
        return self._data

    def to_public_dict(self) -> Dict[str, Any]:
        """返回可安全暴露给 HTTP/日志的配置，密钥已掩码。"""
        safe = copy.deepcopy(self._data)
        if "llm" in safe and "api_key" in safe["llm"]:
            safe["llm"]["api_key"] = mask_secret(safe["llm"].get("api_key"))
        if "upload" in safe and "token" in safe["upload"]:
            safe["upload"]["token"] = mask_secret(safe["upload"].get("token"))
        if "skill_server" in safe and "token" in safe["skill_server"]:
            safe["skill_server"]["token"] = mask_secret(safe["skill_server"].get("token"))
        return safe
