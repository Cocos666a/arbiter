"""指纹特征与相似度单元测试。"""

from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbiter import features as F  # noqa: E402

M = 400


class TestParseIntegers(unittest.TestCase):
    def test_basic_list(self):
        self.assertEqual(F.parse_integers("12, 34, 56"), [12, 34, 56])

    def test_multiline_and_punctuation(self):
        text = "1, 2\n3、4；5"
        self.assertEqual(F.parse_integers(text), [1, 2, 3, 4, 5])

    def test_thousands_separator(self):
        self.assertEqual(F.parse_integers("1,234 和 56"), [1234, 56])

    def test_empty(self):
        self.assertEqual(F.parse_integers(""), [])
        self.assertEqual(F.parse_integers("没有数字"), [])

    def test_negative(self):
        self.assertEqual(F.parse_integers("-7, 8"), [-7, 8])

    def test_parse_count(self):
        self.assertEqual(F.parse_count("1, 2, 3"), 3)


class TestUsableSample(unittest.TestCase):
    """可用性判定：200 ≤ parsed ≤ N+40 且 ≥100 个数字。"""

    def test_ok(self):
        self.assertTrue(F.usable_sample(300, 300))

    def test_below_200(self):
        self.assertFalse(F.usable_sample(150, 300))

    def test_above_tolerance(self):
        self.assertFalse(F.usable_sample(360, 300))

    def test_at_tolerance_boundary(self):
        self.assertTrue(F.usable_sample(340, 300))

    def test_zero(self):
        self.assertFalse(F.usable_sample(0, 300))


class TestFeatureVector(unittest.TestCase):
    def test_dimension(self):
        vec = F.feature_vector(list(range(1, 301)), M)
        self.assertEqual(len(vec), F.FEATURE_DIM)

    def test_l2_normalized(self):
        vec = F.feature_vector([random.randint(1, M) for _ in range(300)], M)
        norm = sum(x * x for x in vec) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=9)

    def test_empty_returns_zeros(self):
        vec = F.feature_vector([], M)
        self.assertEqual(len(vec), F.FEATURE_DIM)
        self.assertEqual(sum(vec), 0.0)

    def test_uniform_beats_skewed(self):
        """同分布相似度应显著高于异分布。"""
        rng = random.Random(7)
        base = [rng.randint(1, M) for _ in range(300)]
        same = [rng.randint(1, M) for _ in range(300)]
        skewed = [rng.randint(380, M) for _ in range(300)]

        v_base = F.feature_vector(base, M)
        v_same = F.feature_vector(same, M)
        v_skewed = F.feature_vector(skewed, M)

        sim_same = F.cosine(v_base, v_same)
        sim_skewed = F.cosine(v_base, v_skewed)

        # 同分布接近 1；窄区间分布只落在末桶，直方图项完全不相交
        self.assertGreater(sim_same, 0.9)
        self.assertLess(sim_skewed, 0.85)
        self.assertGreater(sim_same - sim_skewed, 0.25)

    def test_identical_vectors_similarity_one(self):
        values = [1, 2, 3] * 100
        vec = F.feature_vector(values, M)
        self.assertAlmostEqual(F.cosine(vec, vec), 1.0, places=9)

    def test_zero_vector_similarity(self):
        self.assertEqual(F.cosine([0.0] * F.FEATURE_DIM, [1.0] * F.FEATURE_DIM), 0.0)
        self.assertEqual(F.cosine([], []), 0.0)


class TestStatistics(unittest.TestCase):
    def test_duplicate_rate(self):
        self.assertAlmostEqual(F.duplicate_rate([1, 1, 2, 2]), 0.5)
        self.assertEqual(F.duplicate_rate([1, 2, 3]), 0.0)
        self.assertEqual(F.duplicate_rate([]), 0.0)

    def test_difference_duplicate_rate(self):
        # 差值 [1,1,1] → 唯一 1 个 / 共 3 个 → 重复率 1 - 1/3
        self.assertAlmostEqual(
            F.difference_duplicate_rate([1, 2, 3, 4]), 1 - 1 / 3, places=9
        )
        self.assertEqual(F.difference_duplicate_rate([5]), 0.0)

    def test_mean_vector(self):
        v1 = [1.0, 0.0]
        v2 = [0.0, 1.0]
        self.assertEqual(F.mean_vector([v1, v2]), [0.5, 0.5])

    def test_mean_vector_empty_is_zero_dim(self):
        """空输入返回全零的满维度向量（调用方无需再补维度）。"""
        vec = F.mean_vector([])
        self.assertEqual(len(vec), F.FEATURE_DIM)
        self.assertEqual(sum(vec), 0.0)

    def test_softmax_sums_to_one(self):
        probs = F.softmax([1.0, 2.0, 3.0], scale=8.0)
        self.assertAlmostEqual(sum(probs), 1.0, places=9)
        self.assertGreater(probs[2], probs[0])

    def test_softmax_empty(self):
        self.assertEqual(F.softmax([]), [])

    def test_softmax_stable_with_large_values(self):
        probs = F.softmax([1000.0, 1001.0], scale=8.0)
        self.assertAlmostEqual(sum(probs), 1.0, places=9)

    def test_top_counter(self):
        self.assertEqual(F.top_counter([3, 3, 1, 2])[0], (3, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
