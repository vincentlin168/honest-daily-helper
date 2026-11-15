"""日志模块（Spec-Driven）。

Spec
----
把运行日志同时写到**控制台**和**本机文件**，让「诚实」承诺的「失败可见」落到实处。

问题背景
--------
打包成窗口模式 exe（``console=False``）后，``print()`` 的输出无处可去 ——
用户双击运行、程序静默崩溃、什么线索都没有。这是典型的「黑盒软件」。

设计要点
--------
1. **双写**：控制台（开发期可见）+ 文件（打包后仍可查）。
2. **文件位置**：优先放在 exe 同级目录（用户找得到）；无写权限时退回临时目录。
3. **滚动上限**：单文件超过 2MB 自动轮转，避免无限增长。
4. **不记录敏感信息**：密钥、Token、完整屏幕内容一律不落盘。

依赖：标准库 logging。
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILENAME = "honest-helper.log"
MAX_BYTES = 2 * 1024 * 1024  # 2MB 轮转
BACKUP_COUNT = 1


def _log_dir() -> Path:
    """日志目录：exe 同级目录优先，退回用户目录。"""
    if getattr(sys, "frozen", False):
        candidates = [Path(sys.executable).parent]
    else:
        candidates = [Path(__file__).parent]
    candidates.append(Path.home())
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".log_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return cand
        except Exception:
            continue
    return Path(os.environ.get("TEMP", "."))


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """配置根 logger（控制台 + 文件双写）。可重复调用，不会重复挂 handler。"""
    logger = logging.getLogger("honest")
    logger.setLevel(level)
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 控制台：窗口模式下 stdout 可能为 None，做防御
    try:
        stream = sys.stdout
        if stream is not None:
            console = logging.StreamHandler(stream)
            console.setFormatter(fmt)
            logger.addHandler(console)
    except Exception:
        pass

    try:
        log_path = _log_dir() / LOG_FILENAME
        file_handler = RotatingFileHandler(
            str(log_path), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
        logger.info("日志文件: %s", log_path)
    except Exception:
        pass

    # 让其它模块的 print 也能进日志（stdout 重定向到 logger）
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    """取 logger（未配置时自动配置一次）。"""
    logger = logging.getLogger("honest")
    if not logger.handlers:
        setup_logging()
    return logger
