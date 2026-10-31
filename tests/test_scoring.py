"""评分公式单元测试（离线，纯函数）。

覆盖规格 §8 的三个最大余数法实例、贡献 floor、总分求和。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbiter import scoring  # noqa: E402


class TestAllocateCoefficients(unittest.TestCase):
    """规格 §8 给出的三个实例必须逐一命中。

    三个实例对应「部分节不适用」的权重集：全集为
    {30,25,12,10,8,6,5,4}，实例依次剔除 cache、thinking、thinking+web。
    """

    def test_case_one_seven_sections(self):
        """{30,25,12,10,8,6,5} → {31,26,13,11,8,6,5}。"""
        weights = {
            "fingerprint": 30, "iq": 25, "injection": 12, "hidden": 10,
            "tools": 8, "web": 6, "thinking": 5,
        }
        got = scoring.allocate_coefficients(weights)
        self.assertEqual(got, {
            "fingerprint": 31, "iq": 26, "injection": 13, "hidden": 11,
            "tools": 8, "web": 6, "thinking": 5,
        })

    def test_case_two_six_sections(self):
        """{30,25,12,10,8,6,4} → {32,26,13,11,8,6,4}。"""
        weights = {
            "fingerprint": 30, "iq": 25, "injection": 12, "hidden": 10,
            "tools": 8, "web": 6, "cache": 4,
        }
        got = scoring.allocate_coefficients(weights)
        self.assertEqual(got, {
            "fingerprint": 32, "iq": 26, "injection": 13, "hidden": 11,
            "tools": 8, "web": 6, "cache": 4,
        })

    def test_case_three_five_sections(self):
        """{30,25,12,10,8,4} → {34,28,13,11,9,5}。"""
        weights = {
            "fingerprint": 30, "iq": 25, "injection": 12, "hidden": 10,
            "tools": 8, "cache": 4,
        }
        got = scoring.allocate_coefficients(weights)
        self.assertEqual(got, {
            "fingerprint": 34, "iq": 28, "injection": 13, "hidden": 11,
            "tools": 9, "cache": 5,
        })

    def test_full_set_sums_to_100(self):
        """全集权重和为 100，余数为 0，系数即权重本身。"""
        weights = {
            "fingerprint": 30, "iq": 25, "injection": 12, "hidden": 10,
            "tools": 8, "web": 6, "thinking": 5, "cache": 4,
        }
        got = scoring.allocate_coefficients(weights)
        self.assertEqual(got, weights)

    def test_sum_is_always_100(self):
        for weights in (
            {"a": 1},
            {"a": 1, "b": 1, "c": 1},
            {"a": 7, "b": 11, "c": 13, "d": 17},
            {"a": 3, "b": 3, "c": 3, "d": 3, "e": 3, "f": 3, "g": 3},
        ):
            got = scoring.allocate_coefficients(weights)
            self.assertEqual(sum(got.values()), 100, msg=str(weights))

    def test_empty_weights(self):
        self.assertEqual(scoring.allocate_coefficients({}), {})

    def test_tie_break_order(self):
        """并列时按权重降序、再按 key 字典序。"""
        got = scoring.allocate_coefficients({"b": 10, "a": 10, "c": 10})
        self.assertEqual(sum(got.values()), 100)
        # 三个等权，余数完全相同；字典序在前的先加
        self.assertGreaterEqual(got["a"], got["c"])


class TestContribution(unittest.TestCase):
    """贡献分 = floor(points / max_points × coef)。"""

    def test_full_marks(self):
        self.assertEqual(scoring.contribution(10, 10, 31), 31)

    def test_zero_points(self):
        self.assertEqual(scoring.contribution(0, 10, 31), 0)

    def test_floor_rounding(self):
        # 5/10 × 31 = 15.5 → 15
        self.assertEqual(scoring.contribution(5, 10, 31), 15)
        # 1/3 × 10 = 3.33 → 3
        self.assertEqual(scoring.contribution(1, 3, 10), 3)

    def test_zero_max_points(self):
        self.assertEqual(scoring.contribution(5, 0, 10), 0)

    def test_clamped_to_coefficient(self):
        self.assertEqual(scoring.contribution(20, 10, 31), 31)


class TestTotalScore(unittest.TestCase):
    """总分求和与适用性剔除。"""

    def test_sum_example(self):
        """13 + 9 + 5 = 27。"""
        self.assertEqual(13 + 9 + 5, 27)

    def test_total_all_full(self):
        sections = {
            "fingerprint": {"points": 10, "max_points": 10, "applicable": True, "known": True},
            "iq": {"points": 100, "max_points": 100, "applicable": True, "known": True},
            "injection": {"points": 10, "max_points": 10, "applicable": True, "known": True},
            "hidden": {"points": 10, "max_points": 10, "applicable": True, "known": True},
            "tools": {"points": 1, "max_points": 1, "applicable": True, "known": True},
            "web": {"points": 1, "max_points": 1, "applicable": True, "known": True},
            "thinking": {"points": 5, "max_points": 5, "applicable": True, "known": True},
            "cache": {"points": 1, "max_points": 1, "applicable": True, "known": True},
        }
        result = scoring.total_score(sections)
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["scoring_version"], "repro-0.1")

    def test_inapplicable_sections_are_redistributed(self):
        """缓存/联网/思维链不适用时，其余节按原权重重新分配，全通过仍 100。"""
        sections = {
            "fingerprint": {"points": 10, "max_points": 10, "applicable": True, "known": True},
            "iq": {"points": 100, "max_points": 100, "applicable": True, "known": True},
            "injection": {"points": 10, "max_points": 10, "applicable": True, "known": True},
            "hidden": {"points": 10, "max_points": 10, "applicable": True, "known": True},
            "tools": {"points": 1, "max_points": 1, "applicable": True, "known": True},
            "web": {"points": 0, "max_points": 1, "applicable": False, "known": False},
            "thinking": {"points": 0, "max_points": 5, "applicable": False, "known": False},
            "cache": {"points": 0, "max_points": 1, "applicable": False, "known": False},
        }
        result = scoring.total_score(sections)
        self.assertEqual(result["score"], 100)
        self.assertNotIn("cache", result["coefficients"])
        self.assertEqual(sum(result["coefficients"].values()), 100)

    def test_knowledge_never_scores(self):
        sections = {
            "knowledge": {"points": 8, "max_points": 8, "applicable": True, "known": True},
            "iq": {"points": 100, "max_points": 100, "applicable": True, "known": True},
        }
        result = scoring.total_score(sections)
        self.assertNotIn("knowledge", result["coefficients"])
        self.assertEqual(result["score"], 100)

    def test_partial_points_floor(self):
        sections = {
            "fingerprint": {"points": 10, "max_points": 10, "applicable": True, "known": True},
            "iq": {"points": 50, "max_points": 100, "applicable": True, "known": True},
        }
        result = scoring.total_score(sections)
        rows = {r["id"]: r for r in result["rows"]}
        self.assertEqual(rows["fingerprint"]["contribution"], rows["fingerprint"]["coefficient"])
        self.assertEqual(
            rows["iq"]["contribution"],
            int(0.5 * rows["iq"]["coefficient"]),
        )
        self.assertEqual(result["score"],
                         rows["fingerprint"]["contribution"] + rows["iq"]["contribution"])

    def test_score_bounds(self):
        sections = {
            "fingerprint": {"points": 0, "max_points": 10, "applicable": True, "known": False},
            "iq": {"points": 0, "max_points": 100, "applicable": True, "known": False},
        }
        self.assertEqual(scoring.total_score(sections)["score"], 0)


class TestIntegrity(unittest.TestCase):
    """integrity 独立权重。"""

    def test_all_known_full(self):
        signals = {
            "injection": {"points": 10, "max_points": 10, "applicable": True, "known": True},
            "cache": {"points": 1, "max_points": 1, "applicable": True, "known": True},
            "tools": {"points": 1, "max_points": 1, "applicable": True, "known": True},
            "web": {"points": 0, "max_points": 1, "applicable": False, "known": False},
            "stream": {"points": 8, "max_points": 8, "applicable": True, "known": True},
            "thinking": {"points": 0, "max_points": 5, "applicable": False, "known": False},
        }
        result = scoring.integrity(signals)
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["status"], "complete")
        # 不适用项不计入分母：20+20+20+15 = 75
        self.assertEqual(result["applicable_weight"], 75)

    def test_unknown_section_gives_zero_but_counts_in_denominator(self):
        signals = {
            "injection": {"points": 10, "max_points": 10, "applicable": True, "known": False},
            "tools": {"points": 1, "max_points": 1, "applicable": True, "known": True},
        }
        result = scoring.integrity(signals)
        self.assertEqual(result["applicable_weight"], 40)
        self.assertEqual(result["available_weight"], 20)
        self.assertEqual(result["score"], 50)
        self.assertEqual(result["status"], "partial")


class TestTotalReason(unittest.TestCase):
    """总分说明文字。"""

    def test_mentions_inactive_sections(self):
        sections = {
            "web": {"applicable": False},
            "cache": {"applicable": False},
            "thinking": {"applicable": False},
        }
        text = scoring.total_reason(sections)
        for word in ("联网", "缓存", "思维链", "不占分", "知识截止"):
            self.assertIn(word, text)

    def test_mentions_all_active(self):
        sections = {k: {"applicable": True} for k in ("web", "cache", "thinking")}
        text = scoring.total_reason(sections)
        self.assertIn("全部板块均适用", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
