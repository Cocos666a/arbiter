"""命令行入口：run / bank build / bank show / render。

规格 §6 的 CLI 契约与退出码：
0=完成、1=运行错误、2=参数错误、3=完成但有板块失败。
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

from . import __version__
from . import report as report_mod
from . import scoring
from .bank import DEFAULT_BANK_PATH, Bank
from .engine import load_section, normalize_sections, run_audit
from .http_client import HttpClient, redact
from .protocols import VALID_PROTOCOLS, resolve_protocol
from .sections import SECTION_IDS, SECTION_NAMES

DEFAULT_KEY_ENV = "ARBITER_API_KEY"

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 3


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def _add_key_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--key-file", metavar="PATH",
                       help="从外部文件读取 API key（首行，非空）")
    group.add_argument("--api-key-env", metavar="VAR", nargs="?", const=DEFAULT_KEY_ENV,
                       default=DEFAULT_KEY_ENV,
                       help=f"从环境变量读取 API key（默认 {DEFAULT_KEY_ENV}）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arbiter",
        description="Arbiter：对 OpenAI 兼容网关执行九板块「掺水」自检。",
    )
    parser.add_argument("--version", action="version", version=f"arbiter {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    run_p = sub.add_parser("run", help="执行一次完整审计")
    run_p.add_argument("--base-url", required=True, help="网关地址，带或不带 /v1 均可")
    run_p.add_argument("--model", required=True, help="声称的模型名")
    _add_key_args(run_p)
    run_p.add_argument("--protocol", default="auto", choices=list(VALID_PROTOCOLS),
                       help="协议，默认 auto（按模型名前缀判定）")
    run_p.add_argument("--sections", default="all",
                       help="英文板块 id 逗号分隔，或 all（默认）")
    run_p.add_argument("--timeout", type=float, default=360.0, help="单请求超时秒数")
    run_p.add_argument("--out", default="reports", help="报告输出目录")
    run_p.add_argument("--bank", default=DEFAULT_BANK_PATH, help="指纹基线库路径")
    run_p.add_argument("--samples", type=int, default=3, help="指纹采样次数")
    run_p.add_argument("--quiet", action="store_true", help="只输出最终摘要")

    # --- bank build ---
    bank_p = sub.add_parser("bank", help="指纹基线库操作")
    bank_sub = bank_p.add_subparsers(dest="bank_command", required=True)

    build_p = bank_sub.add_parser("build", help="为指定模型建立数字基线")
    build_p.add_argument("--base-url", required=True)
    build_p.add_argument("--model", required=True)
    _add_key_args(build_p)
    build_p.add_argument("--protocol", default="auto", choices=list(VALID_PROTOCOLS))
    build_p.add_argument("--timeout", type=float, default=360.0)
    build_p.add_argument("--samples", type=int, default=3)
    build_p.add_argument("--bank", default=DEFAULT_BANK_PATH)
    build_p.add_argument("--family", default="unknown", help="家族标签，用于同家族部分分")
    build_p.add_argument("--family-name", default="", help="家族展示名")
    build_p.add_argument("--quiet", action="store_true")

    show_p = bank_sub.add_parser("show", help="查看基线库内容")
    show_p.add_argument("--bank", default=DEFAULT_BANK_PATH)

    # --- render ---
    render_p = sub.add_parser("render", help="由 report.json 重新渲染 HTML")
    render_p.add_argument("--report", required=True, help="report.json 路径")
    render_p.add_argument("--out", help="HTML 输出路径；省略则同名同目录")

    return parser


# ---------------------------------------------------------------------------
# 密钥读取
# ---------------------------------------------------------------------------


def resolve_api_key(args) -> str:
    """按 --key-file / --api-key-env 读取密钥；缺失时抛 SystemExit(2)。"""
    key_file = getattr(args, "key_file", None)
    if key_file:
        if not os.path.exists(key_file):
            _fail_usage(f"密钥文件不存在：{key_file}")
        try:
            with open(key_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except OSError as exc:
            _fail_usage(f"读取密钥文件失败：{exc}")
        _fail_usage(f"密钥文件为空：{key_file}")

    env_name = getattr(args, "api_key_env", DEFAULT_KEY_ENV) or DEFAULT_KEY_ENV
    key = os.environ.get(env_name, "").strip()
    if not key:
        _fail_usage(
            f"未提供密钥：请用 --key-file 指定文件，或设置环境变量 {env_name}。"
        )
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in key):
        _fail_usage(
            "密钥含空白或控制字符；请检查 --key-file 的文件内容"
            "（第一行非空、非 # 注释行应为纯密钥）。"
        )
    if any(ord(ch) > 126 for ch in key):
        print(
            "[arbiter] 提示：密钥含非 ASCII 字符，将按 UTF-8 字节发送授权头"
            "（部分网关可能拒绝该形式）。",
            file=sys.stderr,
        )
    return key


class UsageError(Exception):
    """参数错误：由 main 转成退出码 2。"""


def _fail_usage(message: str) -> None:
    raise UsageError(message)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def cmd_run(args) -> int:
    api_key = resolve_api_key(args)
    try:
        selected, _ = normalize_sections(args.sections, args.model)
    except ValueError as exc:
        _fail_usage(str(exc))

    # thinking 被单独指定但模型不匹配 → 按 400 语义直接报错
    if args.sections and args.sections.strip().lower() != "all" and \
            "thinking" in [s.strip() for s in args.sections.split(",")] and \
            not args.model.lower().startswith("claude-"):
        _fail_usage("thinking 板块只适用于 claude- 前缀模型，当前模型不适用。")

    protocol = resolve_protocol(args.base_url, args.model, args.protocol, args.timeout)
    if not args.quiet:
        print(f"[arbiter] 目标 {args.base_url} 模型 {args.model} 协议 {protocol}")
        print(f"[arbiter] 板块：{', '.join(selected) or '（无）'}")

    started = time.perf_counter()
    audit = run_audit(
        base_url=args.base_url,
        model=args.model,
        api_key=api_key,
        protocol=protocol,
        sections_raw=args.sections,
        timeout=args.timeout,
        bank_path=args.bank,
        samples=args.samples,
        progress=_progress if not args.quiet else None,
    )
    elapsed = time.perf_counter() - started

    report = report_mod.build_report(args.base_url, args.model, audit)
    json_path, html_path = report_mod.save_report(report, args.out)

    _print_summary(report, json_path, html_path, elapsed)

    failed = [p for p in report["probes"] if p.get("status") == "error"]
    return EXIT_PARTIAL if failed else EXIT_OK


def _progress(section_id: str, phase: str, result) -> None:
    if phase == "start":
        print(f"  → {section_id} ({SECTION_NAMES.get(section_id, '')}) ...", flush=True)
    else:
        status = (result or {}).get("status", "ok")
        print(f"    ✓ {section_id}: {status}", flush=True)


def _print_summary(report: dict, json_path: str, html_path: str, elapsed: float) -> None:
    verdict = report.get("verdict") or {}
    total = verdict.get("total") or {}
    integrity = verdict.get("integrity") or {}

    print("")
    print("=" * 70)
    print(f"id         {report.get('id')}")
    print(f"模型       {report.get('model')}  @ {report.get('host')}")
    print(f"协议       {verdict.get('protocol')}   评分版本 {verdict.get('scoring_version')}")
    print(f"总分       {total.get('score', 0)} / 100")
    print(f"证据完整度 {integrity.get('score', 0)} / 100 （{integrity.get('status')}）")
    print("-" * 70)
    for row in total.get("rows") or []:
        sid = row["id"]
        data = verdict.get(sid) or {}
        status = data.get("status", "-")
        summary = report_mod.summary_line(sid, data)
        print(f"  {sid:<12} {row['points']:>5} / {row['max_points']:<5} "
              f"系数 {row['coefficient']:<3} 贡献 {row['contribution']:<3} [{status}]")
        if summary:
            print(f"  {'':<12} {summary}")
    # 不计分但需要展示结论的板块（知识截止）
    for sid in ("knowledge",):
        data = verdict.get(sid) or {}
        if data:
            print(f"  {sid:<12} {data.get('label') or '-'} （不计分）")
    for item in report.get("skipped") or []:
        print(f"  {item.get('id'):<12} 已跳过：{item.get('reason')}")
    print("-" * 70)
    print(f"用时       {elapsed:.1f}s")
    print(f"报告       {json_path}")
    print(f"           {html_path}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# bank build / bank show
# ---------------------------------------------------------------------------


def cmd_bank_build(args) -> int:
    api_key = resolve_api_key(args)
    protocol = resolve_protocol(args.base_url, args.model, args.protocol, args.timeout)
    if not args.quiet:
        print(f"[arbiter] 建立基线：{args.model} @ {args.base_url}（协议 {protocol}）")

    client = HttpClient(args.base_url, api_key, timeout=args.timeout, protocol=protocol)
    rng = random.Random()

    from .protocols import send
    from .sections.fingerprint import collect_samples

    def _send(model, messages, max_tokens):
        return send(client, model, messages=messages, max_tokens=max_tokens)

    usable, evidence, _ = collect_samples(
        _send, args.model, rng,
        target=args.samples, max_total=args.samples + 3,
    )
    for i, ev in enumerate(evidence, 1):
        if not args.quiet:
            print(f"  尝试 {i}：期望 {ev['expected_count']}，解析 {ev['parsed_count']}，可用={ev['usable']}")
        if not ev["usable"]:
            print(f"[arbiter] 尝试 {i} 不可用（计数未达容差），已跳过。", file=sys.stderr)

    samples = [
        {"n": u["expected"], "parsed_count": u["parsed_count"], "vector": u["vector"]}
        for u in usable
    ]
    if len(samples) < args.samples:
        print(f"[arbiter] 注意：仅集齐 {len(samples)}/{args.samples} 个可用样本。", file=sys.stderr)

    if not samples:
        print("[arbiter] 未取得任何可用样本，基线未更新。", file=sys.stderr)
        return EXIT_RUNTIME

    bank = Bank.load(args.bank)
    meta = {
        "family": args.family,
        "family_name": args.family_name or args.family,
    }
    bank.put(args.model, samples, meta=meta)
    path = bank.save(args.bank)
    print(f"[arbiter] 基线已写入 {path}（{len(samples)} 个样本）")
    return EXIT_OK


def cmd_bank_show(args) -> int:
    bank = Bank.load(args.bank)
    if not bank.entries:
        print(f"基线库为空或不存在：{args.bank}")
        return EXIT_OK
    print(f"基线库    {args.bank}")
    print(f"版本      {bank.created_at}")
    print(f"模型数    {len(bank.entries)}")
    for model in bank.models():
        entry = bank.get(model)
        meta = entry.get("meta") or {}
        samples = entry.get("samples") or []
        print(
            f"  {model:<28} 家族 {meta.get('family', 'unknown'):<12} "
            f"样本 {len(samples)}  更新 {entry.get('updated_at', '-')}"
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def cmd_render(args) -> int:
    if not os.path.exists(args.report):
        _fail_usage(f"报告不存在：{args.report}")
    report = report_mod.load_report(args.report)
    html_text = report_mod.render_html(report)
    out = args.out or os.path.splitext(args.report)[0] + ".html"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html_text)
    print(f"[arbiter] HTML 已写入 {out}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "bank":
            if args.bank_command == "build":
                return cmd_bank_build(args)
            if args.bank_command == "show":
                return cmd_bank_show(args)
        if args.command == "render":
            return cmd_render(args)
    except UsageError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n[arbiter] 已中断。", file=sys.stderr)
        return EXIT_RUNTIME
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底，错误信息需脱敏
        print(f"[arbiter] 运行错误：{redact(f'{type(exc).__name__}: {exc}')}",
              file=sys.stderr)
        return EXIT_RUNTIME

    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
