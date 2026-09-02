"""邮件分类：判断一封邮件是否属于某个处理场景。

目前只有「ADS 文献推送」（场景一）；后续项目二会加「项目申报通知」。

判定信号强度（由强到弱）：
  1. 正文含 ADS 文献链接（ui.adsabs.harvard.edu/abs/）→ 必是
  2. 发件人来自 ADS 官方域（adsabs.harvard.edu / cfa.harvard.edu）
     → 是，但排除「验证/欢迎」类系统邮件（不含文献链接）
  3. 主题含文献推送特征词 → 是
"""
from __future__ import annotations

import re

from .models import Mail

_ADS_LINK_RE = re.compile(r"ui\.adsabs\.harvard\.edu/abs/")
_ADS_DOMAIN_HINTS = ("adsabs.harvard.edu", "cfa.harvard.edu")
_ADS_EXCLUDE_SUBJECT = ("verify", "welcome", "confirm your email")
# 我们自己推送/测试的邮件（主题特征），正文虽含 ADS 链接但不能当推送再处理（防循环）
_ADS_SELF_PUSH_HINTS = ("ads 文献简报", "[mail-digest]")
_ADS_SUBJECT_HINTS = (
    "astrophysics data system", "new article", "toc alert",
    "table of contents", "citation alert", "notification criteria",
    "new notifications",
)


def is_ads_email(mail: Mail) -> bool:
    subject_lower = mail.subject.lower()
    # 自推送邮件排除（防循环：正文含 ADS 链接的邮件可能是我们发出的简报）
    if any(w in subject_lower for w in _ADS_SELF_PUSH_HINTS):
        return False
    blob = f"{mail.body_html}\n{mail.body_text}"
    if _ADS_LINK_RE.search(blob):
        return True
    from_lower = mail.from_.lower()
    if any(d in from_lower for d in _ADS_DOMAIN_HINTS):
        if any(w in subject_lower for w in _ADS_EXCLUDE_SUBJECT):
            return False
        return True
    return any(h in subject_lower for h in _ADS_SUBJECT_HINTS)


def classify(mail: Mail) -> None:
    """给 Mail 打上场景标签（就地修改）。"""
    mail.is_ads = is_ads_email(mail)
