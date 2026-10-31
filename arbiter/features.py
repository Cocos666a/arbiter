"""指纹数值特征：从模型输出的数字串提取特征向量并计算相似度。

规格 §3.1：
- 20 桶直方图（值域 [1, M] 归一化）+ 均值/M + 标准差/M + 重复率 + 相邻差值重复率；
- 向量做 L2 归一化；
- 相似度为余弦相似度。
"""

from __future__ import annotations

import math
import re
from collections import Counter

# 特征维度：20 桶 + 4 个标量
HIST_BINS = 20
FEATURE_DIM = HIST_BINS + 4

# 提取整数：允许千分位之外的一切十进制整数，负数取其绝对值前的符号也一并匹配
_INT_RE = re.compile(r"-?\d+")


def parse_integers(text: str) -> list:
    """从回复文本里解析全部整数。

    数字串里常见的分隔符（逗号、空格、换行、顿号）不影响匹配；
    带千分位的 `1,234` 会先剥离逗号再解析。
    """
    if not text:
        return []
    # 去掉数字之间的千分位逗号：仅当逗号两侧都是数字时移除
    cleaned = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    out = []
    for token in _INT_RE.findall(cleaned):
        try:
            out.append(int(token))
        except ValueError:
            continue
    return out


def parse_count(text: str) -> int:
    """解析出的整数个数。"""
    return len(parse_integers(text))


def usable_sample(parsed_count: int, expected_count: int) -> bool:
    """样本是否可用（规格 §3.1）。

    200 ≤ parsed ≤ N+40 且解析到 ≥100 个数字。
    """
    if parsed_count < 100:
        return False
    if parsed_count < 200:
        return False
    return parsed_count <= expected_count + 40


def feature_vector(values: list, m: int = 400) -> list:
    """把整数列表转成 L2 归一化后的特征向量。

    维度：20 桶直方图 + [均值/M, 标准差/M, 重复率, 相邻差值重复率]。
    空输入返回全零向量。
    """
    vec = [0.0] * FEATURE_DIM
    if not values:
        return vec

    m = max(1, int(m))
    n = len(values)
    values = [abs(v) for v in values]

    # --- 20 桶直方图：值域 [1, M] 归一化 ---
    for v in values:
        if v < 1:
            idx = 0
        elif v >= m:
            idx = HIST_BINS - 1
        else:
            idx = int((v - 1) / m * HIST_BINS)
            idx = min(max(idx, 0), HIST_BINS - 1)
        vec[idx] += 1.0
    for i in range(HIST_BINS):
        vec[i] /= n

    # --- 均值 / 标准差（按 M 归一化） ---
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    vec[HIST_BINS] = mean / m
    vec[HIST_BINS + 1] = math.sqrt(var) / m

    # --- 重复率：1 - 唯一值占比 ---
    unique_ratio = len(set(values)) / n
    vec[HIST_BINS + 2] = 1.0 - unique_ratio

    # --- 相邻差值重复率：1 - 唯一差值占比 ---
    if n > 1:
        diffs = [values[i + 1] - values[i] for i in range(n - 1)]
        diff_unique_ratio = len(set(diffs)) / len(diffs)
        vec[HIST_BINS + 3] = 1.0 - diff_unique_ratio
    else:
        vec[HIST_BINS + 3] = 0.0

    return l2_normalize(vec)


def l2_normalize(vec: list) -> list:
    """L2 归一化；零向量原样返回。"""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0:
        return list(vec)
    return [x / norm for x in vec]


def cosine(a: list, b: list) -> float:
    """余弦相似度；任一为零向量返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na <= 0 or nb <= 0:
        return 0.0
    # 数值裁剪，避免浮点误差越界
    return max(-1.0, min(1.0, dot / (na * nb)))


def mean_vector(vectors: list) -> list:
    """对多个样本特征取均值，作为该模型的中心向量。"""
    vectors = [v for v in vectors if v]
    if not vectors:
        return [0.0] * FEATURE_DIM
    dim = len(vectors[0])
    acc = [0.0] * dim
    for vec in vectors:
        for i in range(min(dim, len(vec))):
            acc[i] += vec[i]
    return [x / len(vectors) for x in acc]


def softmax(values: list, scale: float = 1.0) -> list:
    """数值稳定的 softmax。"""
    if not values:
        return []
    scaled = [v * scale for v in values]
    top = max(scaled)
    exps = [math.exp(v - top) for v in scaled]
    total = sum(exps)
    if total <= 0:
        return [0.0] * len(values)
    return [e / total for e in exps]


def duplicate_rate(values: list) -> float:
    """重复率：1 - 唯一值占比。"""
    if not values:
        return 0.0
    return 1.0 - len(set(values)) / len(values)


def difference_duplicate_rate(values: list) -> float:
    """相邻差值重复率。"""
    if len(values) < 2:
        return 0.0
    diffs = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    return 1.0 - len(set(diffs)) / len(diffs)


def top_counter(values: list, k: int = 3) -> list:
    """出现次数最多的前 k 个值，用于报告展示。"""
    return Counter(values).most_common(k)
