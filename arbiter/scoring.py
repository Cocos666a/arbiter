"""评分系统：权重分配（最大余数法）、贡献 floor、总分、integrity。

规格 §4 是硬契约，公式不得改动：
- 系数：`coef_i = floor(w_i / W * 100)`，余数最大的若干节各 +1 补足 100；
- 贡献：`contribution_i = floor(points_i / max_points_i * coef_i)`；
- 总分：Σ contribution（0–100 整数）；
- integrity 用独立权重，衡量证据完整度。
"""

from __future__ import annotations

import math

SCORING_VERSION = "repro-0.1"

# 总分权重（规格 §4）
WEIGHTS = {
    "fingerprint": 30,
    "iq": 25,
    "injection": 12,
    "hidden": 10,
    "tools": 8,
    "web": 6,
    "thinking": 5,
    "cache": 4,
}

# 展示名（与报告卡片一致）
SECTION_LABELS = {
    "fingerprint": "模型指纹归因",
    "iq": "推理表现",
    "injection": "注入大小",
    "hidden": "隐藏指令检测",
    "tools": "工具调用",
    "web": "联网检测",
    "thinking": "思维链回传",
    "cache": "缓存读写",
}


# integrity 独立权重（规格 §4）
INTEGRITY_WEIGHTS = {
    "injection": 20,
    "cache": 20,
    "tools": 20,
    "web": 15,
    "stream": 15,
    "thinking": 10,
}


# ---------------------------------------------------------------------------
# 最大余数法
# ---------------------------------------------------------------------------


def allocate_coefficients(weights: dict, total: int = 100) -> dict:
    """按最大余数法把 100 分分配给各节。

    并列时的次序：权重降序 → key 字典序升序。保证 Σcoef == total。
    """
    items = [(k, w) for k, w in weights.items() if w and w > 0]
    if not items:
        return {}

    weight_sum = sum(w for _, w in items)
    coefs, remainders = {}, []
    for key, weight in items:
        exact = weight / weight_sum * total
        base = int(math.floor(exact))
        coefs[key] = base
        remainders.append((exact - base, weight, key))

    missing = total - sum(coefs.values())
    # 余数降序；并列按权重降序，再按 key 字典序
    remainders.sort(key=lambda t: (-t[0], -t[1], t[2]))
    for i in range(max(0, missing)):
        if i >= len(remainders):
            break
        coefs[remainders[i][2]] += 1
    return coefs


def contribution(points: float, max_points: float, coef: int) -> int:
    """单节贡献分：`floor(points / max_points * coef)`。"""
    if not max_points or max_points <= 0:
        return 0
    ratio = max(0.0, min(1.0, float(points) / float(max_points)))
    return int(math.floor(ratio * coef))


def total_score(sections: dict) -> dict:
    """计算总分与逐节贡献。

    `sections` 结构：`{id: {"points":float, "max_points":float,
    "applicable":bool, "known":bool}}`。
    仅 applicable 的节参与权重分配；knowledge 属 applicable=False 天然剔除。
    """
    applicable = {
        k: v for k, v in sections.items()
        if v.get("applicable") and WEIGHTS.get(k)
    }
    weights = {k: WEIGHTS[k] for k in applicable}
    coefs = allocate_coefficients(weights) if weights else {}

    rows, total = [], 0
    for key in sorted(applicable):
        spec = applicable[key]
        coef = coefs.get(key, 0)
        got = contribution(spec.get("points", 0), spec.get("max_points", 0), coef)
        total += got
        rows.append({
            "id": key,
            "key": key,
            "label": SECTION_LABELS.get(key, key),
            "applicable": True,
            "weight": WEIGHTS[key],
            "coefficient": coef,
            "points": spec.get("points", 0),
            "max_points": spec.get("max_points", 0),
            "contribution": got,
            "known": bool(spec.get("known")),
        })

    return {
        "score": total,
        "max": 100,
        "coefficients": coefs,
        "applicable_weight": sum(weights.values()),
        "rows": rows,
        "parts": rows,  # 上游命名别名，便于互通
        "scoring_version": SCORING_VERSION,
    }


# ---------------------------------------------------------------------------
# integrity：证据完整度
# ---------------------------------------------------------------------------


def integrity(signals: dict) -> dict:
    """证据完整度（独立权重，不参与总分）。

    `signals` 结构：`{name: {"points":float, "max_points":float,
    "applicable":bool, "known":bool}}`，合法 name 为 INTEGRITY_WEIGHTS 的键。
    """
    denom = sum(
        INTEGRITY_WEIGHTS[k] for k, v in signals.items()
        if v.get("applicable") and k in INTEGRITY_WEIGHTS
    )
    numerator = 0.0
    known_weight = 0
    known_count = 0
    applicable_count = 0
    rows = []
    for key in sorted(INTEGRITY_WEIGHTS):
        spec = signals.get(key)
        weight = INTEGRITY_WEIGHTS[key]
        if not spec or not spec.get("applicable"):
            rows.append({"id": key, "weight": weight, "state": "not_applicable",
                         "known": False, "points": 0, "max_points": 0})
            continue
        applicable_count += 1
        max_points = spec.get("max_points") or 0
        ratio = (float(spec.get("points", 0)) / max_points) if max_points else 0.0
        if spec.get("known"):
            numerator += ratio * weight
            known_weight += weight
            known_count += 1
        rows.append({
            "id": key,
            "weight": weight,
            "state": "known" if spec.get("known") else "unknown",
            "known": bool(spec.get("known")),
            "points": spec.get("points", 0),
            "max_points": max_points,
        })

    score = (numerator / denom * 100.0) if denom else 0.0
    # coverage：证据覆盖率（已确认信号数 / 适用信号数），对齐上游字段语义
    coverage = (known_count / applicable_count * 100.0) if applicable_count else 0.0
    all_known = all(
        (not v.get("applicable")) or v.get("known")
        for k, v in signals.items() if k in INTEGRITY_WEIGHTS
    ) and bool(signals)
    return {
        "available_weight": known_weight,
        "applicable_weight": denom,
        "coverage": round(coverage, 3),
        "score": int(round(score)),
        "status": "complete" if all_known else "partial",
        "signals": rows,
    }


# ---------------------------------------------------------------------------
# 总分说明
# ---------------------------------------------------------------------------


def total_reason(sections: dict, skipped: list | None = None) -> str:
    """生成总分区说明文字（模板见规格 §4）。"""
    inactive = []
    names = {
        "fingerprint": "指纹", "iq": "推理", "injection": "注入",
        "hidden": "隐藏指令", "tools": "工具", "web": "联网",
        "thinking": "思维链", "cache": "缓存",
    }
    for key in ("web", "cache", "thinking"):
        spec = sections.get(key) or {}
        if not spec.get("applicable"):
            inactive.append(names[key])

    head = "总分 100。指纹系数最高。"
    if inactive:
        head += "、".join(inactive) + "未启用，这些项不占分，其余按原权重重分配，全通过仍是 100。"
    else:
        head += "全部板块均适用，按原权重分配。"
    head += "未完成的适用项按 0 分计入。知识截止日期只展示，不计入。"
    if skipped:
        head += "跳过的板块：" + "、".join(s.get("id", "") for s in skipped) + "。"
    return head
