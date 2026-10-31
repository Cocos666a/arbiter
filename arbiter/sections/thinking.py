"""思维链回传检测：只适用于 claude- 前缀模型。

规格 §3.8。请求一个 CRT 型数学题，检查响应里是否带 thinking block。
计分简化：score = summary_score（上限 5），signature 仅作展示。
"""

from __future__ import annotations

from ..prompts import THINKING_MAX_SCORE, THINKING_PROMPT
from ..protocols import send

SUMMARY_SCORE = 5
SIGNATURE_MIN_LEN = 64


def run(ctx) -> dict:
    """执行思维链板块。"""
    verdict = {
        "answer": "",
        "ms": 0,
        "pass": False,
        "reason": "",
        "score": 0,
        "sig_len": 0,
        "signature": "",
        "signature_score": 0,
        "summary_score": 0,
        "think_len": 0,
        "thinking_text": "",
        # 内部计分接口
        "earned": 0,
        "max_points": THINKING_MAX_SCORE,
        "applicable": True,
        "known": False,
        "status": "unknown",
    }

    res = send(
        ctx.client, ctx.model,
        messages=[{"role": "user", "content": THINKING_PROMPT}],
        max_tokens=4096,
    )
    verdict["ms"] = res.ms

    if not res.ok:
        verdict["reason"] = f"请求失败：{res.err}"
        verdict["status"] = "error"
        verdict["applicable"] = False
        return verdict

    thinking = res.thinking or ""
    signature = res.signature or ""
    verdict["thinking_text"] = thinking
    verdict["think_len"] = len(thinking)
    verdict["signature"] = signature
    verdict["sig_len"] = len(signature)
    verdict["answer"] = res.text

    # 摘要分：回复里同时给出思路摘要即得分
    has_summary = bool(res.text) and any(
        kw in res.text for kw in ("思路", "摘要", "mod", "同余", "因为", "步骤")
    )
    verdict["summary_score"] = SUMMARY_SCORE if has_summary else 0
    # 签名分仅作展示，不参与本节计分（规格 §3.8 简化）
    verdict["signature_score"] = (
        5 if (signature and len(signature) >= SIGNATURE_MIN_LEN) else 0
    )
    verdict["score"] = min(SUMMARY_SCORE, verdict["summary_score"])
    verdict["earned"] = verdict["score"]
    verdict["pass"] = verdict["think_len"] > 0
    verdict["known"] = True

    if verdict["pass"]:
        verdict["status"] = "pass"
        verdict["reason"] = (
            f"回传思维链 {verdict['think_len']} 字符，签名 {verdict['sig_len']} 字符。"
        )
    else:
        verdict["status"] = "fail"
        verdict["reason"] = "响应中未包含思维链内容，未检出思维链回传。"
    return verdict
