"""注入大小测量：用固定短问 + 1600 字符填充，估算网关额外的输入开销。

规格 §3.2。两次独立请求（不共享上下文），比较 total tokens 的增量，
扣除可见文本本身的 token 估计后，剩余部分即「注入」开销。

键名说明：`points` 按规格 §3.2 是逐点测量列表；本节的得分放在 `earned`
（内部计分接口统一用 `earned` / `max_points`，避免与规格键名冲突）。
"""

from __future__ import annotations

import math
import unicodedata

from ..prompts import (
    INJECTION_PAD_CHAR,
    INJECTION_PAD_LEN,
    INJECTION_SHORT_QUESTION,
    INJECTION_SYSTEM,
)
from ..protocols import send

MAX_POINTS = 10

# 计分分档（近似映射，写入 note）
TIER_LOW = 1024
TIER_MID = 4096


def visible_token_estimate(text: str) -> int:
    """可见文本的 token 估算。

    规格 §3.2：`ceil(ascii_chars/4) + ceil(cjk_chars/1.6) + 8`，
    其中 +8 为消息模板开销（system + user 两条，各约 4 tok）。
    """
    cjk, other = 0, 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    return int(math.ceil(other / 4.0) + math.ceil(cjk / 1.6) + 8)


def _is_cjk(ch: str) -> bool:
    """判断是否为 CJK 字符（含全角标点与假名）。"""
    if ch in "\n\r\t":
        return False
    code = ord(ch)
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
        return True
    if 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF:
        return True
    if 0x3040 <= code <= 0x30FF:
        return True
    return unicodedata.east_asian_width(ch) in ("W", "F")


def run(ctx) -> dict:
    """执行注入板块。"""
    question = INJECTION_SHORT_QUESTION
    padded = question + "\n" + INJECTION_PAD_CHAR * INJECTION_PAD_LEN

    verdict = {
        # --- 规格 §3.2 输出键 ---
        "cache_header": "",
        "inj": 0,
        "max_residual": None,
        "max_score": MAX_POINTS,
        "note": "",
        "points": [],
        "r2": None,
        "reliable": False,
        "score": 0,
        "slope": None,
        "via": "",
        # --- 内部计分接口 ---
        "earned": 0,
        "max_points": MAX_POINTS,
        "applicable": True,
        "known": False,
        "status": "unknown",
    }

    measurements = []
    for n, user_text in ((1, question), (INJECTION_PAD_LEN, padded)):
        res = send(
            ctx.client, ctx.model,
            messages=[{"role": "user", "content": user_text}],
            system=INJECTION_SYSTEM,
            max_tokens=16,
        )
        usage = res.usage or {}
        total = usage.get("prompt_tokens")
        cached = usage.get("cached_tokens") or 0
        input_tokens = None if total is None else max(0, total - cached)
        measurements.append({
            "n": n,
            "user_text": user_text,
            "res": res,
            "total": total,
            "input": input_tokens,
        })

    verdict["points"] = [
        {"input": m["input"], "n": m["n"], "total": m["total"]}
        for m in measurements
    ]

    errors = [m["res"].err for m in measurements if not m["res"].ok]
    if errors:
        verdict["note"] = "请求失败：" + "；".join(e for e in errors if e)
        verdict["status"] = "error"
        # 两点测量缺一不可：整体失败视为本节无有效数据
        verdict["applicable"] = False
        return verdict

    totals = [m["total"] for m in measurements]
    if any(t is None for t in totals):
        verdict["note"] = "上游未上报 usage，无法估算注入开销。"
        verdict["status"] = "unreliable"
        verdict["applicable"] = False
        return verdict

    monotonic = totals[1] >= totals[0]
    verdict["reliable"] = bool(monotonic)

    overheads = []
    for m in measurements:
        est = visible_token_estimate(m["user_text"]) + visible_token_estimate(INJECTION_SYSTEM)
        overheads.append(m["total"] - est)

    inj = max(0, int(round(sum(overheads) / len(overheads))))
    verdict["inj"] = inj
    verdict["max_residual"] = max(overheads) if overheads else None

    span = measurements[1]["n"] - measurements[0]["n"]
    verdict["slope"] = round((totals[1] - totals[0]) / span, 6) if span else None

    if inj <= TIER_LOW:
        earned = MAX_POINTS
    elif inj <= TIER_MID:
        earned = 4
    else:
        earned = 0

    verdict["earned"] = earned
    verdict["score"] = earned
    verdict["known"] = True
    verdict["status"] = "measured"

    note = (
        f"估算注入开销 {inj} tokens；分档（≤{TIER_LOW}→{MAX_POINTS} 分、"
        f"≤{TIER_MID}→4 分、其余 0 分）为按观察推断的近似映射，非上游原始阈值。"
    )
    if not monotonic:
        note += " 上报 usage 非单调，结论不可靠。"
    verdict["note"] = note
    return verdict
