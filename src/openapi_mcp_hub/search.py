"""零依赖的工具检索：BM25 词法索引（用于 progressive tool discovery）。

为什么需要：`tools/list` 会整个进入模型上下文。Anthropic 的实测数据是五个
server 的工具定义就占约 55k token，且可用工具超过 30–50 个以后选择准确率开始
下降。注册一个真实 API（Stripe 量级约 500 个端点）时，把所有工具一次塞给模型
是**网关自己造成的失效**。

官方解法有两种：把工具定义推迟加载、按需检索（tool search），或者把调用搬进
沙箱执行。这里实现前者中最轻的一种——词法检索，不引入 embedding、不调用模型、
不新增依赖：

- 索引只来自工具自身的文本（名称、路径、描述、参数名），没有外部数据；
- 打分用标准 BM25，纯 Python，几百行以内；
- 中文用字符 bigram 切分，避免引入分词库。

刻意不做的事：不删除工具、不改变 `tools/call` 的解析路径（被检索出来的工具
照旧按名字调用），不改变小服务的默认行为（工具数少时索引不生效）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# ASCII 词 + 中文串；中文串后续再切成 bigram
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
# 常见但无区分度的 REST 词，检索时忽略（否则 "resource"/"api" 命中一切）
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "for", "to", "in", "on", "by", "and", "or", "is", "are",
        "api", "v1", "v2", "v3", "rest", "json", "http", "https", "returns", "return",
        "this", "that", "from", "with", "as", "at", "it", "its",
    }
)

# BM25 参数（业界默认值，未针对本场景调优）
_K1 = 1.2
_B = 0.75


def tokenize(text: str) -> list[str]:
    """切成检索词：ASCII 按词、中文按二元组（bigram）。"""
    tokens: list[str] = []
    for chunk in _TOKEN_RE.findall(text.lower()):
        if chunk[0].isascii():
            if chunk not in _STOPWORDS and len(chunk) > 1:
                tokens.append(chunk)
        elif len(chunk) == 1:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return tokens


@dataclass
class _Doc:
    name: str
    text: str
    # 命中后回给模型的摘要（一行，不重复完整 schema）
    summary: str
    tokens: list[str] = field(default_factory=list)
    tf: dict[str, int] = field(default_factory=dict)


class ToolIndex:
    """一个服务内全部工具的 BM25 索引。"""

    def __init__(self, entries: list[tuple[str, str, str]]):
        """entries: [(工具名, 用于检索的文本, 一行摘要)]"""
        self._docs: list[_Doc] = []
        df: dict[str, int] = {}
        for name, text, summary in entries:
            doc = _Doc(name=name, text=text, summary=summary, tokens=tokenize(text))
            for tok in doc.tokens:
                doc.tf[tok] = doc.tf.get(tok, 0) + 1
            for tok in doc.tf:
                df[tok] = df.get(tok, 0) + 1
            self._docs.append(doc)
        n = len(self._docs)
        self._idf = {tok: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for tok, freq in df.items()}
        self._avgdl = (sum(len(d.tokens) for d in self._docs) / n) if n else 0.0
        self._by_name = {d.name: d for d in self._docs}

    def __len__(self) -> int:
        return len(self._docs)

    def search(self, query: str, limit: int = 8) -> list[tuple[str, float, str]]:
        """返回 [(工具名, 分数, 摘要)]，按分数降序；无命中返回空列表。"""
        terms = tokenize(query)
        if not terms or not self._docs:
            return []
        scored: list[tuple[str, float, str]] = []
        for doc in self._docs:
            dl = len(doc.tokens) or 1
            score = 0.0
            for term in terms:
                tf = doc.tf.get(term)
                if not tf:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = tf + _K1 * (1 - _B + _B * dl / (self._avgdl or 1))
                score += idf * tf * (_K1 + 1) / denom
            # 只匹配到极常见词（idf 接近 0）时分数接近 0，返回它只会干扰模型
            if score > 0.01:
                scored.append((doc.name, score, doc.summary))
        # 分数降序；同分按名字升序，保证顺序确定（利于 prompt cache）
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]

    def summary(self, name: str) -> str:
        doc = self._by_name.get(name)
        return doc.summary if doc else ""
