"""认证方式相关测试（SDD 验证，标准库 unittest）。

覆盖点：
1. URL 内嵌 ``user:pass@`` 的拆分与清洗
2. 认证方式自动判定（Bearer / Basic / none）
3. 显式 ``auth_type`` 覆盖自动判定
4. 请求头生成（Basic 走 base64，Bearer 走 token）
5. ``api_password`` 的掩码与「掩码值不覆盖真实值」

纯逻辑测试，不真实调用 API。
"""

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config
from llm_client import LLMClient, _split_url_credentials


class TestSplitUrlCredentials(unittest.TestCase):
    """URL 内嵌凭据的拆分。"""

    def test_plain_url_unchanged(self):
        url, user, pwd = _split_url_credentials("https://api.deepseek.com/v1")
        self.assertEqual(url, "https://api.deepseek.com/v1")
        self.assertEqual(user, "")
        self.assertEqual(pwd, "")

    def test_user_pass_embedded(self):
        url, user, pwd = _split_url_credentials("https://alice:s3cret@llm.example.com/v1")
        self.assertEqual(url, "https://llm.example.com/v1")
        self.assertEqual(user, "alice")
        self.assertEqual(pwd, "s3cret")

    def test_user_only_embedded(self):
        url, user, pwd = _split_url_credentials("https://alice@llm.example.com/v1")
        self.assertEqual(url, "https://llm.example.com/v1")
        self.assertEqual(user, "alice")
        self.assertEqual(pwd, "")

    def test_port_preserved(self):
        url, user, pwd = _split_url_credentials("http://u:p@127.0.0.1:8080/v1")
        self.assertEqual(url, "http://127.0.0.1:8080/v1")
        self.assertEqual(user, "u")
        self.assertEqual(pwd, "p")

    def test_empty(self):
        self.assertEqual(_split_url_credentials(""), ("", "", ""))


class TestAuthResolution(unittest.TestCase):
    """认证方式判定。"""

    def test_auto_bearer_when_key_present(self):
        c = LLMClient("https://x.example.com/v1", "sk-123", "m")
        self.assertEqual(c._resolved_auth_type(), "bearer")
        self.assertEqual(c._request_headers()["Authorization"], "Bearer sk-123")

    def test_auto_basic_when_username_password(self):
        c = LLMClient("https://x.example.com/v1", "", "m",
                      api_username="u", api_password="p")
        self.assertEqual(c._resolved_auth_type(), "basic")
        expected = "Basic " + base64.b64encode(b"u:p").decode("ascii")
        self.assertEqual(c._request_headers()["Authorization"], expected)

    def test_auto_basic_from_url_embedded(self):
        c = LLMClient("https://u:p@x.example.com/v1", "", "m")
        self.assertEqual(c._resolved_auth_type(), "basic")
        expected = "Basic " + base64.b64encode(b"u:p").decode("ascii")
        self.assertEqual(c._request_headers()["Authorization"], expected)

    def test_auto_none_when_no_credentials(self):
        c = LLMClient("https://x.example.com/v1", "", "m")
        self.assertEqual(c._resolved_auth_type(), "none")
        self.assertNotIn("Authorization", c._request_headers())

    def test_explicit_basic_overrides_key(self):
        """显式指定 basic 时，即使有 api_key 也应走 Basic。"""
        c = LLMClient("https://x.example.com/v1", "sk-123", "m",
                      auth_type="basic", api_username="u", api_password="p")
        self.assertEqual(c._resolved_auth_type(), "basic")
        self.assertTrue(c._request_headers()["Authorization"].startswith("Basic "))

    def test_explicit_bearer_overrides_password(self):
        c = LLMClient("https://x.example.com/v1", "sk-123", "m",
                      auth_type="bearer", api_username="u", api_password="p")
        self.assertEqual(c._resolved_auth_type(), "bearer")
        self.assertEqual(c._request_headers()["Authorization"], "Bearer sk-123")

    def test_explicit_none_sends_no_auth(self):
        c = LLMClient("https://x.example.com/v1", "sk-123", "m", auth_type="none")
        self.assertNotIn("Authorization", c._request_headers())

    def test_explicit_username_overrides_url_embedded(self):
        """显式用户名优先于 URL 内嵌的用户名。"""
        c = LLMClient("https://urluser:urlpass@x.example.com/v1", "", "m",
                      api_username="real", api_password="realpwd")
        expected = "Basic " + base64.b64encode(b"real:realpwd").decode("ascii")
        self.assertEqual(c._request_headers()["Authorization"], expected)

    def test_credentials_not_left_in_api_base(self):
        """凭据不应残留在 api_base 里（避免出现在日志/错误信息中）。"""
        c = LLMClient("https://u:p@x.example.com/v1", "", "m")
        self.assertNotIn("u:p", c.api_base)
        self.assertNotIn("@", c.api_base)

    def test_content_type_always_present(self):
        c = LLMClient("https://x.example.com/v1", "", "m")
        self.assertEqual(c._request_headers()["Content-Type"], "application/json")


