"""数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Mail:
    """一封邮件（已解码、已落盘 .eml）。"""
    uid: int
    folder: str
    message_id: str
    subject: str
    from_: str
    date: datetime | None
    body_text: str                  # 纯文本正文（已解码）
    body_html: str                  # HTML 正文原文
    raw_path: Path                  # .eml 落盘路径
    is_ads: bool = False            # 场景一：ADS 文献推送
    is_grant: bool = False          # 场景二：基金/项目申报通知


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
