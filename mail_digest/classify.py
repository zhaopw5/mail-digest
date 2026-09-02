"""邮件分类：判断一封邮件属于哪个处理场景。

场景一：ADS 文献推送（is_ads）——按 ADS 链接/发件人/主题信号识别。
场景二：基金/项目申报通知（is_grant）——按主题关键词识别，不看发件人。

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


# ---------- 场景二：基金/项目申报通知 ----------
# 强信号词：命中任一即视为基金/项目申报类
_GRANT_STRONG = (
    "申报指南", "申报通知", "组织申报", "项目申报", "基金",
    "重点研发计划", "科技计划项目", "专项项目", "立项", "揭榜挂帅",
    "申报意向", "申报工作", "申请指南", "项目指南",
)
# 弱信号：需与"项目/指南/需求/专项"搭配
_GRANT_WEAK = ("征集", "指南", "项目", "通知", "通告")
# 排除词：命中任一即不是项目申报机会
_GRANT_EXCLUDE = (
    "考核", "培训", "讲座", "论坛", "会议通知", "报销", "体检",
    "安全", "党建", "消防", "专家库", "专家征集", "专家", "答辩", "评审结果",
    "立项结果", "结题", "验收", "汇报", "开题", "中期检查", "调研邀请",
)


def is_grant_email(mail: Mail) -> bool:
    """按主题关键词识别基金/项目申报通知（不看发件人）。"""
    subj = mail.subject or ""
    if len(subj) < 4:
        return False
    if any(w in subj for w in _GRANT_EXCLUDE):
        return False
    if any(w in subj for w in _GRANT_STRONG):
        return True
    # 弱信号：征集/指南 + 项目语境
    hits = sum(1 for w in _GRANT_WEAK if w in subj)
    return hits >= 2


def classify(mail: Mail) -> None:
    """给 Mail 打上场景标签（就地修改）；两个场景互斥标记。"""
    mail.is_ads = is_ads_email(mail)
    mail.is_grant = is_grant_email(mail)
    if mail.is_ads:
        mail.is_grant = False
