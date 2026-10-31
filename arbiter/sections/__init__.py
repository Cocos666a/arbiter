"""九板块审计模块。

每个模块导出 `run(ctx) -> dict`，返回该节 verdict 子对象。
约定（规格 §3）：单请求异常写进 attempts/err，不使整个审计崩溃；
section 级异常由 engine 捕获并标记 `status:"error"`。
"""

SECTION_IDS = [
    "fingerprint",
    "injection",
    "hidden",
    "cache",
    "tools",
    "web",
    "knowledge",
    "thinking",
    "iq",
]

SECTION_NAMES = {
    "fingerprint": "模型指纹归因",
    "injection": "注入大小测量",
    "hidden": "隐藏指令检测",
    "cache": "缓存读写验证",
    "tools": "工具调用检测",
    "web": "服务端联网检测",
    "knowledge": "知识截止估计",
    "thinking": "思维链回传",
    "iq": "推理表现测试",
}

# 不计入总分的板块
NON_SCORING = {"knowledge"}

__all__ = ["SECTION_IDS", "SECTION_NAMES", "NON_SCORING"]
