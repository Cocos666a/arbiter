"""协议适配：chat / anthropic / openai(responses) 三种上游协议。

规格 §2 约定：
- `resolve_protocol` 按显式值或模型名前缀判定协议；
- 三种协议统一返回 `ProbeResult`，并把 usage 归一化成一致的键；
- base_url 支持带或不带 `/v1`。
"""

from __future__ import annotations

import json

from .http_client import HttpClient, ProbeResult

CHAT = "chat"
ANTHROPIC = "anthropic"
OPENAI = "openai"

VALID_PROTOCOLS = (CHAT, ANTHROPIC, OPENAI, "auto")

# 走 OpenAI 原生 responses 接口的模型名前缀（规格 §2）
RESPONSES_PREFIXES = ("gpt-5.6", "gpt-6", "grok-")


def normalize_base(base_url: str) -> str:
    """去掉尾部斜杠。"""
    return (base_url or "").rstrip("/")


def endpoint(base_url: str, path: str) -> str:
    """拼接端点；base_url 已带 /v1 时不再重复拼接。"""
    base = normalize_base(base_url)
    if base.endswith("/v1") and path.startswith("/v1/"):
        return base + path[len("/v1"):]
    return base + path


def resolve_protocol(base_url: str, model: str, protocol: str = "auto", timeout: float = 360.0) -> str:
    """判定协议。

    显式 chat/anthropic/openai 直接返回；auto 时按模型名前缀：
    `claude-*` → anthropic；`gpt-5.6`/`gpt-6`/`grok-*` → openai(responses)；
    其余（deepseek/glm 等）→ chat。
    """
    if protocol in (CHAT, ANTHROPIC, OPENAI):
        return protocol
    name = (model or "").lower()
    if name.startswith("claude-"):
        return ANTHROPIC
    for prefix in RESPONSES_PREFIXES:
        if name.startswith(prefix):
            return OPENAI
    return CHAT


# ---------------------------------------------------------------------------
# 消息与工具映射
# ---------------------------------------------------------------------------


def _chat_tools(tools):
    if not tools:
        return None
    return [{"type": "function", "function": t} for t in tools]


def _anthropic_tools(tools):
    if not tools:
        return None
    out = []
    for t in tools:
        out.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


def _responses_tools(tools):
    if not tools:
        return None
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


def _headers_for_search(base_url: str, protocol: str) -> dict:
    """responses 的 web_search 工具需要显式声明，无需额外头。"""
    return {}


# ---------------------------------------------------------------------------
# 统一调用入口
# ---------------------------------------------------------------------------


def send(
    client: HttpClient,
    model: str,
    messages: list,
    system: str | None = None,
    max_tokens: int = 1024,
    tools: list | None = None,
    tool_choice: str | None = None,
    protocol: str | None = None,
    extra_body: dict | None = None,
) -> ProbeResult:
    """按协议发送一次对话请求，返回归一化后的 `ProbeResult`。"""
    proto = protocol or client.protocol
    base = client.base_url
    body: dict = {"model": model}

    if proto == ANTHROPIC:
        body["max_tokens"] = max_tokens or 4096
        body["messages"] = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]
        if system:
            body["system"] = system
        a_tools = _anthropic_tools(tools)
        if a_tools:
            body["tools"] = a_tools
        path = endpoint(base, "/v1/messages")
    elif proto == OPENAI:
        body["max_output_tokens"] = max_tokens
        body["input"] = list(messages)
        if system:
            body["instructions"] = system
        r_tools = _responses_tools(tools)
        if r_tools:
            body["tools"] = r_tools
        path = endpoint(base, "/v1/responses")
    else:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        body["messages"] = msgs
        body["max_tokens"] = max_tokens
        body["stream"] = False
        c_tools = _chat_tools(tools)
        if c_tools:
            body["tools"] = c_tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        path = endpoint(base, "/v1/chat/completions")

    if extra_body:
        body.update(extra_body)

    result = client.post_json(path, body)
    result.data = result.data or {}
    _normalize_result(result, proto)
    return result


