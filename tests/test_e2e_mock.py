"""端到端测试：对 mock 网关跑全量审计，断言报告结构完整。

全程离线，只访问 127.0.0.1 上由本测试启动的 mock 服务。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from mock_gateway import build_server  # noqa: E402

from arbiter import cli, report as report_mod  # noqa: E402
from arbiter.bank import Bank  # noqa: E402
from arbiter.engine import run_audit  # noqa: E402
from arbiter.protocols import CHAT, resolve_protocol  # noqa: E402

# fake key, not a credential（测试专用假值，不对应任何真实账号）
FAKE_KEY = "sk-TESTONLY-not-a-real-key"
MODEL = "mock-model"


class MockGatewayFixture:
    """上下文管理器：启动 mock 网关并返回 base_url。"""

    def __init__(self, injected: bool = False):
        self.server = build_server("127.0.0.1", 0, injected=injected)
        host, port = self.server.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.base_url

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        return False


def _seed_bank(path: str, model: str = MODEL) -> None:
    """写入一个与 mock 输出同分布的基线，让指纹节能走到归因逻辑。"""
    from arbiter import features as F
    from arbiter.prompts import FINGERPRINT_M, FINGERPRINT_TEMPLATE

    bank = Bank(path=path)
    samples = []
    for n in (300, 310, 290):
        text = _mock_numbers(FINGERPRINT_TEMPLATE.format(N=n, M=FINGERPRINT_M), n)
        numbers = F.parse_integers(text)
        samples.append({
            "n": n,
            "parsed_count": len(numbers),
            "vector": F.feature_vector(numbers, FINGERPRINT_M),
        })
    bank.put(model, samples, meta={"family": "mock-family", "family_name": "Mock 家族"})
    bank.save(path)


def _mock_numbers(prompt: str, count: int) -> str:
    """复刻 mock 网关的确定性数字生成，用于构造同分布基线。"""
    state = 12345
    out = []
    for _ in range(count):
        state = (1103515245 * state + 12345) % (2 ** 31)
        out.append(str(state % 400 + 1))
    return ", ".join(out)


class TestEndToEndMock(unittest.TestCase):
    """全量审计的端到端验证。"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="arbiter-e2e-")
        cls.bank_path = os.path.join(cls.tmpdir, "bank.json")
        _seed_bank(cls.bank_path)

        cls.gateway = MockGatewayFixture()
        cls.base_url = cls.gateway.__enter__()
        cls.audit = run_audit(
            base_url=cls.base_url,
            model=MODEL,
            api_key=FAKE_KEY,
            protocol=CHAT,
            sections_raw="all",
            timeout=30.0,
            bank_path=cls.bank_path,
            samples=3,
        )
        cls.report = report_mod.build_report(cls.base_url, MODEL, cls.audit)

    @classmethod
    def tearDownClass(cls):
        cls.gateway.__exit__()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # -- 报告结构 -----------------------------------------------------------

    def test_top_level_keys(self):
        expected = {
            "base_url", "created_at", "host", "id", "model",
            "status", "probes", "verdict",
        }
        self.assertTrue(expected.issubset(set(self.report.keys())))
        self.assertEqual(self.report["status"], "done")
        self.assertEqual(self.report["model"], MODEL)
        self.assertEqual(len(self.report["id"]), 32)
        int(self.report["id"], 16)  # 必须是合法十六进制

    def test_verdict_has_all_sections(self):
        verdict = self.report["verdict"]
        for section in ("fingerprint", "injection", "hidden", "cache", "tools",
                        "web", "knowledge", "iq"):
            self.assertIn(section, verdict, msg=section)

    def test_thinking_skipped_for_non_claude(self):
        """非 claude- 模型在 all 模式下跳过 thinking 并记入 skipped。

        跳过板块与上游报告一致，在 verdict 中以 None 占位（键集合齐九节），
        同时 skipped 列表保留跳过原因。
        """
        skipped_ids = [s["id"] for s in self.report.get("skipped") or []]
        self.assertIn("thinking", skipped_ids)
        self.assertIn("thinking", self.report["verdict"])
        self.assertIsNone(self.report["verdict"]["thinking"])

    def test_verdict_meta_keys(self):
        verdict = self.report["verdict"]
        for key in ("timing", "protocol", "scoring_version", "total",
                    "integrity", "iq"):
            self.assertIn(key, verdict)
        self.assertEqual(verdict["scoring_version"], "repro-0.1")
        self.assertEqual(verdict["protocol"], CHAT)

    def test_total_score_in_range(self):
        total = self.report["verdict"]["total"]
        self.assertIsInstance(total["score"], int)
        self.assertGreaterEqual(total["score"], 0)
        self.assertLessEqual(total["score"], 100)
        self.assertEqual(sum(total["coefficients"].values()), 100)

    def test_integrity_present(self):
        integrity = self.report["verdict"]["integrity"]
        self.assertIn(integrity["status"], ("complete", "partial"))
        self.assertGreaterEqual(integrity["score"], 0)
        self.assertLessEqual(integrity["score"], 100)

    def test_skipped_section_not_in_integrity_denominator(self):
        """被跳过的 thinking 必须标记为不适用，不得拉低证据完整度。"""
        signals = {s["id"]: s for s in self.report["verdict"]["integrity"]["signals"]}
        self.assertEqual(signals["thinking"]["state"], "not_applicable")
        self.assertFalse(signals["thinking"]["known"])
        # 不适用项权重不计入分母：injection20 + cache20 + tools20 + stream15 = 75
        self.assertEqual(self.report["verdict"]["integrity"]["applicable_weight"], 75)

    def test_fingerprint_scoring_keys(self):
        """指纹节用 earned 作计分键，不得与规格的 points 字段冲突。"""
        fp = self.report["verdict"]["fingerprint"]
        self.assertIn("earned", fp)
        self.assertIn("max_points", fp)
        self.assertNotIn("points", fp)

    # -- 各节结果 -----------------------------------------------------------

    def test_iq_complete(self):
        iq = self.report["verdict"]["iq"]
        self.assertTrue(iq["complete"], msg=json.dumps(iq, ensure_ascii=False)[:400])
        self.assertEqual(len(iq["per_question"]), 8)
        self.assertEqual(iq["max"], 100)
        self.assertEqual(iq["score"], 100)  # mock 全部答对

    def test_iq_metrics_keys(self):
        metrics = self.report["verdict"]["iq"]["per_question"][0]["metrics"]
        for key in ("Attempts", "Protocol", "Status", "Ms", "InputTokens",
                    "OutputTokens", "ThinkingTokens", "CacheRead", "ThinkLen",
                    "SigLen", "FirstByteMs", "StreamComplete"):
            self.assertIn(key, metrics)

    def test_fingerprint_attribution(self):
        fp = self.report["verdict"]["fingerprint"]
        self.assertEqual(fp["requests"], 3)
        self.assertEqual(len(fp["evidence"]), 3)
        self.assertTrue(all(e["usable"] for e in fp["evidence"]))
        self.assertEqual(fp["top_model"], MODEL)
        self.assertGreater(fp["top_prob"], 0.5)

    def test_tools_roundtrip(self):
        tools = self.report["verdict"]["tools"]
        self.assertTrue(tools["pass"], msg=str(tools.get("reason")))
        self.assertTrue(tools["evidence"]["roundtrip"])
        self.assertEqual(tools["evidence"]["calls"][0]["name"], "audit_echo")
        self.assertEqual(tools["evidence"]["calls"][0]["arguments"]["sum"], 42)

    def test_cache_six_rounds(self):
        cache = self.report["verdict"]["cache"]
        self.assertIsNotNone(cache["first"])
        self.assertEqual(len(cache["samples"]), 5)  # 轮 2..6
        self.assertTrue(cache["pass"], msg=str(cache.get("reason")))
        self.assertEqual(len(cache["prefix_sha256"]), 64)

    def test_hidden_clean(self):
        hidden = self.report["verdict"]["hidden"]
        self.assertEqual(hidden["attempts"], 7)
        self.assertEqual(len(hidden["asks"]), 6)
        self.assertEqual(hidden["ack_count"], 0)
        self.assertFalse(hidden["injected"])

    def test_injection_measured(self):
        injection = self.report["verdict"]["injection"]
        self.assertTrue(injection["reliable"])
        self.assertEqual(len(injection["points"]), 2)
        self.assertEqual(injection["points"][1]["n"], 1600)

    def test_web_not_applicable_for_chat(self):
        web = self.report["verdict"]["web"]
        self.assertIsNone(web["pass"])
        self.assertEqual(web["status"], "unknown")
        self.assertEqual(web["attempts"], 0)

    def test_knowledge_not_scored(self):
        knowledge = self.report["verdict"]["knowledge"]
        self.assertEqual(knowledge["max_points"], 0)
        self.assertFalse(knowledge["applicable"])
        self.assertTrue(knowledge["label"].startswith("知识"), knowledge["label"])

    # -- probes -------------------------------------------------------------

    def test_probes_cover_sections_and_iq(self):
        probes = self.report["probes"]
        ids = [p["id"] for p in probes]
        for section in ("fingerprint", "injection", "hidden", "cache",
                        "tools", "web", "knowledge", "iq"):
            self.assertIn(section, ids)
        for qid in ("Q1", "Q8"):
            self.assertIn(qid, ids)
        self.assertTrue(all("ms" in p and "status" in p for p in probes))

    # -- HTML ---------------------------------------------------------------

    def test_html_rendered(self):
        html_text = report_mod.render_html(self.report)
        self.assertIn("<!DOCTYPE html>", html_text)
        self.assertIn(MODEL, html_text)
        self.assertIn("100", html_text)
        self.assertIn("Arbiter", html_text)
        # 无外部资源
        for token in ("http://", "https://", "<script"):
            self.assertNotIn(token, html_text.replace(self.base_url, ""),
                             msg=f"HTML 不应包含 {token}")
        # 模板占位必须全部替换
        self.assertNotIn("@@", html_text)

    def test_save_report_writes_both_files(self):
        out_dir = os.path.join(self.tmpdir, "reports")
        json_path, html_path = report_mod.save_report(self.report, out_dir)
        self.assertTrue(os.path.exists(json_path))
        self.assertTrue(os.path.exists(html_path))
        self.assertTrue(os.path.basename(json_path).startswith(f"report_{MODEL}_"))
        with open(json_path, "r", encoding="utf-8") as fh:
            reloaded = json.load(fh)
        self.assertEqual(reloaded["model"], MODEL)

    def test_html_escapes_model_output(self):
        """模型回复进 HTML 必须转义，否则是注入面。"""
        report = {
            "model": MODEL, "host": "h", "base_url": "http://h", "id": "0" * 32,
            "created_at": "2026-01-01T00:00:00+08:00", "status": "done",
            "probes": [],
            "verdict": {
                "protocol": "chat", "scoring_version": "repro-0.1",
                "total": {"score": 0, "coefficients": {}, "rows": []},
                "integrity": {"score": 0, "status": "partial", "coverage": 0},
                "iq": {"per_question": [{
                    "id": "Q1", "title": "t", "answer": "<script>alert(1)</script>",
                    "earned": 0, "half": False,
                }]},
                "hidden": {"asks": [{
                    "id": "direct", "stance": "unclear",
                    "answer": "<img src=x onerror=alert(2)>",
                }]},
            },
        }
        html_text = report_mod.render_html(report)
        self.assertNotIn("<script>alert(1)</script>", html_text)
        self.assertNotIn("<img src=x onerror=alert(2)>", html_text)
        self.assertIn("&lt;script&gt;", html_text)

    def test_report_content_is_not_rescanned_as_placeholder(self):
        """单次替换：报告数据里即便含 @@key@@ 字样，也不会被当成模板占位再替换。

        多轮 str.replace 的老实现会在这里把模型名替换成总分值，属于数据被
        当成模板二次求值。
        """
        report = {
            "model": "@@score@@", "host": "h", "base_url": "http://h",
            "id": "0" * 32, "created_at": "t", "status": "done", "probes": [],
            "verdict": {
                "protocol": "chat", "scoring_version": "v",
                "total": {"score": 42, "coefficients": {}, "rows": []},
                "integrity": {"score": 0, "status": "partial", "coverage": 0},
            },
        }
        html_text = report_mod.render_html(report)
        # 模板占位正常取值
        self.assertIn(">42<", html_text)
        # 数据原样插入，未被二次求值（模型名保持字面量）
        self.assertIn("@@score@@", html_text)
        # 总分是 42 而不是被模型名顶掉
        self.assertNotIn('class="score-num">@@score@@', html_text)


