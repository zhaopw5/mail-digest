"""ADS 域模型。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ADSArticle:
    """一条 ADS 文献。"""
    bibcode: str
    title: str = ""
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    citation_count: int | None = None
    doi: str = ""
    pubdate: str = ""
    source: str = "email"           # email | api
    error: str = ""                 # 非空表示该文献信息获取失败的原因
