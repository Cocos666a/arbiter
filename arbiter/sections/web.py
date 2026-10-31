"""服务端联网检测：只在协议提供统一联网工具时才判定，不采信模型自述。

规格 §3.6。chat 协议没有统一的服务端联网工具协议，本节直接返回 unknown；
responses / anthropic 协议尝试带搜索工具发起请求，检查执行证据与来源引用。
"""

from __future__ import annotations

import re

from ..prompts import WEB_PROMPT
from ..protocols import ANTHROPIC, CHAT, OPENAI, send

MAX_POINTS = 1

# URL 提取：排除中英文括号与常见收尾标点，避免把包裹符号吞进链接
URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]（）【】,，。；;]+")
# responses 里表示搜索已完成的 output item 类型
SEARCH_ITEM_TYPES = ("web_search_call", "search", "web_search")


def run(ctx) -> dict:
    """执行联网板块。"""
    verdict = {
        "attempts": 0,
        "evidence": {"search_completed": False, "sources": []},
        "pass": None,
        "protocol": ctx.protocol,
        "reason": "",
        "status": "unknown",
        # 内部计分接口
        "earned": 0,
        "max_points": MAX_POINTS,
        "applicable": False,
        "known": False,
        "score": 0,
    }

    if ctx.protocol == CHAT:
        verdict["reason"] = (
            "Chat Completions没有统一的服务端联网工具协议；不根据模型自述判断联网。"
        )
        return verdict

    if ctx.protocol == OPENAI:
        result = send(
            ctx.client, ctx.model,
            messages=[{"role": "user", "content": WEB_PROMPT}],
            max_tokens=512,
            extra_body={"tools": [{"type": "web_search"}]},
        )
    else:  # anthropic server tool
        result = send(
            ctx.client, ctx.model,
            messages=[{"role": "user", "content": WEB_PROMPT}],
            max_tokens=512,
            extra_body={"tools": [{"type": "web_search_20250305", "name": "web_search",
                                   "max_uses": 3}]},
        )
    verdict["attempts"] = 1

    if not result.ok:
        verdict["reason"] = f"联网请求失败：{result.err}"
        verdict["status"] = "unknown"
        return verdict

    items = result.output_items or []
    completed = False
    for item in items:
        if isinstance(item, dict) and item.get("type") in SEARCH_ITEM_TYPES:
            completed = True
            break
        # anthropic 的 server_tool_use / web_search_tool_result
        if isinstance(item, dict) and item.get("type") in (
            "server_tool_use", "web_search_tool_result"
        ):
            completed = True
            break

    sources = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "web_search_tool_result":
                    for r in c.get("content") or []:
                        if isinstance(r, dict) and r.get("url"):
                            sources.append(r["url"])
        for key in ("url", "source"):
            if isinstance(item.get(key), str) and item[key].startswith("http"):
                sources.append(item[key])
    # 回复正文里的 URL 也作为引用证据
    sources.extend(URL_RE.findall(result.text or ""))

    # 去重保序
    seen, unique_sources = set(), []
    for url in sources:
        if url not in seen:
            seen.add(url)
            unique_sources.append(url)

    verdict["evidence"] = {
        "search_completed": bool(completed),
        "sources": unique_sources[:20],
    }
    verdict["applicable"] = True
    verdict["pass"] = bool(completed)
    verdict["earned"] = MAX_POINTS if completed else 0
    verdict["score"] = verdict["earned"]
    verdict["known"] = True
    verdict["status"] = "pass" if completed else "fail"
    if completed:
        verdict["reason"] = (
            f"检出服务端搜索工具执行痕迹，来源引用 {len(unique_sources)} 条。"
        )
    else:
        verdict["reason"] = "未在响应中发现服务端搜索工具的执行证据。"
    return verdict
