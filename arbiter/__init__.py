"""Arbiter：OpenAI 兼容网关「掺水」自检工具。

提供九板块审计（指纹/注入/隐藏指令/缓存/工具/联网/知识截止/思维链/推理），
输出 report.json 与静态 report.html。仅依赖 Python 标准库。
"""

__version__ = "0.1.0"