class TestPasswordMasking(unittest.TestCase):
    """密码的掩码与合并行为。"""

    def _cfg(self):
        return Config()

    def test_password_masked_in_public_dict(self):
        cfg = self._cfg()
        cfg.set("llm", "api_password", "supersecret")
        public = cfg.to_public_dict()
        self.assertNotEqual(public["llm"]["api_password"], "supersecret")
        self.assertIn("...", public["llm"]["api_password"])

    def test_masked_password_does_not_overwrite_real(self):
        """前端回传掩码值时不应把真实密码冲掉。"""
        cfg = self._cfg()
        cfg.set("llm", "api_password", "supersecret")
        masked = cfg.to_public_dict()["llm"]["api_password"]
        cfg.update_from_dict({"llm": {"api_password": masked}})
        self.assertEqual(cfg.get("llm", "api_password"), "supersecret")

    def test_new_password_does_overwrite(self):
        cfg = self._cfg()
        cfg.set("llm", "api_password", "old")
        cfg.update_from_dict({"llm": {"api_password": "brand-new"}})
        self.assertEqual(cfg.get("llm", "api_password"), "brand-new")


class TestUrlCredentialMasking(unittest.TestCase):
    """URL 内嵌凭据的回显掩码与回写保护。"""

    def _cfg(self):
        return Config()

    def test_password_in_url_is_masked(self):
        """api_base 里的密码回显时必须掩码。"""
        cfg = self._cfg()
        cfg.set("llm", "api_base", "https://alice:s3cretpwd@llm.example.com/v1")
        public = cfg.to_public_dict()
        base = public["llm"]["api_base"]
        self.assertNotIn("s3cretpwd", base)
        self.assertIn("...", base)
        # 用户名保留，用户需要靠它确认填的是哪个账号
        self.assertIn("alice", base)

    def test_url_without_credentials_unchanged(self):
        cfg = self._cfg()
        cfg.set("llm", "api_base", "https://api.deepseek.com/v1")
        self.assertEqual(cfg.to_public_dict()["llm"]["api_base"],
                         "https://api.deepseek.com/v1")

    def test_masked_url_roundtrip_keeps_real_password(self):
        """前端回传掩码地址时，真密码必须保留。"""
        cfg = self._cfg()
        cfg.set("llm", "api_base", "https://alice:s3cretpwd@llm.example.com/v1")
        masked = cfg.to_public_dict()["llm"]["api_base"]

        # 模拟前端「读设置 -> 原样保存」
        cfg.update_from_dict({"llm": {"api_base": masked}})
        self.assertEqual(cfg.get("llm", "api_base"),
                         "https://alice:s3cretpwd@llm.example.com/v1")

    def test_short_password_roundtrip_keeps_real_password(self):
        """短密码的掩码形态是全星号（不含 '...'），同样不能被写回。

        这是实测踩到的坑：mask_secret 对长度 <= 8 的密码返回 ``******``，
        若只按 '...' 判定掩码，短密码的掩码会被当成「用户新填的密码」
        写回配置，导致 Basic 认证直接失败。
        """
        cfg = self._cfg()
        cfg.set("llm", "api_base", "https://alice:s3cret@llm.example.com/v1")
        masked = cfg.to_public_dict()["llm"]["api_base"]
        self.assertNotIn("...", masked)  # 短密码掩码不含 '...'
        self.assertIn("***", masked)

        cfg.update_from_dict({"llm": {"api_base": masked}})
        self.assertEqual(cfg.get("llm", "api_base"),
                         "https://alice:s3cret@llm.example.com/v1")

    def test_new_url_password_takes_effect(self):
        """用户改了密码时应以新值为准。"""
        cfg = self._cfg()
        cfg.set("llm", "api_base", "https://alice:oldpwd@llm.example.com/v1")
        cfg.update_from_dict({"llm": {"api_base": "https://alice:newpwd@llm.example.com/v1"}})
        self.assertEqual(cfg.get("llm", "api_base"),
                         "https://alice:newpwd@llm.example.com/v1")

    def test_plain_base_does_not_get_creds_injected(self):
        """原本没有凭据的地址，不该被凭空塞进密码。"""
        cfg = self._cfg()
        cfg.set("llm", "api_base", "https://api.deepseek.com/v1")
        cfg.update_from_dict({"llm": {"api_base": "https://api.deepseek.com/v1"}})
        self.assertEqual(cfg.get("llm", "api_base"), "https://api.deepseek.com/v1")


if __name__ == "__main__":
    unittest.main()