def _normalize_result(result: ProbeResult, proto: str) -> None:
    """从原始响应里抽出文本、思维链、工具调用与 usage。"""
    data = result.data or {}
    if not result.ok:
        return

    if proto == ANTHROPIC:
        blocks = data.get("content") or []
        texts, thinks, sigs, calls = [], [], [], []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "thinking":
                thinks.append(block.get("thinking", ""))
                sigs.append(block.get("signature", "") or "")
            elif btype == "tool_use":
                calls.append({
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": block.get("input", {}),
                })
        result.text = "\n".join(t for t in texts if t).strip()
        result.thinking = "\n".join(t for t in thinks if t).strip()
        result.signature = sigs[0] if sigs else ""
        result.tool_calls = calls
        result.output_items = blocks

    elif proto == OPENAI:
        texts, thinks, sigs, calls = [], [], [], []
        for item in data.get("output") or []:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message":
                for c in item.get("content") or []:
                    if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                        texts.append(c.get("text", ""))
            elif itype == "function_call":
                args = item.get("arguments", "")
                calls.append({
                    "id": item.get("call_id") or item.get("id", ""),
                    "name": item.get("name", ""),
                    "arguments": _maybe_json(args),
                })
            elif itype == "reasoning":
                for s in item.get("summary") or []:
                    if isinstance(s, dict) and s.get("text"):
                        thinks.append(s["text"])
        if not texts and isinstance(data.get("output_text"), str):
            texts.append(data["output_text"])
        result.text = "\n".join(t for t in texts if t).strip()
        result.thinking = "\n".join(thinks).strip()
        result.tool_calls = calls
        result.output_items = data.get("output") or []

    else:  # chat
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            result.text = (msg.get("content") or "").strip()
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                result.tool_calls.append({
                    "id": call.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": _maybe_json(fn.get("arguments", "")),
                })
            raw_calls = msg.get("tool_calls") or []
            result.output_items = raw_calls
        # DeepSeek 系把思维链放在 reasoning_content
        if not result.thinking:
            try:
                result.thinking = (choices[0]["message"].get("reasoning_content") or "").strip()
            except (IndexError, KeyError, AttributeError):
                result.thinking = ""

    result.usage = extract_usage(data, proto)


def _maybe_json(value):
    """把 JSON 字符串参数解析为对象；解析失败原样返回字符串。"""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


# ---------------------------------------------------------------------------
# usage 归一化
# ---------------------------------------------------------------------------


def extract_usage(data: dict, proto: str) -> dict:
    """把三协议 usage 归一化为统一键。

    返回键：prompt_tokens / completion_tokens / cached_tokens / cache_write_tokens
    以及 `reported`（是否有任何非空上报）与 `raw`。
    """
    usage = (data or {}).get("usage") or {}
    out = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "cached_tokens": None,
        "cache_write_tokens": None,
        "raw": usage,
    }
    if not isinstance(usage, dict):
        out["reported"] = False
        return out

    if proto == ANTHROPIC:
        out["prompt_tokens"] = usage.get("input_tokens")
        out["completion_tokens"] = usage.get("output_tokens")
        out["cached_tokens"] = usage.get("cache_read_input_tokens")
        out["cache_write_tokens"] = usage.get("cache_creation_input_tokens")
    elif proto == OPENAI:
        out["prompt_tokens"] = usage.get("input_tokens")
        out["completion_tokens"] = usage.get("output_tokens")
        details = usage.get("input_tokens_details") or {}
        out["cached_tokens"] = details.get("cached_tokens")
    else:
        out["prompt_tokens"] = usage.get("prompt_tokens")
        out["completion_tokens"] = usage.get("completion_tokens")
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens")
        if cached is None:
            cached = usage.get("prompt_cache_hit_tokens")  # DeepSeek 系
        out["cached_tokens"] = cached
        written = details.get("cache_creation_tokens")
        if written is None:
            written = usage.get("prompt_cache_miss_tokens")
        out["cache_write_tokens"] = written

    out["reported"] = any(
        out[k] is not None
        for k in ("prompt_tokens", "completion_tokens", "cached_tokens")
    )
    return out
