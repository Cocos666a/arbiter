"""隐藏指令检测：用六种问法交叉验证模型是否被注入了前置指令。

规格 §3.3。7 次独立请求（6 个 presence 问 + 1 个 identity 问），
对每个回复做立场分类（deny / ack / unclear），再按承认与否认的组合定状态。
"""

from __future__ import annotations

from ..prompts import (
    HIDDEN_ACK_CONDITION_MARKERS,
    HIDDEN_ACK_MARKERS,
    HIDDEN_ACK_NEGATION_WORDS,
    HIDDEN_ACK_WINDOW,
    HIDDEN_ASKS,
    HIDDEN_DENY_MARKERS,
    HIDDEN_IDENTITY_PROMPT,
    HIDDEN_IDENTITY_WORDS,
)
from ..protocols import send

MAX_POINTS = 10
STANCE_WINDOW = 10


def classify_stance(answer: str) -> str:
    """对单条回复做立场分类（规格 §3.3）。

    deny：以「无/没有/不存在/否」开头（前 10 字符内）且无后续承认性内容；
    ack：含「我有/存在/有的/确实有」、或「遵守」+设定语义、或英文 system prompt 承认；
    unclear：其余（包括裸「有」）。
    """
    if not answer:
        return "unclear"
    text = answer.strip()
    head = text[:STANCE_WINDOW]

    acked = _has_ack(text)
    if acked:
        return "ack"

    for marker in HIDDEN_DENY_MARKERS:
        if marker in head:
            return "deny"
    return "unclear"


def _has_ack(text: str) -> bool:
    """是否含有承认性内容。

    「存在」「有的」这类词条本身不区分肯定与否定，所以在匹配位置左侧开一个
    小窗口查否定/疑问词：`不存在`、`是否有` 都不算承认，避免把否认误判为承认。
    """
    for marker in HIDDEN_ACK_MARKERS:
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx < 0:
                break
            window = text[max(0, idx - HIDDEN_ACK_WINDOW):idx]
            if not any(neg in window for neg in HIDDEN_ACK_NEGATION_WORDS):
                return True
            start = idx + len(marker)

    if "遵守" in text and any(m in text for m in HIDDEN_ACK_CONDITION_MARKERS):
        return True
    lowered = text.lower()
    if "system prompt" in lowered and any(
        w in lowered for w in ("yes", "存在", "有", "follow", "given")
    ):
        # 英文侧同样排除 "no system prompt" 这类否认
        idx = lowered.find("system prompt")
        window = lowered[max(0, idx - HIDDEN_ACK_WINDOW):idx]
        if not any(neg in window for neg in ("no", "not", "without", "n't")):
            return True
    return False


def _identity_self_introduces(answer: str) -> bool:
    """identity 问是否给出自我介绍（含模型名或身份词）。"""
    if not answer:
        return False
    return any(word in answer for word in HIDDEN_IDENTITY_WORDS)


def run(ctx) -> dict:
    """执行隐藏指令板块。"""
    verdict = {
        "ack_count": 0,
        "asks": [],
        "attempts": 0,
        "deny_count": 0,
        "identity": {"self_introduces": False, "cutoff_mismatch": False},
        "injected": False,
        "max_score": MAX_POINTS,
        "protocol": ctx.protocol,
        "reason": "",
        "score": 0,
        "signals": [],
        "status": "unknown",
        # 内部计分接口
        "earned": 0,
        "max_points": MAX_POINTS,
        "applicable": True,
        "known": False,
    }

    unclear_count = 0
    failed = 0

    for ask in HIDDEN_ASKS:
        res = send(
            ctx.client, ctx.model,
            messages=[{"role": "user", "content": ask["prompt"]}],
            max_tokens=256,
        )
        verdict["attempts"] += 1
        if not res.ok:
            failed += 1
            stance = "unclear"
        else:
            stance = classify_stance(res.text)

        if stance == "ack":
            verdict["ack_count"] += 1
            verdict["signals"].append({"id": ask["id"], "stance": "ack"})
        elif stance == "deny":
            verdict["deny_count"] += 1
        else:
            unclear_count += 1

        verdict["asks"].append({
            "id": ask["id"],
            "kind": ask["kind"],
            "prompt": ask["prompt"],
            "answer": res.text if res.ok else "",
            "stance": stance,
            "http_status": res.http_status,
            "ms": res.ms,
            "error": res.err,
        })

    # identity 问：不进 asks
    id_res = send(
        ctx.client, ctx.model,
        messages=[{"role": "user", "content": HIDDEN_IDENTITY_PROMPT}],
        max_tokens=256,
    )
    verdict["attempts"] += 1
    if not id_res.ok:
        failed += 1
    verdict["identity"] = {
        "self_introduces": _identity_self_introduces(id_res.text) if id_res.ok else False,
        "cutoff_mismatch": False,  # 简化：不做截止日期一致性校验
        "answer": id_res.text if id_res.ok else "",
        "error": id_res.err,
    }

    presence_answered = sum(1 for a in verdict["asks"] if a.get("error") is None)

    # 全部请求都失败 → 本节未取得有效数据，判为不适用（不得凭空给「存疑」分）
    if presence_answered == 0:
        verdict.update({
            "injected": False, "status": "error", "earned": 0, "score": 0,
            "applicable": False, "known": False,
            "reason": "全部问询请求失败，本节未取得有效数据，不计分。",
        })
        return verdict

    # --- 判定（观察映射，写入 reason） ---
    ack, deny = verdict["ack_count"], verdict["deny_count"]
    if ack >= 1 and deny >= 1:
        verdict.update({
            "injected": True, "status": "injected", "earned": 6, "score": 6,
            "reason": f"检出隐藏前置指令：自述自相矛盾：{ack} 次承认，{deny} 次否认。",
        })
    elif ack >= 1 and deny == 0:
        verdict.update({
            "injected": True, "status": "injected", "earned": 5, "score": 5,
            "reason": f"检出隐藏前置指令：{ack} 次明确承认存在前置指令/运行设定。",
        })
    elif ack == 0 and unclear_count > 0:
        verdict.update({
            "injected": False, "status": "suspect", "earned": 1, "score": 1,
            "reason": "问法之间未完全一致，但证据不足以判定注入，建议人工复核 asks 字段。",
        })
    else:
        verdict.update({
            "injected": False, "status": "clean", "earned": 0, "score": 0,
            "reason": "多种问法均一致否认存在前置指令，未检出隐藏指令。",
        })

    if failed:
        verdict["reason"] += f"（{failed} 个请求失败，结论可能不完整。）"
    verdict["known"] = failed == 0
    return verdict
