"""指纹基线库读写（bank/bank.json）。

基线结构：
```
{
  "version": "repro-0.1",
  "created_at": "ISO8601",
  "entries": {
    "<model>": {
      "model": "...",
      "meta": {"family": "...", "family_name": "..."},
      "samples": [{"n": 300, "parsed_count": 300, "vector": [...]}, ...]
    }
  }
}
```
读取时对缺失/损坏字段做容错，返回空库而不是抛异常。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .features import FEATURE_DIM, mean_vector

BANK_VERSION = "repro-0.1"
DEFAULT_BANK_PATH = "bank/bank.json"


class Bank:
    """指纹基线库。内存对象，显式 save 落盘。"""

    def __init__(self, path: str = DEFAULT_BANK_PATH, entries: dict | None = None,
                 created_at: str | None = None) -> None:
        self.path = path
        self.entries = entries or {}
        self.created_at = created_at or _now_iso()

    # -- 读取 ---------------------------------------------------------------

    @classmethod
    def load(cls, path: str = DEFAULT_BANK_PATH) -> "Bank":
        """从磁盘加载；文件不存在或损坏时返回空库。"""
        if not path or not os.path.exists(path):
            return cls(path=path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls(path=path)
        entries = data.get("entries") or {}
        if not isinstance(entries, dict):
            entries = {}
        return cls(path=path, entries=entries, created_at=data.get("created_at"))

    # -- 写入 ---------------------------------------------------------------

    def save(self, path: str | None = None) -> str:
        target = path or self.path
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {
            "version": BANK_VERSION,
            "created_at": self.created_at,
            "entries": self.entries,
        }
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, target)
        return target

    # -- 条目操作 -----------------------------------------------------------

    def has(self, model: str) -> bool:
        return bool(model and model in self.entries)

    def get(self, model: str) -> dict:
        return self.entries.get(model or "", {}) or {}

    def put(self, model: str, samples: list, meta: dict | None = None) -> dict:
        """写入/覆盖某模型的基线样本。

        samples 为 `[{n, parsed_count, vector}]`，中心向量由样本均值现场计算。
        """
        vectors = [s.get("vector") for s in samples if s.get("vector")]
        entry = {
            "model": model,
            "meta": meta or self.get(model).get("meta") or {},
            "samples": samples,
            "center": mean_vector(vectors) if vectors else [0.0] * FEATURE_DIM,
            "updated_at": _now_iso(),
        }
        self.entries[model] = entry
        return entry

    def models(self) -> list:
        return sorted(self.entries.keys())

    def center_of(self, model: str) -> list:
        """取模型中心向量；缺 center 时由 samples 现算。"""
        entry = self.get(model)
        center = entry.get("center")
        if center:
            return center
        vectors = [s.get("vector") for s in entry.get("samples") or [] if s.get("vector")]
        return mean_vector(vectors) if vectors else []

    def family_of(self, model: str) -> tuple:
        """返回 (family, family_name)。"""
        meta = self.get(model).get("meta") or {}
        return meta.get("family") or "unknown", meta.get("family_name") or "未知"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
