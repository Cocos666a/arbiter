"""知识截止估计：用 2025–2026 客观事件做二分，定位知识边界。不计分。

规格 §3.7。事件按日期升序，从中间开始问；知道就往更晚半区追，
不知道就往更早半区退，最多 8 问。
"""

from __future__ import annotations

from datetime import date

from ..prompts import KNOWLEDGE_EVENTS, KNOWLEDGE_MAX_ASKS, KNOWLEDGE_TEMPLATE
from ..protocols import send

REASON = "按 2025–2026 客观事件串行二分估计知识边界，不参与评分。"


def classify(answer: str, keywords: list) -> str:
    """判定 know / unknown（规格 §3.7）。

    选 A 或 B 且简述含任一关键词 → know；选 C 或简述与关键词无关 → unknown。
    """
    if not answer:
        return "unknown"
    text = answer.strip()
    head = text[:40].upper()

    chose_c = "C" in head and "A" not in head and "B" not in head
    has_keyword = any(kw in text for kw in keywords)
    if chose_c:
        return "unknown"
    if has_keyword:
        return "know"
    return "unknown"


def _events_sorted() -> list:
    return sorted(KNOWLEDGE_EVENTS, key=lambda e: e["date"])


def run(ctx) -> dict:
    """执行知识截止板块。"""
    events = _events_sorted()
    today = ctx.today or date.today().isoformat()

    verdict = {
        "attempts": 0,
        "correct": 0,
        "cutoff": None,
        "label": "",
        "protocol": ctx.protocol,
        "reason": REASON,
        "rows": [],
        "status": "estimated",
        # 内部计分接口
        "earned": 0,
        "max_points": 0,
        "applicable": False,   # 永远不计分
        "known": False,
        "score": 0,
        "total": 0,
    }

    lo, hi = 0, len(events) - 1
    asked = set()
    ask_count = 0
    know_dates, unknown_dates = [], []

    while lo <= hi and ask_count < KNOWLEDGE_MAX_ASKS:
        mid = (lo + hi) // 2
        if mid in asked:
            break
        asked.add(mid)
        event = events[mid]
        prompt = KNOWLEDGE_TEMPLATE.format(event=event["event"])

        res = send(
            ctx.client, ctx.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
        )
        ask_count += 1
        verdict["attempts"] += 1

        if not res.ok:
            kind = "unknown"
            answer = ""
        else:
            answer = res.text
            kind = classify(answer, event["keywords"])

        if kind == "know":
            know_dates.append(event["date"])
            lo = mid + 1
        else:
            unknown_dates.append(event["date"])
            hi = mid - 1

        verdict["rows"].append({
            "answer": answer,
            "event": event["event"],
            "happened_at": event["date"],
            "id": event["id"],
            "kind": kind,
            "ok": kind == "know",
            "quarter": event["date"],
            "unsure": kind == "unknown",
            "http_status": res.http_status,
            "ms": res.ms,
            "error": res.err,
        })

    verdict["total"] = len(verdict["rows"])
    verdict["correct"] = sum(1 for r in verdict["rows"] if r["ok"])

    # 边界：最晚 know 之后、最早 unknown 之前
    earliest_unknown = min(unknown_dates) if unknown_dates else today
    latest_know = max(know_dates) if know_dates else events[0]["date"]
    if not unknown_dates:
        cutoff_hi = today
    else:
        cutoff_hi = earliest_unknown
    if not know_dates:
        latest_know = events[0]["date"]

    verdict["cutoff"] = earliest_unknown
    verdict["cutoff_lo"] = latest_know
    verdict["cutoff_hi"] = cutoff_hi
    if know_dates and unknown_dates:
        verdict["label"] = f"知识≈{latest_know} 至 {cutoff_hi}（{verdict['total']} 问）"
    elif know_dates and not unknown_dates:
        verdict["label"] = (
            f"知识≥{latest_know}（{verdict['total']} 问；问到的全部事件均已知）"
        )
    elif unknown_dates and not know_dates:
        verdict["label"] = (
            f"知识≤{earliest_unknown}（{verdict['total']} 问；问到的全部事件均未知）"
        )
    else:
        verdict["label"] = "未取得有效回答"
    verdict["known"] = bool(verdict["rows"])
    if not verdict["rows"]:
        verdict["status"] = "error"
        verdict["reason"] = REASON + " 本次未取得任何有效回答。"
    return verdict
