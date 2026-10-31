"""缓存读写验证：六轮串行会话，检查前缀缓存是否被上游真实回传。

规格 §3.4。固定 8000 字符确定性前缀，逐轮累积消息；
用 `cached_tokens` 判断命中，`cache_creation/miss` 判断写入。
"""

from __future__ import annotations

import hashlib

from ..prompts import CACHE_PREFIX_LEN, CACHE_PREFIX_WORDS, CACHE_SHORT_QUESTION
from ..protocols import send

MAX_POINTS = 1
ROUNDS = 6
PASS_RATIO = 0.8


def build_prefix(length: int = CACHE_PREFIX_LEN) -> str:
    """生成确定性长前缀：重复固定词表直到达到指定长度。

    内容每次运行相同，因此可跨轮、跨进程命中同一前缀缓存。
    """
    if length <= 0:
        return ""
    repeats = length // len(CACHE_PREFIX_WORDS) + 1
    return (CACHE_PREFIX_WORDS * repeats)[:length]


def _round_record(index, res, messages, usage, read_reported, write_reported):
    """组装单轮记录（键名对齐规格 §3.4）。"""
    total = usage.get("prompt_tokens")
    read = usage.get("cached_tokens")
    write = usage.get("cache_write_tokens")
    output = usage.get("completion_tokens")

    input_tokens = None
    if total is not None:
        input_tokens = max(0, total - (read or 0))

    hit_rate = None
    if total:
        hit_rate = round((read or 0) / total * 100, 4)

    prompt_chars = sum(len(m.get("content") or "") for m in messages)
    request_sha256 = res.request_sha256 or ""

    return {
        "index": index,
        "input": input_tokens,
        "total": total,
        "read": read,
        "write": write,
        "read_reported": read_reported,
        "write_reported": write_reported,
        "input_reported": total is not None,
        "output": output,
        "output_reported": output is not None,
        "hit_rate": hit_rate,
        "messages": len(messages),
        "prompt_chars": prompt_chars,
        "request_sha256": request_sha256,
        "ms": res.ms,
        "http_status": res.http_status,
        "error": res.err,
    }


def run(ctx) -> dict:
    """执行缓存板块。"""
    prefix = build_prefix()
    prefix_sha = hashlib.sha256(prefix.encode("utf-8")).hexdigest()

    verdict = {
        "actual_read": 0,
        "delta": 0,
        "expected_read": 0,
        "first": None,
        "pass": False,
        "prefix_sha256": prefix_sha,
        "protocol": ctx.protocol,
        "reason": "",
        "retries_observed": False,
        "samples": [],
        # 内部计分接口
        "earned": 0,
        "max_points": MAX_POINTS,
        "applicable": True,
        "known": False,
        "status": "unknown",
        "score": 0,
    }

    messages = [{"role": "system", "content": prefix}]
    rounds = []

    for index in range(1, ROUNDS + 1):
        messages = list(messages)
        messages.append({"role": "user", "content": f"{CACHE_SHORT_QUESTION}（第 {index} 轮）"})
        res = send(ctx.client, ctx.model, messages=list(messages), max_tokens=16)

        usage = res.usage or {}
        raw_usage = usage.get("raw") or {}
        read_reported = usage.get("cached_tokens") is not None or any(
            k in raw_usage for k in ("prompt_cache_hit_tokens", "prompt_tokens_details")
        )
        write_reported = usage.get("cache_write_tokens") is not None or any(
            k in raw_usage for k in ("prompt_cache_miss_tokens",)
        )

        record = _round_record(index, res, messages, usage, read_reported, write_reported)
        rounds.append(record)
        if index == 1:
            verdict["first"] = record
        else:
            verdict["samples"].append(record)

        if not res.ok:
            break
        messages.append({"role": "assistant", "content": res.text or "A"})

    if len(rounds) < ROUNDS:
        # 会话中断视为本节未产生有效数据（规格 §4：cache 失败 → 不适用）
        verdict["reason"] = f"会话在第 {len(rounds)} 轮中断，未完成 6 轮，本节不计分。"
        verdict["status"] = "error"
        verdict["applicable"] = False
        verdict["delta"] = 0
        return verdict

    last = rounds[-1]
    prev = rounds[-2]

    actual_read = last.get("read") or 0
    expected_read = (prev.get("read") or 0) + (prev.get("write") or 0)
    verdict["actual_read"] = actual_read
    verdict["expected_read"] = expected_read
    verdict["delta"] = actual_read - expected_read

    passed = expected_read > 0 and actual_read >= PASS_RATIO * expected_read
    verdict["pass"] = bool(passed)
    verdict["earned"] = MAX_POINTS if passed else 0
    verdict["score"] = verdict["earned"]
    verdict["known"] = True

    if not last.get("read_reported"):
        verdict["status"] = "unreported"
        verdict["reason"] = "上游未上报缓存读取字段，无法判定缓存是否生效，本节不计分。"
        verdict["known"] = False
        verdict["applicable"] = False
    elif passed:
        verdict["status"] = "pass"
        verdict["reason"] = (
            f"末轮读取 {actual_read} tokens，达到前一轮写入+读取合计的 "
            f"{int(PASS_RATIO * 100)}%，前缀缓存确认命中。"
        )
    else:
        verdict["status"] = "fail"
        verdict["reason"] = (
            f"末轮读取 {actual_read} tokens，低于前一轮预期的 {expected_read} tokens，"
            "前缀缓存未按预期回传。"
        )
    return verdict