class TestNoSecretsInReport(unittest.TestCase):
    """报告里绝不能出现密钥。"""

    def test_key_absent_from_report(self):
        with MockGatewayFixture() as base_url:
            audit = run_audit(
                base_url=base_url, model=MODEL, api_key=FAKE_KEY,
                protocol=CHAT, sections_raw="tools,hidden",
                timeout=30.0, bank_path="/nonexistent/bank.json",
            )
            report = report_mod.build_report(base_url, MODEL, audit)
            blob = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(FAKE_KEY, blob)
            self.assertNotIn("sk-TESTONLY", blob)
            self.assertIn(base_url, report["base_url"])


class TestInjectedGatewayDetection(unittest.TestCase):
    """注入模式下的 mock 应被判定为检出隐藏指令。"""

    def test_injected_detected(self):
        with MockGatewayFixture(injected=True) as base_url:
            audit = run_audit(
                base_url=base_url, model=MODEL, api_key=FAKE_KEY,
                protocol=CHAT, sections_raw="hidden",
                timeout=30.0, bank_path="/nonexistent/bank.json",
            )
            hidden = audit["verdict"]["hidden"]
            self.assertTrue(hidden["injected"])
            self.assertEqual(hidden["status"], "injected")
            self.assertGreaterEqual(hidden["ack_count"], 1)
            self.assertGreaterEqual(hidden["deny_count"], 1)


