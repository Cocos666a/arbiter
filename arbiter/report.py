"""报告组装与静态 HTML 渲染。

规格 §5：report.json 顶层键对齐上游，report.html 为单文件静态页（无外部资源）。
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
from datetime import datetime
from urllib.parse import urlparse

from .http_client import redact_deep
from .sections import SECTION_NAMES

TOP_LEVEL_KEYS = (
    "base_url", "created_at", "host", "id", "model",
    "status", "probes", "verdict",
)

# 卡片展示顺序与徽章文案
CARD_ORDER = [
    "fingerprint", "iq", "injection", "hidden",
    "tools", "cache", "web", "knowledge", "thinking",
]

STATUS_LABELS = {
    "pass": "通过", "fail": "未通过", "clean": "未检出", "injected": "检出注入",
    "suspect": "存疑", "unknown": "未判定", "error": "执行失败", "match": "命中",
    "family": "同家族", "mismatch": "不匹配", "measured": "已测量",
    "insufficient": "样本不足", "no_baseline": "无基线", "estimated": "已估计",
    "unreported": "未上报", "partial": "部分完成", "unreliable": "不可靠",
}

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(_PKG_DIR)

# 模板随包分发（arbiter/templates/），保证 `pip install` 后仍能找到；
# 同时保留仓库根 templates/ 作为回退（本地开发/复刻场景）。
TEMPLATE_PATH = os.path.join(_PKG_DIR, "templates", "report.html.tpl")
if not os.path.isfile(TEMPLATE_PATH):  # pragma: no cover - 兼容旧布局
    TEMPLATE_PATH = os.path.join(_REPO_DIR, "templates", "report.html.tpl")

# 模板占位符：@@key@@
_PLACEHOLDER_RE = re.compile(r"@@([A-Za-z_][A-Za-z0-9_]*)@@")


# ---------------------------------------------------------------------------
# report.json
# ---------------------------------------------------------------------------


def new_job_id() -> str:
    """32 位十六进制 job id。"""
    return secrets.token_hex(16)


def build_report(base_url: str, model: str, audit: dict) -> dict:
    """组装 report.json 的顶层结构。"""
    parsed = urlparse(base_url if "://" in base_url else "https://" + base_url)
    report = {
        "base_url": base_url,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": parsed.hostname or "",
        "id": new_job_id(),
        "model": model,
        "status": "done",
        "probes": audit.get("probes") or [],
        "verdict": audit.get("verdict") or {},
    }
    if audit.get("skipped"):
        report["skipped"] = audit["skipped"]
    return redact_deep(report)


def report_filenames(model: str, when: datetime | None = None) -> tuple:
    """生成报告文件名（规格 §5）。"""
    when = when or datetime.now()
    stamp = when.strftime("%Y%m%d-%H%M%S")
    safe_model = re.sub(r"[^A-Za-z0-9._\-]", "_", model or "model")
    return f"report_{safe_model}_{stamp}.json", f"report_{safe_model}_{stamp}.html"


def save_report(report: dict, out_dir: str = "reports") -> tuple:
    """写出 report.json 与 report.html，返回 (json_path, html_path)。"""
    os.makedirs(out_dir, exist_ok=True)
    json_name, html_name = report_filenames(report.get("model", "model"))
    json_path = os.path.join(out_dir, json_name)
    html_path = os.path.join(out_dir, html_name)

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(report))
    return json_path, html_path


def load_report(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def summary_line(section_id: str, data: dict) -> str:
    """每节的一句话结论摘要（HTML 卡片与 CLI 摘要共用）。"""
    if not isinstance(data, dict):
        return ""
    if section_id == "fingerprint":
        if data.get("status") == "no_baseline":
            return "基线库为空，未做归因"
        return (
            f"归属 {data.get('top_model') or '未知'}"
            f"（{data.get('top_prob', 0):.0%}），家族 {data.get('family_name') or '未知'}"
        )
    if section_id == "iq":
        questions = data.get("per_question") or []
        done = sum(1 for q in questions if not q.get("error"))
        return f"卷面 {data.get('score')}/{data.get('max')}，{done}/{len(questions)} 题完成"
    if section_id == "injection":
        if data.get("status") in ("error", "unreliable"):
            return "未能测量"
        return f"估算注入 {data.get('inj')} tokens，斜率 {data.get('slope')}"
    if section_id == "hidden":
        return (
            f"承认 {data.get('ack_count')} / 否认 {data.get('deny_count')} / "
            f"共 {data.get('attempts')} 问"
        )
    if section_id == "tools":
        return "工具调用与结果回传均通过" if data.get("pass") else "未通过"
    if section_id == "cache":
        if data.get("status") == "error":
            return "会话未完成，本节不计分"
        return f"末轮读 {data.get('actual_read')} / 预期 {data.get('expected_read')}"
    if section_id == "web":
        if data.get("pass") is None:
            return "未适用（该协议没有统一的服务端联网工具）"
        return "检出搜索工具执行证据" if data.get("pass") else "未检出搜索执行"
    if section_id == "knowledge":
        return data.get("label") or "未估计"
    if section_id == "thinking":
        return f"思维链 {data.get('think_len')} 字符，签名 {data.get('sig_len')} 字符"
    return ""


def _card(section_id: str, data: dict, coefficient: int | None) -> str:
    if not isinstance(data, dict):
        data = {}
    status = data.get("status", "unknown")
    badge = STATUS_LABELS.get(status, status)
    earned = data.get("earned", data.get("score", 0))
    max_points = data.get("max_points")
    if max_points is None:
        max_points = data.get("max_score", "")

    coef_line = f"<span class=\"coef\">系数 {coefficient}</span>" if coefficient else ""
    score_line = (
        f"<span class=\"pts\">{_esc(earned)} / {_esc(max_points)}</span>"
        if max_points != "" else ""
    )
    reason = data.get("reason") or data.get("note") or ""

    detail = _detail(section_id, data)
    return f"""
    <section class="card">
      <header>
        <h2>{_esc(SECTION_NAMES.get(section_id, section_id))}</h2>
        <span class="badge badge-{_esc(status)}">{_esc(badge)}</span>
      </header>
      <p class="summary">{_esc(summary_line(section_id, data))}</p>
      <p class="meta">{score_line} {coef_line}</p>
      {f'<p class="reason">{_esc(reason)}</p>' if reason else ''}
      {detail}
    </section>"""


def _detail(section_id: str, data: dict) -> str:
    """折叠详情：展示关键证据摘要。"""
    rows = []
    if section_id == "fingerprint":
        for item in data.get("evidence") or []:
            rows.append(
                f"{item.get('id')}：期望 {item.get('expected_count')}，"
                f"解析 {item.get('parsed_count')}，可用={item.get('usable')}"
            )
    elif section_id == "iq":
        for q in data.get("per_question") or []:
            mark = "✓" if q.get("earned") else ("½" if q.get("half") else "✗")
            rows.append(
                f"{mark} {q.get('id')} {q.get('title')}：{q.get('answer') or '（无回答）'}"
            )
    elif section_id == "hidden":
        for ask in data.get("asks") or []:
            rows.append(f"{ask.get('id')} [{ask.get('stance')}]：{ask.get('answer') or '（空）'}")
    elif section_id == "cache":
        for s in [data.get("first")] + list(data.get("samples") or []):
            if not s:
                continue
            rows.append(
                f"第 {s.get('index')} 轮：total={s.get('total')} read={s.get('read')} "
                f"write={s.get('write')}"
            )
    elif section_id == "injection":
        for p in data.get("points") or []:
            rows.append(f"n={p.get('n')} input={p.get('input')} total={p.get('total')}")
    elif section_id == "tools":
        for call in (data.get("evidence") or {}).get("calls") or []:
            rows.append(f"{call.get('name')}({call.get('arguments')}) id={call.get('id')}")
    elif section_id == "knowledge":
        for r in data.get("rows") or []:
            rows.append(f"{r.get('happened_at')} {r.get('event')} → {r.get('kind')}")
    elif section_id == "thinking":
        rows.append(f"签名长度 {data.get('sig_len')}")
        if data.get("thinking_text"):
            rows.append(data["thinking_text"][:200])

    if not rows:
        return ""
    items = "".join(f"<li>{_esc(r)}</li>" for r in rows[:40])
    return f'<details><summary>详情</summary><ul>{items}</ul></details>'


def render_html(report: dict) -> str:
    """渲染静态 HTML（单文件、无外部资源）。"""
    verdict = report.get("verdict") or {}
    total = verdict.get("total") or {}
    integrity = verdict.get("integrity") or {}
    coefs = total.get("coefficients") or {}

    cards = "".join(
        _card(sid, verdict.get(sid) or {}, coefs.get(sid))
        for sid in CARD_ORDER
    )

    probes = report.get("probes") or []
    probe_rows = "".join(
        f"<tr><td>{_esc(p.get('id'))}</td><td>{_esc(p.get('name'))}</td>"
        f"<td>{_esc(p.get('status'))}</td><td>{_esc(p.get('ms'))} ms</td>"
        f"<td>{_esc(p.get('error') or '')}</td></tr>"
        for p in probes
    )

    skipped = report.get("skipped") or []
    skipped_html = ""
    if skipped:
        items = "".join(
            f"<li>{_esc(s.get('id'))}：{_esc(s.get('reason'))}</li>" for s in skipped
        )
        skipped_html = f'<div class="skipped"><strong>已跳过</strong><ul>{items}</ul></div>'

    context = {
        "model": _esc(report.get("model")),
        "host": _esc(report.get("host")),
        "base_url": _esc(report.get("base_url")),
        "id": _esc(report.get("id")),
        "created_at": _esc(report.get("created_at")),
        "protocol": _esc(verdict.get("protocol")),
        "scoring_version": _esc(verdict.get("scoring_version") or total.get("scoring_version")),
        "score": _esc(total.get("score", 0)),
        "total_reason": _esc(total.get("reason")),
        "integrity_score": _esc(integrity.get("score", 0)),
        "integrity_status": _esc(integrity.get("status")),
        "integrity_coverage": _esc(integrity.get("coverage", 0)),
        "cards": cards,
        "probe_rows": probe_rows,
        "skipped": skipped_html,
    }

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        template = fh.read()
    # 模板用 @@key@@ 占位（避开 CSS 的花括号与百分号）。
    # 单次扫描替换：避免逐键 replace 时，先插入的报告内容里若含 @@其他键@@ 被二次替换。
    return _PLACEHOLDER_RE.sub(
        lambda m: str(context.get(m.group(1), m.group(0))), template
    )
