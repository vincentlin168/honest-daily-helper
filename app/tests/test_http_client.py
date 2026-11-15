"""http_client 模块的测试（SDD 验证）。

覆盖点：默认不信任系统代理、显式开启代理、配置驱动、超时透传。

关键回归
--------
httpx 默认 ``trust_env=True``，会读取 Windows 注册表的系统代理。
开着系统代理的机器上，本机请求被代理转发 → 502。本项目主张「链路透明」，
所以默认必须关掉代理，且必须是**默认**关（而不是靠用户记得去配）。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from http_client import client_from_config, make_client


class TestMakeClient(unittest.TestCase):
    def test_default_disables_system_proxy(self):
        """默认不读系统代理 —— 否则本机请求会被代理转发成 502。"""
        client = make_client(timeout=5)
        try:
            self.assertFalse(client.trust_env)
        finally:
            client.close()

    def test_can_opt_in_to_system_proxy(self):
        """确有需要走代理的用户可显式开启。"""
        client = make_client(timeout=5, use_system_proxy=True)
        try:
            self.assertTrue(client.trust_env)
        finally:
            client.close()

    def test_timeout_is_applied(self):
        client = make_client(timeout=7.5)
        try:
            self.assertEqual(client.timeout.read, 7.5)
        finally:
            client.close()


class TestClientFromConfig(unittest.TestCase):
    def _cfg(self, tmp, llm_proxy, upload_proxy):
        import json
        p = Path(tmp) / "config.json"
        p.write_text(json.dumps({
            "llm": {"use_system_proxy": llm_proxy},
            "upload": {"use_system_proxy": upload_proxy},
        }), encoding="utf-8")
        return Config(str(p))

    def test_reads_section_flag(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp, True, False)
            c1 = client_from_config(cfg, section="llm")
            try:
                self.assertTrue(c1.trust_env)
            finally:
                c1.close()
            c2 = client_from_config(cfg, section="upload")
            try:
                self.assertFalse(c2.trust_env)
            finally:
                c2.close()

    def test_missing_flag_defaults_to_no_proxy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text("{}", encoding="utf-8")
            cfg = Config(str(p))
            c = client_from_config(cfg, section="llm")
            try:
                self.assertFalse(c.trust_env)
            finally:
                c.close()


if __name__ == "__main__":
    unittest.main()
