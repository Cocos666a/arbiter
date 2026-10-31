"""工具调用检测：验证模型能否正确发起函数调用并把结果回传。

规格 §3.5。两轮：
1. 发起调用（nonce 随机、sum=42），校验名称与参数；
2. 回传 tool 结果，要求最终回复原样包含确认串。
"""

from __future__ import annotations

import secrets

from ..prompts import (
    TOOL_CONFIRM_TEMPLATE,
    TOOL_FINAL_INSTRUCTION,
    TOOL_INSTRUCTION,
    TOOL_NAME,
    TOOL_SPEC,
    TOOL_SYSTEM,
)
from ..protocols import ANTHROPIC, OPENAI, send

MAX_POINTS = 1


def new_nonce() -> str:
    """16 位十六进制随机串。"""
    return secrets.token_hex(8)


def _check_arguments(args) -> tuple:
    """校验调用参数，返回 (nonce 是否相符, sum 是否等于 42)。"""
    if not isinstance(args, dict):
        return False, False
    nonce_ok = isinstance(args.get("nonce"), str) and args.get("nonce").strip() != ""
    try:
        sum_ok = int(args.get("sum")) == 42
    except (TypeError, ValueError):
        sum_ok = False
    return nonce_ok, sum_ok


def _tool_result_message(proto: str, call_id: str, payload: str) -> dict:
    """按协议组装回传工具结果的消息。"""
    if proto == ANTHROPIC:
        return {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": payload,
            }],
        }
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": payload,
    }


def run(ctx) -> dict:
    """执行工具板块。"""
    nonce = new_nonce()
    confirm = TOOL_CONFIRM_TEMPLATE.format(nonce=nonce)
    instruction = TOOL_INSTRUCTION.format(nonce=nonce)

    verdict = {
        "attempts": 0,
        "evidence": {"calls": [], "confirmation_received": False, "roundtrip": False},
        "pass": False,
        "protocol": ctx.protocol,
        "reason": "",
        "status": "fail",
        # 内部计分接口
        "earned": 0,
        "max_points": MAX_POINTS,
        "applicable": True,
        "known": False,
        "score": 0,
    }

    tool_choice = "auto" if ctx.protocol == OPENAI else "required"
    first = send(
        ctx.client, ctx.model,
        messages=[{"role": "user", "content": instruction}],
        system=TOOL_SYSTEM,
        max_tokens=512,
        tools=[TOOL_SPEC],
        tool_choice=tool_choice,
    )
    verdict["attempts"] += 1

    if not first.ok:
        verdict["reason"] = f"首次请求失败：{first.err}"
        verdict["status"] = "error"
        verdict["applicable"] = False
        return verdict

    calls = first.tool_calls or []
    verdict["evidence"]["calls"] = [
        {
            "arguments": c.get("arguments"),
            "id": c.get("id", ""),
            "name": c.get("name", ""),
        }
        for c in calls
    ]

    target = None
    for call in calls:
        if call.get("name") == TOOL_NAME:
            target = call
            break
    if target is None:
        verdict["reason"] = "模型未发起 audit_echo 调用。"
        verdict["known"] = True
        return verdict

    nonce_ok, sum_ok = _check_arguments(target.get("arguments"))
    nonce_match = nonce_ok and isinstance(target.get("arguments"), dict) and \
        str(target["arguments"].get("nonce")).strip() == nonce
    args_ok = nonce_match and sum_ok

    if not args_ok:
        verdict["reason"] = (
            f"调用参数校验失败（nonce 相符：{nonce_match}，sum 为 42：{sum_ok}）。"
        )
        verdict["known"] = True
        return verdict

    # 第二轮：回传工具结果
    payload = f'{{"ok":true,"confirm":"{confirm}"}}'
    if ctx.protocol == ANTHROPIC:
        messages = [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": target.get("id", ""),
                 "name": TOOL_NAME, "input": target.get("arguments")},
            ]},
            _tool_result_message(ctx.protocol, target.get("id", ""), payload),
            {"role": "user", "content": TOOL_FINAL_INSTRUCTION.format(confirm=confirm)},
        ]
    else:
        messages = [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": target.get("id", ""),
                "type": "function",
                "function": {"name": TOOL_NAME,
                             "arguments": _dump(target.get("arguments"))},
            }]},
            _tool_result_message(ctx.protocol, target.get("id", ""), payload),
            {"role": "user", "content": TOOL_FINAL_INSTRUCTION.format(confirm=confirm)},
        ]

    second = send(ctx.client, ctx.model, messages=messages, max_tokens=256)
    verdict["attempts"] += 1
    if not second.ok:
        verdict["reason"] = f"回传工具结果后请求失败：{second.err}"
        verdict["status"] = "error"
        # 回传轮失败 → 闭环无法验证，本节无有效结论
        verdict["applicable"] = False
        return verdict

    roundtrip = confirm in (second.text or "")
    verdict["evidence"]["confirmation_received"] = roundtrip
    verdict["evidence"]["roundtrip"] = roundtrip

    passed = args_ok and roundtrip
    verdict["pass"] = bool(passed)
    verdict["status"] = "pass" if passed else "fail"
    verdict["earned"] = MAX_POINTS if passed else 0
    verdict["score"] = verdict["earned"]
    verdict["known"] = True
    verdict["reason"] = (
        "工具名称、随机参数、调用ID与结果回传均验证通过。" if passed
        else "参数校验通过，但最终回复未原样包含确认串，结果回传未验证。"
    )
    return verdict


def _dump(value) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)
