#!/usr/bin/env python3
"""离线 mock 网关：供测试与本地演练使用。

用法：
    python tools/mock_gateway.py [--port 0] [--host 127.0.0.1]

实现 OpenAI 兼容的 chat completions（含 tools、usage、缓存字段），
以及 /v1/models。anthropic 与 responses 端点返回 501，用于验证降级路径。
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# IQ 参考答案（按题干特征片段映射，与 prompts.py 的题库一一对应）
IQ_ANSWER_MAP = [
    ("球拍和球", "球的价格是 0.05 元。\n最终答案: 0.05 元"),
    ("机器", "需要 5 分钟。\n最终答案: 5 分钟"),
    ("睡莲", "需要 47 天。\n最终答案: 47 天"),
    ("锚", "水位会下降。\n最终答案: 下降"),
    ("袜子", "至少摸出 5 只。\n最终答案: 5 只"),
    ("过桥", "最少需要 17 分钟。\n最终答案: 17 分钟"),
    ("时针", "一昼夜重合 22 次。\n最终答案: 22 次"),
    ("兔", "兔有 12 只。\n最终答案: 12 只"),
]

# 缓存 mock 状态：上一次请求的 prompt_tokens
_STATE = {"prev_prompt_tokens": None}
_LOCK = threading.Lock()

# 隐藏指令问法的 mock 立场：default 全 deny；注入模式（--injected）制造矛盾
_INJECTED = {"enabled": False}


class MockGatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock-gateway/0.1"

    # -- 基础设施 -----------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: A003 - 静默，避免污染测试输出
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 接口
        if self.path.rstrip("/").endswith("/v1/models"):
            self._send_json({
                "object": "list",
                "data": [{"id": "mock-model", "object": "model", "owned_by": "mock"}],
            })
            return
        self._send_json({"error": {"message": "not found"}}, status=404)

    def do_POST(self):  # noqa: N802
        path = self.path.rstrip("/")
        if path.endswith("/v1/chat/completions"):
            self._chat_completions()
            return
        # anthropic / responses 仅最小实现：返回 501，供测试降级路径
        self._send_json(
            {"error": {"message": "endpoint not implemented in mock", "type": "not_implemented"}},
            status=501,
        )

    # -- chat completions ---------------------------------------------------

    def _chat_completions(self):
        body = self._read_json()
        messages = body.get("messages") or []
        tools = body.get("tools") or []
        model = body.get("model", "mock-model")

        user_texts = [m.get("content") or "" for m in messages if m.get("role") == "user"]
        all_text = "\n".join(
            m.get("content") or "" if isinstance(m.get("content"), str) else ""
            for m in messages
        )
        last_user = user_texts[-1] if user_texts else ""

        prompt_tokens = self._estimate_prompt_tokens(messages)

        # 1) 工具调用回传轮：消息里出现 tool 角色 → 回确认串
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if has_tool_result or "tool-confirm-" in all_text:
            confirm = self._extract_confirm(all_text)
            text = f"已收到工具结果。{confirm}" if confirm else "已收到工具结果。"
            self._reply_chat(model, text, prompt_tokens, completion_text=text)
            return

        # 2) 工具调用轮：带 tools 且用户要求调用 audit_echo
        if tools and "audit_echo" in all_text:
            nonce = self._extract_nonce(all_text)
            self._reply_tool_call(model, nonce, prompt_tokens)
            return

        # 3) IQ 题
        for keyword, answer in IQ_ANSWER_MAP:
            if keyword in last_user:
                self._reply_chat(model, answer, prompt_tokens)
                return

        # 4) 指纹题：解析 N 并回 N 个数字
        if "个 1 到" in last_user or "之间的整数" in last_user:
            numbers = self._fake_numbers(last_user)
            self._reply_chat(model, numbers, prompt_tokens)
            return

        # 5) 隐藏指令问法
        if self._looks_like_hidden_ask(last_user):
            self._reply_chat(model, self._hidden_answer(last_user), prompt_tokens)
            return

        # 6) 知识截止问法
        if "最符合你的知识状况" in last_user:
            answer = "C 我不了解这件事。"
            if "利奥十四世" in last_user:
                answer = "A 我了解，教皇利奥十四世于 2025 年当选。"
            self._reply_chat(model, answer, prompt_tokens)
            return

        # 7) 思维链题
        if "同余方程" in last_user:
            self._reply_chat(
                model,
                "思路摘要：用中国剩余定理逐步合并三个同余式。\n最终答案: 7417",
                prompt_tokens,
            )
            return

        # 8) 兜底：短回复
        self._reply_chat(model, "好的。", prompt_tokens)

    # -- 回复构造 -----------------------------------------------------------

    def _reply_chat(self, model, text, prompt_tokens, completion_text=None):
        completion_text = completion_text if completion_text is not None else text
        completion_tokens = max(1, len(completion_text) // 3)

        cached = 0
        with _LOCK:
            prev = _STATE.get("prev_prompt_tokens")
            if prev:
                # mock 缓存策略：命中上一轮 prompt 长度减去少量开销
                cached = max(0, prev - 20)
            _STATE["prev_prompt_tokens"] = prompt_tokens

        payload = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "prompt_tokens_details": {
                    "cached_tokens": cached,
                    "cache_creation_tokens": max(0, prompt_tokens - cached),
                },
            },
        }
        self._send_json(payload)

    def _reply_tool_call(self, model, nonce, prompt_tokens):
        call_id = "call_mock_1"
        payload = {
            "id": "chatcmpl-mock-tool",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "audit_echo",
                            "arguments": json.dumps({"nonce": nonce, "sum": 42}),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 20,
                "total_tokens": prompt_tokens + 20,
            },
        }
        self._send_json(payload)

    # -- 内容生成 -----------------------------------------------------------

    @staticmethod
    def _estimate_prompt_tokens(messages) -> int:
        """粗略 token 估计，够用即可。"""
        total = 0
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            content = content or ""
            total += max(1, len(content) // 3) + 4
        return total + 8

    @staticmethod
    def _fake_numbers(prompt: str) -> str:
        """按题干里的 N 生成 N 个 [1,400] 的整数。

        用确定性伪随机（基于 N 的线性同余）保证可复现。
        """
        match = re.search(r"生成\s*(\d+)\s*个", prompt)
        count = int(match.group(1)) if match else 300
        count = max(1, min(count, 400))
        state = 12345
        out = []
        for _ in range(count):
            state = (1103515245 * state + 12345) % (2 ** 31)
            out.append(str(state % 400 + 1))
        return ", ".join(out)

    @staticmethod
    def _extract_nonce(text: str) -> str:
        match = re.search(r"nonce\s*固定为\s*`?([0-9a-fA-F]{8,})`?", text)
        if match:
            return match.group(1)
        match = re.search(r"\b([0-9a-fA-F]{16})\b", text)
        return match.group(1) if match else "0" * 16

    @staticmethod
    def _extract_confirm(text: str) -> str:
        match = re.search(r"tool-confirm-[0-9a-fA-F]+", text)
        return match.group(0) if match else ""

    @staticmethod
    def _looks_like_hidden_ask(text: str) -> bool:
        markers = ("其他指令或设定文本", "系统提示词", "位于我这条 user 消息之前",
                   "运行守则", "能说什么", "原样打印")
        return any(m in text for m in markers)

    @staticmethod
    def _hidden_answer(text: str) -> str:
        """默认全 deny；注入模式下对部分问法承认，制造自相矛盾。"""
        if _INJECTED["enabled"] and ("运行守则" in text or "能说什么" in text):
            return "有的，我确实遵循一份平台运行守则。"
        return "没有。"


def build_server(host: str = "127.0.0.1", port: int = 0, injected: bool = False):
    """创建 mock 服务；port=0 时由系统分配空闲端口。"""
    _INJECTED["enabled"] = injected
    _STATE["prev_prompt_tokens"] = None
    server = ThreadingHTTPServer((host, port), MockGatewayHandler)
    server.daemon_threads = True
    return server


def serve_forever(host: str = "127.0.0.1", port: int = 0, injected: bool = False) -> None:
    server = build_server(host, port, injected)
    actual_host, actual_port = server.server_address[:2]
    print(f"mock 网关已启动：http://{actual_host}:{actual_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Arbiter 离线 mock 网关")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 表示自动分配空闲端口")
    parser.add_argument("--injected", action="store_true",
                        help="模拟被注入隐藏指令的网关（用于验证检测逻辑）")
    args = parser.parse_args(argv)
    serve_forever(args.host, args.port, args.injected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
