"""推理表现测试：8 道经典公版智力题，按正则自动评分。

规格 §3.9。每题独立请求，取回复中最后一个「最终答案:」之后的文本做匹配。
半分只在卷面展示，不进入百分制。
"""

from __future__ import annotations

import re

from ..prompts import IQ_ANSWER_MARKER, IQ_QUESTIONS, iq_prompt
from ..protocols import send

PAPER_MAX_SCORE = 100
ANSWER_TAIL_CHARS = 120


def extract_answer(text: str) -> str:
    """取最后一个「最终答案:」之后的文本；没有标记则用括号内末尾兜底。"""
    if not text:
        return ""
    idx = text.rfind(IQ_ANSWER_MARKER)
    if idx >= 0:
        return text[idx + len(IQ_ANSWER_MARKER):].strip()
    return text.strip()[-ANSWER_TAIL_CHARS:]


def grade(answer: str, question: dict) -> tuple:
    """按 accept/half 正则评分，返回 (满分命中, 半分命中)。

    大小写与空格不敏感（规格附录 E）。
    """
    if not answer:
        return False, False
    normalized = re.sub(r"\s+", "", answer).lower()
    for pattern in question.get("accept") or []:
        if re.search(pattern, answer, re.IGNORECASE) or \
                re.search(re.sub(r"\s+", "", pattern), normalized, re.IGNORECASE):
            return True, False
    for pattern in question.get("half") or []:
        if re.search(pattern, answer, re.IGNORECASE):
            return False, True
    return False, False


def _metrics(res, protocol: str) -> dict:
    """单题指标（缺失一律填 null/false/0）。"""
    usage = res.usage or {}
    return {
        "Attempts": res.attempts,
        "Protocol": protocol,
        "Status": res.http_status,
        "Ms": res.ms,
        "InputTokens": usage.get("prompt_tokens"),
        "OutputTokens": usage.get("completion_tokens"),
        "ThinkingTokens": None,
        "CacheRead": usage.get("cached_tokens"),
        "ThinkLen": len(res.thinking or ""),
        "SigLen": len(res.signature or ""),
        "FirstByteMs": res.first_byte_ms,
        "StreamComplete": bool(res.stream_complete),
    }


def run(ctx) -> dict:
    """执行推理板块。"""
    per_question = []
    earned_sum = 0
    half_display = 0.0
    complete = True

    for question in IQ_QUESTIONS:
        res = send(
            ctx.client, ctx.model,
            messages=[{"role": "user", "content": iq_prompt(question)}],
            max_tokens=1024,
        )
        max_score = question["max"]
        if not res.ok:
            complete = False
            per_question.append({
                "id": question["id"],
                "title": question["title"],
                "max": max_score,
                "earned": 0,
                "half": False,
                "tags": ["failed"],
                "prompt": iq_prompt(question),
                "expected": question["expected"],
                "answer": "",
                "thinking_text": res.thinking or "",
                "signature": res.signature or "",
                "ms": res.ms,
                "error": res.err,
                "metrics": _metrics(res, ctx.protocol),
            })
            continue

        answer = extract_answer(res.text)
        full, half = grade(answer, question)
        earned = max_score if full else 0
        earned_sum += earned
        if half:
            half_display += max_score / 2

        per_question.append({
            "id": question["id"],
            "title": question["title"],
            "max": max_score,
            "earned": earned,
            "half": bool(half),
            "tags": [],
            "prompt": iq_prompt(question),
            "expected": question["expected"],
            "answer": answer,
            "full_text": res.text,
            "thinking_text": res.thinking or "",
            "signature": res.signature or "",
            "ms": res.ms,
            "error": None,
            "metrics": _metrics(res, ctx.protocol),
        })

    paper_max = sum(q["max"] for q in IQ_QUESTIONS)
    score = int(round(earned_sum / paper_max * PAPER_MAX_SCORE)) if paper_max else 0

    return {
        "complete": complete,
        "max": PAPER_MAX_SCORE,
        "note": (
            f"当前卷面 {earned_sum + half_display:g}；半分仅在卷面展示，"
            "不进入百分制通过分。失败请求标记未完成，不伪装为模型答错。"
        ),
        "paper_max": paper_max,
        "per_question": per_question,
        "score": score,
        # 内部计分接口：统一用百分制（earned 与 max_points 同刻度，比例与卷面一致）
        "earned": score,
        "max_points": PAPER_MAX_SCORE,
        "applicable": True,
        "known": complete,
        "status": "pass" if complete else "partial",
    }