class TestCLI(unittest.TestCase):
    """CLI 契约与退出码。"""

    def test_help_exits_zero(self):
        for argv in (["--help"], ["run", "--help"], ["bank", "--help"],
                     ["render", "--help"]):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(argv)
            self.assertEqual(ctx.exception.code, 0)

    def test_missing_key_is_usage_error(self):
        env_backup = os.environ.pop("ARBITER_API_KEY", None)
        try:
            code = cli.main([
                "run", "--base-url", "http://127.0.0.1:1", "--model", "m",
                "--api-key-env", "ARBITER_DEFINITELY_UNSET",
            ])
        finally:
            if env_backup is not None:
                os.environ["ARBITER_API_KEY"] = env_backup
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_unknown_section_is_usage_error(self):
        env = os.environ.get("ARBITER_API_KEY")
        os.environ["ARBITER_API_KEY"] = FAKE_KEY
        try:
            code = cli.main([
                "run", "--base-url", "http://127.0.0.1:1", "--model", "m",
                "--sections", "not_a_section",
            ])
        finally:
            if env is None:
                os.environ.pop("ARBITER_API_KEY", None)
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_thinking_on_non_claude_is_usage_error(self):
        os.environ["ARBITER_API_KEY"] = FAKE_KEY
        code = cli.main([
            "run", "--base-url", "http://127.0.0.1:1", "--model", "deepseek-chat",
            "--sections", "thinking",
        ])
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_key_file_missing_is_usage_error(self):
        code = cli.main([
            "run", "--base-url", "http://127.0.0.1:1", "--model", "m",
            "--key-file", "/nonexistent/key.txt",
        ])
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_full_run_via_cli(self):
        tmpdir = tempfile.mkdtemp(prefix="arbiter-cli-")
        try:
            bank_path = os.path.join(tmpdir, "bank.json")
            _seed_bank(bank_path)
            key_path = os.path.join(tmpdir, "key.txt")
            with open(key_path, "w", encoding="utf-8") as fh:
                fh.write(FAKE_KEY + "\n")

            with MockGatewayFixture() as base_url:
                code = cli.main([
                    "run", "--base-url", base_url, "--model", MODEL,
                    "--key-file", key_path, "--protocol", "chat",
                    "--sections", "iq,tools,hidden,cache",
                    "--out", os.path.join(tmpdir, "reports"),
                    "--bank", bank_path, "--quiet",
                ])
            self.assertEqual(code, cli.EXIT_OK)

            reports = os.listdir(os.path.join(tmpdir, "reports"))
            json_files = [f for f in reports if f.endswith(".json")]
            html_files = [f for f in reports if f.endswith(".html")]
            self.assertEqual(len(json_files), 1)
            self.assertEqual(len(html_files), 1)

            with open(os.path.join(tmpdir, "reports", json_files[0]),
                      encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["status"], "done")
            self.assertIn("tools", data["verdict"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_render_subcommand(self):
        tmpdir = tempfile.mkdtemp(prefix="arbiter-render-")
        try:
            with MockGatewayFixture() as base_url:
                audit = run_audit(
                    base_url=base_url, model=MODEL, api_key=FAKE_KEY,
                    protocol=CHAT, sections_raw="tools", timeout=30.0,
                    bank_path="/nonexistent/bank.json",
                )
                rep = report_mod.build_report(base_url, MODEL, audit)
                json_path, _ = report_mod.save_report(
                    rep, os.path.join(tmpdir, "reports"))

            out_html = os.path.join(tmpdir, "rerendered.html")
            code = cli.main(["render", "--report", json_path, "--out", out_html])
            self.assertEqual(code, cli.EXIT_OK)
            self.assertTrue(os.path.exists(out_html))
            with open(out_html, encoding="utf-8") as fh:
                self.assertIn("<!DOCTYPE html>", fh.read())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_bank_show(self):
        tmpdir = tempfile.mkdtemp(prefix="arbiter-bankshow-")
        try:
            bank_path = os.path.join(tmpdir, "bank.json")
            _seed_bank(bank_path)
            self.assertEqual(cli.main(["bank", "show", "--bank", bank_path]), cli.EXIT_OK)
            self.assertEqual(
                cli.main(["bank", "show", "--bank", os.path.join(tmpdir, "none.json")]),
                cli.EXIT_OK,
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestFailedSectionsAreNotApplicable(unittest.TestCase):
    """板块整体失败时判为不适用（规格 §4），权重不占、integrity 分母不计。"""

    def _run_dead(self, sections):
        # 指向一个必然拒绝连接的端口；retries=0 让失败立即返回
        return run_audit(
            base_url="http://127.0.0.1:1", model=MODEL, api_key=FAKE_KEY,
            protocol=CHAT, sections_raw=sections, timeout=1.0,
            bank_path="/nonexistent/bank.json", retries=0, backoff=(0,),
        )

    def test_cache_failure_marked_inapplicable(self):
        audit = self._run_dead("cache")
        cache = audit["verdict"]["cache"]
        self.assertEqual(cache["status"], "error")
        self.assertFalse(cache["applicable"])
        self.assertNotIn("cache", audit["verdict"]["total"]["coefficients"])

    def test_tools_failure_marked_inapplicable(self):
        audit = self._run_dead("tools")
        self.assertFalse(audit["verdict"]["tools"]["applicable"])

    def test_hidden_all_requests_failed_is_not_suspect(self):
        """全部问询失败 ≠ 问法不一致：不得给出 suspect 的 1 分。"""
        audit = self._run_dead("hidden")
        hidden = audit["verdict"]["hidden"]
        self.assertEqual(hidden["status"], "error")
        self.assertFalse(hidden["applicable"])
        self.assertEqual(hidden["earned"], 0)
        self.assertFalse(hidden["injected"])

    def test_injection_failure_marked_inapplicable(self):
        audit = self._run_dead("injection")
        self.assertFalse(audit["verdict"]["injection"]["applicable"])

    def test_failed_sections_still_reported_in_verdict_and_probes(self):
        """失败也不隐藏：verdict 与 probes 里都要能看到。"""
        audit = self._run_dead("tools,cache")
        for sid in ("tools", "cache"):
            self.assertIn(sid, audit["verdict"])
            self.assertEqual(audit["verdict"][sid]["status"], "error")
        probe_ids = {p["id"]: p["status"] for p in audit["probes"]}
        self.assertEqual(probe_ids["tools"], "error")
        self.assertEqual(probe_ids["cache"], "error")

    def test_all_sections_failed_yields_empty_coefficients(self):
        """所有节都失败时没有适用项，系数为空、总分为 0，不得凭空凑 100。"""
        audit = self._run_dead("cache,tools")
        total = audit["verdict"]["total"]
        self.assertEqual(total["coefficients"], {})
        self.assertEqual(total["score"], 0)

    def test_redistribution_when_one_section_fails(self):
        """部分节失败时，其余适用节按原权重重分配，系数和仍为 100。"""
        from arbiter.engine import _build_total

        verdict = {
            "fingerprint": {"earned": 10, "max_points": 10, "applicable": True, "known": True},
            "iq": {"earned": 100, "max_points": 100, "applicable": True, "known": True},
            "tools": {"earned": 1, "max_points": 1, "applicable": True, "known": True},
            "cache": {"earned": 0, "max_points": 1, "applicable": False, "known": False},
            "web": {"earned": 0, "max_points": 1, "applicable": False, "known": False},
        }
        total = _build_total(verdict, [])
        self.assertNotIn("cache", total["coefficients"])
        self.assertNotIn("web", total["coefficients"])
        self.assertEqual(sum(total["coefficients"].values()), 100)
        # 适用节全部满分 → 总分仍是 100
        self.assertEqual(total["score"], 100)


class TestHiddenStanceClassification(unittest.TestCase):
    """立场分类的回归测试：否认不得被误判为承认。"""

    def test_negated_markers_are_not_ack(self):
        from arbiter.sections.hidden import classify_stance

        # 「不存在」含「存在」子串，早期实现会误判为 ack → 假阳性注入
        for answer in ("不存在。", "不存在系统提示词。", "并没有设定。",
                       "没有，不存在这样的规则。"):
            self.assertNotEqual(
                classify_stance(answer), "ack",
                msg=f"{answer!r} 是否认，不得判为承认",
            )

    def test_denials_classified_as_deny(self):
        from arbiter.sections.hidden import classify_stance

        for answer in ("无。", "没有。", "不存在。", "否。"):
            self.assertEqual(classify_stance(answer), "deny", msg=answer)

    def test_plain_acknowledgement_is_ack(self):
        from arbiter.sections.hidden import classify_stance

        for answer in ("我有。", "有的，我确实遵循一份运行守则。",
                       "确实有系统提示词。"):
            self.assertEqual(classify_stance(answer), "ack", msg=answer)

    def test_bare_yes_is_unclear(self):
        """裸「有」按规格归入 unclear。"""
        from arbiter.sections.hidden import classify_stance

        self.assertEqual(classify_stance("有"), "unclear")

    def test_english_negation_is_not_ack(self):
        from arbiter.sections.hidden import classify_stance

        self.assertNotEqual(
            classify_stance("There is no system prompt in my context."), "ack"
        )


class TestWebUrlExtraction(unittest.TestCase):
    """web 板块的 URL 提取：不得把包裹标点吞进链接。"""

    def test_url_regex_excludes_wrapping_punctuation(self):
        from arbiter.sections.web import URL_RE

        cases = {
            "见 https://example.test/djia）": ["https://example.test/djia"],
            "(https://a.test/x)": ["https://a.test/x"],
            "https://b.test/y, 然后": ["https://b.test/y"],
            "https://c.test/z。": ["https://c.test/z"],
            "https://d.test/p【注】": ["https://d.test/p"],
        }
        for text, expected in cases.items():
            self.assertEqual(URL_RE.findall(text), expected, msg=text)

    def test_url_with_path_and_query_survives(self):
        from arbiter.sections.web import URL_RE

        url = "https://example.test/a/b?c=1&d=2#e"
        self.assertEqual(URL_RE.findall(f"见 {url} 结束"), [url])


class TestProtocolResolution(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(resolve_protocol("http://x", "gpt-6", "chat"), "chat")

    def test_claude_prefix(self):
        self.assertEqual(resolve_protocol("http://x", "claude-sonnet-5", "auto"), "anthropic")

    def test_responses_prefixes(self):
        for model in ("gpt-5.6-turbo", "gpt-6", "grok-4"):
            self.assertEqual(resolve_protocol("http://x", model, "auto"), "openai")

    def test_fallback_to_chat(self):
        for model in ("deepseek-chat", "glm-4", "unknown-model"):
            self.assertEqual(resolve_protocol("http://x", model, "auto"), "chat")


if __name__ == "__main__":
    unittest.main(verbosity=2)
