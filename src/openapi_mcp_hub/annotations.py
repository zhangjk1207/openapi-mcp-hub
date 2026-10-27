"""按 HTTP 语义推断 MCP 工具标注（ToolAnnotations）。

为什么值得做：agent 端看到 ``list_pets`` 和 ``delete_pet_by_id`` 时，若没有
``readOnlyHint``，只能靠工具名去猜哪个会改数据，于是要么过度谨慎（每次只读都
要人确认），要么过度自信（直接删）。MCP 协议标准里就有这几个提示字段，填上
几乎零成本。

安全原则（保守优先）：
- MCP 规范里 ``destructiveHint`` 与 ``openWorldHint`` 的默认值都是 **true**，
  所以"不填"本身就是危险侧；我们只在有把握时把危险侧改成 false。
- 拿不准的 POST/PATCH 一律按 destructive 处理（宁可让 agent 多问一句）。
- 只有 GET/HEAD 才标 readOnlyHint=True —— 这是本模块唯一"放宽"的判断，
  因为 HTTP 方法语义对只读是可靠信号。
"""

from __future__ import annotations

import re
from typing import Any

# 明确表示"新增/追加"的动词：符合 additive，不算 destructive
_ADDITIVE = re.compile(
    r"(?:^|_)(?:create|add|new|register|upload|append|import|insert|send|submit|"
    r"post|put_file|attach)(?:$|_)",
    re.IGNORECASE,
)
# 明确表示"会毁数据/覆盖"的动词，即使方法是 POST 也按 destructive 处理
_DESTRUCTIVE = re.compile(
    r"(?:^|_)(?:delete|remove|drop|purge|clear|reset|destroy|revoke|truncate|"
    r"overwrite|replace|kill|terminate|force)(?:$|_)",
    re.IGNORECASE,
)

_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_DESTRUCTIVE_METHODS = frozenset({"DELETE", "PUT", "PATCH"})


def infer_annotations(
    method: str,
    name: str = "",
    path: str = "",
    *,
    closed_world: bool = True,
) -> dict[str, Any]:
    """推断一个工具的 MCP 标注。

    参数：
    - method: HTTP 方法（GET/POST/...）
    - name / path: 用于动词启发式（方法语义不够时兜底）
    - closed_world: True 表示目标域是固定的（单 base_url 的 REST API、或某个库），
      False 表示会访问开放世界（例如通用搜索类接口）。

    返回值直接塞给 ``mcp.types.ToolAnnotations``。
    """
    verb = method.upper()
    haystack = f"{name}_{path}"

    read_only = verb in _READ_ONLY_METHODS
    if read_only:
        return {
            "readOnlyHint": True,
            # readOnly 时 destructive 无意义，显式给 False 以免客户端叠加默认值
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": not closed_world,
        }

    if verb in _DESTRUCTIVE_METHODS:
        destructive = True
    elif verb == "POST":
        # POST 语义最模糊：只有明确的 additive 动词才降级为「仅新增」，
        # 否则按 destructive 处理（与 MCP 规范的默认值一致）。
        if _DESTRUCTIVE.search(haystack):
            destructive = True
        else:
            destructive = not _ADDITIVE.search(haystack)
    else:
        destructive = bool(_DESTRUCTIVE.search(haystack))

    return {
        "readOnlyHint": False,
        "destructiveHint": destructive,
        "idempotentHint": verb in _IDEMPOTENT_METHODS,
        "openWorldHint": not closed_world,
    }


def annotations_to_dict(annotations: dict[str, Any]) -> dict[str, Any]:
    """转成 mcp.types.ToolAnnotations 可直接展开的 kwargs（已对齐字段名）。"""
    mapping = {
        "readOnlyHint": "read_only_hint",
        "destructiveHint": "destructive_hint",
        "idempotentHint": "idempotent_hint",
        "openWorldHint": "open_world_hint",
    }
    return {mapping[k]: v for k, v in annotations.items() if k in mapping}
