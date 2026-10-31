"""审计编排：按 sections 调度各板块、收集 timing、组装 verdict、计算评分。

规格 §3 与 §4。引擎负责：
- 构造审计上下文（客户端、基线库、随机源）；
- 逐节调用 `run(ctx)`，捕获 section 级异常并标记 `status:"error"`；
- 汇总每节的 points/max_points/applicable/known，交给 scoring 算总分与 integrity。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import date

from . import scoring
from .bank import Bank
from .http_client import HttpClient
from .sections import NON_SCORING, SECTION_IDS, SECTION_NAMES

# 各节默认 max_tokens
FINGERPRINT_MAX_TOKENS = 4096


@dataclass
class AuditContext:
    """单次审计的运行上下文。"""

    client: HttpClient
    model: str
    protocol: str
    bank: Bank
    samples: int = 3
    rng: random.Random = field(default_factory=random.Random)
    today: str = field(default_factory=lambda: date.today().isoformat())
    max_tokens_for_fingerprint: int = FINGERPRINT_MAX_TOKENS


def load_section(section_id: str):
    """按需导入板块模块，避免无关板块的导入开销。"""
    from importlib import import_module
    return import_module(f".sections.{section_id}", package="arbiter")


def normalize_sections(raw: str | None, model: str) -> tuple:
    """解析 `--sections` 参数，返回 (待跑板块, 跳过说明)。

    `all` 展开为全部板块；thinking 对非 claude- 模型自动跳过并记入 skipped。
    """
    skipped = []
    if not raw or raw.strip().lower() == "all":
        selected = list(SECTION_IDS)
    else:
        requested = [s.strip() for s in raw.split(",") if s.strip()]
        unknown = [s for s in requested if s not in SECTION_IDS]
        if unknown:
            raise ValueError(
                "未知板块：" + ", ".join(unknown) + "；可用：" + ", ".join(SECTION_IDS)
            )
        selected = [s for s in SECTION_IDS if s in requested]

    if "thinking" in selected and not (model or "").lower().startswith("claude-"):
        selected = [s for s in selected if s != "thinking"]
        skipped.append({
            "id": "thinking",
            "reason": "思维链回传只适用于 claude- 前缀模型，已跳过",
        })
    return selected, skipped


def run_audit(
    base_url: str,
    model: str,
    api_key: str,
    protocol: str,
    sections_raw: str | None = None,
    timeout: float = 360.0,
    bank_path: str = "bank/bank.json",
    samples: int = 3,
    retries: int = 3,
    backoff: tuple = (2.0, 5.0, 10.0),
    progress=None,
) -> dict:
    """执行一次完整审计，返回报告 verdict 与 probes。

    返回 `{"verdict": {...}, "probes": [...], "skipped": [...],
    "protocol": str, "timing": {...}}`。

    `retries` / `backoff` 默认遵循规格 §0.4（≤3 次重试，2s/5s/10s 退避）；
    离线测试可传 `retries=0` 让失败路径立即返回。
    """
    selected, skipped = normalize_sections(sections_raw, model)
    client = HttpClient(
        base_url, api_key, timeout=timeout, retries=retries,
        backoff=backoff, protocol=protocol,
    )
    bank = Bank.load(bank_path) if "fingerprint" in selected else Bank(path=bank_path)

    ctx = AuditContext(
        client=client, model=model, protocol=protocol, bank=bank, samples=samples,
    )

    verdict = {"protocol": protocol, "scoring_version": scoring.SCORING_VERSION}
    probes = []
    timing = {"total_ms": 0, "sections": {}}

    started_all = time.perf_counter()
    for section_id in selected:
        started = time.perf_counter()
        if progress:
            progress(section_id, "start", None)
        try:
            module = load_section(section_id)
            result = module.run(ctx)
            status = result.get("status", "ok")
            error = None
        except Exception as exc:  # noqa: BLE001 - 单节异常不应中断整个审计
            result = {
                "status": "error",
                "reason": f"板块执行异常：{type(exc).__name__}: {exc}",
                "earned": 0,
                "max_points": scoring.WEIGHTS.get(section_id, 0) or 1,
                "applicable": False,
                "known": False,
                "score": 0,
            }
            status = "error"
            error = f"{type(exc).__name__}: {exc}"

        elapsed = int((time.perf_counter() - started) * 1000)
        timing["sections"][section_id] = elapsed
        verdict[section_id] = result

        probe = {
            "id": section_id,
            "name": SECTION_NAMES.get(section_id, section_id),
            "status": status,
            "ms": elapsed,
        }
        if error:
            probe["error"] = error
        probes.append(probe)
        if progress:
            progress(section_id, "done", result)

    # IQ 的 8 道题各记一条 probe（规格 §5）
    iq_result = verdict.get("iq") or {}
    for question in iq_result.get("per_question") or []:
        probes.append({
            "id": question.get("id"),
            "name": question.get("title"),
            "status": "error" if question.get("error") else "ok",
            "ms": question.get("ms", 0),
            **({"error": question["error"]} if question.get("error") else {}),
        })

    # 被跳过的板块在上游报告里以 null 占位（如非 claude 模型的 thinking）：
    # 保持同样的键集合，便于逐节对照（None 不参与计分）。
    for item in skipped:
        verdict.setdefault(item.get("id"), None)

    # 被跳过的板块在上游报告里以 null 占位（如非 claude 模型的 thinking）：
    # 保持同样的键集合，便于逐节对照（None 不参与计分）。
    for item in skipped:
        verdict.setdefault(item.get("id"), None)

    timing["total_ms"] = int((time.perf_counter() - started_all) * 1000)
    verdict["timing"] = timing

    # --- 评分 ---
    verdict["total"] = _build_total(verdict, skipped)
    verdict["integrity"] = _build_integrity(verdict, iq_result)

    return {
        "verdict": verdict,
        "probes": probes,
        "skipped": skipped,
        "protocol": protocol,
        "timing": timing,
    }


def _section_spec(result: dict, section_id: str) -> dict:
    """从节结果里抽取计分所需字段。

    节未运行（被跳过或被 --sections 排除）时不适用，不得计入分母。
    """
    if not result:
        return {"points": 0, "max_points": 1, "applicable": False, "known": False}

    max_points = result.get("max_points")
    if max_points is None:
        max_points = result.get("max_score")
    if max_points is None:
        max_points = scoring.WEIGHTS.get(section_id, 0) or 1
    earned = result.get("earned")
    if earned is None:
        earned = result.get("score", 0)
    applicable = result.get("applicable", section_id not in NON_SCORING)
    if section_id in NON_SCORING:
        applicable = False
    return {
        "points": earned,
        "max_points": max_points,
        "applicable": bool(applicable),
        "known": bool(result.get("known", False)),
    }


def _build_total(verdict: dict, skipped: list) -> dict:
    specs = {
        sid: _section_spec(verdict.get(sid) or {}, sid)
        for sid in SECTION_IDS
        if sid in verdict
    }
    result = scoring.total_score(specs)
    result["reason"] = scoring.total_reason(
        {sid: {"applicable": s["applicable"]} for sid, s in specs.items()},
        skipped,
    )
    result["max"] = 100
    return result


def _build_integrity(verdict: dict, iq_result: dict) -> dict:
    """integrity 的信号来自各节 + stream（IQ 全题完成的流式完整性）。

    只统计本次实际运行的板块：被跳过的（如非 claude 模型的 thinking）
    视为不适用，不计入分母。
    """
    signals = {}
    for key in scoring.INTEGRITY_WEIGHTS:
        if key == "stream":
            per_question = iq_result.get("per_question") or []
            if not per_question:
                signals["stream"] = {"applicable": False, "known": False,
                                     "points": 0, "max_points": 0}
                continue
            completed = sum(
                1 for q in per_question
                if (q.get("metrics") or {}).get("StreamComplete")
            )
            signals["stream"] = {
                "applicable": True,
                "known": True,
                "points": completed,
                "max_points": len(per_question),
            }
            continue

        if key not in verdict:
            # 该节本次未运行（跳过或未选择）→ 不适用
            signals[key] = {"applicable": False, "known": False,
                            "points": 0, "max_points": 0}
            continue
        signals[key] = _section_spec(verdict.get(key) or {}, key)

    result = scoring.integrity(signals)
    result["note"] = (
        "证据完整度按各适用板块的上报完整性与响应完整性加权，独立于总分。"
    )
    return result
