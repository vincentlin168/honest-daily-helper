"""数据上报模块（Spec-Driven）。

Spec
----
把日报/记录推送到「用户自己的服务端」（用于汇总、归档或接入其他系统）。
这是诚实版的可选功能：默认关闭，且只推用户自己配置的 URL。

设计要点（数据上报 + 诚实原则）：
1. **Bearer Token 鉴权**：可选，设置后请求头带 Authorization。
2. **失败重试 1 次**，超时 30 秒，失败计入日志。
3. **默认关闭**：不配置 url 就不发，绝不悄悄上报。

依赖：httpx。
"""

import time
from typing import Any, Dict, Optional

import httpx


class ReportUploader:
    """日报上报器。"""

    REQUEST_TIMEOUT = 30.0
    MAX_RETRY = 1

    def __init__(self, url: str = "", token: str = ""):
        self.url = (url or "").strip()
        self.token = (token or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def upload(self, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """推送一条数据到用户服务端。

        :return: {ok, http_status, duration_ms, error}
        """
        if not self.enabled:
            return {"ok": False, "error": "未配置上报 URL"}

        body = {"event": event, "payload": payload}
        last_error = ""
        last_status = 0
        last_duration = 0
        for attempt in range(self.MAX_RETRY + 1):
            started = time.perf_counter()
            try:
                with httpx.Client(timeout=self.REQUEST_TIMEOUT) as client:
                    response = client.post(self.url, headers=self._headers(), json=body)
                last_duration = int((time.perf_counter() - started) * 1000)
                last_status = response.status_code
                if response.status_code < 300:
                    return {"ok": True, "http_status": response.status_code, "duration_ms": last_duration}
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            except httpx.TimeoutException:
                last_error = "超时"
                last_duration = int((time.perf_counter() - started) * 1000)
            except httpx.RequestError as exc:
                last_error = f"连接失败：{exc}"
                last_duration = int((time.perf_counter() - started) * 1000)
            # 非 2xx 或网络错误，重试
        return {"ok": False, "http_status": last_status, "duration_ms": last_duration, "error": last_error}

    def upload_daily_report(self, date: str, report_md: str) -> Dict[str, Any]:
        """上报某天的日报。"""
        return self.upload("daily_report", {"date": date, "report": report_md})
