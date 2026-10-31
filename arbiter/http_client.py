"""HTTP 客户端：urllib 封装，含重试、脱敏、计时。

设计约束（规格 §0）：
- 零第三方依赖，只用标准库 urllib。
- 单请求失败重试 ≤3 次，指数退避 2s/5s/10s。
- 所有对外文本（错误信息、回复文本）在返回前脱敏。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

REDACTED = "«REDACTED»"
# 密钥样式：sk- 前缀 + 至少 4 位
KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_\-]{4,}")
# 认证头样式：Bearer <token>
AUTH_HEADER_PATTERN = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{6,}")


def redact(text: str) -> str:
    """把字符串中的密钥样式替换为占位符。"""
    if not isinstance(text, str):
        return text
    return AUTH_HEADER_PATTERN.sub(r"\1" + REDACTED, KEY_PATTERN.sub(REDACTED, text))


def redact_deep(value):
    """递归脱敏任意 JSON 结构（dict/list/str）。"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_deep(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# 探测结果
# ---------------------------------------------------------------------------


def encode_headers(headers: dict) -> dict:
    """把含非 ASCII 的头部值转为 UTF-8 字节。

    HTTP 头默认按 latin-1 编码，直接发送含中文的密钥会抛 UnicodeEncodeError；
    改为发送 UTF-8 字节（多数网关可接受，如带中文的 `sk-<服务商>-预留-…` 密钥）。
    """
    return {
        k: (v.encode("utf-8") if isinstance(v, str) and any(ord(ch) > 126 for ch in v) else v)
        for k, v in headers.items()
    }


@dataclass
class ProbeResult:
    """单次探测请求的统一结果（规格 §2 要求记录 http_status/ms/err/usage）。"""

    ok: bool = False
    http_status: int | None = None
    ms: int = 0
    first_byte_ms: int | None = None
    err: str | None = None
    data: dict = field(default_factory=dict)
    text: str = ""              # 助手可见回复文本
    thinking: str = ""          # 思维链文本（anthropic thinking block）
    signature: str = ""         # 思维链签名
    tool_calls: list = field(default_factory=list)
    output_items: list = field(default_factory=list)  # responses 的 output 数组
    usage: dict = field(default_factory=dict)         # 原始 usage（未归一化）
    attempts: int = 0
    request_sha256: str = ""
    stream_complete: bool = True
    error_body_excerpt: str = ""


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

DEFAULT_BACKOFF = (2.0, 5.0, 10.0)


class HttpClient:
    """串行 HTTP 客户端；默认不并发，符合「低频」红线。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 360.0,
        retries: int = 3,
        backoff: tuple = DEFAULT_BACKOFF,
        protocol: str = "chat",
        sleeper=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.backoff = tuple(backoff)
        self.protocol = protocol
        self._sleep = sleeper
        # 防御：含空白/控制字符的密钥无法作为 HTTP 头发送（明确报错）；
        # 含非 ASCII 的密钥（如某些网关的「sk-<服务商>-中文-…」样式）会按 UTF-8
        # 字节发送授权头，见 _request 中的头部编码。
        if any(ord(ch) < 33 or ord(ch) == 127 for ch in self.api_key):
            raise ValueError(
                "API key 含空白或控制字符，无法作为 HTTP 头发送；"
                "请检查密钥来源（是否误取了整行说明文本）。"
            )

    # -- 认证头 -------------------------------------------------------------

    def auth_headers(self) -> dict:
        """按协议返回认证头。

        anthropic 用 x-api-key + anthropic-version；其余用 Authorization。
        """
        if self.protocol == "anthropic":
            return {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            }
        return {"Authorization": "Bearer " + self.api_key}

    # -- 请求 ---------------------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.base_url + path

    def post_json(self, path: str, body: dict, extra_headers: dict | None = None) -> ProbeResult:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self._request("POST", path, payload, extra_headers)

    def get_json(self, path: str, extra_headers: dict | None = None) -> ProbeResult:
        return self._request("GET", path, None, extra_headers)

    def _request(
        self,
        method: str,
        path: str,
        payload: bytes | None,
        extra_headers: dict | None,
    ) -> ProbeResult:
        url = self._url(path)
        result = ProbeResult()
        if payload is not None:
            result.request_sha256 = hashlib.sha256(payload).hexdigest()[:16]

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "arbiter/0.1",
        }
        headers.update(self.auth_headers())
        if extra_headers:
            headers.update(extra_headers)
        # HTTP 头默认按 latin-1 编码；对含非 ASCII 的头部值（例如密钥里带中文）
        # 改为给出 UTF-8 字节，避免 UnicodeEncodeError（多数网关可正常接受）。
        headers = encode_headers(headers)

        last_err = None
        for attempt in range(1, self.retries + 2):
            result.attempts = attempt
            started = time.perf_counter()
            try:
                req = urllib.request.Request(
                    url, data=payload, headers=headers, method=method
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    # 响应头已返回，作为首字节到达的近似时刻
                    result.first_byte_ms = int((time.perf_counter() - started) * 1000)
                    raw = resp.read()
                    result.http_status = resp.status
                result.ms = int((time.perf_counter() - started) * 1000)
                result.stream_complete = True
                try:
                    result.data = json.loads(raw.decode("utf-8", "replace"))
                except (ValueError, UnicodeDecodeError) as exc:
                    result.ok = False
                    result.err = redact(f"响应不是合法 JSON：{exc}")
                    return result
                result.ok = True
                result.err = None
                return result
            except urllib.error.HTTPError as exc:
                result.ms = int((time.perf_counter() - started) * 1000)
                result.http_status = exc.code
                excerpt = ""
                try:
                    excerpt = exc.read().decode("utf-8", "replace")[:500]
                except Exception:  # noqa: BLE001 - 读取错误体失败不应中断审计
                    excerpt = ""
                result.error_body_excerpt = redact(excerpt)
                last_err = f"HTTP {exc.code}: {result.error_body_excerpt}"
                # 4xx 属确定性错误，重试无意义，直接返回
                if 400 <= exc.code < 500:
                    result.ok = False
                    result.err = redact(last_err)
                    return result
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                result.ms = int((time.perf_counter() - started) * 1000)
                last_err = f"网络错误：{exc}"

            if attempt <= self.retries:
                delay = self.backoff[min(attempt - 1, len(self.backoff) - 1)]
                if delay:
                    self._sleep(delay)

        result.ok = False
        result.err = redact(last_err or "请求失败")
        return result

    # -- 便捷方法 -----------------------------------------------------------

    def list_models(self) -> ProbeResult:
        return self.get_json("/v1/models")
