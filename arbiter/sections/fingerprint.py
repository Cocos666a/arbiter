"""指纹归因：用随机数字生成任务提取模型的采样指纹，与基线库比对。

规格 §3.1。要点：
- 3 次请求，每次让模型生成 N∈[280,330] 个 [1,400] 的整数；
- 解析出的整数转特征向量，与基线库每个模型的中心向量算余弦；
- softmax(similarity × 8) 给概率；
- 计分：usable≥3 且 top_model==声称模型 → 10；同家族 → 6；否则 0。
"""

from __future__ import annotations

import random

from .. import features as F
from ..prompts import (
    FINGERPRINT_M,
    FINGERPRINT_N_MAX,
    FINGERPRINT_N_MIN,
    FINGERPRINT_TEMPLATE,
)
from ..protocols import send

MAX_POINTS = 10
SOFTMAX_SCALE = 8.0


def collect_samples(send_fn, model, rng, target=3, max_total=None, max_tokens=4096):
    """采集数字样本：目标 target 个可用，最多尝试 max_total 次（默认 target+3）。

    说明（验收修正）：部分上游对「生成 N 个」的计数遵从度较差，单次请求
    可能产出不可用样本（数量超出容差）。允许有限次数的补采，保证「凑满
    target 个有效样本」的判定有机会成立；次数上限避免无限消耗用量。

    `send_fn(model, messages, max_tokens) -> ProbeResult`。
    返回 (usable, evidence, used_outputs)。
    """
    target = int(target or 3)
    max_total = int(max_total or (target + 3))
    usable, evidence, used_outputs = [], [], []
    attempts = 0
    while len(usable) < target and attempts < max_total:
        attempts += 1
        expected = rng.randint(FINGERPRINT_N_MIN, FINGERPRINT_N_MAX)
        prompt = FINGERPRINT_TEMPLATE.format(N=expected, M=FINGERPRINT_M)
        res = send_fn(model, [{"role": "user", "content": prompt}], max_tokens)
        numbers = F.parse_integers(res.text) if res.ok else []
        parsed = len(numbers)
        ok_sample = bool(res.ok and F.usable_sample(parsed, expected))
        evidence.append({
            "id": f"numeric-{attempts}",
            "expected_count": expected,
            "parsed_count": parsed,
            "usable": ok_sample,
            "attempts": res.attempts,
            "http_status": res.http_status,
            "ms": res.ms,
            "error": res.err,
        })
        if ok_sample:
            vec = F.feature_vector(numbers, FINGERPRINT_M)
            usable.append({
                "expected": expected,
                "parsed_count": parsed,
                "numbers": numbers,
                "vector": vec,
            })
            used_outputs.append({
                "sample": len(usable),
                "expected_count": expected,
                "parsed_count": parsed,
                "mean": round(sum(numbers) / len(numbers), 3) if numbers else 0.0,
                "duplicate_rate": round(F.duplicate_rate(numbers), 4),
            })
    return usable, evidence, used_outputs


