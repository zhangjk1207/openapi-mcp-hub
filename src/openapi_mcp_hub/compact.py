"""工具描述与输入 schema 的体积压缩（零依赖、零配置、纯机械变换）。

动机：`tools/list` 的结果会整个进入 LLM 上下文，token 大头不是工具名，而是
每个工具的 description 和 schema 里逐字段的说明文字。真实 OpenAPI 里这类文字
通常包含大量对 agent 无用的内容（换行缩进、HTML、示例链接、重复的 summary）。

这里的变换只做「不改变语义」的裁剪：
- 折叠空白、剥离 HTML/Markdown 装饰；
- 超长描述按句子边界截断并显式标注被截断；
- schema 内 `title` 这类纯展示字段丢弃，超长字段说明截断。

不做的事（刻意）：不合并/删除工具、不改 enum 取值、不改 required、不猜默认值。
任何会改变调用行为的字段一律原样保留。
"""

from __future__ import annotations

import re
from typing import Any

# 工具描述上限（字符）。经验值：单工具描述控制在 300 字符以内，
# 100 个工具的描述总量约 7.5k 字符，对上下文友好。
TOOL_DESCRIPTION_LIMIT = 320
# schema 中单个字段说明的上限
FIELD_DESCRIPTION_LIMIT = 160
# 丢弃的纯展示性 schema 键（不影响校验与调用）
_DISPLAY_ONLY_KEYS = ("title", "$comment", "$schema", "$id")

_HTML_TAG = re.compile(r"<[^>]{1,200}>")
_MD_LINK = re.compile(r"\[([^\]]{0,200})\]\((?:[^)]{0,500})\)")
_MD_EMPHASIS = re.compile(r"(\*\*|__|`{1,3})")
_WS = re.compile(r"\s+")
# CJK 句末标点后面常常不带空格，ASCII 句点必须跟空白才算（避免把 "v1.2" 当句尾）
_SENTENCE_END = re.compile(r"(?<=[。！？；])|(?<=[.!?;])\s+")

# 明显的模板化/无信息量句子，出现在描述里只会消耗 token
_BOILERPLATE = re.compile(
    r"^(?:"
    r"returns?\b.*\b(?:list|array|object|response|result)s?\b.*"
    r"|this (?:endpoint|api|method|operation)\b.*"
    r"|please (?:note|refer|see)\b.*"
    r"|see (?:also|the)\b.*"
    r"|for more information\b.*"
    r"|read more\b.*"
    r")$",
    re.IGNORECASE,
)


def _unmark(text: str) -> str:
    """剥离 HTML 标签与 Markdown 装饰，保留可读文本。"""
    text = _MD_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub(" ", text)
    text = _MD_EMPHASIS.sub("", text)
    return text


def _drop_boilerplate(sentences: list[str]) -> list[str]:
    """去掉无信息量的句子；至少保留一句，避免把描述清空。"""
    kept = [s for s in sentences if not _BOILERPLATE.match(s.strip())]
    return kept or sentences


def compact_text(text: str | None, limit: int) -> str:
    """把任意说明文字压成单段、不超过 limit 字符的纯文本。

    超限时在句子边界截断并追加省略号，明确告诉读者（和 agent）内容被截断。
    """
    if not text:
        return ""
    cleaned = _WS.sub(" ", _unmark(text)).strip()
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    sentences = _drop_boilerplate([s for s in _SENTENCE_END.split(cleaned) if s.strip()])
    budget = limit - 1  # 预留省略号
    out = ""
    for sentence in sentences:
        candidate = f"{out} {sentence}".strip() if out else sentence
        if len(candidate) <= budget:
            out = candidate
            continue
        if not out:  # 第一句就超长：在这句内部硬截断到词边界
            cut = sentence[:budget]
            space = cut.rfind(" ")
            out = cut[:space] if space > budget * 0.6 else cut
        break
    out = out.rstrip(" ,;、，")
    return (out + "…") if out else cleaned[:budget] + "…"


def compact_description(text: str | None) -> str:
    return compact_text(text, TOOL_DESCRIPTION_LIMIT)


def compact_schema(value: Any, depth: int = 0) -> Any:
    """递归压缩 schema：丢弃展示性键、截断字段说明，其余原样保留。

    只处理 dict/list，标量原样返回；深度不设限（schema 本身已被 loader 解析过，
    不存在循环引用）。
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in _DISPLAY_ONLY_KEYS:
                continue
            if key == "description" and isinstance(item, str):
                out[key] = compact_text(item, FIELD_DESCRIPTION_LIMIT)
                continue
            out[key] = compact_schema(item, depth + 1)
        return out
    if isinstance(value, list):
        return [compact_schema(x, depth + 1) for x in value]
    return value


def estimate_chars(tool_dict: dict[str, Any]) -> int:
    """粗估一个工具在 tools/list 中占用的字符数（用于测试与诊断）。"""
    desc = tool_dict.get("description") or ""
    schema = tool_dict.get("inputSchema") or tool_dict.get("input_schema") or {}
    return len(desc) + len(str(schema))