def run(ctx) -> dict:
    """执行指纹板块。ctx 见 engine.AuditContext。"""
    bank = ctx.bank
    claimed = ctx.model
    samples_n = int(ctx.samples or 3)

    verdict = {
        "behavioral_top3": [],
        "evidence": [],
        "family": "unknown",
        "family_name": "未建立数字基线",
        "family_prob": 0.0,
        "low_confidence": True,
        "note": "",
        "requests": 0,
        "results": [],
        "top_model": None,
        "top_prob": 0.0,
        "used_outputs": [],
        "max_points": MAX_POINTS,
        "earned": 0,
        "applicable": True,
        "known": False,
        "score": 0,
        "status": "unknown",
    }

    # 银行内没有任何可用基线时，不消耗用量，直接返回该状态（规格 §3.1 末段）
    if not bank or not bank.entries:
        verdict["note"] = "基线库为空，请先执行 `bank build` 建立数字基线。"
        verdict["status"] = "no_baseline"
        return verdict

    rng = ctx.rng or random.Random()

    def _send(model, messages, max_tokens):
        return send(ctx.client, model, messages=messages, max_tokens=max_tokens)

    usable, evidence, used_outputs = collect_samples(
        _send, claimed, rng,
        target=samples_n, max_total=samples_n + 3,
        max_tokens=ctx.max_tokens_for_fingerprint,
    )
    verdict["evidence"] = evidence
    verdict["used_outputs"] = used_outputs
    verdict["requests"] = len(evidence)
    sample_vectors = [u["vector"] for u in usable]
    usable_count = len(sample_vectors)

    if usable_count == 0:
        verdict["note"] = "样本不足（0 个可用样本），无法归因；已计入 0 分。"
        verdict["status"] = "insufficient"
        return verdict

    # --- 与基线比对 ---
    results = []
    per_model_sims = {}
    for model in bank.models():
        center = bank.center_of(model)
        if not center:
            continue
        sims = [F.cosine(vec, center) for vec in sample_vectors]
        for idx, sim in enumerate(sims):
            results.append({
                "model": model,
                "sample": idx + 1,
                "similarity": round(sim, 6),
            })
        per_model_sims[model] = sum(sims) / len(sims)

    verdict["results"] = results
    if not per_model_sims:
        verdict["note"] = "基线库内无有效中心向量，无法归因；已计入 0 分。"
        verdict["status"] = "insufficient"
        return verdict

    models = sorted(per_model_sims)
    sims = [per_model_sims[m] for m in models]
    probs = F.softmax(sims, scale=SOFTMAX_SCALE)
    ranked = sorted(zip(models, sims, probs), key=lambda t: -t[1])
    top_model, top_sim, top_prob = ranked[0]

    verdict["top_model"] = top_model
    verdict["top_prob"] = round(top_prob, 6)
    verdict["behavioral_top3"] = [
        {"model": m, "similarity": round(s, 6), "probability": round(p, 6)}
        for m, s, p in ranked[:3]
    ]

    # --- 家族概率：同家族概率求和 ---
    fam_of = {m: bank.family_of(m)[0] for m in models}
    fam_sims = {}
    for m, s in zip(models, sims):
        fam_sims.setdefault(fam_of[m], []).append(s)
    fam_mean = {f: sum(v) / len(v) for f, v in fam_sims.items()}
    fam_list = sorted(fam_mean)
    fam_probs = F.softmax([fam_mean[f] for f in fam_list], scale=SOFTMAX_SCALE)
    fam_ranked = sorted(zip(fam_list, fam_probs), key=lambda t: -t[1])

    top_family, family_prob = fam_ranked[0]
    top_family_name = bank.family_of(top_model)[1] if top_model else "未知"
    verdict["family"] = top_family
    verdict["family_name"] = top_family_name
    verdict["family_prob"] = round(family_prob, 6)

    claimed_family, claimed_family_name = bank.family_of(claimed)
    low_confidence = top_prob < 0.5 or usable_count < samples_n
    verdict["low_confidence"] = bool(low_confidence)

    # --- 计分（部分分公式为推断值，写入 note） ---
    note_parts = ["部分分公式（同家族 6 分）为按观察推断的值，非上游原始实现。"]
    if usable_count < samples_n:
        note_parts.append(f"可用样本 {usable_count}/{samples_n} 个。")

    if usable_count >= samples_n and top_model == claimed:
        points, status = MAX_POINTS, "match"
        note_parts.append(f"指纹与声称模型 {claimed} 一致。")
    elif claimed_family and top_family == claimed_family and claimed_family != "unknown":
        points, status = 6, "family"
        if top_model == claimed:
            note_parts.append(
                f"最接近的模型即声称模型 {claimed}，但可用样本仅 {usable_count}/{samples_n}，"
                "样本不足以判满分，按家族一致（推断公式）记 6 分。"
            )
        else:
            note_parts.append(
                f"指纹最接近 {top_model}（同族 {claimed_family}），非声称型号 {claimed}，"
                "按推断公式记 6 分。"
            )
    else:
        points, status = 0, "mismatch"
        if usable_count < samples_n:
            note_parts.append("样本不足，按 0 分计入。")
        else:
            note_parts.append(
                f"指纹最接近 {top_model}，与声称模型 {claimed} 不一致，记 0 分。"
            )

    verdict["earned"] = points
    verdict["score"] = points
    verdict["known"] = True
    verdict["status"] = status
    verdict["note"] = " ".join(note_parts)
    return verdict
